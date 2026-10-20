"""#394: 첫 턴 도구 호출 강제 ReAct 에이전트.

대역 LLM은 실측한 gpt-5.4-mini의 행동을 흉내 낸다 — ``tool_choice="required"``가 붙으면
``tool_calls``를 돌려주고, 붙지 않으면 도구 없이 곧바로 ``Final Answer:``를 쓴다. 그래서
벤더 ``ReActAgentGraph``로 같은 입력을 돌리면 도구가 한 번도 불리지 않는다(대조 테스트).
"""

import logging
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import BaseMessage
from langchain_core.messages import HumanMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.outputs import ChatResult
from langchain_core.tools import tool
from pydantic import Field

from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph
from nat.plugins.langchain.agent.react_agent.agent import ReActGraphState
from nat.plugins.langchain.agent.react_agent.agent import create_react_agent_prompt
from nat.plugins.langchain.agent.react_agent.register import ReActAgentWorkflowConfig
from finus_agents.tool_first_react import FinusToolFirstReActAgentConfig
from finus_agents.tool_first_react import ToolFirstReActAgentGraph

NEWS_OBSERVATION = "삼성전자 뉴스 헤드라인 OBS"
DIRECT_REFUSAL = "Thought: I now know the final answer\nFinal Answer: 조회 결과가 없어 답할 수 없습니다."


class _ScriptedChatModel(BaseChatModel):
    """도구가 강제되지 않으면 도구 없이 끝내는 모델."""

    honors_tool_choice: bool = True
    # 강제 호출 중 앞에서 이 횟수만큼은 인자 JSON이 깨진 응답(invalid_tool_calls)을 돌려준다
    invalid_forced_calls: int = 0
    # 그다음 이 횟수만큼은 JSON은 온전하지만 도구가 받지 않는 인자(empty_args)로 도구를 고른다 (#436)
    empty_forced_calls: int = 0
    empty_args: dict[str, Any] = Field(default_factory=dict)
    # 정상 강제 호출의 인자
    forced_args: dict[str, Any] = Field(default_factory=lambda: {"stock_name": "삼성전자"})
    calls: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[t.name for t in tools], **kwargs)

    def forced_calls(self) -> int:
        return sum(1 for call in self.calls if call.get("tool_choice") == "required")

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(kwargs)
        forced = kwargs.get("tool_choice") == "required" and self.honors_tool_choice
        if forced and self.forced_calls() <= self.invalid_forced_calls:
            message = AIMessage(content="", invalid_tool_calls=[
                {"name": kwargs["tools"][0], "args": '{"stock_name": "삼성', "id": "call_0",
                 "error": "Function arguments are not valid JSON", "type": "invalid_tool_call"},
            ])
        elif forced and self.forced_calls() <= self.invalid_forced_calls + self.empty_forced_calls:
            message = AIMessage(content="", tool_calls=[
                {"name": kwargs["tools"][0], "args": dict(self.empty_args), "id": "call_empty"},
            ])
        elif forced:
            message = AIMessage(content="", tool_calls=[
                {"name": kwargs["tools"][0], "args": dict(self.forced_args), "id": "call_1"},
            ])
        elif any(NEWS_OBSERVATION in str(m.content) for m in messages):
            message = AIMessage(content="Thought: I now know the final answer\nFinal Answer: 뉴스 요약")
        else:
            message = AIMessage(content=DIRECT_REFUSAL)
        return ChatResult(generations=[ChatGeneration(message=message)])


def _tools(called: list[str]):
    @tool("mcp-news-get-market-news")
    def news(stock_name: str) -> str:
        """종목 뉴스"""
        called.append(f"news:{stock_name}")
        return NEWS_OBSERVATION

    @tool("get_user_memory")
    def memory(note: str = "") -> str:
        """메모리"""
        called.append("memory")
        return "memory disabled"

    return news, memory


def _prompt(tools):
    return create_react_agent_prompt(ReActAgentWorkflowConfig(llm_name="llm", tool_names=[t.name for t in tools]))


async def _run(graph_cls, llm, tools, **kwargs) -> ReActGraphState:
    graph = await graph_cls(llm=llm, prompt=_prompt(tools), tools=list(tools),
                            raise_on_parsing_failure=False, **kwargs).build_graph()
    state = await graph.ainvoke(ReActGraphState(messages=[HumanMessage(content="삼성전자 최근 뉴스 분석해줘")]),
                                config={"recursion_limit": 12})
    return ReActGraphState(**state)


async def test_vendor_graph_ends_without_any_tool_call():
    """대조군: 대역이 #394 증상을 재현한다는 확인. 이것이 초록이 아니면 아래 테스트는 아무것도 증명하지 않는다."""
    called: list[str] = []
    tools = _tools(called)

    state = await _run(ReActAgentGraph, _ScriptedChatModel(), tools)

    assert called == []
    assert state.final_answer == "조회 결과가 없어 답할 수 없습니다."


async def test_first_turn_forces_a_data_tool_then_answers_from_its_observation():
    """뮤테이션: agent_node 재정의를 지우면(벤더 경로) called==[] 로 red.
    강제 턴을 벤더 ``_call_llm``으로 부르면 tool_calls가 버려져 폴백하므로 역시 red."""
    called: list[str] = []
    news, memory = _tools(called)
    llm = _ScriptedChatModel()

    state = await _run(ToolFirstReActAgentGraph, llm, (news, memory), first_turn_tools=[news])

    assert called == ["news:삼성전자"]
    assert state.final_answer == "뉴스 요약"
    forced, *rest = llm.calls
    assert forced["tool_choice"] == "required"
    assert forced["parallel_tool_calls"] is False
    # 메모리 도구처럼 first_turn_tools에 없는 도구로는 강제 호출을 채울 수 없다
    assert forced["tools"] == ["mcp-news-get-market-news"]
    # Observation을 받은 뒤의 턴은 강제하지 않는다
    assert rest and all("tool_choice" not in call for call in rest)


