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

    이 테스트는 "패치 상태 문자열"이 아니라 그라운드 트루스를 확인한다: `ReActAgentGraph.__init__`이
    여전히 벤더 모듈 소속의 원본 함수이고, 소스를 재작성하는 패치가 붙였을 `_finus_*` 속성이
    전혀 없어야 한다. 누군가 이 unit이 되돌린 것과 같은 소스 재작성 패치를 다시 들여오는 순간
    이 테스트가 실패한다.
    """
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    import nat.plugins.langchain.agent.react_agent.agent as ra_mod
    from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph

    init_fn = ReActAgentGraph.__init__
    assert init_fn.__module__ == "nat.plugins.langchain.agent.react_agent.agent"
    assert [name for name in vars(init_fn) if name.startswith("_finus")] == []

    assert not hasattr(ReActAgentGraph, "_finus_plain_final_after_tool")

    # 옛 `_patch_react_system_prompt`는 상류 호환 여부를 확인하지 않고 이 속성을
    # 모듈에 무조건 심었다("applied" 오탐의 실체). 이제 그 패치 자체가 없다.
    assert [name for name in vars(ra_mod) if name.startswith("_finus")] == []
