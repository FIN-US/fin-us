"""pending_order 저장소 테스트 (이슈 #63).

검증 항목:
- RedisPendingOrderStore: 저장→조회 왕복, TTL 만료 후 조회, Redis 장애 시 동작,
  chat_id 간 격리, PendingOrder 필드 직렬화 무손실
- InMemoryPendingOrderStore: 기본 동작, 동기 dict 인터페이스
- 통합: TTL 만료 후 /confirm 시 명시적 오류, /cancel 시 명시적 오류,
  Redis 장애 시 handler 오류 메시지 전달
- 확정 프롬프트 message_id(#386): 직렬화 왕복, 기존·손상 저장값, 조건부 claim, id 기록
"""

import json
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.redis_state import (
    _DELETE_IF_UNCHANGED_SCRIPT,
    _REPLACE_IF_UNCHANGED_SCRIPT,
    CONFIRM_UPDATE_MARKER_TTL_SEC,
    PENDING_ORDER_TTL_SEC,
    ConditionalClaim,
    InMemoryPendingOrderStore,
    PendingOrderContentionError,
    RedisKeys,
    RedisPendingOrderStore,
)
from backend.trading_orders import OrderExecutionResult, PendingOrder
from backend.telegram_commands import (
    CONFIRM_AUTO_PROPOSAL_BUTTON_ONLY_TEXT,
    CONFIRM_BEFORE_PROMPT_TEXT,
    TelegramCommandHandler,
)

KST = ZoneInfo("Asia/Seoul")

_SAMPLE_ORDER = PendingOrder(
    chat_id="123",
    stock_name="삼성전자",
    stock_code="005930",
    side="BUY",
    quantity=10,
    price=75000,
    created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    order_type="LIMIT",
    callback_token="abc123",
    prompt_message_id=4242,
    # 기본값(auto_proposal)이 아닌 값을 싣는다. 직렬화가 이 필드를 빠뜨리면 왕복 뒤 기본값으로
    # 돌아와 test_redis_store_set_get_roundtrip이 잡는다 (#390).
    origin="user_command",
)


# ---------------------------------------------------------------------------
# FakeRedis — delete 지원 포함 (test_redis_state.py의 FakeRedis와 별도 정의)
# ---------------------------------------------------------------------------

class FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self._error: Exception | None = None

    def set_error(self, exc: Exception) -> None:
        """다음 호출부터 지정 예외를 발생시킨다."""
        self._error = exc

    def _check_error(self) -> None:
        if self._error is not None:
            raise self._error

    async def get(self, key):
        self._check_error()
        return self.store.get(key)

    async def set(self, key, value, *, ex=None, nx=False):
        self._check_error()
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key):
        self._check_error()
        self.store.pop(key, None)

    async def exists(self, key):
        self._check_error()
        return 1 if key in self.store else 0

    async def getdel(self, key):
        self._check_error()
        return self.store.pop(key, None)

    async def eval(self, script, numkeys, key, *args):
        """redis_state의 두 조건부 스크립트만 흉내 낸다 (#386).

        스크립트 본문이 실제 redis에서 같은 뜻인지는 test_redis_integration이 대조한다. 여기서
        모르는 스크립트를 조용히 받아 주면 새 스크립트가 대역을 그냥 통과하므로 끊는다.
        """
        self._check_error()
        assert numkeys == 1
        expected = args[0]
        if script == _DELETE_IF_UNCHANGED_SCRIPT:
            if self.store.get(key) != expected:
                return 0
            del self.store[key]
            return 1
        if script == _REPLACE_IF_UNCHANGED_SCRIPT:
            if self.store.get(key) != expected:
                return 0
            self.store[key] = args[1]
            return 1
        raise AssertionError(f"FakeRedis가 모르는 스크립트다: {script!r}")


# ---------------------------------------------------------------------------
# RedisPendingOrderStore 단위 테스트
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_store_set_get_roundtrip():
    """저장→조회 왕복: 모든 PendingOrder 필드가 손실 없이 복원된다."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    await store.set("123", _SAMPLE_ORDER)
    recovered = await store.get("123")

    assert recovered is not None
    assert recovered.chat_id == "123"
    assert recovered.stock_name == "삼성전자"
    assert recovered.stock_code == "005930"
    assert recovered.side == "BUY"
    assert recovered.quantity == 10
    assert recovered.price == 75000
    assert recovered.order_type == "LIMIT"
    assert recovered.callback_token == "abc123"
    # 확정 프롬프트 id(#386). 왕복에서 빠지면 텍스트 /confirm이 모든 주문을 "id 모름"으로 거절한다.
    assert recovered.prompt_message_id == 4242
    # 출처(#390). 왕복에서 빠지면 사용자 주문이 자동 제안으로 읽혀 텍스트 /confirm이 막힌다.
    assert recovered.origin == "user_command"
    # datetime 왕복: isoformat → fromisoformat 과정에서 timezone 보존
    assert recovered.created_at == _SAMPLE_ORDER.created_at
    assert recovered == _SAMPLE_ORDER


@pytest.mark.asyncio
async def test_redis_store_get_returns_none_when_missing():
    """키가 없으면 None을 반환한다 (TTL 만료 또는 미저장)."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    result = await store.get("nonexistent")

    assert result is None


