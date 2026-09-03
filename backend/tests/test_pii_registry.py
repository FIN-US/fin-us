"""#354: 사용자 발화의 금액을 매매일지에 저장할 수 있어야 한다.

#230이 세운 마스킹 계층은 `llm_chat`이 NAT을 부르기 **전에** 사용자 발화를 자리표시자로
바꾸고, 그 매핑을 함수 지역 변수로만 들고 있었다. 그래서 LLM이 그 발화를 보고 쓴 일지가
`POST /api/v1/db/diary`로 돌아왔을 때 되돌릴 방법이 없었다 — #339(PR #351)는 그 지점에서
저장을 거부해 손상만 막았고, **기능은 회복되지 않았다.** 사용자가 값을 다시 적어도 새
scope로 다시 마스킹돼 똑같이 거부됐다.

이 파일이 고정하는 것은 그 회복이다. 세 층으로 본다:

1. `pii_registry` 단위 — 등록 수명이 딱 `active_mapping` 블록인가, 동시 등록이 서로를
   덮지 않는가, 되돌릴 수 없는 것을 정직하게 돌려주는가.
2. `create_db_diary` 경계 — 살아 있는 매핑은 원값으로 저장하고, 되돌릴 수 없는 것은
   422로 거부하는가(#339의 판정 유지).
3. 실제 배선 — `llm_chat`이 NAT 응답을 기다리는 **동안** 별개의 HTTP 요청으로 들어온
   일지 저장이 원값을 남기는가. 이것이 이슈 본문의 경로 그대로다.

3층이 이 파일의 요점이다. 1·2층만 있으면 "등록은 되는데 저장 요청이 그것을 못 보는"
회귀(예: ContextVar로 되돌리기)가 통과한다 — 저장은 같은 요청 안의 함수 호출이 아니라
**별개의 HTTP 요청**이기 때문이다.
"""

import asyncio
import re

import httpx
import pytest
import pytest_asyncio
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from backend import pii_registry, services
from backend.database import get_session
from backend.main import app
from backend.models import Diary
from backend.pii_mask import _PLACEHOLDER_RE, mask_pii
from backend.pii_registry import (
    _REGISTRY,
    active_mapping,
    placeholder_kind,
    restore_live_placeholders,
)


@pytest.fixture(autouse=True)
def _registry_is_empty_around_every_test():
    """등록소는 전역이므로 테스트 사이에 새면 다음 테스트가 조용히 다른 것을 본다.

    앞뒤로 모두 비어 있는지 단언한다 — 뒤쪽 단언이 `active_mapping`의 `finally`가
    실제로 도는지를 매 테스트에서 공짜로 검사해 준다.
    """
    assert not _REGISTRY
    yield
    assert not _REGISTRY, "active_mapping을 벗어난 뒤에도 등록이 남았다"


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest_asyncio.fixture(name="asgi_client")
async def asgi_client_fixture(session: Session):
    """앱을 **같은 이벤트 루프 위에서** 부르는 클라이언트.

    `TestClient`가 아니라 `ASGITransport`인 것이 의도다. 이 파일의 3층은 "채팅 요청이
    LLM 응답을 기다리는 동안 저장 요청이 들어온다"를 재현해야 하는데, 그 동시성은 하나의
    이벤트 루프 위에서 일어난다(uvicorn 단일 프로세스). 별도 스레드로 도는
    `TestClient`를 쓰면 재현하려는 배치가 달라진다.
    """
    app.dependency_overrides[get_session] = lambda: session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


def _first_placeholder(mapping: dict[str, str]) -> str:
    return next(iter(mapping))


def _scope_of(placeholder: str) -> str:
    return placeholder.rsplit("_", 2)[-2]


# ---------------------------------------------------------------------------
# 1. 등록소 단위
# ---------------------------------------------------------------------------

