"""#260: 추론 메타데이터(routed_agent / tools_used) 동작 고정 테스트.

핵심 계약 세 가지를 고정한다.

1. 두 필드는 **코드 기록**에서만 만들어진다 — supervisor의 분기 선택 결과와 도구 강제
   원장(:class:`DataToolLedger`). 모델 출력 텍스트를 파싱하지 않는다 (#129와 같은 원칙).
2. 기존 응답 필드(``choices``/``usage``/``model`` 등)는 불변이다 — 추가만 한다.
3. 원장이 비면 ``tools_used``는 빈 목록이다.

실 KIS·OpenAI 연결 없이 동작한다.
"""

import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from nat.data_models.api_server import (
    ChatRequest,
    ChatRequestOrMessage,
    ChatResponse,
    Message,
    Usage,
    UserMessageContentRoleType,
)

from nat_finus_nat.agents import (
    FinusSqliteTranscriptAgentConfig,
    _run_with_gate,
    _trace_route,
    finus_sqlite_transcript_agent,
    with_reasoning_trace,
)
from nat_finus_nat.finus_api import (
    DATA_TOOL_LEDGER,
    REASONING_TRACE,
    DataToolLedger,
    ReasoningTrace,
    ToolUse,
    _record_to_ledger,
)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _simple_req(text: str = "삼성전자 뉴스 알려줘") -> ChatRequest:
    return ChatRequest(
        messages=[Message(role=UserMessageContentRoleType.USER, content=text)]
    )


def _inner_calling_tools(*tool_names: str, answer: str = "정리했습니다.") -> MagicMock:
    """호출될 때마다 *tool_names*를 순서대로 원장에 기록하는 가짜 내부 에이전트."""

    async def ainvoke(_request):
        for name in tool_names:
            _record_to_ledger(name, "조회 결과 있음")
        return ChatResponse.from_string(answer, usage=Usage())

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=ainvoke)
    return inner


# ---------------------------------------------------------------------------
# 원장 → tools_used
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_records_executed_tools_into_trace_in_call_order():
    """게이트를 통과한 호출에서 실행된 도구가 호출 순서대로 기록된다."""
    trace = ReasoningTrace()
    token = REASONING_TRACE.set(trace)
    try:
        await _run_with_gate(
            inner=_inner_calling_tools("finus_account_balance", "finus_market_news"),
            query="삼성전자 뉴스 알려줘",
            chat_request=_simple_req(),
            inner_name="news_agent",
        )
    finally:
        REASONING_TRACE.reset(token)

    assert trace.tools_used == [
        ToolUse("finus_account_balance", ok=True),
        ToolUse("finus_market_news", ok=True),
    ]


@pytest.mark.asyncio
async def test_trace_deduplicates_tools_across_gate_retry():
    """게이트 재시도로 같은 도구가 다시 실행돼도 tools_used에는 한 번만 남는다.

    재시도는 원장을 새로 만들기 때문에 병합이 두 번 일어난다. 중복 제거가 없으면
    각주에 "뉴스 검색, 뉴스 검색"처럼 같은 자료가 반복 노출된다.
    """
    call_count = {"n": 0}

    async def ainvoke(_request):
        call_count["n"] += 1
        # 1차: 오류 응답만 기록 → 수치 주장과 함께 게이트 트립 → 재시도
        # 2차: 성공 기록 → 통과
        if call_count["n"] == 1:
            _record_to_ledger("finus_market_news", '{"error": "일시 장애"}')
            return ChatResponse.from_string("잔고는 5,000,000원입니다.", usage=Usage())
        _record_to_ledger("finus_market_news", "뉴스 3건")
        return ChatResponse.from_string("뉴스를 정리했습니다.", usage=Usage())

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=ainvoke)

    trace = ReasoningTrace()
    token = REASONING_TRACE.set(trace)
    try:
        await _run_with_gate(
            inner=inner,
            query="잔고 알려줘",
            chat_request=_simple_req("잔고 알려줘"),
            inner_name="news_agent",
        )
    finally:
        REASONING_TRACE.reset(token)

    assert call_count["n"] == 2, "재시도가 일어나는 시나리오여야 한다"
    # 1차 실패 + 2차 성공 → 한 항목으로 접히고, 결국 데이터를 얻었으므로 ok=True.
    assert trace.tools_used == [ToolUse("finus_market_news", ok=True)]


@pytest.mark.asyncio
async def test_trace_is_empty_when_no_tool_ran():
    """도구를 하나도 호출하지 않으면 tools_used는 빈 목록이다."""
    trace = ReasoningTrace()
    token = REASONING_TRACE.set(trace)
    try:
        await _run_with_gate(
            inner=_inner_calling_tools(answer="일반 지식으로 답합니다."),
            query="투자란 무엇인가?",
            chat_request=_simple_req("투자란 무엇인가?"),
            inner_name="strategy_agent",
        )
    finally:
        REASONING_TRACE.reset(token)

    assert trace.tools_used == []


