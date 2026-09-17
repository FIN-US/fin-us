import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Awaitable, cast
from uuid import uuid4

import pytest

from backend.redis_state import (
    _DELETE_IF_UNCHANGED_SCRIPT,
    _REPLACE_IF_UNCHANGED_SCRIPT,
    ConditionalClaim,
    RedisKeys,
    RedisPendingOrderStore,
    RedisSchedulerState,
    RedisTelegramPollerStore,
    TelegramPollerState,
    signal_hash,
)
from backend.trading_orders import PendingOrder


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
        # redis-py는 동기·비동기 클라이언트가 명령 시그니처를 공유하느라
        # ping()을 Awaitable[bool] | bool로 선언한다. 비동기 클라이언트에서는
        # 항상 앞쪽이지만 체커는 그것을 알 수 없다.
        await cast(Awaitable[bool], client.ping())
    except Exception as exc:
        await client.aclose()
        # 접속 실패를 skip으로 넘기면, CI에서 서비스 컨테이너가 죽거나 주소가 어긋난
        # 순간 이 파일 전체가 조용히 사라지고 잡은 초록불로 끝난다 — #267이 없애려는
        # "실 redis 커버리지 0" 상태로 아무 신호 없이 되돌아간다. URL을 준 것은
        # "여기 redis가 있다"는 선언이므로, 없으면 skip이 아니라 실패다.
        #
        # URL 값 자체는 찍지 않는다. redis://user:pass@host 형태면 credential이 그대로
        # 나가는데 이 레포는 public이라 Actions 로그가 공개다. 지금 CI 값에는
        # credential이 없지만 나중에 관리형 redis를 secret으로 물리는 순간 샌다.
        # 진단에 필요한 host:port는 redis-py 예외 메시지가 이미 갖고 있어 잃는 것도 없다.
        pytest.fail(f"REDIS_INTEGRATION_URL 로 지정한 redis에 접속하지 못했습니다: {exc}")
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
        await writer.save(TelegramPollerState(offset=44))

        reader = RedisTelegramPollerStore(redis, keys=keys)
        loaded = await reader.load()

        assert loaded.offset == 44
        assert await redis.ttl(keys.telegram_poller_state()) > 0
    finally:
        stale = await redis.keys(f"{prefix}:*")
        if stale:
            await redis.delete(*stale)
        await redis.aclose()


async def test_real_redis_pending_order_conditional_writes_compare_values_and_keep_ttl():
    """대기 주문의 조건부 claim·프롬프트 id 기록이 실제 redis에서 뜻대로 돈다 (#386).

    FakeRedis는 두 Lua 스크립트를 파이썬으로 흉내 내므로 스크립트 본문이 틀려도 모른다.
    KEEPTTL(redis 6.0+)도 여기서만 보인다 — 빠지면 id를 남기는 SET이 TTL을 지워 대기 주문
    키가 영구히 남는다.
    """
    redis = await _redis_client()
    prefix = f"finus:test:{uuid4().hex}"
    keys = RedisKeys(prefix=prefix)
    store = RedisPendingOrderStore(redis, keys=keys, ttl_sec=600)
    key = keys.pending_order("123")
    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=1,
        price=75000,
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone(timedelta(hours=9))),
        callback_token="tok-a",
    )

    try:
        await store.set("123", order)

        # 다른 주문(토큰)에는 쓰지 않고, 같은 주문이면 id를 남기되 TTL은 유지한다.
        assert await store.set_prompt_message_id("123", "tok-other", 77) is False
        assert await store.set_prompt_message_id("123", "tok-a", 77) is True
        stored = await store.get("123")
        assert stored == replace(order, prompt_message_id=77)
        assert 0 < await redis.ttl(key) <= 600

        # 읽은 값과 다르면 두 스크립트 모두 아무것도 바꾸지 않는다.
        deleted = await cast(
            Awaitable[int], redis.eval(_DELETE_IF_UNCHANGED_SCRIPT, 1, key, "stale")
        )
        replaced = await cast(
            Awaitable[int], redis.eval(_REPLACE_IF_UNCHANGED_SCRIPT, 1, key, "stale", "x")
        )
        assert (deleted, replaced) == (0, 0)
        assert await store.get("123") == stored

        # 조건에 걸리면 남기고, 통과하면 꺼내며 지운다.
        refused = await store.claim_if("123", lambda pending: pending.prompted_before(77))
        assert refused == ConditionalClaim(order=stored, claimed=False)
        assert await redis.exists(key) == 1
        claimed = await store.claim_if("123", lambda pending: pending.prompted_before(78))
        assert claimed == ConditionalClaim(order=stored, claimed=True)
        assert await redis.exists(key) == 0
    finally:
        stale = await redis.keys(f"{prefix}:*")
        if stale:
            await redis.delete(*stale)
        await redis.aclose()