async def test_falls_back_to_text_react_with_warning_when_provider_ignores_tool_choice(caplog):
    """tool_choice를 무시하는 공급자에서는 막지 않고 벤더 경로로 답하되, 조용히 넘어가지 않는다."""
    called: list[str] = []
    news, memory = _tools(called)

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, _ScriptedChatModel(honors_tool_choice=False),
                           (news, memory), first_turn_tools=[news])

    assert called == []
    assert state.final_answer == "조회 결과가 없어 답할 수 없습니다."
    assert any("도구 호출이 없어" in r.getMessage() for r in caplog.records)


async def test_retries_forced_call_when_tool_arguments_are_invalid_json(caplog):
    """인자 JSON이 깨진 강제 응답(invalid_tool_calls)은 공급자 문제가 아니므로 폴백하지 않고 다시 강제한다.

    뮤테이션: invalid_tool_calls 분기를 지우면(공급자 무시로 취급해 즉시 폴백) called==[] 로 red.
    """
    called: list[str] = []
    news, memory = _tools(called)
    llm = _ScriptedChatModel(invalid_forced_calls=1)

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, llm, (news, memory), first_turn_tools=[news])

    assert called == ["news:삼성전자"]
    assert state.final_answer == "뉴스 요약"
    assert llm.forced_calls() == 2
    messages = [r.getMessage() for r in caplog.records]
    assert any("인자를 파싱하지 못했다" in m and "not valid JSON" in m for m in messages)
    assert not any("도구 호출이 없어" in m for m in messages)


async def test_falls_back_after_repeated_invalid_tool_arguments(caplog):
    """재시도는 상한까지만 한다 — 계속 깨지면 벤더 경로로 답하고 원인을 구분해 남긴다."""
    from finus_agents.tool_first_react import FIRST_TURN_MAX_ATTEMPTS

    called: list[str] = []
    news, memory = _tools(called)
    llm = _ScriptedChatModel(invalid_forced_calls=FIRST_TURN_MAX_ATTEMPTS)

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, llm, (news, memory), first_turn_tools=[news])

    assert called == []
    assert llm.forced_calls() == FIRST_TURN_MAX_ATTEMPTS
    assert state.final_answer == "조회 결과가 없어 답할 수 없습니다."
    assert any("계속 유효하지 않아" in r.getMessage() for r in caplog.records)


async def test_retries_forced_call_when_arguments_fail_tool_schema(caplog):
    """#436: JSON은 온전하지만 도구 스키마가 받지 않는 인자({})로 고른 강제 호출은 실행하지 않고 다시 강제한다.

    뮤테이션: ``agent_node``에서 ``_schema_rejection`` 결과를 무시하면 {} 호출이 그대로 실행돼
    도구 검증 오류만 돌아가고 called==[] 로 red.
    """
    called: list[str] = []
    news, memory = _tools(called)
    llm = _ScriptedChatModel(empty_forced_calls=1)

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, llm, (news, memory), first_turn_tools=[news])

    assert called == ["news:삼성전자"]
    assert state.final_answer == "뉴스 요약"
    assert llm.forced_calls() == 2
    messages = [r.getMessage() for r in caplog.records]
    assert any("도구에 맞지 않는다" in m and "stock_name" in m for m in messages)


async def test_executes_last_schema_rejected_call_instead_of_falling_back(caplog):
    """리뷰 지적 4: 마지막 시도까지 거부되면 폴백(#394: 도구 없이 답함) 대신 그 호출을 실행해
    오류를 Observation으로 남긴다.

    뮤테이션: 마지막 시도에서도 ``continue``하면(폴백) scratchpad가 비어 red.
    """
    from finus_agents.tool_first_react import FIRST_TURN_MAX_ATTEMPTS

    called: list[str] = []
    news, memory = _tools(called)
    llm = _ScriptedChatModel(empty_forced_calls=FIRST_TURN_MAX_ATTEMPTS)

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, llm, (news, memory), first_turn_tools=[news])

    assert llm.forced_calls() == FIRST_TURN_MAX_ATTEMPTS
    assert [(a.tool, a.tool_input) for a in state.agent_scratchpad] == [("mcp-news-get-market-news", "{}")]
    assert called == []  # 도구 검증에서 실패했다 — 실행 결과는 오류 Observation이다
    messages = [r.getMessage() for r in caplog.records]
    assert any("상한이라 그대로 실행한다" in m for m in messages)
    assert not any("계속 유효하지 않아" in m for m in messages)


async def test_schema_rejection_and_invalid_json_share_the_attempt_cap(caplog):
    """리뷰 지적 5(c): 첫 시도는 인자 JSON 깨짐, 두 번째는 스키마 거부 — 상한(2)을 함께 써서
    세 번째 강제 호출은 없고, 마지막 거부 호출은 실행된다.

    뮤테이션: 스키마 거부가 시도 횟수를 따로 세면 forced_calls가 3이 되어 red.
    """
    from finus_agents.tool_first_react import FIRST_TURN_MAX_ATTEMPTS

    called: list[str] = []
    news, memory = _tools(called)
    llm = _ScriptedChatModel(invalid_forced_calls=1, empty_forced_calls=1)

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, llm, (news, memory), first_turn_tools=[news])

    assert FIRST_TURN_MAX_ATTEMPTS == 2
    assert llm.forced_calls() == 2
    assert [a.tool_input for a in state.agent_scratchpad] == ["{}"]
    messages = [r.getMessage() for r in caplog.records]
    assert any("인자를 파싱하지 못했다" in m and "1/2" in m for m in messages)
    assert any("도구에 맞지 않는다" in m and "2/2" in m for m in messages)