class TestRegistryLifetime:
    def test_mapping_is_visible_only_inside_the_block(self):
        masked, mapping = mask_pii("300만원 벌었어")
        placeholder = _first_placeholder(mapping)

        # 블록 밖 — 되돌릴 수 없다. 이것이 #339가 저장을 거부하던 상태 그대로다.
        assert restore_live_placeholders(masked) == (masked, [placeholder])

        with active_mapping(mapping):
            assert restore_live_placeholders(masked) == ("300만원 벌었어", [])

        assert restore_live_placeholders(masked) == (masked, [placeholder])

    def test_registration_is_cleared_when_the_block_raises(self):
        _, mapping = mask_pii("300만원 벌었어")

        with pytest.raises(RuntimeError):
            with active_mapping(mapping):
                assert _REGISTRY
                raise RuntimeError("provider 호출이 터졌다")

        # 등록이 남으면 그 매핑(사용자 발화의 평문 금액)이 프로세스에 영원히 산다.
        # autouse 픽스처도 같은 것을 보지만, 예외 경로는 명시적으로 못박아 둔다.
        assert not _REGISTRY

    def test_empty_mapping_does_not_touch_the_registry(self):
        """마스킹 대상이 없는 대다수 호출은 등록소를 건드리지 않는다."""
        masked, mapping = mask_pii("삼성전자 어때?")
        assert mapping == {}

        with active_mapping(mapping):
            assert not _REGISTRY
            assert restore_live_placeholders(masked) == (masked, [])


class TestUnrestorable:
    """되돌릴 수 없는 것을 **중립 문구로 바꾸지 않고** 그대로 돌려준다.

    `unmask_pii`는 매핑에 없는 자리표시자를 "(이전 금액 1)"로 바꾼다 — 사용자에게 보여
    줄 텍스트에서는 그것이 맞다. 여기서는 아니다. 이 값은 DB에 영구히 남고, 중립 문구를
    저장하면 손상의 종류만 바뀌고 소실은 그대로다(#339).
    """

    def test_live_scope_with_an_invented_number(self):
        _, mapping = mask_pii("300만원 벌었어")
        invented = f"<AMOUNT_{_scope_of(_first_placeholder(mapping))}_9>"

        with active_mapping(mapping):
            restored, leftover = restore_live_placeholders(f"오늘 {invented} 벌었다")

        assert leftover == [invented]
        assert invented in restored  # 중립 문구로 갈아엎지 않는다

    def test_scope_that_is_not_registered(self):
        """이전 턴의 자리표시자 — NAT SQLite 히스토리를 타고 다음 턴에 올라온 것."""
        _, previous_turn = mask_pii("잔고 12,345,000원")
        _, this_turn = mask_pii("5,000,000원 더")
        stale = _first_placeholder(previous_turn)

        with active_mapping(this_turn):
            restored, leftover = restore_live_placeholders(f"앞서 말한 {stale}")

        assert leftover == [stale]
        assert stale in restored

    def test_kinds_are_reported_without_leaking_the_token(self):
        _, mapping = mask_pii("계좌 12345678-01에서 300만원")
        kinds = {placeholder_kind(ph) for ph in mapping}
        assert kinds == {"ACCOUNT", "AMOUNT"}
        assert placeholder_kind("자리표시자가 아님") == ""


