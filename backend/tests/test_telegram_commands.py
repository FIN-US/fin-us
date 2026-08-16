import ast
import asyncio
import logging
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path
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
    UNRESOLVED_STOCK_WARNING,
    TelegramCommandHandler,
    TelegramCommandPoller,
)
from backend.redis_state import InMemoryTelegramPollerStore, TelegramPollerState
from backend.trading_orders import OrderExecutionResult, PendingOrder

KST = ZoneInfo("Asia/Seoul")


def _make_poller(notifier, handler, *, state_store=None):
    """폴러 테스트용 생성기 — 상태 저장소를 인메모리로 고정한다 (#248).

    state_store 기본값은 redis 클라이언트라, 주입하지 않으면 테스트가 실제 연결을 시도한다.
    재시작 시나리오는 같은 state_store를 두 폴러에 넘겨 재현한다.
    """
    return TelegramCommandPoller(
        notifier=notifier,
        handler=handler,
        state_store=state_store if state_store is not None else InMemoryTelegramPollerStore(),
    )


async def _noop(*args, **kwargs):
    return None


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
    def __init__(self, chat_id="123", send_text_result=True, bot_username="", fail_sends=0):
        self.chat_id = chat_id
        self.send_text_result = send_text_result
        # 처음 N번의 전송만 실패시킨다. 429처럼 곧 복구되는 일시 장애를 재현해, 실패 뒤
        # 무엇이 이어지는지(재시도인지 대체 메시지인지)를 구분할 수 있게 한다 (#247, #249).
        self.fail_sends = fail_sends
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
        if self.fail_sends > 0:
            self.fail_sends -= 1
            return False
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


def _order_mcp_runner_response(tool_name):
    if tool_name == "resolve_stock_code":
        return "삼성전자 (005930, KOSPI)"
    if tool_name == "get_stock_quote":
        return "현재가: 74,500원"
    if tool_name == "get_balance":
        return "주문가능금액: 1,000,000원"
    raise AssertionError(f"unexpected tool: {tool_name}")


def _order_mcp_runner():
    """/buy → /confirm 경로가 기대하는 세 MCP 응답을 돌려준다."""

    async def mcp_runner(server_params, tool_name, arguments):
        return _order_mcp_runner_response(tool_name)

    return mcp_runner


def _capture_settled_sleeps(monkeypatch, handler):
    """_send_text_settled의 인플레이스 재시도 간격을 기록하고 실제 대기는 없앤다."""
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(handler, "_sleep", fake_sleep)
    return sleeps


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


@pytest.mark.asyncio
async def test_polling_failure_log_has_no_bot_token(
    monkeypatch, caplog, failing_telegram_client
):
    """폴러의 getUpdates 실패 로그에 봇 토큰이 남지 않아야 한다 (PR #253 2차 리뷰, #257).

    401(토큰 폐기)·409(인스턴스 중복 또는 웹훅 병행)·429·5xx에서 폴링 루프가 5초마다
    재시도하며 매 회 기록하므로, 전송 경로보다 트리거가 잦다.

    예전에는 backend.telegram_commands가 리댁션 목록에 들어 있어야만 막혔다. 지금은
    _get_updates가 URL을 직접 만들지 않고 call_telegram_api에 맡겨서 막힌다 — 이 테스트는
    목록이 아니라 그 위임을 지킨다. _get_updates에 URL 조립이 되돌아오면 여기서 깨진다.
    """
    token = "8666951614:SECRET"
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = token
    poller = _make_poller(notifier, handler=TelegramCommandHandler(notifier=notifier))

    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        failing_telegram_client(409, {"ok": False, "description": "Conflict"}),
    )

    async def stop_after_first_failure(delay):
        raise pytest.fail.Exception("stop after first failed polling iteration")

    monkeypatch.setattr(poller, "_sleep", stop_after_first_failure)
    monkeypatch.setattr(poller, "_setup_bot_profile", _noop)
    monkeypatch.setattr(poller, "_restore_state", _noop)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(pytest.fail.Exception):
            await poller.run()

    assert "Telegram command polling failed" in caplog.text
    assert token not in caplog.text
    assert "api.telegram.org" not in caplog.text
    # 진단 정보는 남아야 한다 — 409는 인스턴스 중복이라 상태 코드 없이는 원인을 못 좁힌다.
    assert "409" in caplog.text


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

    poller = _make_poller(notifier, handler=handler)

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

    poller = _make_poller(notifier, handler=FailingHandler())

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

    poller = _make_poller(notifier, handler=NoopHandler())

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

    poller = _make_poller(notifier, handler=NoopHandler())

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
async def test_poller_does_not_rerun_llm_when_nat_response_send_fails(monkeypatch):
    """NAT 응답 전송이 실패해도 LLM을 다시 호출하지 않는다 (#247).

    LLM 호출은 확정된 부수효과다 — 과금되고 conversation_id에 대화 이력이 남는다.
    예전에는 update 전체가 재시도 대상이라 전송 실패 예산(10회)만큼 재호출됐다.
    지금은 전송만 그 자리에서 재시도하고 offset은 전진한다.
    """
    calls = []
    notifier = FakeNotifier(send_text_result=False)
    notifier.enabled = True
    notifier.bot_token = "token"

    async def fake_llm_runner(provider, text, *, conversation_id=None):
        calls.append((provider, text, conversation_id))
        return "NAT 응답"

    handler = TelegramCommandHandler(notifier=notifier, llm_runner=fake_llm_runner)
    settled_sleeps = _capture_settled_sleeps(monkeypatch, handler)
    poller = _make_poller(notifier, handler=handler)
    polls = 0

    async def fake_get_updates():
        nonlocal polls
        polls += 1
        if polls > 1:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "질문"}}]

    async def unexpected_backoff(delay):
        raise pytest.fail.Exception(f"폴러가 재시도 대기에 들어갔다 (delay={delay})")

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)
    monkeypatch.setattr(poller, "_sleep", unexpected_backoff)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert calls == [("nat", "질문", "telegram:123")]
    assert settled_sleeps == list(telegram_commands.SETTLED_SEND_RETRY_BACKOFF_SECONDS)
    assert notifier.messages == ["NAT 응답"] * (len(settled_sleeps) + 1)
    assert poller.offset == 42
    assert poller._failures == {}


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

    poller = _make_poller(notifier, handler=FailingHandler())
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

    poller = _make_poller(notifier, handler=SendFailingHandler())
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

    poller = _make_poller(notifier, handler=FailingHandler())
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
    poller = _make_poller(notifier, handler=handler)
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
    poller = _make_poller(notifier, handler=handler)
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

    poller = _make_poller(notifier, handler=FailingHandler())
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

    poller = _make_poller(notifier, handler=SendFailingHandler())
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

    poller = _make_poller(notifier, handler=PartialHandler())
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

    poller = _make_poller(notifier, handler=PartialHandler())
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

    poller = _make_poller(notifier, handler=PartialHandler())
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

    poller = _make_poller(notifier, handler=FailingHandler())
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

    poller = _make_poller(notifier, handler=FailingHandler())
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

    poller = _make_poller(notifier, handler=FailingHandler())
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
    poller = _make_poller(notifier, handler=handler)

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
# 폴러 상태 영속화 (#248)
# ---------------------------------------------------------------------------


