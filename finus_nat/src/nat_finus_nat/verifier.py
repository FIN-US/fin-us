"""주문 검증 에이전트 (#299).

``/v1/verify-order`` 엔드포인트가 노출하는 ``finus_order_verifier`` 함수를 등록한다.
backend의 ``order_assist``가 하드 한도 코드 검사를 **통과한** 제안에 대해서만 호출한다.

이 모듈이 기존 에이전트들과 다른 점 세 가지:

1. **react_agent가 아니다.** 도구가 없고, 단발 LLM 호출 한 번 + pydantic 파싱이 전부다.
   ReAct 루프는 도구를 고르기 위한 장치인데 여기엔 고를 도구가 없다.

2. **환각 게이트(``_run_with_gate``) 밖이다.** 게이트는 "도구 Observation 없이 수치를 말하면
   거부"라는 규칙인데(agents.py ``_check_tool_enforcement``), 검증자의 일은 backend가 이미
   조회해서 넘겨준 수치를 근거로 판정문을 쓰는 것이다. 판정문에는 그 수치가 그대로 등장하고
   검증자는 도구를 하나도 부르지 않으므로, 게이트에 넣으면 정상 응답이 구조적으로 오탐에 걸린다.
   게이트를 뺀 자리는 더 보수적인 방어로 메운다 — **파싱 실패는 REJECT다**(아래 fail-closed).

3. **브랜치가 아니다.** supervisor의 브랜치 6개에 들어가지 않으므로 채팅으로는 도달할 수 없고,
   backend가 전용 엔드포인트로만 호출한다.

한쪽 방향 잠금(one-way lock)
---------------------------
검증자는 **거부 방향으로만** 개입할 수 있다. 코드가 이미 거부한 제안을 승인으로 뒤집는 경로는
존재하지 않는다:

- backend는 하드 한도 위반 시 이 함수를 아예 호출하지 않는다(order_assist).
- 그럼에도 ``hard_check.passed=False``가 실려 오면 LLM을 부르지 않고 즉시 REJECT한다.
  방어선을 한 겹 더 두는 것이지, backend의 게이트를 대신하는 것이 아니다.

fail-closed
-----------
LLM 호출 실패·타임아웃·JSON 파싱 실패·verdict 값 불명·proposal_id 불일치는 **전부 REJECT**다.
"판정할 수 없었다"와 "승인한다"를 구분하지 못하는 순간 이 모듈은 무의미해진다.
"""

import asyncio
import json
import logging
import re
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.component_ref import LLMRef
from nat.data_models.function import FunctionBaseConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 실패 코드 — 사용자에게 보이는 문장이 아니라 backend 로그·테스트가 붙잡는 식별자다.
# 값이 바뀌면 backend/order_assist.py의 거부 사유 표기와 테스트가 함께 흔들린다.
# ---------------------------------------------------------------------------
FAILURE_HARD_CHECK = "hard_check_failed"
FAILURE_LLM_ERROR = "llm_error"
FAILURE_TIMEOUT = "llm_timeout"
FAILURE_PARSE = "parse_error"
FAILURE_ID_MISMATCH = "proposal_id_mismatch"


class ProposalPayload(BaseModel):
    """검증 대상 주문 제안. 값은 전부 backend가 코드로 확정한 것이다.

    ``extra="allow"``인 이유: backend가 필드를 하나 늘렸다고 검증 요청 전체가 422로
    떨어지면, 그 순간 주문 보조 경로가 통째로 죽는다. 모르는 필드는 무시하고
    아는 필드로 판정하는 편이 낫다 — 판정에 쓰는 값은 아래 명시된 것뿐이다.
    """

    model_config = ConfigDict(extra="allow")

    stock_name: str = ""
    stock_code: str = ""
    side: str = ""
    quantity: int = 0
    order_type: str = ""
    price: int = 0
    rationale: str = ""
    confidence: float = 0.0


class SnapshotPayload(BaseModel):
    """주문 시점의 계좌·시세 스냅샷. backend가 KIS에서 직접 조회한 값이다."""

    model_config = ConfigDict(extra="allow")

    current_price: int = 0
    cash: int = 0
    total_value: int = 0
    holding_qty: int = 0


class HardCheckPayload(BaseModel):
    """코드가 이미 내린 하드 한도 판정. 검증자는 이 결과를 뒤집을 수 없다."""

    model_config = ConfigDict(extra="allow")

    passed: bool = False
    violations: list[str] = Field(default_factory=list)


class VerifyOrderRequest(BaseModel):
    """``POST /v1/verify-order`` 요청 본문.

    NAT은 단일 BaseModel 인자를 받는 함수의 요청 스키마로 그 모델을 그대로 쓴다
    (``FunctionDescriptor.get_base_model_function_input``). 즉 이 모델의 JSON이
    곧 HTTP 본문이며, ``{"input_message": ...}``나 ``{"query": ...}`` 래핑이 없다.
    """

    model_config = ConfigDict(extra="allow")

    proposal_id: str = ""
    proposal: ProposalPayload = Field(default_factory=ProposalPayload)
    snapshot: SnapshotPayload = Field(default_factory=SnapshotPayload)
    limits: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    hard_check: HardCheckPayload = Field(default_factory=HardCheckPayload)