class TestConcurrentRequests:
    """동시 호출이 서로의 매핑을 덮지 않는다 — 이슈가 명시한 요구사항이다.

    매핑을 모듈 전역 dict **하나**로 올리는 것이 가장 짧은 "요청 범위 승격"인데, 그러면
    동시에 도는 두 요청 중 나중에 시작한 쪽이 앞선 쪽의 매핑을 덮어 **다른 사용자의
    금액이 남의 일지에 저장된다.** 등록소를 scope로 가르는 이유가 그것이다.
    """

    def test_two_live_mappings_do_not_see_each_other(self):
        masked_a, mapping_a = mask_pii("300만원 벌었어")
        masked_b, mapping_b = mask_pii("잔고 12,345,000원")

        with active_mapping(mapping_a):
            with active_mapping(mapping_b):
                assert restore_live_placeholders(masked_a) == ("300만원 벌었어", [])
                assert restore_live_placeholders(masked_b) == ("잔고 12,345,000원", [])
            # 안쪽 요청이 끝나도 바깥 요청의 매핑은 살아 있어야 한다.
            assert restore_live_placeholders(masked_a) == ("300만원 벌었어", [])
            assert restore_live_placeholders(masked_b)[1] == [
                _first_placeholder(mapping_b)
            ]

    @pytest.mark.asyncio
    async def test_gathered_llm_chat_calls_keep_their_own_mappings(self, monkeypatch):
        """`asyncio.gather`로 겹쳐 도는 `llm_chat` 둘이 서로의 값을 되돌리지 않는다.

        provider 스텁이 마스킹된 프롬프트를 받은 그 자리에서 등록소를 조회한다 — 즉
        "NAT이 일지를 저장하는 시점"을 함수 호출로 축약한 것이다. 3층 테스트가 이것을
        진짜 HTTP 요청으로 다시 한 번 본다.
        """
        seen: list[tuple[str, str]] = []

        async def fake_nat(masked_msg, *, conversation_id=None):
            # 두 호출이 확실히 겹치도록 서로를 기다리게 만든다.
            await asyncio.sleep(0)
            restored, leftover = restore_live_placeholders(masked_msg)
            assert leftover == []
            seen.append((masked_msg, restored))
            return "확인했습니다"

        monkeypatch.setattr(services, "_llm_nat_chat", fake_nat)

        await asyncio.gather(
            services.llm_chat("nat", "300만원 벌었어"),
            services.llm_chat("nat", "잔고 12,345,000원"),
        )

        assert sorted(restored for _, restored in seen) == sorted(
            ["300만원 벌었어", "잔고 12,345,000원"]
        )


class TestPlaceholderContract:
    def test_matches_the_same_tokens_as_pii_mask(self):
        """등록소 정규식이 `pii_mask`의 것과 갈리면 조회가 조용히 빗나간다.

        여기는 scope를 캡처하려고 별도 정규식을 두므로, 두 벌이 **같은 것을 매치한다**는
        사실을 사람이 눈으로 맞춰 두는 대신 고정한다. `finus_nat/tests/test_pii_guard.py`가
        NAT 사본에 대해 같은 검사를 한다.
        """
        _, mapping = mask_pii("계좌 12345678-01, 평가금액 1,234원, 삼성전자 3주")
        text = " ".join(mapping)

        assert [m.group(0) for m in _PLACEHOLDER_RE.finditer(text)] == [
            m.group(0) for m in pii_registry._SCOPED_PLACEHOLDER_RE.finditer(text)
        ]

    def test_old_style_placeholders_are_not_restored(self):
        """scope 없는 구형식(`<AMOUNT_1>`)은 매치되지 않으므로 손대지 않는다."""
        _, mapping = mask_pii("300만원 벌었어")
        with active_mapping(mapping):
            assert restore_live_placeholders("<AMOUNT_1>") == ("<AMOUNT_1>", [])