def test_poller_defaults_to_redis_state_store(monkeypatch):
    """상태 저장소를 주입하지 않는 프로덕션 경로는 redis를 쓴다 (#248).

    기본값이 인메모리로 되돌아가면 이 이슈의 중복 창이 조용히 재발한다.
    """
    notifier = FakeNotifier()
    client = object()
    monkeypatch.setattr(telegram_commands, "create_redis_client", lambda: client)

    poller = TelegramCommandPoller(notifier=notifier, handler=TelegramCommandHandler(notifier=notifier))

    assert isinstance(poller.state_store, telegram_commands.RedisTelegramPollerStore)
    assert poller.state_store.redis is client


@pytest.mark.asyncio
async def test_poller_restart_does_not_reexecute_updates_handled_ahead_of_poison(monkeypatch):
    """poison 뒤에서 먼저 실행한 update는 재시작 후에도 다시 실행되지 않는다 (#248).

    이 이슈의 본체다. 영속화 이전에는 blocked 구간(일반 실패 65초, 전송 실패 335초)에
    프로세스가 죽으면 offset과 _handled_ahead가 함께 사라져 42, 43이 재실행됐다.
    """
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    handled = []
    store = InMemoryTelegramPollerStore()
    batch = [
        {"update_id": 41, "message": {"chat": {"id": 123}, "text": "/alerts off"}},
        {"update_id": 42, "message": {"chat": {"id": 123}, "text": "/help"}},
        {"update_id": 43, "message": {"chat": {"id": 123}, "text": "/trade"}},
    ]

    class PartialHandler:
        async def handle_update(self, update):
            if update["update_id"] == 41:
                raise RuntimeError("poison update")
            handled.append(update["update_id"])

    def run_process():
        """새 폴러 = 새 프로세스. 인메모리 상태는 버리고 store만 넘겨준다."""
        poller = _make_poller(notifier, handler=PartialHandler(), state_store=store)
        clock = FakePollerClock(stop_after=1)
        clock.install(monkeypatch, poller)

        async def fake_get_updates():
            # 실제 Telegram처럼 offset 이후의 update만 배달한다.
            offset = poller.offset
            return [u for u in batch if offset is None or u["update_id"] >= offset]

        monkeypatch.setattr(poller, "_get_updates", fake_get_updates)
        return poller

    first = run_process()
    with pytest.raises(asyncio.CancelledError):
        await first.run()

    # 41은 아직 창 안이라 offset이 멈춰 있고, 42·43은 이미 실행됐다.
    assert handled == [42, 43]
    assert first.offset is None
    assert first._handled_ahead == {42, 43}

    # 프로세스 교체. 41은 여전히 미확정이라 텔레그램이 42·43까지 다시 배달한다.
    second = run_process()
    with pytest.raises(asyncio.CancelledError):
        await second.run()

    assert handled == [42, 43]  # 재실행 없음
    assert second.offset is None
    assert second._handled_ahead == {42, 43}