@pytest.mark.asyncio
async def test_gate_does_not_fail_without_trace_box():
    """추론 기록 박스가 없어도(설치하지 않는 config·CLI 경로) 게이트는 그대로 동작한다."""
    assert REASONING_TRACE.get() is None

    answer = await _run_with_gate(
        inner=_inner_calling_tools("finus_market_news", answer="정리했습니다."),
        query="삼성전자 뉴스 알려줘",
        chat_request=_simple_req(),
        inner_name="news_agent",
    )

    assert answer == "정리했습니다."


def test_failed_tool_is_kept_but_marked_not_ok():
    """실패한 호출도 목록에 남기되 ok=False로 구분한다.

    빼면 시도조차 안 한 것처럼 보이고, ok 없이 이름만 실으면 소비자가 실패한 호출까지
    "확인한 자료"로 표시해 사용자가 답변의 근거를 오독한다.
    """
    ledger = DataToolLedger()
    ledger.record("finus_account_balance", ok=False, produced_rows=False)
    trace = ReasoningTrace()

    trace.record_ledger_tools(ledger)

    assert trace.tools_used == [ToolUse("finus_account_balance", ok=False)]


def test_repeated_failure_stays_not_ok():
    """같은 도구가 계속 실패하면 ok=False를 유지한다."""
    ledger = DataToolLedger()
    ledger.record("finus_account_balance", ok=False, produced_rows=False)
    ledger.record("finus_account_balance", ok=False, produced_rows=False)
    trace = ReasoningTrace()

    trace.record_ledger_tools(ledger)

    assert trace.tools_used == [ToolUse("finus_account_balance", ok=False)]


# ---------------------------------------------------------------------------
# 빈 결과(ok=True, empty=True) — 성공과도 실패와도 다른 세 번째 상태 (PR #263 리뷰)
# ---------------------------------------------------------------------------

def test_empty_result_is_carried_as_its_own_state():
    """ok=True지만 0행인 호출을 데이터를 얻은 호출과 구분해 싣는다 (#209).

    구분하지 않으면 게이트가 only_empty_reads()로 돌려주는 '[조회 결과 없음]' 답변에도
    각주가 "확인한 자료: 뉴스 검색"이라고 적혀, 본문과 각주가 정면으로 충돌한다.
    답변은 그 데이터에 근거하지 않았다 — ok=False에 대해 세운 논리가 그대로 성립한다.
    """
    ledger = DataToolLedger()
    ledger.record("finus_market_news", ok=True, produced_rows=False, empty=True)
    trace = ReasoningTrace()

    trace.record_ledger_tools(ledger)

    assert trace.tools_used == [ToolUse("finus_market_news", ok=True, empty=True)]


def test_a_failed_call_is_never_marked_empty():
    """오류 응답은 결과 집합이 비었는지를 말하지 않는다 — 두 필드가 모순되지 않게 눕힌다."""
    ledger = DataToolLedger()
    ledger.record("finus_market_news", ok=False, produced_rows=False, empty=True)
    trace = ReasoningTrace()

    trace.record_ledger_tools(ledger)

    assert trace.tools_used == [ToolUse("finus_market_news", ok=False, empty=False)]


def test_data_result_upgrades_an_earlier_empty_result():
    """재시도에서 데이터를 얻었으면 결국 얻은 것이다 — 각주도 그렇게 말해야 한다."""
    ledger = DataToolLedger()
    ledger.record("finus_market_news", ok=True, produced_rows=False, empty=True)
    ledger.record("finus_market_news", ok=True, produced_rows=True, empty=False)
    trace = ReasoningTrace()

    trace.record_ledger_tools(ledger)

    assert trace.tools_used == [ToolUse("finus_market_news", ok=True, empty=False)]


def test_empty_result_upgrades_an_earlier_failure():
    """실패 < 빈 결과 — 오류로 끝난 게 아니라 조회에는 성공했다는 정보가 더 정확하다."""
    ledger = DataToolLedger()
    ledger.record("finus_market_news", ok=False, produced_rows=False)
    ledger.record("finus_market_news", ok=True, produced_rows=False, empty=True)
    trace = ReasoningTrace()

    trace.record_ledger_tools(ledger)

    assert trace.tools_used == [ToolUse("finus_market_news", ok=True, empty=True)]


def test_a_later_empty_result_does_not_downgrade_obtained_data():
    """한 번 얻은 데이터는 뒤 호출의 빈 결과·실패로 사라지지 않는다."""
    ledger = DataToolLedger()
    ledger.record("finus_market_news", ok=True, produced_rows=True, empty=False)
    ledger.record("finus_market_news", ok=True, produced_rows=False, empty=True)
    ledger.record("finus_market_news", ok=False, produced_rows=False)
    trace = ReasoningTrace()

    trace.record_ledger_tools(ledger)

    assert trace.tools_used == [ToolUse("finus_market_news", ok=True, empty=False)]