# ---------------------------------------------------------------------------
# 2. create_db_diary 경계
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCreateDiaryRestores:
    async def test_stores_the_original_value_while_the_mapping_is_live(
        self, asgi_client, session
    ):
        masked, mapping = mask_pii("300만원 벌었어")

        with active_mapping(mapping):
            response = await asgi_client.post(
                "/api/v1/db/diary",
                json={"title": "매매일지", "content": f"오늘 {masked}"},
            )

        assert response.status_code == 200
        assert response.json()["data"]["content"] == "오늘 300만원 벌었어"
        stored = session.exec(select(Diary)).one()
        assert stored.content == "오늘 300만원 벌었어"

    async def test_restores_the_title_too(self, asgi_client, session):
        masked, mapping = mask_pii("300만원")

        with active_mapping(mapping):
            response = await asgi_client.post(
                "/api/v1/db/diary", json={"title": f"{masked} 수익", "content": "잘 됐다"}
            )

        assert response.status_code == 200
        assert session.exec(select(Diary)).one().title == "300만원 수익"

    async def test_refuses_when_the_mapping_is_already_gone(self, asgi_client, session):
        """#339의 판정은 그대로다 — 되돌릴 수 없으면 저장하지 않는다."""
        masked, _mapping = mask_pii("300만원 벌었어")

        response = await asgi_client.post(
            "/api/v1/db/diary", json={"title": "매매일지", "content": f"오늘 {masked}"}
        )

        assert response.status_code == 422
        assert "AMOUNT" in response.json()["detail"]
        assert session.exec(select(Diary)).all() == []

    async def test_refuses_an_invented_number_in_a_live_scope(self, asgi_client, session):
        _, mapping = mask_pii("300만원 벌었어")
        invented = f"<AMOUNT_{_scope_of(_first_placeholder(mapping))}_9>"

        with active_mapping(mapping):
            response = await asgi_client.post(
                "/api/v1/db/diary",
                json={"title": "매매일지", "content": f"오늘 {invented} 벌었다"},
            )

        assert response.status_code == 422
        assert session.exec(select(Diary)).all() == []

    async def test_the_rejection_does_not_echo_the_token(self, asgi_client):
        """거부 사유는 NAT을 거쳐 LLM 컨텍스트로 되돌아간다.

        `finus_save_diary`의 `diary_api_http_error`가 이 응답의 `detail`을 그대로 싣기
        때문이다. 토큰을 실으면 에이전트가 답변에 옮겨 적을 여지를 준다 — 지금 막으려는
        것과 같은 종류의 오염이다. 원문은 서버 로그에만 남는다.
        """
        masked, mapping = mask_pii("300만원 벌었어")
        placeholder = _first_placeholder(mapping)

        response = await asgi_client.post(
            "/api/v1/db/diary", json={"title": "매매일지", "content": f"오늘 {masked}"}
        )

        assert response.status_code == 422
        assert placeholder not in response.text

    async def test_a_plain_diary_is_untouched(self, asgi_client, session):
        """자리표시자가 없는 보통 일지는 이 계층을 그대로 지나간다."""
        response = await asgi_client.post(
            "/api/v1/db/diary",
            json={"title": "매매일지", "content": "오늘 3,000,000원 벌었다"},
        )

        assert response.status_code == 200
        assert session.exec(select(Diary)).one().content == "오늘 3,000,000원 벌었다"


