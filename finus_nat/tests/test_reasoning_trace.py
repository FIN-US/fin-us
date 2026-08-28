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
from typing import TypedDict
from unittest.mock import AsyncMock, MagicMock

from nat.data_models.api_server import (
    ChatRequest,
    ChatRequestOrMessage,
    ChatResponse,
    Message,
    Usage,
    UserMessageContentRoleType,
)

from nat.utils.type_converter import GlobalTypeConverter

from nat_finus_nat.agents import (
    MEMORY_PROMPT_PREFIX,
    FinusReasoningTraceAgentConfig,
    FinusSqliteTranscriptAgentConfig,
    _run_with_gate,
    _trace_branch_answer,
    _trace_route,
    chat_response_plain_text,
    finus_reasoning_trace_agent,
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
        branch_answer="답변 본문",
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
    trace = ReasoningTrace(routed_agent="strategy_agent", branch_answer="답변")

    dumped = with_reasoning_trace(response, trace).model_dump()

    assert dumped["routed_agent"] == "strategy_agent"
    assert dumped["tools_used"] == []


def test_with_reasoning_trace_adds_nothing_when_nothing_observed():
    """관측된 것이 전혀 없으면 필드를 붙이지 않는다 — 백엔드는 각주를 생략한다."""
    response = ChatResponse.from_string("답변", usage=Usage())

    dumped = with_reasoning_trace(response, ReasoningTrace()).model_dump()

    assert "routed_agent" not in dumped
    assert "tools_used" not in dumped


def test_with_reasoning_trace_adds_nothing_when_body_is_not_the_branch_answer():
    """본문이 브랜치가 만든 답변이 아니면 붙이지 않는다 (#294).

    박스가 가득 차 있어도 마찬가지다 — vendor가 예외를 삼키고 오류 문자열을 답변으로
    돌려주는 경로가 정확히 이 모습이다.
    """
    trace = ReasoningTrace(
        routed_agent="news_agent",
        tools_used=[ToolUse("finus_market_news", ok=True)],
        branch_answer="삼성전자 뉴스입니다.",
    )
    error_body = ChatResponse.from_string("Connection refused: mem0", usage=Usage())

    dumped = with_reasoning_trace(error_body, trace).model_dump()

    assert "routed_agent" not in dumped
    assert "tools_used" not in dumped
    assert dumped["choices"][0]["message"]["content"] == "Connection refused: mem0"


def test_with_reasoning_trace_adds_nothing_when_no_branch_answer_was_recorded():
    """브랜치 답변 기록 자체가 없으면 붙이지 않는다 (#294).

    브랜치가 예외로 죽으면 supervisor의 기록 지점에 도달하지 못한 채 ``routed_agent``만
    남는다. "기록이 있다"가 아니라 "이 본문이 그 기록의 결과다"가 부착 조건이다.
    """
    trace = ReasoningTrace(routed_agent="news_agent")

    dumped = with_reasoning_trace(
        ChatResponse.from_string("boom", usage=Usage()), trace
    ).model_dump()

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
            branch_answer="답변 본문",
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
#
# 각주는 최상위 finus_reasoning_trace_agent 한 곳에서만 붙는다(#273). 아래 두
# 헬퍼는 두 라우터 config의 체인을 그대로 재현한다 — 부착 지점이 같으므로 같은
# 단언이 양쪽에 걸린다.
# ---------------------------------------------------------------------------

def _branch_recording(route: str, *tool_names: str, answer: str = "답변"):
    """supervisor + 브랜치가 하는 일을 흉내내는 가짜 내부 에이전트 함수.

    최상단이 심은 박스를 mutate하기만 한다 — 실제 _trace_route/_trace_ledger_tools와 같다.
    """

    async def inner_response(_request):
        _trace_route(route)
        ledger = DataToolLedger()
        ledger_token = DATA_TOOL_LEDGER.set(ledger)
        try:
            for name in tool_names:
                _record_to_ledger(name, "뉴스 3건")
        finally:
            DATA_TOOL_LEDGER.reset(ledger_token)
        trace = REASONING_TRACE.get()
        assert trace is not None, "최상단이 박스를 심어 안쪽까지 전파돼야 한다"
        trace.record_ledger_tools(ledger)
        # 실제 supervisor와 같이 돌려보내는 본문을 함께 기록한다 (#294).
        _trace_branch_answer(answer)
        return ChatResponse.from_string(answer, usage=Usage())

    return inner_response


def _as_function(response_fn) -> MagicMock:
    fn = MagicMock()
    fn.ainvoke = AsyncMock(side_effect=response_fn)
    return fn


def _builder_returning(function_mock: MagicMock) -> MagicMock:
    builder = MagicMock()
    builder.get_function = AsyncMock(return_value=function_mock)
    return builder


@asynccontextmanager
async def _nomemory_chain(tmp_path, inner_response_fn):
    """router_nomemory.yml 체인: trace_agent → transcript_agent → 브랜치."""
    transcript_config = FinusSqliteTranscriptAgentConfig(
        inner_agent_name="router_supervisor_agent",
        db_path=str(tmp_path / "conversations.sqlite3"),
    )
    async with finus_sqlite_transcript_agent(
        transcript_config, _builder_returning(_as_function(inner_response_fn))
    ) as transcript_info:
        transcript_fn = transcript_info.single_fn
        async with finus_reasoning_trace_agent(
            FinusReasoningTraceAgentConfig(inner_agent_name="transcript_router_agent"),
            _builder_returning(_as_function(transcript_fn)),
        ) as trace_info:
            yield trace_info.single_fn


class _VendorState(TypedDict):
    """vendor AutoMemoryWrapperState 자리 — 홉을 재현하는 데 필요한 최소 필드."""

    text: str


def _compiled_vendor_graph(transcript_fn, capture_error: Exception | None = None):
    """vendor가 안쪽을 부르는 방식(CompiledStateGraph.ainvoke)까지 재현한다.

    #273 수정은 두 가정 위에 서 있다.

    1. vendor의 str 경계에서 ChatResponse의 추가 필드가 버려진다 — 그래서 부착
       지점을 그 **바깥**으로 올려야 한다.
    2. 최상단이 심은 박스가 그 경계를 **안쪽으로는** 넘어간다 — 그래서 올려도
       supervisor/브랜치가 같은 박스를 기록할 수 있다.

    transcript_fn을 같은 컨텍스트에서 그냥 await하면 1번만 덮이고 2번은 검증되지
    않는다. LangGraph는 노드를 copy_context()로 감싸 실행하므로, 실제로 건너야
    하는 경계는 이 홉이다. 노드 하나짜리 그래프면 그 경계를 그대로 만든다.
    """
    from langgraph.graph import END, START, StateGraph

    async def inner_node(state: _VendorState) -> _VendorState:
        # vendor `inner_agent_node`는 안쪽을 **문자열이 아니라** `ChatRequest(messages=...)`로
        # 부른다 — 그 앞 `memory_retrieve_node`가 회수한 기억을 `MEMORY_PROMPT_PREFIX`
        # 시스템 메시지로 마지막 user 앞에 끼워 넣기 때문이다. 여기를 `input_message=`로
        # 흉내내면 transcript agent가 production에서 타지 않는 문자열 분기로 들어가고,
        # 기억 시스템 메시지를 걸러내는 경로도 안 밟힌다 (#291 자가리뷰).
        result = await transcript_fn(ChatRequestOrMessage(messages=[
            {"role": "system", "content": f"{MEMORY_PROMPT_PREFIX}\n사용자는 삼성전자를 보유 중이다."},
            {"role": "user", "content": state["text"]},
        ]))
        # vendor는 마지막 메시지의 content(str)만 상태에 남긴다 — 필드 소실 지점.
        return {"text": chat_response_plain_text(result)}

    async def capture_node(state: _VendorState) -> _VendorState:
        """vendor `capture_ai_response_node` 자리 — 브랜치가 **성공한 뒤에** 돈다.

        vendor 그래프 순서가 `inner_agent` → `capture_ai_response`이므로(agent.py:268),
        여기서 `memory_editor.add_items()`가 터지면(Mem0 다운·타임아웃) 박스가 라우팅·
        도구로 가득 찬 상태에서 예외가 `graph.ainvoke`를 뚫고 나간다 (#294).
        """
        if capture_error is not None:
            raise capture_error
        return state

    graph = StateGraph(_VendorState)
    graph.add_node("inner", inner_node)
    graph.add_node("capture", capture_node)
    graph.add_edge(START, "inner")
    graph.add_edge("inner", "capture")
    graph.add_edge("capture", END)
    return graph.compile()


@asynccontextmanager
async def _memory_chain(tmp_path, inner_response_fn, capture_error: Exception | None = None):
    """router.yml 체인: trace_agent → auto_memory_agent(vendor) → transcript_agent → 브랜치.

    가운데 vendor 흉내는 실제 `_response_fn(input_message: str) -> str` 시그니처와
    `graph.ainvoke(state)` 홉을 함께 재현한다 — NAT 타입 변환이 요청을 문자열로
    눕히고, 반환도 문자열이라 안쪽이 ChatResponse에 붙인 추가 필드는 **여기서 전부
    사라진다**. #273의 원인이 바로 이 지점이므로, 흉내를 느슨하게 만들면 회귀
    테스트가 아무것도 지키지 않는다. LangGraph 홉을 넣는 이유는
    :func:`_compiled_vendor_graph` 참고.

    vendor `_response_fn`의 `except Exception: return str(ex)`(register.py:215-218,
    `router.yml`이 `verbose: true`)까지 재현한다 — 실패가 예외가 아니라 **답변 문자열**로
    올라오는 것이 #294의 전제다. *capture_error*를 주면 브랜치가 성공한 뒤 메모리 쓰기가
    터지는 변종이 된다.
    """
    transcript_config = FinusSqliteTranscriptAgentConfig(
        inner_agent_name="router_supervisor_agent",
        db_path=str(tmp_path / "conversations.sqlite3"),
    )
    async with finus_sqlite_transcript_agent(
        transcript_config, _builder_returning(_as_function(inner_response_fn))
    ) as transcript_info:
        vendor_graph = _compiled_vendor_graph(transcript_info.single_fn, capture_error)

        async def vendor_auto_memory(request):
            text = GlobalTypeConverter.get().convert(request, to_type=str)
            try:
                result_state = await vendor_graph.ainvoke({"text": text})
            except Exception as ex:  # noqa: BLE001 — vendor가 삼키는 그 지점
                return str(ex)
            return str(result_state["text"])

        async with finus_reasoning_trace_agent(
            FinusReasoningTraceAgentConfig(inner_agent_name="memory_router_agent"),
            _builder_returning(_as_function(vendor_auto_memory)),
        ) as trace_info:
            yield trace_info.single_fn


_ROUTER_CHAINS = [(_nomemory_chain, "router_nomemory.yml"), (_memory_chain, "router.yml")]
_ROUTER_CHAIN_IDS = [name for _, name in _ROUTER_CHAINS]


@pytest.mark.asyncio
@pytest.mark.parametrize("chain,_config_name", _ROUTER_CHAINS, ids=_ROUTER_CHAIN_IDS)
async def test_workflow_attaches_trace_to_response(chain, _config_name, tmp_path):
    """최상단이 심은 박스에 안쪽이 기록하고, 그 결과가 응답에 실린다.

    router.yml 파라미터가 #273의 회귀 가드다 — 부착 지점이 vendor 안쪽으로 돌아가면
    두 필드가 str 경계에서 버려져 red가 된다.
    """
    async with chain(tmp_path, _branch_recording(
        "news_agent", "finus_market_news", answer="삼성전자 뉴스입니다."
    )) as response_fn:
        result = await response_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "삼성전자 뉴스"}])
        )
    dumped = result.model_dump()

    assert dumped["routed_agent"] == "news_agent"
    assert dumped["tools_used"] == [{"name": "finus_market_news", "ok": True, "empty": False}]
    assert dumped["choices"][0]["message"]["content"] == "삼성전자 뉴스입니다."


