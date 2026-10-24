"""#449: 보유종목 뉴스는 지름길 없이 news_agent의 정상 경로(잔고 조회 → 종목별 뉴스)로 답한다.

제거한 지름길(``_holdings_news_answer``)은 news_agent 분기 맨 앞에서 대화 기록의 종목명(하드코딩한
삼성전자·NAVER와 "- 이름 N주" 줄)을 모아, 잔고를 조회하지 않고 그 뉴스를 "보유종목 관련 최신 뉴스"로
답했다. 대화에 다른 종목이 나온 뒤 "카카오 종목 뉴스"를 물어도 그 종목 뉴스가 보유종목 뉴스로 나갔다.

여기서는 ``fe_branch``(news_agent 분기) → 도구 강제 게이트 → ``ToolFirstReActAgentGraph`` → 실제 조회 전용
KIS 래퍼(원격 MCP만 대역, PII 마스킹 포함) → 뉴스 도구까지 이어 돌린다. 대역 LLM은 질문과 Observation만
보고 도구를 고르므로(지름길이 쓰던 대화 기록의 종목명은 쓰지 않는다), 이 테스트가 증명하는 것은 배선이다:
분기가 질문을 가로채지 않고, 첫 턴에 잔고 도구를 강제 대상으로 고를 수 있고, 마스킹된 잔고 Observation에
종목명이 남아 이후 턴(도구 바인딩)에서 종목별 뉴스를 조회할 수 있다. 모델의 실제 선택은 실측으로 본다.
"""

import json
import re
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.outputs import ChatResult
from langchain_core.tools import StructuredTool
from langchain_core.tools import tool
from pydantic import Field

from nat.data_models.api_server import ChatRequest
from nat.data_models.api_server import ChatRequestOrMessage
from nat.data_models.api_server import Message
from nat.data_models.api_server import UserMessageContentRoleType as Role
from nat.plugins.langchain.agent.react_agent.agent import ReActGraphState
from nat.plugins.langchain.agent.react_agent.agent import create_react_agent_prompt
from nat.plugins.langchain.agent.react_agent.register import ReActAgentWorkflowConfig

from finus_agents import agents
from finus_agents import finus_api
from finus_agents.pii_guard import PII_MAPPING
from finus_agents.pii_guard import install_mapping_box
from finus_agents.tool_first_react import ToolFirstReActAgentGraph

_FIXTURES = Path(__file__).parent / "fixtures" / "kis_trading_mcp_inquire_balance"
# 합성 잔고: 보유 종목은 삼성전자 하나다.
_BALANCE = (_FIXTURES / "synthetic_with_holding.json").read_text(encoding="utf-8")
# 잔고 원문의 금액(평가금액·예수금). 마스킹되면 LLM 입력 어디에도 나오면 안 된다.
_RAW_AMOUNTS = ("8269500", "4817263", "13086763")
_PRDT_NAME_RE = re.compile(r'prdt_name\\*"\s*:\s*\\*"([^"\\]+)')
_NO_HOLDINGS = "보유 종목이 없어 보유종목 뉴스가 없습니다."


def _empty_balance() -> str:
    """같은 봉투에 보유 종목(output1)만 비운 잔고."""
    envelope = json.loads(_BALANCE)
    body = json.loads(envelope["data"]["data"])
    body["output1"] = []
    envelope["data"]["data"] = json.dumps(body, ensure_ascii=False)
    return json.dumps(envelope, ensure_ascii=False)


class _NewsAnalystModel(BaseChatModel):
    """질문과 Observation만 보고 도구를 고르는 대역 LLM.

    - 강제 첫 턴: 현재 요청이 보유종목을 가리키면 잔고를, 종목명을 직접 말하면 그 종목 뉴스를 고른다.
    - 이후 턴: 잔고 Observation의 ``prdt_name`` 중 아직 조회하지 않은 종목의 뉴스를 고르고, 다 봤으면 답한다.
    """

    inputs: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-news-analyst"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[t.name for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        text = "\n".join(str(m.content) for m in messages)
        self.inputs.append(text)
        request = text.split("[Current user request]\n")[-1].splitlines()[0]
        if kwargs.get("tool_choice") == "required":
            if "보유" in request:
                call = {"name": "kis-trading-mcp-tool-readonly",
                        "args": {"api_type": "inquire_balance", "params": {"inqr_dvsn": "01"}}, "id": "b"}
            else:
                call = {"name": "mcp-news-get-market-news", "args": {"stock_name": request.split()[0]}, "id": "n"}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])
        held = list(dict.fromkeys(_PRDT_NAME_RE.findall(text)))
        fetched = re.findall(r"NEWS\[([^\]]+)\]", text)
        pending = [name for name in held if name not in fetched]
        if pending:
            call = {"name": "mcp-news-get-market-news", "args": {"stock_name": pending[0]}, "id": "n2"}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])
        answer = f"뉴스 요약: {', '.join(fetched)}" if fetched else _NO_HOLDINGS
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=f"Thought: I now know the final answer\nFinal Answer: {answer}"))])


