import pytest

from backend.redis_state import (
    RedisSchedulerState,
    news_hash,
    normalize_news_text,
    signal_hash,
    normalize_signal_text,
)


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.sets: dict[str, set] = {}

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

    async def sadd(self, key, *values):
        if key not in self.sets:
            self.sets[key] = set()
        added = sum(1 for v in values if v not in self.sets[key])
        self.sets[key].update(values)
        return added

    async def srem(self, key, *values):
        if key not in self.sets:
            return 0
        removed = sum(1 for v in values if v in self.sets[key])
        self.sets[key].difference_update(values)
        return removed

    async def smembers(self, key):
        return self.sets.get(key, set())


def test_news_hash_normalizes_order_and_whitespace():
    first = " 삼성전자  급등\nSK하이닉스   실적 개선\n삼성전자 급등"
    second = "sk하이닉스 실적 개선\n삼성전자 급등"

    assert normalize_news_text(first) == normalize_news_text(second)
    assert news_hash(first) == news_hash(second)
    assert normalize_signal_text(first) == normalize_signal_text(second)
    assert signal_hash(first) == signal_hash(second)


@pytest.mark.asyncio
async def test_last_news_hash_round_trip():
    state = RedisSchedulerState(FakeRedis())
    digest = news_hash("삼성전자 신규 뉴스")

    await state.set_last_news("삼성전자", "삼성전자 신규 뉴스", digest)

    assert await state.get_last_news_hash("삼성전자") == digest
    assert await state.get_last_news_text("삼성전자") == "삼성전자 신규 뉴스"


@pytest.mark.asyncio
async def test_last_signal_state_is_scoped_by_source():
    state = RedisSchedulerState(FakeRedis())
    news_digest = signal_hash("삼성전자 뉴스")
    sns_digest = signal_hash("삼성전자 SNS")

    await state.set_last_signal("news", "삼성전자", "삼성전자 뉴스", news_digest)
    await state.set_last_signal("sns", "삼성전자", "삼성전자 SNS", sns_digest)

    assert await state.get_last_signal_hash("news", "삼성전자") == news_digest
    assert await state.get_last_signal_hash("sns", "삼성전자") == sns_digest
    assert await state.get_last_signal_text("news", "삼성전자") == "삼성전자 뉴스"
    assert await state.get_last_signal_text("sns", "삼성전자") == "삼성전자 SNS"


@pytest.mark.asyncio
async def test_analysis_lock_allows_single_owner_and_token_release():
    redis = FakeRedis()
    state = RedisSchedulerState(redis)

    token = await state.acquire_analysis_lock("news", "삼성전자")
    second_token = await state.acquire_analysis_lock("news", "삼성전자")

    assert token is not None
    assert second_token is None

    await state.release_lock(state.keys.analysis_lock("news", "삼성전자"), "wrong-token")
    assert await state.acquire_analysis_lock("news", "삼성전자") is None

    await state.release_lock(state.keys.analysis_lock("news", "삼성전자"), token)
    assert await state.acquire_analysis_lock("news", "삼성전자") is not None


@pytest.mark.asyncio
async def test_analysis_lock_is_scoped_by_source():
    state = RedisSchedulerState(FakeRedis())

    news_token = await state.acquire_analysis_lock("news", "삼성전자")
    sns_token = await state.acquire_analysis_lock("sns", "삼성전자")

    assert news_token is not None
    assert sns_token is not None


@pytest.mark.asyncio
async def test_cooldown_flag():
    state = RedisSchedulerState(FakeRedis())

    assert await state.in_cooldown("news", "삼성전자") is False

    await state.set_cooldown("news", "삼성전자", "nat_failed")

    assert await state.in_cooldown("news", "삼성전자") is True
    assert await state.in_cooldown("sns", "삼성전자") is False


@pytest.mark.asyncio
async def test_scheduler_lock_allows_single_owner():
    state = RedisSchedulerState(FakeRedis())

    token = await state.acquire_scheduler_lock()

    assert token is not None
    assert await state.acquire_scheduler_lock() is None


@pytest.mark.asyncio
async def test_telegram_alert_mode_defaults_to_urgent_and_round_trips():
    state = RedisSchedulerState(FakeRedis())

    assert await state.get_telegram_alert_mode() == "urgent"

    await state.set_telegram_alert_mode("all")
    assert await state.get_telegram_alert_mode() == "all"

    await state.set_telegram_alert_mode("off")
    assert await state.get_telegram_alert_mode() == "off"


@pytest.mark.asyncio
async def test_telegram_alert_mode_ignores_invalid_values():
    state = RedisSchedulerState(FakeRedis())

    await state.set_telegram_alert_mode("all")
    await state.set_telegram_alert_mode("invalid")

    assert await state.get_telegram_alert_mode() == "all"


@pytest.mark.asyncio
async def test_watchlist_is_empty_by_default():
    state = RedisSchedulerState(FakeRedis())

    assert await state.get_watchlist() == []


@pytest.mark.asyncio
async def test_watchlist_add_and_list():
    state = RedisSchedulerState(FakeRedis())

    await state.add_to_watchlist("삼성전자")
    await state.add_to_watchlist("NAVER")

    result = await state.get_watchlist()
    assert result == ["NAVER", "삼성전자"]


@pytest.mark.asyncio
async def test_watchlist_add_duplicate_is_idempotent():
    state = RedisSchedulerState(FakeRedis())

    await state.add_to_watchlist("삼성전자")
    await state.add_to_watchlist("삼성전자")

    assert await state.get_watchlist() == ["삼성전자"]


@pytest.mark.asyncio
async def test_watchlist_remove():
    state = RedisSchedulerState(FakeRedis())

    await state.add_to_watchlist("삼성전자")
    await state.add_to_watchlist("NAVER")
    await state.remove_from_watchlist("삼성전자")

    assert await state.get_watchlist() == ["NAVER"]


@pytest.mark.asyncio
async def test_watchlist_remove_nonexistent_is_safe():
    state = RedisSchedulerState(FakeRedis())

    await state.remove_from_watchlist("없는종목")

    assert await state.get_watchlist() == []