async def test_precheck_exception_does_not_block_the_forced_call(caplog):
    """리뷰 지적 3: 사전 검사가 ValidationError 밖의 예외를 내면 막지 않고 실행에 맡긴다."""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel

    class _Input(BaseModel):
        stock_name: str = ""

        def forced_call_rejection(self):
            raise RuntimeError("사전 검사 버그")

    called: list[str] = []

    async def news(**kwargs) -> str:
        called.append(f"news:{kwargs.get('stock_name')}")
        return NEWS_OBSERVATION

    tool_ = StructuredTool.from_function(coroutine=news, name="mcp-news-get-market-news",
                                         description="뉴스", args_schema=_Input)
    _, memory = _tools([])
    llm = _ScriptedChatModel()

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, llm, (tool_, memory), first_turn_tools=[tool_])

    assert called == ["news:삼성전자"]
    assert llm.forced_calls() == 1
    assert state.final_answer == "뉴스 요약"
    assert any("검사 없이 실행한다" in r.getMessage() for r in caplog.records)


# --- KIS 래퍼 실물로 (#436) -------------------------------------------------------------------


@pytest.fixture
def kis_ledger(monkeypatch):
    """실제 조회 전용 KIS 래퍼를 LangChain 도구로 감싸고, 원장을 붙인다. MCP 원격 호출은 대역."""
    from finus_agents import finus_api

    remote_calls: list[dict] = []

    async def fake_remote(**kwargs):
        remote_calls.append(kwargs)
        return '{"output": {"stck_prpr": "216000"}}'

    monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake_remote)
    monkeypatch.setenv("FINUS_SKIP_MCP_LIST_TOOLS", "1")
    ledger = finus_api.DataToolLedger()
    token = finus_api.DATA_TOOL_LEDGER.set(ledger)
    try:
        yield ledger, remote_calls
    finally:
        finus_api.DATA_TOOL_LEDGER.reset(token)


async def _kis_tool():
    """NAT ``langchain_tool_wrapper``와 같이 래퍼 입력 스키마를 args_schema로 쓰는 도구."""
    from langchain_core.tools import StructuredTool

    from finus_agents import finus_api

    config = finus_api.FinusAccountBalanceReadonlyConfig(trading_tool_name="domestic_stock")
    gen = finus_api.finus_account_balance_readonly(config, None)
    info = await gen.__aenter__()

    async def kis(**kwargs) -> str:
        return await info.single_fn(finus_api.KisTradingMcpCallInput(**kwargs))

    return StructuredTool.from_function(coroutine=kis, name="kis-trading-mcp-tool-readonly",
                                        description="KIS", args_schema=finus_api.KisTradingMcpCallInput), gen


@pytest.mark.parametrize("empty_args", [{}, {"tool_input": '{"params": {}}'}], ids=["missing_arguments", "missing_api_type"])
async def test_kis_tool_forced_without_api_type_is_retried_not_executed(kis_ledger, empty_args):
    """#436 실측 경로: 강제 첫 턴이 KIS 조회 도구를 api_type 없이 골랐다. 입력 스키마는 이를 받으므로
    사전 검사(``forced_call_rejection``)만이 가려낼 수 있다. 래퍼의 두 오류 갈래 모두 다시 강제한다.

    뮤테이션: ``KisTradingMcpCallInput.forced_call_rejection``이 늘 None이면 첫 {} 호출이 실행돼
    원장에 ok=False 기록이 생겨 red.
    """
    ledger, remote_calls = kis_ledger
    kis_tool, gen = await _kis_tool()
    _, memory = _tools([])
    llm = _ScriptedChatModel(empty_forced_calls=1, empty_args=empty_args,
                             forced_args={"api_type": "inquire_balance", "params": {"inqr_dvsn": "01"}})
    try:
        await _run(ToolFirstReActAgentGraph, llm, (kis_tool, memory), first_turn_tools=[kis_tool])
    finally:
        await gen.__aexit__(None, None, None)

    assert llm.forced_calls() == 2
    assert [c["arguments"]["api_type"] for c in remote_calls] == ["inquire_balance"]
    assert [(r.tool_name, r.ok) for r in ledger.records] == [("finus_account_balance", True)]


class _TextActionModel(BaseChatModel):
    """도구 강제 없이 텍스트 ReAct로 KIS 도구를 {}로 부르고, Observation을 받으면 끝낸다."""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "text-action"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls += 1
        if any("kis_mcp_missing_arguments" in str(m.content) for m in messages):
            text = "Thought: I now know the final answer\nFinal Answer: 조회 실패"
        else:
            text = "Thought: 조회한다\nAction: kis-trading-mcp-tool-readonly\nAction Input: {}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


