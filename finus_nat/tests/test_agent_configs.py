"""#152: ReAct 에이전트 YAML이 계속 빌드되는지 확인하는 저렴한 오프라인 스모크 테스트.

`system_prompt`를 register.py 런타임 패치가 아니라 YAML의 `ReActAgentWorkflowConfig.system_prompt`
필드로 직접 전달하도록 옮겼다. `create_react_agent_prompt`는 내부적으로
`ReActAgentGraph.validate_system_prompt`를 호출해 `{tools}`/`{tool_names}` 플레이스홀더가
있는지 확인하고 없으면 ValueError를 던진다. 이 테스트는 실제 LLM/MCP 연결 없이(오프라인)
Config 파싱과 프롬프트 조립만으로 그 검증을 통과하는지 확인한다 - 다음 nvidia-nat 업그레이드가
`validate_system_prompt`의 요구사항을 바꾸면 여기서 바로 드러난다.

`configs/agents/*.yml`을 단독으로 로드하는 것 외에, 프로덕션이 실제로 로드하는
`configs/router.yml` / `configs/router_nomemory.yml`도 포함한다. `base:` 상속 체인 때문에
두 라우터 모두 결과적으로 여섯 react_agent 함수를 전부 포함하므로, 라우터 레벨에서
`system_prompt`나 `tool_names`를 덮어쓰는 미래의 변경도(지금은 그런 오버라이드가 없다)
이 파라미터화가 놓치지 않는다.
"""

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