@pytest.mark.asyncio
async def test_redis_store_get_returns_none_after_ttl_expiry():
    """TTL 만료 시뮬레이션: store에서 키를 직접 제거하면 get()이 None을 반환한다."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    await store.set("123", _SAMPLE_ORDER)
    # TTL 만료 시뮬레이션: Redis가 키를 자동 삭제한 것처럼 처리
    redis.store.clear()

    result = await store.get("123")

    assert result is None


@pytest.mark.asyncio
async def test_redis_store_set_writes_with_correct_ttl():
    """set() 호출 시 지정된 TTL로 Redis에 저장된다."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis, ttl_sec=300)
    # FakeRedis는 TTL을 실제 만료에 쓰지 않지만, set 파라미터를 확인할 수 있도록
    # set을 래핑해 기록한다.
    calls: list = []
    original_set = redis.set

    async def recording_set(key, value, *, ex=None, nx=False):
        calls.append((key, ex))
        return await original_set(key, value, ex=ex, nx=nx)

    redis.set = recording_set

    await store.set("123", _SAMPLE_ORDER)

    assert len(calls) == 1
    key, ttl = calls[0]
    assert "pending_order:123" in key
    assert ttl == 300


@pytest.mark.asyncio
async def test_redis_store_default_ttl_matches_constant():
    """기본 TTL이 PENDING_ORDER_TTL_SEC(600초)와 일치한다."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    calls: list = []
    original_set = redis.set

    async def recording_set(key, value, *, ex=None, nx=False):
        calls.append(ex)
        return await original_set(key, value, ex=ex, nx=nx)

    redis.set = recording_set
    await store.set("123", _SAMPLE_ORDER)

    assert calls[0] == PENDING_ORDER_TTL_SEC


@pytest.mark.asyncio
async def test_redis_store_delete_removes_order():
    """delete() 후 get()이 None을 반환한다."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    await store.set("123", _SAMPLE_ORDER)
    await store.delete("123")
    result = await store.get("123")

    assert result is None


@pytest.mark.asyncio
async def test_redis_store_has_true_after_set():
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    await store.set("123", _SAMPLE_ORDER)

    assert await store.has("123") is True


@pytest.mark.asyncio
async def test_redis_store_has_false_after_delete():
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    await store.set("123", _SAMPLE_ORDER)
    await store.delete("123")

    assert await store.has("123") is False


@pytest.mark.asyncio
async def test_redis_store_chat_id_isolation():
    """다른 chat_id 간 데이터가 격리된다."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    order_a = PendingOrder(
        chat_id="100", stock_name="삼성전자", stock_code="005930",
        side="BUY", quantity=1, price=70000,
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    order_b = PendingOrder(
        chat_id="200", stock_name="NAVER", stock_code="035420",
        side="SELL", quantity=2, price=200000,
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await store.set("100", order_a)
    await store.set("200", order_b)

    recovered_a = await store.get("100")
    recovered_b = await store.get("200")

    assert recovered_a is not None and recovered_a.stock_name == "삼성전자"
    assert recovered_b is not None and recovered_b.stock_name == "NAVER"

    await store.delete("100")

    assert await store.get("100") is None
    assert await store.get("200") is not None


@pytest.mark.asyncio
async def test_redis_store_get_raises_on_redis_failure():
    """Redis 장애 시 get()이 예외를 전파한다 (fail-closed)."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    redis.set_error(ConnectionError("Redis 연결 실패"))

    with pytest.raises(ConnectionError, match="Redis 연결 실패"):
        await store.get("123")


@pytest.mark.asyncio
async def test_redis_store_set_raises_on_redis_failure():
    """Redis 장애 시 set()이 예외를 전파한다 (fail-closed)."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    redis.set_error(ConnectionError("Redis 연결 실패"))

    with pytest.raises(ConnectionError):
        await store.set("123", _SAMPLE_ORDER)


@pytest.mark.asyncio
async def test_redis_store_key_pattern():
    """저장 키가 'finus:pending_order:{chat_id}' 패턴을 따른다."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    await store.set("999", _SAMPLE_ORDER)

    assert "finus:pending_order:999" in redis.store


# ---------------------------------------------------------------------------
# InMemoryPendingOrderStore 단위 테스트
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_store_set_get_roundtrip():
    store = InMemoryPendingOrderStore()

    await store.set("123", _SAMPLE_ORDER)
    result = await store.get("123")

    assert result is _SAMPLE_ORDER


@pytest.mark.asyncio
async def test_memory_store_get_none_when_missing():
    store = InMemoryPendingOrderStore()

    assert await store.get("missing") is None


@pytest.mark.asyncio
async def test_memory_store_delete():
    store = InMemoryPendingOrderStore()

    await store.set("123", _SAMPLE_ORDER)
    await store.delete("123")

    assert await store.get("123") is None
    assert await store.has("123") is False


