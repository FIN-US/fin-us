"""주문 검증자(#299) 테스트.

두 축을 고정한다:

1. **verdict 파싱** — ``parse_verdict``의 fail-closed 규칙. 파싱 실패·미지 verdict 값·
   proposal_id 불일치는 전부 REJECT여야 한다. APPROVE가 나오는 유일한 경로는
   "JSON이 멀쩡하고 verdict가 APPROVE이고 id가 일치할 때"뿐이다.

2. **게이트 밖 / 브랜치 아님** — 검증자가 환각 게이트(``fe_branch``)에 감싸이지 않았고
   supervisor 브랜치에도 없다는 구조를 config에서 직접 확인한다. 동시에 제안 엔드포인트는
   반대로 ``fe_branch``를 노출해 게이트가 유지되는지 확인한다.
"""

import asyncio
from pathlib import Path

import pytest

from nat_finus_nat import verifier
from nat_finus_nat.verifier import (
    FAILURE_HARD_CHECK,
    FAILURE_ID_MISMATCH,
    FAILURE_LLM_ERROR,
    FAILURE_PARSE,
    FAILURE_TIMEOUT,
    HardCheckPayload,
    OrderVerifierConfig,
    ProposalPayload,
    SnapshotPayload,
    VerifyOrderRequest,
    parse_verdict,
)

CONFIGS_ROOT = Path(__file__).resolve().parents[1] / "configs"

PROPOSAL_ID = "adv-20260518-0001"


def _request(*, passed: bool = True) -> VerifyOrderRequest:
    return VerifyOrderRequest(
        proposal_id=PROPOSAL_ID,
        proposal=ProposalPayload(
            stock_name="삼성전자",
            stock_code="005930",
            side="BUY",
            quantity=10,
            order_type="LIMIT",
            price=74_500,
            rationale="분기 실적 개선과 수급 회복",
            confidence=0.72,
        ),
        snapshot=SnapshotPayload(
            current_price=74_500,
            cash=5_000_000,
            total_value=10_000_000,
            holding_qty=3,
        ),
        limits={"max_order_amount": 1_000_000},
        usage={"order_count": 1, "order_amount": 200_000},
        hard_check=HardCheckPayload(passed=passed, violations=[]),
    )


# ---------------------------------------------------------------------------
# verdict 파싱 — fail-closed
# ---------------------------------------------------------------------------


def test_parse_verdict_accepts_well_formed_approve():
    raw = f'{{"proposal_id": "{PROPOSAL_ID}", "verdict": "APPROVE", "reason": "근거가 충분합니다."}}'

    verdict = parse_verdict(raw, _request())

    assert verdict.verdict == "APPROVE"
    assert verdict.failure is None
    assert verdict.reason == "근거가 충분합니다."
    assert verdict.proposal_id == PROPOSAL_ID


def test_parse_verdict_accepts_reject_with_reason():
    raw = f'{{"proposal_id": "{PROPOSAL_ID}", "verdict": "reject", "reason": "근거가 빈약합니다."}}'

    verdict = parse_verdict(raw, _request())

    # 소문자 verdict도 정규화해 받는다 — 형식 습관 하나로 판정이 뒤집히면 안 된다.
    assert verdict.verdict == "REJECT"
    assert verdict.failure is None
    assert verdict.reason == "근거가 빈약합니다."


def test_parse_verdict_strips_code_fence_and_surrounding_prose():
    raw = (
        "판정 결과입니다.\n"
        "```json\n"
        f'{{"proposal_id": "{PROPOSAL_ID}", "verdict": "APPROVE", "reason": "이상 없음"}}\n'
        "```\n"
    )

    verdict = parse_verdict(raw, _request())

    assert verdict.verdict == "APPROVE"
    assert verdict.failure is None


@pytest.mark.parametrize(
    "raw",
    [
        "죄송합니다, 판정할 수 없습니다.",  # JSON이 아예 없음
        "{",  # 잘린 JSON
        '{"proposal_id": "x", "verdict": ',  # 중간에 끊김
        "[]",  # 객체가 아님
    ],
    ids=["no_json", "truncated", "cut_off", "not_an_object"],
)
def test_parse_verdict_rejects_unparsable_output(raw: str):
    verdict = parse_verdict(raw, _request())

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_PARSE
    assert verdict.proposal_id == PROPOSAL_ID


