"""투자 성향 설정(/risk, /start 두 번째 문항)과 NAT 사용자 선호 클라이언트 (#397).

성향 값은 NAT 사용자 선호 메모리에만 저장된다. backend가 지키는 것은 세 가지다.

1. 명령·버튼이 정규값(conservative/aggressive/clear)으로만 NAT에 간다.
2. 저장 실패가 성공 문구로 나가지 않는다.
3. 사용자 식별자 헤더는 텔레그램 채팅 스레드에만 붙는다 — 스케줄러·실적 스레드에는 성향이 적용되지 않는다.
"""

import pytest

import backend.services as services
import backend.user_preferences as user_preferences
from backend.telegram_commands import (
    RISK_COMMAND_HELP,
    RISK_MEMORY_DISABLED_TEXT,
    RISK_ONBOARDING_QUESTION,
    TelegramCommandHandler,
)
from backend.tests.test_telegram_level import FakeNotifier, FakeState, _press, _send
from backend.user_preferences import (
    UserMemoryDisabled,
    nat_user_id_for_conversation,
    normalize_risk_choice,
    request_risk_profile,
)

USER = "telegram:123"


class FakeRiskClient:
    def __init__(self, stored=None, error=None):
        self.stored = stored
        self.error = error
        self.calls = []

    async def __call__(self, user_id, choice=None):
        self.calls.append((user_id, choice))
        if self.error is not None:
            raise self.error
        if choice == "clear":
            self.stored = None
        elif choice is not None:
            self.stored = choice
        return self.stored


def _handler(notifier, client):
    return TelegramCommandHandler(
        notifier=notifier,
        state_factory=lambda: FakeState(),
        risk_profile_client=client,
    )


# ---- /start ----


@pytest.mark.asyncio
async def test_start_sends_the_risk_question_without_calling_nat():
    """성향 문항은 버튼만 보여준다. NAT이 내려가 있어도 /start가 실패하지 않아야 한다."""
    notifier = FakeNotifier()
    client = FakeRiskClient(error=AssertionError("/start must not call NAT"))

    await _send(_handler(notifier, client), "/start")

    assert len(notifier.messages) == 2
    assert RISK_ONBOARDING_QUESTION in notifier.messages[1]
    assert client.calls == []
    buttons = notifier.reply_markups[1]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["risk:conservative", "risk:aggressive", "risk:clear"]


# ---- /risk ----


@pytest.mark.asyncio
async def test_risk_without_argument_reads_the_current_setting():
    notifier = FakeNotifier()
    client = FakeRiskClient(stored="aggressive")

    await _send(_handler(notifier, client), "/risk")

    assert client.calls == [(USER, None)]
    assert notifier.messages[-1].startswith("현재 투자 성향: 공격형.")
    assert RISK_COMMAND_HELP in notifier.messages[-1]


@pytest.mark.asyncio
async def test_risk_without_stored_value_says_not_set():
    notifier = FakeNotifier()

    await _send(_handler(notifier, FakeRiskClient()), "/risk")

    assert notifier.messages[-1].startswith("현재 투자 성향: 설정 안 함")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "choice", "expected"),
    [
        ("/risk 안정형", "conservative", "투자 성향을 안정형(으)로 저장했습니다."),
        ("/risk 공격형", "aggressive", "투자 성향을 공격형(으)로 저장했습니다."),
        ("/risk 해제", "clear", "투자 성향을 해제했습니다."),
    ],
)
async def test_risk_command_sends_the_normalized_choice(text, choice, expected):
    notifier = FakeNotifier()
    client = FakeRiskClient(stored="aggressive")

    await _send(_handler(notifier, client), text)

    assert client.calls == [(USER, choice)]
    assert notifier.messages[-1].startswith(expected)


@pytest.mark.asyncio
async def test_unknown_risk_argument_shows_usage_without_calling_nat():
    notifier = FakeNotifier()
    client = FakeRiskClient()

    await _send(_handler(notifier, client), "/risk 중립형")

    assert client.calls == []
    assert notifier.messages[-1] == RISK_COMMAND_HELP


@pytest.mark.asyncio
async def test_risk_button_answers_the_callback_and_stores():
    notifier = FakeNotifier()
    client = FakeRiskClient()

    await _press(_handler(notifier, client), "risk:conservative")

    assert notifier.callback_answers == [("cb", None)]
    assert client.calls == [(USER, "conservative")]
    assert "안정형" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_unknown_risk_button_is_rejected():
    notifier = FakeNotifier()
    client = FakeRiskClient()

    await _press(_handler(notifier, client), "risk:yolo")

    assert client.calls == []
    assert notifier.callback_answers == [("cb", "지원하지 않는 버튼입니다.")]


