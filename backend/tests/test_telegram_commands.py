import pytest

from backend.telegram_commands import TelegramCommandHandler, TelegramCommandPoller


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

    async def send_text(self, text):
        self.messages.append(text)


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
