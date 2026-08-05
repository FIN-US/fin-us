from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

import backend.telegram_commands as telegram_commands
from backend.config import DART_MCP_PARAMS, NEWS_MCP_PARAMS, TRADING_MCP_PARAMS
from backend.telegram_commands import (
    BUY_COMMAND_HELP,
    CATALYST_COMMAND_HELP,
    EARNINGS_COMMAND_HELP,
    QUOTE_COMMAND_HELP,
    TELEGRAM_INTERACTIVE_HELP,
    TRADE_COMMAND_HELP,
    LOOKUP_COMMAND_HELP,
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


class FakeWatchlistRepo:
    def __init__(self, stocks: list[str] | None = None):
        self._watchlist: list[str] = list(stocks or [])

    async def get_watchlist(self):
        return sorted(self._watchlist)

    async def add_to_watchlist(self, stock: str):
        if stock not in self._watchlist:
            self._watchlist.append(stock)

    async def remove_from_watchlist(self, stock: str):
        self._watchlist = [s for s in self._watchlist if s != stock]


class FakeCatalystRepo:
    def __init__(self, events: dict[str, list[SimpleNamespace]] | None = None):
        self.events = events or {}
        self.calls = []

    async def list_upcoming(self, stock_name: str, *, today: date, limit: int = 20):
        self.calls.append((stock_name, today, limit))
        return list(self.events.get(stock_name, []))


class FakeNotifier:
    def __init__(self, chat_id="123", send_text_result=True, bot_username=""):
        self.chat_id = chat_id
        self.send_text_result = send_text_result
        self.bot_username = bot_username
        self.loaded_bot_username = False
        self.bot_commands = None
        self.messages = []
        self.reply_markups = []
        self.actions = []
        self.callback_answers = []

    async def send_text(self, text, *, reply_markup=None):
        self.messages.append(text)
        self.reply_markups.append(reply_markup)
        return self.send_text_result

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)
        return True

    async def answer_callback_query(self, callback_query_id, text=None):
        self.callback_answers.append((callback_query_id, text))
        return True

    async def load_bot_username(self):
        self.loaded_bot_username = True
        return self.bot_username

    async def set_bot_commands(self, commands):
        self.bot_commands = commands
        return True


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
    assert notifier.messages[-1].startswith("현재 Telegram 알림 모드: 🔕 off")


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
    assert "/trade - 매수·매도 주문 입력 안내" in notifier.messages[-1]
    assert "/lookup - 현재가·수급 조회 입력 안내" in notifier.messages[-1]
    assert "/visualize - Unity 포트폴리오 시각화 링크" in notifier.messages[-1]
    assert "/earnings <종목명> [기간] - DART 실적·뉴스 분석" in notifier.messages[-1]
    assert "/quote <종목명> - 현재가 조회" in notifier.messages[-1]
    assert "/trend <종목명> - 외국인·기관·개인 수급 조회" in notifier.messages[-1]
    assert "/buy <종목명> <수량> [지정가] - 매수 주문 준비" in notifier.messages[-1]
    assert "/sell <종목명> <수량> [지정가] - 매도 주문 준비" in notifier.messages[-1]
    assert "/confirm - 대기 주문 확정" in notifier.messages[-1]
    assert "/cancel - 대기 주문 취소" in notifier.messages[-1]
    assert "일반 문장은 NAT에게 바로 질문합니다." in notifier.messages[-1]
    assert notifier.reply_markups[-1] == {
        "inline_keyboard": [
            [
                {"text": "💰 잔고", "callback_data": "balance:refresh"},
                {"text": "🔔 알림", "callback_data": "alerts:status"},
            ],
            [
                {"text": "🧾 매매", "callback_data": "trade:menu"},
                {"text": "🔎 조회", "callback_data": "lookup:menu"},
            ],
        ]
    }


