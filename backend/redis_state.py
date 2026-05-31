import hashlib
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator
from uuid import uuid4

logger = logging.getLogger(__name__)

NEWS_HASH_TTL_SEC = 60 * 60 * 24 * 14
STOCK_LOCK_TTL_SEC = 60 * 10
SCHEDULER_LOCK_TTL_SEC = 60 * 30
COOLDOWN_TTL_SEC = 60 * 10
TELEGRAM_ALERT_MODES = {"urgent", "all", "off"}
DEFAULT_TELEGRAM_ALERT_MODE = "urgent"


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


normalize_news_text = normalize_signal_text
news_hash = signal_hash


@dataclass(frozen=True)
class RedisKeys:
    prefix: str = "finus"

    def last_hash(self, source: str, stock: str) -> str:
        return f"{self.prefix}:signal:last_hash:{source}:{stock}"

    def last_text(self, source: str, stock: str) -> str:
        return f"{self.prefix}:signal:last_text:{source}:{stock}"

    def last_analyzed_at(self, source: str, stock: str) -> str:
        return f"{self.prefix}:signal:last_analyzed_at:{source}:{stock}"

    def analysis_lock(self, source: str, stock: str) -> str:
        return f"{self.prefix}:analysis:lock:{source}:{stock}"

    def analysis_cooldown(self, source: str, stock: str) -> str:
        return f"{self.prefix}:analysis:cooldown:{source}:{stock}"

    def scheduler_lock(self, job_name: str = "market_monitoring") -> str:
        return f"{self.prefix}:scheduler:lock:{job_name}"

    def telegram_alert_mode(self) -> str:
        return f"{self.prefix}:telegram:alert_mode"

    def watchlist(self) -> str:
        return f"{self.prefix}:user:watchlist"


class RedisSchedulerState:
    def __init__(
        self,
        redis: Any,
        *,
        keys: RedisKeys | None = None,
        news_hash_ttl_sec: int = NEWS_HASH_TTL_SEC,
        stock_lock_ttl_sec: int = STOCK_LOCK_TTL_SEC,
        scheduler_lock_ttl_sec: int = SCHEDULER_LOCK_TTL_SEC,
        cooldown_ttl_sec: int = COOLDOWN_TTL_SEC,
    ):
        self.redis = redis
        self.keys = keys or RedisKeys()
        self.news_hash_ttl_sec = news_hash_ttl_sec
        self.stock_lock_ttl_sec = stock_lock_ttl_sec
        self.scheduler_lock_ttl_sec = scheduler_lock_ttl_sec
        self.cooldown_ttl_sec = cooldown_ttl_sec

    async def get_last_signal_hash(self, source: str, stock: str) -> str | None:
        value = await self.redis.get(self.keys.last_hash(source, stock))
        return self._decode(value)

    async def get_last_signal_text(self, source: str, stock: str) -> str | None:
        value = await self.redis.get(self.keys.last_text(source, stock))
        return self._decode(value)

    async def get_last_news_hash(self, stock: str) -> str | None:
        return await self.get_last_signal_hash("news", stock)

    async def get_last_news_text(self, stock: str) -> str | None:
        return await self.get_last_signal_text("news", stock)

    @staticmethod
    def _decode(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def set_last_signal(self, source: str, stock: str, signal_text: str, digest: str) -> None:
        await self.redis.set(self.keys.last_hash(source, stock), digest, ex=self.news_hash_ttl_sec)
        await self.redis.set(self.keys.last_text(source, stock), signal_text, ex=self.news_hash_ttl_sec)
        await self.redis.set(
            self.keys.last_analyzed_at(source, stock),
            datetime.now(UTC).isoformat(),
            ex=self.news_hash_ttl_sec,
        )

    async def set_last_news(self, stock: str, news_text: str, digest: str) -> None:
        await self.set_last_signal("news", stock, news_text, digest)

    async def in_cooldown(self, source: str, stock: str | None = None) -> bool:
        source, stock = self._coerce_source_stock(source, stock)
        return bool(await self.redis.exists(self.keys.analysis_cooldown(source, stock)))

    async def set_cooldown(self, source: str, stock: str | None = None, reason: str | None = None) -> None:
        source, stock = self._coerce_source_stock(source, stock)
        await self.redis.set(
            self.keys.analysis_cooldown(source, stock),
            reason or "analysis_failed",
            ex=self.cooldown_ttl_sec,
        )

    @staticmethod
    def _coerce_source_stock(source: str, stock: str | None) -> tuple[str, str]:
        if stock is None:
            return "news", source
        return source, stock

    async def acquire_scheduler_lock(self, job_name: str = "market_monitoring") -> str | None:
        token = uuid4().hex
        acquired = await self.redis.set(
            self.keys.scheduler_lock(job_name),
            token,
            nx=True,
            ex=self.scheduler_lock_ttl_sec,
        )
        return token if acquired else None

    async def acquire_analysis_lock(self, source: str, stock: str | None = None) -> str | None:
        source, stock = self._coerce_source_stock(source, stock)
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

    async def get_watchlist(self) -> list[str]:
        members = await self.redis.smembers(self.keys.watchlist())
        return sorted(self._decode(m) for m in members if m)

    async def add_to_watchlist(self, stock: str) -> None:
        await self.redis.sadd(self.keys.watchlist(), stock)

    async def remove_from_watchlist(self, stock: str) -> None:
        await self.redis.srem(self.keys.watchlist(), stock)


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
