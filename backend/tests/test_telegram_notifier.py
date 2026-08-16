import logging

import httpx
import pytest

from backend.telegram_notifier import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramNotifier,
    _retry_after_seconds,
    should_send_telegram_alert,
)


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


# ── #260: 진행 메시지 전송(message_id 확보) · 편집 ──────────────────────────


def _fake_client_factory(calls, response):
    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, json):
            calls.append((url, json))
            return response

    return FakeAsyncClient


class _FakeResponse:
    def __init__(self, body=None, error=None):
        self._body = body
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.mark.asyncio
async def test_send_text_returning_id_extracts_message_id(monkeypatch):
    calls = []
    response = _FakeResponse({"ok": True, "result": {"message_id": 4242}})
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory(calls, response),
    )
    notifier = TelegramNotifier("token", "123")

    message_id = await notifier.send_text_returning_id("⏳ 분석 중입니다...")

    assert message_id == 4242
    assert calls[0][0] == "https://api.telegram.org/bottoken/sendMessage"
    assert calls[0][1]["text"] == "⏳ 분석 중입니다..."


@pytest.mark.asyncio
async def test_send_text_returning_id_returns_none_on_unusable_body(monkeypatch):
    """본문을 읽을 수 없으면 전송은 성공했어도 '편집 불가'로 떨어뜨린다."""
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory([], _FakeResponse(body=None)),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.send_text_returning_id("⏳") is None


@pytest.mark.asyncio
async def test_send_text_returning_id_rejects_a_body_that_says_not_ok(monkeypatch):
    """2xx + ok:false 본문의 message_id는 쓰지 않는다 (PR #263 리뷰 — 뮤테이션 생존).

    ok:false면 result가 무엇이든 그 메시지는 만들어지지 않았다. 그 id로 삭제·편집을
    시도하면 남의 메시지를 건드리거나 조용히 실패한다.
    """
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory([], _FakeResponse({"ok": False, "result": {"message_id": 4242}})),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.send_text_returning_id("⏳") is None


@pytest.mark.asyncio
async def test_send_text_returning_id_returns_none_on_send_failure(monkeypatch):
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory([], _FakeResponse(error=httpx.HTTPError("boom"))),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.send_text_returning_id("⏳") is None


