"""#152: ReAct 에이전트 YAML이 계속 빌드되는지 확인하는 저렴한 오프라인 스모크 테스트.

`system_prompt`를 register.py 런타임 패치가 아니라 YAML의 `ReActAgentWorkflowConfig.system_prompt`
필드로 직접 전달하도록 옮겼다. `create_react_agent_prompt`는 내부적으로
`ReActAgentGraph.validate_system_prompt`를 호출해 `{tools}`/`{tool_names}` 플레이스홀더가
있는지 확인하고 없으면 ValueError를 던진다. 이 테스트는 실제 LLM/MCP 연결 없이(오프라인)
Config 파싱과 프롬프트 조립만으로 그 검증을 통과하는지 확인한다 - 다음 nvidia-nat 업그레이드가
`validate_system_prompt`의 요구사항을 바꾸면 여기서 바로 드러난다.

`configs/agents/*.yml`을 단독으로 로드하는 것 외에, 프로덕션이 실제로 로드하는
`configs/router.yml` / `configs/router_nomemory.yml`도 포함한다. `base:` 상속 체인 때문에
두 라우터 모두 결과적으로 여섯 react_agent 함수를 전부 포함한다. 라우터로 로드했을 때의
`system_prompt`/`tool_names`를 같은 함수를 `configs/agents/*.yml`에서 단독으로 로드했을
때와 정확히 비교한다(아래 `_assert_router_config_matches_direct_agent_config`) - 그래서
라우터 레벨에서 `system_prompt`를 다른(하지만 그 자체로는 유효한) 값으로 덮어쓰거나
`tool_names`를 바꾸는 미래의 변경도 잡는다. 참고로 `validate_system_prompt`와
`ChatPromptTemplate.input_variables` 비교만으로는 이걸 못 잡는다 - 유효하고 플레이스홀더가
멀쩡한 다른 프롬프트는 두 검사 모두 통과하기 때문이다.
"""

import re
from functools import lru_cache
from pathlib import Path

import pytest

CONFIGS_ROOT = Path(__file__).resolve().parents[1] / "configs"
AGENTS_DIR = CONFIGS_ROOT / "agents"

AGENT_FUNCTION_NAMES = [
    "diary_agent",
    "monitoring_agent",
    "news_agent",
    "recommend_agent",
    "strategy_agent",
    "trading_agent_react",
]

# (yaml 경로, 그 파일 안의 react_agent 함수 이름) - agents/*.yml을 단독으로 로드.
DIRECT_AGENT_CONFIGS = [
    (AGENTS_DIR / "diary_agent.yml", "diary_agent"),
    (AGENTS_DIR / "monitoring_agent.yml", "monitoring_agent"),
    (AGENTS_DIR / "news_agent.yml", "news_agent"),
    (AGENTS_DIR / "recommend_agent.yml", "recommend_agent"),
    (AGENTS_DIR / "strategy_agent.yml", "strategy_agent"),
    (AGENTS_DIR / "trading_agent.yml", "trading_agent_react"),
]

# function_name -> 그 함수가 정의된 agents/*.yml 파일명. 라우터로 로드한 함수를
# 같은 함수의 단독 로드 결과와 비교할 때 어느 파일을 direct 기준으로 쓸지 찾는 데 쓴다.
AGENT_YAML = {function_name: path.name for path, function_name in DIRECT_AGENT_CONFIGS}

# 프로덕션이 실제로 로드하는 두 라우터 - 각각 여섯 함수 전부를 상속 체인으로 포함한다.
ROUTER_CONFIGS = [
    (router_path, function_name)
    for router_path in (CONFIGS_ROOT / "router.yml", CONFIGS_ROOT / "router_nomemory.yml")
    for function_name in AGENT_FUNCTION_NAMES
]

ALL_CONFIGS = DIRECT_AGENT_CONFIGS + ROUTER_CONFIGS
ALL_CONFIG_IDS = [f"{path.relative_to(CONFIGS_ROOT)}::{function_name}" for path, function_name in ALL_CONFIGS]

# create_react_agent_prompt가 조립하는 ChatPromptTemplate이 실제로 받아야 하는 입력 변수.
# {tools}/{tool_names}는 system_prompt에서, question/chat_history는 USER_PROMPT에서 온다.
# 이 집합과의 등가성 검사는 존재 확인(`is not None`)보다 강한 보증이다: `{tools}`처럼 이름이
# 딱 맞는 플레이스홀더뿐 아니라 `{tool_name}`(단수 오타) 같은 낯선 플레이스홀더가 섞여 들어가도
# ChatPromptTemplate 생성 자체는 성공하고 실제 LLM 호출 시점에야 터지는데, 이 비교는 그 오타도
# 여기서 미리 잡는다.
EXPECTED_PROMPT_INPUT_VARIABLES = {"tools", "tool_names", "question", "chat_history"}


@lru_cache(maxsize=None)
def _load_direct_agent_config(function_name: str):
    """function_name이 정의된 agents/*.yml을 단독으로 로드한다.

    12개 라우터 파라미터(2개 라우터 x 6개 함수)가 각자 같은 파일을 다시 로드하던 것을
    함수당 1회로 줄인다.
    """
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.runtime.loader import load_config

    return load_config(AGENTS_DIR / AGENT_YAML[function_name]).functions[function_name]