# ---------------------------------------------------------------------------
# 3. 실제 배선 — 이슈 본문의 경로
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSaveDuringLlmChat:
    """"300만원 벌었어, 일지 써줘"의 원값이 `Diary.content`에 저장된다 (이슈의 검증 항목).

    provider 스텁이 NAT 역할을 한다 — 마스킹된 발화를 받아, 그것을 그대로 일지 본문에
    실어 **별개의 HTTP 요청**으로 backend에 POST한다. NAT 에이전트가 실제로 하는 일이
    그것이고(`finus_save_diary`), 그 요청이 원래 채팅 요청의 컨텍스트를 물려받지 않는
    것도 그대로다.
    """

    async def test_user_amount_survives_the_round_trip(
        self, asgi_client, session, monkeypatch
    ):
        async def fake_nat(masked_msg, *, conversation_id=None):
            # NAT이 저장 도구를 부르는 지점. 마스킹된 발화가 그대로 본문에 실린다 —
            # LLM은 원값을 본 적이 없다.
            saved = await asgi_client.post(
                "/api/v1/db/diary",
                json={"title": "매매일지 2026-09-03", "content": masked_msg},
            )
            assert saved.status_code == 200
            return "일지를 저장했습니다"

        monkeypatch.setattr(services, "_llm_nat_chat", fake_nat)

        answer = await services.llm_chat("nat", "300만원 벌었어, 일지 써줘")

        assert answer == "일지를 저장했습니다"
        assert session.exec(select(Diary)).one().content == "300만원 벌었어, 일지 써줘"

    async def test_two_concurrent_chats_store_their_own_amounts(
        self, asgi_client, session, monkeypatch
    ):
        """겹쳐 도는 두 대화가 서로의 금액을 일지에 남기지 않는다.

        매핑을 전역 dict 하나로 승격하면 이 테스트만 빨간불이 된다 — 나중에 시작한
        요청의 매핑이 앞선 요청 것을 덮어, 한쪽 일지에 다른 쪽 금액이 박힌다.
        """

        async def fake_nat(masked_msg, *, conversation_id=None):
            await asyncio.sleep(0)  # 두 호출이 확실히 겹치게 한다
            saved = await asgi_client.post(
                "/api/v1/db/diary",
                json={"title": f"일지 {conversation_id}", "content": masked_msg},
            )
            assert saved.status_code == 200
            return "저장 완료"

        monkeypatch.setattr(services, "_llm_nat_chat", fake_nat)

        await asyncio.gather(
            services.llm_chat("nat", "300만원 벌었어", conversation_id="a"),
            services.llm_chat("nat", "잔고가 12,345,000원이야", conversation_id="b"),
        )

        stored = {d.title: d.content for d in session.exec(select(Diary)).all()}
        assert stored == {
            "일지 a": "300만원 벌었어",
            "일지 b": "잔고가 12,345,000원이야",
        }

    async def test_a_save_arriving_after_the_chat_finished_is_refused(
        self, asgi_client, session, monkeypatch
    ):
        """왕복이 끝난 뒤 도착한 저장은 거부된다 — 수명이 요청 범위라는 뜻이다.

        늦게 도착한 요청을 위해 매핑을 더 오래 살려 두는 것은 일부러 하지 않았다.
        매핑은 사용자 발화의 평문이라, 사는 시간이 길어질수록 프로세스 메모리에 평문이
        남는 창이 넓어진다.
        """
        leaked: dict[str, str] = {}

        async def fake_nat(masked_msg, *, conversation_id=None):
            leaked["masked"] = masked_msg
            return "확인했습니다"

        monkeypatch.setattr(services, "_llm_nat_chat", fake_nat)
        await services.llm_chat("nat", "300만원 벌었어")

        response = await asgi_client.post(
            "/api/v1/db/diary",
            json={"title": "뒤늦은 저장", "content": leaked["masked"]},
        )

        assert response.status_code == 422
        assert session.exec(select(Diary)).all() == []


def test_placeholder_regex_is_anchored_to_the_documented_format():
    """등록소 조회 키가 scope라는 사실을 형식 차원에서 못박는다.

    scope 자리가 6자리 hex가 아니게 되면(예: 길이 변경) 이 정규식이 조용히 아무것도
    매치하지 않고, 증상은 "저장이 전부 422"로만 드러난다.
    """
    assert pii_registry._SCOPED_PLACEHOLDER_RE.fullmatch("<AMOUNT_9f2a1c_1>")
    assert not pii_registry._SCOPED_PLACEHOLDER_RE.fullmatch("<AMOUNT_9f2a1_1>")
    assert not pii_registry._SCOPED_PLACEHOLDER_RE.fullmatch("<PRICE_9f2a1c_1>")
    assert pii_registry._scope_of("<QTY_9f2a1c_3>") == "9f2a1c"
    assert pii_registry._scope_of("<QTY_1>") is None
    assert isinstance(pii_registry._SCOPED_PLACEHOLDER_RE, re.Pattern)