@pytest.mark.parametrize(
    "value",
    ['"MAYBE"', '"승인"', "null", "true", '""'],
    ids=["maybe", "korean", "null", "bool", "empty"],
)
def test_parse_verdict_rejects_unknown_verdict_value(value: str):
    """APPROVE/REJECT가 아닌 값은 '판정 불가'다 — 승인으로 흐르지 않는다."""
    raw = f'{{"proposal_id": "{PROPOSAL_ID}", "verdict": {value}, "reason": "..."}}'

    verdict = parse_verdict(raw, _request())

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_PARSE


def test_parse_verdict_rejects_proposal_id_mismatch_even_on_approve():
    """id가 어긋나면 모델이 승인했더라도 승인으로 쓰지 않는다."""
    raw = '{"proposal_id": "other-proposal", "verdict": "APPROVE", "reason": "좋습니다."}'

    verdict = parse_verdict(raw, _request())

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_ID_MISMATCH
    # 되돌려주는 id는 요청의 것이다 — backend가 자기 제안과 대조할 수 있어야 한다.
    assert verdict.proposal_id == PROPOSAL_ID


def test_parse_verdict_rejects_missing_proposal_id():
    raw = '{"verdict": "APPROVE", "reason": "좋습니다."}'

    verdict = parse_verdict(raw, _request())

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_ID_MISMATCH


# ---------------------------------------------------------------------------
# 함수 동작 — LLM 실패/타임아웃/한쪽 방향 잠금
# ---------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, *, content: str = "", error: Exception | None = None, hang: bool = False):
        self.content = content
        self.error = error
        self.hang = hang
        self.calls: list[object] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error

        class _Response:
            def __init__(self, content):
                self.content = content

        return _Response(self.content)


class _FakeBuilder:
    def __init__(self, llm):
        self._llm = llm

    async def get_llm(self, name, wrapper_type=None):
        return self._llm


async def _run_verifier(llm, request, *, timeout_sec: float = 30.0):
    config = OrderVerifierConfig(llm_name="verifier_openai_llm", timeout_sec=timeout_sec)
    async with verifier.finus_order_verifier(config, _FakeBuilder(llm)) as info:
        return await info.single_fn(request)


async def test_verifier_returns_approve_for_well_formed_llm_output():
    llm = _FakeLLM(
        content=f'{{"proposal_id": "{PROPOSAL_ID}", "verdict": "APPROVE", "reason": "이상 없음"}}'
    )

    verdict = await _run_verifier(llm, _request())

    assert verdict.verdict == "APPROVE"
    assert verdict.failure is None
    assert len(llm.calls) == 1


async def test_verifier_rejects_without_calling_llm_when_hard_check_failed():
    """한쪽 방향 잠금: 코드가 거부한 제안에 LLM이 의견을 낼 기회 자체가 없다."""
    llm = _FakeLLM(
        content=f'{{"proposal_id": "{PROPOSAL_ID}", "verdict": "APPROVE", "reason": "괜찮습니다"}}'
    )

    verdict = await _run_verifier(llm, _request(passed=False))

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_HARD_CHECK
    assert llm.calls == []


async def test_verifier_rejects_on_llm_error():
    llm = _FakeLLM(error=RuntimeError("upstream 500"))

    verdict = await _run_verifier(llm, _request())

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_LLM_ERROR


async def test_verifier_rejects_on_timeout():
    llm = _FakeLLM(hang=True)

    verdict = await _run_verifier(llm, _request(), timeout_sec=0.01)

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_TIMEOUT


async def test_verifier_prompt_carries_only_payload_values():
    """프롬프트에 실리는 값은 요청 페이로드에서만 온다 (지어낸 수치 유입 방지)."""
    request = _request()
    prompt = verifier.build_user_prompt(request)

    assert PROPOSAL_ID in prompt
    assert "005930" in prompt
    assert "74500" in prompt  # snapshot.current_price / proposal.price
    assert "5000000" in prompt  # snapshot.cash