@pytest.mark.asyncio
async def test_poller_restores_offset_across_restart(monkeypatch):
    """확정된 offset은 재시작 후에도 유지되어 지나간 update를 다시 받지 않는다 (#248)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    store = InMemoryTelegramPollerStore(TelegramPollerState(offset=42, handled_ahead=frozenset()))
    requested_offsets = []

    class NoopHandler:
        async def handle_update(self, update):
            return None

    poller = _make_poller(notifier, handler=NoopHandler(), state_store=store)

    async def fake_get_updates():
        requested_offsets.append(poller.offset)
        raise asyncio.CancelledError

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert requested_offsets == [42]


@pytest.mark.asyncio
async def test_poller_persists_state_after_each_update(monkeypatch):
    """배치 끝이 아니라 update마다 저장한다 — 배치 중간에 죽어도 기록이 남아야 한다 (#248)."""
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    saved = []

    class NoopHandler:
        async def handle_update(self, update):
            return None

    class RecordingStore(InMemoryTelegramPollerStore):
        async def save(self, state):
            saved.append(state.offset)
            await super().save(state)

    poller = _make_poller(notifier, handler=NoopHandler(), state_store=RecordingStore())

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [
            {"update_id": 41, "message": {"chat": {"id": 123}, "text": "/help"}},
            {"update_id": 42, "message": {"chat": {"id": 123}, "text": "/help"}},
        ]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with pytest.raises(asyncio.CancelledError):
        await poller.run()

    assert saved == [42, 43]


@pytest.mark.asyncio
async def test_poller_keeps_polling_when_state_store_fails(monkeypatch, caplog):
    """redis 장애로 load·save가 실패해도 폴링은 인메모리 상태로 계속된다 (#248).

    fail-closed로 두면 redis 장애가 곧 텔레그램 명령 전면 중단이 된다.
    """
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    handled = []

    class NoopHandler:
        async def handle_update(self, update):
            handled.append(update["update_id"])

    class BrokenStore:
        async def load(self):
            raise RuntimeError("redis unavailable")

        async def save(self, state):
            raise RuntimeError("redis unavailable")

    poller = _make_poller(notifier, handler=NoopHandler(), state_store=BrokenStore())

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/help"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with caplog.at_level("ERROR"):
        with pytest.raises(asyncio.CancelledError):
            await poller.run()

    assert handled == [41]
    assert poller.offset == 42
    assert "상태 복원 실패" in caplog.text
    assert "상태 저장 실패" in caplog.text


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


# ──────────────────────────────────────────────────────────────────────────
# 부수효과 확정 뒤의 전송 실패 (#247) / 변환 경로의 전송 실패 삼킴 (#249)
# ──────────────────────────────────────────────────────────────────────────


def test_settled_send_retry_is_bounded():
    """settled 재시도가 폴러 루프를 붙잡는 시간에 상한이 있어야 한다 (#247).

    이 시간만큼 (1) 같은 배치에서 재시도를 기다리는 다른 update가 시도 없이 예산을 잃고,
    (2) /buy 확인 프롬프트는 대기 주문의 60초 만료 창을 나눠 쓴다.

    상한은 백오프 합이 아니라 SETTLED_SEND_TIMEOUT_SECONDS다. 시도마다 HTTP 왕복이
    붙으므로(httpx 타임아웃 10초) 백오프 합만 재면 실제 최악을 40초 놓친다 (PR #253 리뷰).
    """
    bound = telegram_commands.SETTLED_SEND_TIMEOUT_SECONDS
    expiry = telegram_commands.ORDER_EXPIRES_AFTER.total_seconds()

    # 만료 창의 절반은 사용자가 확인 버튼을 누를 시간으로 남는다.
    assert bound * 2 <= expiry

    # 백오프 합이 상한을 넘으면 429 흡수가 상한에 잘려 재시도의 목적을 잃는다.
    assert sum(telegram_commands.SETTLED_SEND_RETRY_BACKOFF_SECONDS) < bound

    # 상한이 없을 때의 최악(무응답 4시도 × httpx 10초 + 백오프)은 53초다. 만료 창을 넘지는
    # 않지만 사용자에게 7초만 남기므로, 위의 "절반은 남긴다" 보장이 무너진다.
    # 벽시계 상한을 두는 이유가 이것이다.
    attempts = len(telegram_commands.SETTLED_SEND_RETRY_BACKOFF_SECONDS) + 1
    unbounded_worst = sum(telegram_commands.SETTLED_SEND_RETRY_BACKOFF_SECONDS) + 10.0 * attempts
    assert unbounded_worst > expiry / 2
    assert expiry - unbounded_worst < 10.0


@pytest.mark.asyncio
async def test_settled_send_gives_up_at_the_wall_clock_bound(monkeypatch, caplog):
    """Telegram이 무응답이면 시도 횟수가 아니라 벽시계 상한에서 끊는다 (PR #253 리뷰).

    시도마다 httpx 타임아웃 10초가 그대로 붙으므로, 횟수만으로는 상한이 서지 않는다.
    """
    hung = 0

    class HangingNotifier(FakeNotifier):
        async def send_text(self, text, *, reply_markup=None):
            nonlocal hung
            hung += 1
            await asyncio.sleep(30)  # 응답 없는 Telegram
            raise AssertionError("상한 안에 끊겼어야 한다")

    notifier = HangingNotifier()
    handler = TelegramCommandHandler(notifier=notifier)
    monkeypatch.setattr(telegram_commands, "SETTLED_SEND_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level(logging.ERROR):
        # 예외를 던지지 않는다는 것이 요지다 — settled 전송은 update를 재시도시키지 않는다.
        await handler._send_text_settled("확정된 결과")

    assert hung == 1
    assert "벽시계 상한" in caplog.text


@pytest.mark.asyncio
async def test_order_prompt_states_absolute_expiry_time(monkeypatch):
    """만료를 "60초 후"가 아니라 절대 시각으로 알린다 (#247 자가리뷰).

    created_at은 MCP 조회 전에 찍히고, 전송이 429로 밀리면 settled 재시도가 최대 13초를
    더 쓴다. "60초 후"는 메시지가 언제 도착하든 60초를 약속하므로 사실과 어긋난다.
    """
    notifier = FakeNotifier(fail_sends=3)
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=_order_mcp_runner(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST),
    )
    _capture_settled_sleeps(monkeypatch, handler)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10"}}
    )

    # created_at 10:00:00 + 60초. 네 번째 시도에서야 도착해도 같은 시각을 가리킨다.
    assert "이 주문은 10:01:00에 만료됩니다." in notifier.messages[-1]
    assert "60초 후 만료" not in notifier.messages[-1]


