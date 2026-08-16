import asyncio
import os
from uuid import uuid4

import pytest

from backend.redis_state import (
    RedisKeys,
    RedisSchedulerState,
    RedisTelegramPollerStore,
    TelegramPollerState,
    signal_hash,
)


pytestmark = pytest.mark.asyncio


async def _redis_client():
    url = os.environ.get("REDIS_INTEGRATION_URL")
    if not url:
        pytest.skip("REDIS_INTEGRATION_URL is not set")

    try:
        from redis.asyncio import Redis
    except ModuleNotFoundError:
        pytest.skip("redis package is not installed")

    client = Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        # 접속 실패를 skip으로 넘기면, CI에서 서비스 컨테이너가 죽거나 주소가 어긋난
        # 순간 이 파일 전체가 조용히 사라지고 잡은 초록불로 끝난다 — #267이 없애려는
        # "실 redis 커버리지 0" 상태로 아무 신호 없이 되돌아간다. URL을 준 것은
        # "여기 redis가 있다"는 선언이므로, 없으면 skip이 아니라 실패다.
        pytest.fail(f"REDIS_INTEGRATION_URL={url} 에 접속하지 못했습니다: {exc}")
    return client


async def test_real_redis_signal_state_lock_and_cooldown_round_trip():
    redis = await _redis_client()
    prefix = f"finus:test:{uuid4().hex}"
    state = RedisSchedulerState(redis, keys=RedisKeys(prefix=prefix))

    try:
        digest = signal_hash("삼성전자 신규 signal")
        await state.set_last_signal("sns", "삼성전자", "삼성전자 신규 signal", digest)

        assert await state.get_last_signal_hash("sns", "삼성전자") == digest
        assert await state.get_last_signal_text("sns", "삼성전자") == "삼성전자 신규 signal"

        token = await state.acquire_analysis_lock("sns", "삼성전자")
        assert token is not None
        assert await state.acquire_analysis_lock("sns", "삼성전자") is None

        await state.release_lock(state.keys.analysis_lock("sns", "삼성전자"), "wrong-token")
        assert await state.acquire_analysis_lock("sns", "삼성전자") is None

        await state.release_lock(state.keys.analysis_lock("sns", "삼성전자"), token)
        assert await state.acquire_analysis_lock("sns", "삼성전자") is not None

        await state.set_cooldown("sns", "삼성전자", "nat_failed")
        assert await state.in_cooldown("sns", "삼성전자") is True
    finally:
        keys = await redis.keys(f"{prefix}:*")
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


async def test_real_redis_allows_only_one_concurrent_analysis_lock_holder():
    redis = await _redis_client()
    prefix = f"finus:test:{uuid4().hex}"
    state = RedisSchedulerState(redis, keys=RedisKeys(prefix=prefix))

    try:
        tokens = await asyncio.gather(
            *[state.acquire_analysis_lock("news", "삼성전자") for _ in range(20)]
        )

        acquired = [token for token in tokens if token is not None]
        assert len(acquired) == 1
    finally:
        keys = await redis.keys(f"{prefix}:*")
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


async def test_real_redis_poller_state_survives_a_new_store_instance():
    """폴러 상태가 실제 redis를 통해 새 인스턴스로 넘어간다 (#248).

    인스턴스 교체 = 프로세스 재시작. FakeRedis는 JSON 왕복과 TTL 적용을 흉내만 내므로
    실제 서버에서 한 번 확인한다.
    """
    redis = await _redis_client()
    prefix = f"finus:test:{uuid4().hex}"
    keys = RedisKeys(prefix=prefix)

    try:
        writer = RedisTelegramPollerStore(redis, keys=keys)
        await writer.save(TelegramPollerState(offset=44, handled_ahead=frozenset({42, 43})))

        reader = RedisTelegramPollerStore(redis, keys=keys)
        loaded = await reader.load()

        assert loaded.offset == 44
        assert loaded.handled_ahead == frozenset({42, 43})
        assert await redis.ttl(keys.telegram_poller_state()) > 0
    finally:
        stale = await redis.keys(f"{prefix}:*")
        if stale:
            await redis.delete(*stale)
        await redis.aclose()
