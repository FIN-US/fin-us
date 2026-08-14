import json

import pytest

from backend.redis_state import (
    MAX_HANDLED_AHEAD,
    SIGNAL_HASH_TTL_SEC,
    TELEGRAM_POLLER_STATE_TTL_SEC,
    RedisSchedulerState,
    RedisTelegramPollerStore,
    TelegramPollerState,
    signal_hash,
    normalize_signal_text,
)


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.calls = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.store:
            return False
        # nx 조기 반환 뒤에 기록하므로 calls에는 "실제로 쓰인 것"만 남는다.
        # 획득 실패한 락 시도는 잡히지 않으니, 시도 횟수를 보려면 별도 카운터가 필요하다.
        self.calls.append((key, value, ex))
        self.store[key] = value
        return True

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def delete(self, key):
        self.store.pop(key, None)

    async def eval(self, script, numkeys, key, token):
        _ = script, numkeys
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


def test_signal_hash_normalizes_order_and_whitespace():
    first = " 삼성전자  급등\nSK하이닉스   실적 개선\n삼성전자 급등"
    second = "sk하이닉스 실적 개선\n삼성전자 급등"

    assert normalize_signal_text(first) == normalize_signal_text(second)
    assert signal_hash(first) == signal_hash(second)


@pytest.mark.asyncio
async def test_set_last_signal_writes_only_hash_and_text_with_ttl():
    """이 리팩터링이 실제로 바꾼 것(쓰기 3건 → 2건, 두 키 모두 TTL 부여)을 고정한다.

    값 왕복은 test_last_signal_state_is_scoped_by_source가 덮으므로 여기서는
    쓰기 대상·횟수·TTL만 본다. 키 문자열은 RedisKeys에서 받아 와, 키 포맷이
    바뀌었을 때 이 테스트가 엉뚱한 사유로 실패하지 않게 한다.
    """
    redis = FakeRedis()
    state = RedisSchedulerState(redis)

    await state.set_last_signal("news", "삼성전자", "삼성전자 신규 뉴스", "d")

    assert redis.calls == [
        (state.keys.last_hash("news", "삼성전자"), "d", SIGNAL_HASH_TTL_SEC),
        (
            state.keys.last_text("news", "삼성전자"),
            "삼성전자 신규 뉴스",
            SIGNAL_HASH_TTL_SEC,
        ),
    ]


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




# ---------------------------------------------------------------------------
# RedisTelegramPollerStore (#248)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poller_state_round_trips_offset_and_handled_ahead():
    redis = FakeRedis()
    store = RedisTelegramPollerStore(redis)

    assert await store.load() == TelegramPollerState()

    await store.save(TelegramPollerState(offset=44, handled_ahead=frozenset({42, 43})))
    loaded = await store.load()

    assert loaded.offset == 44
    assert loaded.handled_ahead == frozenset({42, 43})
    # 두 값이 한 키에 함께 저장되어야 절반만 반영된 상태가 생기지 않는다.
    assert list(redis.store) == ["finus:telegram:poller_state"]
    assert redis.calls[-1][2] == TELEGRAM_POLLER_STATE_TTL_SEC


@pytest.mark.asyncio
async def test_poller_state_load_drops_corrupted_value():
    """손상된 값을 남겨두면 매 재시작마다 같은 실패를 반복한다 (#248)."""
    redis = FakeRedis()
    redis.store["finus:telegram:poller_state"] = "not json"
    store = RedisTelegramPollerStore(redis)

    assert await store.load() == TelegramPollerState()
    assert redis.store == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        '{"offset": "42"}',
        # bool은 int의 하위 타입이라 타입 검사를 그냥 통과한다. offset=True가 통과하면
        # getUpdates가 offset=1로 해석해 24시간치 update를 통째로 재배달한다.
        '{"offset": true}',
        # 음수 offset은 Telegram이 "마지막 N개만"으로 해석한다. 통과하면 앞선 미확정
        # update가 조용히 삭제된다 (PR #251 리뷰).
        '{"offset": -1}',
        # 위쪽도 닫혀 있어야 한다. 거대 양수가 새면 Telegram이 미확정 update를 전부 삭제하고
        # 봇이 영구 무응답이 된다 (PR #251 리뷰).
        '{"offset": 99999999999}',
        '{"offset": 42, "handled_ahead": "42"}',
        '{"offset": 42, "handled_ahead": ["42"]}',
        # 원소의 bool 가드. 구현은 있었는데 케이스가 없어 뮤테이션이 살아 있었다
        # (PR #251 리뷰). `1 in frozenset({True})`가 True라 update_id 1이 건너뛰어진다.
        '{"offset": 42, "handled_ahead": [true]}',
        # falsy 비-list 값. `or []`로 기본값을 주면 조용히 []가 되어 검사를 건너뛴다
        # (PR #251 리뷰).
        '{"handled_ahead": false}',
        '{"handled_ahead": 0}',
        '{"handled_ahead": ""}',
    ],
)
async def test_poller_state_load_rejects_wrong_types(payload):
    redis = FakeRedis()
    redis.store["finus:telegram:poller_state"] = payload
    store = RedisTelegramPollerStore(redis)

    assert await store.load() == TelegramPollerState()
    assert redis.store == {}


