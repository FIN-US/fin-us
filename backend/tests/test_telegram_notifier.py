import inspect
import logging
from pathlib import Path

import httpx
import pytest

import backend.telegram_notifier
from backend.telegram_notifier import (
    TELEGRAM_MESSAGE_LIMIT,
    TelegramApiError,
    TelegramNotifier,
    _retry_after_seconds,
    call_telegram_api,
    fetch_telegram_api,
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

    # 필드명도 값도 한국어다 (#297 검수 2). decision/urgency는 AgentReport의 정해진
    # 값이라 출력 계층이 결정론적으로 번역한다 — LLM에게 시키면 매번 다른 말이 나온다.
    # 제목은 목록 밖이고 값이 늘어선 줄만 "- " 표시를 받는다. 좁은 말풍선에서 줄이 접혀도
    # 표시 없는 줄은 앞 줄의 계속이라는 뜻이 되므로 항목 경계가 유지된다 (#297 검수 3차).
    assert message.splitlines() == [
        "삼성전자 / disclosure",
        "- 판단: 보유 유지 (확신도 0.82)",
        "- 이유: 단기 변동성 확대 가능성",
        "- 긴급도: 매우 높음, 대량보유 변동 공시",
        "- 요약: 대량보유 변동",
    ]


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
    # 긴급 여부는 이제 render가 붙이는 배너(🚨 긴급 알림)가 알린다. 제목의 "[긴급]"을
    # 뺐으므로 이 본문에는 긴급도 라벨 말고 긴급이라는 말이 없어야 한다 (#297 검수 1).
    assert "긴급도: 보통" in message
    assert "🚨" not in message


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


@pytest.mark.asyncio
async def test_httpx_request_logging_is_redacted_end_to_end(caplog):
    """httpx가 스스로 찍는 요청 로그에 토큰이 남지 않아야 한다 (#257).

    이것만은 발생 지점 차단으로 못 막는다. 예외가 아니라 라이브러리의 정상 INFO 로그라
    우리 코드를 어떻게 고쳐도 httpx가 request.url을 직접 찍는다. 그래서 리댁션 필터의
    로거 이름 목록에 "httpx" 한 항목이 남아 있다.

    로거 이름을 문자열로 비교하는 대신 실제 요청을 태워 잡는다 — httpx가 로거를
    httpx._client 같은 이름으로 옮기면 필터는 조용히 무력화되는데(필터는 전파된
    레코드에 걸리지 않는다), 이 테스트는 그때 빨갛게 깨진다. 목록에 기댄 리댁션이
    조용히 되돌아가는 것이 이 이슈의 출발점이었다.
    """
    token = "8666951614:SECRET"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))

    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post(url, json={"text": "안녕"})

    assert "HTTP Request" in caplog.text
    assert token not in caplog.text
    assert "bot<redacted>" in caplog.text


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


def _api_error(status_code, body, *, method="sendMessage"):
    """call_telegram_api가 상태 오류에서 만드는 것과 같은 예외.

    실제 생성 경로(raise_for_status → TelegramApiError)는
    test_call_telegram_api_raises_error_without_token이 따로 검증한다.
    """
    return TelegramApiError(method, status_code=status_code, body=body)


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


# main의 _http_status_error는 여기서 사라진다. _retry_after_seconds가 httpx 예외 대신
# TelegramApiError를 읽게 되어(#257) 쓰는 곳이 없다 — 그 자리는 위 _api_error가 맡는다.


def test_no_module_builds_a_telegram_url_outside_the_gateway():
    """봇 토큰이 URL에 실리는 지점은 _request_telegram_api 하나여야 한다 (#257).

    이 가드가 필요한 이유는 가정이 아니다. 이 PR이 열려 있는 동안 머지된 #260이
    deleteMessage와 editMessageText 호출을 추가하면서 URL을 직접 조립하고 그 예외를
    logger.error("...: %s", exc)로 찍었다. allowlist에서 backend.*를 뺀 뒤였으므로
    병합하면 이 PR이 막은 누출이 그대로 다시 열렸다.

    #257이 예측한 재발이 #257을 고치는 중에 실제로 일어난 셈이다. 리뷰가 잡지 못하면
    배포까지 간다는 것이 이 이슈의 출발점이었으니, 리뷰에 맡기지 않고 여기서 잡는다.
    """
    backend_dir = Path(backend.telegram_notifier.__file__).parent
    gateway_source = inspect.getsource(backend.telegram_notifier._request_telegram_api)

    offenders = []
    for path in sorted(backend_dir.rglob("*.py")):
        # 우리 소스만 본다. CI는 backend/ 안에 가상환경을 만들고, 거기 설치된 서드파티
        # 패키지에도 텔레그램 URL 문자열이 있다(실제로 이 가드가 처음 그걸로 깨졌다).
        # 가상환경 이름은 .venv일 수도 venv일 수도 있어 이름으로 거르지 않고, 서드파티
        # 코드가 반드시 들어가는 site-packages/dist-packages로 거른다.
        #
        # 판정은 반드시 backend_dir 기준 상대 경로로 한다. 절대 경로의 부분을 보면
        # 체크아웃 위치에 점으로 시작하는 디렉토리가 하나만 있어도(worktree가 .claude/
        # 아래 있는 경우처럼) 전부 걸러져 가드가 조용히 무력해진다 — 실측으로 확인했다.
        relative = path.relative_to(backend_dir)
        if any(
            part.startswith(".")
            or part in {"__pycache__", "site-packages", "dist-packages", "tests"}
            for part in relative.parts
        ):
            continue
        source = path.read_text(encoding="utf-8")
        for number, line in enumerate(source.splitlines(), 1):
            if "api.telegram.org" not in line:
                continue
            # 관문 안의 그 한 줄만 허용한다.
            if path.name == "telegram_notifier.py" and line in gateway_source:
                continue
            # 상대 경로로 남긴다 — 파일명만 찍으면 어느 트리의 파일인지 알 수 없다.
            offenders.append(f"{relative}:{number}: {line.strip()}")

    assert offenders == [], (
        "텔레그램 URL을 직접 만드는 곳이 생겼다. call_telegram_api / fetch_telegram_api를 "
        "쓰지 않으면 raise_for_status의 예외에 봇 토큰이 실려 로그로 샌다 (#257):\n"
        + "\n".join(offenders)
    )