@pytest.mark.asyncio
async def test_edit_message_text_posts_edit_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory(calls, _FakeResponse({"ok": True})),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.edit_message_text(4242, "최종 답변") is True
    assert calls == [
        (
            "https://api.telegram.org/bottoken/editMessageText",
            {
                "chat_id": "123",
                "message_id": 4242,
                "text": "최종 답변",
                "disable_web_page_preview": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_edit_message_text_truncates_to_the_telegram_limit(monkeypatch):
    """공개 API이므로 한도를 넘는 본문이 들어와도 잘라 보낸다 (PR #263 리뷰 — 뮤테이션 생존).

    현재 호출부는 짧은 종료 표시 하나뿐이라 절단이 발동하지 않지만, 넘겨 보내면
    텔레그램이 400으로 거부하고 진행 메시지가 '분석 중'인 채로 남는다.
    """
    calls = []
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory(calls, _FakeResponse({"ok": True})),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.edit_message_text(4242, "가" * (TELEGRAM_MESSAGE_LIMIT + 500)) is True
    assert len(calls[0][1]["text"]) == TELEGRAM_MESSAGE_LIMIT


@pytest.mark.asyncio
async def test_send_text_truncates_to_the_telegram_limit(monkeypatch):
    """sendMessage도 같은 한도로 자른다 — 각주 계산이 이 상수를 전제로 한다."""
    calls = []
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory(calls, _FakeResponse({"ok": True, "result": {"message_id": 1}})),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.send_text("나" * (TELEGRAM_MESSAGE_LIMIT + 500)) is True
    assert len(calls[0][1]["text"]) == TELEGRAM_MESSAGE_LIMIT


@pytest.mark.asyncio
async def test_edit_message_text_returns_false_on_failure(monkeypatch):
    """편집 실패는 예외가 아니라 False — 호출부가 새 메시지로 폴백한다."""
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory([], _FakeResponse(error=httpx.HTTPError("message is too old"))),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.edit_message_text(4242, "최종 답변") is False


@pytest.mark.asyncio
async def test_progress_helpers_are_inert_when_notifier_disabled():
    notifier = TelegramNotifier("", "")

    assert await notifier.send_text_returning_id("⏳") is None
    assert await notifier.edit_message_text(1, "답변") is False


@pytest.mark.asyncio
async def test_delete_message_posts_delete_payload(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory(calls, _FakeResponse({"ok": True})),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.delete_message(4242) is True
    assert calls == [
        (
            "https://api.telegram.org/bottoken/deleteMessage",
            {"chat_id": "123", "message_id": 4242},
        )
    ]


@pytest.mark.asyncio
async def test_delete_message_returns_false_on_failure(monkeypatch):
    """삭제 거부는 예외가 아니라 False — 호출부가 종료 표시 편집으로 폴백한다."""
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        _fake_client_factory([], _FakeResponse(error=httpx.HTTPError("message can't be deleted"))),
    )
    notifier = TelegramNotifier("token", "123")

    assert await notifier.delete_message(4242) is False


@pytest.mark.asyncio
async def test_delete_message_is_inert_when_notifier_disabled():
    assert await TelegramNotifier("", "").delete_message(1) is False


def _http_status_error(status_code, body):
    request = httpx.Request("POST", "https://api.telegram.org/bottoken/sendMessage")
    response = httpx.Response(status_code, json=body, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def test_retry_after_seconds_reads_429_parameters():
    """429의 retry_after를 로그에 남길 수 있어야 한다 (#247 자가리뷰).

    send_text가 bool만 돌려주는 탓에 호출부의 재시도 간격은 고정 추측값이다.
    실제 ban 길이가 그 가정과 맞는지 판단할 근거가 로그에 있어야 한다.
    """
    exc = _http_status_error(429, {"ok": False, "parameters": {"retry_after": 37}})
    assert _retry_after_seconds(exc) == 37


def test_retry_after_seconds_returns_none_for_non_429_or_malformed_body():
    assert _retry_after_seconds(_http_status_error(500, {"ok": False})) is None
    assert _retry_after_seconds(_http_status_error(429, {"ok": False})) is None
    assert _retry_after_seconds(_http_status_error(429, {"parameters": {}})) is None
    assert (
        _retry_after_seconds(_http_status_error(429, {"parameters": {"retry_after": "30"}}))
        is None
    )
    # bool은 int의 서브클래스라 가드가 없으면 True가 1로 통과한다 (PR #253 리뷰).
    assert (
        _retry_after_seconds(_http_status_error(429, {"parameters": {"retry_after": True}}))
        is None
    )
    assert _retry_after_seconds(httpx.ConnectError("boom")) is None


@pytest.mark.asyncio
async def test_send_text_logs_retry_after_on_429(monkeypatch, caplog):
    async def raise_429(text, *, reply_markup=None):
        raise _http_status_error(429, {"ok": False, "parameters": {"retry_after": 42}})

    notifier = TelegramNotifier("token", "123")
    monkeypatch.setattr(notifier, "_post_message", raise_429)

    with caplog.at_level(logging.ERROR):
        assert await notifier.send_text("안녕") is False

    assert "retry_after=42s" in caplog.text


@pytest.mark.asyncio
async def test_send_text_failure_log_redacts_bot_token(monkeypatch, caplog):
    """전송 실패 로그에 봇 토큰이 평문으로 남지 않아야 한다 (PR #253 리뷰).

    raise_for_status가 만드는 str(HTTPStatusError)에는 요청 URL이 들어 있고, 그 URL에
    토큰이 포함된다. 리댁션 필터가 httpx/httpcore 로거에만 걸려 있어 이 모듈 자신의
    로거로 찍는 경로는 걸러지지 않았다.
    """
    token = "SECRET-BOT-TOKEN-123"
    request = httpx.Request("POST", f"https://api.telegram.org/bot{token}/sendMessage")
    response = httpx.Response(
        429, json={"ok": False, "parameters": {"retry_after": 42}}, request=request
    )

    async def raise_for_status(text, *, reply_markup=None):
        response.raise_for_status()

    notifier = TelegramNotifier(token, "123")
    monkeypatch.setattr(notifier, "_post_message", raise_for_status)

    with caplog.at_level(logging.ERROR):
        assert await notifier.send_text("안녕") is False

    assert token not in caplog.text
    assert "bot<redacted>" in caplog.text
    # 진단에 필요한 정보는 남아 있어야 한다.
    assert "retry_after=42s" in caplog.text


def test_polling_error_log_redacts_bot_token(caplog):
    """폴러의 getUpdates 실패 로그에도 리댁션이 걸린다 (PR #253 3차 리뷰).

    _get_updates가 URL에 토큰을 넣고 raise_for_status를 호출하며, 401·409(인스턴스 중복)·
    429·5xx에서 폴링 루프가 5초마다 재시도하므로 전송 경로보다 트리거가 잦다.

    기존 test_telegram_error_log_redacts_bot_token도 getUpdates URL을 쓰지만 로거가
    httpx라 이 항목을 검증하지 않는다. allowlist에서 backend.telegram_commands만 빼도
    스위트 전체가 초록이었다 — 자격증명 누출은 조용히 되돌아가면 알 방법이 없다.
    """
    token = "8666951614:SECRET"
    url = f"https://api.telegram.org/bot{token}/getUpdates"

    with caplog.at_level(logging.ERROR, logger="backend.telegram_commands"):
        logging.getLogger("backend.telegram_commands").error(
            "Telegram command polling failed: %s", httpx.HTTPError(url)
        )

    assert token not in caplog.text
    assert "https://api.telegram.org/bot<redacted>/getUpdates" in caplog.text


@pytest.mark.asyncio
async def test_send_text_publishes_and_clears_retry_after(monkeypatch):
    """429의 flood-wait을 호출부가 읽을 수 있게 남기고, 성공하면 지운다 (PR #253 3차 리뷰).

    telegram_commands._send_text_settled가 이 값 하나로 "재시도할 가치가 있는가"를 가른다.
    소비 측 두 분기는 고정돼 있었지만 생산 측이 무커버라, 두 대입을 지워도 스위트가
    초록이었다 — 재시도가 조용히 (1, 3, 9) 추측 백오프로 되돌아간다.
    """
    notifier = TelegramNotifier("token", "123")

    async def fail(text, *, reply_markup=None):
        raise _http_status_error(429, {"ok": False, "parameters": {"retry_after": 42}})

    monkeypatch.setattr(notifier, "_post_message", fail)
    assert await notifier.send_text("안녕") is False
    assert notifier.last_retry_after_seconds == 42

    # 429가 아닌 실패는 값을 남기지 않는다 — 이월되면 다음 재시도가 엉뚱한 값을 기다린다.
    async def fail_500(text, *, reply_markup=None):
        raise _http_status_error(500, {"ok": False})

    monkeypatch.setattr(notifier, "_post_message", fail_500)
    assert await notifier.send_text("안녕") is False
    assert notifier.last_retry_after_seconds is None

    async def succeed(text, *, reply_markup=None):
        return None

    monkeypatch.setattr(notifier, "_post_message", fail)
    assert await notifier.send_text("안녕") is False
    monkeypatch.setattr(notifier, "_post_message", succeed)
    assert await notifier.send_text("안녕") is True
    assert notifier.last_retry_after_seconds is None