@pytest.mark.parametrize("config_path,function_name", ALL_CONFIGS, ids=ALL_CONFIG_IDS)
def test_agent_config_builds_with_valid_system_prompt(config_path: Path, function_name: str):
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph
    from nat.plugins.langchain.agent.react_agent.agent import create_react_agent_prompt
    from nat.plugins.langchain.agent.react_agent.register import ReActAgentWorkflowConfig
    from nat.runtime.loader import load_config

    config = load_config(config_path)
    fn_config = config.functions[function_name]
    assert isinstance(fn_config, ReActAgentWorkflowConfig)

    # system_prompt가 YAML에서 실제로 채워졌는지(모듈 import 시점 패치가 아니라).
    assert fn_config.system_prompt
    assert ReActAgentGraph.validate_system_prompt(fn_config.system_prompt) is True

    # additional_instructions까지 합친 최종 프롬프트가 기대한 입력 변수로만 빌드되는지 확인한다.
    prompt = create_react_agent_prompt(fn_config)
    assert set(prompt.input_variables) == EXPECTED_PROMPT_INPUT_VARIABLES

    if config_path.parent == CONFIGS_ROOT:  # router.yml / router_nomemory.yml
        direct = _load_direct_agent_config(function_name)
        assert isinstance(direct, ReActAgentWorkflowConfig)
        assert fn_config.system_prompt == direct.system_prompt
        assert [str(t) for t in fn_config.tool_names] == [str(t) for t in direct.tool_names]


# 프롬프트가 ``…`` 로 감싸 안내하는 도구성 토큰 중 실제 tool_names에 없는 값을 걸러낸다. 이 집합에
# 들어가는 값은 KIS API 파라미터/값(tool_name, api_type, ...)처럼 도구 이름이 아닌 것으로 확인된
# 토큰뿐이어야 한다 - 여기에 실수로 실제 도구 이름을 넣으면 그 이름에 대한 검사가 조용히 꺼진다.
# inquire_account_balance/inquire_daily_itemchartprice/inqr_dvsn_1/overseas_stock은
# additional_instructions까지 스캔 범위를 넓히면서 새로 드러난 항목 - domestic_stock/env_dv와
# 같은 부류로, kis-trading-mcp-tool 하나에 대한 KIS TR(api_type)/tool_name 파라미터 값이지
# NAT에 등록된 도구 이름이 아니다. 여섯 에이전트의 tool_names와 대조해 겹치지 않음을 확인했다.
KNOWN_NON_TOOL_TOKENS = {
    "tool_name",
    "api_type",
    "domestic_stock",
    "overseas_stock",
    "params",
    "find_api_detail",
    "period",
    "stock_name",
    "env_dv",
    "inqr_dvsn_1",
    "inquire_account_balance",
    "inquire_daily_itemchartprice",
}


@pytest.mark.parametrize("config_path,function_name", ALL_CONFIGS, ids=ALL_CONFIG_IDS)
def test_system_prompt_only_names_real_tools(config_path: Path, function_name: str):
    """system_prompt + additional_instructions가 ``도구이름`` 형태로 안내하는 이름이 실제
    tool_names에 있는지 확인한다.

    두 필드를 함께 스캔하는 이유: 벤더 `create_react_agent_prompt`(agent.py:489-490)가
    `if config.additional_instructions: prompt_str += f" {{config.additional_instructions}}"`로
    system_prompt 뒤에 그대로 이어붙여 모델에게 보낸다 - additional_instructions는 별도 필드가
    아니라 같은 프롬프트 문자열의 일부다. system_prompt만 스캔하면 strategy_agent.yml의
    additional_instructions에 있던 ``get_market_news``/``get_disclosure_signal``(실제 이름은
    mcp-news-get-market-news/mcp-dart-get-disclosure-signal) 같은 오타가 green으로 통과한다.

    옛 죽은 프롬프트(register.py 몽키패치)에는 등록조차 안 된 이름(finus_market_news 등 common.yml의
    `_type` 값)이 섞여 있었고, 이제는 매 턴 살아 있는 텍스트라 오타/유령 이름이 곧바로
    TOOL_NOT_FOUND_ERROR_MESSAGE로 이어져 max_tool_calls 사이클을 하나 버린다.
    """
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.runtime.loader import load_config

    config = load_config(config_path)
    fn_config = config.functions[function_name]

    tools = {str(t) for t in fn_config.tool_names}
    # additional_instructions는 스키마상 `str | None`(기본값 None)이라 정의하지 않은 에이전트가
    # 있을 수 있다 - 현재 6개 에이전트는 모두 값을 채워 두지만, agent.py의
    # `if config.additional_instructions:` 가드와 동일하게 None을 방어적으로 처리한다.
    prompt_text = fn_config.system_prompt + (f" {fn_config.additional_instructions}" if fn_config.additional_instructions else "")
    quoted = set(re.findall(r"``([\w.-]+)``", prompt_text)) - KNOWN_NON_TOOL_TOKENS
    assert quoted <= tools, f"프롬프트가 보유하지 않은 도구를 안내함: {sorted(quoted - tools)}"