async def test_kis_call_without_api_type_outside_forced_turn_reaches_wrapper_and_ledger(kis_ledger):
    """리뷰 지적 2·5(b): 강제 턴 밖(텍스트 ReAct)의 {} 호출은 입력 스키마에서 막히지 않고 래퍼까지
    가서, main과 같이 원장에 ``finus_account_balance ok=False``로 남고 ``tools_used`` 각주에 실패로 보인다.

    뮤테이션: api_type 필수 판정을 입력 스키마 검증(model_validator)으로 옮기면 래퍼에 닿지 않아
    원장이 비어 red.
    """
    from finus_agents.finus_api import ReasoningTrace
    from finus_agents.finus_api import ToolUse

    ledger, remote_calls = kis_ledger
    kis_tool, gen = await _kis_tool()
    _, memory = _tools([])
    try:
        state = await _run(ReActAgentGraph, _TextActionModel(), (kis_tool, memory))
    finally:
        await gen.__aexit__(None, None, None)

    assert remote_calls == []
    assert [(r.tool_name, r.ok) for r in ledger.records] == [("finus_account_balance", False)]
    assert "kis_mcp_missing_arguments" in str(state.tool_responses[0].content)
    trace = ReasoningTrace()
    trace.record_ledger_tools(ledger)
    assert trace.tools_used == [ToolUse("finus_account_balance", ok=False)]


class _TwoLookupChatModel(BaseChatModel):
    """#435의 실측 행동을 흉내 낸다: 조회가 두 번 필요한 질문에서 첫 Observation을 받은 뒤,
    도구가 API로 바인딩돼 있으면 남은 종목을 조회하고, 텍스트 ReAct만 열려 있으면
    "두 번째 종목은 조회하지 않았다"는 Final Answer로 끝낸다."""

    stocks: tuple[str, ...] = ("삼성전자", "SK하이닉스")
    calls: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-two-lookup"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[t.name for t in tools], **kwargs)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(kwargs)
        seen = [s for s in self.stocks if any(f"{s} 뉴스 OBS" in str(m.content) for m in messages[2:])]
        missing = [s for s in self.stocks if s not in seen]
        if "tools" in kwargs and missing:
            message = AIMessage(content="", tool_calls=[
                {"name": "mcp-news-get-market-news", "args": {"stock_name": missing[0]}, "id": f"call_{len(seen)}"},
            ])
        elif missing:
            message = AIMessage(content=f"Thought: I now know the final answer\nFinal Answer: {missing[0]}는 조회하지 않아 비교할 수 없습니다.")
        else:
            message = AIMessage(content="Thought: I now know the final answer\nFinal Answer: 두 종목 비교 완료")
        return ChatResult(generations=[ChatGeneration(message=message)])


def _per_stock_news_tools(called: list[str]):
    @tool("mcp-news-get-market-news")
    def news(stock_name: str) -> str:
        """종목 뉴스"""
        called.append(stock_name)
        return f"{stock_name} 뉴스 OBS"

    @tool("get_user_memory")
    def memory(note: str = "") -> str:
        """메모리"""
        return "memory disabled"

    return news, memory


async def test_vendor_text_turn_after_first_observation_stops_at_one_lookup():
    """대조군: 첫 턴만 강제하고 이후 턴을 텍스트 ReAct(도구 미바인딩)로 두면 #435가 재현된다.

    대역이 증상을 재현한다는 확인이다 — 이것이 초록이 아니면 아래 테스트는 아무것도 증명하지 않는다.
    """
    called: list[str] = []
    news, memory = _per_stock_news_tools(called)
    llm = _TwoLookupChatModel()
    graph = ToolFirstReActAgentGraph(llm=llm, prompt=_prompt([news, memory]), tools=[news, memory],
                                     raise_on_parsing_failure=False, first_turn_tools=[news])
    graph.agent = graph.agent.first | llm  # 수정 전처럼 이후 턴에 도구를 바인딩하지 않는다
    compiled = await graph.build_graph()

    state = ReActGraphState(**await compiled.ainvoke(
        ReActGraphState(messages=[HumanMessage(content="삼성전자와 SK하이닉스 뉴스 비교해줘")]),
        config={"recursion_limit": 12}))

    assert called == ["삼성전자"]
    assert state.final_answer == "SK하이닉스는 조회하지 않아 비교할 수 없습니다."


async def test_turns_after_first_observation_keep_tools_bound_and_reach_every_lookup():
    """#435: Observation 뒤의 턴도 도구를 API로 바인딩(강제 없이)해, 두 번째 조회까지 하고 답한다.

    뮤테이션: ``__init__``의 ``self.agent`` 재바인딩을 지우면 called==["삼성전자"]로 red.
    ``_stream_llm`` 재정의를 지우면(벤더가 tool_calls를 버림) 두 번째 조회가 빈 응답이 되어 red.
    """
    called: list[str] = []
    news, memory = _per_stock_news_tools(called)
    llm = _TwoLookupChatModel()
    graph = await ToolFirstReActAgentGraph(llm=llm, prompt=_prompt([news, memory]), tools=[news, memory],
                                           raise_on_parsing_failure=False, first_turn_tools=[news]).build_graph()

    state = ReActGraphState(**await graph.ainvoke(
        ReActGraphState(messages=[HumanMessage(content="삼성전자와 SK하이닉스 뉴스 비교해줘")]),
        config={"recursion_limit": 12}))

    assert called == ["삼성전자", "SK하이닉스"]
    assert state.final_answer == "두 종목 비교 완료"
    forced, *rest = llm.calls
    assert forced["tool_choice"] == "required"
    # 이후 턴: 강제하지 않고(답할 수 있으면 끝내야 한다) 데이터 도구만 API로 연다 — 메모리·쓰기 도구는 텍스트로만
    assert rest and all("tool_choice" not in call for call in rest)
    assert all(call["tools"] == ["mcp-news-get-market-news"] for call in rest)
    assert all(call["parallel_tool_calls"] is False for call in rest)
    # 두 번째 조회는 텍스트 ReAct로 옮겨져 scratchpad에 남는다 — 다음 턴의 모델이 무엇을 불렀는지 본다
    assert state.agent_scratchpad[1].log == (
        'Thought: 요청에 답하려면 데이터를 조회한다.\nAction: mcp-news-get-market-news\n'
        'Action Input: {"stock_name":"SK하이닉스"}')


