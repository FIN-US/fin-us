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


# monitoring/trading은 tool_names가 완전히 같아(kis-trading-mcp-tool,
# mcp-news-get-market-news, mcp-dart-get-disclosure-signal, add_user_memory, get_user_memory)
# system_prompt를 의도적으로 바이트 단위로 동일하게 유지한다.
# #66 수정으로 strategy_agent는 kis-trading-mcp-tool-readonly를 tool_names에 쓰게 되어
# system_prompt가 이 둘과 분기했으므로 비교 대상에서 제외한다.
# 파일이 나뉘어 있어 YAML 앵커로 공유할 수 없으니 복제 자체는 불가피하지만, 한 파일만
# 고치고 나머지를 빠뜨리는 드리프트는 이 테스트가 잡는다.
_IDENTICAL_SYSTEM_PROMPT_AGENTS = [
    ("monitoring_agent.yml", "monitoring_agent"),
    ("trading_agent.yml", "trading_agent_react"),
]


# ---------------------------------------------------------------------------
# #66 회귀 가드 — 두 KIS 함수의 타입과 에이전트별 tool_names 참조를 고정
# ---------------------------------------------------------------------------

_ROUTER_PATHS = (CONFIGS_ROOT / "router.yml", CONFIGS_ROOT / "router_nomemory.yml")

# kis-trading-mcp-tool-readonly(조회 전용)가 scope에 있어야 하는 설정.
# diary_agent는 tool_names에 포함하지 않지만 scope에 정의되어 있으므로 타입을 고정한다.
# 단독 로드 + 프로덕션 라우터 양쪽에서 검사한다 — 이름을 분리했으므로 라우터 deep-merge가
# trading/monitoring의 kis-trading-mcp-tool(전체 권한)을 덮어쓰지 않음을 함께 보증한다.
_NON_TRADING_KIS_CONFIGS = [
    AGENTS_DIR / "news_agent.yml",
    AGENTS_DIR / "recommend_agent.yml",
    AGENTS_DIR / "strategy_agent.yml",
    AGENTS_DIR / "diary_agent.yml",
    *_ROUTER_PATHS,
]

# kis-trading-mcp-tool(전체 권한 finus_account_balance)이어야 하는 설정.
# 단독 로드뿐 아니라 프로덕션 라우터도 포함 — 이름 분리로 news_agent.yml의 readonly 함수가
# 이 함수를 덮어쓰지 않음을 라우터 병합 결과에서 직접 확인한다.
_TRADING_KIS_CONFIGS = [
    AGENTS_DIR / "trading_agent.yml",
    AGENTS_DIR / "monitoring_agent.yml",
    *_ROUTER_PATHS,
]

_NON_TRADING_KIS_IDS = [p.name for p in _NON_TRADING_KIS_CONFIGS]
_TRADING_KIS_IDS = [p.name for p in _TRADING_KIS_CONFIGS]


@pytest.mark.parametrize("config_path", _NON_TRADING_KIS_CONFIGS, ids=_NON_TRADING_KIS_IDS)
def test_non_trading_agents_kis_tool_is_readonly(config_path: Path):
    """#66: kis-trading-mcp-tool-readonly는 어떤 설정에서 로드해도 finus_account_balance_readonly여야 한다.

    news_agent.yml이 별도 이름(kis-trading-mcp-tool-readonly)으로 조회 전용 래퍼를 등록하므로,
    라우터 deep-merge 결과에서도 전체 권한 kis-trading-mcp-tool을 건드리지 않는다.
    """
    import nat_finus_nat.register  # noqa: F401
    from nat.runtime.loader import load_config
    from nat_finus_nat.finus_api import FinusAccountBalanceReadonlyConfig

    config = load_config(config_path)
    tool_config = config.functions["kis-trading-mcp-tool-readonly"]
    assert isinstance(tool_config, FinusAccountBalanceReadonlyConfig), (
        f"{config_path.name}: kis-trading-mcp-tool-readonly는 finus_account_balance_readonly여야 합니다. "
        f"실제 타입: {type(tool_config).__name__}"
    )


@pytest.mark.parametrize("config_path", _TRADING_KIS_CONFIGS, ids=_TRADING_KIS_IDS)
def test_trading_agents_kis_tool_is_full(config_path: Path):
    """#66: kis-trading-mcp-tool은 어떤 설정에서 로드해도 전체 권한(finus_account_balance)이어야 한다.

    이름 분리 후에는 news_agent.yml의 readonly 함수가 별도 키(kis-trading-mcp-tool-readonly)에
    등록되므로 kis-trading-mcp-tool(전체 권한)이 라우터 병합으로 덮이지 않는다.
    """
    import nat_finus_nat.register  # noqa: F401
    from nat.runtime.loader import load_config
    from nat_finus_nat.finus_api import FinusAccountBalanceConfig, FinusAccountBalanceReadonlyConfig

    config = load_config(config_path)
    tool_config = config.functions["kis-trading-mcp-tool"]
    assert not isinstance(tool_config, FinusAccountBalanceReadonlyConfig), (
        f"{config_path.name}: kis-trading-mcp-tool은 readonly가 아닌 finus_account_balance여야 합니다."
    )
    assert isinstance(tool_config, FinusAccountBalanceConfig), (
        f"{config_path.name}: kis-trading-mcp-tool은 finus_account_balance여야 합니다. "
        f"실제 타입: {type(tool_config).__name__}"
    )