@pytest.fixture
def kis_remote(monkeypatch):
    """조회 전용 KIS 래퍼의 원격 MCP 호출만 대역으로 바꾼다. 응답은 ``kis_remote.reply``로 고른다."""
    state = {"reply": _BALANCE, "calls": []}

    async def fake_remote(**kwargs):
        state["calls"].append(kwargs["arguments"].get("api_type"))
        return state["reply"]

    monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake_remote)
    monkeypatch.setenv("FINUS_SKIP_MCP_LIST_TOOLS", "1")
    return state


async def _kis_readonly_tool():
    config = finus_api.FinusAccountBalanceReadonlyConfig(trading_tool_name="domestic_stock")
    gen = finus_api.finus_account_balance_readonly(config, None)
    info = await gen.__aenter__()

    async def kis(**kwargs) -> str:
        return await info.single_fn(finus_api.KisTradingMcpCallInput(**kwargs))

    return StructuredTool.from_function(coroutine=kis, name="kis-trading-mcp-tool-readonly", description="KIS",
                                        args_schema=finus_api.KisTradingMcpCallInput), gen


def _news_tool(called: list[str]):
    @tool("mcp-news-get-market-news")
    def news(stock_name: str) -> str:
        """종목 뉴스"""
        called.append(stock_name)
        finus_api._record_to_ledger("finus_market_news", "ok")
        return f"NEWS[{stock_name}] 헤드라인"

    return news


class _NewsAgent:
    """``builder.get_function("news_agent")`` 대역 — 문자열 입력으로 도구 강제 그래프를 돌린다."""

    def __init__(self, graph):
        self.graph = graph

    async def ainvoke(self, request: ChatRequestOrMessage) -> str:
        state = await self.graph.ainvoke(ReActGraphState(messages=[HumanMessage(content=request.input_message)]),
                                         config={"recursion_limit": 20})
        return str(ReActGraphState(**state).messages[-1].content)


class _Builder:
    def __init__(self, functions: dict):
        self.functions = functions
        self.requested: list[str] = []

    async def get_function(self, name: str):
        self.requested.append(name)
        return self.functions[name]


def _conversation(*turns: tuple[Role, str]) -> ChatRequest:
    return ChatRequest(messages=[Message(role=role, content=content) for role, content in turns])


# 지름길이 보유 종목으로 집어 가던 대화 기록: 하드코딩 이름(NAVER)과 "- 이름 N주" 줄(SK하이닉스).
_OTHER_STOCK_HISTORY = (
    (Role.USER, "NAVER 현재가 알려줘"),
    (Role.ASSISTANT, "NAVER 현재가를 조회했습니다.\n- SK하이닉스 3주 매수는 텔레그램 /buy로 하세요."),
)


async def _ask_news_branch(conversation: ChatRequest, llm: _NewsAnalystModel, news_called: list[str]):
    kis_tool, gen = await _kis_readonly_tool()
    news = _news_tool(news_called)

    @tool("get_user_memory")
    def memory(note: str = "") -> str:
        """메모리"""
        return "memory disabled"

    tools = [kis_tool, news, memory]
    prompt = create_react_agent_prompt(ReActAgentWorkflowConfig(llm_name="llm", tool_names=[t.name for t in tools]))
    graph = await ToolFirstReActAgentGraph(llm=llm, prompt=prompt, tools=tools, raise_on_parsing_failure=False,
                                           first_turn_tools=[kis_tool, news]).build_graph()
    # 지름길은 builder에서 뉴스 도구를 직접 꺼내 불렀다. 같은 이름으로 기록하는 대역을 둬서, 되살아나면 드러나게 한다.
    shortcut_news: list[str] = []

    class _DirectNews:
        async def ainvoke(self, stock_name):
            shortcut_news.append(stock_name)
            return "직접 호출한 뉴스"

    builder = _Builder({"news_agent": _NewsAgent(graph), "mcp-news-get-market-news": _DirectNews()})
    branch = agents.fe_branch(agents.FeBranchConfig(inner_function_name="news_agent"), builder)
    info = await branch.__aenter__()
    token = PII_MAPPING.set(install_mapping_box())
    try:
        answer = await info.single_fn(conversation)
    finally:
        PII_MAPPING.reset(token)
        await branch.__aexit__(None, None, None)
        await gen.__aexit__(None, None, None)
    return answer, builder, shortcut_news