@pytest.mark.asyncio
async def test_buy_prompt_send_failure_does_not_ask_poller_to_retry(monkeypatch):
    """확인 프롬프트 전송이 실패해도 update를 재시도하지 않는다 (#247).

    대기 주문은 이미 저장돼 있어 재실행하면 has_pending에 걸려 "이미 대기 중인 주문이
    있습니다"로 끝난다. 사용자는 확인 버튼을 영영 받지 못한 채 주문만 60초 뒤 만료되고,
    로그에는 "재시도로 복구됨"으로 남는다.
    """
    notifier = FakeNotifier(send_text_result=False)
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=_order_mcp_runner(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    sleeps = _capture_settled_sleeps(monkeypatch, handler)

    # TelegramSendError를 던지지 않는다는 것이 이 테스트의 요지다.
    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10"}}
    )

    assert sleeps == list(telegram_commands.SETTLED_SEND_RETRY_BACKOFF_SECONDS)
    assert len(notifier.messages) == len(sleeps) + 1
    assert all("삼성전자 매수 주문 확인" in message for message in notifier.messages)
    # 프롬프트가 끝내 안 나갔으므로 대기 주문을 남기지 않는다. 남기면 사용자는 존재를
    # 모르는 주문 때문에 다음 /buy가 "이미 대기 중"으로 막힌다 (PR #253 2차 리뷰).
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_buy_prompt_send_recovers_within_settled_retry(monkeypatch):
    """일시적 전송 실패는 그 자리 재시도로 흡수한다 — 사용자는 프롬프트를 받는다 (#247)."""
    notifier = FakeNotifier(fail_sends=2)
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=_order_mcp_runner(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    sleeps = _capture_settled_sleeps(monkeypatch, handler)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10"}}
    )

    assert sleeps == list(telegram_commands.SETTLED_SEND_RETRY_BACKOFF_SECONDS[:2])
    assert len(notifier.messages) == 3
    assert notifier.reply_markups[-1]["inline_keyboard"][0][0]["text"] == "✅ 확정"


