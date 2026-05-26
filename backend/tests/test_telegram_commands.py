from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

import backend.telegram_commands as telegram_commands
from backend.config import TRADING_MCP_PARAMS
from backend.telegram_commands import (
    BUY_COMMAND_HELP,
    QUOTE_COMMAND_HELP,
    TELEGRAM_INTERACTIVE_HELP,
    TELEGRAM_MESSAGE_LIMIT,
    TELEGRAM_TRUNCATION_SUFFIX,
    TREND_COMMAND_HELP,
    TelegramCommandHandler,
    TelegramCommandPoller,
)
from backend.trading_orders import OrderExecutionResult, PendingOrder

KST = ZoneInfo("Asia/Seoul")


class FakeState:
    def __init__(self):
        self.mode = "urgent"

    async def get_telegram_alert_mode(self):
        return self.mode

    async def set_telegram_alert_mode(self, mode):
        self.mode = mode


class FakeNotifier:
    def __init__(self, chat_id="123", send_text_result=True, bot_username=""):
        self.chat_id = chat_id
        self.send_text_result = send_text_result
        self.bot_username = bot_username
        self.loaded_bot_username = False
        self.messages = []
        self.actions = []

    async def send_text(self, text):
        self.messages.append(text)
        return self.send_text_result

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)
        return True

    async def load_bot_username(self):
        self.loaded_bot_username = True
        return self.bot_username


class FakeOrderGateway:
    def __init__(self, *, error=None):
        self.error = error
        self.orders = []

    async def place_order(self, order):
        self.orders.append(order)
        if self.error is not None:
            raise self.error
        return OrderExecutionResult(
            stock_code=order.stock_code,
            stock_name=order.stock_name,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            message="주문 접수",
            raw_result="{}",
        )


class FakeTradeRecorder:
    def __init__(self, *, error=None):
        self.error = error
        self.results = []

    def record(self, result):
        self.results.append(result)
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_alerts_all_command_updates_mode_and_replies():
    state = FakeState()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, state_factory=lambda: state)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/alerts all"}})

    assert state.mode == "all"
    assert "all" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_alerts_status_reports_current_mode():
    state = FakeState()
    state.mode = "off"
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, state_factory=lambda: state)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/alerts status"}})

    assert state.mode == "off"
    assert "off" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_alerts_command_accepts_matching_telegram_bot_suffix():
    state = FakeState()
    notifier = FakeNotifier(bot_username="finus_bot")
    handler = TelegramCommandHandler(notifier=notifier, state_factory=lambda: state)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/alerts@finus_bot all"}}
    )

    assert state.mode == "all"
    assert "all" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_help_command_replies_with_supported_commands():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/help"}})

    assert notifier.messages == [TELEGRAM_INTERACTIVE_HELP]
    assert "/balance - 예수금·총자산·보유 종목 조회" in notifier.messages[-1]
    assert "/quote <종목명> - 현재가 조회" in notifier.messages[-1]
    assert "/trend <종목명> - 외국인·기관·개인 수급 조회" in notifier.messages[-1]
    assert "/buy <종목명> <수량> [지정가] - 매수 주문 준비" in notifier.messages[-1]
    assert "/sell <종목명> <수량> [지정가] - 매도 주문 준비" in notifier.messages[-1]
    assert "/confirm - 대기 주문 확정" in notifier.messages[-1]
    assert "/cancel - 대기 주문 취소" in notifier.messages[-1]
    assert "일반 문장은 NAT에게 바로 질문합니다." in notifier.messages[-1]


@pytest.mark.asyncio
async def test_unknown_slash_command_replies_with_help():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/unknown"}})

    assert notifier.messages == [TELEGRAM_INTERACTIVE_HELP]


