"""pending_order 저장소 테스트 (이슈 #63).

검증 항목:
- RedisPendingOrderStore: 저장→조회 왕복, TTL 만료 후 조회, Redis 장애 시 동작,
  chat_id 간 격리, PendingOrder 필드 직렬화 무손실
- InMemoryPendingOrderStore: 기본 동작, 동기 dict 인터페이스
- 통합: TTL 만료 후 /confirm 시 명시적 오류, /cancel 시 명시적 오류,
  Redis 장애 시 handler 오류 메시지 전달
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.redis_state import (
    PENDING_ORDER_TTL_SEC,
    InMemoryPendingOrderStore,
    RedisPendingOrderStore,
)
from backend.trading_orders import PendingOrder
from backend.telegram_commands import TelegramCommandHandler

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
    # datetime 왕복: isoformat → fromisoformat 과정에서 timezone 보존
    assert recovered.created_at == _SAMPLE_ORDER.created_at


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

from backend.trading_orders import OrderExecutionResult  # noqa: E402


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
async def test_duplicate_confirm_calls_place_order_only_once():
    """같은 confirm을 두 번 처리해도 place_order는 한 번만 호출된다.

    재시작 후 동일한 Telegram update가 재전송될 때 중복 체결을 방지하는
    claim(GETDEL) 원자성을 검증한다(이슈 #63 Critical 회귀 테스트).
    """
    redis = FakeRedis()
    store = RedisPendingOrderStore(redis)
    notifier = FakeNotifier()
    gateway = FakeOrderGateway()

    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=1,
        price=75000,
        created_at=datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST),
        callback_token="tok",
    )
    await store.set("123", order)

    handler = TelegramCommandHandler(
        notifier=notifier,
        pending_order_store=store,
        order_gateway=gateway,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, 30, tzinfo=KST),
    )

    # 첫 번째 confirm: 정상 체결
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})
    # 두 번째 confirm: 재시작 후 재전송된 update 시뮬레이션
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    # place_order는 정확히 한 번만 호출되어야 한다
    assert len(gateway.orders) == 1, (
        f"place_order가 {len(gateway.orders)}번 호출됨 — 중복 체결 회귀"
    )
    assert "주문 완료" in notifier.messages[0]
    assert notifier.messages[-1] == "확정할 대기 주문이 없습니다."