# monitoring/trading은 tool_names가 완전히 같아(kis-trading-mcp-tool-readonly,
# mcp-news-get-market-news, mcp-dart-get-disclosure-signal, add_user_memory, get_user_memory)
# system_prompt를 의도적으로 바이트 단위로 동일하게 유지한다.
# strategy_agent도 같은 조회 전용 도구를 쓰지만 채팅 주문 안내(#380)가 없는
# react_kis_readonly.md를 쓴다 — /v1/propose-order의 제안 JSON에 주문 명령 안내가 섞이지
# 않게 하려는 분기라 비교 대상에서 제외한다.
# #284 이후로는 둘 다 configs/prompts/react_kis_chat.md(#380 전에는 react_kis_full.md) 하나를
# file://로 참조하므로 복제가 사라졌고, 이 동일성은 구조적으로 보장된다 - 아래 테스트는
# 그래서 지금은 실패할 수 없다.
_IDENTICAL_SYSTEM_PROMPT_AGENTS = [
    ("monitoring_agent.yml", "monitoring_agent"),
    ("trading_agent.yml", "trading_agent_react"),
]


# ---------------------------------------------------------------------------
# #66·#380 회귀 가드 — KIS 함수의 타입과 에이전트별 tool_names 참조를 고정
# ---------------------------------------------------------------------------

_ROUTER_PATHS = (CONFIGS_ROOT / "router.yml", CONFIGS_ROOT / "router_nomemory.yml")

# kis-trading-mcp-tool-readonly(조회 전용)가 scope에 있어야 하는 설정 — 전부다.
# diary_agent는 tool_names에 포함하지 않지만 scope에 정의되어 있으므로 타입을 고정한다.
# 단독 로드 + 프로덕션 라우터 양쪽에서 검사한다.
_KIS_CONFIGS = [*(path for path, _ in DIRECT_AGENT_CONFIGS), *_ROUTER_PATHS]
_KIS_CONFIG_IDS = [p.name for p in _KIS_CONFIGS]


@pytest.mark.parametrize("config_path", _KIS_CONFIGS, ids=_KIS_CONFIG_IDS)
def test_kis_tool_is_readonly_in_every_config(config_path: Path):
    """#66: kis-trading-mcp-tool-readonly는 어떤 설정에서 로드해도 finus_account_balance_readonly여야 한다."""
    import nat_finus_nat.register  # noqa: F401
    from nat.runtime.loader import load_config
    from nat_finus_nat.finus_api import FinusAccountBalanceReadonlyConfig

    config = load_config(config_path)
    tool_config = config.functions["kis-trading-mcp-tool-readonly"]
    assert isinstance(tool_config, FinusAccountBalanceReadonlyConfig), (
        f"{config_path.name}: kis-trading-mcp-tool-readonly는 finus_account_balance_readonly여야 합니다. "
        f"실제 타입: {type(tool_config).__name__}"
    )


@pytest.mark.parametrize("config_path", _KIS_CONFIGS, ids=_KIS_CONFIG_IDS)
def test_full_permission_kis_tool_is_not_registered(config_path: Path):
    """#380: 전체 권한 이름 kis-trading-mcp-tool은 어떤 설정에도 없어야 한다.

    #66 시절에는 trading/monitoring이 이 이름으로 전체 권한(주문 가능) 래퍼를 참조했고,
    이 테스트 자리는 "전체 권한이어야 한다"를 고정했다. #380으로 채팅 주문 경로를 닫으면서
    반대로 뒤집었다. 이름만 보는 이 검사는 입구일 뿐이고, 다른 이름으로 같은 타입을 싣는
    경우까지는 아래 전수 스캔(``test_no_config_can_reach_an_order_capable_tool``)이 잡는다.
    """
    import nat_finus_nat.register  # noqa: F401
    from nat.runtime.loader import load_config

    config = load_config(config_path)
    assert "kis-trading-mcp-tool" not in config.functions


# 각 에이전트의 tool_names가 권한에 맞는 KIS 도구를 참조하는지 고정한다.
# 타입만 검사하면 tool_names 드리프트를 놓칠 수 있다 — 참조 고정으로 이중 보증한다.
_KIS_AGENT_FUNCTIONS = (
    "trading_agent_react", "monitoring_agent", "news_agent", "recommend_agent", "strategy_agent",
)
_AGENT_KIS_TOOL_REFS = [
    # (config_path, agent_fn, expected_tool_name_in_tool_names) — 단독 로드는 그 함수가 정의된 파일에서.
    (config_path, agent_fn, "kis-trading-mcp-tool-readonly")
    for agent_fn in _KIS_AGENT_FUNCTIONS
    for config_path in (AGENTS_DIR / AGENT_YAML[agent_fn], *_ROUTER_PATHS)
]
_AGENT_KIS_TOOL_REF_IDS = [f"{p.name}::{a}::{t}" for p, a, t in _AGENT_KIS_TOOL_REFS]


@pytest.mark.parametrize("config_path,agent_fn,expected_tool", _AGENT_KIS_TOOL_REFS, ids=_AGENT_KIS_TOOL_REF_IDS)
def test_agent_references_expected_kis_tool(config_path: Path, agent_fn: str, expected_tool: str):
    """#66·#380: 에이전트 tool_names가 조회 전용 KIS 도구 하나만 참조하는지 고정한다.

    #380 전에는 trading/monitoring이 전체 권한 kis-trading-mcp-tool을 참조했다. 지금은
    다섯 에이전트 모두 kis-trading-mcp-tool-readonly다. 라우터 설정에서도 동일하게 검사한다.
    """
    import nat_finus_nat.register  # noqa: F401
    from nat.runtime.loader import load_config

    config = load_config(config_path)
    tool_names = [str(t) for t in config.functions[agent_fn].tool_names]
    kis_tools = [t for t in tool_names if t.startswith("kis-trading-mcp-tool")]
    assert kis_tools == [expected_tool], (
        f"{config_path.name}::{agent_fn}: KIS 도구 참조는 [{expected_tool!r}] 하나여야 합니다. 실제: {kis_tools}"
    )