@pytest.mark.asyncio
async def test_confirm_result_send_failure_does_not_ask_poller_to_retry(monkeypatch):
    """체결 결과 전송이 실패해도 update를 재시도하지 않는다 (#247).

    claim(GETDEL)으로 주문이 이미 소비돼 재실행은 "확정할 대기 주문이 없습니다"로 끝난다.
    주문은 체결됐는데 사용자는 미체결로 인식하게 된다.
    """
    gateway = FakeOrderGateway()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=_order_mcp_runner(),
        order_gateway=gateway,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    sleeps = _capture_settled_sleeps(monkeypatch, handler)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    notifier.messages.clear()
    notifier.send_text_result = False

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert len(gateway.orders) == 1
    assert sleeps == list(telegram_commands.SETTLED_SEND_RETRY_BACKOFF_SECONDS)
    assert notifier.messages == ["주문 완료: 주문 접수"] * (len(sleeps) + 1)
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_confirm_unclear_result_send_failure_does_not_ask_poller_to_retry(monkeypatch):
    """403이 아닌 오류(타임아웃·5xx)의 통지도 확정 뒤 전송이다 (PR #253 리뷰).

    claim으로 주문이 이미 소비됐고 복원도 하지 않으므로, 재실행은 "확정할 대기 주문이
    없습니다"로 끝나 "상태 확인 필요"라는 경고 자체가 사라진다.
    """
    gateway = FakeOrderGateway(error=RuntimeError("broker timeout"))
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=_order_mcp_runner(),
        order_gateway=gateway,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    sleeps = _capture_settled_sleeps(monkeypatch, handler)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    notifier.messages.clear()
    notifier.send_text_result = False

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert len(gateway.orders) == 1
    assert sleeps == list(telegram_commands.SETTLED_SEND_RETRY_BACKOFF_SECONDS)
    assert len(notifier.messages) == len(sleeps) + 1
    assert all(
        message.startswith("주문 실패 또는 상태 확인 필요: broker timeout")
        for message in notifier.messages
    )
    # 403과 달리 복원하지 않는다 — 중복 주문 방지가 우선이다.
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_confirm_403_keeps_update_retryable_when_order_is_restored():
    """403은 대기 주문이 복원되므로 재시도해도 같은 결과다 — 전송 실패를 폴러에 알린다 (#247)."""
    gateway = FakeOrderGateway(error=HTTPException(status_code=403, detail="실계좌 가드"))
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=_order_mcp_runner(),
        order_gateway=gateway,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    notifier.messages.clear()
    notifier.send_text_result = False

    with pytest.raises(telegram_commands.TelegramSendError):
        await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    # 인플레이스 재시도 없이 한 번만 시도하고 폴러에 넘긴다.
    assert notifier.messages == ["주문 실패: 실계좌 가드"]
    assert handler.pending_orders["123"].stock_code == "005930"


@pytest.mark.asyncio
async def test_cancel_confirmation_send_failure_does_not_ask_poller_to_retry(monkeypatch):
    """취소 완료 전송이 실패해도 update를 재시도하지 않는다 (#247).

    대기 주문이 이미 삭제돼 재실행은 "취소할 대기 주문이 없습니다"로 끝난다.
    """
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=_order_mcp_runner(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    sleeps = _capture_settled_sleeps(monkeypatch, handler)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    notifier.messages.clear()
    notifier.send_text_result = False

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/cancel"}})

    assert notifier.messages == ["대기 주문을 취소했습니다."] * (len(sleeps) + 1)
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_earnings_send_failure_does_not_rerun_llm(monkeypatch):
    """실적 리포트 전송이 실패해도 DART·뉴스 조회와 LLM 호출을 반복하지 않는다 (#247)."""
    llm_calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        return f"{tool_name} 결과"

    async def llm_runner(provider, prompt, *, conversation_id=None):
        llm_calls.append(conversation_id)
        return "호재\n실적이 좋다"

    notifier = FakeNotifier(send_text_result=False)
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        llm_runner=llm_runner,
    )
    sleeps = _capture_settled_sleeps(monkeypatch, handler)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/earnings 삼성전자"}}
    )

    assert len(llm_calls) == 1
    assert len(notifier.messages) == len(sleeps) + 1


@pytest.mark.asyncio
async def test_order_prepare_does_not_convert_send_failure_into_user_message():
    """전송 실패는 사용자 메시지로 변환하지 않고 폴러에 그대로 올린다 (#249).

    변환하면 사용자는 원래 메시지 대신 "주문 준비 실패: telegram send failed"를 한 번 더
    받는다. 이 지점은 아직 부수효과가 없어 재시도가 안전하다.

    판별력은 messages 비교에 있다 — FakeNotifier가 실패한 전송도 messages에 먼저 남기므로
    변환이 일어나면 2건이 된다. pytest.raises만으로는 부족한데, except를 통째로 지우면
    변환한 전송도 결국 TelegramSendError라 어느 쪽이든 던지기 때문이다 (PR #253 2차 리뷰).
    """

    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "해당 종목을 찾을 수 없습니다"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier(fail_sends=1)
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    with pytest.raises(telegram_commands.TelegramSendError):
        await handler.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10"}}
        )

    assert notifier.messages == ["주문 준비 실패: 종목코드를 확인할 수 없습니다."]
    assert "123" not in handler.pending_orders


