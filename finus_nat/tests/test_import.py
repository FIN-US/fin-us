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


def test_vendor_patch_status_characterization():
    """`VENDOR_PATCH_STATUS`의 현재 상태를 고정한다 - 이 값들이 "정상"이라고 보증하는
    테스트가 아니다. 지금 상태는 알려진 고장이고, 이 테스트는 그 고장이 조용히 더
    나빠지거나(예: 다른 패치까지 깨짐) 조용히 좋아지는(상류가 다시 호환되는) 순간을
    CI에서 드러내기 위해서만 존재한다. 픽스는 #152에서 트래킹한다.

    pin된 버전(nvidia-nat-langchain==1.6.0)에서 세 패치 모두 사실상 죽은 코드다:

    - `react_graph_init`: "signature_changed" — `ReActAgentGraph.__init__`에서
      `accept_direct_answer_without_react_format` 인자 자체가 사라져서 wrapping을
      포기하고 조기 반환한다(register.py의 `_patch_nat_react_accept_direct_for_kis_tools`).
      이 인자는 1.8.0에도 없다 - 이름이 바뀐 게 아니라 상류가 삭제했다.
    - `agent_node_plain_final`: "needle_not_found" — `agent_node` 소스에서 기대한
      조건문(needle)을 찾지 못해 평문 최종 답변 허용 패치를 포기한다.
    - `react_system_prompt`: "applied" — **이 값은 신호가 아니다.** 이 패치는 상류를
      조사하지 않고 항상 무조건 "applied"로 기록한다(`_patch_react_system_prompt`).
      실제로 그 결과물(`_finus_react_prompt_for_tools`)을 소비하는 곳은 `_wrapped`
      하나뿐인데, `_wrapped`는 `react_graph_init`이 위에서 조기 반환하는 바람에
      `ReActAgentGraph.__init__`에 설치조차 되지 않는다. 즉 세 패치 전부 죽은
      코드이고, "applied"라는 문구만 보고 이게 동작 중이라고 읽으면 안 된다.
    """
    import nat_finus_nat.register as register

    assert set(register.VENDOR_PATCH_STATUS.keys()) == {
        "react_system_prompt",
        "agent_node_plain_final",
        "react_graph_init",
    }
    assert register.VENDOR_PATCH_STATUS == {
        "react_system_prompt": "applied",  # 무조건부 기록. 아래 docstring 참고 - 신호 없음.
        "agent_node_plain_final": "needle_not_found",
        "react_graph_init": "signature_changed",
    }
