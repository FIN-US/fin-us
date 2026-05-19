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