@pytest.mark.asyncio
async def test_regular_text_calls_nat_with_telegram_conversation_id():
    calls = []

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        calls.append((provider, text, conversation_id))
        return "NAT 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "삼성전자 오늘 어때?"}})

    assert calls == [("nat", "삼성전자 오늘 어때?", "telegram:123")]
    assert notifier.actions == ["typing"]
    assert notifier.messages[-1] == "NAT 응답"


@pytest.mark.asyncio
async def test_regular_text_uses_actual_chat_id_in_conversation_id():
    calls = []

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        calls.append(conversation_id)
        return "ok"

    notifier = FakeNotifier(chat_id="456")
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 456}, "text": "계속 봐줘"}})

    assert calls == ["telegram:456"]


@pytest.mark.asyncio
async def test_unknown_slash_command_does_not_call_nat():
    calls = []

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        calls.append((provider, text, conversation_id))
        raise AssertionError("unknown slash command must not call NAT")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/unknown"}})

    assert calls == []
    assert notifier.messages == [TELEGRAM_INTERACTIVE_HELP]


@pytest.mark.asyncio
async def test_balance_command_calls_mcp_runner():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "잔고 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/balance"}})

    assert calls == [(TRADING_MCP_PARAMS, "get_balance", {})]
    assert notifier.actions == ["typing"]
    assert notifier.messages == ["잔고 응답"]


@pytest.mark.asyncio
async def test_quote_command_calls_mcp_runner_with_stock_name():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "현재가 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/quote 삼성전자"}})

    assert calls == [(TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": "삼성전자"})]
    assert notifier.actions == ["typing"]
    assert notifier.messages == ["현재가 응답"]


@pytest.mark.asyncio
async def test_quote_command_accepts_matching_telegram_bot_suffix():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "현재가 응답"

    notifier = FakeNotifier(bot_username="finus_bot")
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/quote@finus_bot 삼성전자"}}
    )

    assert calls == [(TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": "삼성전자"})]
    assert notifier.actions == ["typing"]
    assert notifier.messages == ["현재가 응답"]


@pytest.mark.asyncio
async def test_quote_command_rejects_foreign_telegram_bot_suffix():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "현재가 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/quote@other_bot 삼성전자"}}
    )

    assert calls == []
    assert notifier.actions == []
    assert notifier.messages == [TELEGRAM_INTERACTIVE_HELP]


@pytest.mark.asyncio
async def test_trend_command_calls_mcp_runner_with_stock_name():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "수급 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/trend 삼성전자"}})

    assert calls == [
        (TRADING_MCP_PARAMS, "get_investor_trading", {"stock_name": "삼성전자"})
    ]
    assert notifier.actions == ["typing"]
    assert notifier.messages == ["수급 응답"]


@pytest.mark.asyncio
async def test_quote_and_trend_missing_args_reply_with_usage():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/quote"}})
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/trend"}})

    assert notifier.messages == [QUOTE_COMMAND_HELP, TREND_COMMAND_HELP]