@pytest.mark.asyncio
async def test_alerts_button_updates_mode_and_replies():
    state = FakeState()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, state_factory=lambda: state)

    await handler.handle_update(
        {
            "callback_query": {
                "id": "alert-callback",
                "data": "alerts:off",
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert state.mode == "off"
    assert notifier.callback_answers == [("alert-callback", None)]
    assert "🔕 off" in notifier.messages[-1]
    assert notifier.reply_markups[-1]["inline_keyboard"][0][0]["text"] == "🚨 긴급만"
    assert notifier.reply_markups[-1]["inline_keyboard"][0][1]["text"] == "📣 전체"
    assert notifier.reply_markups[-1]["inline_keyboard"][0][2]["text"] == "🔕 끄기"
    assert notifier.reply_markups[-1]["inline_keyboard"][1][0]["text"] == "🔎 현재 상태"
    assert notifier.reply_markups[-1]["inline_keyboard"][0][0]["callback_data"] == (
        "alerts:urgent"
    )


@pytest.mark.asyncio
async def test_unknown_slash_command_replies_with_help():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/unknown"}})

    assert notifier.messages == [TELEGRAM_INTERACTIVE_HELP]


@pytest.mark.asyncio
async def test_trade_command_replies_with_entrypoint_buttons():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/trade"}})

    assert notifier.messages == [TRADE_COMMAND_HELP]
    assert notifier.reply_markups[-1] == {
        "inline_keyboard": [
            [
                {"text": "🛒 매수 입력법", "callback_data": "trade:buy"},
                {"text": "💸 매도 입력법", "callback_data": "trade:sell"},
            ]
        ]
    }


@pytest.mark.asyncio
async def test_lookup_command_replies_with_entrypoint_buttons():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/lookup"}})

    assert notifier.messages == [LOOKUP_COMMAND_HELP]
    assert notifier.reply_markups[-1] == {
        "inline_keyboard": [
            [
                {"text": "💵 현재가 입력법", "callback_data": "lookup:quote"},
                {"text": "📊 수급 입력법", "callback_data": "lookup:trend"},
            ]
        ]
    }


@pytest.mark.asyncio
async def test_visualize_command_replies_with_configured_url():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        visualization_url="http://100.64.0.10:8080/portfolio",
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/visualize"}})

    assert notifier.messages == [
        "Unity 포트폴리오 시각화:\nhttp://100.64.0.10:8080/portfolio"
    ]


@pytest.mark.asyncio
async def test_visualize_command_reports_missing_url():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, visualization_url="")

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/visualize"}})

    assert notifier.messages == [
        "시각화 URL이 설정되지 않았습니다. VISUALIZATION_URL 환경변수를 설정하세요."
    ]


@pytest.mark.asyncio
async def test_visualize_command_accepts_matching_telegram_bot_suffix():
    notifier = FakeNotifier(bot_username="finus_bot")
    handler = TelegramCommandHandler(
        notifier=notifier,
        visualization_url="http://100.64.0.10:8080/",
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/visualize@finus_bot"}}
    )

    assert notifier.messages[-1] == "Unity 포트폴리오 시각화:\nhttp://100.64.0.10:8080/"


@pytest.mark.asyncio
async def test_trade_menu_button_replies_with_buy_and_sell_guidance():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update(
        {
            "callback_query": {
                "id": "trade-menu",
                "data": "trade:menu",
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert notifier.callback_answers == [("trade-menu", None)]
    assert notifier.messages == [TRADE_COMMAND_HELP]


@pytest.mark.asyncio
async def test_lookup_quote_button_replies_with_quote_guidance():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update(
        {
            "callback_query": {
                "id": "lookup-quote",
                "data": "lookup:quote",
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert notifier.callback_answers == [("lookup-quote", None)]
    assert notifier.messages == [QUOTE_COMMAND_HELP]


def test_bot_command_menu_includes_all_user_commands():
    commands = [command["command"] for command in telegram_commands.TELEGRAM_BOT_COMMANDS]

    assert commands == [
        "help", "balance", "watch", "catalysts", "quote", "trend", "earnings",
        "alerts", "visualize", "trade", "lookup", "buy", "sell", "confirm", "cancel",
    ]


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
    assert notifier.reply_markups[-1] == {
        "inline_keyboard": [
            [{"text": "🔄 새로고침", "callback_data": "balance:refresh"}]
        ]
    }


@pytest.mark.asyncio
async def test_balance_refresh_button_calls_mcp_runner():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "잔고 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update(
        {
            "callback_query": {
                "id": "balance-callback",
                "data": "balance:refresh",
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert calls == [(TRADING_MCP_PARAMS, "get_balance", {})]
    assert notifier.callback_answers == [("balance-callback", None)]
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
    assert notifier.reply_markups[-1]["inline_keyboard"][0][0]["text"] == "📊 수급 보기"


@pytest.mark.asyncio
async def test_quote_result_trend_button_uses_same_stock_name():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "get_stock_quote":
            return "현재가 응답"
        if tool_name == "get_investor_trading":
            return "수급 응답"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/quote 삼성전자"}})
    callback_data = notifier.reply_markups[-1]["inline_keyboard"][0][0]["callback_data"]
    await handler.handle_update(
        {
            "callback_query": {
                "id": "trend-callback",
                "data": callback_data,
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert calls == [
        (TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": "삼성전자"}),
        (TRADING_MCP_PARAMS, "get_investor_trading", {"stock_name": "삼성전자"}),
    ]
    assert notifier.callback_answers == [("trend-callback", None)]
    assert notifier.messages[-1] == "수급 응답"


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
    assert notifier.reply_markups[-1]["inline_keyboard"][0][0]["text"] == "💵 현재가 보기"


@pytest.mark.asyncio
async def test_trend_result_quote_button_uses_same_stock_name():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "get_stock_quote":
            return "현재가 응답"
        if tool_name == "get_investor_trading":
            return "수급 응답"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/trend 삼성전자"}})
    callback_data = notifier.reply_markups[-1]["inline_keyboard"][0][0]["callback_data"]
    await handler.handle_update(
        {
            "callback_query": {
                "id": "quote-callback",
                "data": callback_data,
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert calls == [
        (TRADING_MCP_PARAMS, "get_investor_trading", {"stock_name": "삼성전자"}),
        (TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": "삼성전자"}),
    ]
    assert notifier.callback_answers == [("quote-callback", None)]
    assert notifier.messages[-1] == "현재가 응답"


@pytest.mark.asyncio
async def test_earnings_command_combines_dart_news_and_nat_analysis():
    mcp_calls = []
    llm_calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        mcp_calls.append((server_params, tool_name, arguments))
        if server_params is DART_MCP_PARAMS:
            return "DART 실적: 매출 +12%, 영업이익 +5%"
        if server_params is NEWS_MCP_PARAMS:
            return "뉴스: 반도체 수요 회복"
        raise AssertionError(f"unexpected server params: {server_params}")

    async def llm_runner(provider, text, *, conversation_id=None):
        llm_calls.append((provider, text, conversation_id))
        return "실적 분석 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        llm_runner=llm_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/earnings 삼성전자"}})

    assert mcp_calls == [
        (DART_MCP_PARAMS, "get_earnings_report", {"stock_name": "삼성전자"}),
        (NEWS_MCP_PARAMS, "get_market_news", {"stock_name": "삼성전자"}),
    ]
    assert len(llm_calls) == 1
    provider, prompt, conversation_id = llm_calls[0]
    assert provider == "nat"
    assert conversation_id == "telegram:123:earnings:%EC%82%BC%EC%84%B1%EC%A0%84%EC%9E%90"
    conversation_id.encode("ascii")
    assert "DART 실적: 매출 +12%, 영업이익 +5%" in prompt
    assert "뉴스: 반도체 수요 회복" in prompt
    assert "컨센서스 대비 서프라이즈/미스" in prompt
    assert "다음 분기 전망" in prompt
    assert "Markdown 문법" in prompt
    assert "호재`, `악재`, `중립" in prompt
    assert notifier.actions == ["typing"]
    assert notifier.messages == ["⚪ 중립\n실적 분석 응답"]


@pytest.mark.asyncio
async def test_earnings_command_sends_plain_text_with_verdict_emoji():
    async def mcp_runner(server_params, tool_name, arguments):
        if server_params is DART_MCP_PARAMS:
            return "DART 실적"
        if server_params is NEWS_MCP_PARAMS:
            return "뉴스"
        raise AssertionError(f"unexpected server params: {server_params}")

    async def llm_runner(provider, text, *, conversation_id=None):
        return "\n".join(
            [
                "## **호재**",
                "",
                "### **실적 요약**",
                "- **매출**: 전년 대비 증가",
                "- **영업이익**: 개선",
            ]
        )

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        llm_runner=llm_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/earnings 삼성전자"}})

    message = notifier.messages[-1]
    assert message.startswith("🟢 호재\n")
    assert "호재\n호재" not in message
    assert "#" not in message
    assert "*" not in message
    assert "- " not in message
    assert "실적 요약" in message
    assert "• 매출: 전년 대비 증가" in message


@pytest.mark.asyncio
async def test_earnings_command_passes_optional_period_to_dart():
    mcp_calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        mcp_calls.append((server_params, tool_name, arguments))
        if server_params is DART_MCP_PARAMS:
            return "DART 실적"
        if server_params is NEWS_MCP_PARAMS:
            return "뉴스"
        raise AssertionError(f"unexpected server params: {server_params}")

    async def llm_runner(provider, text, *, conversation_id=None):
        return "실적 분석 응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        llm_runner=llm_runner,
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/earnings 삼성전자 2025Q1"}}
    )

    assert mcp_calls[0] == (
        DART_MCP_PARAMS,
        "get_earnings_report",
        {"stock_name": "삼성전자", "period": "2025Q1"},
    )


@pytest.mark.asyncio
async def test_earnings_command_requires_stock_name():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "unexpected"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/earnings"}})

    assert calls == []
    assert notifier.messages == [EARNINGS_COMMAND_HELP]


@pytest.mark.asyncio
async def test_earnings_command_reports_collection_failure():
    async def mcp_runner(server_params, tool_name, arguments):
        raise RuntimeError("dart down")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/earnings 삼성전자"}})

    assert notifier.messages == ["조회 실패: dart down"]


@pytest.mark.asyncio
async def test_stale_market_button_does_not_call_mcp_runner():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "응답"

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)

    await handler.handle_update(
        {
            "callback_query": {
                "id": "stale-market-callback",
                "data": "market:quote:old-token",
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert calls == []
    assert notifier.callback_answers == [
        ("stale-market-callback", "이전 조회 버튼입니다. 최신 조회 메시지에서 다시 선택하세요.")
    ]


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
    assert notifier.reply_markups[-1] == {
        "inline_keyboard": [
            [
                {
                    "text": "✅ 확정",
                    "callback_data": (
                        f"order:confirm:{handler.pending_orders['123'].callback_token}"
                    ),
                },
                {
                    "text": "❌ 취소",
                    "callback_data": (
                        f"order:cancel:{handler.pending_orders['123'].callback_token}"
                    ),
                },
            ]
        ]
    }
    assert "현재가: 현재가" not in notifier.messages[-1]
    assert "잔고: 주문가능금액" not in notifier.messages[-1]
    assert handler.pending_orders["123"].stock_name == "삼성전자"
    assert handler.pending_orders["123"].stock_code == "005930"
    assert handler.pending_orders["123"].side == "BUY"
    assert handler.pending_orders["123"].quantity == 10
    assert handler.pending_orders["123"].price == 75000
    assert handler.pending_orders["123"].order_type == "LIMIT"


@pytest.mark.asyncio
async def test_buy_command_includes_current_price_line_when_quote_has_header():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "[삼성전자] 현재가 시세\n- 현재가: 354,000원\n- 전일 대비: +1,000 (0.28%)"
        if tool_name == "get_balance":
            return "[계좌 잔고 현황]\n- 거래가능금액: 5,546,116원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 354000"}}
    )

    assert "[삼성전자] 현재가 시세" not in notifier.messages[-1]
    assert "- 현재가: 354,000원" in notifier.messages[-1]
    assert "- 거래가능금액: 5,546,116원" in notifier.messages[-1]


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
async def test_natural_language_market_buy_creates_pending_order_without_nat():
    calls = []
    nat_calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        nat_calls.append((provider, text, conversation_id))
        raise AssertionError("natural language order must not call NAT")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        llm_runner=fake_llm_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "삼성전자 1주 시장가로 매수해줘"}}
    )

    assert nat_calls == []
    assert calls == [
        (TRADING_MCP_PARAMS, "resolve_stock_code", {"stock_name": "삼성전자"}),
        (TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": "삼성전자"}),
        (TRADING_MCP_PARAMS, "get_balance", {}),
    ]
    assert "삼성전자 매수 주문 확인" in notifier.messages[-1]
    assert "주문유형: 시장가" in notifier.messages[-1]
    assert notifier.reply_markups[-1] == {
        "inline_keyboard": [
            [
                {
                    "text": "✅ 확정",
                    "callback_data": (
                        f"order:confirm:{handler.pending_orders['123'].callback_token}"
                    ),
                },
                {
                    "text": "❌ 취소",
                    "callback_data": (
                        f"order:cancel:{handler.pending_orders['123'].callback_token}"
                    ),
                },
            ]
        ]
    }
    assert handler.pending_orders["123"].stock_name == "삼성전자"
    assert handler.pending_orders["123"].side == "BUY"
    assert handler.pending_orders["123"].quantity == 1
    assert handler.pending_orders["123"].price == 0
    assert handler.pending_orders["123"].order_type == "MARKET"


@pytest.mark.asyncio
async def test_natural_language_limit_sell_creates_pending_order_without_nat():
    calls = []
    nat_calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "resolve_stock_code":
            return "NAVER (035420, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 200,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        nat_calls.append((provider, text, conversation_id))
        raise AssertionError("natural language order must not call NAT")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        llm_runner=fake_llm_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "NAVER 2주 200,000원에 매도해줘"}}
    )

    assert nat_calls == []
    assert calls[0] == (
        TRADING_MCP_PARAMS,
        "resolve_stock_code",
        {"stock_name": "NAVER"},
    )
    assert "NAVER 매도 주문 확인" in notifier.messages[-1]
    assert "주문유형: 지정가" in notifier.messages[-1]
    assert "지정가: 200,000원" in notifier.messages[-1]
    assert handler.pending_orders["123"].stock_name == "NAVER"
    assert handler.pending_orders["123"].side == "SELL"
    assert handler.pending_orders["123"].quantity == 2
    assert handler.pending_orders["123"].price == 200000
    assert handler.pending_orders["123"].order_type == "LIMIT"


@pytest.mark.asyncio
async def test_ambiguous_natural_language_order_replies_with_usage_without_nat():
    nat_calls = []

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        nat_calls.append((provider, text, conversation_id))
        raise AssertionError("ambiguous natural language order must not call NAT")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=fake_llm_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "삼성전자 1주 시장가 75000원에 매수해줘"}}
    )

    assert nat_calls == []
    assert notifier.messages == [
        "자연어 주문을 해석할 수 없습니다. /buy 또는 /sell 형식으로 입력하세요."
    ]
    assert handler.pending_orders == {}


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


def test_extract_stock_code_numeric():
    handler = TelegramCommandHandler(notifier=FakeNotifier())
    assert handler._extract_stock_code("삼성전자 (005930, KOSPI)") == "005930"


def test_extract_stock_code_alphanumeric():
    handler = TelegramCommandHandler(notifier=FakeNotifier())
    assert handler._extract_stock_code("덕양에너젠 (0001A0, KOSDAQ)") == "0001A0"


def test_extract_stock_code_ignores_parentheses_in_stock_name():
    """종목명에 괄호가 있어도 코드+쉼표 앵커가 코드만 뽑아낸다.

    종목마스터 실제 항목이다. 이름의 괄호가 닫히지 않는 종목(0015E0)까지 있어
    단순 괄호 매칭으로는 안전하지 않다.
    """
    handler = TelegramCommandHandler(notifier=FakeNotifier())
    assert (
        handler._extract_stock_code("한투글로벌넥스트웨이브1(A-e) (F70100027, KOSPI)")
        == "F70100027"
    )
    assert (
        handler._extract_stock_code(
            "KIWOOM 엔비디아미국30년국채혼합액티브(H (0015E0, KOSPI)"
        )
        == "0015E0"
    )


def test_extract_stock_code_accepts_seven_and_nine_char_codes():
    """코드 길이 상한을 두지 않는 이유를 고정한다.

    종목마스터 코드 길이는 6자 3,889 / 7자 ETN 389 / 9자 펀드 75종목이다.
    정규식을 {6}으로 좁히면 464종목이, {6,7}로 좁히면 9자 펀드 75종목이
    조용히 추출 실패로 돌아간다.
    """
    handler = TelegramCommandHandler(notifier=FakeNotifier())
    assert (
        handler._extract_stock_code("신한 레버리지 다우존스지수 선물 ETN(H) (Q500020, KOSPI)")
        == "Q500020"
    )
    assert (
        handler._extract_stock_code("한투글로벌넥스트웨이브1(A) (F70100026, KOSPI)")
        == "F70100026"
    )


def test_extract_stock_code_returns_none_when_unmatched():
    handler = TelegramCommandHandler(notifier=FakeNotifier())
    assert handler._extract_stock_code("종목을 찾지 못했습니다") is None


@pytest.mark.parametrize(
    "stock_name, resolved, stock_code",
    [
        ("덕양에너젠", "덕양에너젠 (0001A0, KOSDAQ)", "0001A0"),
        (
            "한투글로벌넥스트웨이브1(A)",
            "한투글로벌넥스트웨이브1(A) (F70100026, KOSPI)",
            "F70100026",
        ),
    ],
)
@pytest.mark.parametrize(
    "text_template",
    [
        "/buy {name} 10",
        "/sell {name} 10",
        # 자연어 경로는 _parse_natural_order_text가 "매수"/"매도" 리터럴을 요구한다.
        # 매수·매도 양쪽 분기를 모두 거쳐 _handle_order_command로 합류하는지 본다.
        "{name} 10주 시장가로 매수해줘",
        "{name} 10주 시장가로 매도해줘",
    ],
)
@pytest.mark.asyncio
async def test_order_command_rejects_unorderable_stock_code_before_quote_and_balance(
    stock_name, resolved, stock_code, text_template
):
    """주문 미지원 코드는 시세·잔고 조회 전에 끊고 사유를 정확히 알린다.

    코드 추출은 성공하지만 mcp-trading/order.js의 buildCashOrderBody()가 숫자
    코드만 받는다(#73). 가드가 없으면 /confirm 이후에야 실패하면서 60초 대기
    슬롯까지 점유한다.

    자연어 주문도 _handle_order_command로 합류하므로 세 경로를 함께 고정한다.
    """
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "resolve_stock_code":
            return resolved
        raise AssertionError(f"주문 불가 종목인데 호출됨: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {
            "message": {
                "chat": {"id": 123},
                "text": text_template.format(name=stock_name),
            }
        }
    )

    assert [call[1] for call in calls] == ["resolve_stock_code"]
    message = notifier.messages[-1]
    # 이름과 코드를 따로 단언하면 resolve_stock_code 응답("덕양에너젠 (0001A0, KOSDAQ)")을
    # 그대로 흘려보내도 통과한다. 조합된 형태를 단언해야 실제로 우리가 만든 문장이 나갔음이
    # 고정된다.
    assert f"{stock_name}({stock_code})" in message
    assert "주문을 지원하지 않습니다" in message
    # 왜 안 되는지를 설명하는 문장이 이 수정의 핵심이므로 함께 고정한다.
    assert "ETN·펀드 등 영숫자 종목코드는 아직 주문 대상이 아닙니다." in message
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
async def test_cancel_button_removes_pending_order():
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
    callback_data = notifier.reply_markups[-1]["inline_keyboard"][0][1]["callback_data"]
    await handler.handle_update(
        {
            "callback_query": {
                "id": "callback-1",
                "data": callback_data,
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert notifier.callback_answers == [("callback-1", None)]
    assert "대기 주문을 취소했습니다." in notifier.messages[-1]
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_cancel_without_pending_order_replies():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/cancel"}})

    assert notifier.messages[-1] == "취소할 대기 주문이 없습니다."


def test_format_order_prompt_shows_warning_when_stock_name_equals_stock_code():
    """미해석 코드(name == code)일 때 확인 메시지에 경고 문구가 포함된다."""
    from backend.trading_orders import PendingOrder

    handler = TelegramCommandHandler(notifier=FakeNotifier())
    order = PendingOrder(
        chat_id="123",
        stock_code="123456",
        stock_name="123456",
        side="BUY",
        quantity=10,
        price=0,
        order_type="MARKET",
        callback_token="tok",
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    message = handler._format_order_prompt(order, "", "")
    assert "⚠️ 종목명을 확인하지 못했습니다. 입력한 코드가 맞는지 다시 확인하세요." in message


def test_format_order_prompt_no_warning_when_stock_name_differs_from_stock_code():
    """정상 종목(name != code)일 때 경고 문구가 없다."""
    from backend.trading_orders import PendingOrder

    handler = TelegramCommandHandler(notifier=FakeNotifier())
    order = PendingOrder(
        chat_id="123",
        stock_code="005930",
        stock_name="삼성전자",
        side="BUY",
        quantity=10,
        price=0,
        order_type="MARKET",
        callback_token="tok",
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    message = handler._format_order_prompt(order, "", "")
    assert "⚠️ 종목명을 확인하지 못했습니다." not in message


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
async def test_confirm_button_executes_gateway_and_records_trade():
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
    callback_data = notifier.reply_markups[-1]["inline_keyboard"][0][0]["callback_data"]
    await handler.handle_update(
        {
            "callback_query": {
                "id": "callback-2",
                "data": callback_data,
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert notifier.callback_answers == [("callback-2", None)]
    assert len(gateway.orders) == 1
    assert len(recorder.results) == 1
    assert "주문 완료" in notifier.messages[-1]
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_tokenless_old_confirm_button_does_not_execute_current_pending_order():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        raise AssertionError(f"unexpected tool: {tool_name}")

    gateway = FakeOrderGateway()
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
    await handler.handle_update(
        {
            "callback_query": {
                "id": "old-callback",
                "data": "order:confirm",
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert notifier.callback_answers == [
        ("old-callback", "이전 주문 버튼입니다. 최신 주문 메시지에서 다시 선택하세요.")
    ]
    assert gateway.orders == []
    assert "123" in handler.pending_orders


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
async def test_poller_sets_bot_command_menu_before_updates(monkeypatch):
    notifier = FakeNotifier(bot_username="finus_bot")
    notifier.enabled = True
    notifier.bot_token = "token"

    class NoopHandler:
        async def handle_update(self, update):
            return None

    poller = TelegramCommandPoller(notifier=notifier, handler=NoopHandler())

    async def fake_get_updates():
        assert notifier.bot_commands == telegram_commands.TELEGRAM_BOT_COMMANDS
        raise RuntimeError("stop after bot command setup")

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


# ---------------------------------------------------------------------------
# /watch command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watch_list_empty_shows_empty_message():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, watchlist_repo=FakeWatchlistRepo())

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    assert "관심 종목이 없습니다" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_watch_list_shows_stocks_with_refresh_button():
    notifier = FakeNotifier()

    async def mcp_runner(server_params, tool_name, arguments):
        return f"[{arguments.get('stock_name')}] 현재가 시세\n- 현재가: 75,400원\n- 전일 대비: +930 (1.23%)"

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["삼성전자", "NAVER"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    assert "NAVER" in notifier.messages[-1]
    assert "삼성전자" in notifier.messages[-1]
    markup = notifier.reply_markups[-1]
    assert markup is not None
    buttons_flat = [btn for row in markup["inline_keyboard"] for btn in row]
    callback_datas = [btn["callback_data"] for btn in buttons_flat]
    assert "watch:list" in callback_datas


@pytest.mark.asyncio
async def test_watch_add_adds_stock_and_confirms():
    repo = FakeWatchlistRepo()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, watchlist_repo=repo)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch add 삼성전자"}})

    assert "삼성전자" in repo._watchlist
    assert "삼성전자" in notifier.messages[-1]
    assert "추가" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_watch_remove_removes_stock_and_confirms():
    repo = FakeWatchlistRepo(["삼성전자"])
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, watchlist_repo=repo)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch remove 삼성전자"}})

    assert "삼성전자" not in repo._watchlist
    assert "삼성전자" in notifier.messages[-1]
    assert "삭제" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_watch_add_without_stock_name_shows_help():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, watchlist_repo=FakeWatchlistRepo())

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch add"}})

    assert "사용법" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_watch_remove_without_stock_name_shows_help():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, watchlist_repo=FakeWatchlistRepo())

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch remove"}})

    assert "사용법" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_watch_unknown_subcommand_shows_help():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier, watchlist_repo=FakeWatchlistRepo())

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch foo"}})

    assert "사용법" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_watch_list_callback_refreshes_watchlist():
    notifier = FakeNotifier()

    async def mcp_runner(server_params, tool_name, arguments):
        return "[SK하이닉스] 현재가 시세\n- 현재가: 180,000원\n- 전일 대비: -990 (-0.55%)"

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["SK하이닉스"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update(
        {
            "callback_query": {
                "id": "cb-watch",
                "data": "watch:list",
                "message": {"chat": {"id": 123}},
            }
        }
    )

    assert notifier.callback_answers == [("cb-watch", None)]
    assert "SK하이닉스" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_watch_list_shows_rising_stock_with_red_emoji():
    notifier = FakeNotifier()

    async def mcp_runner(server_params, tool_name, arguments):
        return "[삼성전자] 현재가 시세\n- 현재가: 75,400원\n- 전일 대비: +930 (1.23%)"

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["삼성전자"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    msg = notifier.messages[-1]
    assert "🔴" in msg
    assert "▲" in msg
    assert "+1.23%" in msg
    assert "75,400원" in msg
    assert "삼성전자" in msg


@pytest.mark.asyncio
async def test_watch_list_shows_falling_stock_with_blue_emoji():
    notifier = FakeNotifier()

    async def mcp_runner(server_params, tool_name, arguments):
        return "[SK하이닉스] 현재가 시세\n- 현재가: 180,000원\n- 전일 대비: -990 (-0.55%)"

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["SK하이닉스"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    msg = notifier.messages[-1]
    assert "🔵" in msg
    assert "▼" in msg
    assert "-0.55%" in msg
    assert "180,000원" in msg
    assert "SK하이닉스" in msg


@pytest.mark.asyncio
async def test_watch_list_shows_flat_stock_with_white_emoji():
    notifier = FakeNotifier()

    async def mcp_runner(server_params, tool_name, arguments):
        return "[카카오] 현재가 시세\n- 현재가: 45,000원\n- 전일 대비: 0 (0.00%)"

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["카카오"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    msg = notifier.messages[-1]
    assert "⬜" in msg
    assert "0.00%" in msg
    assert "45,000원" in msg
    assert "카카오" in msg


@pytest.mark.asyncio
async def test_watch_list_shows_failure_message_when_quote_fails():
    notifier = FakeNotifier()

    async def mcp_runner(server_params, tool_name, arguments):
        raise RuntimeError("connection error")

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["현대차"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    msg = notifier.messages[-1]
    assert "현대차" in msg
    assert "조회 실패" in msg


@pytest.mark.asyncio
async def test_watch_list_shows_failure_message_when_rate_is_unparseable():
    notifier = FakeNotifier()

    async def mcp_runner(server_params, tool_name, arguments):
        return "[LG화학] 현재가 시세\n- 현재가: 312,000원\n- 전일 대비: - (-%)"

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["LG화학"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    msg = notifier.messages[-1]
    assert "LG화학" in msg
    assert "조회 실패" in msg
    assert "🔴" not in msg
    assert "▲" not in msg


@pytest.mark.asyncio
async def test_watch_list_calls_quote_for_each_stock_sequentially():
    notifier = FakeNotifier()
    calls: list[str] = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append(arguments.get("stock_name"))
        return f"[{arguments.get('stock_name')}] 현재가 시세\n- 현재가: 75,400원\n- 전일 대비: +930 (1.23%)"

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["삼성전자", "NAVER"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    assert calls == ["NAVER", "삼성전자"]  # FakeState.get_watchlist returns sorted


@pytest.mark.asyncio
async def test_watch_list_waits_between_quote_calls(monkeypatch):
    notifier = FakeNotifier()
    events: list[tuple[str, str | float]] = []
    expected_delay_seconds = 1.1

    async def mcp_runner(server_params, tool_name, arguments):
        events.append(("quote", arguments.get("stock_name")))
        return f"[{arguments.get('stock_name')}] 현재가 시세\n- 현재가: 75,400원\n- 전일 대비: +930 (1.23%)"

    async def fake_sleep(seconds):
        events.append(("sleep", seconds))

    monkeypatch.setattr("backend.telegram_commands.asyncio.sleep", fake_sleep)

    handler = TelegramCommandHandler(
        notifier=notifier,
        watchlist_repo=FakeWatchlistRepo(["CCC", "AAA", "BBB"]),
        mcp_runner=mcp_runner,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/watch list"}})

    assert events == [
        ("quote", "AAA"),
        ("sleep", expected_delay_seconds),
        ("quote", "BBB"),
        ("sleep", expected_delay_seconds),
        ("quote", "CCC"),
    ]


@pytest.mark.asyncio
async def test_help_command_includes_watch_description():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/help"}})

    assert "/watch" in notifier.messages[-1]


# ---------------------------------------------------------------------------
# /catalysts command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_catalysts_without_stock_name_shows_help():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        catalyst_repo=FakeCatalystRepo(),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/catalysts"}})

    assert notifier.messages == [CATALYST_COMMAND_HELP]


@pytest.mark.asyncio
async def test_catalysts_lists_upcoming_events_for_stock():
    notifier = FakeNotifier()
    repo = FakeCatalystRepo(
        {
            "삼성전자": [
                SimpleNamespace(
                    event_type="earnings",
                    event_date=date(2026, 1, 28),
                    description="분기 실적 발표",
                ),
                SimpleNamespace(
                    event_type="dividend",
                    event_date=date(2026, 1, 30),
                    description="배당락일",
                ),
            ]
        }
    )
    handler = TelegramCommandHandler(
        notifier=notifier,
        catalyst_repo=repo,
        now_factory=lambda: datetime(2026, 1, 27, 9, 0, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/catalysts 삼성전자"}})

    assert repo.calls == [("삼성전자", date(2026, 1, 27), 20)]
    message = notifier.messages[-1]
    assert message.startswith("📅 삼성전자 예정 이벤트")
    assert "2026-01-28" in message
    assert "분기 실적 발표" in message
    assert "2026-01-30" in message
    assert "배당락일" in message


@pytest.mark.asyncio
async def test_catalysts_reports_empty_events_for_stock():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        catalyst_repo=FakeCatalystRepo(),
        now_factory=lambda: datetime(2026, 1, 27, 9, 0, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/catalysts NAVER"}})

    assert "NAVER 예정 이벤트가 없습니다" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_help_and_bot_menu_include_catalysts_command(monkeypatch):
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/help"}})

    assert "/catalysts <종목명> - 예정 촉매 이벤트 조회" in notifier.messages[-1]
    assert "catalysts" in [command["command"] for command in telegram_commands.TELEGRAM_BOT_COMMANDS]
