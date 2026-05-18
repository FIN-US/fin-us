import pytest

from backend.config import TRADING_MCP_PARAMS
from backend.telegram_commands import (
    QUOTE_COMMAND_HELP,
    TELEGRAM_INTERACTIVE_HELP,
    TELEGRAM_MESSAGE_LIMIT,
    TELEGRAM_TRUNCATION_SUFFIX,
    TREND_COMMAND_HELP,
    TelegramCommandHandler,
    TelegramCommandPoller,
)


class FakeState:
    def __init__(self):
        self.mode = "urgent"

    async def get_telegram_alert_mode(self):
        return self.mode

    async def set_telegram_alert_mode(self, mode):
        self.mode = mode


class FakeNotifier:
    def __init__(self, chat_id="123"):
        self.chat_id = chat_id
        self.messages = []
        self.actions = []

    async def send_text(self, text):
        self.messages.append(text)
        return True

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)
        return True


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
async def test_help_command_replies_with_supported_commands():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/help"}})

    assert notifier.messages == [TELEGRAM_INTERACTIVE_HELP]
    assert "/balance - 예수금·총자산·보유 종목 조회" in notifier.messages[-1]
    assert "/quote <종목명> - 현재가 조회" in notifier.messages[-1]
    assert "/trend <종목명> - 외국인·기관·개인 수급 조회" in notifier.messages[-1]
    assert "NAT" not in notifier.messages[-1]


@pytest.mark.asyncio
async def test_unknown_slash_command_replies_with_help():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(notifier=notifier)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/unknown"}})

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
async def test_alerts_command_ignores_other_chats():
    state = FakeState()
    notifier = FakeNotifier(chat_id="123")
    handler = TelegramCommandHandler(notifier=notifier, state_factory=lambda: state)

    await handler.handle_update({"message": {"chat": {"id": 456}, "text": "/alerts all"}})

    assert state.mode == "urgent"
    assert notifier.messages == []


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