async def test_scratchpad_keeps_only_arguments_the_tool_schema_accepts():
    """#435 실측: 모델이 뉴스 도구 인자에 지어낸 기사(``articles``)를 실었다. 도구는 스키마 밖 필드를
    버리지만, 그대로 Action Input에 남기면 다음 턴의 답에 지어낸 기사가 섞인다.

    뮤테이션: ``_stream_llm``에서 ``_call_with_schema_args``를 빼면 log에 articles가 남아 red.
    """
    called: list[str] = []
    news, memory = _per_stock_news_tools(called)

    class _Fabricating(_TwoLookupChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            message = result.generations[0].message
            for call in message.tool_calls:
                call["args"] = {**call["args"], "articles": [{"title": "지어낸 기사"}]}
            return result

    graph = await ToolFirstReActAgentGraph(llm=_Fabricating(), prompt=_prompt([news, memory]), tools=[news, memory],
                                           raise_on_parsing_failure=False, first_turn_tools=[news]).build_graph()
    state = ReActGraphState(**await graph.ainvoke(
        ReActGraphState(messages=[HumanMessage(content="삼성전자와 SK하이닉스 뉴스 비교해줘")]),
        config={"recursion_limit": 12}))

    assert called == ["삼성전자", "SK하이닉스"]
    assert all("지어낸 기사" not in step.log and "지어낸 기사" not in step.tool_input
               for step in state.agent_scratchpad)


@pytest.mark.parametrize(("content", "thought"), [
    ("", "요청에 답하려면 데이터를 조회한다."),
    ("SK하이닉스도\n조회한다.", "SK하이닉스도 조회한다."),
    # 파서 키워드가 든 본문은 Thought로 옮기면 Action/Final Answer로 오인된다
    ("Final Answer: 끝", "요청에 답하려면 데이터를 조회한다."),
    ("Action: x", "요청에 답하려면 데이터를 조회한다."),
])
def test_native_tool_call_round_trips_through_vendor_parser(content: str, thought: str):
    """도구 호출을 옮긴 텍스트는 벤더 파서가 같은 도구·인자로 읽는다(한글·중첩 JSON 포함)."""
    import json

    from nat.plugins.langchain.agent.react_agent.output_parser import ReActOutputParser

    from finus_agents.tool_first_react import react_text_for_tool_call

    args = {"tool_name": "domestic_stock", "api_type": "inquire_price", "params": {"stock_name": "SK하이닉스"}}
    text = react_text_for_tool_call(content, {"name": "kis-trading-mcp-tool-readonly", "args": args})
    action = ReActOutputParser().parse(text)

    assert text.startswith(f"Thought: {thought}\n")
    assert action.tool == "kis-trading-mcp-tool-readonly"
    assert json.loads(action.tool_input) == args


# --- PR #439 리뷰 반영 -------------------------------------------------------------------------


def _two_lookup_graph(llm, news, memory, **kwargs):
    return ToolFirstReActAgentGraph(llm=llm, prompt=_prompt([news, memory]), tools=[news, memory],
                                    raise_on_parsing_failure=False, first_turn_tools=[news], **kwargs)


async def _ask(graph, question="삼성전자와 SK하이닉스 뉴스 비교해줘") -> ReActGraphState:
    compiled = await graph.build_graph()
    return ReActGraphState(**await compiled.ainvoke(ReActGraphState(messages=[HumanMessage(content=question)]),
                                                    config={"recursion_limit": 12}))


class _RecordingTwoLookup(_TwoLookupChatModel):
    """매 호출의 프롬프트 메시지를 남긴다."""

    seen: list[list[str]] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append([str(m.content) for m in messages])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_kst_today_text_uses_korean_date_and_weekday():
    from datetime import datetime
    from datetime import timezone

    from finus_agents.tool_first_react import kst_today_text

    # UTC 10/19 16:00은 한국 시간 10/20 01:00(화)이다
    assert kst_today_text(datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)) == "2026-10-20(화)"


async def test_every_turn_sees_today_read_from_the_clock_at_call_time():
    """#440: 날짜·기간 인자를 정하려면 에이전트가 오늘 날짜를 봐야 한다. 날짜는 빌드 시점이 아니라
    LLM을 부를 때마다 시계에서 읽는다 — 장기 실행 서버가 다음 날 어제 날짜를 주면 안 된다.

    뮤테이션: ``prompt_with_today``의 partial을 빌드 시점 문자열로 바꾸면 두 번째 이후 턴의 날짜가
    첫 날짜에 굳어 red. 날짜 시스템 메시지를 빼면 첫 assert부터 red.
    """
    from datetime import datetime

    from finus_agents.tool_first_react import KST

    days = iter([datetime(2026, 10, 20, 9, tzinfo=KST), datetime(2026, 10, 21, 9, tzinfo=KST),
                 datetime(2026, 10, 22, 9, tzinfo=KST)])
    called: list[str] = []
    news, memory = _per_stock_news_tools(called)
    llm = _RecordingTwoLookup()

    state = await _ask(_two_lookup_graph(llm, news, memory, clock=lambda: next(days)))

    assert state.final_answer == "두 종목 비교 완료"
    dates = [next(m for m in msgs if m.startswith("오늘 날짜(한국 시간)")) for msgs in llm.seen]
    assert [d.split(": ", 1)[1][:13] for d in dates] == ["2026-10-20(화)", "2026-10-21(수)", "2026-10-22(목)"]


