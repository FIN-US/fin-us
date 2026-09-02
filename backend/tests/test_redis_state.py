import inspect
import json

import pytest

from backend.redis_state import (
    SIGNAL_HASH_TTL_SEC,
    TELEGRAM_POLLER_STATE_TTL_SEC,
    InMemoryPendingOrderStore,
    InMemoryTelegramPollerStore,
    PendingOrderStore,
    RedisPendingOrderStore,
    RedisSchedulerState,
    RedisTelegramPollerStore,
    TelegramPollerState,
    TelegramPollerStore,
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
async def test_poller_state_round_trips_offset():
    redis = FakeRedis()
    store = RedisTelegramPollerStore(redis)

    assert await store.load() == TelegramPollerState()

    await store.save(TelegramPollerState(offset=44))
    loaded = await store.load()

    assert loaded.offset == 44
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

    await store.save(TelegramPollerState(offset=None))
    await store.save(TelegramPollerState(offset=44))

    assert await store.load() == TelegramPollerState(offset=44)


@pytest.mark.asyncio
async def test_poller_state_load_accepts_zero_offset():
    """0은 정상값이다 — falsy라고 손상으로 몰면 안 된다 (PR #251 리뷰)."""
    redis = FakeRedis()
    redis.store["finus:telegram:poller_state"] = '{"offset": 0}'
    store = RedisTelegramPollerStore(redis)

    assert await store.load() == TelegramPollerState(offset=0)
    # 정상값이므로 키가 남아 있어야 한다.
    assert "finus:telegram:poller_state" in redis.store


@pytest.mark.asyncio
async def test_poller_state_load_ignores_legacy_handled_ahead_field():
    """#259 1단계 이전 배포가 남긴 payload를 손상으로 몰지 않는다.

    되돌린 최적화의 상태라 복원할 대상이 아니지만, 거부하면 키가 삭제되면서 offset까지 함께
    날아가 재시작이 미확정 update를 처음부터 다시 배달받는다 — 필드 하나 무시로 끝날 일이
    전면 재실행이 된다.
    """
    redis = FakeRedis()
    redis.store["finus:telegram:poller_state"] = json.dumps(
        {"offset": 44, "handled_ahead": [42, 43]}
    )
    store = RedisTelegramPollerStore(redis)

    assert await store.load() == TelegramPollerState(offset=44)
    assert "finus:telegram:poller_state" in redis.store


@pytest.mark.asyncio
async def test_poller_state_save_drops_the_legacy_handled_ahead_field():
    """다음 쓰기가 옛 필드를 지운다 — payload에 읽지 않는 값이 TTL 내내 남지 않는다."""
    redis = FakeRedis()
    redis.store["finus:telegram:poller_state"] = json.dumps(
        {"offset": 44, "handled_ahead": [42, 43]}
    )
    store = RedisTelegramPollerStore(redis)

    await store.save(TelegramPollerState(offset=45))

    assert json.loads(redis.store["finus:telegram:poller_state"]) == {"offset": 45}


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


# ---------------------------------------------------------------------------
# 저장소 프로토콜 적합성 (#271)
# ---------------------------------------------------------------------------


def _protocol_methods(protocol) -> dict:
    """Protocol이 선언한 메서드만 추린다 (typing이 붙이는 내부 속성 제외).

    isfunction은 staticmethod·classmethod를 걸러낸다. 두 Protocol 모두 인스턴스 메서드만
    선언하고 있어 지금은 빠지는 것이 없지만, 나중에 그런 멤버가 추가되면 이 테스트가
    실패하는 대신 조용히 검사 대상에서 빠진다. 그때 여기를 함께 고쳐야 한다 (PR #287 리뷰).
    """
    return {
        name: member
        for name, member in vars(protocol).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


def _shape(signature: inspect.Signature) -> list:
    """self를 뺀 (이름, 종류, 필수 여부). 애노테이션은 타입 체커의 몫이라 보지 않는다."""
    return [
        (name, parameter.kind, parameter.default is inspect.Parameter.empty)
        for name, parameter in signature.parameters.items()
        if name != "self"
    ]


@pytest.mark.parametrize(
    ("protocol", "implementation"),
    [
        (TelegramPollerStore, RedisTelegramPollerStore),
        (TelegramPollerStore, InMemoryTelegramPollerStore),
        (PendingOrderStore, RedisPendingOrderStore),
        (PendingOrderStore, InMemoryPendingOrderStore),
    ],
    ids=lambda obj: obj.__name__,
)
def test_store_matches_protocol_shape(protocol, implementation):
    """구현체의 메서드 이름·인자 모양이 Protocol과 어긋나지 않는다 (#271).

    Protocol은 정적 검사 장치인데 이 레포의 CI에는 타입 체크 잡이 없다. 어긋남이 런타임까지
    살아남으면 두 주입 지점 모두 그것을 조용히 삼킨다 — TelegramCommandPoller의
    _persist_state·_restore_state에 있는 except Exception이 TypeError를 로그 한 줄로
    흘려보내고, 폴러는 "영속화 없음"으로 degrade한다. #248이 닫으려던 중복 실행 창이
    그대로 열리는데 예외는 어디에도 오르지 않는다.

    그래서 타입 체커 없이도 CI가 잡을 수 있는 만큼(이름·인자 모양)을 여기서 고정한다.
    타입의 적합성 자체는 여전히 체커의 몫이다.

    이 테스트가 잡는 mutation: 구현체의 메서드 이름 변경(save → save_state),
    인자 추가·삭제(save(self)), 위치 인자의 키워드 전용 전환.
    """
    for name, declared in _protocol_methods(protocol).items():
        actual = getattr(implementation, name, None)
        assert actual is not None, f"{implementation.__name__}에 {name}이 없다"
        assert inspect.iscoroutinefunction(actual), f"{name}은 async여야 한다"
        assert _shape(inspect.signature(actual)) == _shape(inspect.signature(declared)), (
            f"{implementation.__name__}.{name}의 인자가 {protocol.__name__}과 다르다"
        )
