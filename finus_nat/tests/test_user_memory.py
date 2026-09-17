"""사용자 선호 메모리(mem0 로컬 모드)와 추천 성향 주입 (#397).

세 가지 보장을 고정한다.

1. **로컬** — mem0 쓰기·조회·삭제가 루프백 밖으로 연결하지 않고, 텔레메트리·임베더·추출 LLM이
   외부로 나갈 수 없는 구현으로 바뀌어 있다.
2. **허용목록** — 저장되는 것은 ``risk_profile`` 열거형 값뿐이다. 계좌·금액 같은 값은 mem0 ``add``에
   닿기 전에 거부된다.
3. **회귀 없음** — 성향이 없으면 추천 브랜치에 넘어가는 요청이 입력과 **같은 객체**다.
"""
from __future__ import annotations

import socket
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from nat.data_models.api_server import ChatRequest, Message, UserMessageContentRoleType
from nat.memory.models import MemoryItem
from nat_finus_nat import user_memory
from nat_finus_nat.user_memory import (
    DisallowedMemoryWrite,
    FinusPreferenceMemoryEditor,
    FinusRiskProfileBranchConfig,
    UserPreferencesRequest,
    UserPreferencesResponse,
    finus_risk_profile_branch,
    finus_user_preferences_disabled,
    with_risk_profile,
)

USER = "telegram:4242"