@pytest.mark.asyncio
@pytest.mark.parametrize("chain,_config_name", _ROUTER_CHAINS, ids=_ROUTER_CHAIN_IDS)
async def test_workflow_does_not_leak_trace_between_requests(chain, _config_name, tmp_path):
    """앞선 요청의 라우팅·도구가 다음 요청의 각주로 새지 않는다."""
    routes = ["news_agent", "trading_agent"]

    async def inner_response(request):
        return await _branch_recording(routes.pop(0))(request)

    async with chain(tmp_path, inner_response) as response_fn:
        first = await response_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "뉴스"}])
        )
        second = await response_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "잔고"}])
        )

    assert first.model_dump()["routed_agent"] == "news_agent"
    assert second.model_dump()["routed_agent"] == "trading_agent"
    assert second.model_dump()["tools_used"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("chain,_config_name", _ROUTER_CHAINS, ids=_ROUTER_CHAIN_IDS)
async def test_workflow_returns_plain_text_for_string_input(chain, _config_name, tmp_path):
    """`nat run --input` 같은 문자열 경로는 각주를 실을 곳이 없으므로 본문만 돌려준다."""
    async with chain(tmp_path, _branch_recording(
        "news_agent", "finus_market_news", answer="삼성전자 뉴스입니다."
    )) as response_fn:
        result = await response_fn(ChatRequestOrMessage(input_message="삼성전자 뉴스"))

    assert result == "삼성전자 뉴스입니다."


# ---------------------------------------------------------------------------
# vendor가 삼킨 실패에는 각주가 붙지 않는다 (#294)
#
# 두 변종 모두 사용자에게 보이는 모습은 같다 — 본문은 오류 문자열인데 그 아래
# "담당: 뉴스 에이전트"가 정상 답변과 똑같이 달린다. 답변이 근거하지 않은 경로를
# 근거로 제시하는 셈이라, 박스가 얼마나 차 있든 붙이지 않는 것이 계약이다.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_mode_omits_footnote_when_the_branch_raises(tmp_path):
    """변종 1: 브랜치가 예외로 죽는다 — 라우팅만 남고 도구 기록은 비어 있다.

    supervisor는 브랜치를 부르기 **전에** `_trace_route()`를 부르므로, 브랜치가 터져도
    `routed_agent`는 박스에 남는다. vendor가 그 예외를 삼켜 `str(ex)`를 답변으로
    돌려주면 각주가 오류 문자열에 붙는다 — 그 경로를 막는다.
    """
    async def failing_branch(_request):
        _trace_route("news_agent")
        raise RuntimeError("news_agent가 터졌습니다")

    async with _memory_chain(tmp_path, failing_branch) as response_fn:
        result = await response_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "삼성전자 뉴스"}])
        )
    dumped = result.model_dump()

    assert "news_agent가 터졌습니다" in dumped["choices"][0]["message"]["content"]
    assert "routed_agent" not in dumped
    assert "tools_used" not in dumped