@pytest.mark.asyncio
async def test_memory_store_has():
    store = InMemoryPendingOrderStore()

    assert await store.has("123") is False
    await store.set("123", _SAMPLE_ORDER)
    assert await store.has("123") is True


def test_memory_store_sync_getitem():
    """동기 __getitem__: 기존 테스트의 handler.pending_orders['123'] 호환."""
    store = InMemoryPendingOrderStore()
    store._store["123"] = _SAMPLE_ORDER

    assert store["123"] is _SAMPLE_ORDER


def test_memory_store_sync_contains():
    """동기 __contains__: 기존 테스트의 '123' in handler.pending_orders 호환."""
    store = InMemoryPendingOrderStore()

    assert "123" not in store
    store._store["123"] = _SAMPLE_ORDER
    assert "123" in store


def test_memory_store_sync_eq_with_empty_dict():
    """동기 __eq__: handler.pending_orders == {} 호환."""
    store = InMemoryPendingOrderStore()

    assert store == {}
    store._store["123"] = _SAMPLE_ORDER
    assert store != {}


# ---------------------------------------------------------------------------
# 통합: TTL 만료 후 /confirm, /cancel 시 명시적 오류 메시지
# ---------------------------------------------------------------------------

class FakeNotifier:
    def __init__(self, chat_id="123"):
        self.chat_id = chat_id
        self.bot_username = ""
        self.messages: list[str] = []
        self.reply_markups: list = []
        self.actions: list = []
        self.callback_answers: list = []

    async def send_text(self, text, *, reply_markup=None):
        self.messages.append(text)
        self.reply_markups.append(reply_markup)
        return True

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)
        return True

    async def answer_callback_query(self, callback_query_id, text=None):
        self.callback_answers.append((callback_query_id, text))
        return True


class FakeTradeLedger:
    """체결 원장 대역 (#259 2단계).

    주문 경로는 이제 원장 없이 돌지 않는다 — 체결 통지 outbox가 그 위에 서기 때문이다.
    대역을 안 주면 conftest의 _forbid_implicit_trade_ledger가 터뜨린다.
    """

    def __init__(self) -> None:
        self.results: list = []
        self.notified: list = []

    def record(self, result) -> int:
        self.results.append(result)
        return len(self.results)

    def mark_notified(self, trade_id, *, notified_at) -> None:
        self.notified.append(trade_id)


class FakeOrderGateway:
    def __init__(self) -> None:
        self.orders: list = []

    async def place_order(self, order):
        self.orders.append(order)
        return OrderExecutionResult(
            stock_code=order.stock_code,
            stock_name=order.stock_name,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            message="주문 접수",
            raw_result="{}",
        )


@pytest.mark.asyncio
async def test_confirm_after_ttl_expiry_gives_explicit_error():
    """/confirm 시 Redis TTL 만료(키 없음)이면 '확정할 대기 주문이 없습니다.' 응답.

    조용히 아무 일도 안 일어나지 않음을 고정한다(이슈 #63 요구사항).
    """
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        order_gateway=FakeOrderGateway(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 2, tzinfo=KST),
    )

    # Redis에 키가 없는 상태(TTL 만료 시뮬레이션)
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert notifier.messages[-1] == "확정할 대기 주문이 없습니다."


@pytest.mark.asyncio
async def test_cancel_after_ttl_expiry_gives_explicit_error():
    """/cancel 시 Redis TTL 만료(키 없음)이면 '취소할 대기 주문이 없습니다.' 응답."""
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        now_factory=lambda: datetime(2026, 5, 20, 10, 2, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/cancel"}})

    assert notifier.messages[-1] == "취소할 대기 주문이 없습니다."


@pytest.mark.asyncio
async def test_confirm_button_after_ttl_expiry_gives_stale_callback_text():
    """인라인 버튼 클릭 시 TTL 만료(키 없음)이면 ORDER_STALE_CALLBACK_TEXT 응답."""
    from backend.telegram_commands import ORDER_STALE_CALLBACK_TEXT

    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
    )

    await handler.handle_update({
        "callback_query": {
            "id": "cb-1",
            "data": "order:confirm:sometoken",
            "message": {"chat": {"id": 123}},
        }
    })

    assert len(notifier.callback_answers) == 1
    _, text = notifier.callback_answers[0]
    assert text == ORDER_STALE_CALLBACK_TEXT


