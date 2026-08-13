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

# 폴러 상태(offset·handled_ahead) TTL: 7일.
# Telegram 서버는 미확정 update를 24시간만 보관하므로, 그보다 오래된 offset은 보호할 대상이
# 이미 없다. TTL은 값의 유효기간이 아니라 폴러가 영구히 내려간 뒤 남는 키를 정리하는 장치다.
# 쓰기마다 갱신되므로 폴러가 살아 있는 한 만료되지 않는다.
TELEGRAM_POLLER_STATE_TTL_SEC = 60 * 60 * 24 * 7
# 저장된 handled_ahead의 허용 상한. 도달 가능한 최댓값(약 6,700 — 산출 근거는
# RedisTelegramPollerStore._deserialize)의 15배로, 오염된 값만 걸러내고 정상 상태는
# 절대 버리지 않도록 잡았다. 상한에 걸리면 상태를 버리므로 중복 실행이 생긴다.
MAX_HANDLED_AHEAD = 100_000

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

    def telegram_poller_state(self) -> str:
        return f"{self.prefix}:telegram:poller_state"


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


@dataclass(frozen=True)
class TelegramPollerState:
    """폴러가 재시작을 넘겨 살려야 하는 최소 상태 (#248).

    두 값은 항상 함께 읽고 함께 쓴다. offset만 살아남고 handled_ahead가 사라지면
    poison 뒤에서 이미 실행한 update가 재배달되어 중복 실행되고, 반대로 handled_ahead만
    살아남으면 offset이 되감겨 같은 결과가 된다. 한 덩어리로 다뤄야 의미가 있다.

    재시도 예산(_UpdateFailure)은 일부러 담지 않는다. 유실되면 poison의 폐기 시점이
    미뤄질 뿐 중복 실행으로 이어지지 않으므로, 매 update 쓰기에 얹을 값이 아니다.
    """

    offset: int | None = None
    handled_ahead: frozenset[int] = frozenset()


class InMemoryTelegramPollerStore:
    """테스트 전용 인메모리 폴러 상태 저장소.

    재시작 시나리오는 같은 인스턴스를 두 폴러에 공유해 재현한다 —
    프로세스는 죽어도 redis는 살아남는 상황이 그대로 모형화된다.
    """

    def __init__(self, state: TelegramPollerState | None = None) -> None:
        self.state = state or TelegramPollerState()

    async def load(self) -> TelegramPollerState:
        return self.state

    async def save(self, state: TelegramPollerState) -> None:
        self.state = state