def test_retry_after_seconds_reads_429_parameters():
    """429의 retry_after를 로그에 남길 수 있어야 한다 (#247 자가리뷰).

    send_text가 bool만 돌려주는 탓에 호출부의 재시도 간격은 고정 추측값이다.
    실제 ban 길이가 그 가정과 맞는지 판단할 근거가 로그에 있어야 한다.
    """
    exc = _api_error(429, {"ok": False, "parameters": {"retry_after": 37}})
    assert _retry_after_seconds(exc) == 37


def test_retry_after_seconds_returns_none_for_non_429_or_malformed_body():
    assert _retry_after_seconds(_api_error(500, {"ok": False})) is None
    assert _retry_after_seconds(_api_error(429, {"ok": False})) is None
    assert _retry_after_seconds(_api_error(429, {"parameters": {}})) is None
    assert (
        _retry_after_seconds(_api_error(429, {"parameters": {"retry_after": "30"}}))
        is None
    )
    # bool은 int의 서브클래스라 가드가 없으면 True가 1로 통과한다 (PR #253 리뷰).
    assert (
        _retry_after_seconds(_api_error(429, {"parameters": {"retry_after": True}}))
        is None
    )
    assert _retry_after_seconds(httpx.ConnectError("boom")) is None


@pytest.mark.asyncio
async def test_send_text_logs_retry_after_on_429(monkeypatch, caplog):
    async def raise_429(text, *, reply_markup=None):
        raise _api_error(429, {"ok": False, "parameters": {"retry_after": 42}})

    notifier = TelegramNotifier("token", "123")
    monkeypatch.setattr(notifier, "_post_message", raise_429)

    with caplog.at_level(logging.ERROR):
        assert await notifier.send_text("안녕") is False

    assert "retry_after=42s" in caplog.text


@pytest.mark.asyncio
async def test_call_telegram_api_raises_error_without_token(
    monkeypatch, failing_telegram_client
):
    """상태 오류를 URL 없는 예외로 바꿔 던진다 — allowlist를 없앨 수 있는 근거다 (#257).

    httpx의 HTTPStatusError는 메시지에 요청 URL을 담고, 텔레그램 URL의 경로가 곧 봇
    토큰이다. 그 예외가 이 경계를 넘으면 "이걸 %s로 찍는 모든 로거"에 리댁션을 걸어야
    하고, 그 목록은 두 번 뚫렸다 (PR #253 1·2차 리뷰).
    """
    token = "8666951614:SECRET"
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        failing_telegram_client(
            429,
            {
                "ok": False,
                "description": "Too Many Requests: retry after 42",
                "parameters": {"retry_after": 42},
            },
        ),
    )

    with pytest.raises(TelegramApiError) as excinfo:
        await call_telegram_api(token, "sendMessage", payload={})

    exc = excinfo.value
    assert token not in str(exc)
    assert "api.telegram.org" not in str(exc)
    # 예외 체인도 끊겨 있어야 한다. 남으면 exc_info 로깅이나 처리되지 않은 트레이스백이
    # 원본 HTTPStatusError를 — 따라서 URL을 — 그대로 찍는다.
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    # URL이 빠진 만큼 진단 정보는 오히려 늘어야 한다.
    assert exc.status_code == 429
    assert "HTTP 429" in str(exc)
    assert "Too Many Requests: retry after 42" in str(exc)