@pytest.mark.asyncio
async def test_confirm_redis_failure_sends_error_message():
    """Redis 장애 시 /confirm이 사용자에게 오류 메시지를 보낸다 (fail-closed)."""
    redis = FakeRedis()
    redis.set_error(ConnectionError("Redis 연결 실패"))
    store = RedisPendingOrderStore(redis)
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        order_gateway=FakeOrderGateway(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert len(notifier.messages) == 1
    assert "저장소 오류" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_cancel_redis_failure_sends_error_message():
    """Redis 장애 시 /cancel이 사용자에게 오류 메시지를 보낸다 (fail-closed)."""
    redis = FakeRedis()
    redis.set_error(ConnectionError("Redis 연결 실패"))
    store = RedisPendingOrderStore(redis)
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/cancel"}})

    assert len(notifier.messages) == 1
    assert "저장소 오류" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_app_level_expiry_drops_order_before_confirm():
    """앱 레벨 ORDER_EXPIRES_AFTER(60초) 초과 시 confirm이 '없음' 응답을 반환한다.

    Redis TTL(600초)과 별개로 앱 레벨 체크가 동작함을 고정한다.
    """
    store = InMemoryPendingOrderStore()
    notifier = FakeNotifier()
    # 주문 생성: 10:00:00
    # confirm 시각: 10:01:01 → ORDER_EXPIRES_AFTER(60초) 초과
    created_at = datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST)
    confirm_at = datetime(2026, 5, 20, 10, 1, 1, tzinfo=KST)

    order = PendingOrder(
        chat_id="123", stock_name="삼성전자", stock_code="005930",
        side="BUY", quantity=1, price=70000,
        created_at=created_at,
        callback_token="tok",
    )
    await store.set("123", order)

    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        order_gateway=FakeOrderGateway(),
        now_factory=lambda: confirm_at,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert notifier.messages[-1] == "확정할 대기 주문이 없습니다."
    # 앱 레벨 expiry가 Redis에서도 삭제했는지 확인
    assert await store.has("123") is False


@pytest.mark.asyncio
async def test_confirm_cancel_flow_with_redis_store():
    """저장→confirm/cancel 정상 흐름 통합 테스트."""
    store = InMemoryPendingOrderStore()
    notifier = FakeNotifier()

    order = PendingOrder(
        chat_id="123", stock_name="삼성전자", stock_code="005930",
        side="BUY", quantity=1, price=70000,
        created_at=datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST),
        callback_token="tok",
    )
    await store.set("123", order)

    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, 30, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/cancel"}})

    assert "취소" in notifier.messages[-1]
    assert await store.has("123") is False


# ---------------------------------------------------------------------------
# Critical 회귀: claim 원자성으로 중복 체결 방지 (이슈 #63)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_confirm_calls_place_order_only_once():
    """같은 confirm을 두 번 처리해도 place_order는 한 번만 호출된다.

    재시작 후 동일한 Telegram update가 재전송될 때 중복 체결을 방지하는
    claim(GETDEL) 원자성을 검증한다(이슈 #63 Critical 회귀 테스트).
    """
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    notifier = FakeNotifier()
    gateway = FakeOrderGateway()
    ledger = FakeTradeLedger()

    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=1,
        price=75000,
        created_at=datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST),
        callback_token="tok",
        prompt_message_id=100,
        origin="user_command",
    )
    await store.set("123", order)

    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        order_gateway=gateway,
        trade_recorder=ledger,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, 30, tzinfo=KST),
    )
    confirm = {"message": {"chat": {"id": 123}, "message_id": 101, "text": "/confirm"}}

    # 첫 번째 confirm: 정상 체결
    await handler.handle_update(confirm)
    # 두 번째 confirm: 재시작 후 재전송된 update 시뮬레이션
    await handler.handle_update(confirm)

    # place_order는 정확히 한 번만 호출되어야 한다
    assert len(gateway.orders) == 1, (
        f"place_order가 {len(gateway.orders)}번 호출됨 — 중복 체결 회귀"
    )
    assert "주문 완료" in notifier.messages[0]
    assert notifier.messages[-1] == "확정할 대기 주문이 없습니다."
    # 체결이 한 번만 원장에 남았다 — 통지 outbox도 한 건만 책임진다 (#259 2단계).
    assert len(ledger.results) == 1
    assert ledger.notified == [1]


# ---------------------------------------------------------------------------
# set_if_absent NX 시맨틱 가드 (이슈 #63, PR #223 Improvement 1)
# ---------------------------------------------------------------------------

_ORDER_A = PendingOrder(
    chat_id="123",
    stock_name="삼성전자",
    stock_code="005930",
    side="BUY",
    quantity=1,
    price=75000,
    created_at=datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST),
    callback_token="tok-a",
)
_ORDER_B = PendingOrder(
    chat_id="123",
    stock_name="NAVER",
    stock_code="035420",
    side="BUY",
    quantity=2,
    price=200000,
    created_at=datetime(2026, 5, 20, 10, 0, 1, tzinfo=KST),
    callback_token="tok-b",
)


@pytest.mark.asyncio
async def test_memory_store_set_if_absent_returns_true_when_empty():
    """비어 있으면 True를 반환하고 주문을 저장한다.

    이 테스트가 잡는 mutation: set_if_absent가 항상 False를 반환하도록 변경.
    """
    store = InMemoryPendingOrderStore()

    result = await store.set_if_absent("123", _ORDER_A)

    assert result is True
    assert await store.get("123") is _ORDER_A