class OrderVerdict(BaseModel):
    """``POST /v1/verify-order`` 응답 본문.

    ``proposal_id``는 **요청에 실려 온 값을 그대로 되돌려준다** — LLM이 말한 id가 아니다.
    LLM이 다른 id를 답하면 그것은 승인 근거가 아니라 :data:`FAILURE_ID_MISMATCH` 거부 사유다.
    backend는 자기가 보낸 id와 이 필드를 다시 대조한다(전송·라우팅 사고에 대한 이중 방어).

    ``reason``은 **정성 사유**로만 쓰라고 backend에 약속된 필드다. 검증자가 지어낸 수치가
    사용자에게 수치로 노출되지 않도록, backend는 사용자 메시지의 숫자를 전부 페이로드
    원본에서만 만든다.
    """

    proposal_id: str
    verdict: Literal["APPROVE", "REJECT"]
    reason: str = ""
    failure: str | None = Field(
        default=None,
        description="판정 불가로 REJECT된 경우의 원인 코드. 정상 판정이면 None.",
    )


_SYSTEM_PROMPT = (
    "당신은 Fin-Us 주문 검증자입니다. 이미 코드가 하드 한도 검사를 통과시킨 주문 제안 하나를 "
    "마지막으로 검토합니다.\n\n"
    "규칙:\n"
    "- 당신의 권한은 거부뿐입니다. 승인은 '거부할 이유를 찾지 못했다'는 뜻이지 추천이 아닙니다.\n"
    "- 제시된 스냅샷·한도·사용량 밖의 수치를 새로 지어내지 마세요. 판단 근거는 주어진 값뿐입니다.\n"
    "- 근거가 빈약하거나(rationale이 비어 있거나 종목과 무관), 확신도가 낮거나, 스냅샷과 제안이 "
    "서로 어긋나면(예: 매도 수량이 보유량과 맞지 않음, 지정가가 현재가와 크게 다름) REJECT하세요.\n"
    "- 판단이 서지 않으면 REJECT하세요. 애매함은 승인 사유가 아닙니다.\n"
    "- reason은 한국어 한두 문장의 정성적 사유로 쓰고, 숫자를 나열하지 마세요.\n\n"
    "출력은 JSON 객체 하나뿐입니다. 코드블록도 설명 문장도 붙이지 마세요:\n"
    # 아래 두 줄은 f-string으로 바꾸면 안 된다 — JSON 예시의 중괄호가 치환 필드로
    # 해석돼 프롬프트가 깨진다. 값을 끼워 넣을 일이 생기면 이 리터럴은 그대로 두고
    # 바깥에서 이어 붙인다. (작은따옴표 래핑이라 안쪽 큰따옴표는 이스케이프가 필요 없다.)
    '{"proposal_id": "<요청의 proposal_id 그대로>", "verdict": "APPROVE" 또는 "REJECT", '
    '"reason": "<한국어 사유>"}'
)

# ```json … ``` 코드펜스를 벗겨낸다. 프롬프트로 금지해도 모델은 종종 붙인다 —
# 그 한 줄 때문에 파싱이 실패하면 fail-closed 규칙상 REJECT가 되므로, 정상 판정이
# 형식 습관 하나로 뒤집히지 않게 여기서 먼저 걷어낸다.
_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_code_fence(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text.strip()).strip()


