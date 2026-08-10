import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, AsyncIterator
from uuid import uuid4

logger = logging.getLogger(__name__)

SIGNAL_HASH_TTL_SEC = 60 * 60 * 24 * 14
STOCK_LOCK_TTL_SEC = 60 * 10
SCHEDULER_LOCK_TTL_SEC = 60 * 30
COOLDOWN_TTL_SEC = 60 * 10
TELEGRAM_ALERT_MODES = {"urgent", "all", "off"}
DEFAULT_TELEGRAM_ALERT_MODE = "urgent"

# pending_order TTL: 10분(600초).
# 앱 레벨 ORDER_EXPIRES_AFTER(60초)는 별도 존재하며 직접 만료 체크를 수행한다.
# Redis TTL은 프로세스 크래시·재시작 후 stale 키를 자동 정리하는 안전망 역할이다.
# 이슈 #63이 "5~10분" 범위를 제안했고, ORDER_EXPIRES_AFTER의 10× 여유를 택한다.
PENDING_ORDER_TTL_SEC = 60 * 10


def normalize_signal_text(signal_text: str) -> str:
    lines = []
    for line in (signal_text or "").splitlines():
        normalized = re.sub(r"\s+", " ", line).strip().casefold()
        if normalized:
            lines.append(normalized)
    return "\n".join(sorted(set(lines)))


