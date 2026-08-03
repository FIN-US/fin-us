"""#152: 여섯 ReAct 에이전트 YAML이 계속 빌드되는지 확인하는 저렴한 오프라인 스모크 테스트.

`system_prompt`를 register.py 런타임 패치가 아니라 YAML의 `ReActAgentWorkflowConfig.system_prompt`
필드로 직접 전달하도록 옮겼다. `create_react_agent_prompt`는 내부적으로
`ReActAgentGraph.validate_system_prompt`를 호출해 `{tools}`/`{tool_names}` 플레이스홀더가
있는지 확인하고 없으면 ValueError를 던진다. 이 테스트는 실제 LLM/MCP 연결 없이(오프라인)
Config 파싱과 프롬프트 조립만으로 그 검증을 통과하는지 확인한다 - 다음 nvidia-nat 업그레이드가
`validate_system_prompt`의 요구사항을 바꾸면 여기서 바로 드러난다.
"""

from pathlib import Path

import pytest

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs" / "agents"

# (yaml 파일명, 그 파일 안의 react_agent 함수 이름)
AGENT_CONFIGS = [
    ("diary_agent.yml", "diary_agent"),
    ("monitoring_agent.yml", "monitoring_agent"),
    ("news_agent.yml", "news_agent"),
    ("recommend_agent.yml", "recommend_agent"),
    ("strategy_agent.yml", "strategy_agent"),
    ("trading_agent.yml", "trading_agent_react"),
]


@pytest.mark.parametrize("yaml_name,function_name", AGENT_CONFIGS)
def test_agent_config_builds_with_valid_system_prompt(yaml_name: str, function_name: str):
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph
    from nat.plugins.langchain.agent.react_agent.agent import create_react_agent_prompt
    from nat.plugins.langchain.agent.react_agent.register import ReActAgentWorkflowConfig
    from nat.runtime.loader import load_config

    config = load_config(CONFIGS_DIR / yaml_name)
    fn_config = config.functions[function_name]
    assert isinstance(fn_config, ReActAgentWorkflowConfig)

    # system_prompt가 YAML에서 실제로 채워졌는지(모듈 import 시점 패치가 아니라).
    assert fn_config.system_prompt
    assert ReActAgentGraph.validate_system_prompt(fn_config.system_prompt) is True

    # additional_instructions까지 합친 최종 프롬프트도 빌드되는지 확인한다.
    prompt = create_react_agent_prompt(fn_config)
    assert prompt is not None