@pytest.mark.asyncio
async def test_memory_mode_omits_footnote_when_memory_write_fails_after_success(tmp_path):
    """변종 2: 브랜치는 성공하고, 그 뒤 `capture_ai_response`의 Mem0 쓰기가 터진다.

    변종 1보다 나쁘다 — 브랜치가 실제로 성공했으므로 박스에 `routed_agent`와
    `tools_used`가 **가득 찬** 상태이고, 각주만 보면 정상 답변과 구분되지 않는다.
    "브랜치가 성공했는가"를 기준으로 삼으면 이 변종을 못 막는다는 것이 A안이 아니라
    A'안(본문 일치)을 고른 이유다.
    """
    async with _memory_chain(
        tmp_path,
        _branch_recording("news_agent", "finus_market_news", answer="삼성전자 뉴스입니다."),
        capture_error=RuntimeError("Mem0 연결이 거부되었습니다"),
    ) as response_fn:
        result = await response_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "삼성전자 뉴스"}])
        )
    dumped = result.model_dump()

    body = dumped["choices"][0]["message"]["content"]
    assert "Mem0 연결이 거부되었습니다" in body
    assert body != "삼성전자 뉴스입니다.", "본문이 브랜치 답변이면 이 테스트는 변종을 재현하지 못한 것이다"
    assert "routed_agent" not in dumped
    assert "tools_used" not in dumped