async def test_holdings_news_answers_from_balance_not_from_conversation(kis_remote):
    """보유종목 뉴스는 잔고를 조회하고, 잔고의 종목(삼성전자)만 뉴스를 조회한다 — 대화 속 NAVER·SK하이닉스가 아니다.
    잔고 금액은 마스킹돼 LLM 입력에 원문으로 실리지 않는다.

    뮤테이션: fe_branch에 지름길(``_holdings_news_answer``)을 되살리면 대화 속 NAVER·SK하이닉스 뉴스를 직접 조회해
    그대로 답하므로 red(잔고 조회 없음, 답이 "보유종목 관련 최신 뉴스입니다"). KIS 래퍼의 ``_record_and_mask``를
    마스킹 없는 기록으로 바꾸면 원문 금액이 LLM 입력에 실려 red.
    """
    llm = _NewsAnalystModel()
    news_called: list[str] = []
    conversation = _conversation(*_OTHER_STOCK_HISTORY, (Role.USER, "내 보유종목 뉴스 알려줘"))

    answer, builder, shortcut_news = await _ask_news_branch(conversation, llm, news_called)

    assert kis_remote["calls"] == ["inquire_balance"]
    assert news_called == ["삼성전자"]
    assert shortcut_news == []
    assert builder.requested == ["news_agent"]
    assert answer == "뉴스 요약: 삼성전자"
    # 잔고 Observation은 종목명을 남기고 금액은 가린 채로 이후 턴에 들어간다
    after_balance = [text for text in llm.inputs if "prdt_name" in text]
    assert after_balance, "잔고 Observation이 이후 턴의 LLM 입력에 들어가지 않았다"
    assert not any(amount in text for text in llm.inputs for amount in _RAW_AMOUNTS)


async def test_holdings_news_with_empty_balance_does_not_borrow_conversation_stocks(kis_remote):
    """잔고에 보유 종목이 없으면 뉴스를 조회하지 않는다. 지름길은 이 경우에도 대화 속 종목 뉴스를 보유종목 뉴스로 냈다.

    뮤테이션: 지름길을 되살리면 NAVER·SK하이닉스 뉴스가 직접 조회돼 red.
    """
    kis_remote["reply"] = _empty_balance()
    news_called: list[str] = []
    conversation = _conversation(*_OTHER_STOCK_HISTORY, (Role.USER, "보유 종목 관련 최신 뉴스 정리해줘"))

    answer, _, shortcut_news = await _ask_news_branch(conversation, _NewsAnalystModel(), news_called)

    assert kis_remote["calls"] == ["inquire_balance"]
    assert news_called == []
    assert shortcut_news == []
    assert answer == _NO_HOLDINGS


async def test_named_stock_news_is_not_rerouted_to_holdings(kis_remote):
    """이슈 재현: 대화에 다른 종목이 나온 뒤 "카카오 종목 뉴스"를 물으면 카카오 뉴스만 조회한다.

    뮤테이션: 지름길을 되살리면 "뉴스"+"종목"에 걸려 NAVER·SK하이닉스 뉴스를 보유종목 뉴스로 답해 red.
    """
    news_called: list[str] = []
    conversation = _conversation(*_OTHER_STOCK_HISTORY, (Role.USER, "카카오 종목 뉴스 알려줘"))

    answer, _, shortcut_news = await _ask_news_branch(conversation, _NewsAnalystModel(), news_called)

    assert kis_remote["calls"] == []
    assert news_called == ["카카오"]
    assert shortcut_news == []
    assert answer == "뉴스 요약: 카카오"


@pytest.mark.parametrize("config_name", ["agents/news_agent.yml", "router.yml", "router_nomemory.yml"])
def test_news_agent_can_force_a_readonly_balance_lookup(config_name: str):
    """보유종목 뉴스의 첫 조회(잔고)를 강제 첫 턴에서 고를 수 있고, 그 도구는 조회 전용 래퍼다.

    뮤테이션: news_agent.yml의 ``first_turn_tool_names``에서 ``kis-trading-mcp-tool-readonly``를 빼면 red.
    common.yml의 그 도구 ``_type``을 주문 가능한 타입으로 바꾸면 red.
    """
    import finus_agents.register  # noqa: F401
    from nat.runtime.loader import load_config

    config = load_config(Path(__file__).resolve().parents[1] / "configs" / config_name)
    news = config.functions["news_agent"]
    forced = {str(name) for name in news.first_turn_tool_names}

    assert "kis-trading-mcp-tool-readonly" in forced
    assert type(config.functions["kis-trading-mcp-tool-readonly"]) is finus_api.FinusAccountBalanceReadonlyConfig
