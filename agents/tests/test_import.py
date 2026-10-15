# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


def test_register_module_imports():
    """`nat_finus_nat.register` import로 NAT 컴포넌트 등록이 로드되는지 확인한다."""
    import nat_finus_nat.register  # noqa: F401


def test_react_agent_graph_init_is_unpatched_vendor_code():
    """#152: `_patch_nat_react_accept_direct_for_kis_tools`와 그 하위 패치들
    (`_patch_react_system_prompt`, `_patch_react_agent_node_plain_final_after_tool`)을
    제거했다 - 애초에 `accept_direct_answer_without_react_format` 인자가 상류에서
    삭제되어 셋 다 죽은 코드였고(`ReActAgentGraph.__init__`을 감싸는 `_wrapped`가 설치조차
    되지 않았다), 세 Fin-Us ReAct 프롬프트는 이제 `system_prompt`로 YAML에서 직접 전달한다
    (`ReActAgentWorkflowConfig.system_prompt` -> `create_react_agent_prompt`).

    이 테스트는 "패치 상태 문자열"이나 `_finus_*` 속성 이름이 아니라 소스 위치라는
    그라운드 트루스를 확인한다: `__code__.co_filename`은 함수가 실제로 정의된 파일이며,
    `functools.wraps`도 이 값은 건드리지 않고(`__module__`/`__name__`/`__doc__`는 복사하지만
    `__code__`는 원본 그대로 남는다), `exec(compile(...))`로 재작성한 소스는 애초에
    다른 filename(예: 옛 `_patch_react_agent_node_plain_final_after_tool`이 쓰던
    `"<finus_react_agent_node_plain_final_patch>"`)으로 컴파일된다. 즉 패치가 속성 이름을
    무엇으로 바꾸든 이 값은 위장할 수 없다. `endswith("agent.py")`가 아니라 벤더 모듈
    자신의 `__file__`과 정확히 같은지 비교한다 - `endswith`는 `react_agent.py`나
    `supervisor_agent.py`처럼 이름만 비슷한 다른 파일도 통과시킨다.

    옛 `_wrapped`의 실제 페이로드는 소스 재작성이 아니라
    `ra_mod.SYSTEM_PROMPT = picker(tools)`라는 모듈 전역 재바인딩이었다 - 속성 마커도
    없고 `co_filename`도 안 바뀌는, 탐지하기 가장 쉬운데 지금까지 아무 테스트도
    보지 않던 경로다. `ra_mod.SYSTEM_PROMPT is prompt_mod.SYSTEM_PROMPT`로 그 재바인딩이
    없는지 직접 확인한다.

    누군가 소스 재작성 패치나 SYSTEM_PROMPT 런타임 재바인딩을 다시 들여오면
    (마커 이름과 무관하게) 이 테스트가 실패한다.
    """
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    import nat.plugins.langchain.agent.react_agent.agent as ra_mod
    import nat.plugins.langchain.agent.react_agent.prompt as prompt_mod
    from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph

    assert ReActAgentGraph.agent_node.__code__.co_filename == ra_mod.__file__
    assert ReActAgentGraph.__init__.__code__.co_filename == ra_mod.__file__

    # 옛 패치의 실제 페이로드: 소스 재작성이 아니라 모듈 전역 SYSTEM_PROMPT 재바인딩.
    assert ra_mod.SYSTEM_PROMPT is prompt_mod.SYSTEM_PROMPT


def test_vendor_patch_status_reflects_actual_installation():
    """#152: VENDOR_PATCH_STATUS가 실제 벤더 패치 설치 여부를 반영한다.

    패치 코드가 제거됐으므로 세 항목 모두 ``"not_applied"``여야 한다.
    값이 ``"applied"``이면 예상치 못한 런타임 패치가 벤더를 수정하고 있다는 뜻이고,
    값이 ``"unknown"``이면 벤더 모듈을 import하지 못했다는 뜻 — 둘 다 이상 상태다.
    """
    from nat_finus_nat.register import VENDOR_PATCH_STATUS

    assert VENDOR_PATCH_STATUS["react_system_prompt"] == "not_applied", (
        "react_system_prompt: 패치가 예상치 않게 설치돼 있거나 모듈 import 실패"
    )
    assert VENDOR_PATCH_STATUS["agent_node_plain_final"] == "not_applied", (
        "agent_node_plain_final: 패치가 예상치 않게 설치돼 있거나 모듈 import 실패"
    )
    assert VENDOR_PATCH_STATUS["react_graph_init"] == "not_applied", (
        "react_graph_init: 패치가 예상치 않게 설치돼 있거나 모듈 import 실패"
    )
