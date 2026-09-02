"""주문 검증자(#299) 테스트.

두 축을 고정한다:

1. **verdict 파싱** — ``parse_verdict``의 fail-closed 규칙. 파싱 실패·미지 verdict 값·
   proposal_id 불일치는 전부 REJECT여야 한다. APPROVE가 나오는 유일한 경로는
   "JSON이 멀쩡하고 verdict가 APPROVE이고 id가 일치할 때"뿐이다.

2. **게이트 밖 / 브랜치 아님** — 검증자가 환각 게이트(``fe_branch``)에 감싸이지 않았고
   supervisor 브랜치에도 없다는 구조를 config에서 직접 확인한다. 동시에 제안 엔드포인트는
   반대로 ``fe_branch``를 노출해 게이트가 유지되는지 확인한다.

3. **스냅샷 마스킹 (#336)** — 계좌 스냅샷의 원값이 외부 LLM으로 나가는 프롬프트에
   하나도 실리지 않고, 그 자리를 비율 구간이 대신하며, 변환이 fail-closed를 정상 주문
   쪽으로 무너뜨리지 않는다는 것을 고정한다.
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
    _first_json_object,
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
        limits={
            "max_order_amount": 1_000_000,
            "max_daily_amount": 3_000_000,
            "max_daily_count": 5,
        },
        usage={
            "order_count": 1,
            "order_amount": 200_000,
            "confidence_below_soft_threshold": False,
        },
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
# _first_json_object — 손으로 짠 중괄호 균형 파서
#
# parse_verdict를 통해 간접적으로만 덮이면, 이 파서를 고칠 때 어느 케이스가 깨졌는지
# 실패 메시지가 말해주지 않는다. fail-closed 규칙상 이 함수의 오탐은 곧 정상 판정의
# REJECT이고 오검출은 곧 엉뚱한 객체의 판정이라, 분기를 직접 걸어 둔다.
# ---------------------------------------------------------------------------


def test_first_json_object_reads_a_bare_object():
    assert _first_json_object('{"verdict": "APPROVE"}') == {"verdict": "APPROVE"}


def test_first_json_object_ignores_prose_on_both_sides():
    """모델은 JSON 앞뒤에 설명 문장을 붙인다 — 전체 파싱이 실패하면 균형 세기로 넘어간다."""
    raw = "검토했습니다.\n" '{"verdict": "REJECT", "reason": "근거 부족"}' "\n이상입니다."

    assert _first_json_object(raw) == {"verdict": "REJECT", "reason": "근거 부족"}


def test_first_json_object_is_not_fooled_by_braces_inside_a_string():
    """사유 문장에 중괄호가 들어가도 객체가 거기서 끊기면 안 된다.

    중괄호는 **짝이 맞지 않게** 넣는다. 짝이 맞으면 문자열 인식을 통째로 없애도
    깊이가 우연히 0으로 돌아와 같은 결과가 나오고, 그러면 이 테스트가 아무것도
    고정하지 못한다(뮤테이션 실측으로 확인).
    """
    raw = '설명\n{"verdict": "REJECT", "reason": "가격 조건 {일부가 어긋납니다"}\n끝'

    assert _first_json_object(raw) == {
        "verdict": "REJECT",
        "reason": "가격 조건 {일부가 어긋납니다",
    }


def test_first_json_object_handles_escaped_quotes_in_a_string():
    """이스케이프된 따옴표가 문자열을 조기 종료시키면 그 뒤 중괄호 세기가 어긋난다.

    이스케이프 따옴표를 **홀수 개**로 두고 그 뒤에 여는 중괄호를 놓는다. 짝수 개면
    문자열 안팎이 다시 뒤집혀 이스케이프 처리를 없애도 결과가 같아진다 — 여기서는
    ``\\"`` 하나를 놓쳐 문자열이 조기 종료되는 순간 그다음 ``{``를 바깥으로 세고,
    닫는 ``}``는 문자열 안으로 세어 깊이가 0으로 돌아오지 못한다.
    """
    raw = '앞말 {"verdict": "REJECT", "reason": "보류\\" {주의"} 뒷말'

    assert _first_json_object(raw) == {"verdict": "REJECT", "reason": '보류" {주의'}


def test_first_json_object_keeps_nested_objects_whole():
    raw = '설명 {"verdict": "APPROVE", "meta": {"depth": {"n": 2}}} 끝'

    assert _first_json_object(raw) == {"verdict": "APPROVE", "meta": {"depth": {"n": 2}}}


def test_first_json_object_takes_the_first_object_when_several_appear():
    """두 판정이 실려 오면 앞의 것을 쓴다 — 뒤엣것을 고르면 어느 쪽이 판정인지 흔들린다."""
    raw = '앞말 {"verdict": "REJECT", "reason": "첫 번째"} 그리고 {"verdict": "APPROVE"}'

    assert _first_json_object(raw) == {"verdict": "REJECT", "reason": "첫 번째"}


@pytest.mark.parametrize(
    "raw",
    [
        "판정할 수 없습니다.",
        '{"verdict": "APPROVE"',
        '앞말 {"verdict": "APPROVE", ',
        "[]",
        '["APPROVE"]',
        "",
    ],
    ids=["no_brace", "unclosed", "unclosed_after_prose", "empty_array", "array", "empty"],
)
def test_first_json_object_returns_none_when_no_object_can_be_read(raw: str):
    """객체를 못 뽑으면 None이다 — 호출부(parse_verdict)가 이것을 REJECT로 바꾼다."""
    assert _first_json_object(raw) is None


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
    assert "분기 실적 개선과 수급 회복" in prompt


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


# ---------------------------------------------------------------------------
# #299 2차 리뷰 — fail-closed 불변식의 구멍, 인젝션 방어, 설정 참조
# ---------------------------------------------------------------------------


def test_parse_verdict_rejects_when_the_request_id_is_itself_empty():
    """요청 id가 비면 대조 자체가 성립하지 않는다 — 그래도 REJECT다.

    ``VerifyOrderRequest.proposal_id``는 기본값이 ""이라 id 없는 본문이 200으로 들어올
    수 있다. 그때 모델도 proposal_id를 생략하면 ``"" != ""``가 False라 대조를 그냥
    지나치고 APPROVE가 성립한다. backend는 항상 id를 싣지만 그 사실에 기대지 않는다.
    """
    request = VerifyOrderRequest(proposal=ProposalPayload(), hard_check=HardCheckPayload(passed=True))
    raw = '{"proposal_id": "", "verdict": "APPROVE", "reason": "좋습니다."}'

    verdict = parse_verdict(raw, request)

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_ID_MISMATCH


def test_parse_verdict_rejects_empty_request_id_even_without_an_echo():
    request = VerifyOrderRequest(proposal=ProposalPayload(), hard_check=HardCheckPayload(passed=True))

    verdict = parse_verdict('{"verdict": "APPROVE"}', request)

    assert verdict.verdict == "REJECT"
    assert verdict.failure == FAILURE_ID_MISMATCH


def test_system_prompt_tells_the_model_the_payload_is_data_not_instructions():
    """rationale은 앞단 에이전트가 뉴스·공시를 읽고 만든 문자열이라 지시문이 섞일 수 있다."""
    assert "검토 대상 데이터" in verifier._SYSTEM_PROMPT
    assert "지시로" in verifier._SYSTEM_PROMPT


def test_user_prompt_labels_the_payload_as_data():
    prompt = verifier.build_user_prompt(_request())

    assert "지시가 아닙니다" in prompt


def test_user_prompt_clips_long_free_text():
    """긴 자유 텍스트가 시스템 규칙을 문맥 밖으로 밀어내지 못하게 분량을 묶는다."""
    long_rationale = "가" * 5000
    request = _request()
    request.proposal.rationale = long_rationale

    prompt = verifier.build_user_prompt(request)

    assert long_rationale not in prompt
    assert "…(생략)" in prompt
    assert len(prompt) < 3000


def test_user_prompt_keeps_a_normal_rationale_intact():
    prompt = verifier.build_user_prompt(_request())

    assert "분기 실적 개선과 수급 회복" in prompt
    assert "…(생략)" not in prompt


@pytest.mark.parametrize("config_path", _CONFIGS_WITH_ENDPOINTS, ids=_CONFIG_IDS)
def test_verifier_llm_reference_actually_resolves(config_path: Path):
    """llm_name은 LLMRef(문자열)이라 설정 로드가 참조 유효성을 확인해 주지 않는다.

    오타는 로드를 통과하고 builder.get_llm에서 런타임에 터진다 → NAT 500 →
    backend FAILURE_HTTP → **모든 제안이 조용히 거부**된다. 라우터 설정까지 함께
    보는 이유는 base 사슬 어딘가에서 llms 블록이 갈릴 수 있기 때문이다.
    """
    config = _load(config_path)

    llm_name = str(config.functions["order_verifier"].llm_name)
    assert llm_name in config.llms, f"{llm_name}이 llms에 정의돼 있지 않다"


# ---------------------------------------------------------------------------
# 스냅샷 마스킹 — (b) 비율·구간 변환 (#336, F-17 / NFR-05)
# ---------------------------------------------------------------------------
#
# 아래 픽스처의 수치는 전부 **다른 어디에도 부분 문자열로 나타나지 않는** 값으로 골랐다.
# 구간 이름("10~25%")·종목코드("005930")·proposal_id에 우연히 섞이면 "원값이 안 실렸다"는
# 단언이 통과할 수도 실패할 수도 있는 시험이 된다.
_LEAK_PRICE = 74_513  # 지정가
_LEAK_CURRENT = 74_827  # 현재가
_LEAK_QTY = 163  # 주문 수량
_LEAK_CASH = 31_408_257  # 주문가능현금
_LEAK_TOTAL = 88_216_403  # 총 평가금액
_LEAK_HOLDING = 209  # 보유 수량
_LEAK_USED = 4_310_762  # 오늘 누적 거래대금
_LEAK_DAILY_LIMIT = 50_000_000  # 일 거래대금 한도

_LEAK_AMOUNT = _LEAK_PRICE * _LEAK_QTY  # 12,145,619
_LEAK_HOLDING_AFTER = _LEAK_HOLDING + _LEAK_QTY  # 372
_LEAK_POSITION_AFTER = _LEAK_HOLDING_AFTER * _LEAK_PRICE
_LEAK_CASH_AFTER = _LEAK_CASH - _LEAK_AMOUNT  # 매수
_LEAK_CASH_AFTER_SELL = _LEAK_CASH + _LEAK_AMOUNT  # 매도 — 부호가 반대라 다른 수가 된다
_LEAK_DAILY_AFTER = _LEAK_USED + _LEAK_AMOUNT

# 프롬프트에 절대 나타나면 안 되는 수. 계좌 원값뿐 아니라 **주문 단가·수량과 한도 금액**도
# 포함한다 — 하나라도 남으면 구간에서 원값 범위가 역산된다(verifier.py의 근거 주석).
_FORBIDDEN_NUMBERS = (
    _LEAK_PRICE,
    _LEAK_CURRENT,
    _LEAK_QTY,
    _LEAK_CASH,
    _LEAK_TOTAL,
    _LEAK_HOLDING,
    _LEAK_USED,
    _LEAK_DAILY_LIMIT,
    _LEAK_AMOUNT,
    _LEAK_HOLDING_AFTER,
    _LEAK_POSITION_AFTER,
    _LEAK_CASH_AFTER,
    _LEAK_CASH_AFTER_SELL,
    _LEAK_DAILY_AFTER,
)


def _leak_request(*, side: str = "BUY", order_type: str = "LIMIT") -> VerifyOrderRequest:
    return VerifyOrderRequest(
        proposal_id=PROPOSAL_ID,
        proposal=ProposalPayload(
            stock_name="삼성전자",
            stock_code="005930",
            side=side,
            quantity=_LEAK_QTY,
            order_type=order_type,
            price=_LEAK_PRICE,
            rationale="분기 실적 개선과 수급 회복",
            confidence=0.72,
        ),
        snapshot=SnapshotPayload(
            current_price=_LEAK_CURRENT,
            cash=_LEAK_CASH,
            total_value=_LEAK_TOTAL,
            holding_qty=_LEAK_HOLDING,
        ),
        limits={"max_daily_amount": _LEAK_DAILY_LIMIT, "max_daily_count": 5},
        usage={
            "order_count": 2,
            "order_amount": _LEAK_USED,
            "confidence_below_soft_threshold": False,
        },
        hard_check=HardCheckPayload(passed=True, violations=[]),
    )


@pytest.mark.parametrize("value", _FORBIDDEN_NUMBERS, ids=[str(v) for v in _FORBIDDEN_NUMBERS])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("order_type", ["LIMIT", "MARKET"])
def test_prompt_never_carries_a_raw_amount_or_quantity(value: int, side: str, order_type: str):
    """#336의 회귀 테스트 — 스냅샷 원값이 프롬프트에 들어가면 실패한다.

    쉼표 표기까지 함께 막는 이유: ``json.dumps``는 쉼표를 붙이지 않지만, 나중에 사람이
    읽기 좋으라고 ``f"{v:,}"`` 꼴을 끼워 넣는 순간 같은 유출이 다른 모양으로 되살아난다.
    """
    prompt = verifier.build_user_prompt(_leak_request(side=side, order_type=order_type))

    assert str(value) not in prompt
    assert f"{value:,}" not in prompt


def test_prompt_carries_ratio_bands_in_place_of_the_snapshot():
    prompt = verifier.build_user_prompt(_leak_request())

    assert "order_ratio_of_cash" in prompt
    assert "25~50%" in prompt  # 12,145,619 / 31,408,257 = 38.7%
    assert "snapshot" not in prompt


def test_ratio_view_matches_the_hand_computed_bands():
    """구간 경계를 손으로 계산한 값에 고정한다 — 경계가 움직이면 여기서 걸린다."""
    view = verifier.derive_ratio_view(_leak_request())

    assert view["ratios"] == {
        "order_ratio_of_cash": "25~50%",  # 12,145,619 / 31,408,257 = 38.7%
        "order_ratio_of_total": "10~25%",  # 12,145,619 / 88,216,403 = 13.8%
        "position_weight_after": "25~50%",  # 27,718,836 / 88,216,403 = 31.4%
        "cash_weight_after": "10~25%",  # 19,262,638 / 88,216,403 = 21.8%
        "sell_ratio_of_holding": None,  # 매수라 성립하지 않는다
        "limit_price_gap": "0.5% 이내",  # 74,513 vs 74,827 = -0.42%
        "daily_amount_ratio_after": "25~50%",  # 16,456,381 / 50,000,000 = 32.9%
    }
    assert view["signals"] == {
        "has_holding": True,
        "daily_order_count": 2,
        "daily_order_count_limit": 5,
        "confidence_below_soft_threshold": False,
    }


def test_sell_gets_a_holding_ratio_and_a_growing_cash_weight():
    view = verifier.derive_ratio_view(_leak_request(side="SELL"))

    assert view["ratios"]["sell_ratio_of_holding"] == "75~95%"  # 163 / 209 = 78.0%
    # 매도는 현금이 늘어난다 — 매수와 같은 부호로 계산하면 여기서 갈린다.
    assert view["ratios"]["cash_weight_after"] == "25~50%"  # 43,553,876 / 88,216,403 = 49.4%


def test_market_order_prices_the_ratio_with_the_current_price():
    """backend ``estimated_unit_price``의 거울. 갈리면 코드와 LLM이 다른 주문을 본다."""
    view = verifier.derive_ratio_view(_leak_request(order_type="MARKET"))

    assert view["ratios"]["limit_price_gap"] is None  # 시장가엔 지정가 괴리가 없다
    # 163 × 74,827(현재가) = 12,196,801 / 31,408,257 = 38.8% — 지정가로 계산해도 같은 구간이라
    # 여기서는 구간이 아니라 "지정가를 쓰지 않았다"를 단가로 직접 확인한다.
    assert verifier.derive_ratio_view(_leak_request(order_type="MARKET"))["ratios"][
        "order_ratio_of_cash"
    ] == verifier._ratio_band(_LEAK_QTY * _LEAK_CURRENT, _LEAK_CASH)


@pytest.mark.parametrize(
    "ratio,expected",
    [
        (0.0, "1% 미만"),
        (0.009, "1% 미만"),
        (0.01, "1~5%"),
        (0.05, "5~10%"),
        (0.25, "25~50%"),
        (0.9499, "75~95%"),
        (0.95, "95~100%"),
        (1.0, "95~100%"),  # 전량 — "100% 초과"가 아니다
        (1.0001, "100% 초과"),
    ],
)
def test_ratio_band_boundaries(ratio: float, expected: str):
    assert verifier._ratio_band(ratio, 1.0) == expected


def test_a_zero_quantity_proposal_is_unknown_not_the_safest_possible_order():
    """분자 쪽 방어 — 수량·단가가 0이면 금액 계열 비율이 '1% 미만'으로 나가면 안 된다.

    '1% 미만'은 모델에게 **가장 안전한 주문**으로 읽힌다. 분모 0을 '0%'로 뭉개지 않는
    것과 같은 실패 양상이다. backend가 지금 이 값을 막는다는 사실에 기대지 않는다.
    """
    request = _leak_request()
    request.proposal.quantity = 0
    request.proposal.price = 0

    ratios = verifier.derive_ratio_view(request)["ratios"]

    assert ratios["order_ratio_of_cash"] == verifier._BAND_UNKNOWN
    assert ratios["order_ratio_of_total"] == verifier._BAND_UNKNOWN
    assert ratios["position_weight_after"] == verifier._BAND_UNKNOWN
    assert ratios["cash_weight_after"] == verifier._BAND_UNKNOWN
    assert ratios["daily_amount_ratio_after"] == verifier._BAND_UNKNOWN
    # 지정가 0은 "공짜"가 아니라 값이 없는 것 — -100% 괴리로 읽히면 안 된다.
    assert ratios["limit_price_gap"] == verifier._BAND_UNKNOWN


def test_a_zero_quantity_sell_does_not_read_as_a_tiny_partial_sale():
    request = _leak_request(side="SELL")
    request.proposal.quantity = 0

    assert (
        verifier.derive_ratio_view(request)["ratios"]["sell_ratio_of_holding"]
        == verifier._BAND_UNKNOWN
    )


def test_ratio_band_reports_unknown_instead_of_dividing_by_zero():
    """분모 0을 '0%'로 뭉개면 모델이 안전 신호로 읽는다. 산출 불가는 산출 불가다."""
    assert verifier._ratio_band(100, 0) == verifier._BAND_UNKNOWN
    assert verifier._ratio_band(100, None) == verifier._BAND_UNKNOWN
    assert verifier._ratio_band(-1, 100) == verifier._BAND_NEGATIVE


@pytest.mark.parametrize(
    "price,expected",
    [
        (100, "0.5% 이내"),
        (101, "0.5~2% 높음"),
        (99, "0.5~2% 낮음"),
        (103, "2~5% 높음"),
        (140, "10% 초과 높음"),
        (60, "10% 초과 낮음"),
    ],
)
def test_gap_band_keeps_direction(price: int, expected: str):
    assert verifier._gap_band(price, 100) == expected


def test_build_user_prompt_never_raises_on_a_degenerate_snapshot():
    """변환이 터지면 fail-closed 규칙상 **정상 주문이 REJECT**된다. 터지지 않는 것이 계약이다."""
    request = _leak_request()
    request.snapshot = SnapshotPayload(current_price=0, cash=0, total_value=0, holding_qty=0)
    request.limits = {}
    request.usage = {}

    prompt = verifier.build_user_prompt(request)

    assert verifier._BAND_UNKNOWN in prompt
    assert PROPOSAL_ID in prompt


async def test_a_normal_order_still_approves_after_the_conversion():
    """비율 변환이 정상 주문을 거부 쪽으로 흘리지 않는지 — 이 경로의 fail-closed 특성상 필수."""
    llm = _FakeLLM(
        content=f'{{"proposal_id": "{PROPOSAL_ID}", "verdict": "APPROVE", "reason": "이상 없음"}}'
    )

    verdict = await _run_verifier(llm, _leak_request())

    assert verdict.verdict == "APPROVE"
    assert verdict.failure is None
    # 프롬프트가 실제로 만들어졌고 그 안에 원값이 없다.
    assert str(_LEAK_CASH) not in llm.calls[0][1].content


def test_hard_check_violation_messages_never_reach_the_prompt():
    """위반 메시지에는 backend가 만든 원 금액이 문장으로 들어 있다."""
    request = _leak_request()
    request.hard_check = HardCheckPayload(
        passed=True, violations=[f"주문가능금액 부족: 주문금액 {_LEAK_CASH:,}원"]
    )

    prompt = verifier.build_user_prompt(request)

    assert "주문가능금액 부족" not in prompt
    assert str(_LEAK_CASH) not in prompt
    assert f"{_LEAK_CASH:,}" not in prompt


def test_prompt_drops_proposal_fields_it_does_not_know():
    """``extra='allow'``라 backend가 필드를 늘릴 수 있다. 모르는 필드가 외부 LLM으로 나가면 안 된다."""
    request = _leak_request()
    request.proposal = ProposalPayload(
        **request.proposal.model_dump(), account_no="12345678-01"  # type: ignore[arg-type]
    )

    prompt = verifier.build_user_prompt(request)

    assert "account_no" not in prompt
    assert "12345678-01" not in prompt


@pytest.mark.parametrize("block", ["ratios", "signals"])
def test_system_prompt_documents_every_derived_key(block: str):
    """설명 없는 키는 모델에게 뜻 없는 문자열이다 — 키를 늘리면 프롬프트도 함께 늘려야 한다."""
    for key in verifier.derive_ratio_view(_leak_request())[block]:
        assert key in verifier._SYSTEM_PROMPT, f"{key}의 뜻이 시스템 프롬프트에 없다"


def test_system_prompt_binds_the_confidence_rule_to_the_signal():
    """임계값이 페이로드에 없으므로, 확신도 규칙은 boolean 신호에 직접 붙어 있어야 한다.

    "확신도가 낮으면 REJECT"만 남기면 모델이 비교할 기준이 프롬프트 어디에도 없다.
    """
    assert "confidence_below_soft_threshold가 true면" in verifier._SYSTEM_PROMPT
    assert "임계값과 직접 비교하지 마세요" in verifier._SYSTEM_PROMPT


def test_system_prompt_does_not_invite_rejecting_on_a_code_checked_axis():
    """코드가 이미 통과시킨 축을 거부 예시로 들면 정상 주문이 거부된다.

    지정가 괴리가 그 예다 — ``ORDER_MAX_PRICE_GAP_RATIO``(기본 3%)를 넘는 주문은 검증자에
    도달하지 못하므로, 모델이 볼 수 있는 최대치조차 "크게 벌어짐"이 아니다.
    """
    rules = verifier._SYSTEM_PROMPT.split("규칙:\n", 1)[1]

    assert "limit_price_gap" not in rules
    # 대신 규칙 밖(항목 설명)에서 "코드가 이미 확인했다"고 알려 준다.
    assert "허용 범위 안이라는 것은 코드가 이미 확인했습니다" in verifier._SYSTEM_PROMPT


def test_system_prompt_tells_the_model_the_snapshot_is_masked():
    """원값이 오지 않는다고 말해 두지 않으면 모델이 없는 금액을 요구하거나 지어낸다."""
    assert "원값이 오지 않습니다" in verifier._SYSTEM_PROMPT
    assert "코드가 이미 판정해 통과시켰습니다" in verifier._SYSTEM_PROMPT