def test_successful_write_tool_is_not_reported_as_empty():
    """쓰기 도구는 produced_rows=False여도 '실제로 저장했다'는 뜻이다 — 빈 결과가 아니다."""
    ledger = DataToolLedger()
    ledger.record("finus_save_diary", ok=True, produced_rows=False, empty=False)
    trace = ReasoningTrace()

    trace.record_ledger_tools(ledger)

    assert trace.tools_used == [ToolUse("finus_save_diary", ok=True, empty=False)]


@pytest.mark.asyncio
async def test_empty_read_path_reports_empty_not_success():
    """게이트의 빈 결과 경로 전체를 통과시켜 상태가 각주까지 살아 오는지 본다.

    only_empty_reads()가 True면 게이트는 재시도 없이 _EMPTY_RESULT_REJECTION을
    돌려준다. 그 답변에 붙는 각주의 근거가 이 tools_used다.
    """

    async def ainvoke(_request):
        # mcp-news가 결과 없음일 때 내는 리터럴 (_EMPTY_RESULT_LITERALS).
        _record_to_ledger("finus_market_news", "'삼성전자'에 대한 뉴스를 찾지 못했습니다.")
        # 수치 주장이 있어야 게이트가 트립한다(_check_tool_enforcement).
        return ChatResponse.from_string("삼성전자는 74,200원입니다.", usage=Usage())

    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=ainvoke)

    trace = ReasoningTrace()
    token = REASONING_TRACE.set(trace)
    try:
        answer = await _run_with_gate(
            inner=inner,
            query="삼성전자 뉴스 알려줘",
            chat_request=_simple_req(),
            inner_name="news_agent",
        )
    finally:
        REASONING_TRACE.reset(token)

    assert answer.startswith("[조회 결과 없음]"), "빈 결과 경로를 타는 시나리오여야 한다"
    assert trace.tools_used == [ToolUse("finus_market_news", ok=True, empty=True)]


# ---------------------------------------------------------------------------
# 라우팅 → routed_agent
# ---------------------------------------------------------------------------

def test_trace_route_records_selected_branch():
    trace = ReasoningTrace()
    token = REASONING_TRACE.set(trace)
    try:
        _trace_route("news_agent")
    finally:
        REASONING_TRACE.reset(token)

    assert trace.routed_agent == "news_agent"


def test_trace_route_is_noop_without_box():
    """박스가 없으면 조용히 no-op한다 — 각주 부재가 답변 경로를 막지 않는다."""
    assert REASONING_TRACE.get() is None
    _trace_route("news_agent")  # 예외 없이 통과해야 한다


# ---------------------------------------------------------------------------
# 응답 부착 — 기존 필드 불변
# ---------------------------------------------------------------------------

def test_with_reasoning_trace_adds_fields_without_touching_existing_ones():
    original = ChatResponse.from_string("답변 본문", usage=Usage())
    trace = ReasoningTrace(
        routed_agent="news_agent",
        tools_used=[
            ToolUse("finus_market_news", ok=True),
            ToolUse("finus_earnings_report", ok=True, empty=True),
            ToolUse("finus_account_balance", ok=False),
        ],
    )

    updated = with_reasoning_trace(original, trace)
    dumped = updated.model_dump()

    assert dumped["routed_agent"] == "news_agent"
    assert dumped["tools_used"] == [
        {"name": "finus_market_news", "ok": True, "empty": False},
        {"name": "finus_earnings_report", "ok": True, "empty": True},
        {"name": "finus_account_balance", "ok": False, "empty": False},
    ]
    # 기존 필드 전부 불변 — 백엔드 파서와 scheduler가 이 필드들만 읽는다.
    for field_name in ("id", "object", "model", "created", "choices", "usage"):
        assert dumped[field_name] == original.model_dump()[field_name]
    assert updated.choices[0].message.content == "답변 본문"


def test_with_reasoning_trace_emits_empty_tools_when_ledger_was_empty():
    """라우팅은 됐는데 도구가 하나도 실행되지 않은 경우는 빈 목록으로 드러낸다."""
    response = ChatResponse.from_string("답변", usage=Usage())
    trace = ReasoningTrace(routed_agent="strategy_agent")

    dumped = with_reasoning_trace(response, trace).model_dump()

    assert dumped["routed_agent"] == "strategy_agent"
    assert dumped["tools_used"] == []


def test_with_reasoning_trace_adds_nothing_when_nothing_observed():
    """관측된 것이 전혀 없으면 필드를 붙이지 않는다 — 백엔드는 각주를 생략한다."""
    response = ChatResponse.from_string("답변", usage=Usage())

    dumped = with_reasoning_trace(response, ReasoningTrace()).model_dump()

    assert "routed_agent" not in dumped
    assert "tools_used" not in dumped