def _first_json_object(text: str) -> dict[str, Any] | None:
    """*text*에서 JSON 객체 하나를 뽑는다. 실패하면 None.

    전체 파싱을 먼저 시도하고, 실패하면 중괄호 균형을 세어 첫 객체만 잘라 재시도한다.
    모델이 JSON 앞뒤에 문장을 덧붙이는 흔한 경우를 덮되, 여기서도 실패하면
    호출부가 fail-closed로 REJECT한다.
    """
    stripped = _strip_code_fence(text)
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    candidate = json.loads(stripped[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return candidate if isinstance(candidate, dict) else None
    return None


def parse_verdict(raw: str, request: VerifyOrderRequest) -> OrderVerdict:
    """LLM 원문 *raw*를 :class:`OrderVerdict`로 파싱한다. 실패는 전부 REJECT다.

    순수 함수로 분리해 둔 이유는 fail-closed 규칙(파싱 실패·미지 verdict 값·id 불일치)과
    정상 판정을 LLM 없이 테스트로 고정하기 위해서다.
    """
    data = _first_json_object(raw)
    if data is None:
        logger.warning("검증자 응답 JSON 파싱 실패 — REJECT (proposal_id=%s)", request.proposal_id)
        return OrderVerdict(
            proposal_id=request.proposal_id,
            verdict="REJECT",
            reason="검증 응답을 해석하지 못해 보류했습니다.",
            failure=FAILURE_PARSE,
        )

    raw_verdict = data.get("verdict")
    verdict = raw_verdict.strip().upper() if isinstance(raw_verdict, str) else ""
    reason = data.get("reason")
    reason_text = reason.strip() if isinstance(reason, str) else ""

    if verdict not in ("APPROVE", "REJECT"):
        # "판정 불가"와 "모델이 REJECT라고 답함"을 같은 값으로 뭉개지 않도록 failure를 함께 싣는다.
        logger.warning(
            "검증자 verdict 값이 APPROVE/REJECT가 아님 — REJECT (proposal_id=%s, got=%r)",
            request.proposal_id,
            raw_verdict,
        )
        return OrderVerdict(
            proposal_id=request.proposal_id,
            verdict="REJECT",
            reason=reason_text or "검증 결과를 판정하지 못해 보류했습니다.",
            failure=FAILURE_PARSE,
        )

    echoed = data.get("proposal_id")
    echoed_id = echoed.strip() if isinstance(echoed, str) else ""
    if echoed_id != request.proposal_id:
        # 모델이 다른 제안을 판정했을 가능성이 있다. APPROVE였더라도 승인으로 쓰지 않는다.
        logger.warning(
            "검증자 proposal_id 불일치 — REJECT (expected=%s, got=%r)",
            request.proposal_id,
            echoed,
        )
        return OrderVerdict(
            proposal_id=request.proposal_id,
            verdict="REJECT",
            reason="검증 대상이 일치하지 않아 보류했습니다.",
            failure=FAILURE_ID_MISMATCH,
        )

    return OrderVerdict(
        proposal_id=request.proposal_id,
        verdict=verdict,  # type: ignore[arg-type]
        reason=reason_text,
        failure=None,
    )


def build_user_prompt(request: VerifyOrderRequest) -> str:
    """검증자에게 넘길 사용자 프롬프트. 값은 전부 요청 페이로드에서만 온다."""
    payload = {
        "proposal_id": request.proposal_id,
        "proposal": request.proposal.model_dump(),
        "snapshot": request.snapshot.model_dump(),
        "limits": request.limits,
        "usage": request.usage,
        "hard_check": request.hard_check.model_dump(),
    }
    return (
        "다음 주문 제안을 검토하고 JSON 하나로만 답하세요.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


class OrderVerifierConfig(FunctionBaseConfig, name="finus_order_verifier"):
    llm_name: LLMRef = Field(..., description="단발 판정에 쓸 LLM. 도구는 붙이지 않는다.")
    timeout_sec: float = Field(
        default=30.0,
        gt=0,
        description="LLM 호출 상한(초). 초과하면 판정 불가로 보고 REJECT한다.",
    )
    description: str = Field(
        default=(
            "Fin-Us order verifier: reviews one pre-checked order proposal and returns "
            "APPROVE/REJECT. Rejection-only authority; fails closed."
        )
    )


@register_function(config_type=OrderVerifierConfig, framework_wrappers=[LLMFrameworkEnum.LANGCHAIN])
async def finus_order_verifier(config: OrderVerifierConfig, builder: Builder):
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)

    async def _verify(request: VerifyOrderRequest) -> OrderVerdict:
        # 한쪽 방향 잠금 — 코드가 거부한 제안에 LLM이 승인 의견을 낼 기회 자체를 만들지 않는다.
        if not request.hard_check.passed:
            logger.warning(
                "hard_check 미통과 제안이 검증자에 도달 — LLM 호출 없이 REJECT (proposal_id=%s)",
                request.proposal_id,
            )
            return OrderVerdict(
                proposal_id=request.proposal_id,
                verdict="REJECT",
                reason="코드 한도 검사를 통과하지 않은 제안입니다.",
                failure=FAILURE_HARD_CHECK,
            )

        try:
            response = await asyncio.wait_for(
                llm.ainvoke(
                    [
                        SystemMessage(content=_SYSTEM_PROMPT),
                        HumanMessage(content=build_user_prompt(request)),
                    ]
                ),
                timeout=config.timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.error(
                "검증자 LLM 타임아웃(%.1fs) — REJECT (proposal_id=%s)",
                config.timeout_sec,
                request.proposal_id,
            )
            return OrderVerdict(
                proposal_id=request.proposal_id,
                verdict="REJECT",
                reason="검증이 제한 시간 안에 끝나지 않아 보류했습니다.",
                failure=FAILURE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 승인으로 흐르면 안 된다.
            logger.exception(
                "검증자 LLM 호출 실패 — REJECT (proposal_id=%s): %s", request.proposal_id, exc
            )
            return OrderVerdict(
                proposal_id=request.proposal_id,
                verdict="REJECT",
                reason="검증을 수행하지 못해 보류했습니다.",
                failure=FAILURE_LLM_ERROR,
            )

        return parse_verdict(str(getattr(response, "content", response)), request)

    yield FunctionInfo.from_fn(_verify, description=config.description)