class _BrokenJsonOnceTwoLookup(_TwoLookupChatModel):
    """이후 턴의 첫 도구 호출 인자 JSON이 깨진다(invalid_tool_calls). 깨졌다는 안내를 받으면 바로 고친다."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        told = any("인자 JSON이 깨져" in str(m.content) for m in messages)
        if message.tool_calls and message.tool_calls[0]["args"]["stock_name"] == "SK하이닉스" and not told:
            result.generations[0].message = AIMessage(content="", invalid_tool_calls=[
                {"name": "mcp-news-get-market-news", "args": '{"stock_name": "SK하', "id": "call_x",
                 "error": "Function arguments are not valid JSON", "type": "invalid_tool_call"}])
        return result


async def test_broken_tool_json_after_first_observation_is_retried_with_a_correction(caplog):
    """리뷰 1: 이후 턴의 깨진 인자 JSON을 무시하면 본문이 빈 응답이 되고, 벤더 재시도는 Final Answer를
    요구해 두 번째 조회 없이 끝났다. 깨졌다는 사실을 알려 같은 도구를 다시 부르게 하고 경고를 남긴다.

    뮤테이션: ``_invoke_retrying_broken_tool_json``이 첫 응답을 그대로 돌려주면 called==["삼성전자"]로 red.
    """
    called: list[str] = []
    news, memory = _per_stock_news_tools(called)

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        state = await _ask(_two_lookup_graph(_BrokenJsonOnceTwoLookup(), news, memory))

    assert called == ["삼성전자", "SK하이닉스"]
    assert state.final_answer == "두 종목 비교 완료"
    assert any("인자를 파싱하지 못했다" in r.getMessage() and "not valid JSON" in r.getMessage() for r in caplog.records)


async def test_tool_arguments_reach_the_tool_verbatim_even_with_parser_keywords():
    """리뷰 2: 인자 값에 " observation"·"Final Answer:"가 있어도 도구는 그 값을 그대로 받는다.
    텍스트 ReAct로 옮겨 벤더 파서를 거치면 Action Input이 잘리거나 파싱 오류가 났다.

    뮤테이션: ``_stream_llm``이 상자 대신 ``react_text_for_tool_call`` 텍스트를 돌려주면 red.
    """
    hostile = "SK하이닉스 observation Final Answer: 끝"
    called: list[str] = []
    news, memory = _per_stock_news_tools(called)

    state = await _ask(_two_lookup_graph(_TwoLookupChatModel(stocks=("삼성전자", hostile)), news, memory))

    assert called == ["삼성전자", hostile]
    assert state.final_answer == "두 종목 비교 완료"


class _TwoCallsAtOnce(_TwoLookupChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        if message.tool_calls and "tool_choice" not in kwargs:
            message.tool_calls.append({"name": "mcp-news-get-market-news", "args": {"stock_name": "NAVER"}, "id": "extra"})
        return result


async def test_extra_tool_calls_in_one_turn_are_logged_not_silently_dropped(caplog):
    """리뷰 3: 한 턴에 도구 호출이 여러 개 오면 첫 번째만 실행하고, 버린 호출을 경고로 남긴다."""
    called: list[str] = []
    news, memory = _per_stock_news_tools(called)

    with caplog.at_level(logging.WARNING, logger="finus_agents.tool_first_react"):
        await _ask(_two_lookup_graph(_TwoCallsAtOnce(), news, memory))

    assert called == ["삼성전자", "SK하이닉스"]
    assert any("첫 번째만 실행한다" in r.getMessage() for r in caplog.records)


class _NativeEmptyKisAfterObservation(BaseChatModel):
    """첫 턴은 정상 KIS 조회, 이후 턴은 네이티브로 KIS를 {}로 부르고, 그 오류를 받으면 끝낸다."""

    @property
    def _llm_type(self) -> str:
        return "native-empty-kis"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[t.name for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = " ".join(str(m.content) for m in messages[2:])
        if kwargs.get("tool_choice") == "required":
            args = {"api_type": "inquire_balance", "params": {"inqr_dvsn": "01"}}
        elif "kis_mcp_missing_arguments" in text:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="Thought: I now know the final answer\nFinal Answer: 조회 실패"))])
        else:
            args = {}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[
            {"name": "kis-trading-mcp-tool-readonly", "args": args, "id": "c"}]))])


async def test_empty_kis_call_after_first_observation_reaches_wrapper_and_ledger(kis_ledger):
    """리뷰 2: 이후 턴은 강제하지 않으므로 #437의 사전 검사를 타지 않는다. 그런 {} 호출은 래퍼까지 가서
    원장에 실패로 남고 오류가 Observation으로 모델에 간다(재강제 없음)."""
    ledger, remote_calls = kis_ledger
    kis_tool, gen = await _kis_tool()
    _, memory = _tools([])
    try:
        state = await _run(ToolFirstReActAgentGraph, _NativeEmptyKisAfterObservation(), (kis_tool, memory),
                           first_turn_tools=[kis_tool])
    finally:
        await gen.__aexit__(None, None, None)

    assert [c["arguments"]["api_type"] for c in remote_calls] == ["inquire_balance"]
    assert [(r.tool_name, r.ok) for r in ledger.records] == [("finus_account_balance", True),
                                                               ("finus_account_balance", False)]
    assert state.final_answer == "조회 실패"


def test_followup_binding_keeps_vendor_stop_sequence_for_non_gpt5_models():
    """리뷰 7: gpt-5 계열이 아닌 모델에 벤더가 붙이는 정지 시퀀스를 이후 턴 바인딩도 유지한다.

    뮤테이션: ``**vendor_bind``를 빼면 red.
    """
    news, memory = _per_stock_news_tools([])
    graph = _two_lookup_graph(_TwoLookupChatModel(), news, memory)

    assert graph.agent.last.kwargs["stop"] == ["Observation:"]
    assert graph.agent.last.kwargs["parallel_tool_calls"] is False


def test_native_tool_calling_config_is_rejected():
    """리뷰 7: 이 그래프는 도구를 직접 바인딩한다. 벤더 네이티브 모드와 겹치면 바인딩이 두 번 된다."""
    news, memory = _per_stock_news_tools([])
    with pytest.raises(ValueError, match="use_native_tool_calling"):
        _two_lookup_graph(_TwoLookupChatModel(), news, memory, use_native_tool_calling=True)


async def test_kis_schema_filter_keeps_legacy_tool_input_and_nested_params():
    """리뷰 7: 스키마 필터는 스키마 밖 필드만 버린다. KIS 레거시 ``tool_input``과 중첩 ``params``는 남는다."""
    kis_tool, gen = await _kis_tool()
    _, memory = _tools([])
    try:
        graph = ToolFirstReActAgentGraph(llm=_TwoLookupChatModel(), prompt=_prompt([kis_tool, memory]),
                                         tools=[kis_tool, memory], first_turn_tools=[kis_tool])
    finally:
        await gen.__aexit__(None, None, None)
    args = {"tool_name": "domestic_stock", "tool_input": '{"api_type": "inquire_price"}',
            "api_type": "inquire_daily_itemchartprice",
            "params": {"stock_name": "삼성전자", "nested": {"fid_input_date_1": "20251020"}},
            "articles": [{"title": "지어낸 기사"}]}

    kept = graph._call_with_schema_args({"name": "kis-trading-mcp-tool-readonly", "args": args})["args"]

    assert kept == {key: value for key, value in args.items() if key != "articles"}


def test_api_tools_reject_write_and_memory_tool_types():
    """리뷰 5: API로 여는 도구 집합에 쓰기·메모리 도구가 들어오면 빌드를 멈춘다(fail-closed).

    뮤테이션: ``reject_non_data_api_tools``가 아무것도 거부하지 않으면 red.
    """
    from finus_agents.agents import FinusMemoryDisabledConfig
    from finus_agents.finus_api import FinusMarketNewsConfig
    from finus_agents.finus_api import FinusSaveDiaryConfig
    from finus_agents.tool_first_react import reject_non_data_api_tools

    configs = {"news": FinusMarketNewsConfig.model_construct(), "save": FinusSaveDiaryConfig.model_construct(),
               "mem": FinusMemoryDisabledConfig.model_construct()}

    reject_non_data_api_tools(["news"], configs.__getitem__)
    with pytest.raises(ValueError, match=r"\['mem', 'save'\]"):
        reject_non_data_api_tools(["news", "save", "mem"], configs.__getitem__)


@pytest.mark.parametrize("config_name", ["router.yml", "router_nomemory.yml"])
def test_router_api_tool_sets_hold_data_tools_only(config_name: str):
    """라우터가 로드하는 실제 설정은 위 검사를 통과한다(빌드가 깨지지 않는다)."""
    from finus_agents.tool_first_react import reject_non_data_api_tools

    config = _load_config(config_name)
    agents = [fn for fn in config.functions.values() if isinstance(fn, FinusToolFirstReActAgentConfig)]

    assert agents
    for agent in agents:
        reject_non_data_api_tools(agent.first_turn_tool_names, lambda name: config.functions[str(name)])


def test_graph_kwargs_cover_vendor_graph_signature():
    """NAT를 올려 ``ReActAgentGraph.__init__``에 인자가 생기거나 사라지면 여기서 드러난다.

    복제한 워크플로가 새 인자를 넘기지 않으면 빌드는 성공하고 그 옵션만 조용히 기본값이 된다.
    ``callbacks``는 벤더 ``react_agent_workflow``도 넘기지 않는다.
    뮤테이션: ``graph_kwargs``에서 ``raise_on_parsing_failure`` 한 줄을 지우면 red.
    """
    import inspect

    from finus_agents.tool_first_react import graph_kwargs

    vendor = set(inspect.signature(ReActAgentGraph.__init__).parameters) - {"self", "callbacks"}
    config = FinusToolFirstReActAgentConfig(llm_name="llm", tool_names=["t"], first_turn_tool_names=["t"])
    passed = set(graph_kwargs(config, llm=None, prompt=None, tools=[]))

    assert passed == vendor, f"빠진 인자: {sorted(vendor - passed)}, 없어진 인자: {sorted(passed - vendor)}"
    assert set(inspect.signature(ToolFirstReActAgentGraph.__init__).parameters) == {
        "self", "first_turn_tools", "clock", "kwargs"}


def test_config_rejects_first_turn_tool_outside_tool_names():
    with pytest.raises(ValueError, match="first_turn_tool_names"):
        FinusToolFirstReActAgentConfig(llm_name="llm", tool_names=["mcp-news-get-market-news"],
                                       first_turn_tool_names=["mcp-dart-get-earnings-report"])


@pytest.mark.parametrize("config_name", ["agents/news_agent.yml", "router.yml", "router_nomemory.yml"])
def test_news_agent_forces_data_tools_only(config_name: str):
    """news_agent가 첫 턴 강제 타입을 쓰고, 강제 대상에 메모리 도구가 없다.

    뮤테이션: news_agent.yml의 ``_type``을 ``react_agent``로 되돌리면 red.
    """
    config = _load_config(config_name)
    news = config.functions["news_agent"]

    assert isinstance(news, FinusToolFirstReActAgentConfig)
    forced = {str(name) for name in news.first_turn_tool_names}
    assert {"mcp-news-get-market-news", "mcp-dart-get-earnings-report"} <= forced
    assert not forced & {"add_user_memory", "get_user_memory"}


_MEMORY_TOOLS = {"add_user_memory", "get_user_memory"}
# 일지 저장처럼 외부 상태를 바꾸는 도구. 강제 턴이 이것으로 채워지면 사용자가 요청하지 않은 쓰기가 나간다.
_WRITE_TOOLS = {"finus-save-diary"}

# #399: (정의 파일, 함수 이름, 첫 턴에 강제할 도구 전부). 강제 대상은 tool_names에서 메모리·쓰기
# 도구를 뺀 나머지와 정확히 같아야 한다 — 데이터 도구가 빠지면 그 질문 유형에서 조회를 못 고르고,
# 메모리·쓰기 도구가 들어가면 조회 없이(또는 쓰기로) 강제 호출을 채울 수 있다.
_TOOL_FIRST_AGENTS = [
    ("agents/trading_agent.yml", "trading_agent_react",
     {"kis-trading-mcp-tool-readonly", "mcp-news-get-market-news", "mcp-dart-get-disclosure-signal"}),
    ("agents/monitoring_agent.yml", "monitoring_agent",
     {"kis-trading-mcp-tool-readonly", "mcp-news-get-market-news", "mcp-dart-get-disclosure-signal"}),
    ("agents/recommend_agent.yml", "recommend_agent",
     {"kis-trading-mcp-tool-readonly", "mcp-news-get-market-news"}),
    ("agents/strategy_agent.yml", "strategy_agent",
     {"kis-trading-mcp-tool-readonly", "mcp-news-get-market-news", "mcp-dart-get-disclosure-signal"}),
    ("agents/diary_agent.yml", "diary_agent",
     # #405: 일지 조회를 묶음 도구 하나로 강제한다 — 개별 조회 도구가 있으면 당일 주문만 조회하고 끝났다
     {"mcp-trading-diary-snapshot", "finus-list-diaries"}),
]


def _load_config(config_name: str):
    from pathlib import Path

    import finus_agents.register  # noqa: F401
    from nat.runtime.loader import load_config

    return load_config(Path(__file__).resolve().parents[1] / "configs" / config_name)


@pytest.mark.parametrize("config_source", ["own", "router.yml", "router_nomemory.yml"])
@pytest.mark.parametrize(("own_file", "function_name", "expected_forced"), _TOOL_FIRST_AGENTS,
                         ids=[fn for _, fn, _ in _TOOL_FIRST_AGENTS])
def test_agent_forces_data_tools_only(own_file: str, function_name: str, expected_forced: set[str],
                                      config_source: str):
    """#399: trading·monitoring·recommend·strategy·diary도 첫 턴 강제 타입을 쓰고, 강제 대상은
    메모리·쓰기 도구를 뺀 데이터 조회 도구 전부다. 라우터가 실제로 로드하는 설정에서도 같아야 한다.

    뮤테이션: 각 yml의 ``_type``을 ``react_agent``로 되돌리면 그 에이전트의 3개 케이스가 red.
    diary의 ``first_turn_tool_names``에 ``finus-save-diary``를 넣거나 어느 에이전트에든 메모리 도구를
    넣으면 red.
    """
    config = _load_config(own_file if config_source == "own" else config_source)
    agent = config.functions[function_name]

    assert isinstance(agent, FinusToolFirstReActAgentConfig), f"{function_name}: {type(agent).__name__}"
    forced = {str(name) for name in agent.first_turn_tool_names}
    tool_names = {str(name) for name in agent.tool_names}
    assert not forced & (_MEMORY_TOOLS | _WRITE_TOOLS)
    assert forced == expected_forced
    assert forced == tool_names - _MEMORY_TOOLS - _WRITE_TOOLS, (
        f"{function_name}: 새 도구가 쓰기 도구라면 _WRITE_TOOLS에, 조회 도구라면 first_turn_tool_names에 넣는다"
    )


@pytest.mark.parametrize("config_name", ["router.yml", "router_nomemory.yml"])
def test_every_router_react_agent_is_tool_first(config_name: str):
    """#399: 라우터가 로드하는 ReAct 에이전트는 예외 없이 첫 턴 강제 타입이다.

    ``_TOOL_FIRST_AGENTS``는 이름으로 나열한 목록이라, 새 에이전트를 벤더 ``react_agent``로
    추가하면 위 테스트는 그대로 통과하고 #394 증상(도구를 한 번도 부르지 않는 첫 턴)이 새
    에이전트에서 조용히 재발한다. 전수로 훑어 그 경로를 막는다.

    뮤테이션: 아무 에이전트 yml의 ``_type``을 ``react_agent``로 되돌리면 red.
    """
    config = _load_config(config_name)
    react_agents = {name: fn for name, fn in config.functions.items() if isinstance(fn, ReActAgentWorkflowConfig)}

    assert react_agents, "라우터 설정에서 ReAct 에이전트를 하나도 찾지 못했다 — 전수 검사가 공허하다"
    vendor_typed = sorted(name for name, fn in react_agents.items()
                          if not isinstance(fn, FinusToolFirstReActAgentConfig))
    assert not vendor_typed, f"벤더 react_agent 타입이 남아 있다: {vendor_typed}"