@pytest.mark.asyncio
async def test_poller_state_save_overwrites_whole_state():
    """전체 상태를 매번 덮어쓰므로 이전 쓰기가 실패해도 다음 쓰기가 따라잡는다 (#248)."""
    redis = FakeRedis()
    store = RedisTelegramPollerStore(redis)

    await store.save(TelegramPollerState(offset=None, handled_ahead=frozenset({42, 43})))
    await store.save(TelegramPollerState(offset=44, handled_ahead=frozenset()))

    assert await store.load() == TelegramPollerState(offset=44, handled_ahead=frozenset())


@pytest.mark.asyncio
async def test_poller_state_load_accepts_zero_offset_and_empty_handled_ahead():
    """0과 빈 리스트는 정상값이다 — falsy라고 손상으로 몰면 안 된다 (PR #251 리뷰)."""
    redis = FakeRedis()
    redis.store["finus:telegram:poller_state"] = '{"offset": 0, "handled_ahead": []}'
    store = RedisTelegramPollerStore(redis)

    assert await store.load() == TelegramPollerState(offset=0, handled_ahead=frozenset())
    # 정상값이므로 키가 남아 있어야 한다.
    assert "finus:telegram:poller_state" in redis.store


@pytest.mark.asyncio
async def test_poller_state_load_rejects_oversized_handled_ahead():
    """오염된 거대 집합은 버린다. frozenset 생성과 매 update sorted()가 CPU를 먹는다 (PR #251 리뷰)."""
    redis = FakeRedis()
    oversized = list(range(MAX_HANDLED_AHEAD + 1))
    redis.store["finus:telegram:poller_state"] = json.dumps({"offset": 1, "handled_ahead": oversized})
    store = RedisTelegramPollerStore(redis)

    assert await store.load() == TelegramPollerState()
    assert redis.store == {}


@pytest.mark.asyncio
async def test_poller_state_load_keeps_realistic_handled_ahead_size():
    """도달 가능한 최댓값(약 6,700)은 상한에 걸리지 않아야 한다 (PR #251 리뷰).

    상한에 걸리면 상태를 버려 중복 실행이 생긴다 — 정상 상태를 버리는 상한은 이 PR이
    고치려는 버그를 되살린다. 전송 실패 창 335초 / 최소 간격 5초 = 67배치 × limit 100.
    """
    redis = FakeRedis()
    reachable = list(range(6_700))
    redis.store["finus:telegram:poller_state"] = json.dumps({"offset": 1, "handled_ahead": reachable})
    store = RedisTelegramPollerStore(redis)

    assert len((await store.load()).handled_ahead) == 6_700


@pytest.mark.asyncio
async def test_poller_state_load_logs_when_corrupted_key_delete_fails(caplog):
    """삭제 실패를 삼키되 원인이 로그에 남아야 한다 (PR #251 리뷰).

    호출자의 blanket except가 "상태 복원 실패"로 뭉뚱그리면 실제 원인이 가려지고,
    손상 키는 TTL까지 남아 매 재시작이 같은 경로를 반복한다.
    """
    class DeleteFailingRedis(FakeRedis):
        async def delete(self, key):
            raise RuntimeError("redis unavailable")

    redis = DeleteFailingRedis()
    redis.store["finus:telegram:poller_state"] = "not json"
    store = RedisTelegramPollerStore(redis)

    with caplog.at_level("ERROR"):
        assert await store.load() == TelegramPollerState()

    assert "손상 키 삭제 실패" in caplog.text


@pytest.mark.asyncio
async def test_poller_state_save_warns_when_handled_ahead_nears_limit(caplog):
    """상한은 load에만 걸려 있어, 넘어가는 순간을 save가 알리지 않으면 신호가 없다 (PR #251 리뷰)."""
    store = RedisTelegramPollerStore(FakeRedis())
    near_limit = frozenset(range(MAX_HANDLED_AHEAD // 2 + 1))

    with caplog.at_level("WARNING"):
        await store.save(TelegramPollerState(offset=1, handled_ahead=near_limit))

    assert "상한의 절반을 넘었다" in caplog.text


@pytest.mark.asyncio
async def test_poller_state_save_is_quiet_below_warning_threshold(caplog):
    """도달 가능한 크기(약 6,700)에서 경고가 울리면 로그가 무의미해진다 (PR #251 리뷰)."""
    store = RedisTelegramPollerStore(FakeRedis())

    with caplog.at_level("WARNING"):
        await store.save(TelegramPollerState(offset=1, handled_ahead=frozenset(range(6_700))))

    assert caplog.text == ""