@contextmanager
def _no_external_network():
    """루프백 외 연결을 실패시킨다. Windows 이벤트 루프는 루프백 소켓쌍을 쓰므로 루프백은 허용한다."""
    original = socket.socket.connect
    attempts: list[object] = []

    def guarded(self, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1", "localhost"):
            return original(self, address)
        attempts.append(address)
        raise AssertionError(f"외부 연결 시도: {address}")

    socket.socket.connect = guarded
    try:
        yield attempts
    finally:
        socket.socket.connect = original


@pytest.fixture
def local_editor(tmp_path):
    from nat_finus_nat.mem0_local import build_local_async_memory

    memory = build_local_async_memory(tmp_path / "mem0", "test_preferences")
    yield FinusPreferenceMemoryEditor(memory), memory
    memory.vector_store.client.close()
    memory.db.close()


# ---------------------------------------------------------------------------
# 1. 로컬
# ---------------------------------------------------------------------------


async def test_local_memory_round_trip_without_external_network(local_editor):
    editor, memory = local_editor
    with _no_external_network() as attempts:
        await editor.set_preference(USER, "risk_profile", "conservative")
        assert await editor.get_preferences(USER) == {"risk_profile": "conservative"}

        await editor.set_preference(USER, "risk_profile", "aggressive")
        assert await editor.get_preferences(USER) == {"risk_profile": "aggressive"}
        # 같은 키를 다시 쓰면 옛 레코드는 지운다 — 값이 쌓이지 않는다.
        assert len((await memory.get_all(user_id=USER))["results"]) == 1

        assert await editor.get_preferences("telegram:other") == {}

        await editor.clear_preference(USER, "risk_profile")
        assert await editor.get_preferences(USER) == {}
    assert attempts == []


async def test_cleared_value_is_purged_from_mem0_history(local_editor):
    """mem0 삭제는 이력 테이블에 옛 값을 남긴다. 사용자가 해제한 값은 이력에도 남지 않아야 한다.

    뮤테이션: ``_delete_records``에서 ``_purge_history`` 호출을 빼면 red.
    """
    editor, memory = local_editor
    await editor.set_preference(USER, "risk_profile", "aggressive")
    await editor.clear_preference(USER, "risk_profile")
    rows = memory.db.connection.execute("SELECT old_memory, new_memory FROM history").fetchall()
    assert all("aggressive" not in str(value) for row in rows for value in row)


def test_history_purge_fails_loudly_when_mem0_internals_move():
    """이력 삭제가 기대는 mem0 내부 속성이 없으면 조용히 건너뛰지 않고 예외로 드러난다 (PR #403 리뷰).

    뮤테이션: ``self._memory.db``를 ``getattr(self._memory, "db", None)`` + None이면 return으로 되돌리면 red.
    """
    editor = FinusPreferenceMemoryEditor(SimpleNamespace())
    with pytest.raises(AttributeError):
        editor._purge_history(["memory-id"])


def test_second_process_on_the_same_storage_gets_actionable_error(tmp_path):
    """qdrant 로컬 잠금에 걸리면 해결 방법(저장 경로 분리·메모리 끄기)을 담은 오류가 난다 (PR #403 리뷰).

    같은 프로세스 안에서도 qdrant 로컬 잠금이 걸려 두 번째 기동을 재현할 수 있다.
    뮤테이션: ``build_local_async_memory``의 ``except RuntimeError`` 안내를 지우면 red(qdrant 원문만 남는다).
    """
    from nat_finus_nat.mem0_local import build_local_async_memory

    first = build_local_async_memory(tmp_path / "mem0", "shared")
    try:
        with pytest.raises(RuntimeError) as excinfo:
            build_local_async_memory(tmp_path / "mem0", "shared")
        message = str(excinfo.value)
        assert "FINUS_MEM0_STORAGE_DIR" in message
        assert "FINUS_MEM0_ENABLED=0" in message
        assert "already accessed" in str(excinfo.value.__cause__)
    finally:
        first.vector_store.client.close()
        first.db.close()


def test_mem0_telemetry_embedder_and_llm_cannot_leave_the_process(local_editor):
    """외부로 나갈 수 있는 mem0 경로 셋이 코드로 막혀 있다.

    뮤테이션: ``silence_mem0_telemetry``에서 ``memory_main.capture_event`` 교체를 빼면 red.
    """
    import mem0.client.main as client_main
    import mem0.memory.main as memory_main
    import mem0.memory.telemetry as telemetry
    from nat_finus_nat.mem0_local import LocalHashEmbedding, RefusingLlm, _no_telemetry

    _, memory = local_editor
    assert memory_main.capture_event is _no_telemetry
    assert client_main.capture_client_event is _no_telemetry
    assert telemetry.MEM0_TELEMETRY is False
    assert telemetry.client_telemetry.posthog.disabled is True

    assert isinstance(memory.embedding_model, LocalHashEmbedding)
    assert isinstance(memory.llm, RefusingLlm)
    with pytest.raises(RuntimeError):
        memory.llm.generate_response(messages=[{"role": "user", "content": "x"}])
    assert memory.vector_store.is_local is True


def test_local_hash_embedding_is_deterministic_and_normalized():
    from nat_finus_nat.mem0_local import EMBEDDING_DIMS, LocalHashEmbedding

    embedder = LocalHashEmbedding()
    first = embedder.embed("risk_profile=conservative")
    assert first == embedder.embed("risk_profile=conservative")
    assert len(first) == EMBEDDING_DIMS
    assert abs(sum(v * v for v in first) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# 2. 허용목록
# ---------------------------------------------------------------------------


async def test_persisted_payloads_hold_only_the_allowlisted_preference(local_editor):
    """저장소에 실제로 남은 레코드를 읽어, 선호 키·값 외의 내용이 없는지 본다."""
    editor, memory = local_editor
    await editor.set_preference(USER, "risk_profile", "conservative")

    points, _ = memory.vector_store.list(filters={"user_id": USER})
    assert len(points) == 1
    payload = points[0].payload
    assert payload["data"] == "risk_profile=conservative"
    assert payload["pref_key"] == "risk_profile"
    assert payload["pref_value"] == "conservative"
    assert set(payload) <= {"data", "hash", "created_at", "user_id", "role", "pref_key", "pref_value"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("risk_profile", "계좌 12345678-01 잔고 5,000,000원"),
        ("risk_profile", "5000000"),
        ("risk_profile", "moderate"),
        ("account_number", "12345678-01"),
        ("balance", "5000000"),
    ],
)
async def test_disallowed_writes_never_reach_mem0(key, value):
    """허용목록 밖의 키·값은 mem0 ``add``가 불리기 전에 거부된다.

    뮤테이션: ``_write``에서 ``validate_preference`` 호출을 빼면 red(``add``가 불린다).
    """
    memory = MagicMock()
    memory.add = AsyncMock()
    memory.get_all = AsyncMock(return_value={"results": []})
    editor = FinusPreferenceMemoryEditor(memory)

    with pytest.raises(DisallowedMemoryWrite):
        await editor._write(USER, key, value)
    with pytest.raises(DisallowedMemoryWrite):
        await editor.set_preference(USER, key, value)
    memory.add.assert_not_called()


async def test_add_items_rejects_conversation_text():
    """NAT MemoryEditor 경로로 대화 본문을 넣어도 저장되지 않는다(auto_memory_agent가 하던 쓰기)."""
    memory = MagicMock()
    memory.add = AsyncMock()
    memory.get_all = AsyncMock(return_value={"results": []})
    editor = FinusPreferenceMemoryEditor(memory)

    item = MemoryItem(
        conversation=[{"role": "assistant", "content": "예수금 5,000,000원, 삼성전자 100주 보유"}],
        user_id=USER,
        memory="",
        metadata={},
    )
    with pytest.raises(DisallowedMemoryWrite):
        await editor.add_items([item])
    memory.add.assert_not_called()


async def test_invalid_user_id_is_rejected_before_mem0():
    memory = MagicMock()
    memory.add = AsyncMock()
    editor = FinusPreferenceMemoryEditor(memory)
    with pytest.raises(DisallowedMemoryWrite):
        await editor._write("telegram:1 OR 1=1", "risk_profile", "conservative")
    memory.add.assert_not_called()


def test_endpoint_request_accepts_only_the_enum_and_known_fields():
    UserPreferencesRequest(user_id=USER, action="set", risk_profile="aggressive")
    with pytest.raises(ValidationError):
        UserPreferencesRequest(user_id=USER, action="set", risk_profile="moderate")
    with pytest.raises(ValidationError):
        UserPreferencesRequest(user_id=USER, action="set", risk_profile="conservative", balance="5000000")
    with pytest.raises(ValidationError):
        UserPreferencesRequest(user_id="bad id", action="get")


async def test_disabled_preferences_report_disabled():
    async with finus_user_preferences_disabled(MagicMock(), MagicMock()) as info:
        response = await info.single_fn(UserPreferencesRequest(user_id=USER, action="set", risk_profile="aggressive"))
    assert response == UserPreferencesResponse(enabled=False)


# ---------------------------------------------------------------------------
# 3. 추천 브랜치 주입
# ---------------------------------------------------------------------------

_GUIDANCE = {
    "conservative_guidance": "변동성 경고를 먼저 적으세요.",
    "aggressive_guidance": "기회 요인을 먼저 적으세요.",
}


def _branch_config() -> FinusRiskProfileBranchConfig:
    return FinusRiskProfileBranchConfig(
        inner_function_name="recommend_gated_branch_agent",
        preferences_function_name="user_preferences",
        **_GUIDANCE,
    )


def _builder(inner, preferences):
    builder = MagicMock()
    builder.get_function = AsyncMock(
        side_effect=lambda name: {"recommend_gated_branch_agent": inner, "user_preferences": preferences}[str(name)]
    )
    return builder


def _request(text: str = "반도체 종목 하나 추천해줘") -> ChatRequest:
    return ChatRequest(
        messages=[
            Message(role=UserMessageContentRoleType.USER, content="이전 질문"),
            Message(role=UserMessageContentRoleType.ASSISTANT, content="이전 답변"),
            Message(role=UserMessageContentRoleType.USER, content=text),
        ]
    )


def _preferences_returning(profile: str | None, *, enabled: bool = True) -> MagicMock:
    fn = MagicMock()
    fn.ainvoke = AsyncMock(return_value=UserPreferencesResponse(enabled=enabled, risk_profile=profile))
    return fn


def _inner_recording() -> MagicMock:
    fn = MagicMock()
    fn.ainvoke = AsyncMock(return_value="추천 답변")
    return fn


def _with_user_header(monkeypatch, value: str | None):
    headers = {} if value is None else {"x-user-id": value}
    monkeypatch.setattr(user_memory, "Context", SimpleNamespace(get=lambda: SimpleNamespace(metadata=SimpleNamespace(headers=headers))))


@pytest.mark.parametrize(
    ("header", "preferences"),
    [
        (USER, _preferences_returning(None)),  # 미설정
        (USER, _preferences_returning(None, enabled=False)),  # 메모리 꺼짐
        (None, _preferences_returning("aggressive")),  # 헤더 없음(스케줄러·API 경로)
    ],
    ids=["unset", "memory-disabled", "no-user-header"],
)
async def test_without_profile_the_request_passes_through_untouched(monkeypatch, header, preferences):
    """성향이 없으면 안쪽 브랜치가 받는 것은 입력과 **같은 객체**다 — 미설정 사용자 회귀 없음.

    뮤테이션: ``run_branch``에서 ``profile is None`` 분기를 빼고 항상 변환·복사하면 identity 검사에서 red.
    """
    _with_user_header(monkeypatch, header)
    inner = _inner_recording()
    request = _request()
    async with finus_risk_profile_branch(_branch_config(), _builder(inner, preferences)) as info:
        assert await info.single_fn(request) == "추천 답변"
    passed = inner.ainvoke.await_args.args[0]
    assert passed is request
    if header is None:
        preferences.ainvoke.assert_not_called()


async def test_preference_read_failure_falls_back_to_untouched_request(monkeypatch):
    _with_user_header(monkeypatch, USER)
    preferences = MagicMock()
    preferences.ainvoke = AsyncMock(side_effect=RuntimeError("qdrant lock"))
    inner = _inner_recording()
    request = _request()
    async with finus_risk_profile_branch(_branch_config(), _builder(inner, preferences)) as info:
        await info.single_fn(request)
    assert inner.ainvoke.await_args.args[0] is request


@pytest.mark.parametrize(
    ("profile", "label", "guidance", "other_guidance"),
    [
        ("conservative", "안정형", _GUIDANCE["conservative_guidance"], _GUIDANCE["aggressive_guidance"]),
        ("aggressive", "공격형", _GUIDANCE["aggressive_guidance"], _GUIDANCE["conservative_guidance"]),
    ],
)
async def test_stored_profile_is_injected_before_the_current_request(monkeypatch, profile, label, guidance, other_guidance):
    """성향 값과 그 성향의 지시문만 현재 요청 앞에 붙는다. 이전 메시지와 원본 요청 객체는 그대로다."""
    _with_user_header(monkeypatch, USER)
    inner = _inner_recording()
    request = _request()
    async with finus_risk_profile_branch(_branch_config(), _builder(inner, _preferences_returning(profile))) as info:
        await info.single_fn(request)

    passed: ChatRequest = inner.ainvoke.await_args.args[0]
    latest = passed.messages[-1].content
    assert latest.startswith(f"[사용자 투자 성향: {label}]")
    assert guidance in latest
    assert other_guidance not in latest
    assert latest.endswith("\n\n반도체 종목 하나 추천해줘")
    assert [m.content for m in passed.messages[:-1]] == ["이전 질문", "이전 답변"]
    assert request.messages[-1].content == "반도체 종목 하나 추천해줘"


def test_with_risk_profile_without_user_message_returns_request_unchanged():
    request = ChatRequest(messages=[Message(role=UserMessageContentRoleType.SYSTEM, content="시스템")])
    assert with_risk_profile(request, "[블록]") is request


def test_guidance_with_digits_is_rejected():
    """지시문의 숫자는 도구 강제 게이트가 근거 있는 수치로 취급하므로 설정 단계에서 막는다."""
    with pytest.raises(ValidationError):
        FinusRiskProfileBranchConfig(
            inner_function_name="a",
            preferences_function_name="b",
            conservative_guidance="변동성 20% 이상은 경고",
            aggressive_guidance="기회 요인 먼저",
        )


def test_request_user_id_rejects_malformed_header(monkeypatch):
    _with_user_header(monkeypatch, "telegram:1\nx")
    assert user_memory.request_user_id() is None
    _with_user_header(monkeypatch, " telegram:1 ")
    assert user_memory.request_user_id() == "telegram:1"
