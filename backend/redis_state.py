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


def normalize_news_text(news_text: str) -> str:
    lines = []
    for line in (news_text or "").splitlines():
        normalized = re.sub(r"\s+", " ", line).strip().casefold()
        if normalized:
            lines.append(normalized)
    return "\n".join(sorted(set(lines)))


def news_hash(news_text: str) -> str:
    normalized = normalize_news_text(news_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RedisKeys:
    prefix: str = "finus"

    def last_hash(self, stock: str) -> str:
        return f"{self.prefix}:news:last_hash:{stock}"

    def last_text(self, stock: str) -> str:
        return f"{self.prefix}:news:last_text:{stock}"

    def last_analyzed_at(self, stock: str) -> str:
        return f"{self.prefix}:news:last_analyzed_at:{stock}"

    def analysis_lock(self, stock: str) -> str:
        return f"{self.prefix}:analysis:lock:{stock}"

    def analysis_cooldown(self, stock: str) -> str:
        return f"{self.prefix}:analysis:cooldown:{stock}"

    def scheduler_lock(self) -> str:
        return f"{self.prefix}:scheduler:lock:market_monitoring"


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

    async def get_last_news_hash(self, stock: str) -> str | None:
        value = await self.redis.get(self.keys.last_hash(stock))
        return self._decode(value)

    async def get_last_news_text(self, stock: str) -> str | None:
        value = await self.redis.get(self.keys.last_text(stock))
        return self._decode(value)

    @staticmethod
    def _decode(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def set_last_news(self, stock: str, news_text: str, digest: str) -> None:
        await self.redis.set(self.keys.last_hash(stock), digest, ex=self.news_hash_ttl_sec)
        await self.redis.set(self.keys.last_text(stock), news_text, ex=self.news_hash_ttl_sec)
        await self.redis.set(
            self.keys.last_analyzed_at(stock),
            datetime.now(UTC).isoformat(),
            ex=self.news_hash_ttl_sec,
        )

    async def in_cooldown(self, stock: str) -> bool:
        return bool(await self.redis.exists(self.keys.analysis_cooldown(stock)))

    async def set_cooldown(self, stock: str, reason: str) -> None:
        await self.redis.set(self.keys.analysis_cooldown(stock), reason, ex=self.cooldown_ttl_sec)

    async def acquire_scheduler_lock(self) -> str | None:
        token = uuid4().hex
        acquired = await self.redis.set(
            self.keys.scheduler_lock(),
            token,
            nx=True,
            ex=self.scheduler_lock_ttl_sec,
        )
        return token if acquired else None

    async def acquire_analysis_lock(self, stock: str) -> str | None:
        token = uuid4().hex
        acquired = await self.redis.set(
            self.keys.analysis_lock(stock),
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
