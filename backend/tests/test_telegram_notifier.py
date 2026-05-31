import logging

import httpx
import pytest

from backend.telegram_notifier import TelegramNotifier, should_send_telegram_alert


def test_should_send_telegram_alert_requires_high_or_critical_with_flag():
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "high"}) is True
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "critical"}) is True
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "normal"}) is False
    assert should_send_telegram_alert({"telegram_alert": False, "urgency": "critical"}) is False
    assert should_send_telegram_alert({}) is False


def test_should_send_telegram_alert_supports_alert_modes():
    normal_analysis = {"telegram_alert": False, "urgency": "normal"}
    urgent_analysis = {"telegram_alert": True, "urgency": "critical"}

    assert should_send_telegram_alert(normal_analysis, alert_mode="urgent") is False
    assert should_send_telegram_alert(normal_analysis, alert_mode="all") is True
    assert should_send_telegram_alert(urgent_analysis, alert_mode="off") is False
    assert should_send_telegram_alert(urgent_analysis, alert_mode="unknown") is False


def test_notifier_disabled_for_missing_or_placeholder_config():
    assert TelegramNotifier("", "123").enabled is False
    assert TelegramNotifier("your_telegram_bot_token_here", "123").enabled is False
    assert TelegramNotifier("token", "your_telegram_chat_id_here").enabled is False


def test_format_analysis_alert_uses_plain_text():
    notifier = TelegramNotifier("token", "123")
    message = notifier.format_analysis_alert(
        stock="삼성전자",
        source="disclosure",
        analysis_data={
            "summary": "대량보유 변동",
            "details": {
                "decision": "HOLD",
                "confidence_score": 0.82,
                "reason": "단기 변동성 확대 가능성",
            },
            "urgency": "critical",
            "urgency_reason": "대량보유 변동 공시",
            "telegram_alert": True,
        },
    )

    assert "[긴급] 삼성전자 / disclosure" in message
    assert "Decision: HOLD (0.82)" in message
    assert "Reason: 단기 변동성 확대 가능성" in message
    assert "Urgency: critical - 대량보유 변동 공시" in message
    assert "Summary: 대량보유 변동" in message


def test_format_analysis_alert_marks_only_actual_urgent_alerts():
    notifier = TelegramNotifier("token", "123")
    message = notifier.format_analysis_alert(
        stock="삼성전자",
        source="news",
        analysis_data={
            "summary": "시장 동향 업데이트",
            "details": {
                "decision": "HOLD",
                "reason": "추가 확인 필요",
            },
            "urgency": "normal",
            "telegram_alert": False,
        },
    )

    assert message.splitlines()[0] == "삼성전자 / news"
    assert "긴급" not in message


def test_format_morning_briefing_uses_expected_sections():
    notifier = TelegramNotifier("token", "123")

    message = notifier.format_morning_briefing({
        "market_summary": "미국 증시 상승, 달러 약세",
        "watchlist": ["삼성전자: 반도체 수급 개선"],
        "trading_ideas": ["삼성전자 눌림목 매수 시나리오"],
        "catalysts": ["오늘 CPI 발표"],
    })

    assert "📰 오늘의 시장 요약" in message
    assert "미국 증시 상승, 달러 약세" in message
    assert "📊 관심종목 동향" in message
    assert "- 삼성전자: 반도체 수급 개선" in message
    assert "🎯 오늘의 트레이딩 아이디어" in message
    assert "- 삼성전자 눌림목 매수 시나리오" in message
    assert "⚡ 주요 촉매 이벤트" in message
    assert "- 오늘 CPI 발표" in message
    assert len(message) <= 4000