def test_extra_fields_survive_the_fastapi_response_model_boundary():
    """두 필드가 실제 HTTP 응답 본문까지 나가는지 고정한다 (PR #263 리뷰 🔵6).

    model_dump()가 필드를 담는다고 HTTP 응답에 나간다는 보장은 없다. FastAPI는
    response_model로 반환값을 한 번 더 검증·직렬화하는데, 그 경로에서 extra 필드가
    떨어지면 backend는 필드가 없는 것으로 보고 각주를 **조용히** 생략한다 — 양 끝
    (model_dump / backend 파싱)만 덮여 있고 그 사이가 비어 있던 구간이다.

    response_model은 vendor 라우트와 같은 형태를 쓴다:
      nat/front_ends/fastapi/routes/v1_chat_completions.py:158
        response_model=ChatResponse | ChatResponseChunk
    vendor가 이 시그니처를 바꾸면 이 테스트는 더 이상 그 경계를 대변하지 않는다.
    """
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from nat.data_models.api_server import ChatResponseChunk

    tagged = with_reasoning_trace(
        ChatResponse.from_string("답변 본문", usage=Usage()),
        ReasoningTrace(
            routed_agent="news_agent",
            tools_used=[ToolUse("finus_market_news", ok=True, empty=True)],
        ),
    )

    app = fastapi.FastAPI()

    @app.post("/v1/chat/completions", response_model=ChatResponse | ChatResponseChunk)
    async def _endpoint():
        return tagged

    body = TestClient(app).post("/v1/chat/completions").json()

    assert body["routed_agent"] == "news_agent"
    assert body["tools_used"] == [
        {"name": "finus_market_news", "ok": True, "empty": True}
    ]
    # 기존 필드도 그대로 나간다 — backend의 _nat_message_from_payload가 이걸 읽는다.
    assert body["choices"][0]["message"]["content"] == "답변 본문"


# ---------------------------------------------------------------------------
# workflow 최상단 통합 — 실제 응답에 실리는지
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _transcript_agent_fn(tmp_path, inner_response_fn):
    """finus_sqlite_transcript_agent를 만들어 내부 호출 함수를 넘긴다."""
    inner = MagicMock()
    inner.ainvoke = AsyncMock(side_effect=inner_response_fn)

    builder = MagicMock()
    builder.get_function = AsyncMock(return_value=inner)

    config = FinusSqliteTranscriptAgentConfig(
        inner_agent_name="router_supervisor_agent",
        db_path=str(tmp_path / "conversations.sqlite3"),
    )
    async with finus_sqlite_transcript_agent(config, builder) as function_info:
        yield function_info.single_fn


@pytest.mark.asyncio
async def test_transcript_agent_attaches_trace_to_response(tmp_path):
    """최상단이 심은 박스에 안쪽이 기록하고, 그 결과가 응답에 실린다."""

    async def inner_response(_request):
        # supervisor와 브랜치가 하는 일을 그대로 흉내낸다 — 박스를 mutate한다.
        _trace_route("news_agent")
        ledger = DataToolLedger()
        ledger_token = DATA_TOOL_LEDGER.set(ledger)
        try:
            _record_to_ledger("finus_market_news", "뉴스 3건")
        finally:
            DATA_TOOL_LEDGER.reset(ledger_token)
        REASONING_TRACE.get().record_ledger_tools(ledger)
        return ChatResponse.from_string("삼성전자 뉴스입니다.", usage=Usage())

    async with _transcript_agent_fn(tmp_path, inner_response) as response_fn:
        result = await response_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "삼성전자 뉴스"}])
        )
    dumped = result.model_dump()

    assert dumped["routed_agent"] == "news_agent"
    assert dumped["tools_used"] == [{"name": "finus_market_news", "ok": True, "empty": False}]
    assert dumped["choices"][0]["message"]["content"] == "삼성전자 뉴스입니다."


@pytest.mark.asyncio
async def test_transcript_agent_does_not_leak_trace_between_requests(tmp_path):
    """앞선 요청의 라우팅·도구가 다음 요청의 각주로 새지 않는다."""
    routes = ["news_agent", "trading_agent"]

    async def inner_response(_request):
        _trace_route(routes.pop(0))
        return ChatResponse.from_string("답변", usage=Usage())

    async with _transcript_agent_fn(tmp_path, inner_response) as response_fn:
        first = await response_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "뉴스"}])
        )
        second = await response_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "잔고"}])
        )

    assert first.model_dump()["routed_agent"] == "news_agent"
    assert second.model_dump()["routed_agent"] == "trading_agent"
    assert second.model_dump()["tools_used"] == []
