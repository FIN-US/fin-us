"""규칙 기반 주문 보조 (#299).

``/advise <종목명>`` 한 번이 여기로 들어와 다섯 단계를 지난다:

1. 종목코드를 MCP로 확정하고(냉각 키와 주문 가능 여부가 이 코드 위에 선다),
   냉각이 아니면 NAT ``/v1/propose-order``로 제안을 받는다(전용 프롬프트, JSON 응답 요구).
2. 제안을 파싱해 확정한 코드와 대조하고, KIS 스냅샷(현금·현재가·보유량·
   총평가)을 직접 조회한 뒤 **하드 한도를 코드로 판정한다**.
3. 한도를 통과한 제안만 NAT ``/v1/verify-order``로 넘긴다.
4. verdict를 파싱한다. APPROVE가 아니면 거부다.
5. APPROVE면 기존 :class:`PendingOrder` 경로에 합류한다 — 60초 확정 버튼과 체결 흐름은
   이 모듈이 건드리지 않는다.

설계 원칙 세 가지가 이 모듈 전체를 지배한다.

**하드 한도는 코드가 판정한다.** :func:`evaluate_hard_limits`는 순수 함수이고, 위반이 하나라도
있으면 검증자를 **호출하지 않고** 거부로 끝난다. LLM은 코드의 거부를 승인으로 뒤집을 수단이
없다 — 뒤집을 수 있는 코드 경로 자체가 없다. 검증자는 통과한 제안을 한 번 더 거부할 수만 있다.

**fail-closed.** 제안 파싱 실패, 스냅샷 조회·파싱 실패, 검증 응답 파싱 실패, verdict 미지값,
proposal_id 불일치, 타임아웃, redis 조회 실패 — 전부 거부다. "확인하지 못했다"가 "괜찮다"로
바뀌는 지점을 하나도 남기지 않는다.

**확정 버튼 없이는 주문이 나가지 않는다.** 이 모듈이 만드는 것은 대기 주문과 그 프롬프트까지다.
체결은 사용자가 확정 버튼(또는 ``/confirm``)을 누른 뒤 기존 ``_handle_confirm``이 한다.

호출자는 :class:`OrderAssistResult` 세 상태를 모두 사용자에게 전달해야 한다. 특히 ``conflict``는
조용히 사라지면 안 된다 — 사용자는 자기가 방금 친 ``/advise``가 왜 아무 반응이 없는지 알 수 없다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal

import httpx
from sqlmodel import Session

from .config import (
    NAT_BASE_URL,
    ORDER_BLACKLIST,
    ORDER_MAX_DAILY_AMOUNT,
    ORDER_MAX_DAILY_COUNT,
    ORDER_MAX_ORDER_AMOUNT,
    ORDER_MAX_POSITION_RATIO,
    ORDER_MAX_PRICE_GAP_RATIO,
    ORDER_MIN_CASH_RATIO,
    ORDER_MIN_CONFIDENCE,
    ORDER_PROPOSE_TIMEOUT_SECONDS,
    ORDER_REPROPOSAL_COOLDOWN_MINUTES,
    ORDER_VERIFY_TIMEOUT_SECONDS,
    TRADING_MCP_PARAMS,
)
# services는 order_assist를 import하지 않으므로 순환하지 않는다. run_mcp_tool은
# 호출 시점에 늦게 가져오지만(테스트가 주입하는 것이 기본 경로다) short_error는
# 순수 포매터라 여기서 바로 묶는다.
from .services import short_error
# presentation은 의존이 없는 리프 모듈이라 여기서 바로 가져와도 순환하지 않는다.
# /advise 승인 메시지에는 제안자·검증자가 쓴 문장이 실린다 — 이 봇은 parse_mode를
# 쓰지 않으므로 마크다운 표기가 남으면 화면에 그대로 별표로 보인다 (#297).
from .presentation import sanitize_markdown
from .stock_code import (
    _ORDERABLE_STOCK_CODE_RE,
    _is_unresolved_echo,
    _STOCK_CODE_EXTRACT_RE,
    extract_stock_name,
)
from .timeutil import KST
from .trading_orders import ORDER_EXPIRES_AFTER, OrderSide, OrderType, PendingOrder, is_korean_market_open

logger = logging.getLogger(__name__)

ProposalSource = Literal["telegram", "scheduler_rule"]
AssistStatus = Literal["approved", "rejected", "conflict"]

CONFLICT_MESSAGE = "대기 중인 주문이 있어 제안을 보류했어요. /confirm 또는 /cancel로 먼저 처리하세요."

# 검증자 응답에서 정성 사유를 한 문장도 건지지 못했을 때 쓰는 고정 문구.
# 모델이 만든 문장이 아니라 코드 리터럴이다.
_GENERIC_VERDICT_REASON = "검증자가 추가 사유를 남기지 않았습니다."


# ---------------------------------------------------------------------------
# 입력 / 출력 자료형
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalTrigger:
    """제안을 요청한 계기.

    ``source``를 지금 두는 이유는 스케줄러 룰 트리거(#314)가 같은
    함수로 들어오게 하기 위해서다. 냉각 키가 종목코드+룰 단위라, 룰이 없는 텔레그램 수동
    요청과 룰 기반 자동 제안이 서로의 냉각을 잡아먹지 않는다.

    ``stock``은 사용자가 친 문자열 그대로다 — 종목명일 수도 코드일 수도 있다. 냉각 키와
    한도 판정에 쓰는 정본 식별자는 :func:`run_order_assist`가 MCP로 확정한 종목코드다.
    """

    source: ProposalSource
    stock: str
    chat_id: str
    rule_id: str | None = None
    trigger_signal: str | None = None


@dataclass(frozen=True)
class OrderProposal:
    """제안 에이전트가 낸 주문 한 건. 값은 전부 코드가 검증한 뒤에만 채워진다."""

    stock_name: str
    stock_code: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    price: int
    rationale: str
    confidence: float


@dataclass(frozen=True)
class AccountSnapshot:
    """주문 판정에 쓰는 계좌·시세 스냅샷. 전부 KIS에서 직접 조회한 값이다."""

    current_price: int
    cash: int
    total_value: int
    holding_qty: int


@dataclass(frozen=True)
class DailyUsage:
    """오늘(KST) 이미 나간 주문의 사용량. TradeHistory 집계."""

    order_count: int = 0
    order_amount: int = 0


@dataclass(frozen=True)
class OrderLimits:
    """하드 한도 묶음. 기본값은 ``backend.config``의 env 로드 결과다."""

    max_order_amount: int = ORDER_MAX_ORDER_AMOUNT
    max_position_ratio: float = ORDER_MAX_POSITION_RATIO
    max_daily_count: int = ORDER_MAX_DAILY_COUNT
    max_daily_amount: int = ORDER_MAX_DAILY_AMOUNT
    min_cash_ratio: float = ORDER_MIN_CASH_RATIO
    max_price_gap_ratio: float = ORDER_MAX_PRICE_GAP_RATIO
    # soft — 위반해도 코드가 거부하지 않는다. 검증자에게 참고 신호로만 넘어간다.
    min_confidence: float = ORDER_MIN_CONFIDENCE
    cooldown_minutes: int = ORDER_REPROPOSAL_COOLDOWN_MINUTES
    blacklist: frozenset[str] = ORDER_BLACKLIST

    def as_payload(self) -> dict[str, Any]:
        """검증자에게 참고용으로 넘길 직렬화 형태. 판정은 여전히 코드가 한다."""
        return {
            "max_order_amount": self.max_order_amount,
            "max_position_ratio": self.max_position_ratio,
            "max_daily_count": self.max_daily_count,
            "max_daily_amount": self.max_daily_amount,
            "min_cash_ratio": self.min_cash_ratio,
            "max_price_gap_ratio": self.max_price_gap_ratio,
            "min_confidence_soft": self.min_confidence,
            "cooldown_minutes": self.cooldown_minutes,
        }


@dataclass(frozen=True)
class LimitViolation:
    """한도 위반 한 건. ``message``의 수치는 전부 코드가 만든 것이다."""

    code: str
    message: str


@dataclass(frozen=True)
class HardCheckResult:
    violations: tuple[LimitViolation, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations

    def as_payload(self) -> dict[str, Any]:
        return {"passed": self.passed, "violations": [v.message for v in self.violations]}


@dataclass(frozen=True)
class OrderAssistResult:
    """``/advise`` 한 번의 결과.

    ``conflict``는 실패가 아니라 "지금은 때가 아니다"라는 상태다. 호출자가 이 상태를
    조용히 버리면 사용자는 명령이 씹힌 것으로 읽는다.
    """

    status: AssistStatus
    message: str
    order: PendingOrder | None = None
    violations: tuple[LimitViolation, ...] = ()
    verdict_reason: str = ""
    failure: str | None = None
    proposal: OrderProposal | None = None
    snapshot: AccountSnapshot | None = None
    verifier_called: bool = False


# ---------------------------------------------------------------------------
# 제안 프롬프트 / 파싱
# ---------------------------------------------------------------------------

PROPOSAL_PROMPT_TEMPLATE = """당신은 Fin-Us 주문 제안자입니다. 아래 종목 하나에 대해 지금 낼 만한 주문을 한 건만 제안하세요.

종목: {stock}
{trigger_line}
반드시 도구로 현재가·수급·뉴스를 확인한 뒤 판단하세요.

출력은 JSON 객체 하나뿐입니다. 코드블록이나 설명 문장을 붙이지 마세요.
{{
  "stock_name": "<종목명>",
  "stock_code": "<6자리 종목코드>",
  "side": "BUY" 또는 "SELL",
  "quantity": <주문 수량, 양의 정수>,
  "order_type": "LIMIT" 또는 "MARKET",
  "price": <지정가(원, 정수). MARKET이면 0>,
  "rationale": "<한국어 근거 두세 문장>",
  "confidence": <0.0~1.0 사이의 확신도>
}}

지금 주문할 이유가 없다면 side에 "HOLD"를 넣고 rationale에 그 이유를 쓰세요."""

_SIDES: dict[str, OrderSide] = {"BUY": "BUY", "SELL": "SELL"}
_ORDER_TYPES: dict[str, OrderType] = {"LIMIT": "LIMIT", "MARKET": "MARKET"}


def build_proposal_prompt(trigger: ProposalTrigger) -> str:
    trigger_line = ""
    if trigger.trigger_signal:
        trigger_line = f"참고 신호: {trigger.trigger_signal}\n"
    return PROPOSAL_PROMPT_TEMPLATE.format(stock=trigger.stock, trigger_line=trigger_line)


def _coerce_int(value: Any) -> int | None:
    """정수로 읽을 수 있으면 정수로, 아니면 None.

    모델은 ``10``, ``"10"``, ``"10주"``, ``10.0``을 섞어 낸다. 앞 셋은 받고
    소수점이 실제로 살아 있는 값(10.5주)은 거부한다.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        cleaned = re.sub(r"[,\s원주]", "", value.strip())
        if not cleaned:
            return None
        try:
            return int(cleaned)
        except ValueError:
            try:
                as_float = float(cleaned)
            except ValueError:
                return None
            return int(as_float) if as_float.is_integer() else None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def parse_proposal(raw: str) -> tuple[OrderProposal | None, str]:
    """제안 응답 텍스트에서 :class:`OrderProposal`을 뽑는다.

    실패 시 ``(None, 사유)``를 돌려준다 — 사유는 사용자에게 그대로 보여도 되는 문장이다.
    성공 시에도 ``stock_code``는 아직 신뢰하지 않는다. 호출부가 MCP로 다시 확인한다.
    """
    from .services import _json_objects_from_text

    text = (raw or "").strip()
    if not text:
        return None, "제안 응답이 비어 있어 진행하지 않았습니다."

    candidates = _json_objects_from_text(text)
    if not candidates:
        return None, "제안 응답을 해석하지 못해 진행하지 않았습니다."

    last_error = "제안 응답을 해석하지 못해 진행하지 않았습니다."
    for data in candidates:
        raw_side = data.get("side")
        side_key = raw_side.strip().upper() if isinstance(raw_side, str) else ""
        if side_key in ("HOLD", "NONE", "SKIP"):
            # 정상적인 "지금은 주문하지 않는다" 응답이다. 파싱 실패가 아니다.
            return None, "지금은 주문을 제안하지 않았습니다."
        if side_key not in _SIDES:
            last_error = "제안의 매매 방향을 알 수 없어 진행하지 않았습니다."
            continue

        raw_type = data.get("order_type")
        type_key = raw_type.strip().upper() if isinstance(raw_type, str) else "LIMIT"
        if type_key not in _ORDER_TYPES:
            last_error = "제안의 주문 유형을 알 수 없어 진행하지 않았습니다."
            continue

        quantity = _coerce_int(data.get("quantity"))
        if quantity is None or quantity <= 0:
            last_error = "제안의 주문 수량을 읽지 못해 진행하지 않았습니다."
            continue

        price = _coerce_int(data.get("price")) or 0
        if type_key == "LIMIT" and price <= 0:
            last_error = "제안의 지정가를 읽지 못해 진행하지 않았습니다."
            continue
        if price < 0:
            last_error = "제안의 지정가를 읽지 못해 진행하지 않았습니다."
            continue

        confidence = _coerce_float(data.get("confidence"))
        if confidence is None or not (0.0 <= confidence <= 1.0):
            # 확신도는 soft 한도지만, 값 자체를 못 읽으면 검증자에게 넘길 신호가 없다.
            # 0.0으로 두면 "확신 없음"이라는 관측이 아니라 조작된 관측이 되므로 거부한다.
            last_error = "제안의 확신도를 읽지 못해 진행하지 않았습니다."
            continue

        stock_name = data.get("stock_name")
        rationale = data.get("rationale")
        code = data.get("stock_code")
        return (
            OrderProposal(
                stock_name=stock_name.strip() if isinstance(stock_name, str) else "",
                stock_code=code.strip().upper() if isinstance(code, str) else "",
                side=_SIDES[side_key],
                quantity=quantity,
                order_type=_ORDER_TYPES[type_key],
                price=price if type_key == "LIMIT" else 0,
                rationale=rationale.strip() if isinstance(rationale, str) else "",
                confidence=confidence,
            ),
            "",
        )

    return None, last_error


# ---------------------------------------------------------------------------
# KIS 스냅샷 파싱 — mcp-trading의 출력 계약에 붙는다
# ---------------------------------------------------------------------------

# mcp-trading/index.js getStockQuote(): "- 현재가: 74,500원"
_CURRENT_PRICE_RE = re.compile(r"현재가:\s*([\d,]+)\s*원")
# mcp-trading/balance.js formatBalanceReport(): "- 거래가능금액: 1,000,000원"
#
# 이 값의 출처는 balance.js:292의 summary.dnca_tot_amt, 즉 **예수금총금액**이다. 같은
# 필드를 balance-rlz-pl-report.js:25는 "예수금"이라고 부른다 — "거래가능금액"이라는
# 라벨이 붙어 있을 뿐 KIS의 주문가능현금(inquire-psbl-order의 ord_psbl_cash)이 아니다.
# 미수/증거금/미결제 정산이 반영되지 않으므로 실제 주문가능액과 어긋날 수 있다.
# 그래서 사용자에게도 "예수금"으로 표기한다(format_approval_message).
# 정확한 주문가능현금 조회는 후속 이슈다 (#310).
#
# "주문가능금액" 대안은 mcp-trading 어디에도 없는 라벨이라 지금은 아무것도 매치하지
# 않는다. 방어적으로 남겨 두지만 계약이 아니다 — 라벨이 바뀌면 cash=None으로 떨어져
# fail-closed로 거부되므로, 이 대안이 없어도 한도가 헐거워지지는 않는다.
_CASH_RE = re.compile(r"(?:거래가능금액|주문가능금액):\s*([\d,]+)\s*원")
# "- 총 평가금액: 1,210,000원"
_TOTAL_VALUE_RE = re.compile(r"총\s*평가금액:\s*([\d,]+)\s*원")


def _first_amount(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text or "")
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_current_price(quote_text: str) -> int | None:
    """``get_stock_quote`` 응답에서 현재가를 읽는다. 읽지 못하면 ``None``.

    :func:`parse_snapshot`만 쓰던 것을 따로 뺐다. /buy의 시장가 경로도 같은 값을
    같은 계약으로 읽어야 하기 때문이다(#309) — 정규식을 그쪽에 한 벌 더 두면
    mcp-trading의 출력 라벨이 바뀔 때 한쪽만 고쳐지고 다른 쪽은 조용히 None으로
    떨어진다.

    0 이하는 ``None``과 같이 취급한다. 현재가 0은 "공짜"가 아니라 "못 읽었다"다.
    """
    price = _first_amount(_CURRENT_PRICE_RE, quote_text)
    if price is None or price <= 0:
        return None
    return price


def parse_snapshot(
    quote_text: str,
    balance_text: str,
    stock_code: str,
) -> tuple[AccountSnapshot | None, str]:
    """시세·잔고 응답에서 :class:`AccountSnapshot`을 만든다.

    한 값이라도 읽지 못하면 ``(None, 사유)``다 — 한도 판정은 이 네 값 위에 서 있으므로,
    빠진 값을 0으로 채우면 한도가 조용히 무력해진다(현금 0은 "현금 없음"이 아니라
    "현금을 확인하지 못함"이다).

    보유량만 예외다. 잔고 섹션 마커가 있는데 해당 종목 줄이 없으면 그것은 관측된
    0주다. 마커 자체가 없으면 응답을 읽지 못한 것이므로 실패로 본다.
    """
    from .scheduler import _BALANCE_HOLDINGS_MARKER, _parse_balance_holdings, is_balance_truncated

    current_price = parse_current_price(quote_text)
    if current_price is None:
        return None, "현재가를 확인하지 못해 진행하지 않았습니다."

    if _BALANCE_HOLDINGS_MARKER not in (balance_text or ""):
        return None, "계좌 잔고를 확인하지 못해 진행하지 않았습니다."

    # 잘린 잔고는 "마커도 있고 파싱도 되는" 응답이라 위 검사를 그대로 통과한다. 그런데
    # 대상 종목 줄이 잘려 나갔으면 holding_qty가 관측된 0주가 아니라 **못 본 0주**가 되고,
    # 그 순간 두 한도가 조용히 넓어진다:
    #   - BUY 비중: 기존 보유분을 빼먹고 계산해 한도를 넘는 주문이 통과한다.
    #   - SELL 보유량: 실제로는 있는 보유량을 0으로 보므로 oversell 판정이 뒤집힌다.
    # 총 평가금액도 부분 집계일 수 있어 min_cash_ratio의 분모까지 흔들린다.
    # _parse_balance_holdings는 이 가드를 호출처의 몫으로 명시하고 있고, 기존 호출처
    # (scheduler의 포트폴리오 동기화 두 곳)는 모두 같은 가드를 건다.
    if is_balance_truncated(balance_text or ""):
        return None, "계좌 잔고 조회가 중간에 끊겨(일부 종목 누락) 진행하지 않았습니다."

    cash = _first_amount(_CASH_RE, balance_text)
    if cash is None:
        return None, "예수금을 확인하지 못해 진행하지 않았습니다."

    total_value = _first_amount(_TOTAL_VALUE_RE, balance_text)
    if total_value is None or total_value <= 0:
        return None, "총 평가금액을 확인하지 못해 진행하지 않았습니다."

    holding_qty = 0
    for holding in _parse_balance_holdings(balance_text):
        if holding.code.upper() == stock_code.upper():
            holding_qty = holding.quantity
            break

    return AccountSnapshot(
        current_price=current_price,
        cash=cash,
        total_value=total_value,
        holding_qty=holding_qty,
    ), ""


# ---------------------------------------------------------------------------
# 하드 한도 판정 — 전부 순수 함수
# ---------------------------------------------------------------------------


def check_orderable_code(stock_code: str, blacklist: frozenset[str]) -> LimitViolation | None:
    """"이 코드로 주문을 낼 수 있는가"의 두 갈래를 한자리에서 판정한다.

    주문 가능 코드 형태(``_ORDERABLE_STOCK_CODE_RE``, mcp-trading/order.js 정책의
    백엔드 복제본)와 블랙리스트를 붙여 둔 이유는, 둘을 떨어뜨리면 한쪽만 통과하는
    경로가 생기기 때문이다. 새 코드 제약이 생기면 여기에 함께 넣는다.
    """
    code = (stock_code or "").strip().upper()
    if not _ORDERABLE_STOCK_CODE_RE.fullmatch(code):
        return LimitViolation(
            "unorderable_code",
            f"{code or '(코드 없음)'} — 주문을 지원하지 않는 종목코드입니다.",
        )
    if code in blacklist:
        return LimitViolation("blacklisted", f"{code} — 주문 금지 목록에 있는 종목입니다.")
    return None


def estimated_unit_price(proposal: OrderProposal, snapshot: AccountSnapshot) -> int:
    """주문 금액 계산에 쓸 단가. 시장가는 현재가로 본다."""
    if proposal.order_type == "MARKET":
        return snapshot.current_price
    return proposal.price


def evaluate_hard_limits(
    proposal: OrderProposal,
    snapshot: AccountSnapshot,
    limits: OrderLimits,
    usage: DailyUsage,
) -> HardCheckResult:
    """하드 한도를 전부 평가해 위반 목록을 돌려준다.

    첫 위반에서 멈추지 않는 이유: 사용자는 "왜 거부됐는지"를 한 번에 알아야 다음 요청을
    고칠 수 있다. 하나씩 알려주면 같은 제안으로 여러 번 왕복하게 된다.

    확신도(:attr:`OrderLimits.min_confidence`)는 여기서 판정하지 않는다 — soft 한도라
    검증자 참고 신호로만 넘어간다. 코드가 거부하는 것은 hard 한도뿐이다.

    재제안 냉각도 여기 없다. 냉각은 제안을 요청하기 **전에** redis로 판정해야 의미가
    있어서(냉각 중이면 LLM 왕복 자체를 아끼는 것이 목적) run_order_assist 앞단에 있다.
    여기에 같은 규칙을 한 번 더 두면 프로덕션에서는 한 번도 참이 되지 않는 분기가 생기고,
    테스트만 그 죽은 분기를 타게 된다.
    """
    violations: list[LimitViolation] = []

    unit_price = estimated_unit_price(proposal, snapshot)
    amount = unit_price * proposal.quantity

    if amount > limits.max_order_amount:
        violations.append(
            LimitViolation(
                "order_amount",
                f"1회 주문 한도 초과: 주문금액 {amount:,}원 > 한도 {limits.max_order_amount:,}원",
            )
        )

    if proposal.order_type == "LIMIT":
        gap = abs(proposal.price - snapshot.current_price) / snapshot.current_price
        if gap > limits.max_price_gap_ratio:
            violations.append(
                LimitViolation(
                    "price_gap",
                    f"지정가 괴리 초과: 지정가 {proposal.price:,}원, 현재가 "
                    f"{snapshot.current_price:,}원 (허용 {limits.max_price_gap_ratio:.1%})",
                )
            )

    if usage.order_count + 1 > limits.max_daily_count:
        violations.append(
            LimitViolation(
                "daily_count",
                f"일 주문 횟수 한도 초과: 오늘 {usage.order_count}회 + 1회 > "
                f"한도 {limits.max_daily_count}회",
            )
        )

    if usage.order_amount + amount > limits.max_daily_amount:
        violations.append(
            LimitViolation(
                "daily_amount",
                f"일 거래대금 한도 초과: 오늘 {usage.order_amount:,}원 + {amount:,}원 > "
                f"한도 {limits.max_daily_amount:,}원",
            )
        )

    if proposal.side == "BUY":
        # 비중은 주문 체결 후 기준으로 본다 — 지금 비중이 아니라 이 주문이 만들 비중이 한도다.
        position_value = (snapshot.holding_qty + proposal.quantity) * unit_price
        ratio = position_value / snapshot.total_value
        if ratio > limits.max_position_ratio:
            violations.append(
                LimitViolation(
                    "position_ratio",
                    f"종목 비중 한도 초과: 주문 후 평가금액 {position_value:,}원 / 총 "
                    f"{snapshot.total_value:,}원 = {ratio:.1%} > 한도 "
                    f"{limits.max_position_ratio:.1%}",
                )
            )

        if amount > snapshot.cash:
            violations.append(
                LimitViolation(
                    "insufficient_cash",
                    f"예수금 부족: 주문금액 {amount:,}원 > 예수금 {snapshot.cash:,}원",
                )
            )
        else:
            remaining = snapshot.cash - amount
            floor = snapshot.total_value * limits.min_cash_ratio
            if remaining < floor:
                violations.append(
                    LimitViolation(
                        "cash_floor",
                        f"현금 최소 비중 미달: 주문 후 현금 {remaining:,}원 < 최소 "
                        f"{int(floor):,}원 (총 평가금액의 {limits.min_cash_ratio:.0%})",
                    )
                )
    else:  # SELL
        if proposal.quantity > snapshot.holding_qty:
            violations.append(
                LimitViolation(
                    "oversell",
                    f"보유량 초과 매도: 주문 {proposal.quantity:,}주 > 보유 "
                    f"{snapshot.holding_qty:,}주",
                )
            )

    return HardCheckResult(tuple(violations))


# ---------------------------------------------------------------------------
# 사용량 집계 / 냉각
# ---------------------------------------------------------------------------


def _kst_day_start_utc(now: datetime) -> datetime:
    """*now*가 속한 KST 하루의 시작을 UTC naive datetime으로 돌려준다.

    ``TradeHistory.trade_date``가 tz 없는 UTC로 저장되므로(models.py의
    ``datetime.now(timezone.utc)``가 컬럼에 naive로 내려간다) 비교 값도 같은 축으로 맞춘다.
    """
    from datetime import timezone

    local = now.astimezone(KST) if now.tzinfo is not None else now.replace(tzinfo=KST)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def load_daily_usage(session_factory: Callable[[], Any], now: datetime) -> DailyUsage:
    """오늘(KST) 체결 이력에서 주문 횟수와 거래대금을 집계한다."""
    from .models import TradeHistory

    session = session_factory()
    try:
        since = _kst_day_start_utc(now)
        rows = session.query(TradeHistory).filter(TradeHistory.trade_date >= since).all()
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()

    # 단가가 0 이하인 행은 "0원짜리 거래"가 아니라 **금액을 모르는 거래**다.
    # 그 행을 0원으로 더하면 일 거래대금 한도가 시장가 이력에 대해 **있는 척만 하는
    # 한도**가 된다. 그래서 더하지 않고 집계를 포기한다 — 이 모듈의 규칙("확인하지
    # 못했다"가 "괜찮다"로 바뀌는 지점을 남기지 않는다)이 여기에도 걸린다.
    #
    # #309에서 이 가드를 걷어낼지 검토했고, 걷어내지 않기로 했다. 대신 0이 여기까지
    # 오지 않게 기록 쪽을 고쳤다:
    #   - KIS 현금주문 응답(order-cash)에는 체결가가 없다. output은 KRX_FWDG_ORD_ORGNO·
    #     ODNO·ORD_TMD뿐이라(mcp-trading/order.js formatOrderResult) "체결가를 되받는"
    #     경로 자체가 존재하지 않는다.
    #   - 그래서 두 주문 경로 모두 시장가에 **주문 시점 현재가**를 기록용 참고단가로
    #     싣는다(telegram_commands._handle_order_command, run_order_assist).
    #   - 그 현재가마저 읽지 못하면 참고단가 없이 주문이 나가고 여기에 0이 남는다.
    #     그때는 이 가드가 그대로 작동해 /advise가 usage_failed로 막힌다. 한도가
    #     조용히 넓어지는 것보다 /advise가 막히는 쪽이 낫다는 판단이다(#309).
    #
    # 정상 운영에서 이 raise는 이제 닿지 않는다. 닿는 경우는 둘뿐이다 — 위의 현재가
    # 파싱 실패, 그리고 #309 이전에 쌓인 단가 0 행이 오늘 자로 남아 있는 경우(과거 행은
    # 지우지 않기로 했고, 이 집계는 오늘 KST 행만 보므로 배포 당일 하루만 해당된다).
    # 판정과 합산을 같은 축(int)으로 둔다 — 한쪽만 float면 0 < price < 1인 행이
    # 가드는 통과하고 합계에는 0을 보태는 틈이 생긴다.
    unpriced = [row for row in rows if int(row.price) <= 0]
    if unpriced:
        raise ValueError(
            f"단가가 기록되지 않은 오늘 거래 {len(unpriced)}건이 있어 일 거래대금을 "
            "집계할 수 없습니다(시장가 주문 이력)."
        )

    amount = sum(int(row.quantity) * int(row.price) for row in rows)
    return DailyUsage(order_count=len(rows), order_amount=amount)


class ProposalCooldown:
    """종목코드+룰 단위 재제안 냉각. redis TTL 하나로 강제한다.

    조회에 실패하면 "냉각 아님"이 아니라 **냉각 중으로 본다**. redis가 죽었을 때
    한도가 조용히 풀리면 그게 이 클래스가 막으려던 상황 그 자체다.

    키가 종목**코드**인 것이 이 클래스의 전제다. 사용자가 친 문자열을 키로 쓰면
    "삼성전자"와 "005930"이 다른 키가 되어 냉각이 별칭 한 번에 뚫린다. 호출부
    (:func:`run_order_assist`)가 제안 왕복 전에 코드를 확인해 두는 이유가 이것이다.
    """

    def __init__(self, client_factory: Callable[[], Any] | None = None, *, ttl_seconds: int | None = None):
        self._client_factory = client_factory
        self.ttl_seconds = (
            ttl_seconds if ttl_seconds is not None else ORDER_REPROPOSAL_COOLDOWN_MINUTES * 60
        )

    def _factory(self) -> Callable[[], Any]:
        if self._client_factory is not None:
            return self._client_factory
        from .redis_state import create_redis_client

        return create_redis_client

    @staticmethod
    def key(stock_code: str, rule_id: str | None) -> str:
        from .redis_state import RedisKeys

        return RedisKeys().order_proposal_cooldown(stock_code, rule_id)

    async def active(self, stock_code: str, rule_id: str | None) -> tuple[bool, bool]:
        """``(냉각 중인가, 조회에 성공했는가)``.

        두 값을 함께 돌려주는 이유는 호출부가 "냉각 중"과 "확인 불가"를 사용자에게
        다른 문장으로 알려야 하기 때문이다. 둘 다 진행은 막는다.
        """
        client = self._factory()()
        try:
            return bool(await client.exists(self.key(stock_code, rule_id))), True
        except Exception as exc:  # noqa: BLE001 — fail-closed
            logger.error("재제안 냉각 조회 실패 — 진행을 막는다: %s", exc)
            return True, False
        finally:
            await _aclose(client)

    async def mark(self, stock_code: str, rule_id: str | None) -> None:
        """냉각을 건다. 실패해도 예외를 올리지 않는다 — 이미 판정은 끝났다."""
        client = self._factory()()
        try:
            await client.set(self.key(stock_code, rule_id), "proposed", ex=self.ttl_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.error("재제안 냉각 기록 실패: %s", exc)
        finally:
            await _aclose(client)


async def _aclose(client: Any) -> None:
    aclose = getattr(client, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:  # noqa: BLE001 — 정리 실패가 판정을 바꾸면 안 된다.
        logger.warning("redis 클라이언트 정리 실패: %s", exc)


# ---------------------------------------------------------------------------
# NAT 호출
# ---------------------------------------------------------------------------

# 검증 실패 코드 — finus_nat/src/nat_finus_nat/verifier.py와 짝을 이룬다.
# 여기 있는 값은 backend가 자체적으로 판정한 실패이고, 검증자가 실어 보낸 값은
# verdict payload의 failure 필드에 그대로 들어 있다.
FAILURE_HTTP = "verify_http_error"
FAILURE_TIMEOUT = "verify_timeout"
FAILURE_PARSE = "verify_parse_error"
FAILURE_ID_MISMATCH = "verify_proposal_id_mismatch"


@dataclass(frozen=True)
class VerifierVerdict:
    approved: bool
    reason: str = ""
    failure: str | None = None


async def _post_json(path: str, payload: dict[str, Any], timeout: float) -> Any:
    url = f"{NAT_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        response = await client.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"NAT {path} 응답 {response.status_code}: {response.text[:300]}")
    return response.json()


async def request_proposal(prompt: str, *, timeout: float = ORDER_PROPOSE_TIMEOUT_SECONDS) -> str:
    """``POST /v1/propose-order``.

    이 엔드포인트는 ``fe_branch``(환각 게이트)를 노출하므로 요청은
    ``{"input_message": ...}``이고 응답은 ``{"value": "<답변 텍스트>"}``다.
    NAT은 str 반환 함수의 응답을 ``OutputArgsSchema``로 감싼다 — 로컬 기동 후
    curl로 확인한 계약이다(finus_nat/configs/agents/verifier_agent.yml 주석 참조).
    """
    payload = await _post_json("/v1/propose-order", {"input_message": prompt}, timeout)
    if isinstance(payload, dict):
        value = payload.get("value")
        if isinstance(value, str):
            return value
        # 워크플로 실패는 {"code","message","details"} 봉투로 온다(HTTP 200이 아닐 때가 많지만
        # 형식을 신뢰하지 않는다). 답변 텍스트가 아니면 진행하지 않는다.
        raise RuntimeError(f"NAT /v1/propose-order 응답 형식 오류: {json.dumps(payload)[:300]}")
    raise RuntimeError("NAT /v1/propose-order 응답이 JSON 객체가 아닙니다.")


async def request_verification(
    payload: dict[str, Any],
    proposal_id: str,
    *,
    timeout: float = ORDER_VERIFY_TIMEOUT_SECONDS,
) -> VerifierVerdict:
    """``POST /v1/verify-order``. 승인이 아닌 모든 결과는 거부다.

    ``order_verifier``는 단일 BaseModel을 받고 반환하므로 요청·응답 모두 래핑이 없는
    평평한 JSON이다. 이것도 로컬 기동 후 curl로 확인한 계약이다.

    id 대조를 검증자 쪽에서 이미 하는데 여기서 또 하는 이유: 검증자의 대조는
    "모델이 다른 제안을 판정했는가"를 보고, 여기 대조는 "다른 제안의 응답이 우리에게
    도달했는가"를 본다. 라우팅·프록시 사고는 검증자가 볼 수 없는 층에서 일어난다.
    """
    try:
        body = await _post_json("/v1/verify-order", payload, timeout)
    except (httpx.TimeoutException, asyncio.TimeoutError):
        logger.error("검증 요청 타임아웃(%.1fs) — 거부 (proposal_id=%s)", timeout, proposal_id)
        return VerifierVerdict(False, "검증이 제한 시간 안에 끝나지 않았습니다.", FAILURE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 승인으로 흐르면 안 된다.
        logger.error("검증 요청 실패 — 거부 (proposal_id=%s): %s", proposal_id, exc)
        return VerifierVerdict(False, "검증을 수행하지 못했습니다.", FAILURE_HTTP)

    if not isinstance(body, dict):
        return VerifierVerdict(False, "검증 응답을 해석하지 못했습니다.", FAILURE_PARSE)

    raw_verdict = body.get("verdict")
    verdict = raw_verdict.strip().upper() if isinstance(raw_verdict, str) else ""
    if verdict not in ("APPROVE", "REJECT"):
        logger.warning("검증 응답 verdict 미지값 — 거부 (got=%r)", raw_verdict)
        return VerifierVerdict(False, "검증 결과를 판정하지 못했습니다.", FAILURE_PARSE)

    echoed = body.get("proposal_id")
    if not isinstance(echoed, str) or echoed.strip() != proposal_id:
        logger.warning(
            "검증 응답 proposal_id 불일치 — 거부 (expected=%s, got=%r)", proposal_id, echoed
        )
        return VerifierVerdict(False, "검증 대상이 일치하지 않았습니다.", FAILURE_ID_MISMATCH)

    reason = body.get("reason")
    reason_text = reason.strip() if isinstance(reason, str) else ""
    failure = body.get("failure")
    return VerifierVerdict(
        approved=verdict == "APPROVE",
        reason=reason_text,
        failure=failure if isinstance(failure, str) else None,
    )


# ---------------------------------------------------------------------------
# 사용자 메시지 조립 — 수치는 전부 코드가 만든다
# ---------------------------------------------------------------------------

# 숫자 한 덩어리(자릿수 구분 쉼표·소수점 포함)를 통째로 잡는다. "74,500"이 "74"와
# "500" 둘로 쪼개져 마스킹되지 않도록 쉼표·마침표를 숫자 사이에서만 붙여 받는다.
_NUMBER_RUN_RE = re.compile(r"\d[\d,.]*")
_NUMBER_MASK = "[수치]"
# 마스킹 뒤 "[수치][수치]"처럼 붙어 남는 것을 하나로 접는다(예: "3~4%" → "[수치]~[수치]"는
# 그대로 두고, "1,2,3" 같은 나열이 만드는 연속 마스크만 정리한다).
_REPEATED_MASK_RE = re.compile(r"(?:\[수치\]\s*){2,}")


def qualitative_reason(reason: str) -> str:
    """검증자 문장의 **숫자를 가리고** 정성 사유는 남긴다.

    검증자는 도구 없이 답하므로 그 문장 속 수치는 backend가 관측한 값이라는 보장이
    없다. 사용자에게 보이는 숫자는 전부 페이로드 원본(제안·스냅샷)에서만 만든다 —
    그 불변식은 마스킹으로도 그대로 성립한다(마스킹 후 응답에는 숫자가 남지 않는다).

    문장 단위로 버리지 않는 이유(#299 2차 리뷰): ``_SENTENCE_SPLIT_RE``는 구두점+공백이나
    개행을 요구하는데, 검증자는 한국어 한 문장으로 답하는 경우가 많다. 그러면 전체가
    한 덩어리라 숫자 하나 때문에 사유가 통째로 사라지고, 거부 메시지의 위반 목록은
    "검증자가 보류를 권고했습니다." 한 줄뿐이라 사용자는 **거부 사유를 하나도 못 본다**.
    수치만 가리면 "현재가 [수치]원 수준에서 단기 과열로 판단되어 보류를 권고합니다"가
    남아 사유가 전달되고, 수치 노출 위험은 없다.
    """
    # 마크다운 표기를 먼저 벗긴다 — 봇이 parse_mode를 쓰지 않으므로 남으면 별표가
    # 그대로 보인다. 다른 LLM 응답 경로와 같은 정리를 거친다 (#297).
    text = sanitize_markdown(reason or "").strip()
    if not text:
        return _GENERIC_VERDICT_REASON
    masked = _NUMBER_RUN_RE.sub(_NUMBER_MASK, text)
    masked = _REPEATED_MASK_RE.sub(f"{_NUMBER_MASK} ", masked).strip()
    # 수치 나열뿐이던 응답은 마스크와 구분기호만 남는다 — 정성 사유가 없는 것이므로
    # 고정 문구로 간다. 공백·구두점만으로는 "사유가 있다"고 볼 수 없어 글자·숫자가
    # 하나라도 남는지로 판정한다("[수치] / [수치]"의 잔여물 " / "는 사유가 아니다).
    residual = masked.replace(_NUMBER_MASK, "")
    return masked if any(ch.isalnum() for ch in residual) else _GENERIC_VERDICT_REASON


def _side_text(side: OrderSide) -> str:
    return "매수" if side == "BUY" else "매도"


def format_approval_message(
    proposal: OrderProposal,
    snapshot: AccountSnapshot,
    order: PendingOrder,
    verdict: VerifierVerdict,
) -> str:
    """승인 메시지. 숫자는 전부 ``order``/``snapshot``/``proposal``에서 나온다.

    ``rationale``은 제안 에이전트의 문장이라 그대로 싣는다 — 제안 경로는
    ``fe_branch``의 도구 강제 게이트를 지나므로, 도구 없이 지어낸 수치는 그 층에서 걸린다.
    검증자 문장은 게이트 밖이라 :func:`qualitative_reason`으로 숫자를 걷어낸다.
    """
    amount = order.quantity * (order.price if order.order_type == "LIMIT" else snapshot.current_price)
    lines = [
        f"💡 {order.stock_name} {_side_text(order.side)} 제안",
        f"종목코드: {order.stock_code}",
        f"수량: {order.quantity:,}주",
        f"주문유형: {'시장가' if order.order_type == 'MARKET' else '지정가'}",
    ]
    if order.order_type == "LIMIT":
        lines.append(f"지정가: {order.price:,}원")
    lines.extend(
        [
            f"예상 주문금액: {amount:,}원",
            f"현재가: {snapshot.current_price:,}원",
            f"예수금: {snapshot.cash:,}원",
            f"보유수량: {snapshot.holding_qty:,}주",
            f"확신도: {proposal.confidence:.2f}",
        ]
    )
    # rationale은 제안 에이전트가 쓴 문장이다 — 수치는 그대로 싣지만(도구 강제 게이트를
    # 지난 값이다) 마크다운 표기는 다른 LLM 응답 경로와 같이 벗긴다 (#297).
    rationale = sanitize_markdown(proposal.rationale)
    if rationale:
        lines.append(f"근거: {rationale}")
    lines.append(f"검증 의견: {qualitative_reason(verdict.reason)}")

    expires_at = order.created_at + ORDER_EXPIRES_AFTER
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=KST)
    lines.extend(
        [
            "",
            "확정 버튼을 누르거나 /confirm을 입력해야 주문이 나갑니다.",
            "/cancel 입력 시 대기 주문을 취소합니다.",
            f"이 제안은 {expires_at.astimezone(KST):%H:%M:%S}에 만료됩니다.",
        ]
    )
    return "\n".join(lines)


def format_rejection_message(
    stock: str,
    violations: tuple[LimitViolation, ...],
    verdict_reason: str = "",
) -> str:
    lines = [f"🚫 {stock} 주문 제안을 보류했습니다."]
    if violations:
        lines.append("")
        lines.extend(f"- {violation.message}" for violation in violations)
    if verdict_reason:
        lines.extend(["", f"검증 의견: {verdict_reason}"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 본 흐름
# ---------------------------------------------------------------------------


async def _drop_expired_pending_order(pending_orders: Any, chat_id: str, now: datetime) -> None:
    """앱 레벨 만료(``ORDER_EXPIRES_AFTER``)가 지난 대기 주문을 지운다.

    telegram_commands의 동명 메서드와 같은 판정이다. 두 곳이 같은 규칙을 쓰는 것이
    핵심이라 만료 창(ORDER_EXPIRES_AFTER)은 공유 상수 하나에서 읽고, tz 없는 값의
    해석도 양쪽 모두 KST로 맞춘다.

    *now*까지 보정하는 이유: ``now_factory`` 기본값이 ``datetime.now(KST)``라 지금은
    naive가 들어오지 않지만, 보정을 빼면 이 헬퍼만 naive를 **시스템 로컬**로 읽는다.
    같은 ``run_order_assist`` 안에서 ``is_korean_market_open``은 naive를 KST로 읽으므로,
    한 함수 안에 해석이 두 갈래로 갈린 채 남는다. 상수를 공유하는 것만으로는 "두 경로의
    만료 판정이 갈라지지 않는다"가 절반만 성립한다.
    """
    order = await pending_orders.get(chat_id)
    if order is None:
        return
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    created_at = order.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=KST)
    if now.astimezone(KST) - created_at.astimezone(KST) > ORDER_EXPIRES_AFTER:
        await pending_orders.delete(chat_id)


def _new_proposal_id(now: datetime) -> str:
    return f"adv-{now.astimezone(KST):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


def _rejected(
    stock: str,
    violations: tuple[LimitViolation, ...],
    *,
    verdict_reason: str = "",
    failure: str | None = None,
    verifier_called: bool = False,
    proposal: OrderProposal | None = None,
    snapshot: AccountSnapshot | None = None,
) -> OrderAssistResult:
    return OrderAssistResult(
        status="rejected",
        message=format_rejection_message(stock, violations, verdict_reason),
        violations=violations,
        verdict_reason=verdict_reason,
        failure=failure,
        verifier_called=verifier_called,
        proposal=proposal,
        snapshot=snapshot,
    )


async def run_order_assist(
    trigger: ProposalTrigger,
    *,
    pending_orders: Any,
    mcp_runner: Callable[[Any, str, dict[str, Any]], Awaitable[str]] | None = None,
    now_factory: Callable[[], datetime] | None = None,
    limits: OrderLimits | None = None,
    cooldown: ProposalCooldown | None = None,
    session_factory: Callable[[], Any] | None = None,
    propose: Callable[[str], Awaitable[str]] | None = None,
    verify: Callable[[dict[str, Any], str], Awaitable[VerifierVerdict]] | None = None,
) -> OrderAssistResult:
    """제안 → 코드 한도 → 검증 → 확정 대기까지 한 번에 수행한다.

    승인일 때만 대기 주문을 저장하며, 저장한 주문은 기존 확정 버튼 경로가 소비한다.
    이 함수는 주문을 **내지 않는다**.
    """
    if mcp_runner is None:
        from .services import run_mcp_tool as mcp_runner  # type: ignore[assignment]
    if session_factory is None:
        from .database import engine

        # 파라미터와 같은 이름으로 def를 쓰면 선언이 가려져(체커가 잡는다) 어느 쪽이
        # 유효한지 읽는 사람도 헷갈린다. 다른 이름으로 만들고 대입한다.
        def _default_session_factory() -> Session:
            return Session(engine)

        session_factory = _default_session_factory

    now_factory = now_factory or (lambda: datetime.now(KST))
    limits = limits or OrderLimits()
    cooldown = cooldown or ProposalCooldown(ttl_seconds=limits.cooldown_minutes * 60)
    propose = propose or request_proposal
    verify = verify or request_verification

    stock = trigger.stock.strip()
    if not stock:
        return _rejected(stock or "종목", (LimitViolation("empty_stock", "종목명이 비어 있습니다."),))

    now = now_factory()
    if not is_korean_market_open(now):
        # /buy와 같은 문턱이다. 대기 주문은 60초 뒤 실제 주문으로 이어지므로,
        # 장이 닫힌 시간에 확정 버튼을 띄우면 사용자는 눌러도 실패만 본다.
        return _rejected(
            stock,
            (LimitViolation("market_closed", "현재 장 운영 시간이 아닙니다. (평일 09:00~15:30)"),),
        )

    # ① 충돌 — 대기 주문이 이미 있으면 제안 자체를 만들지 않는다. 슬롯이 하나뿐이라
    #    지금 만들어 봐야 저장하지 못하고, LLM 왕복만 버린다.
    #
    #    검사 전에 만료분을 먼저 치운다(/buy가 같은 자리에서 하는 것과 동일하다).
    #    앱 만료(ORDER_EXPIRES_AFTER 60초)와 redis TTL(PENDING_ORDER_TTL_SEC 600초)이
    #    10배 차이라, 확정도 취소도 하지 않은 주문의 키는 약 9분간 남는다. 그 구간에서
    #    이 정리를 건너뛰면 /advise만 충돌로 막히면서 "/confirm 또는 /cancel로 먼저
    #    처리하세요"라고 안내하는데, 정작 그 주문은 이미 만료라 /confirm이 되지 않는
    #    막다른 길이 된다. InMemoryPendingOrderStore는 TTL 자체가 없어 더 오래 간다.
    try:
        await _drop_expired_pending_order(pending_orders, trigger.chat_id, now)
        has_pending = await pending_orders.has(trigger.chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("대기 주문 조회 실패 — 진행을 막는다: %s", exc)
        return _rejected(stock, (LimitViolation("store_error", f"주문 저장소 오류: {short_error(exc)}"),))
    if has_pending:
        return OrderAssistResult(status="conflict", message=CONFLICT_MESSAGE)

    # ② 종목코드 확인 — 냉각도 제안도 이 코드 위에 선다.
    #
    #    제안 왕복보다 **먼저** 하는 이유는 냉각 키 때문이다. 냉각을 사용자가 친
    #    문자열로 걸면 이름↔코드 별칭 한 번에 뚫린다 — "/advise 삼성전자"로 냉각이
    #    걸린 직후 "/advise 005930"은 키가 달라 그대로 통과한다. resolve_stock_code는
    #    이름과 코드를 같은 코드로 해석하므로 두 입력은 같은 종목이고, "같은 종목에
    #    대한 반복 제안을 막는다"는 냉각의 목적이 그 자리에서 무너진다. 한 종목을
    #    가리키는 정본 식별자는 종목코드 하나뿐이라 그것을 먼저 확보한다.
    #
    #    부수 효과가 하나 더 있다: 해석되지 않는 종목·주문 불가 코드가 제안 왕복
    #    (최대 ORDER_PROPOSE_TIMEOUT_SECONDS초)을 태우기 전에 끊긴다. 이 구간의 거부는
    #    냉각을 걸지 않는다 — 냉각이 아끼려는 자원(제안 왕복)을 애초에 쓰지 않았고,
    #    오타 한 번이 그 문자열을 냉각 시간만큼 잠글 이유도 없다.
    try:
        resolved = str(await mcp_runner(TRADING_MCP_PARAMS, "resolve_stock_code", {"stock_name": stock}))
    except Exception as exc:  # noqa: BLE001
        return _rejected(stock, (LimitViolation("resolve_failed", f"종목코드를 확인하지 못했습니다: {short_error(exc)}"),))

    code_match = _STOCK_CODE_EXTRACT_RE.search(resolved)
    if code_match is None or _is_unresolved_echo(resolved):
        return _rejected(
            stock,
            (LimitViolation("unresolved_stock", f"{stock} — 종목마스터에서 확인하지 못했습니다."),),
        )
    stock_code = code_match.group(1).upper()
    # split("(")로 자르면 이름에 괄호가 든 종목(주문 가능 코드만 178건)이 잘려 나간다.
    resolved_name = extract_stock_name(resolved) or stock

    # "이 코드로 주문을 낼 수 있는가"는 순수 함수 하나로 끝난다. 주문 금지 종목에
    # 제안 왕복을 태울 이유가 없어 여기서 본다.
    code_violation = check_orderable_code(stock_code, limits.blacklist)
    if code_violation is not None:
        return _rejected(stock, (code_violation,))

    # ③ 냉각 — 제안 호출 전에 본다. 냉각 중이면 LLM을 부를 이유가 없다.
    #    키는 ②에서 확인한 종목코드다. 입력 문자열이 아니다.
    cooling, checked = await cooldown.active(stock_code, trigger.rule_id)
    if cooling:
        message = (
            f"같은 종목의 재제안 냉각 시간({limits.cooldown_minutes}분)이 아직 남았습니다."
            if checked
            else "재제안 냉각 상태를 확인하지 못해 진행하지 않았습니다."
        )
        return _rejected(stock, (LimitViolation("cooldown", message),))

    # ④ 제안 받기 — 여기부터가 비싼 구간이다.
    try:
        raw = await propose(build_proposal_prompt(trigger))
    except Exception as exc:  # noqa: BLE001
        logger.error("제안 요청 실패: %s", exc)
        return _rejected(stock, (LimitViolation("propose_failed", f"제안을 받지 못했습니다: {short_error(exc)}"),))

    proposal, parse_error = parse_proposal(raw)
    if proposal is None:
        # 여기서부터는 제안 왕복을 실제로 소비했다 — 같은 종목으로 즉시 재시도해
        # 같은 실패를 반복하지 않도록 냉각을 건다.
        await cooldown.mark(stock_code, trigger.rule_id)
        return _rejected(stock, (LimitViolation("proposal_parse", parse_error),))

    if proposal.stock_code != stock_code:
        # 제안이 다른 종목을 말한 것이다. 우리가 확인한 코드로 "고쳐서" 진행하면
        # 근거와 주문이 어긋난 채 사용자에게 나간다.
        #
        # 빈 문자열도 여기서 걸린다. parse_proposal은 수량·지정가·확신도를 "못 읽으면
        # 거부"로 처리하는데(그 값이 없으면 판정할 근거가 없으므로) 종목코드만 예외를
        # 두면, 코드를 못 읽은 제안이 대조 없이 통과해 이 주석이 막겠다고 적은 상황이
        # 그대로 성립한다 — 어느 종목 근거인지 모르는 문장이 확정 버튼과 함께 나간다.
        await cooldown.mark(stock_code, trigger.rule_id)
        return _rejected(
            stock,
            (
                LimitViolation(
                    "proposal_stock_mismatch",
                    f"제안 종목({proposal.stock_code or '미상'})이 요청 종목({stock_code})과 다릅니다.",
                ),
            ),
        )

    # ⑤ 스냅샷 조회
    try:
        quote_text, balance_text = await asyncio.gather(
            mcp_runner(TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": stock}),
            mcp_runner(TRADING_MCP_PARAMS, "get_balance", {}),
        )
    except Exception as exc:  # noqa: BLE001
        await cooldown.mark(stock_code, trigger.rule_id)
        return _rejected(
            stock,
            (LimitViolation("snapshot_failed", f"계좌·시세를 조회하지 못했습니다: {short_error(exc)}"),),
            proposal=proposal,
        )

    snapshot, snapshot_error = parse_snapshot(str(quote_text), str(balance_text), stock_code)
    if snapshot is None:
        await cooldown.mark(stock_code, trigger.rule_id)
        return _rejected(stock, (LimitViolation("snapshot_parse", snapshot_error),), proposal=proposal)

    try:
        usage = load_daily_usage(session_factory, now)
    except Exception as exc:  # noqa: BLE001
        logger.error("일일 사용량 집계 실패 — 진행을 막는다: %s", exc)
        await cooldown.mark(stock_code, trigger.rule_id)
        return _rejected(
            stock,
            (LimitViolation("usage_failed", f"오늘 주문 사용량을 확인하지 못했습니다: {short_error(exc)}"),),
            proposal=proposal,
            snapshot=snapshot,
        )

    proposal = OrderProposal(
        stock_name=resolved_name,
        stock_code=stock_code,
        side=proposal.side,
        quantity=proposal.quantity,
        order_type=proposal.order_type,
        price=proposal.price,
        rationale=proposal.rationale,
        confidence=proposal.confidence,
    )

    # ⑥ 하드 한도 — 코드가 판정한다. 위반이 있으면 검증자를 부르지 않는다.
    hard_check = evaluate_hard_limits(proposal, snapshot, limits, usage)
    await cooldown.mark(stock_code, trigger.rule_id)
    if not hard_check.passed:
        logger.info(
            "하드 한도 위반으로 거부 — 검증자 미호출 (stock=%s, violations=%s)",
            stock,
            [v.code for v in hard_check.violations],
        )
        return _rejected(
            stock,
            hard_check.violations,
            proposal=proposal,
            snapshot=snapshot,
            verifier_called=False,
        )

    # ⑦ 검증 — 여기서 나올 수 있는 결과는 "그대로 통과" 아니면 "거부"뿐이다.
    proposal_id = _new_proposal_id(now)
    payload = {
        "proposal_id": proposal_id,
        "proposal": {
            "stock_name": proposal.stock_name,
            "stock_code": proposal.stock_code,
            "side": proposal.side,
            "quantity": proposal.quantity,
            "order_type": proposal.order_type,
            "price": proposal.price,
            "rationale": proposal.rationale,
            "confidence": proposal.confidence,
        },
        "snapshot": {
            "current_price": snapshot.current_price,
            "cash": snapshot.cash,
            "total_value": snapshot.total_value,
            "holding_qty": snapshot.holding_qty,
        },
        "limits": limits.as_payload(),
        "usage": {"order_count": usage.order_count, "order_amount": usage.order_amount},
        "hard_check": hard_check.as_payload(),
    }
    # 확신도는 soft라 코드가 거부하지 않는다. 대신 검증자가 볼 수 있도록 신호로 싣는다.
    payload["usage"]["confidence_below_soft_threshold"] = proposal.confidence < limits.min_confidence

    verdict = await verify(payload, proposal_id)
    if not verdict.approved:
        reason = qualitative_reason(verdict.reason)
        logger.info("검증자 거부 (stock=%s, failure=%s)", stock, verdict.failure)
        return _rejected(
            stock,
            (LimitViolation("verifier_rejected", "검증자가 보류를 권고했습니다."),),
            verdict_reason=reason,
            failure=verdict.failure,
            verifier_called=True,
            proposal=proposal,
            snapshot=snapshot,
        )

    # ⑧ 승인 — 기존 대기 주문 경로에 합류한다. 여기서 만드는 것은 대기 주문까지다.
    #
    #    장 운영 시간을 여기서 한 번 더 본다. ②의 판정은 제안 왕복(기본 120초) + MCP
    #    왕복 + 검증(기본 40초) **이전** 시각이라, 15:29에 친 /advise가 15:32에 60초짜리
    #    확정 버튼을 띄울 수 있다. _handle_confirm에는 장 운영 재검사가 없어 사용자는
    #    눌러도 브로커 거절만 본다. created_at과 같은 시각으로 판정해 둘이 갈리지 않게 한다.
    created_at = now_factory()
    if not is_korean_market_open(created_at):
        logger.info("검증 통과 후 장 마감 — 대기 주문을 만들지 않는다 (stock=%s)", stock)
        return _rejected(
            stock,
            (
                LimitViolation(
                    "market_closed",
                    "검증을 마치는 사이 장 운영 시간이 끝났습니다. (평일 09:00~15:30)",
                ),
            ),
            verifier_called=True,
            proposal=proposal,
            snapshot=snapshot,
        )

    order = PendingOrder(
        chat_id=trigger.chat_id,
        stock_name=proposal.stock_name,
        stock_code=proposal.stock_code,
        side=proposal.side,
        quantity=proposal.quantity,
        # MARKET이면 기존 /buy 경로와 같이 price는 표시·기록용이다. 조회 왕복 뒤의
        # 현재가를 그대로 넣어, 만료 시각과 마찬가지로 사용자가 보는 값과 저장된 값이
        # 어긋나지 않게 한다.
        price=proposal.price if proposal.order_type == "LIMIT" else snapshot.current_price,
        created_at=created_at,
        order_type=proposal.order_type,
        callback_token=secrets.token_urlsafe(8),
    )
    try:
        stored = await pending_orders.set_if_absent(trigger.chat_id, order)
    except Exception as exc:  # noqa: BLE001
        logger.error("대기 주문 저장 실패: %s", exc)
        return _rejected(
            stock,
            (LimitViolation("store_error", f"주문 저장소 오류: {short_error(exc)}"),),
            verifier_called=True,
            proposal=proposal,
            snapshot=snapshot,
        )
    if not stored:
        # 제안을 만드는 사이 같은 chat에서 /buy가 먼저 슬롯을 잡았다.
        return OrderAssistResult(status="conflict", message=CONFLICT_MESSAGE)

    return OrderAssistResult(
        status="approved",
        message=format_approval_message(proposal, snapshot, order, verdict),
        order=order,
        verdict_reason=qualitative_reason(verdict.reason),
        verifier_called=True,
        proposal=proposal,
        snapshot=snapshot,
    )