@pytest.mark.asyncio
async def test_settled_send_gives_up_when_flood_wait_exceeds_the_budget(monkeypatch, caplog):
    """flood-wait이 남은 예산보다 길면 재시도하지 않는다 (PR #253 2차 리뷰).

    429의 retry_after는 흔히 30초 이상인데 백오프는 (1, 3, 9)이다. 그대로 두면 4시도가
    전부 ban 구간에 소진되고, ban 중 재요청은 대기 시간을 늘리는 방향으로 작용한다.
    """
    notifier = FakeNotifier(send_text_result=False)
    notifier.last_retry_after_seconds = 45  # 남은 예산(20초)보다 길다
    handler = TelegramCommandHandler(notifier=notifier)
    sleeps = _capture_settled_sleeps(monkeypatch, handler)

    with caplog.at_level(logging.ERROR):
        assert await handler._send_text_settled("확정된 결과") is False

    assert sleeps == []               # 한 번도 자지 않는다
    assert notifier.messages == ["확정된 결과"]   # 시도도 한 번뿐
    assert "재시도 포기" in caplog.text


@pytest.mark.asyncio
async def test_settled_send_waits_at_least_the_flood_wait(monkeypatch):
    """예산 안에 풀리는 flood-wait이면 백오프 대신 그 값을 기다린다 (PR #253 2차 리뷰)."""
    notifier = FakeNotifier(fail_sends=1)
    notifier.last_retry_after_seconds = 6  # 백오프 첫 값(1초)보다 길고 예산 안이다
    handler = TelegramCommandHandler(notifier=notifier)
    sleeps = _capture_settled_sleeps(monkeypatch, handler)

    assert await handler._send_text_settled("확정된 결과") is True
    assert sleeps == [6.0]


@pytest.mark.asyncio
async def test_market_callback_token_survives_a_failed_send(monkeypatch):
    """조회 결과 전송이 실패하면 콜백 토큰을 소비하지 않는다 (PR #253 2차 리뷰).

    소비해 버리면 폴러 재시도가 MARKET_STALE_CALLBACK_TEXT로 끝나 방금 누른 버튼에
    "이전 조회 버튼입니다"가 뜬다.
    """

    async def mcp_runner(server_params, tool_name, arguments):
        return "현재가: 74,500원"

    notifier = FakeNotifier(send_text_result=False)
    handler = TelegramCommandHandler(notifier=notifier, mcp_runner=mcp_runner)
    handler.market_callbacks["tok"] = ("123", "삼성전자")

    with pytest.raises(telegram_commands.TelegramSendError):
        await handler.handle_update(
            {
                "callback_query": {
                    "id": "cb-1",
                    "data": f"{telegram_commands.MARKET_QUOTE_CALLBACK}:tok",
                    "message": {"chat": {"id": 123}},
                }
            }
        )

    # 토큰이 살아 있어야 재시도가 같은 조회를 다시 수행한다.
    assert handler.market_callbacks["tok"] == ("123", "삼성전자")

    notifier.send_text_result = True
    await handler.handle_update(
        {
            "callback_query": {
                "id": "cb-1",
                "data": f"{telegram_commands.MARKET_QUOTE_CALLBACK}:tok",
                "message": {"chat": {"id": 123}},
            }
        }
    )
    assert "74,500원" in notifier.messages[-1]
    assert "tok" not in handler.market_callbacks  # 성공 후에는 소비된다


@pytest.mark.asyncio
async def test_pending_order_is_stamped_at_store_time_not_command_time():
    """created_at은 명령 수신 시각이 아니라 저장 직전 시각이다 (PR #253 2차 리뷰).

    MCP 조회가 run_mcp_tool의 wait_for(30초)를 두 구간 쓰므로 최대 60초가 걸린다.
    명령 수신 시각을 쓰면 프롬프트가 도착하기도 전에 만료 시각이 지나 있고, 절대 시각
    표기가 "과거 시각에 만료됩니다"가 된다.
    """
    clock = [datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST)]

    async def slow_mcp_runner(server_params, tool_name, arguments):
        clock[0] = clock[0] + timedelta(seconds=25)  # 조회가 느리다
        return _order_mcp_runner_response(tool_name)

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=slow_mcp_runner,
        now_factory=lambda: clock[0],
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10"}}
    )

    order = handler.pending_orders["123"]
    # 명령 수신 시각(10:00:00)이었다면 만료가 10:01:00 — 이미 지난 뒤다.
    assert order.created_at > datetime(2026, 5, 20, 10, 0, 0, tzinfo=KST)
    expires_at = order.created_at + telegram_commands.ORDER_EXPIRES_AFTER
    assert expires_at > clock[0]  # 저장 순간엔 항상 미래여야 한다
    assert f"이 주문은 {expires_at:%H:%M:%S}에 만료됩니다." in notifier.messages[-1]


def test_get_updates_bounds_the_batch_size():
    """배치 크기를 명시하지 않으면 Telegram 기본값이 100이라 루프 점유가 무계가 된다.

    상수 invariant만 본다 — limit이 payload에 실제로 실리는지는
    test_get_updates_sends_the_batch_limit이 검사한다 (PR #253 3차 리뷰).
    """
    assert telegram_commands.GET_UPDATES_LIMIT <= 10
    worst_case = (
        telegram_commands.GET_UPDATES_LIMIT * telegram_commands.SETTLED_SEND_TIMEOUT_SECONDS
    )
    assert worst_case <= 200.0