# ---------------------------------------------------------------------------
# #66 allowlist 단위 테스트 — _is_readonly_api_type 판정 고정
# fail-closed 검증: allowlist 판정을 무조건 True로 뒤집으면 이 테스트가 red.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("api_type", [
    "inquire_balance",
    "inquire_price",
    "inquire_account_balance",
    "inquire_daily_itemchartprice",
    "inquire_investor",
    "search_stock_info",
    "find_api_detail",
    "pension_inquire_psbl_order",  # 접두사 밖 조회 TR — 괴리 가드(#365) 오인 방지 (PR #379 리뷰)
], ids=lambda x: x)
@pytest.mark.parametrize("tool_name", ["domestic_stock", "overseas_stock"])
def test_readonly_api_type_allows_read_only(api_type: str, tool_name: str):
    """#66: 조회 계열 api_type은 _is_readonly_api_type이 True를 반환해야 한다.

    #66의 접두사·정확 값은 상품과 무관하다 — 국내주식 전용 목록(#380)이 생긴 뒤에도
    다른 상품에서 그대로 허용되는지 함께 본다. 국내주식 전용 목록의 판정은
    ``test_kis_readonly_allowlist.py``가 고정한다.
    """
    from nat_finus_nat.finus_api import _is_readonly_api_type
    assert _is_readonly_api_type(api_type, tool_name=tool_name) is True, (
        f"{tool_name}/{api_type!r}는 조회 전용 허용 목록에 포함되어야 합니다."
    )


@pytest.mark.parametrize("api_type", [
    "order_cash",
    "order_sell",
    "modify_order",
    "cancel_order",
    "overseas_stock_order",   # order_ 접두사 없는 가상의 주문 TR — fail-closed 핵심 케이스
    "unknown_operation",      # 목록에 없는 임의 api_type — fail-closed 핵심 케이스
    "",                       # 빈 문자열
], ids=lambda x: x or "<empty>")
def test_readonly_api_type_blocks_non_allowlisted(api_type: str):
    """#66: 허용 목록에 없는 api_type은 _is_readonly_api_type이 False를 반환해야 한다.

    ``unknown_operation``, ``overseas_stock_order`` 케이스가 fail-closed 핵심이다.
    allowlist 판정을 무조건 True로 교체하면 이 테스트들이 red가 된다.

    허용 범위가 가장 넓은 국내주식(#380 전용 목록 포함)으로 판정한다 — 거기서 막히면 다른
    상품에서도 막힌다.
    """
    from nat_finus_nat.finus_api import _is_readonly_api_type
    assert _is_readonly_api_type(api_type, tool_name="domestic_stock") is False, (
        f"{api_type!r}는 조회 전용 허용 목록에서 차단되어야 합니다."
    )


# ---------------------------------------------------------------------------
# #66 allowlist 단위 테스트 — _is_readonly_tool_name 판정 고정
# fail-closed 검증: allowlist 판정을 무조건 True로 뒤집으면 이 테스트가 red.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", [
    "domestic_stock",
    "overseas_stock",
    "domestic_bond",
    "domestic_futureoption",
    "overseas_futureoption",
    "elw",
    "etfetn",
], ids=lambda x: x)
def test_readonly_tool_name_allows_asset_classes(tool_name: str):
    """#66: 허용 자산군 7종은 _is_readonly_tool_name이 True를 반환해야 한다."""
    from nat_finus_nat.finus_api import _is_readonly_tool_name
    assert _is_readonly_tool_name(tool_name) is True, (
        f"{tool_name!r}는 조회 전용 tool_name 허용 목록에 포함되어야 합니다."
    )


@pytest.mark.parametrize("tool_name", [
    "auth",     # 인증 상태 변경 가능 — 핵심 차단 케이스
    "foo_bar",  # 알 수 없는 tool_name — fail-closed 핵심 케이스
    "",         # 빈 문자열
], ids=lambda x: x or "<empty>")
def test_readonly_tool_name_blocks_non_allowlisted(tool_name: str):
    """#66: 허용 목록 밖의 tool_name은 _is_readonly_tool_name이 False를 반환해야 한다.

    ``auth`` 케이스가 핵심: 인증 상태를 바꿀 수 있어 조회 전용 래퍼에서 제외한다.
    allowlist 판정을 무조건 True로 교체하면 이 테스트들이 red가 된다.
    """
    from nat_finus_nat.finus_api import _is_readonly_tool_name
    assert _is_readonly_tool_name(tool_name) is False, (
        f"{tool_name!r}는 조회 전용 허용 목록에서 차단되어야 합니다."
    )


@pytest.mark.parametrize("tool_name,expected", [
    ("DOMESTIC_STOCK", True),    # 대문자 → 허용 (대소문자 무시)
    ("Overseas_Stock", True),    # 혼합 대소문자 → 허용
    ("  etfetn  ", True),        # 앞뒤 공백 → 허용 (strip 처리)
    ("AUTH", False),             # 대문자 auth → 차단
    ("DOMESTIC_BOND ", True),    # 뒤 공백 있어도 strip 후 허용
], ids=lambda x: repr(x) if isinstance(x, str) else str(x))
def test_readonly_tool_name_case_and_whitespace(tool_name: str, expected: bool):
    """#66: _is_readonly_tool_name은 대소문자·앞뒤 공백을 정규화해 판정해야 한다."""
    from nat_finus_nat.finus_api import _is_readonly_tool_name
    assert _is_readonly_tool_name(tool_name) is expected, (
        f"{tool_name!r}: expected={expected}"
    )