@pytest.mark.asyncio
async def test_buy_command_creates_pending_order_and_prompts_confirmation():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10 75,000"}}
    )

    assert calls == [
        (TRADING_MCP_PARAMS, "resolve_stock_code", {"stock_name": "삼성전자"}),
        (TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": "삼성전자"}),
        (TRADING_MCP_PARAMS, "get_balance", {}),
    ]
    assert "삼성전자 매수 주문 확인" in notifier.messages[-1]
    assert "/confirm" in notifier.messages[-1]
    assert "/cancel" in notifier.messages[-1]
    assert "현재가: 현재가" not in notifier.messages[-1]
    assert "잔고: 주문가능금액" not in notifier.messages[-1]
    assert handler.pending_orders["123"].stock_name == "삼성전자"
    assert handler.pending_orders["123"].stock_code == "005930"
    assert handler.pending_orders["123"].side == "BUY"
    assert handler.pending_orders["123"].quantity == 10
    assert handler.pending_orders["123"].price == 75000
    assert handler.pending_orders["123"].order_type == "LIMIT"


@pytest.mark.asyncio
async def test_buy_command_without_price_creates_market_order_and_prompts_confirmation():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10"}}
    )

    assert calls == [
        (TRADING_MCP_PARAMS, "resolve_stock_code", {"stock_name": "삼성전자"}),
        (TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": "삼성전자"}),
        (TRADING_MCP_PARAMS, "get_balance", {}),
    ]
    assert "삼성전자 매수 주문 확인" in notifier.messages[-1]
    assert "주문유형: 시장가" in notifier.messages[-1]
    assert "지정가:" not in notifier.messages[-1]
    assert "주문금액:" not in notifier.messages[-1]
    assert "/confirm" in notifier.messages[-1]
    assert "/cancel" in notifier.messages[-1]
    assert handler.pending_orders["123"].stock_name == "삼성전자"
    assert handler.pending_orders["123"].stock_code == "005930"
    assert handler.pending_orders["123"].side == "BUY"
    assert handler.pending_orders["123"].quantity == 10
    assert handler.pending_orders["123"].price == 0
    assert handler.pending_orders["123"].order_type == "MARKET"


@pytest.mark.asyncio
async def test_buy_command_accepts_stock_name_with_spaces():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "resolve_stock_code":
            return "LG 화학 (051910, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 75,000원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy LG 화학 10 75000"}}
    )

    assert calls[0] == (
        TRADING_MCP_PARAMS,
        "resolve_stock_code",
        {"stock_name": "LG 화학"},
    )
    assert "LG 화학 매수 주문 확인" in notifier.messages[-1]
    assert handler.pending_orders["123"].stock_name == "LG 화학"
    assert handler.pending_orders["123"].quantity == 10
    assert handler.pending_orders["123"].price == 75000
    assert handler.pending_orders["123"].order_type == "LIMIT"


@pytest.mark.asyncio
async def test_buy_command_rejects_unresolved_stock_code_before_quote_and_balance():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "resolve_stock_code":
            return "종목을 찾지 못했습니다"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 알수없는종목 1 75000"}}
    )

    assert calls == [
        (TRADING_MCP_PARAMS, "resolve_stock_code", {"stock_name": "알수없는종목"})
    ]
    assert notifier.messages[-1] == "주문 준비 실패: 종목코드를 확인할 수 없습니다."
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_cancel_removes_pending_order():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/cancel"}})

    assert "대기 주문을 취소했습니다." in notifier.messages[-1]
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_cancel_without_pending_order_replies():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/cancel"}})

    assert notifier.messages[-1] == "취소할 대기 주문이 없습니다."


@pytest.mark.asyncio
async def test_confirm_executes_gateway_and_records_trade():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    gateway = FakeOrderGateway()
    recorder = FakeTradeRecorder()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        order_gateway=gateway,
        trade_recorder=recorder,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert len(gateway.orders) == 1
    assert isinstance(gateway.orders[0], PendingOrder)
    assert len(recorder.results) == 1
    assert recorder.results[0].stock_code == "005930"
    assert notifier.actions == ["typing", "typing"]
    assert "주문 완료" in notifier.messages[-1]
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_confirm_gateway_success_recorder_failure_clears_pending_order():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    gateway = FakeOrderGateway()
    recorder = FakeTradeRecorder(error=RuntimeError("db commit failed"))
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        order_gateway=gateway,
        trade_recorder=recorder,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert len(gateway.orders) == 1
    assert len(recorder.results) == 1
    assert notifier.messages[-2] == "주문 완료: 주문 접수\n거래 이력 기록 실패: db commit failed"
    assert notifier.messages[-1] == "확정할 대기 주문이 없습니다."
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_confirm_without_gateway_keeps_pending_order():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert notifier.messages[-1] == "주문 실행 설정이 준비되지 않았습니다."
    assert "123" in handler.pending_orders