@pytest.mark.asyncio
async def test_memory_store_set_if_absent_returns_false_and_preserves_original():
    """이미 대기 주문이 있으면 False를 반환하고 기존 주문이 보존된다.

    이 테스트가 잡는 mutation: set_if_absent의 NX 검사 제거
    (``if chat_id in self._store: return False`` 두 줄을 지운 경우).
    NX 검사 없이 무조건 덮어쓰면 두 번째 /buy가 첫 번째 주문을 교체하고
    사용자는 의도한 것과 다른 주문을 체결하게 된다.
    """
    store = InMemoryPendingOrderStore()
    await store.set_if_absent("123", _ORDER_A)

    result = await store.set_if_absent("123", _ORDER_B)

    assert result is False
    # 원래 주문(A)이 B로 덮어쓰이지 않아야 한다
    stored = await store.get("123")
    assert stored is not None
    assert stored is _ORDER_A
    assert stored.callback_token == "tok-a"


@pytest.mark.asyncio
async def test_redis_store_set_if_absent_returns_true_when_empty():
    """비어 있으면 True를 반환하고 nx=True로 Redis에 저장한다.

    이 테스트가 잡는 mutation: set_if_absent에서 nx=True 제거.
    """
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    result = await store.set_if_absent("123", _ORDER_A)

    assert result is True
    recovered = await store.get("123")
    assert recovered is not None
    assert recovered.callback_token == "tok-a"


@pytest.mark.asyncio
async def test_redis_store_set_if_absent_returns_false_and_preserves_original():
    """이미 키가 있으면 False를 반환하고 기존 값이 보존된다.

    이 테스트가 잡는 mutation: set_if_absent의 nx=True 제거.
    nx=True 없이 무조건 set하면 FakeRedis가 덮어써서 두 번째 호출도 True를 반환한다.
    """
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    await store.set_if_absent("123", _ORDER_A)

    result = await store.set_if_absent("123", _ORDER_B)

    assert result is False
    # Redis에 저장된 값이 A여야 한다(B로 덮어쓰이면 안 됨)
    recovered = await store.get("123")
    assert recovered is not None
    assert recovered.callback_token == "tok-a"


# ---------------------------------------------------------------------------
# /confirm 재실행 표지 (#383)
# ---------------------------------------------------------------------------


class _ExpiryRecordingRedis(FakeRedis):
    def __init__(self):
        super().__init__()
        self.expiries: dict = {}

    async def set(self, key, value, *, ex=None, nx=False):
        result = await super().set(key, value, ex=ex, nx=nx)
        if result:
            self.expiries[key] = ex
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_store",
    [InMemoryPendingOrderStore, lambda: RedisPendingOrderStore(FakeRedis())],
    ids=["memory", "redis"],
)
async def test_confirm_update_marker_lets_only_the_first_owner_through(make_store):
    """처음 표지를 남긴 owner만 통과한다 (#383).

    이 테스트가 잡는 mutation: NX 제거(다른 owner가 표지를 덮어써 통과), owner 비교 제거
    (같은 프로세스의 재시도까지 거절).
    """
    store = make_store()

    assert await store.mark_confirm_update("123", 41, "process-a") is True
    # 같은 프로세스의 재시도
    assert await store.mark_confirm_update("123", 41, "process-a") is True
    # 재시작 뒤 같은 update의 재배달
    assert await store.mark_confirm_update("123", 41, "process-b") is False
    # 표지를 덮어쓰지 않았다 — 원래 프로세스의 재시도는 여전히 통과한다
    assert await store.mark_confirm_update("123", 41, "process-a") is True
    # 새 /confirm과 다른 채팅은 각자의 표지다
    assert await store.mark_confirm_update("123", 42, "process-b") is True
    assert await store.mark_confirm_update("456", 41, "process-b") is True


@pytest.mark.asyncio
async def test_redis_confirm_update_marker_outlives_telegram_update_retention():
    """표지는 Telegram이 미확정 update를 보관하는 24시간 동안 남는다 (#383).

    이 테스트가 잡는 mutation: ex 인자 제거(키가 영구히 쌓인다), 보관 기간보다 짧은 TTL
    (늦게 도착한 재배달이 표지 없이 통과한다).
    """
    redis = _ExpiryRecordingRedis()
    store = RedisPendingOrderStore(redis)

    await store.mark_confirm_update("123", 41, "process-a")

    assert list(redis.expiries.values()) == [CONFIRM_UPDATE_MARKER_TTL_SEC]
    assert CONFIRM_UPDATE_MARKER_TTL_SEC >= 24 * 60 * 60


@pytest.mark.asyncio
async def test_redis_confirm_update_marker_lives_outside_the_pending_order_namespace():
    """표지 키는 대기 주문 네임스페이스 밖에 둔다 (PR #385 리뷰).

    ``pending_order:`` 아래에 있으면 그 패턴으로 대기 주문을 훑는 코드가 표지까지 잡아
    PendingOrder로 역직렬화하다 실패한다.

    이 테스트가 잡는 mutation: 표지 키를 ``pending_order:`` 아래로 되돌림.
    """
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)

    await store.mark_confirm_update("123", 41, "process-a")

    pending_order_prefix = RedisKeys().pending_order("")
    assert list(redis.store) == [RedisKeys().confirm_update("123", 41)]
    assert not any(key.startswith(pending_order_prefix) for key in redis.store)