def signal_hash(signal_text: str) -> str:
    normalized = normalize_signal_text(signal_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RedisKeys:
    prefix: str = "finus"

    def last_hash(self, source: str, stock: str) -> str:
        return f"{self.prefix}:signal:last_hash:{source}:{stock}"

    def last_text(self, source: str, stock: str) -> str:
        return f"{self.prefix}:signal:last_text:{source}:{stock}"

    def analysis_lock(self, source: str, stock: str) -> str:
        return f"{self.prefix}:analysis:lock:{source}:{stock}"

    def analysis_cooldown(self, source: str, stock: str) -> str:
        return f"{self.prefix}:analysis:cooldown:{source}:{stock}"

    def scheduler_lock(self, job_name: str = "market_monitoring") -> str:
        return f"{self.prefix}:scheduler:lock:{job_name}"

    def telegram_alert_mode(self) -> str:
        return f"{self.prefix}:telegram:alert_mode"

    def pending_order(self, chat_id: str) -> str:
        return f"{self.prefix}:pending_order:{chat_id}"


class RedisSchedulerState:
    def __init__(
        self,
        redis: Any,
        *,
        keys: RedisKeys | None = None,
        signal_hash_ttl_sec: int = SIGNAL_HASH_TTL_SEC,
        stock_lock_ttl_sec: int = STOCK_LOCK_TTL_SEC,
        scheduler_lock_ttl_sec: int = SCHEDULER_LOCK_TTL_SEC,
        cooldown_ttl_sec: int = COOLDOWN_TTL_SEC,
    ):
        self.redis = redis
        self.keys = keys or RedisKeys()
        self.signal_hash_ttl_sec = signal_hash_ttl_sec
        self.stock_lock_ttl_sec = stock_lock_ttl_sec
        self.scheduler_lock_ttl_sec = scheduler_lock_ttl_sec
        self.cooldown_ttl_sec = cooldown_ttl_sec

    async def get_last_signal_hash(self, source: str, stock: str) -> str | None:
        value = await self.redis.get(self.keys.last_hash(source, stock))
        return self._decode(value)

    async def get_last_signal_text(self, source: str, stock: str) -> str | None:
        value = await self.redis.get(self.keys.last_text(source, stock))
        return self._decode(value)

    @staticmethod
    def _decode(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def set_last_signal(self, source: str, stock: str, signal_text: str, digest: str) -> None:
        await self.redis.set(self.keys.last_hash(source, stock), digest, ex=self.signal_hash_ttl_sec)
        await self.redis.set(self.keys.last_text(source, stock), signal_text, ex=self.signal_hash_ttl_sec)

    async def in_cooldown(self, source: str, stock: str) -> bool:
        return bool(await self.redis.exists(self.keys.analysis_cooldown(source, stock)))

    async def set_cooldown(self, source: str, stock: str, reason: str | None = None) -> None:
        await self.redis.set(
            self.keys.analysis_cooldown(source, stock),
            reason or "analysis_failed",
            ex=self.cooldown_ttl_sec,
        )

    async def acquire_scheduler_lock(self, job_name: str = "market_monitoring") -> str | None:
        token = uuid4().hex
        acquired = await self.redis.set(
            self.keys.scheduler_lock(job_name),
            token,
            nx=True,
            ex=self.scheduler_lock_ttl_sec,
        )
        return token if acquired else None

    async def acquire_analysis_lock(self, source: str, stock: str) -> str | None:
        token = uuid4().hex
        acquired = await self.redis.set(
            self.keys.analysis_lock(source, stock),
            token,
            nx=True,
            ex=self.stock_lock_ttl_sec,
        )
        return token if acquired else None

    async def release_lock(self, key: str, token: str) -> None:
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        end
        return 0
        """
        await self.redis.eval(script, 1, key, token)

    async def get_telegram_alert_mode(self) -> str:
        mode = self._decode(await self.redis.get(self.keys.telegram_alert_mode()))
        if mode in TELEGRAM_ALERT_MODES:
            return mode
        return DEFAULT_TELEGRAM_ALERT_MODE

    async def set_telegram_alert_mode(self, mode: str) -> bool:
        if mode not in TELEGRAM_ALERT_MODES:
            return False
        await self.redis.set(self.keys.telegram_alert_mode(), mode)
        return True


class InMemoryPendingOrderStore:
    """테스트 전용 인메모리 pending_order 저장소.

    async 메서드(get/set/delete/has)와 동기 dict 인터페이스(__getitem__,
    __contains__, __eq__)를 동시에 제공해, 기존 테스트 코드의
    ``handler.pending_orders['123']``, ``handler.pending_orders == {}`` 등을
    수정 없이 유지할 수 있게 한다.

    멀티워커 프로덕션 환경에서는 사용하지 말 것.
    프로세스 간 격리로 이슈 #63의 주문 유실 버그가 재현된다.
    """

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get(self, chat_id: str) -> Any:
        return self._store.get(chat_id)

    async def set(self, chat_id: str, order: Any) -> None:
        self._store[chat_id] = order

    async def delete(self, chat_id: str) -> None:
        self._store.pop(chat_id, None)

    async def has(self, chat_id: str) -> bool:
        return chat_id in self._store

    async def claim(self, chat_id: str) -> Any:
        """주문을 원자적으로 꺼내며 삭제한다. 재전송 update의 중복 체결을 방지한다."""
        return self._store.pop(chat_id, None)

    async def set_if_absent(self, chat_id: str, order: Any) -> bool:
        """이미 대기 주문이 있으면 False. 경합하는 /buy 요청 중 승자를 하나로 고정한다."""
        if chat_id in self._store:
            return False
        self._store[chat_id] = order
        return True

    # 동기 dict 인터페이스: 기존 테스트의 handler.pending_orders[...] 접근용
    def __getitem__(self, chat_id: str) -> Any:
        return self._store[chat_id]

    def __contains__(self, chat_id: object) -> bool:
        return chat_id in self._store

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict):
            return self._store == other
        if isinstance(other, InMemoryPendingOrderStore):
            return self._store == other._store
        return NotImplemented

    __hash__ = object.__hash__  # __eq__ 정의로 사라진 기본 해시를 복원

    def __repr__(self) -> str:
        return f"InMemoryPendingOrderStore({self._store!r})"


class RedisPendingOrderStore:
    """Redis TTL 저장소로 pending_order를 저장한다. 멀티워커·재시작 대응.

    Redis 장애 시 fail-closed — 인메모리 폴백 없음.
    - 폴백을 두면 멀티워커에서 원래 버그(프로세스 간 격리)가 재현된다.
    - Redis 장애 → 호출자에게 예외가 전파되고 사용자는 명시적 오류 메시지를 받는다.
      조용한 주문 유실보다 명시적 실패가 금전 경로에서 더 안전하다.

    scheduler.py의 Redis fallback(fail-open)은 멱등 모니터링 작업이라 가능하다.
    주문 확인/취소는 금전이 오가는 경로이므로 같은 기준을 적용할 수 없다.

    키 패턴: ``finus:pending_order:{chat_id}``
    TTL: PENDING_ORDER_TTL_SEC (600초 = 10분)
    """

    def __init__(
        self,
        redis: Any,
        *,
        keys: RedisKeys | None = None,
        ttl_sec: int = PENDING_ORDER_TTL_SEC,
    ) -> None:
        self.redis = redis
        self.ttl_sec = ttl_sec
        self._keys = keys or RedisKeys()

    def _serialize(self, order: Any) -> str:
        """PendingOrder → JSON 문자열. set/set_if_absent가 공유한다."""
        data = asdict(order)
        data["created_at"] = data["created_at"].isoformat()
        return json.dumps(data, ensure_ascii=False)

    def _deserialize(self, raw: str | bytes) -> Any:
        """raw JSON → PendingOrder. ValueError/TypeError/KeyError는 호출자가 처리한다."""
        from .trading_orders import PendingOrder

        data: dict[str, Any] = json.loads(raw if isinstance(raw, str) else raw.decode())
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        return PendingOrder(**data)

    async def get(self, chat_id: str) -> Any:
        """PendingOrder를 반환하거나, 없으면(TTL 만료 포함) None을 반환한다.

        역직렬화 오류(스키마 변경·손상 키)는 해당 키를 삭제하고 None을 반환한다.
        남겨두면 /cancel까지 같은 예외로 막혀 사용자가 스스로 복구할 수 없다.
        Redis 연결 오류 등 저장소 자체 장애는 여전히 위로 전파된다(fail-closed 유지).
        """
        raw = await self.redis.get(self._keys.pending_order(chat_id))
        if raw is None:
            return None
        try:
            return self._deserialize(raw)
        except (ValueError, TypeError, KeyError) as exc:
            logger.error("pending_order 역직렬화 실패, 키 삭제: %s", exc)
            await self.delete(chat_id)
            return None

    async def claim(self, chat_id: str) -> Any:
        """주문을 원자적으로 읽으며 삭제한다(GETDEL). 재전송된 Telegram update가
        같은 주문을 두 번 체결하는 것을 방지한다. 경합하는 두 호출 중 정확히 하나만
        order를 받고, 나머지는 None을 받는다.
        """
        raw = await self.redis.getdel(self._keys.pending_order(chat_id))
        if raw is None:
            return None
        try:
            return self._deserialize(raw)
        except (ValueError, TypeError, KeyError) as exc:
            # getdel로 이미 삭제됨 — 복원 없이 None 반환
            logger.error("pending_order 역직렬화 실패 (claim): %s", exc)
            return None

    async def set(self, chat_id: str, order: Any) -> None:
        """PendingOrder를 JSON 직렬화하여 TTL과 함께 저장한다."""
        await self.redis.set(
            self._keys.pending_order(chat_id),
            self._serialize(order),
            ex=self.ttl_sec,
        )

    async def set_if_absent(self, chat_id: str, order: Any) -> bool:
        """이미 대기 주문이 있으면 False. 경합하는 /buy 요청 중 승자를 하나로 고정한다.
        RedisSchedulerState.acquire_*_lock과 같은 NX 관용구를 따른다.
        """
        result = await self.redis.set(
            self._keys.pending_order(chat_id),
            self._serialize(order),
            ex=self.ttl_sec,
            nx=True,
        )
        return bool(result)

    async def delete(self, chat_id: str) -> None:
        await self.redis.delete(self._keys.pending_order(chat_id))

    async def has(self, chat_id: str) -> bool:
        return bool(await self.redis.exists(self._keys.pending_order(chat_id)))


def create_redis_client() -> Any:
    from redis.asyncio import Redis

    from .config import REDIS_URL

    return Redis.from_url(REDIS_URL, decode_responses=True)


@asynccontextmanager
async def redis_state() -> AsyncIterator[RedisSchedulerState]:
    client = create_redis_client()
    try:
        await client.ping()
        yield RedisSchedulerState(client)
    finally:
        await client.aclose()