@pytest.mark.asyncio
async def test_send_analysis_alert_skips_when_gate_is_false(monkeypatch):
    notifier = TelegramNotifier("token", "123")
    called = False

    async def fake_post(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(notifier, "_post_message", fake_post)

    result = await notifier.send_analysis_alert(
        "삼성전자",
        "news",
        {"telegram_alert": False, "urgency": "critical"},
    )

    assert result is False
    assert called is False


@pytest.mark.asyncio
async def test_send_analysis_alert_returns_false_on_http_error(monkeypatch):
    notifier = TelegramNotifier("token", "123")

    async def fake_post(*args, **kwargs):
        raise httpx.HTTPError("boom")

    monkeypatch.setattr(notifier, "_post_message", fake_post)

    result = await notifier.send_analysis_alert(
        "삼성전자",
        "news",
        {"telegram_alert": True, "urgency": "high"},
    )

    assert result is False


def test_httpx_telegram_bot_token_is_redacted_from_logs(caplog):
    telegram_url = "https://api.telegram.org/bot8666951614:SECRET/sendMessage"

    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            'HTTP Request: POST %s "HTTP/1.1 200 OK"',
            telegram_url,
        )

    assert "8666951614:SECRET" not in caplog.text
    assert "https://api.telegram.org/bot<redacted>/sendMessage" in caplog.text


def test_telegram_error_log_redacts_bot_token(caplog):
    telegram_url = "https://api.telegram.org/bot8666951614:SECRET/getUpdates"

    with caplog.at_level(logging.ERROR, logger="httpx"):
        logging.getLogger("httpx").error("request failed: %s", httpx.HTTPError(telegram_url))

    assert "8666951614:SECRET" not in caplog.text
    assert "https://api.telegram.org/bot<redacted>/getUpdates" in caplog.text


@pytest.mark.asyncio
async def test_send_text_posts_reply_markup(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    reply_markup = {
        "inline_keyboard": [
            [{"text": "확정", "callback_data": "order:confirm"}],
        ]
    }
    monkeypatch.setattr("backend.telegram_notifier.httpx.AsyncClient", FakeAsyncClient)
    notifier = TelegramNotifier("token", "123")

    result = await notifier.send_text("주문 확인", reply_markup=reply_markup)

    assert result is True
    assert captured["url"] == "https://api.telegram.org/bottoken/sendMessage"
    assert captured["json"] == {
        "chat_id": "123",
        "text": "주문 확인",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup,
    }


@pytest.mark.asyncio
async def test_answer_callback_query_posts_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("backend.telegram_notifier.httpx.AsyncClient", FakeAsyncClient)
    notifier = TelegramNotifier("token", "123")

    result = await notifier.answer_callback_query("callback-1", text="처리했습니다.")

    assert result is True
    assert captured["url"] == "https://api.telegram.org/bottoken/answerCallbackQuery"
    assert captured["json"] == {
        "callback_query_id": "callback-1",
        "text": "처리했습니다.",
    }


@pytest.mark.asyncio
async def test_send_chat_action_posts_typing_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("backend.telegram_notifier.httpx.AsyncClient", FakeAsyncClient)
    notifier = TelegramNotifier("token", "123")

    result = await notifier.send_chat_action()

    assert result is True
    assert captured["url"] == "https://api.telegram.org/bottoken/sendChatAction"
    assert captured["json"] == {"chat_id": "123", "action": "typing"}


@pytest.mark.asyncio
async def test_set_bot_commands_posts_command_menu_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    commands = [
        {"command": "balance", "description": "잔고 조회"},
        {"command": "alerts", "description": "알림 모드 변경"},
    ]
    monkeypatch.setattr("backend.telegram_notifier.httpx.AsyncClient", FakeAsyncClient)
    notifier = TelegramNotifier("token", "123")

    result = await notifier.set_bot_commands(commands)

    assert result is True
    assert captured["url"] == "https://api.telegram.org/bottoken/setMyCommands"
    assert captured["json"] == {"commands": commands}


@pytest.mark.asyncio
async def test_load_bot_username_fetches_and_caches_get_me(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": {"username": "Finus_Bot"}}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setattr("backend.telegram_notifier.httpx.AsyncClient", FakeAsyncClient)
    notifier = TelegramNotifier("token", "123")

    assert await notifier.load_bot_username() == "finus_bot"
    assert await notifier.load_bot_username() == "finus_bot"
    assert calls == [("https://api.telegram.org/bottoken/getMe", {})]