# ---------------------------------------------------------------------------
# #220 정규화 일원화 — _prepare_kis_trading_mcp_call 이 tool_name을 소문자로 반환하는지 고정
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("DOMESTIC_STOCK", "domestic_stock"),
    ("Overseas_Stock", "overseas_stock"),
    ("  ETFETN  ", "etfetn"),
], ids=lambda x: repr(x) if isinstance(x, str) else str(x))
def test_prepare_kis_normalizes_tool_name_to_lowercase(raw: str, expected: str):
    """#220: _prepare_kis_trading_mcp_call은 대소문자·앞뒤 공백을 정규화해 소문자 tool_name을 반환해야 한다.

    _is_readonly_tool_name 게이트 통과 여부만이 아니라 실제로 MCP 에 전달되는 값을 검증한다.
    정규화가 없으면 "DOMESTIC_STOCK"은 게이트를 통과하지만 MCP 서버가 unknown tool로 거부한다.
    """
    from nat_finus_nat.finus_api import (
        FinusAccountBalanceConfig,
        KisTradingMcpCallInput,
        _prepare_kis_trading_mcp_call,
    )

    config = FinusAccountBalanceConfig()
    inp = KisTradingMcpCallInput(tool_name=raw, api_type="inquire_balance", params={})
    result = _prepare_kis_trading_mcp_call(inp, config)
    assert isinstance(result, tuple), (
        f"{raw!r} → 에러 문자열이 아닌 (tool_name, arguments) 튜플이어야 한다: {result!r}"
    )
    actual_tool_name, _ = result
    assert actual_tool_name == expected, (
        f"{raw!r} → {expected!r} 정규화 기대, 실제: {actual_tool_name!r}"
    )


# ---------------------------------------------------------------------------
# #220 trading_tool_name 설정 시점 검증 — 허용 목록 밖의 값은 ValidationError
# ---------------------------------------------------------------------------

def test_trading_tool_name_invalid_raises_validation_error():
    """#220: trading_tool_name에 허용 목록 밖의 값을 넣으면 Config 로드 시점에 ValidationError가 발생해야 한다.

    FINUS_KIS_TRADING_TOOL_NAME 환경변수 오설정으로 readonly 에이전트가 전면 차단되는 사고를
    런타임 첫 호출 전에 잡는다. allowlist 판정을 항상 통과로 뒤집으면 이 테스트가 red가 된다.
    """
    from pydantic import ValidationError
    from nat_finus_nat.finus_api import FinusAccountBalanceConfig

    with pytest.raises(ValidationError, match="trading_tool_name"):
        FinusAccountBalanceConfig(trading_tool_name="invalid_tool_xyz")


@pytest.mark.parametrize("name", [
    "domestic_stock", "overseas_stock", "domestic_bond",
    "domestic_futureoption", "overseas_futureoption", "elw", "etfetn", "auth",
], ids=lambda x: x)
def test_trading_tool_name_valid_values_accepted(name: str):
    """#220: 허용 목록 8종은 trading_tool_name에 지정해도 ValidationError 없이 로드된다."""
    from nat_finus_nat.finus_api import FinusAccountBalanceConfig

    config = FinusAccountBalanceConfig(trading_tool_name=name)
    assert config.trading_tool_name == name


def test_readonly_config_rejects_auth():
    """#225: FinusAccountBalanceReadonlyConfig은 auth를 설정 시점에 ValidationError로 차단해야 한다.

    readonly 래퍼는 런타임에 auth를 차단하므로, 설정 시점에도 받지 않는다.
    두 YAML이 같은 환경변수(FINUS_KIS_TRADING_TOOL_NAME)를 공유하므로 실제로 발생 가능한 경로다.
    """
    from pydantic import ValidationError
    from nat_finus_nat.finus_api import FinusAccountBalanceReadonlyConfig

    with pytest.raises(ValidationError, match="trading_tool_name"):
        FinusAccountBalanceReadonlyConfig(trading_tool_name="auth")


@pytest.mark.parametrize("name", [
    "domestic_stock", "overseas_stock", "domestic_bond",
    "domestic_futureoption", "overseas_futureoption", "elw", "etfetn",
], ids=lambda x: x)
def test_readonly_config_accepts_asset_classes(name: str):
    """#225: readonly Config는 자산군 7종을 설정 시점에 허용해야 한다."""
    from nat_finus_nat.finus_api import FinusAccountBalanceReadonlyConfig

    config = FinusAccountBalanceReadonlyConfig(trading_tool_name=name)
    assert config.trading_tool_name == name


