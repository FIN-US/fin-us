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
        direct = load_config(AGENTS_DIR / AGENT_YAML[function_name]).functions[function_name]
        assert fn_config.system_prompt == direct.system_prompt
        assert [str(t) for t in fn_config.tool_names] == [str(t) for t in direct.tool_names]


# 프롬프트가 ``…`` 로 감싸 안내하는 도구성 토큰 중 실제 tool_names에 없는 값을 걸러낸다. 이 집합에
# 들어가는 값은 KIS API 파라미터/값(tool_name, api_type, ...)처럼 도구 이름이 아닌 것으로 확인된
# 토큰뿐이어야 한다 - 여기에 실수로 실제 도구 이름을 넣으면 그 이름에 대한 검사가 조용히 꺼진다.
KNOWN_NON_TOOL_TOKENS = {
    "tool_name",
    "api_type",
    "domestic_stock",
    "params",
    "find_api_detail",
    "period",
    "stock_name",
    "env_dv",
}


@pytest.mark.parametrize("config_path,function_name", ALL_CONFIGS, ids=ALL_CONFIG_IDS)
def test_system_prompt_only_names_real_tools(config_path: Path, function_name: str):
    """system_prompt가 ``도구이름`` 형태로 안내하는 이름이 실제 tool_names에 있는지 확인한다.

    옛 죽은 프롬프트(register.py 몽키패치)에는 등록조차 안 된 이름(finus_market_news 등 common.yml의
    `_type` 값)이 섞여 있었고, 이제는 매 턴 살아 있는 텍스트라 오타/유령 이름이 곧바로
    TOOL_NOT_FOUND_ERROR_MESSAGE로 이어져 max_tool_calls 사이클을 하나 버린다.
    """
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.runtime.loader import load_config

    config = load_config(config_path)
    fn_config = config.functions[function_name]

    tools = {str(t) for t in fn_config.tool_names}
    quoted = set(re.findall(r"``([\w.-]+)``", fn_config.system_prompt)) - KNOWN_NON_TOOL_TOKENS
    assert quoted <= tools, f"프롬프트가 보유하지 않은 도구를 안내함: {sorted(quoted - tools)}"