@pytest.mark.asyncio
async def test_confirm_gateway_ambiguous_failure_clears_pending_order_and_blocks_retry():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    gateway = FakeOrderGateway(error=RuntimeError("gateway down"))
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        order_gateway=gateway,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert len(gateway.orders) == 1
    assert notifier.messages[-2] == (
        "주문 실패 또는 상태 확인 필요: gateway down\n"
        "중복 주문 방지를 위해 대기 주문을 제거했습니다."
    )
    assert notifier.messages[-1] == "확정할 대기 주문이 없습니다."
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_confirm_real_order_guard_failure_keeps_pending_order():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    gateway = FakeOrderGateway(
        error=HTTPException(
            status_code=403,
            detail="실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다.",
        )
    )
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        order_gateway=gateway,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert notifier.messages[-1] == (
        "주문 실패: 실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다."
    )
    assert "123" in handler.pending_orders


@pytest.mark.asyncio
async def test_sell_command_rejects_market_closed():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "should not be called"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 8, 59, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/sell 삼성전자 10 75,000"}}
    )

    assert notifier.messages == ["주문 불가: 현재 장 운영 시간이 아닙니다. (평일 09:00~15:30)"]
    assert handler.pending_orders == {}
    assert calls == []


@pytest.mark.asyncio
async def test_buy_command_rejects_invalid_args_with_usage():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 x"}})

    assert notifier.messages == [BUY_COMMAND_HELP]


@pytest.mark.asyncio
async def test_sell_command_rejects_duplicate_pending_order():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/sell 삼성전자 1 75000"}}
    )

    assert (
        notifier.messages[-1]
        == "이미 대기 중인 주문이 있습니다. /confirm 또는 /cancel로 먼저 처리하세요."
    )
    assert handler.pending_orders["123"].side == "BUY"


@pytest.mark.asyncio
async def test_mcp_failure_replies_with_short_failure_message():
    class DetailedError(Exception):
        detail = "x" * 400

    async def mcp_runner(server_params, tool_name, arguments):
        raise DetailedError("fallback")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/balance"}})

    assert notifier.messages[-1] == f"조회 실패: {'x' * 300}"


@pytest.mark.asyncio
async def test_mcp_result_is_truncated_for_telegram_limit():
    async def mcp_runner(server_params, tool_name, arguments):
        return "a" * (TELEGRAM_MESSAGE_LIMIT + 100)

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/balance"}})

    message = notifier.messages[-1]
    assert len(message) == TELEGRAM_MESSAGE_LIMIT
    assert message.endswith(TELEGRAM_TRUNCATION_SUFFIX)


@pytest.mark.asyncio
async def test_nat_failure_replies_with_short_failure_message():
    async def fake_llm_runner(provider, text, *, conversation_id=None):
        raise RuntimeError("nat unavailable")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "질문"}})

    assert notifier.messages[-1] == "응답 생성 실패: nat unavailable"


@pytest.mark.asyncio
async def test_nat_response_is_truncated_for_telegram_limit():
    async def fake_llm_runner(provider, text, *, conversation_id=None):
        return "나" * (TELEGRAM_MESSAGE_LIMIT + 100)

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "긴 답변 줘"}})

    message = notifier.messages[-1]
    assert len(message) == TELEGRAM_MESSAGE_LIMIT
    assert message.endswith(TELEGRAM_TRUNCATION_SUFFIX)


@pytest.mark.asyncio
async def test_alerts_command_ignores_other_chats():
    state = FakeState()
    notifier = FakeNotifier(chat_id="123")
    handler = TelegramCommandHandler(notifier=notifier, state_factory=lambda: state)

    await handler.handle_update({"message": {"chat": {"id": 456}, "text": "/alerts all"}})

    assert state.mode == "urgent"
    assert notifier.messages == []