class RedisTelegramPollerStore:
    """폴러의 offset·handled_ahead를 redis에 영속화한다 (#248).

    두 값을 한 키에 JSON 한 덩어리로 쓴다. 키를 나누면 offset만 전진하고 handled_ahead
    정리는 유실된 절반 상태가 생기는데, 그게 정확히 이 이슈가 막으려는 중복 실행의 원인이다.
    폴러는 단일 인스턴스이므로 원자적 SET 하나면 둘의 일관성이 보장된다.

    호출자(TelegramCommandPoller)가 예외를 삼키는 fail-open이다.
    RedisPendingOrderStore의 fail-closed와 갈리는 이유는 보호 대상이 다르기 때문이다:
    금전 경로는 GETDEL claim과 set_if_absent가 이미 별도로 막고 있고, 여기 남는 위험은
    LLM 재호출과 중복 메시지다. redis 장애에 폴러를 세우면 텔레그램 명령이 전면 중단되는데,
    이는 영속화 없이 폴링을 계속할 때의 손해(#248 이전 수준의 중복 창)보다 크다.

    키: ``finus:telegram:poller_state``
    TTL: TELEGRAM_POLLER_STATE_TTL_SEC (7일, 쓰기마다 갱신)
    """

    def __init__(
        self,
        redis: Any,
        *,
        keys: RedisKeys | None = None,
        ttl_sec: int = TELEGRAM_POLLER_STATE_TTL_SEC,
    ) -> None:
        self.redis = redis
        self.ttl_sec = ttl_sec
        self._keys = keys or RedisKeys()

    async def load(self) -> TelegramPollerState:
        """저장된 상태를 반환하거나, 없으면/손상됐으면 빈 상태를 반환한다.

        손상된 값은 키를 지우고 빈 상태로 떨어진다. 남겨두면 매 재시작마다 같은 값을
        다시 읽어 실패하고, 그때마다 offset이 처음으로 되감겨 재배달 전체가 재실행된다.
        offset이 int가 아닌 채 통과하면 getUpdates payload까지 오염되므로 타입도 검증한다.
        """
        raw = await self.redis.get(self._keys.telegram_poller_state())
        if raw is None:
            return TelegramPollerState()
        try:
            return self._deserialize(raw)
        except (ValueError, TypeError, AttributeError) as exc:
            logger.error("telegram poller state 역직렬화 실패, 키 삭제: %s", exc)
            # 삭제 실패를 여기서 새게 두면 호출자의 blanket except가 "상태 복원 실패"로
            # 뭉뚱그려 로그해 실제 원인(키 삭제 실패)이 가려진다. 손상 키가 TTL까지 남아
            # 매 재시작이 같은 경로를 반복하므로, 그 사실이 로그에 명시적으로 남아야 한다.
            try:
                await self.redis.delete(self._keys.telegram_poller_state())
            except Exception as delete_exc:
                logger.error(
                    "telegram poller state 손상 키 삭제 실패, TTL(%s초)까지 남는다: %s",
                    self.ttl_sec,
                    delete_exc,
                )
            return TelegramPollerState()

    @staticmethod
    def _deserialize(raw: str | bytes) -> TelegramPollerState:
        data = json.loads(raw if isinstance(raw, str) else raw.decode())
        offset = data.get("offset")
        if offset is not None:
            # bool은 int의 하위 타입이라 isinstance만으로는 통과한다. offset에 True가 들어가면
            # getUpdates가 offset=1로 해석해 24시간치 update를 통째로 재배달한다.
            if isinstance(offset, bool) or not isinstance(offset, int):
                raise TypeError(f"offset must be int or null, got {offset!r}")
            # 음수 offset은 Telegram Bot API에서 "마지막 N개만 반환"으로 해석된다. -1이 새면
            # getUpdates가 일부만 돌려주고, 그 뒤 offset이 정상 전진하는 순간 서버가 그보다
            # 앞선 미확정 update를 삭제한다 — 사용자의 명령이 흔적 없이 사라진다 (PR #251 리뷰).
            if offset < 0:
                raise ValueError(f"offset must be non-negative, got {offset}")
        # `or []`로 기본값을 주면 false·0·"" 같은 falsy 비-list 값이 조용히 []로 바뀌어
        # 바로 아래 검사를 건너뛴다. 결과는 fail-safe지만 손상을 감지하지 못해 키가 남고
        # 다음 재시작도 같은 값을 그대로 통과시킨다 (PR #251 리뷰).
        handled_ahead = data.get("handled_ahead")
        if handled_ahead is None:
            handled_ahead = []
        if not isinstance(handled_ahead, list):
            raise TypeError(f"handled_ahead must be a list, got {handled_ahead!r}")
        # 정상 운영에서 이 집합은 _forget_passed_updates가 묶는다. 상한은 오염된 값이
        # 들어왔을 때만 의미가 있다 — 원소가 수백만이면 frozenset 생성과 save()의 매 update
        # sorted()가 CPU·메모리를 먹는다. 한계에 걸리면 상태를 버리고 중복이 생기므로,
        # 도달 가능한 최댓값에서 넉넉히 떨어뜨렸다: 전송 실패 창 335초 동안 최소 간격 5초로
        # 폴링하면 67배치 × getUpdates 기본 limit 100 = 6,700이 이론적 상한이다.
        if len(handled_ahead) > MAX_HANDLED_AHEAD:
            raise ValueError(
                f"handled_ahead too large ({len(handled_ahead)} > {MAX_HANDLED_AHEAD})"
            )
        for update_id in handled_ahead:
            if isinstance(update_id, bool) or not isinstance(update_id, int):
                raise TypeError(f"handled_ahead must hold ints, got {update_id!r}")
        return TelegramPollerState(offset=offset, handled_ahead=frozenset(handled_ahead))

    async def save(self, state: TelegramPollerState) -> None:
        payload = json.dumps(
            {"offset": state.offset, "handled_ahead": sorted(state.handled_ahead)}
        )
        await self.redis.set(
            self._keys.telegram_poller_state(), payload, ex=self.ttl_sec
        )


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