@pytest.mark.asyncio
async def test_get_updates_sends_the_batch_limit(monkeypatch):
    """limit을 payload에 실제로 실어야 배치가 유계가 된다 (PR #253 3차 리뷰).

    앞의 상수 검사는 _get_updates를 부르지 않아, payload에서 limit을 빼도 초록이었다 —
    Telegram 기본값 100으로 되돌아가는 회귀에 아무 신호가 없었다.
    """
    captured = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            captured["payload"] = json
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"ok": True, "result": []},
            )

    # HTTP 호출은 telegram_notifier.call_telegram_api로 옮겨갔다 (#257). 패치 지점도
    # 따라 옮긴다 — 여기서 검사하는 것은 폴러가 싣는 payload이지 호출 위치가 아니다.
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient", lambda **kwargs: FakeClient()
    )
    notifier = FakeNotifier()
    notifier.bot_token = "token"
    poller = _make_poller(notifier, handler=object())

    assert await poller._get_updates() == []
    assert captured["payload"]["limit"] == telegram_commands.GET_UPDATES_LIMIT


# TelegramSendError(RuntimeError)를 삼키는 except 이름들. 이 중 하나라도 먼저 걸리면
# 그 핸들러가 실효 핸들러이고, 뒤에 오는 except TelegramSendError는 도달하지 않는다.
_SEND_ERROR_CATCHING_NAMES = frozenset(
    {"TelegramSendError", "RuntimeError", "Exception", "BaseException"}
)


def _try_blocks_missing_send_failure_reraise(source: str | None = None) -> list[int]:
    """본문에 _send_text_or_raise가 있는데 TelegramSendError를 재던지지 않는 try의 행 번호.

    source를 주면 그 소스를, 없으면 telegram_commands.py를 본다. 파라미터화한 이유는
    test_the_static_send_failure_guard_actually_detects_a_violation이 판정 로직을 다시
    구현하는 대신 이 함수를 직접 부르게 하기 위해서다 — 인라인 재구현은 가드 본체를
    무력화해도 초록으로 남았다 (PR #253 3차 리뷰).

    한계 (이 목록에 없는 위반은 잡히지 않는다):
    - 직접 호출만 본다. 전송을 감싼 헬퍼를 try 안에서 부르면 잡지 못한다.
    - 강제하는 것은 "재던지기"이지 "변환 금지"가 아니다. 재던지기 전에 중복 메시지를
      보내는 핸들러는 통과한다.
    """

    def calls_retryable_send(statements) -> bool:
        for statement in statements:
            for node in ast.walk(statement):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_send_text_or_raise"
                ):
                    return True
        return False

    def caught_names(handler: ast.ExceptHandler) -> set[str]:
        caught = handler.type
        if caught is None:  # bare except: 전부 잡는다
            return set(_SEND_ERROR_CATCHING_NAMES)
        names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
        return {name.id for name in names if isinstance(name, ast.Name)}

    def effective_handler_reraises(node: ast.Try) -> bool:
        """TelegramSendError를 실제로 잡는 첫 핸들러가 bare raise로 끝나는가.

        any(...)로 "어딘가에 except TelegramSendError가 있다"만 보면
        `except Exception` → `except TelegramSendError: raise` 순서를 통과시킨다.
        파이썬은 이 순서를 문법 오류로 보지 않으므로 리팩터링 사고로 나올 수 있다.
        첫 매칭 핸들러만 보면 그 사각이 닫힌다 (PR #253 3차 리뷰).

        아무 핸들러도 잡지 않으면(try/finally 등) 예외는 그대로 전파되므로 안전하다.
        """
        for handler in node.handlers:
            if not (caught_names(handler) & _SEND_ERROR_CATCHING_NAMES):
                continue
            return any(
                isinstance(inner, ast.Raise) and inner.exc is None
                for inner in handler.body
            )
        return True

    tree = ast.parse(
        source
        if source is not None
        else Path(telegram_commands.__file__).read_text(encoding="utf-8")
    )
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and calls_retryable_send(node.body)
        and not effective_handler_reraises(node)
    ]


def test_every_try_containing_a_retryable_send_reraises_it():
    """_send_text_or_raise를 본문에 둔 try는 TelegramSendError를 재던져야 한다 (#249).

    변환하면 원래 메시지 대신 "…: telegram send failed"가 사용자에게 한 번 더 간다.

    런타임 backstop(ContextVar) 대신 이 정적 가드를 쓴다. 앞선 설계는 handle_update가
    종료 시점에 전송 실패 표시를 확인해 다시 던지는 방식이었는데, 실제로 그것이 추가로
    보호하는 프로덕션 경로가 없었다 — 전송을 본문에 둔 try는 파일 전체에 하나뿐이고 그
    하나는 이미 명시적으로 재던진다. "변환 경로가 9곳 이상"이라던 원래 근거는 except
    핸들러 *안*의 전송을 센 것이라 틀렸다(거기서 난 예외는 삼켜지지 않고 그대로 전파된다).

    이 가드가 더 낫다: fail-closed이고, 런타임 비용이 0이며, 태스크 경계 예외조항이 없고,
    "마지막 settled 전송 이후에 실패했다"로 의미가 미끄러지는 순서 의존성도 없다
    (PR #253 2차 리뷰).
    """
    assert _try_blocks_missing_send_failure_reraise() == []