@pytest.mark.asyncio
async def test_disabled_memory_is_reported():
    notifier = FakeNotifier()

    await _send(_handler(notifier, FakeRiskClient(error=UserMemoryDisabled("off"))), "/risk 안정형")

    assert notifier.messages[-1] == RISK_MEMORY_DISABLED_TEXT


@pytest.mark.asyncio
async def test_nat_failure_never_reports_success():
    """저장에 실패했는데 "저장했습니다"가 나가면 사용자는 성향이 적용된 줄 안다.

    뮤테이션: ``_run_risk_request``의 ``except Exception`` 분기를 지우면 예외가 올라가 red.
    """
    notifier = FakeNotifier()

    await _send(_handler(notifier, FakeRiskClient(error=ConnectionError("nat down"))), "/risk 공격형")

    assert notifier.messages[-1].startswith("투자 성향을 처리하지 못했습니다")
    assert all("저장했습니다" not in message for message in notifier.messages)


def test_normalize_risk_choice():
    assert normalize_risk_choice("안정형") == "conservative"
    assert normalize_risk_choice(" 공격 ") == "aggressive"
    assert normalize_risk_choice("Aggressive") == "aggressive"
    assert normalize_risk_choice("설정 안함") == "clear"
    assert normalize_risk_choice("5000000") is None


# ---- NAT 클라이언트 ----


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _fake_async_client(recorder, response):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers=None, json=None):
            recorder.append({"url": url, "headers": headers, "json": json})
            return response

    return FakeAsyncClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "payload"),
    [
        (None, {"user_id": USER, "action": "get"}),
        ("conservative", {"user_id": USER, "action": "set", "risk_profile": "conservative"}),
        ("clear", {"user_id": USER, "action": "clear"}),
    ],
)
async def test_request_risk_profile_payloads(monkeypatch, choice, payload):
    requests = []
    monkeypatch.setattr(
        user_preferences.httpx,
        "AsyncClient",
        _fake_async_client(requests, _FakeResponse(200, {"enabled": True, "risk_profile": "conservative"})),
    )

    assert await request_risk_profile(USER, choice) == "conservative"
    assert requests[0]["url"].endswith("/v1/user-preferences")
    assert requests[0]["json"] == payload


@pytest.mark.asyncio
async def test_request_risk_profile_rejects_unknown_choice_before_sending(monkeypatch):
    requests = []
    monkeypatch.setattr(user_preferences.httpx, "AsyncClient", _fake_async_client(requests, None))

    with pytest.raises(ValueError):
        await request_risk_profile(USER, "5000000")
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_FakeResponse(200, {"enabled": False}), UserMemoryDisabled),
        (_FakeResponse(200, {"enabled": True, "risk_profile": "moderate"}), RuntimeError),
        (_FakeResponse(422, {"detail": "bad"}), RuntimeError),
        (_FakeResponse(200, ["not", "an", "object"]), RuntimeError),
    ],
)
async def test_request_risk_profile_failures(monkeypatch, response, error):
    monkeypatch.setattr(user_preferences.httpx, "AsyncClient", _fake_async_client([], response))

    with pytest.raises(error):
        await request_risk_profile(USER, "aggressive")


# ---- x-user-id 헤더 ----


@pytest.mark.parametrize(
    ("conversation_id", "expected"),
    [
        ("telegram:123", "telegram:123"),
        ("telegram:-100123", "telegram:-100123"),
        ("telegram:123:earnings:%EC%82%BC%EC%84%B1", None),
        ("morning-briefing:2026-09-17", None),
        ("fin-us-default", None),
        (None, None),
    ],
)
def test_nat_user_id_only_for_telegram_chat_threads(conversation_id, expected):
    assert nat_user_id_for_conversation(conversation_id) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation_id", "expected_header"),
    [("telegram:123", "telegram:123"), ("morning-briefing:2026-09-17", None)],
)
async def test_nat_chat_sends_user_header_only_for_chat_threads(monkeypatch, conversation_id, expected_header):
    """뮤테이션: ``_llm_nat_chat``에서 ``x-user-id`` 부착을 지우면 telegram 케이스가 red."""
    requests = []
    response = _FakeResponse(200, {"choices": [{"message": {"content": "답변"}}]})
    monkeypatch.setattr(services.httpx, "AsyncClient", _fake_async_client(requests, response))

    await services._llm_nat_chat("반도체 종목 추천해줘", conversation_id=conversation_id)

    assert requests[0]["headers"].get("x-user-id") == expected_header
    assert requests[0]["headers"]["conversation-id"] == conversation_id
