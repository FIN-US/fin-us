import asyncio
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
    MAX_PROGRESS_EDITS,
    NAT_PROGRESS_MESSAGE,
    QUOTE_COMMAND_HELP,
    REASONING_FOOTNOTE_SEPARATOR,
    TELEGRAM_INTERACTIVE_HELP,
    TRADE_COMMAND_HELP,
    LOOKUP_COMMAND_HELP,
    TELEGRAM_MESSAGE_LIMIT,
    TELEGRAM_TRUNCATION_SUFFIX,
    TREND_COMMAND_HELP,
    UNRESOLVED_STOCK_WARNING,
    TelegramCommandHandler,
    TelegramCommandPoller,
    _ProgressMessage,
    _reasoning_footnote,
)
from backend.services import NatAnswer
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


@pytest.mark.asyncio
async def test_buy_with_stock_code_resolves_name_and_shows_no_warning():
    """코드를 직접 입력해도 해석된 종목명을 쓰므로 미해석 경고가 없다."""

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
        {"message": {"chat": {"id": 123}, "text": "/buy 005930 10"}}
    )

    assert handler.pending_orders["123"].stock_name == "삼성전자"
    assert handler.pending_orders["123"].stock_code == "005930"
    assert "삼성전자 매수 주문 확인" in notifier.messages[-1]
    assert UNRESOLVED_STOCK_WARNING not in notifier.messages[-1]


@pytest.mark.parametrize("code", ["999999", "ZZZZ99", "Q999999"])
@pytest.mark.asyncio
async def test_buy_with_unresolved_echo_is_rejected(code):
    """마스터에 없는 코드 형태 입력은 주문 준비 단계에서 끊는다 (#151).

    stock-master.js Step 3는 이런 입력을 market="UNKNOWN"으로 그대로 에코하므로
    코드 추출은 성공하고 숫자 6~7자 검사(999999)도 통과한다. 백엔드에 이 가드가
    없으면 실재 확인이 KIS 왕복(get_stock_quote)이나 /confirm 시점 브로커 거절로
    미뤄진다 — 리포트 저장 경로는 이미 같은 판정으로 막고 있는데 위험도가 더 높은
    주문 경로만 통과하던 비대칭을 없앤다.

    뮤테이션: _is_unresolved_echo 가드를 지우면 999999가 대기 주문으로 등록돼 red가
    된다(ZZZZ99·Q999999는 영숫자라 _ORDERABLE_STOCK_CODE_RE가 뒤에서 잡지만, 거절
    사유가 "ETN·펀드"로 바뀌므로 메시지 단정에서 red가 된다).
    """

    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return f"{code} ({code}, UNKNOWN)"
        raise AssertionError(
            f"미해석 에코는 시세·잔고 조회 전에 끊어야 한다: {tool_name}"
        )

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": f"/buy {code} 10"}}
    )

    assert handler.pending_orders == {}
    assert "종목마스터에 없는 종목입니다" in notifier.messages[-1]
    assert code in notifier.messages[-1]


def test_format_order_prompt_warns_when_name_equals_code():
    """UNRESOLVED_STOCK_WARNING 방어선이 실제로 동작하는지 포맷터 단위로 고정한다.

    주문 준비 단계(_is_unresolved_echo)가 먼저 끊으므로 현재 마스터로는 이 분기에
    도달하지 않는다(#151) — 그래도 마스터에 name == code인 항목이 생기는 경우를 대비한
    방어선이라면 실제로 경고를 붙이는지 확인할 수 있어야 한다.

    뮤테이션: telegram_commands._format_order_prompt의
    `if order.stock_name == order.stock_code:` 분기를 제거하면 이 단정이 red가 된다.
    """
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)
    order = PendingOrder(
        chat_id="123",
        stock_name="999999",
        stock_code="999999",
        side="BUY",
        quantity=10,
        price=0,
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
        order_type="MARKET",
    )

    prompt = handler._format_order_prompt(order, "현재가: 74,500원", "주문가능금액: 1,000,000원")

    assert UNRESOLVED_STOCK_WARNING in prompt


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


class FakePollerClock:
    """폴러의 _sleep/_now만 대체해 시간 기반 재시도 예산을 결정론적으로 진행시킨다 (#241).

    전역 asyncio.sleep을 패치하면 핸들러 내부의 실제 sleep(_handle_watch의 조회 간격)까지
    가로채므로 인스턴스 수준 간접층만 건드린다 (PR #242 리뷰).
    """

    def __init__(self, *, stop_after=None):
        self.now = 0.0
        self.sleeps = []
        self._stop_after = stop_after

    def install(self, monkeypatch, poller):
        monkeypatch.setattr(poller, "_sleep", self._sleep)
        monkeypatch.setattr(poller, "_now", lambda: self.now)

    async def _sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay
        if self._stop_after is not None and len(self.sleeps) >= self._stop_after:
            raise asyncio.CancelledError


