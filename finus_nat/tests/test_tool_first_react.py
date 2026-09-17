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
from nat_finus_nat.tool_first_react import FinusToolFirstReActAgentConfig
from nat_finus_nat.tool_first_react import ToolFirstReActAgentGraph

NEWS_OBSERVATION = "삼성전자 뉴스 헤드라인 OBS"
DIRECT_REFUSAL = "Thought: I now know the final answer\nFinal Answer: 조회 결과가 없어 답할 수 없습니다."


class _ScriptedChatModel(BaseChatModel):
    """도구가 강제되지 않으면 도구 없이 끝내는 모델."""

    honors_tool_choice: bool = True
    # 강제 호출 중 앞에서 이 횟수만큼은 인자 JSON이 깨진 응답(invalid_tool_calls)을 돌려준다
    invalid_forced_calls: int = 0
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
        elif forced:
            message = AIMessage(content="", tool_calls=[
                {"name": kwargs["tools"][0], "args": {"stock_name": "삼성전자"}, "id": "call_1"},
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

    with caplog.at_level(logging.WARNING, logger="nat_finus_nat.tool_first_react"):
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

    with caplog.at_level(logging.WARNING, logger="nat_finus_nat.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, llm, (news, memory), first_turn_tools=[news])

    assert called == ["news:삼성전자"]
    assert state.final_answer == "뉴스 요약"
    assert llm.forced_calls() == 2
    messages = [r.getMessage() for r in caplog.records]
    assert any("인자를 파싱하지 못했다" in m and "not valid JSON" in m for m in messages)
    assert not any("도구 호출이 없어" in m for m in messages)


async def test_falls_back_after_repeated_invalid_tool_arguments(caplog):
    """재시도는 상한까지만 한다 — 계속 깨지면 벤더 경로로 답하고 원인을 구분해 남긴다."""
    from nat_finus_nat.tool_first_react import FIRST_TURN_MAX_ATTEMPTS

    called: list[str] = []
    news, memory = _tools(called)
    llm = _ScriptedChatModel(invalid_forced_calls=FIRST_TURN_MAX_ATTEMPTS)

    with caplog.at_level(logging.WARNING, logger="nat_finus_nat.tool_first_react"):
        state = await _run(ToolFirstReActAgentGraph, llm, (news, memory), first_turn_tools=[news])

    assert called == []
    assert llm.forced_calls() == FIRST_TURN_MAX_ATTEMPTS
    assert state.final_answer == "조회 결과가 없어 답할 수 없습니다."
    assert any("계속 유효하지 않아" in r.getMessage() for r in caplog.records)


def test_graph_kwargs_cover_vendor_graph_signature():
    """NAT를 올려 ``ReActAgentGraph.__init__``에 인자가 생기거나 사라지면 여기서 드러난다.

    복제한 워크플로가 새 인자를 넘기지 않으면 빌드는 성공하고 그 옵션만 조용히 기본값이 된다.
    ``callbacks``는 벤더 ``react_agent_workflow``도 넘기지 않는다.
    뮤테이션: ``graph_kwargs``에서 ``raise_on_parsing_failure`` 한 줄을 지우면 red.
    """
    import inspect

    from nat_finus_nat.tool_first_react import graph_kwargs

    vendor = set(inspect.signature(ReActAgentGraph.__init__).parameters) - {"self", "callbacks"}
    config = FinusToolFirstReActAgentConfig(llm_name="llm", tool_names=["t"], first_turn_tool_names=["t"])
    passed = set(graph_kwargs(config, llm=None, prompt=None, tools=[]))

    assert passed == vendor, f"빠진 인자: {sorted(vendor - passed)}, 없어진 인자: {sorted(passed - vendor)}"
    assert set(inspect.signature(ToolFirstReActAgentGraph.__init__).parameters) == {"self", "first_turn_tools", "kwargs"}


def test_config_rejects_first_turn_tool_outside_tool_names():
    with pytest.raises(ValueError, match="first_turn_tool_names"):
        FinusToolFirstReActAgentConfig(llm_name="llm", tool_names=["mcp-news-get-market-news"],
                                       first_turn_tool_names=["mcp-dart-get-earnings-report"])


@pytest.mark.parametrize("config_name", ["agents/news_agent.yml", "router.yml", "router_nomemory.yml"])
def test_news_agent_forces_data_tools_only(config_name: str):
    """news_agent가 첫 턴 강제 타입을 쓰고, 강제 대상에 메모리 도구가 없다.

    뮤테이션: news_agent.yml의 ``_type``을 ``react_agent``로 되돌리면 red.
    """
    from pathlib import Path

    import nat_finus_nat.register  # noqa: F401
    from nat.runtime.loader import load_config

    config = load_config(Path(__file__).resolve().parents[1] / "configs" / config_name)
    news = config.functions["news_agent"]

    assert isinstance(news, FinusToolFirstReActAgentConfig)
    forced = {str(name) for name in news.first_turn_tool_names}
    assert {"mcp-news-get-market-news", "mcp-dart-get-earnings-report"} <= forced
    assert not forced & {"add_user_memory", "get_user_memory"}