@pytest.mark.asyncio
async def test_transcript_agent_no_longer_owns_the_trace_box(tmp_path):
    """transcript_agent는 박스를 심지도 붙이지도 않는다 (#273).

    겸하면 박스가 두 겹이 되어(안쪽 set이 바깥 박스를 가림) 최상단이 심은 박스는
    빈 채로 남고, router.yml에서는 다시 각주가 조용히 사라진다.

    최상단 역할을 대신해 sentinel 박스를 미리 심고, 안쪽이 **그 박스 그대로**를 보는지
    동일성으로 확인한다. ``is None``으로 확인하면 ContextVar 기본값이 바뀌는 날
    의도와 무관한 이유로 깨진다 (#291 리뷰).
    """
    outer_box = ReasoningTrace()
    seen: list[object] = []

    async def inner_response(_request):
        seen.append(REASONING_TRACE.get())
        return ChatResponse.from_string("답변", usage=Usage())

    transcript_config = FinusSqliteTranscriptAgentConfig(
        inner_agent_name="router_supervisor_agent",
        db_path=str(tmp_path / "conversations.sqlite3"),
    )
    outer_token = REASONING_TRACE.set(outer_box)
    try:
        async with finus_sqlite_transcript_agent(
            transcript_config, _builder_returning(_as_function(inner_response))
        ) as transcript_info:
            result = await transcript_info.single_fn(
                ChatRequestOrMessage(messages=[{"role": "user", "content": "뉴스"}])
            )
    finally:
        REASONING_TRACE.reset(outer_token)

    assert seen and seen[0] is outer_box, "transcript_agent가 박스를 심으면 최상단 박스를 가린다"
    dumped = result.model_dump()
    assert "routed_agent" not in dumped
    assert "tools_used" not in dumped
