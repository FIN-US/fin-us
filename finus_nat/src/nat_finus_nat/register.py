# agents, finus_api 모듈을 import하여 `@register_function` 등록을 로드한다.
import functools
import inspect
import textwrap
from pathlib import Path

from dotenv import load_dotenv

# finus_nat 패키지 루트의 .env 파일을 찾아 환경 변수에 로드한다.
# register.py -> nat_finus_nat/ -> src/ -> finus_nat/
_FINUS_NAT_ENV = Path(__file__).resolve().parents[2] / ".env"
if _FINUS_NAT_ENV.is_file():
    load_dotenv(_FINUS_NAT_ENV, override=False)

#  NAT에 함수/도구 등록을 트리거
from nat_finus_nat import agents
from nat_finus_nat import finus_api


# 데이터 조회용 도구 이름에 포함되는 부분 문자열 목록 — 이 도구를 가진 ReAct 에이전트는 'strict' 모드로 동작.
_STRICT_TOOL_SUBSTR = (
    "kis-trading-mcp-tool",
    "finus_account_balance",
    "kis-api-daily-trades",
    "mcp-trading",
    "finus_mcp_trading",
    "mcp-news",
    "finus_market_news",
    "mcp-dart",
    "finus_disclosure",
)

# NAT 기본 ReAct 프롬프트를 대체할 Fin-Us 전용 시스템 프롬프트 (에이전트별로 선택 적용).
_FINUS_REACT_PROMPT_HEAD = """
당신은 Fin-Us 도구 사용 에이전트입니다. 사용자의 최신 요청에 바로 답하고, 그 요청에서 벗어나지 마세요.
도구로 조회할 수 있는 데이터에 대해서는 가능한 무조건 도구를 사용하고, 화제 전환·일반적인 멘트·추가 질문으로 회피하지 마세요.

사용 가능한 도구:
{tools}

등록된 도구 이름(정확한 철자): [{tool_names}]

ReAct 출력 규칙 (파서가 영문 키워드 `Thought:` / `Action:` / `Action Input:` 를 찾습니다. 한 응답 안에 세 줄을 빠짐없이 쓰세요):

Thought: (짧게, 한국어 가능)
Action: 여기에는 위 도구 이름 중 하나만 그대로 적습니다(대괄호나 "중 하나" 문구를 넣지 마세요).
"""

_FINUS_REACT_PROMPT_TAIL = """
그 다음 줄부터는 Observation이 옵니다(모델이 직접 쓰지 않음).

절대 하지 마세요:
- `Thought:` 만 쓰고 `Action:` / `Action Input:` 없이 응답을 끝내기
- 응답 본문을 비우기.

형식 규칙:
- `Action Input:` 한 줄에는 JSON 한 덩어리만 두세요. JSON 뒤에 설명 문장을 붙이면 실패합니다.
- 도구 결과(Observation) 없이 수치·잔고·거래내역을 지어내지 마세요.
"""

_FINUS_REACT_ACTION_INPUT_KIS = """
Action Input (Kis Trading MCP 전용 — ``kis-trading-mcp-tool`` / ``finus_account_balance``):
{{"tool_name":"domestic_stock","api_type":"inquire_balance","params":{{...}}}}
오류 시 ``find_api_detail``로 스키마 확인 후 재시도.
"""

_FINUS_REACT_ACTION_INPUT_FINUS = """
Action Input (아래 Fin-Us 래퍼 도구 — ``tool_name``·``api_type``·``domestic_stock`` 금지):
- ``mcp-trading-today-orders``: {{"trade_date":"","stock_name":"","ccld_dvsn":"00","sll_buy_dvsn":"00"}}
- ``mcp-trading-get-balance``: {{}}
- ``mcp-trading-balance-rlz-pl``: {{"stock_name":""}}
- ``finus-save-diary``: {{"title":"매매일지 YYYY-MM-DD","content":"본문"}}
- ``finus-list-diaries``: {{}}
- ``finus_market_news`` / ``finus_investor_trading`` / ``finus_disclosure``: {{"stock_name":"삼성전자"}}
"""

_FINUS_REACT_ACTION_INPUT_MIXED = """
Action Input:
- Kis Trading MCP(``kis-trading-mcp-tool``): {{"tool_name":"domestic_stock","api_type":"...","params":{{...}}}}
- Fin-Us mcp-trading·일지 도구: 위 Kis 형식 쓰지 말고 도구별 필드만 (예: ``mcp-trading-today-orders`` → {{"trade_date":"","stock_name":""}}).
"""


def _react_system_prompt_for_tools(tools) -> str:
    # 등록된 도구 이름을 공백으로 잇고 lowercase 한 뒤 substring 매칭으로 분기한다.
    # 이름 충돌 위험을 줄이기 위해 ``_STRICT_TOOL_SUBSTR`` 와 동일한 “접두어 + 하이픈” 토큰만 사용한다
    # (예: ``mcp-trading-`` 접두어로 KIS wrapped 도구를 식별, 일반 ``trading`` 이라는 단어는 매칭하지 않음).
    names = " ".join(getattr(t, "name", "") for t in (tools or [])).lower()
    has_kis = "kis-trading-mcp-tool" in names or "finus_account_balance" in names
    has_finus_wrapped = any(
        token in names
        for token in (
            "mcp-trading-today",
            "mcp-trading-get-balance",
            "mcp-trading-balance",
            "finus-save-diary",
            "finus-list-diaries",
            "finus_market_news",
            "finus_investor_trading",
            "finus_disclosure",
        )
    )
    if has_finus_wrapped and not has_kis:
        body = _FINUS_REACT_ACTION_INPUT_FINUS
    elif has_kis and not has_finus_wrapped:
        body = _FINUS_REACT_ACTION_INPUT_KIS
    else:
        body = _FINUS_REACT_ACTION_INPUT_MIXED
    return _FINUS_REACT_PROMPT_HEAD + body + _FINUS_REACT_PROMPT_TAIL