# ---------------------------------------------------------------------------
# #273 회귀 가드 — 두 라우터의 최상위 workflow가 각주 부착 지점을 공유하는지
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("config_path", _ROUTER_PATHS, ids=[p.name for p in _ROUTER_PATHS])
def test_router_workflow_is_the_reasoning_trace_agent(config_path: Path):
    """#273: 두 라우터 모두 최상위 workflow가 finus_reasoning_trace_agent여야 한다.

    각주(#260)는 이 한 지점에서만 붙는다. 위에 다른 래퍼를 얹으면 — 특히 vendor
    ``auto_memory_agent``처럼 ``(input_message: str) -> str`` 시그니처인 래퍼를 —
    ``routed_agent``/``tools_used``가 그 경계에서 버려지고, backend는 필드가 없으면
    각주를 **조용히** 생략한다. 관측 형태가 "각주가 안 나온다"뿐이라 런타임에는
    드러나지 않으므로 config 모양을 여기서 고정한다.
    """
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.runtime.loader import load_config
    from nat_finus_nat.agents import FinusReasoningTraceAgentConfig

    workflow = load_config(config_path).workflow
    assert isinstance(workflow, FinusReasoningTraceAgentConfig), (
        f"{config_path.name}: 최상위 workflow는 finus_reasoning_trace_agent여야 합니다. "
        f"실제 타입: {type(workflow).__name__}"
    )


@pytest.mark.parametrize("config_path", _ROUTER_PATHS, ids=[p.name for p in _ROUTER_PATHS])
def test_router_does_not_store_conversation_text_in_memory(config_path: Path):
    """#397: 어떤 라우터도 대화 전문을 메모리에 쓰는 구성 요소를 싣지 않는다.

    vendor ``auto_memory_agent``는 사용자 발화와 답변 전문(잔고·금액 포함)을 매 턴 저장하고,
    vendor ``add_memory``는 LLM이 고른 자유 텍스트를 저장한다. 메모리 모드에서 쓰기 경로는
    허용목록 게이트가 있는 ``finus_user_preferences`` 하나여야 한다. 메모리 백엔드도 vendor
    ``mem0_memory``(HTTP 클라이언트 → 외부 서버/api.mem0.ai)가 아니라 로컬 모드여야 한다.

    뮤테이션: router.yml의 workflow를 옛 ``memory_router_agent``(auto_memory_agent)로 되돌리거나
    ``add_user_memory``를 ``_type: add_memory``로 되돌리면 red.
    """
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.runtime.loader import load_config

    config = load_config(config_path)
    assert str(config.workflow.inner_agent_name) == "transcript_router_agent"

    function_types = {str(name): type(fn).static_type() for name, fn in (config.functions or {}).items()}
    forbidden = {name: t for name, t in function_types.items() if t in {"auto_memory_agent", "add_memory", "get_memory"}}
    assert not forbidden, f"{config_path.name}: 대화 텍스트를 메모리에 쓸 수 있는 구성 요소: {forbidden}"

    memory_types = {type(mem).static_type() for mem in (config.memory or {}).values()}
    assert memory_types <= {"finus_mem0_local_memory"}, f"{config_path.name}: 로컬 모드가 아닌 메모리: {memory_types}"

    expected_preferences = "finus_user_preferences" if config_path.name == "router.yml" else "finus_user_preferences_disabled"
    assert function_types["user_preferences"] == expected_preferences
    assert function_types["recommend_branch_agent"] == "finus_risk_profile_branch"


def test_kis_agents_share_identical_system_prompt():
    """monitoring/trading의 최종 system_prompt는 의도적으로 바이트 동일하다.

    #284로 둘 다 같은 프롬프트 파일(#380부터 configs/prompts/react_kis_chat.md)을 참조하게 되면서 이 테스트는 현재
    실패할 수 없다 - 참조가 갈라지는 시나리오는 test_agent_references_expected_prompt_file이
    먼저 잡는다. 그래도 남겨 두는 이유는 이 테스트만이 "파일 참조"가 아니라 **로드된 최종
    문자열**을 비교하기 때문이다: 프롬프트를 다시 블록 스칼라로 인라인하거나 두 에이전트를
    서로 다른 파일로 갈라 놓는 변경이 오면, 그 결과가 실제로 달라졌는지를 여기서 확인한다.
    """
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.runtime.loader import load_config

    prompts = {
        function_name: load_config(AGENTS_DIR / file_name).functions[function_name].system_prompt
        for file_name, function_name in _IDENTICAL_SYSTEM_PROMPT_AGENTS
    }
    baseline_name, baseline_prompt = next(iter(prompts.items()))
    diverged = [name for name, prompt in prompts.items() if prompt != baseline_prompt]
    assert not diverged, f"{baseline_name} 기준으로 프롬프트가 분기한 에이전트: {diverged}"


# ---------------------------------------------------------------------------
# #284 회귀 가드 — 프롬프트 파일의 존재와 에이전트별 참조 대상을 고정
# ---------------------------------------------------------------------------

PROMPTS_DIR = CONFIGS_ROOT / "prompts"

# (yaml 파일명, 그 안의 react_agent 함수, 참조해야 하는 프롬프트 파일명).
# trading/monitoring이 react_kis_chat.md를 공유하는 것이 바로 위
# test_kis_agents_share_identical_system_prompt가 지키는 "바이트 동일"의 구조적 근거다 -
# 둘 중 하나가 다른 파일을 가리키게 바뀌면 여기서 먼저 잡힌다. react_kis_chat.md는 #380 전의
# react_kis_full.md(전체 권한 도구 안내)를 대체한다 — 조회 전용 도구 + 채팅 주문 안내.
_AGENT_PROMPT_FILES = [
    ("trading_agent.yml", "trading_agent_react", "react_kis_chat.md"),
    ("monitoring_agent.yml", "monitoring_agent", "react_kis_chat.md"),
    ("strategy_agent.yml", "strategy_agent", "react_kis_readonly.md"),
    ("news_agent.yml", "news_agent", "react_news.md"),
    ("recommend_agent.yml", "recommend_agent", "react_recommend.md"),
    ("diary_agent.yml", "diary_agent", "react_diary.md"),
]
_AGENT_PROMPT_FILE_IDS = [f"{yml}::{fn}::{md}" for yml, fn, md in _AGENT_PROMPT_FILES]