# ---------------------------------------------------------------------------
# 확정 프롬프트 message_id와 조건부 claim (#386)
# ---------------------------------------------------------------------------


def _raw_order(**overrides) -> str:
    """역직렬화 경로를 직접 보려고 저장값 JSON을 손으로 만든다."""
    data = {
        "chat_id": "123",
        "stock_name": "삼성전자",
        "stock_code": "005930",
        "side": "BUY",
        "quantity": 1,
        "price": 75000,
        "created_at": "2026-05-20T10:00:00+09:00",
        "order_type": "LIMIT",
        "callback_token": "tok",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.asyncio
async def test_redis_store_reads_a_legacy_value_without_prompt_id_as_unknown():
    """필드가 생기기 전의 저장값은 'id 모름'으로 읽힌다. 주문 자체는 버리지 않는다 (#386)."""
    redis = FakeRedis()
    redis.store[RedisKeys().pending_order("123")] = _raw_order()
    store = RedisPendingOrderStore(redis)

    order = await store.get("123")

    assert order is not None
    assert order.prompt_message_id is None
    assert order.prompted_before(10**9) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored", ["4242", True, 42.0], ids=["str", "bool", "float"]
)
async def test_redis_store_reads_a_non_integer_prompt_id_as_unknown(stored):
    """정수가 아닌 id는 'id 모름'이다. 주문은 버리지 않는다 (#386).

    문자열이 그대로 살면 판정의 ``<``가 TypeError로 터지고, bool은 int의 하위 타입이라
    True가 1로 대조된다. 주문을 남기므로 확정 버튼은 그대로 쓸 수 있다.

    이 테스트가 잡는 mutation: _deserialize의 정수 검사 제거, bool 제외 제거.
    """
    redis = FakeRedis()
    redis.store[RedisKeys().pending_order("123")] = _raw_order(prompt_message_id=stored)
    store = RedisPendingOrderStore(redis)

    order = await store.get("123")

    assert order is not None
    assert order.prompt_message_id is None


# ---------------------------------------------------------------------------
# 대기 주문의 출처 (#390)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user_command", "auto_proposal"])
async def test_redis_store_roundtrips_the_order_origin(origin):
    """출처는 저장소 왕복에서 그대로 살아남는다 (#390).

    이 테스트가 잡는 mutation: 직렬화에서 origin을 빼거나, 역직렬화가 저장된 값을 무시하고
    기본값으로 덮음(user_command가 auto_proposal로 돌아온다).
    """
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    order = replace(_SAMPLE_ORDER, origin=origin)

    await store.set("123", order)

    assert json.loads(redis.store[RedisKeys().pending_order("123")])["origin"] == origin
    assert await store.get("123") == order


@pytest.mark.asyncio
async def test_redis_store_reads_a_legacy_value_without_origin_as_auto_proposal():
    """출처 필드가 생기기 전의 저장값은 자동 제안으로 읽힌다 — 텍스트 /confirm 불가 (#390).

    주문 자체는 버리지 않는다. 확정 버튼은 출처를 보지 않으므로 그대로 쓸 수 있다.

    이 테스트가 잡는 mutation: PendingOrder.origin 기본값을 user_command로 바꿈.
    """
    redis = FakeRedis()
    redis.store[RedisKeys().pending_order("123")] = _raw_order(prompt_message_id=100)
    store = RedisPendingOrderStore(redis)

    order = await store.get("123")

    assert order is not None
    assert order.origin == "auto_proposal"
    assert order.text_confirm_allowed() is False
    assert order.confirmable_by_text(10**9) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    ["USER_COMMAND", "manual", "", 1, True, None, ["user_command"]],
    ids=["upper", "unknown", "empty", "int", "bool", "null", "list"],
)
async def test_redis_store_reads_an_unknown_origin_as_auto_proposal(stored):
    """목록 밖의 출처는 자동 제안으로 접는다. 주문은 버리지 않는다 (#390).

    그대로 두면 문자열 비교가 우연히 통과하지는 않지만, 저장값의 타입이 PendingOrder.origin의
    Literal과 어긋난 채 살아 남아 로그·표시·향후 판정이 그 값을 믿게 된다.

    이 테스트가 잡는 mutation: _deserialize의 origin 정규화 제거.
    """
    redis = FakeRedis()
    redis.store[RedisKeys().pending_order("123")] = _raw_order(
        prompt_message_id=100, origin=stored
    )
    store = RedisPendingOrderStore(redis)

    order = await store.get("123")

    assert order is not None
    assert order.origin == "auto_proposal"