# 레거시 참조용 (패치 전 기본값).
_FINUS_REACT_SYSTEM_PROMPT = _react_system_prompt_for_tools([])


# 주어진 tools 목록에 strict 대상 도구(_STRICT_TOOL_SUBSTR)가 포함되어 있는지 판별한다.
def _strict_data_tools(tools) -> bool:
    names = " ".join(getattr(t, "name", "") for t in (tools or [])).lower()
    return any(s in names for s in _STRICT_TOOL_SUBSTR)


# NAT ReAct 모듈의 SYSTEM_PROMPT를 위의 _FINUS_REACT_SYSTEM_PROMPT로 변경합니다
def _patch_react_system_prompt(ra_mod) -> None:
    # 이미 패치된 모듈이면 중복 적용하지 않는다.
    if getattr(ra_mod, "_finus_system_prompt_patched", False):
        return
    ra_mod.SYSTEM_PROMPT = _FINUS_REACT_SYSTEM_PROMPT
    ra_mod._finus_react_prompt_for_tools = _react_system_prompt_for_tools  # type: ignore[attr-defined]
    ra_mod._finus_system_prompt_patched = True


# 도구를 한 번이라도 호출한 뒤에는 평문 최종 답변을 허용하도록 agent_node를 패치한다(사전 직접 답변은 차단 유지).
def _patch_react_agent_node_plain_final_after_tool(ra_mod, ReActAgentGraph) -> None:
    """Allow a plain final answer after at least one tool call, while blocking pre-tool direct answers."""
    # 중복 패치 방지.
    if getattr(ReActAgentGraph, "_finus_plain_final_after_tool", False):
        return
    # 이미 패치된 소스(marker 포함)면 플래그만 설정하고 종료.
    marker = "self.accept_direct_answer_without_react_format or state.agent_scratchpad"
    src = textwrap.dedent(inspect.getsource(ReActAgentGraph.agent_node))
    if marker in src:
        ReActAgentGraph._finus_plain_final_after_tool = True  # type: ignore[attr-defined]
        return
    # scratchpad가 비어있지 않으면 평문 최종 답변을 허용하도록 조건을 OR로 확장한다.
    needle = "if (self.accept_direct_answer_without_react_format and ex.missing_action and content_str"
    repl = "if ((self.accept_direct_answer_without_react_format or state.agent_scratchpad) and ex.missing_action and content_str"
    src2 = src.replace(needle, repl, 1)
    if src2 == src:
        return
    # 패치된 소스를 컴파일해 새 agent_node로 교체.
    out: dict[str, object] = {}
    exec(compile(src2, "<finus_react_agent_node_plain_final_patch>", "exec"), ra_mod.__dict__, out)
    fn = out.get("agent_node")
    if fn is None:
        return
    ReActAgentGraph.agent_node = fn  # type: ignore[method-assign]
    ReActAgentGraph._finus_plain_final_after_tool = True  # type: ignore[attr-defined]


# 위 패치들을 한 번에 적용하는 진입점: ReActAgentGraph.__init__를 감싸 strict 도구 사용 시 직접 답변을 차단한다.
def _patch_nat_react_accept_direct_for_kis_tools() -> None:
    # NAT ReAct 에이전트 모듈/그래프 클래스 로드.
    import nat.plugins.langchain.agent.react_agent.agent as ra_mod
    from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph

    _patch_react_system_prompt(ra_mod)
    _patch_react_agent_node_plain_final_after_tool(ra_mod, ReActAgentGraph)

    _orig = ReActAgentGraph.__init__
    if getattr(_orig, "_finus_patched", False):
        return

    sig = inspect.signature(_orig)
    if "accept_direct_answer_without_react_format" not in sig.parameters:
        return

    @functools.wraps(_orig)
    def _wrapped(self, llm, prompt, tools, **kwargs):  # noqa: ANN001
        allowed = {k: v for k, v in kwargs.items() if k in sig.parameters}
        is_strict = _strict_data_tools(tools)
        if is_strict and "accept_direct_answer_without_react_format" not in allowed:
            allowed["accept_direct_answer_without_react_format"] = False
        picker = getattr(ra_mod, "_finus_react_prompt_for_tools", _react_system_prompt_for_tools)
        ra_mod.SYSTEM_PROMPT = picker(tools)
        _orig(self, llm, prompt, tools, **allowed)

    _wrapped._finus_patched = True  # type: ignore[attr-defined]
    ReActAgentGraph.__init__ = _wrapped


# 모듈 import 시점에 NAT ReAct 패치를 자동 적용한다.
_patch_nat_react_accept_direct_for_kis_tools()