_EXPECTED_PROMPT_FILES = {md for _, _, md in _AGENT_PROMPT_FILES}


def test_prompt_files_exist_and_are_not_empty():
    """#284: 참조 대상 프롬프트 파일 5개가 실재하고 비어 있지 않아야 한다.

    파일이 없으면 load_config가 FileNotFoundError로 죽으므로 다른 테스트도 red가 되지만,
    그때 실패 메시지는 "어느 파일이 왜 없는지"를 가리키지 않는다. 여기서 먼저 잡는다.
    `configs/prompts/`에 참조되지 않는 고아 파일이 남는 것도 함께 막는다.
    """
    actual = {p.name for p in PROMPTS_DIR.glob("*.md")}
    assert actual == _EXPECTED_PROMPT_FILES, (
        f"configs/prompts/*.md 목록이 기대와 다릅니다. "
        f"누락={sorted(_EXPECTED_PROMPT_FILES - actual)} 고아={sorted(actual - _EXPECTED_PROMPT_FILES)}"
    )
    empty = [name for name in sorted(actual) if not (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()]
    assert not empty, f"내용이 빈 프롬프트 파일: {empty}"


@pytest.mark.parametrize("yaml_name,function_name,prompt_file", _AGENT_PROMPT_FILES, ids=_AGENT_PROMPT_FILE_IDS)
def test_agent_references_expected_prompt_file(yaml_name: str, function_name: str, prompt_file: str):
    """#284: 각 에이전트 YAML의 `system_prompt`가 의도한 프롬프트 파일을 `file://`로 참조해야 한다.

    `load_config`는 `file://`를 이미 내용으로 치환해 돌려주므로 참조 자체를 볼 수 없다 - 그래서
    로더를 거치지 않고 `yaml.safe_load`로 원문을 읽는다. 이 검사가 없으면 프롬프트를 다시
    블록 스칼라로 인라인해 되돌리거나(A-2 되돌리기), strategy가 실수로 채팅 주문 안내가
    든 `react_kis_chat.md`를 가리키게 바뀌어도 - 두 프롬프트 모두 그 자체로는 유효하므로 -
    다른 테스트가 전부 green이다.
    """
    import yaml

    raw = yaml.safe_load((AGENTS_DIR / yaml_name).read_text(encoding="utf-8"))
    system_prompt = raw["functions"][function_name]["system_prompt"]
    assert system_prompt == f"file://../prompts/{prompt_file}", (
        f"{yaml_name}::{function_name}: system_prompt는 'file://../prompts/{prompt_file}' 여야 합니다. "
        f"실제: {system_prompt!r}"
    )


# ---------------------------------------------------------------------------
# #380 전수 스캔 가드 — 어떤 설정도 주문 가능한 도구에 닿지 않는다
# ---------------------------------------------------------------------------
#
# 위 참조 고정(_AGENT_KIS_TOOL_REFS)은 **아는 에이전트·아는 이름**만 본다. 새 에이전트 YAML을
# 추가하거나 전체 권한 타입을 다른 이름으로 등록하면 그대로 green이다. 여기서는 configs/
# 아래 YAML을 **전부** 로드해 등록된 함수 하나하나의 타입을 본다.

# 새 파일이 생기면 자동으로 스캔 대상이 된다. 목록을 손으로 적지 않는 것이 이 가드의 요점이다.
_SCANNED_CONFIG_FILES = sorted(CONFIGS_ROOT.rglob("*.yml"))

# 주문을 낼 수 없다고 **검토한** 함수 타입(`_type`). fail-closed다 — 여기 없는 타입이 설정에
# 나타나면 실패한다. 새 도구 타입을 붙일 때는 그 도구가 주문(또는 주문 도구 호출)에 닿는지
# 확인하고 근거와 함께 여기에 추가한다. 전체 권한 KIS 래퍼 finus_account_balance는 **넣지
# 않는다** — 주문 가능한 유일한 타입이다(#380).
_REVIEWED_NON_ORDER_TYPES = {
    # KIS Trading MCP 조회 전용 — tool_name·api_type 허용 목록 fail-closed (#66, #380)
    "finus_account_balance_readonly",
    # fin-us/mcp-trading stdio — 호출할 MCP 도구 이름을 코드에 고정한 조회(place_order에 닿지 않는다)
    "finus_mcp_trading_get_balance",
    "finus_mcp_trading_balance_rlz_pl",
    "finus_mcp_trading_today_orders",
    # 위 세 조회만 코드에 고정해 차례로 부르는 묶음(#405) — 일지 저장·주문 도구를 부르지 않는다
    "finus_mcp_trading_diary_snapshot",
    # 공개 정보 MCP(뉴스·공시·실적)
    "finus_market_news",
    "finus_disclosure_signal",
    "finus_earnings_report",
    # backend 매매일지 — DB에 쓰지만 주문이 아니다
    "finus_save_diary",
    "finus_list_diaries",
    # 메모리
    "add_memory",
    "get_memory",
    "finus_memory_disabled",
    "auto_memory_agent",
    # 사용자 선호 메모리(#397) — 허용목록 선호 값만 읽고 쓴다. KIS에 닿지 않는다
    "finus_user_preferences",
    "finus_user_preferences_disabled",
    "finus_user_memory_get",
    "finus_user_memory_add_refused",
    # 에이전트·라우팅 래퍼 — 스스로 외부를 부르지 않고 tool_names의 도구만 부른다(아래에서 따로 검사)
    "react_agent",
    # react_agent와 같은 도구 목록만 쓰고 첫 턴 강제 대상도 그 부분집합으로 검증한다 (#394)
    "finus_tool_first_react_agent",
    "fe_branch",
    "finus_risk_profile_branch",
    "finus_supervisor_agent",
    "finus_sqlite_transcript_agent",
    "finus_reasoning_trace_agent",
    # 주문 검증자(#299) — 도구 없이 판정만 돌려준다
    "finus_order_verifier",
    # agents/*.yml 단독 로드 시의 빈 workflow
    "EmptyFunctionConfig",
}


def _registered_components(config) -> list[tuple[str, str, object]]:
    """(구역, 이름, 설정) — 함수·함수 그룹·workflow 전부. 함수 그룹(MCP 클라이언트 등)도
    MCP 도구를 에이전트에 직접 노출할 수 있어 같이 본다."""
    items: list[tuple[str, str, object]] = [
        ("functions", str(name), fn) for name, fn in (config.functions or {}).items()
    ]
    items += [("function_groups", str(name), fg) for name, fg in (config.function_groups or {}).items()]
    items.append(("workflow", "workflow", config.workflow))
    return items


def test_config_scan_is_not_empty():
    """스캔 대상이 0개면 아래 파라미터 테스트는 **skip**으로 조용히 사라진다 — 여기서 실패시킨다.

    뮤테이션: ``_SCANNED_CONFIG_FILES``의 glob을 ``*.yaml``로 바꾸면 red.
    """
    scanned = set(_SCANNED_CONFIG_FILES)
    expected = {CONFIGS_ROOT / "common.yml", *_ROUTER_PATHS, *(path for path, _ in DIRECT_AGENT_CONFIGS)}
    assert expected <= scanned, f"스캔에서 빠진 프로덕션 설정: {sorted(p.name for p in expected - scanned)}"


@pytest.mark.parametrize(
    "config_path", _SCANNED_CONFIG_FILES, ids=[str(p.relative_to(CONFIGS_ROOT)) for p in _SCANNED_CONFIG_FILES]
)
def test_no_config_can_reach_an_order_capable_tool(config_path: Path):
    """#380: 어떤 설정도 주문 가능한 도구를 등록하거나 참조하지 않는다.

    세 가지를 본다.

    1. 등록된 함수가 전체 권한 KIS 래퍼(``FinusAccountBalanceConfig``이면서 조회 전용
       서브클래스가 아님)가 아니다 — 이름과 무관하게 타입으로 판정한다.
    2. 모든 타입이 검토 목록(``_REVIEWED_NON_ORDER_TYPES``)에 있다 — 주문할 수 있는 새 도구
       타입이 검토 없이 들어오지 못한다.
    3. 에이전트의 ``tool_names``가 전부 이 설정에 등록된 이름이다 — ``kis-trading-mcp-tool``처럼
       등록이 사라진 이름을 참조하면 로드는 되지만 빌드에서야 터지므로 여기서 잡는다.

    뮤테이션: common.yml에 ``kis-order-tool: {_type: finus_account_balance, …}``을 다시 넣으면
    8개 설정 전부 red. ``agents/``에 같은 등록만 담은 새 YAML을 추가해도 그 파일이 red.
    trading_agent.yml의 tool_names를 ``kis-trading-mcp-tool``로 되돌리면 3번에서 red.
    """
    import nat_finus_nat.register  # noqa: F401
    from nat.runtime.loader import load_config
    from nat_finus_nat.finus_api import FinusAccountBalanceConfig, FinusAccountBalanceReadonlyConfig

    config = load_config(config_path)
    components = _registered_components(config)

    order_capable = [
        f"{section}:{name}"
        for section, name, cfg in components
        if isinstance(cfg, FinusAccountBalanceConfig) and not isinstance(cfg, FinusAccountBalanceReadonlyConfig)
    ]
    assert not order_capable, f"주문 가능한 전체 권한 KIS 래퍼가 등록돼 있습니다: {order_capable}"

    unreviewed = sorted(
        f"{section}:{name} (_type={type(cfg).static_type()})"
        for section, name, cfg in components
        if type(cfg).static_type() not in _REVIEWED_NON_ORDER_TYPES
    )
    assert not unreviewed, (
        f"주문 가능 여부를 검토하지 않은 도구 타입: {unreviewed}. 주문에 닿지 않는지 확인한 뒤 "
        "_REVIEWED_NON_ORDER_TYPES에 근거와 함께 추가하세요."
    )

    registered = {name for section, name, _ in components if section != "workflow"}
    dangling = sorted(
        f"{name} -> {tool}"
        for section, name, cfg in components
        for tool in (str(t) for t in getattr(cfg, "tool_names", None) or [])
        if tool not in registered
    )
    assert not dangling, f"등록되지 않은 도구를 참조하는 에이전트: {dangling}"
