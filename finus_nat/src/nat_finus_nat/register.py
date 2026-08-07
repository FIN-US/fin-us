# agents, finus_api 모듈을 import하여 `@register_function` 등록을 로드한다.
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


def _build_vendor_patch_status() -> dict[str, str]:
    """Detect actual installation status of NAT ReAct vendor patches.

    The patching approach was abandoned in #152:
    - ``accept_direct_answer_without_react_format`` was removed upstream (1.6.0+)
    - ``_wrapped`` was never installed (react_graph_init failed due to signature change)
    - The three Fin-Us ReAct prompts are now delivered via YAML ``system_prompt``

    All three patch slots are expected to be ``"not_applied"`` — vendor code is clean.
    A value of ``"applied"`` means something unexpected is patching the vendor at runtime.
    Returns ``"unknown"`` for all slots when the vendor module cannot be imported.
    """
    try:
        import nat.plugins.langchain.agent.react_agent.agent as _ra_mod
        import nat.plugins.langchain.agent.react_agent.prompt as _prompt_mod
        from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph

        return {
            "react_system_prompt": (
                "not_applied"
                if _ra_mod.SYSTEM_PROMPT is _prompt_mod.SYSTEM_PROMPT
                else "applied"
            ),
            "agent_node_plain_final": (
                "not_applied"
                if ReActAgentGraph.agent_node.__code__.co_filename == _ra_mod.__file__
                else "applied"
            ),
            "react_graph_init": (
                "not_applied"
                if ReActAgentGraph.__init__.__code__.co_filename == _ra_mod.__file__
                else "applied"
            ),
        }
    except Exception:  # noqa: BLE001
        return {
            "react_system_prompt": "unknown",
            "agent_node_plain_final": "unknown",
            "react_graph_init": "unknown",
        }


#: Reflects actual vendor patch installation status (#152).
#: All slots should be ``"not_applied"`` — patching was abandoned because the
#: upstream hook point (``accept_direct_answer_without_react_format``) was removed.
VENDOR_PATCH_STATUS: dict[str, str] = _build_vendor_patch_status()