def _poison_batch():
    return [
        {"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}},
        {"update_id": 42, "message": {"chat": {"id": 123}, "text": "/help"}},
    ]


@pytest.mark.asyncio
async def test_poller_skips_update_after_retry_window(monkeypatch):
    """복구 창을 넘긴 update는 건너뛰고, 통지에 실패한 명령을 함께 알린다 (#241)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    attempts = []

    class FailingHandler:
        async def handle_update(self, update):
            attempts.append(update["update_id"])
            raise RuntimeError("poison update")

    poller = TelegramCommandPoller(notifier=notifier, handler=FailingHandler())
    clock = FakePollerClock()
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    # 5 + 15 + 45 = 65초 > 60초 창. 예산이 실질 10초가 아니라는 회귀 가드다.
    # 상수를 그대로 비교하면 동어반복이라 값 변경을 못 잡는다. 리터럴로 고정한다 (PR #242 리뷰).
    assert clock.sleeps == [5.0, 15.0, 45.0]
    assert clock.now > telegram_commands.UPDATE_RETRY_WINDOW_SECONDS
    assert attempts == [41, 41, 41, 41]
    assert poller.offset == 42
    assert notifier.messages == [
        f"{telegram_commands.UPDATE_SKIPPED_NOTICE}\n실패한 요청: /alerts off"
    ]
    assert poller._failures == {}


@pytest.mark.asyncio
async def test_poller_does_not_spend_poison_budget_on_send_failures(monkeypatch):
    """전송 실패는 전역 장애라 update별 예산으로 세지 않는다 (#241, PR #242 리뷰)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class SendFailingHandler:
        async def handle_update(self, update):
            raise telegram_commands.TelegramSendError("telegram send failed")

    poller = TelegramCommandPoller(notifier=notifier, handler=SendFailingHandler())
    clock = FakePollerClock(stop_after=5)
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    # 일반 창(60초)은 이미 지났지만 전송 실패는 더 긴 창을 쓰므로 아직 폐기되지 않는다.
    assert clock.now > telegram_commands.UPDATE_RETRY_WINDOW_SECONDS
    assert poller.offset is None
    assert poller._failures[41].send_failure is True
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_poller_retries_when_elapsed_equals_window(monkeypatch):
    """창의 경계는 포함이다 — 경과 == 창이면 아직 폐기하지 않는다 (PR #242 리뷰)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class FailingHandler:
        async def handle_update(self, update):
            raise RuntimeError("poison update")

    poller = TelegramCommandPoller(notifier=notifier, handler=FailingHandler())
    clock = FakePollerClock(stop_after=2)
    clock.install(monkeypatch, poller)
    # 기본 백오프(5/15/45)로는 경과가 창에 정확히 걸리지 않아 경계를 지나친다.
    monkeypatch.setattr(
        poller, "_retry_delay", lambda: telegram_commands.UPDATE_RETRY_WINDOW_SECONDS
    )

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert clock.now == telegram_commands.UPDATE_RETRY_WINDOW_SECONDS * 2
    assert poller.offset is None
    assert poller._failures[41].attempts == 2
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_poller_keeps_send_failure_window_after_other_error(monkeypatch):
    """전송 실패가 섞였던 update는 다른 오류로 바뀌어도 긴 창을 유지한다 (PR #242 리뷰).

    first_at은 첫 실패에 고정인데 창만 마지막 예외 종류로 다시 고르면 기준이 어긋난다.
    429가 이어지다 마지막에 다른 오류가 한 번 나면 창이 60초로 줄어, 그 오류에 재시도가
    0회 주어진 채 즉시 폐기된다.
    """
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class FlippingHandler:
        def __init__(self):
            self.calls = 0

        async def handle_update(self, update):
            self.calls += 1
            if self.calls <= 3:
                raise telegram_commands.TelegramSendError("telegram send failed")
            raise RuntimeError("redis timeout")

    handler = FlippingHandler()
    poller = TelegramCommandPoller(notifier=notifier, handler=handler)
    clock = FakePollerClock(stop_after=4)
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/buy 005930 10"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    # 4번째 시도(t=65초)는 비-전송 오류다. 창이 60초로 줄었다면 여기서 폐기됐을 것이다.
    assert handler.calls == 4
    assert clock.now > telegram_commands.UPDATE_RETRY_WINDOW_SECONDS
    assert poller.offset is None
    assert poller._failures[41].send_failure is True
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_poller_extends_window_when_send_failure_appears_later(monkeypatch):
    """반대 방향도 같다 — 도중에 전송 실패가 섞이면 긴 창으로 넘어간다 (PR #242 리뷰)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class FlippingHandler:
        def __init__(self):
            self.calls = 0

        async def handle_update(self, update):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("redis timeout")
            raise telegram_commands.TelegramSendError("telegram send failed")

    handler = FlippingHandler()
    poller = TelegramCommandPoller(notifier=notifier, handler=handler)
    clock = FakePollerClock(stop_after=4)
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/buy 005930 10"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    # 첫 실패는 일반 오류였지만 이후 전송 실패가 섞였으므로 60초에서 폐기되지 않는다.
    assert handler.calls == 4
    assert clock.now > telegram_commands.UPDATE_RETRY_WINDOW_SECONDS
    assert poller.offset is None
    assert poller._failures[41].send_failure is True


@pytest.mark.asyncio
async def test_poller_retry_delay_follows_the_newest_failure(monkeypatch):
    """간격은 배치에 하나뿐이라 가장 급한 update에 맞춘다 (PR #242 리뷰)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class FailingHandler:
        async def handle_update(self, update):
            raise RuntimeError("poison update")

    poller = TelegramCommandPoller(notifier=notifier, handler=FailingHandler())
    clock = FakePollerClock(stop_after=1)
    clock.install(monkeypatch, poller)
    # 41은 이미 오래 재시도 중(45초 간격), 42는 이제 막 실패한다.
    poller._failures[41] = telegram_commands._UpdateFailure(
        first_at=0.0, attempts=8, send_failure=False
    )

    async def fake_get_updates():
        return [{"update_id": 42, "message": {"chat": {"id": 123}, "text": "/help"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    # 42가 41의 45초 간격을 물려받으면 자기 창(60초) 안에서 시도 횟수를 손해 본다.
    assert clock.sleeps == [5.0]


@pytest.mark.asyncio
async def test_poller_eventually_skips_persistent_send_failures(monkeypatch):
    """전송 실패 창에도 상한이 있어야 한다 (PR #242 리뷰).

    이 창이 사실상 무한대가 되면 영구적 전송 실패(특정 메시지의 파싱 400) 하나로 offset이
    그 시간만큼 얼어붙어 #241이 그대로 재발한다. 창을 늘리는 변경에 신호가 있어야 한다.
    """
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class SendFailingHandler:
        async def handle_update(self, update):
            raise telegram_commands.TelegramSendError("telegram send failed")

    poller = TelegramCommandPoller(notifier=notifier, handler=SendFailingHandler())
    # stop_after는 안전망이다. 창에 상한이 없으면 여기서 끊겨 아래 offset 단언이 실패한다.
    clock = FakePollerClock(stop_after=20)
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert poller.offset == 42
    assert clock.now > telegram_commands.SEND_FAILURE_RETRY_WINDOW_SECONDS
    assert clock.now < telegram_commands.SEND_FAILURE_RETRY_WINDOW_SECONDS * 2
    assert poller._failures == {}


@pytest.mark.asyncio
async def test_poller_handles_later_update_when_earlier_update_is_poison(monkeypatch):
    """배치 안의 poison 1건이 뒤의 정상 update를 막지 않는다 (#241)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    handled = []

    class PartialHandler:
        async def handle_update(self, update):
            if update["update_id"] == 41:
                raise RuntimeError("poison update")
            handled.append(update["update_id"])

    poller = TelegramCommandPoller(notifier=notifier, handler=PartialHandler())
    clock = FakePollerClock()
    clock.install(monkeypatch, poller)
    batch = _poison_batch()

    async def fake_get_updates():
        # 실제 Telegram처럼 offset 이후의 update만 다시 배달한다.
        offset = poller.offset
        if offset is not None and offset > 42:
            raise asyncio.CancelledError
        return [u for u in batch if offset is None or u["update_id"] >= offset]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    # 정상 update는 첫 배치에서 바로 처리되고, 재배달되어도 중복 실행되지 않는다.
    assert handled == [42]
    assert poller.offset == 43
    assert poller._handled_ahead == set()
    assert poller._failures == {}
    # 상수를 그대로 비교하면 동어반복이라 값 변경을 못 잡는다. 리터럴로 고정한다 (PR #242 리뷰).
    assert clock.sleeps == [5.0, 15.0, 45.0]
    assert notifier.messages == [
        f"{telegram_commands.UPDATE_SKIPPED_NOTICE}\n실패한 요청: /alerts off"
    ]


@pytest.mark.asyncio
async def test_poller_offset_stays_behind_poison_until_retries_exhausted(monkeypatch):
    """poison 뒤의 update를 처리해도 offset은 poison이 해소되기 전엔 넘어가지 않는다 (#241)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    handled = []

    class PartialHandler:
        async def handle_update(self, update):
            if update["update_id"] == 41:
                raise RuntimeError("poison update")
            handled.append(update["update_id"])

    poller = TelegramCommandPoller(notifier=notifier, handler=PartialHandler())
    batch = _poison_batch()

    async def fake_get_updates():
        offset = poller.offset
        return [u for u in batch if offset is None or u["update_id"] >= offset]

    class WatermarkClock(FakePollerClock):
        async def _sleep(self, delay):
            # 배치 처리가 끝날 때마다 물막이를 확인한다: 41이 아직 창 안이므로
            # offset은 41을 넘어선 안 된다(넘으면 41이 조용히 버려진다).
            assert poller.offset is None or poller.offset <= 41
            assert poller._failures[41].attempts == len(self.sleeps) + 1
            await super()._sleep(delay)

    clock = WatermarkClock(stop_after=2)
    clock.install(monkeypatch, poller)
    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    # 뒤의 update는 처리됐지만(중복 없이 1회), 41은 아직 창 안이라 버려지지 않았다.
    assert handled == [42]
    assert poller.offset is None
    assert poller._handled_ahead == {42}
    assert poller._failures[41].attempts == 2
    assert clock.now < telegram_commands.UPDATE_RETRY_WINDOW_SECONDS
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_poller_offset_stays_behind_poison_in_out_of_order_batch(monkeypatch):
    """배치가 update_id 역순으로 와도 offset은 poison을 넘지 않는다 (#241)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    handled = []

    class PartialHandler:
        async def handle_update(self, update):
            if update.get("update_id") == 41:
                raise RuntimeError("poison update")
            handled.append(update.get("update_id"))

    poller = TelegramCommandPoller(notifier=notifier, handler=PartialHandler())
    clock = FakePollerClock(stop_after=1)
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        # 정렬 전 순서가 정확성을 좌우하지 않아야 하므로 일부러 뒤집어 준다.
        # update_id 없는 update도 섞어 정렬 키의 나머지 절반을 함께 태운다.
        return [
            {"update_id": 42, "message": {"chat": {"id": 123}, "text": "/help"}},
            {"message": {"chat": {"id": 123}, "text": "/help"}},
            {"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}},
        ]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert poller.offset is None
    assert set(poller._failures) == {41}
    assert poller._failures[41].attempts == 1
    assert poller._handled_ahead == {42}
    assert handled == [None, 42]


@pytest.mark.asyncio
async def test_poller_batches_skip_notice_for_multiple_poison_updates(monkeypatch):
    """한 배치에 poison이 여럿이어도 통지는 한 건으로 합친다 (#241, PR #242 리뷰)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class FailingHandler:
        async def handle_update(self, update):
            raise RuntimeError("poison update")

    poller = TelegramCommandPoller(notifier=notifier, handler=FailingHandler())
    clock = FakePollerClock()
    clock.install(monkeypatch, poller)
    batch = _poison_batch()

    async def fake_get_updates():
        offset = poller.offset
        if offset is not None and offset > 42:
            raise asyncio.CancelledError
        return [u for u in batch if offset is None or u["update_id"] >= offset]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert poller.offset == 43
    assert notifier.messages == [
        f"{telegram_commands.UPDATE_SKIPPED_NOTICE}\n실패한 요청: /alerts off, /help"
    ]


@pytest.mark.asyncio
async def test_poller_logs_when_skip_notice_is_not_delivered(monkeypatch, caplog):
    """send_text는 예외 대신 False를 돌려주므로 반환값을 확인해야 한다 (PR #242 리뷰)."""
    notifier = FakeNotifier(send_text_result=False)
    notifier.enabled = True
    notifier.bot_token = "token"

    class FailingHandler:
        async def handle_update(self, update):
            raise RuntimeError("poison update")

    poller = TelegramCommandPoller(notifier=notifier, handler=FailingHandler())
    clock = FakePollerClock()
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert "skip notice not delivered" in caplog.text
    # 통지가 실패해도 폴러는 계속 전진한다.
    assert poller.offset == 42


@pytest.mark.asyncio
async def test_poller_skip_notice_skipped_for_other_chat(monkeypatch):
    """notifier는 자기 chat에만 보낼 수 있으므로 다른 chat이면 통지하지 않는다 (#241)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class FailingHandler:
        async def handle_update(self, update):
            raise RuntimeError("poison update")

    poller = TelegramCommandPoller(notifier=notifier, handler=FailingHandler())
    clock = FakePollerClock()
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 999}, "text": "/alerts off"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert poller.offset == 42
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_poller_clears_failure_state_after_success(monkeypatch):
    """일시 장애로 실패한 update가 성공하면 재시도 예산 기록이 남지 않는다 (#241)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"

    class FlakyHandler:
        def __init__(self):
            self.calls = 0

        async def handle_update(self, update):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("redis unavailable")

    handler = FlakyHandler()
    poller = TelegramCommandPoller(notifier=notifier, handler=handler)

    class AssertingClock(FakePollerClock):
        async def _sleep(self, delay):
            assert poller._failures[41].attempts == 1
            await super()._sleep(delay)

    clock = AssertingClock()
    clock.install(monkeypatch, poller)

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert handler.calls == 2
    assert poller.offset == 42
    assert poller._failures == {}
    assert clock.sleeps == [5.0]
    assert notifier.messages == []


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


# ── #260: 추론 과정 표시 (진행 메시지 + 응답 근거 각주) ──────────────────────


class EditingFakeNotifier(FakeNotifier):
    """editMessageText를 지원하는 notifier 대역.

    messages에는 sendMessage로 나간 것만, edits에는 편집으로 나간 것만 쌓인다 —
    "진행 메시지를 새로 보냈는지 / 기존 메시지를 고쳤는지"를 구분해 검증하기 위함이다.
    """

    def __init__(self, *, edit_result=True, message_id=4242, **kwargs):
        super().__init__(**kwargs)
        self.edit_result = edit_result
        self.message_id = message_id
        self.edits = []

    async def send_text_returning_id(self, text, *, reply_markup=None):
        self.messages.append(text)
        self.reply_markups.append(reply_markup)
        return self.message_id

    async def edit_message_text(self, message_id, text):
        self.edits.append((message_id, text))
        return self.edit_result


def _nat_runner(result):
    async def runner(provider, text, *, conversation_id=None):
        return result

    return runner


async def _ask(handler, text="삼성전자 뉴스 알려줘", chat_id=123):
    await handler.handle_update({"message": {"chat": {"id": chat_id}, "text": text}})


@pytest.mark.asyncio
async def test_progress_message_is_sent_and_edited_into_the_answer():
    notifier = EditingFakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner(NatAnswer("삼성전자 뉴스입니다.")),
    )

    await _ask(handler)

    assert notifier.actions == ["typing"]
    assert notifier.messages == [NAT_PROGRESS_MESSAGE]
    assert notifier.edits == [(4242, "삼성전자 뉴스입니다.")]


@pytest.mark.asyncio
async def test_answer_carries_reasoning_footnote_with_korean_labels():
    notifier = EditingFakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner(
            NatAnswer(
                "삼성전자 뉴스입니다.",
                routed_agent="news_agent",
                tools_used=("finus_account_balance", "finus_market_news"),
            )
        ),
    )

    await _ask(handler)

    _, final_text = notifier.edits[-1]
    assert final_text == (
        "삼성전자 뉴스입니다.\n\n"
        f"{REASONING_FOOTNOTE_SEPARATOR}\n"
        "🤖 뉴스 에이전트 · 📚 확인한 자료: KIS 시세·계좌 조회, 뉴스 검색"
    )


@pytest.mark.asyncio
async def test_footnote_is_omitted_when_response_has_no_metadata():
    """구버전 finus_nat 응답(메타데이터 없음)에서는 각주를 조용히 생략한다."""
    notifier = EditingFakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner("메타데이터 없는 답변"),  # 평범한 str
    )

    await _ask(handler)

    _, final_text = notifier.edits[-1]
    assert final_text == "메타데이터 없는 답변"
    assert REASONING_FOOTNOTE_SEPARATOR not in final_text


@pytest.mark.asyncio
async def test_footnote_shows_no_sources_when_ledger_was_empty():
    """도구 없이 나온 답변이라는 사실 자체가 사용자가 알아야 할 근거다."""
    notifier = EditingFakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner(NatAnswer("일반론입니다.", routed_agent="strategy_agent")),
    )

    await _ask(handler)

    _, final_text = notifier.edits[-1]
    assert final_text.endswith("🤖 전략 에이전트 · 📚 확인한 자료: 없음")


@pytest.mark.asyncio
async def test_unmapped_tool_name_falls_back_to_internal_name():
    """매핑에 없는 도구는 내부 이름 그대로 노출한다 — 조용히 감추지 않는다."""
    notifier = EditingFakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner(
            NatAnswer("답변", routed_agent="news_agent", tools_used=("finus_brand_new_tool",))
        ),
    )

    await _ask(handler)

    _, final_text = notifier.edits[-1]
    assert final_text.endswith("📚 확인한 자료: finus_brand_new_tool")


@pytest.mark.asyncio
async def test_edit_failure_falls_back_to_a_new_message():
    """편집이 거부되면(오래된 메시지 등) 답변을 새 메시지로 보낸다."""
    notifier = EditingFakeNotifier(edit_result=False)
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner(NatAnswer("최종 답변")),
    )

    await _ask(handler)

    assert notifier.edits == [(4242, "최종 답변")]
    assert notifier.messages == [NAT_PROGRESS_MESSAGE, "최종 답변"]


@pytest.mark.asyncio
async def test_missing_message_id_falls_back_to_a_new_message():
    """message_id를 확보하지 못하면 편집을 시도하지 않고 새 메시지로 보낸다."""
    notifier = EditingFakeNotifier(message_id=None)
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner(NatAnswer("최종 답변")),
    )

    await _ask(handler)

    assert notifier.edits == []
    assert notifier.messages == [NAT_PROGRESS_MESSAGE, "최종 답변"]


@pytest.mark.asyncio
async def test_notifier_without_edit_support_sends_two_plain_messages():
    """편집을 지원하지 않는 notifier에서도 진행 표시와 답변이 모두 전달된다."""
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner(NatAnswer("최종 답변")),
    )

    await _ask(handler)

    assert notifier.messages == [NAT_PROGRESS_MESSAGE, "최종 답변"]


@pytest.mark.asyncio
async def test_llm_failure_replaces_the_progress_message():
    """실패해도 진행 메시지를 교체한다 — 안 그러면 '분석 중'이 영원히 남는다."""
    notifier = EditingFakeNotifier()

    async def failing_runner(provider, text, *, conversation_id=None):
        raise HTTPException(status_code=502, detail="NAT 연결 실패")

    handler = TelegramCommandHandler(notifier=notifier, llm_runner=failing_runner)

    await _ask(handler)

    assert notifier.edits == [(4242, "응답 생성 실패: NAT 연결 실패")]


def test_progress_message_edit_budget_is_capped():
    """텔레그램 편집 빈도 제한 때문에 편집 횟수에 상한을 둔다."""
    progress = _ProgressMessage(message_id=1)

    assert progress.editable is True
    progress.edits_used = MAX_PROGRESS_EDITS
    assert progress.editable is False


def test_progress_message_without_id_is_never_editable():
    assert _ProgressMessage(message_id=None).editable is False


def test_reasoning_footnote_is_empty_without_any_evidence():
    assert _reasoning_footnote(None, ()) == ""
    assert _reasoning_footnote("", []) == ""


def test_reasoning_footnote_ignores_malformed_metadata():
    """타입이 어긋난 값은 각주를 만들지 않는다 — 신뢰 경계 밖의 값이다."""
    assert _reasoning_footnote(42, "not-a-list") == ""


@pytest.mark.asyncio
async def test_footnote_survives_truncation_of_a_long_answer():
    """긴 답변이 잘려도 각주는 남는다 — 각주 자리를 먼저 확보한 뒤 본문을 자른다."""
    notifier = EditingFakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        llm_runner=_nat_runner(
            NatAnswer(
                "가" * (TELEGRAM_MESSAGE_LIMIT * 2),
                routed_agent="news_agent",
                tools_used=("finus_market_news",),
            )
        ),
    )

    await _ask(handler)

    _, final_text = notifier.edits[-1]
    assert len(final_text) <= TELEGRAM_MESSAGE_LIMIT
    assert TELEGRAM_TRUNCATION_SUFFIX in final_text
    assert final_text.endswith("🤖 뉴스 에이전트 · 📚 확인한 자료: 뉴스 검색")