@pytest.mark.asyncio
async def test_text_confirm_with_redis_store_does_not_execute_a_legacy_order_without_origin():
    """출처를 모르는 기존 저장값에 닿은 텍스트 /confirm은 실행하지 않는다 (#390).

    프롬프트 id는 알고 /confirm도 그 뒤에 보낸 것이라 #386 대조는 통과하는 값이다. 출처를 모르는
    것만으로 막혀야 한다(fail-closed). 주문은 남고, 확정 버튼 안내가 나간다.

    이 테스트가 잡는 mutation: 기본값을 user_command로 바꿈, claim_if 판정에서 출처 조건 제거.
    """
    redis = FakeRedis()
    redis.store[RedisKeys().pending_order("123")] = _raw_order(prompt_message_id=100)
    store = RedisPendingOrderStore(redis)
    gateway = FakeOrderGateway()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        order_gateway=gateway,
        trade_recorder=FakeTradeLedger(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, 30, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "message_id": 101, "text": "/confirm"}}
    )

    assert gateway.orders == []
    assert await store.has("123") is True
    assert notifier.messages == [CONFIRM_AUTO_PROPOSAL_BUTTON_ONLY_TEXT]


_BOTH_STORES = pytest.mark.parametrize(
    "make_store",
    [InMemoryPendingOrderStore, lambda: RedisPendingOrderStore(FakeRedis())],
    ids=["memory", "redis"],
)


@pytest.mark.asyncio
@_BOTH_STORES
async def test_claim_if_takes_only_an_order_that_passes(make_store):
    """조건을 통과한 주문만 꺼내고, 걸린 주문은 그대로 남긴다 (#386).

    이 테스트가 잡는 mutation: predicate 무시(걸린 주문까지 꺼낸다), 걸린 주문을 지움.
    """
    store = make_store()
    assert await store.claim_if("123", lambda order: True) == ConditionalClaim(
        order=None, claimed=False
    )

    await store.set("123", _SAMPLE_ORDER)
    refused = await store.claim_if("123", lambda order: False)
    assert refused == ConditionalClaim(order=_SAMPLE_ORDER, claimed=False)
    assert await store.get("123") == _SAMPLE_ORDER

    claimed = await store.claim_if("123", lambda order: order.callback_token == "abc123")
    assert claimed == ConditionalClaim(order=_SAMPLE_ORDER, claimed=True)
    assert await store.has("123") is False


@pytest.mark.asyncio
@_BOTH_STORES
async def test_set_prompt_message_id_writes_only_to_the_same_order(make_store):
    """같은 토큰의 주문에만 id를 남긴다 (#386).

    이 테스트가 잡는 mutation: 토큰 비교 제거(다른 주문에 옛 프롬프트의 id가 붙어, 그 주문의
    프롬프트를 보기 전에 보낸 /confirm이 통과한다).
    """
    store = make_store()
    order = replace(_SAMPLE_ORDER, prompt_message_id=None)
    assert await store.set_prompt_message_id("123", "abc123", 77) is False

    await store.set("123", order)
    assert await store.set_prompt_message_id("123", "other-token", 77) is False
    assert await store.get("123") == order

    assert await store.set_prompt_message_id("123", "abc123", 77) is True
    assert await store.get("123") == replace(order, prompt_message_id=77)


class _SwapOnFirstEvalRedis(FakeRedis):
    """첫 조건부 스크립트 직전에 키 값을 ``swap_to``로 바꾼다.

    GET과 EVAL 사이에 다른 쓰기(확정·만료 뒤 새 주문)가 끼어든 경우를 만든다.
    """

    def __init__(self) -> None:
        super().__init__()
        self.swap_to: str | None = None
        self.evals = 0

    async def eval(self, script, numkeys, key, *args):
        self.evals += 1
        if self.evals == 1 and self.swap_to is not None:
            self.store[key] = self.swap_to
        return await super().eval(script, numkeys, key, *args)


@pytest.mark.asyncio
async def test_redis_claim_if_rejudges_an_order_that_changed_after_the_read():
    """판정한 뒤 꺼내기 전에 주문이 바뀌면, 바뀐 주문으로 다시 판정한다 (#386).

    #386의 틈 그대로다. A를 읽고 통과시킨 사이 A가 치워지고 B가 들어온다. 무조건 지우면
    판정하지 않은 B를 꺼내 실행하게 된다 — 사용자가 본 적 없는 주문이다.

    이 테스트가 잡는 mutation: compare-and-delete를 무조건 DEL로 바꿈, 조건부 삭제가 져도
    claimed로 답함.
    """
    order_a = replace(_SAMPLE_ORDER, callback_token="tok-a", prompt_message_id=10)
    order_b = replace(_SAMPLE_ORDER, callback_token="tok-b", prompt_message_id=30)
    redis = _SwapOnFirstEvalRedis()
    store = RedisPendingOrderStore(redis)
    redis.swap_to = store._serialize(order_b)
    await store.set("123", order_a)

    outcome = await store.claim_if("123", lambda order: order.prompted_before(20))

    assert outcome == ConditionalClaim(order=order_b, claimed=False)
    assert await store.get("123") == order_b