# ---------------------------------------------------------------------------
# 구조 — 게이트 밖, 브랜치 아님, 엔드포인트 배선
# ---------------------------------------------------------------------------

_CONFIGS_WITH_ENDPOINTS = [
    CONFIGS_ROOT / "agents" / "verifier_agent.yml",
    CONFIGS_ROOT / "router.yml",
    CONFIGS_ROOT / "router_nomemory.yml",
]
_CONFIG_IDS = [p.name for p in _CONFIGS_WITH_ENDPOINTS]


def _load(config_path: Path):
    import nat_finus_nat.register  # noqa: F401 - 등록 트리거만 필요
    from nat.runtime.loader import load_config

    return load_config(config_path)


@pytest.mark.parametrize("config_path", _CONFIGS_WITH_ENDPOINTS, ids=_CONFIG_IDS)
def test_order_assist_endpoints_are_registered(config_path: Path):
    """두 엔드포인트가 라우터 설정에도 상속돼 들어오는지 확인한다.

    ``base:`` 사슬에 verifier_agent.yml을 끼운 것이 실제로 프로덕션 라우터에
    반영되는지가 이 테스트의 요점이다.
    """
    config = _load(config_path)
    endpoints = {ep.path: ep for ep in config.general.front_end.endpoints}

    assert endpoints.keys() == {"/v1/propose-order", "/v1/verify-order"}
    assert endpoints["/v1/propose-order"].function_name == "strategy_branch_agent"
    assert endpoints["/v1/verify-order"].function_name == "order_verifier"
    assert endpoints["/v1/propose-order"].method == "POST"
    assert endpoints["/v1/verify-order"].method == "POST"


@pytest.mark.parametrize("config_path", _CONFIGS_WITH_ENDPOINTS, ids=_CONFIG_IDS)
def test_propose_endpoint_keeps_the_hallucination_gate(config_path: Path):
    """제안 엔드포인트는 fe_branch(게이트)를 노출해야 한다 — 안쪽 react_agent 직노출 금지."""
    from nat_finus_nat.agents import FeBranchConfig

    config = _load(config_path)
    exposed = config.functions["strategy_branch_agent"]

    assert isinstance(exposed, FeBranchConfig)
    assert exposed.inner_function_name == "strategy_agent"


@pytest.mark.parametrize("config_path", _CONFIGS_WITH_ENDPOINTS, ids=_CONFIG_IDS)
def test_verifier_is_outside_the_gate(config_path: Path):
    """검증자는 게이트 밖이다 — 어떤 fe_branch도 order_verifier를 감싸지 않는다.

    게이트에 넣으면 backend가 넘겨준 수치를 근거로 쓰는 판정문이 구조적 오탐에 걸린다.
    그 자리는 파싱 실패=REJECT라는 더 보수적인 방어가 대신한다(verifier.py 참조).
    """
    from nat_finus_nat.agents import FeBranchConfig
    from nat_finus_nat.verifier import OrderVerifierConfig as _VerifierConfig

    config = _load(config_path)

    assert isinstance(config.functions["order_verifier"], _VerifierConfig)
    wrapped = {
        fn.inner_function_name
        for fn in config.functions.values()
        if isinstance(fn, FeBranchConfig)
    }
    assert "order_verifier" not in wrapped


@pytest.mark.parametrize(
    "config_path",
    [CONFIGS_ROOT / "router.yml", CONFIGS_ROOT / "router_nomemory.yml"],
    ids=["router.yml", "router_nomemory.yml"],
)
def test_supervisor_branches_are_unchanged_and_exclude_the_verifier(config_path: Path):
    """supervisor 브랜치 6개는 불변이고 검증자는 그중에 없다 — 채팅으로 도달할 수 없다."""
    config = _load(config_path)
    supervisor = config.functions["router_supervisor_agent"]

    branch_names = [branch.name for branch in supervisor.branches]
    assert branch_names == [
        "trading_agent",
        "monitoring_agent",
        "news_agent",
        "recommend_agent",
        "strategy_agent",
        "diary_agent",
    ]
    function_names = {str(branch.function_name) for branch in supervisor.branches}
    assert "order_verifier" not in function_names
