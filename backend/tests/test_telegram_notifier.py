import httpx
import pytest

from backend.telegram_notifier import TelegramNotifier, should_send_telegram_alert


def test_should_send_telegram_alert_requires_high_or_critical_with_flag():
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "high"}) is True
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "critical"}) is True
    assert should_send_telegram_alert({"telegram_alert": True, "urgency": "normal"}) is False
    assert should_send_telegram_alert({"telegram_alert": False, "urgency": "critical"}) is False
    assert should_send_telegram_alert({}) is False


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