@pytest.mark.asyncio
async def test_send_text_succeeds_when_200_body_is_not_json(monkeypatch):
    """본문을 쓰지 않는 호출은 200의 본문이 JSON이 아니어도 성공해야 한다 (#257 자가리뷰).

    무조건 response.json()을 부르면 본문을 읽지도 않는 sendMessage가 파싱 실패로 실패한다 —
    리팩터링 전에는 없던 동작이고, send_text가 False를 돌려주면 _send_text_settled가
    최대 4회 재시도한다. call_telegram_api/fetch_telegram_api 분리가 그 비대칭을 막는다.
    """

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("backend.telegram_notifier.httpx.AsyncClient", FakeAsyncClient)
    notifier = TelegramNotifier("token", "123")

    assert await notifier.send_text("안녕") is True

    # 반대로 본문을 읽는 호출은 파싱 실패를 실패로 남긴다. 조용히 {}로 넘기면 폴러가
    # "update 없음"으로 읽고 로그도 백오프도 없이 다음 폴링으로 넘어간다.
    with pytest.raises(TelegramApiError):
        await fetch_telegram_api("token", "getUpdates", payload={})


@pytest.mark.asyncio
async def test_call_telegram_api_returns_none_so_body_readers_cannot_use_it(monkeypatch):
    """본문이 필요한 호출부가 call_telegram_api를 고르면 조용히가 아니라 즉시 깨져야 한다.

    플래그 하나짜리 API였다면 빠뜨린 호출부가 빈 dict를 받아 getUpdates는 "update 없음",
    getMe는 username ""으로 조용히 퇴화한다. 반환형을 나눠 그 실수를 불가능하게 만든
    것이 이 분리의 목적이다 (#257 자가리뷰).
    """

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": [{"update_id": 1}]}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("backend.telegram_notifier.httpx.AsyncClient", FakeAsyncClient)

    assert await call_telegram_api("token", "getUpdates", payload={}) is None
    assert await fetch_telegram_api("token", "getUpdates", payload={}) == {
        "ok": True,
        "result": [{"update_id": 1}],
    }


@pytest.mark.asyncio
async def test_call_telegram_api_redacts_token_from_transport_error_message(monkeypatch):
    """상태 오류가 아닌 실패도 토큰을 흘리지 않아야 한다 (#257).

    httpx의 전송 계층 예외는 대개 URL을 담지 않지만 전부는 아니다 — UnsupportedProtocol,
    InvalidURL처럼 메시지에 URL을 넣는 타입이 있다. 그 메시지를 그대로 reason에 실으면
    상태 오류 경로만 막고 이쪽으로 새는 것이라, 예외를 만들 때 한 번 더 거른다.
    """
    token = "8666951614:SECRET"

    class ExplodingAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, **kwargs):
            raise httpx.UnsupportedProtocol(f"Request URL has an unsupported protocol: {url}")

    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient", ExplodingAsyncClient
    )

    with pytest.raises(TelegramApiError) as excinfo:
        await call_telegram_api(token, "getUpdates", payload={})

    assert token not in str(excinfo.value)
    assert "bot<redacted>" in str(excinfo.value)
    # 어떤 종류의 실패였는지는 남아야 한다.
    assert "UnsupportedProtocol" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_text_failure_log_redacts_bot_token(
    monkeypatch, caplog, failing_telegram_client
):
    """전송 실패 로그에 봇 토큰이 평문으로 남지 않아야 한다 (PR #253 1차 리뷰, #257).

    _post_message가 실제로 URL을 만들고 raise_for_status가 도는 경로를 그대로 태운다.
    예전에는 이 모듈 로거가 리댁션 목록에 들어 있어야 통과했고, 지금은 예외에 URL이
    없어야 통과한다 — 즉 목록을 지워도 이 테스트가 남는다.
    """
    token = "SECRET-BOT-TOKEN-123"
    monkeypatch.setattr(
        "backend.telegram_notifier.httpx.AsyncClient",
        failing_telegram_client(429, {"ok": False, "parameters": {"retry_after": 42}}),
    )
    notifier = TelegramNotifier(token, "123")

    with caplog.at_level(logging.ERROR):
        assert await notifier.send_text("안녕") is False

    assert token not in caplog.text
    assert "api.telegram.org" not in caplog.text
    # 진단에 필요한 정보는 남아 있어야 한다.
    assert "retry_after=42s" in caplog.text


@pytest.mark.asyncio
async def test_send_text_publishes_and_clears_retry_after(monkeypatch):
    """429의 flood-wait을 호출부가 읽을 수 있게 남기고, 성공하면 지운다 (PR #253 3차 리뷰).

    telegram_commands._send_text_settled가 이 값 하나로 "재시도할 가치가 있는가"를 가른다.
    소비 측 두 분기는 고정돼 있었지만 생산 측이 무커버라, 두 대입을 지워도 스위트가
    초록이었다 — 재시도가 조용히 (1, 3, 9) 추측 백오프로 되돌아간다.
    """
    notifier = TelegramNotifier("token", "123")

    async def fail(text, *, reply_markup=None):
        raise _api_error(429, {"ok": False, "parameters": {"retry_after": 42}})

    monkeypatch.setattr(notifier, "_post_message", fail)
    assert await notifier.send_text("안녕") is False
    assert notifier.last_retry_after_seconds == 42

    # 429가 아닌 실패는 값을 남기지 않는다 — 이월되면 다음 재시도가 엉뚱한 값을 기다린다.
    async def fail_500(text, *, reply_markup=None):
        raise _api_error(500, {"ok": False})

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