# 각 에이전트의 tool_names가 권한에 맞는 KIS 도구를 참조하는지 고정한다.
# 타입만 검사하면 tool_names 드리프트(예: news_agent가 kis-trading-mcp-tool-readonly 대신
# kis-trading-mcp-tool을 참조)를 놓칠 수 있다 — 참조 고정으로 이중 보증한다.
_AGENT_KIS_TOOL_REFS = [
    # (config_path, agent_fn, expected_tool_name_in_tool_names)
    (AGENTS_DIR / "trading_agent.yml", "trading_agent_react", "kis-trading-mcp-tool"),
    (AGENTS_DIR / "monitoring_agent.yml", "monitoring_agent", "kis-trading-mcp-tool"),
    (AGENTS_DIR / "news_agent.yml", "news_agent", "kis-trading-mcp-tool-readonly"),
    (AGENTS_DIR / "recommend_agent.yml", "recommend_agent", "kis-trading-mcp-tool-readonly"),
    (AGENTS_DIR / "strategy_agent.yml", "strategy_agent", "kis-trading-mcp-tool-readonly"),
    (CONFIGS_ROOT / "router.yml", "trading_agent_react", "kis-trading-mcp-tool"),
    (CONFIGS_ROOT / "router.yml", "monitoring_agent", "kis-trading-mcp-tool"),
    (CONFIGS_ROOT / "router.yml", "news_agent", "kis-trading-mcp-tool-readonly"),
    (CONFIGS_ROOT / "router.yml", "recommend_agent", "kis-trading-mcp-tool-readonly"),
    (CONFIGS_ROOT / "router.yml", "strategy_agent", "kis-trading-mcp-tool-readonly"),
    (CONFIGS_ROOT / "router_nomemory.yml", "trading_agent_react", "kis-trading-mcp-tool"),
    (CONFIGS_ROOT / "router_nomemory.yml", "monitoring_agent", "kis-trading-mcp-tool"),
    (CONFIGS_ROOT / "router_nomemory.yml", "news_agent", "kis-trading-mcp-tool-readonly"),
    (CONFIGS_ROOT / "router_nomemory.yml", "recommend_agent", "kis-trading-mcp-tool-readonly"),
    (CONFIGS_ROOT / "router_nomemory.yml", "strategy_agent", "kis-trading-mcp-tool-readonly"),
]
_AGENT_KIS_TOOL_REF_IDS = [f"{p.name}::{a}::{t}" for p, a, t in _AGENT_KIS_TOOL_REFS]


@pytest.mark.parametrize("config_path,agent_fn,expected_tool", _AGENT_KIS_TOOL_REFS, ids=_AGENT_KIS_TOOL_REF_IDS)
def test_agent_references_expected_kis_tool(config_path: Path, agent_fn: str, expected_tool: str):
    """#66: 에이전트 tool_names가 권한에 맞는 KIS 도구를 참조하는지 고정한다.

    kis-trading-mcp-tool(전체 권한)은 trading/monitoring만, kis-trading-mcp-tool-readonly는
    news/recommend/strategy만 참조해야 한다. 라우터 설정에서도 동일하게 검사한다.
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
], ids=lambda x: x)
def test_readonly_api_type_allows_read_only(api_type: str):
    """#66: 조회 계열 api_type은 _is_readonly_api_type이 True를 반환해야 한다."""
    from nat_finus_nat.finus_api import _is_readonly_api_type
    assert _is_readonly_api_type(api_type) is True, (
        f"{api_type!r}는 조회 전용 허용 목록에 포함되어야 합니다."
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
    """
    from nat_finus_nat.finus_api import _is_readonly_api_type
    assert _is_readonly_api_type(api_type) is False, (
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


def test_kis_agents_share_identical_system_prompt():
    """monitoring/strategy/trading 프롬프트는 의도적으로 동일하다 - 한 곳만 수정되는 드리프트를 막는다."""
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.runtime.loader import load_config

    prompts = {
        function_name: load_config(AGENTS_DIR / file_name).functions[function_name].system_prompt
        for file_name, function_name in _IDENTICAL_SYSTEM_PROMPT_AGENTS
    }
    baseline_name, baseline_prompt = next(iter(prompts.items()))
    diverged = [name for name, prompt in prompts.items() if prompt != baseline_prompt]
    assert not diverged, f"{baseline_name} 기준으로 프롬프트가 분기한 에이전트: {diverged}"