def test_poller_default_handler_uses_order_gateway_and_trade_recorder_factories(monkeypatch):
    gateway = object()
    recorder = object()
    notifier = FakeNotifier()

    monkeypatch.setattr(telegram_commands, "_create_order_gateway", lambda: gateway)
    monkeypatch.setattr(telegram_commands, "_create_trade_recorder", lambda: recorder)

    poller = TelegramCommandPoller(notifier=notifier)

    assert poller.handler.order_gateway is gateway
    assert poller.handler.trade_recorder is recorder


def test_poller_explicit_handler_does_not_call_dependency_factories(monkeypatch):
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    def fail_factory():
        raise AssertionError("factory must not be called for explicit handler")

    monkeypatch.setattr(telegram_commands, "_create_order_gateway", fail_factory)
    monkeypatch.setattr(telegram_commands, "_create_trade_recorder", fail_factory)

    poller = TelegramCommandPoller(notifier=notifier, handler=handler)

    assert poller.handler is handler


def test_create_order_gateway_uses_local_mcp_trading(monkeypatch):
    captured = {}

    class FakeGateway:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(telegram_commands, "McpTradingOrderGateway", FakeGateway)
    monkeypatch.setattr(telegram_commands, "KIS_ORDER_ENV", "sandbox")
    monkeypatch.setattr(telegram_commands, "KIS_REAL_ORDER_ENABLED", True)

    gateway = telegram_commands._create_order_gateway()

    assert isinstance(gateway, FakeGateway)
    assert captured == {
        "server_params": TRADING_MCP_PARAMS,
        "mcp_runner": telegram_commands.run_mcp_tool,
        "order_env": "demo",
        "real_order_enabled": True,
    }


@pytest.mark.asyncio
async def test_poller_keeps_offset_when_update_handling_fails(monkeypatch):
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class FailingHandler:
        async def handle_update(self, update):
            raise RuntimeError("redis unavailable")

    poller = TelegramCommandPoller(notifier=notifier, handler=FailingHandler())

    async def fake_get_updates():
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}}]

    async def stop_after_failure(delay):
        raise pytest.fail.Exception("stop after first failed polling iteration")

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)
    monkeypatch.setattr("backend.telegram_commands.asyncio.sleep", stop_after_failure)

    with pytest.raises(pytest.fail.Exception):
        await poller.run()

    assert poller.offset is None


@pytest.mark.asyncio
async def test_poller_loads_bot_username_before_updates(monkeypatch):
    notifier = FakeNotifier(bot_username="finus_bot")
    notifier.enabled = True
    notifier.bot_token = "token"

    class NoopHandler:
        async def handle_update(self, update):
            return None

    poller = TelegramCommandPoller(notifier=notifier, handler=NoopHandler())

    async def fake_get_updates():
        assert notifier.loaded_bot_username is True
        raise RuntimeError("stop after username load")

    async def stop_after_failure(delay):
        raise pytest.fail.Exception("stop after first failed polling iteration")

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)
    monkeypatch.setattr("backend.telegram_commands.asyncio.sleep", stop_after_failure)

    with pytest.raises(pytest.fail.Exception):
        await poller.run()

    assert poller.offset is None


@pytest.mark.asyncio
async def test_poller_keeps_offset_when_nat_response_send_fails(monkeypatch):
    calls = []
    notifier = FakeNotifier(send_text_result=False)
    notifier.enabled = True
    notifier.bot_token = "token"

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        calls.append((provider, text, conversation_id))
        return "NAT 응답"

    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)
    poller = TelegramCommandPoller(notifier=notifier, handler=handler)
    polls = 0

    async def fake_get_updates():
        nonlocal polls
        polls += 1
        if polls > 1:
            raise RuntimeError("stop after handled update")
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "질문"}}]

    async def stop_after_failure(delay):
        raise pytest.fail.Exception("stop after first failed polling iteration")

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)
    monkeypatch.setattr("backend.telegram_commands.asyncio.sleep", stop_after_failure)

    with pytest.raises(pytest.fail.Exception):
        await poller.run()

    assert calls == [("nat", "질문", "telegram:123")]
    assert poller.offset is None
