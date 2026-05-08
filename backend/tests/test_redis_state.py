import pytest

from backend.redis_state import RedisSchedulerState, news_hash, normalize_news_text


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def eval(self, script, numkeys, key, token):
        _ = script, numkeys
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def test_news_hash_normalizes_order_and_whitespace():
    first = " 삼성전자  급등\nSK하이닉스   실적 개선\n삼성전자 급등"
    second = "sk하이닉스 실적 개선\n삼성전자 급등"

    assert normalize_news_text(first) == normalize_news_text(second)
    assert news_hash(first) == news_hash(second)


@pytest.mark.asyncio
async def test_last_news_hash_round_trip():
    state = RedisSchedulerState(FakeRedis())
    digest = news_hash("삼성전자 신규 뉴스")

    await state.set_last_news("삼성전자", "삼성전자 신규 뉴스", digest)

    assert await state.get_last_news_hash("삼성전자") == digest


@pytest.mark.asyncio
async def test_analysis_lock_allows_single_owner_and_token_release():
    redis = FakeRedis()
    state = RedisSchedulerState(redis)

    token = await state.acquire_analysis_lock("삼성전자")
    second_token = await state.acquire_analysis_lock("삼성전자")

    assert token is not None
    assert second_token is None

    await state.release_lock(state.keys.analysis_lock("삼성전자"), "wrong-token")
    assert await state.acquire_analysis_lock("삼성전자") is None

    await state.release_lock(state.keys.analysis_lock("삼성전자"), token)
    assert await state.acquire_analysis_lock("삼성전자") is not None


@pytest.mark.asyncio
async def test_cooldown_flag():
    state = RedisSchedulerState(FakeRedis())

    assert await state.in_cooldown("삼성전자") is False

    await state.set_cooldown("삼성전자", "nat_failed")

    assert await state.in_cooldown("삼성전자") is True


@pytest.mark.asyncio
async def test_scheduler_lock_allows_single_owner():
    state = RedisSchedulerState(FakeRedis())

    token = await state.acquire_scheduler_lock()

    assert token is not None
    assert await state.acquire_scheduler_lock() is None