@pytest.mark.asyncio
async def test_redis_claim_if_gives_up_instead_of_guessing_under_contention():
    """값이 매번 바뀌면 판정 없이 진행하지 않고 예외로 끝낸다 (#386).

    호출부는 이 예외를 "주문 저장소 오류"로 돌려준다. 주문은 건드리지 않는다.
    """

    class _AlwaysChangedRedis(FakeRedis):
        async def eval(self, script, numkeys, key, *args):
            return 0

    store = RedisPendingOrderStore(_AlwaysChangedRedis())
    await store.set("123", _SAMPLE_ORDER)

    with pytest.raises(PendingOrderContentionError):
        await store.claim_if("123", lambda order: True)
    assert await store.get("123") == _SAMPLE_ORDER


@pytest.mark.asyncio
async def test_redis_set_prompt_message_id_does_not_overwrite_an_order_that_replaced_it():
    """id를 남기는 사이 주문이 바뀌면 새 주문을 옛 주문으로 덮지 않는다 (#386).

    자동 제안이 B의 프롬프트를 보내는 사이 B가 확정되고 C가 들어온 경우다. 무조건 SET하면
    C가 사라지고, 이미 실행된 B가 대기 주문으로 되살아난다.

    이 테스트가 잡는 mutation: compare-and-set을 무조건 SET으로 바꿈.
    """
    order_b = replace(_SAMPLE_ORDER, callback_token="tok-b", prompt_message_id=None)
    order_c = replace(_SAMPLE_ORDER, callback_token="tok-c", prompt_message_id=None)
    redis = _SwapOnFirstEvalRedis()
    store = RedisPendingOrderStore(redis)
    redis.swap_to = store._serialize(order_c)
    await store.set("123", order_b)

    assert await store.set_prompt_message_id("123", "tok-b", 77) is False
    assert await store.get("123") == order_c


@pytest.mark.asyncio
async def test_late_text_confirm_with_redis_store_keeps_the_newer_order():
    """늦게 처리된 텍스트 /confirm은 redis 저장소에서도 그사이 생긴 주문을 실행하지 않는다 (#386).

    /confirm(110)은 B의 프롬프트(120)보다 먼저 보낸 것이다. B는 소비되지 않고 남고, B의
    프롬프트를 본 뒤 보낸 /confirm(121)이 B를 실행한다.
    """
    store = RedisPendingOrderStore(FakeRedis())
    order_b = replace(
        _SAMPLE_ORDER,
        quantity=2,
        callback_token="tok-b",
        created_at=datetime(2026, 5, 20, 10, 1, 10, tzinfo=KST),
        prompt_message_id=120,
    )
    await store.set("123", order_b)
    gateway = FakeOrderGateway()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        order_gateway=gateway,
        trade_recorder=FakeTradeLedger(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 1, 30, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "message_id": 110, "text": "/confirm"}}
    )

    assert gateway.orders == []
    assert await store.get("123") == order_b
    assert notifier.messages == [CONFIRM_BEFORE_PROMPT_TEXT]

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "message_id": 121, "text": "/confirm"}}
    )

    assert [order.callback_token for order in gateway.orders] == ["tok-b"]


@pytest.mark.asyncio
async def test_buy_command_second_call_rejected_after_race():
    """/buy 두 번째 호출이 이미 대기 주문이 있을 때 거절된다 (end-to-end).

    이 테스트가 잡는 mutation: has() 체크 제거 또는 set_if_absent가 항상 True를
    반환하도록 변경 (두 번째 /buy가 주문 프롬프트를 내보내면 실패).
    NX 시맨틱 자체(기존 값 보존)는 위의 단위 테스트들이 직접 고정한다.

    시나리오: 첫 번째 /buy가 set_if_absent로 슬롯을 획득한 뒤,
    두 번째 /buy가 has() fast-path 또는 set_if_absent NX 어느 경로에서든
    "이미 대기 중인 주문이 있습니다."로 거절당하는 흐름을 검증한다.
    """
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            # 실제 resolveStock처럼 완전 일치만 해석한다. 아무 이름이나 해석하면 "/buy 삼성전자 1
            # 75000"의 시장가 해석("삼성전자 1")까지 종목이 되어 모호함 안내로 끝난다 (#387).
            if arguments["stock_name"] != "삼성전자":
                raise RuntimeError(f"'{arguments['stock_name']}'의 종목 코드를 찾을 수 없습니다.")
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 75,000원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    store = InMemoryPendingOrderStore()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST),
    )

    # 첫 번째 /buy — 슬롯 획득
    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    # 두 번째 /buy — has() fast-path도 있지만, 여기서는 슬롯이 있으므로 fast-path에서 막힘.
    # set_if_absent NX 검사를 제거한 뮤턴트에서는 fast-path를 통과하더라도
    # set_if_absent 자체가 막아야 하므로, 한 번 더 호출해 set_if_absent 경로도 검증한다.
    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )

    # 두 번째 호출은 거절 메시지를 받아야 한다
    assert "이미 대기 중인 주문이 있습니다" in notifier.messages[-1]
    # 스토어에는 첫 번째 주문만 있어야 한다
    assert await store.has("123") is True