def test_the_static_send_failure_guard_actually_detects_a_violation():
    """위 가드가 tautology가 아님을 고정한다 — 위반을 실제로 잡는지 확인한다.

    가드 본체를 부르지 않고 판정 로직을 인라인으로 다시 구현하면, 검증 대상이 가드가
    아니라 ast 모듈이 된다. 실제로 그 형태였을 때 가드를 `return []`로 무력화해도
    스위트가 초록이었다 — backstop을 걷어낸 지금 이 가드가 #249의 유일한 구조적
    보장이라 무커버로 둘 수 없다 (PR #253 3차 리뷰).
    """
    violating = textwrap.dedent(
        """
        async def handler(self):
            try:
                await self._send_text_or_raise("원래 메시지")
            except Exception as exc:
                await self._send_text_or_raise(f"처리 실패: {exc}")
        """
    )
    assert _try_blocks_missing_send_failure_reraise(violating) == [3]


def test_the_static_send_failure_guard_sees_through_handler_order():
    """앞선 except Exception이 먼저 삼키면 뒤의 재던지기는 도달하지 않는다 (PR #253 3차 리뷰).

    "어딘가에 except TelegramSendError가 있는가"만 보면 이 형태가 통과한다. 파이썬은
    이 순서를 문법 오류로 보지 않으므로 리팩터링 사고로 충분히 나온다.
    """
    shadowed = textwrap.dedent(
        """
        async def handler(self):
            try:
                await self._send_text_or_raise("원래 메시지")
            except Exception as exc:
                await self._send_text_or_raise(f"처리 실패: {exc}")
            except TelegramSendError:
                raise
        """
    )
    assert _try_blocks_missing_send_failure_reraise(shadowed) == [3]

    # 순서를 바로잡으면 통과한다 — 가드가 순서만 보고 무조건 막는 것은 아니다.
    correct = textwrap.dedent(
        """
        async def handler(self):
            try:
                await self._send_text_or_raise("원래 메시지")
            except TelegramSendError:
                raise
            except Exception as exc:
                await self._send_text_or_raise(f"처리 실패: {exc}")
        """
    )
    assert _try_blocks_missing_send_failure_reraise(correct) == []


def test_the_static_send_failure_guard_allows_try_finally():
    """핸들러가 없으면 예외는 그대로 전파된다 — 무의미한 재던지기를 요구하지 않는다.

    앞선 구현은 "재던지는 핸들러가 하나도 없다"만 보고 try/finally를 위반으로 셌다
    (PR #253 3차 리뷰).
    """
    with_finally = textwrap.dedent(
        """
        async def handler(self):
            try:
                await self._send_text_or_raise("원래 메시지")
            finally:
                self._cleanup()
        """
    )
    assert _try_blocks_missing_send_failure_reraise(with_finally) == []


@pytest.mark.asyncio
async def test_poller_keeps_polling_when_state_store_hangs(monkeypatch, caplog):
    """저장소가 예외 대신 hang하면 fail-open이 fail-hang으로 무너진다 (PR #251 리뷰).

    create_redis_client()가 socket_timeout을 주지 않아 redis 호스트가 RST 없이 패킷을
    drop하면 set()이 무한 블록되고, 그 await는 run()의 배치 루프 안이라 폴러 태스크가
    통째로 멈춘다. except Exception은 예외가 나야 도는 방어라 여기서는 발화하지 않는다.
    BrokenStore(즉시 raise)는 이 경로를 재현하지 못한다.
    """
    notifier = FakeNotifier()
    notifier.enabled = True
    notifier.bot_token = "token"
    handled = []

    class NoopHandler:
        async def handle_update(self, update):
            handled.append(update["update_id"])

    class HangingStore:
        async def load(self):
            await asyncio.Event().wait()  # 영원히 깨어나지 않는다

        async def save(self, state):
            await asyncio.Event().wait()

    monkeypatch.setattr(telegram_commands, "STATE_STORE_TIMEOUT_SECONDS", 0.01)
    poller = _make_poller(notifier, handler=NoopHandler(), state_store=HangingStore())

    async def fake_get_updates():
        if poller.offset is not None:
            raise asyncio.CancelledError
        return [{"update_id": 41, "message": {"chat": {"id": 123}, "text": "/help"}}]

    monkeypatch.setattr(poller, "_get_updates", fake_get_updates)

    with caplog.at_level("ERROR"):
        with pytest.raises(asyncio.CancelledError):
            await poller.run()

    # hang에 갇히지 않고 폴링이 진행됐다.
    assert handled == [41]
    assert poller.offset == 42
    assert "상태 복원 실패" in caplog.text
    assert "상태 저장 실패" in caplog.text
