"""주문 보조(#299) 테스트.

고정하는 것들:

- 하드 한도 각각의 위반/통과 (``evaluate_hard_limits``는 순수 함수라 LLM·MCP 없이 검증한다)
- **한쪽 방향 잠금** — 코드가 거부하면 검증자를 호출조차 하지 않는다
- **fail-closed 4종** — 검증 파싱 실패 / verdict 미지값 / proposal_id 불일치 / 타임아웃
- 충돌 결과, 냉각 시간, 블랙리스트
- 승인 시 기존 PendingOrder 경로 합류(확정 버튼 포함)
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from backend import order_assist
from backend.order_assist import (
    CONFLICT_MESSAGE,
    FAILURE_ID_MISMATCH,
    FAILURE_PARSE,
    FAILURE_TIMEOUT,
    AccountSnapshot,
    DailyUsage,
    OrderLimits,
    OrderProposal,
    ProposalTrigger,
    VerifierVerdict,
    check_orderable_code,
    evaluate_hard_limits,
    format_approval_message,
    load_daily_usage,
    parse_proposal,
    parse_snapshot,
    qualitative_reason,
    request_verification,
    run_order_assist,
)
from backend.redis_state import InMemoryPendingOrderStore, RedisKeys
from backend.telegram_commands import ADVISE_COMMAND_HELP, TelegramCommandHandler
from backend.trading_orders import ORDER_EXPIRES_AFTER

KST = ZoneInfo("Asia/Seoul")

# 평일 장중 — is_korean_market_open이 True인 시각.
MARKET_OPEN_NOW = datetime(2026, 5, 20, 10, 0, tzinfo=KST)


def _proposal(**overrides) -> OrderProposal:
    base = dict(
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=10,
        order_type="LIMIT",
        price=74_500,
        rationale="분기 실적 개선과 수급 회복",
        confidence=0.72,
    )
    base.update(overrides)
    return OrderProposal(**base)


def _snapshot(**overrides) -> AccountSnapshot:
    base = dict(current_price=74_500, cash=5_000_000, total_value=20_000_000, holding_qty=3)
    base.update(overrides)
    return AccountSnapshot(**base)


BALANCE_TEXT = """[계좌 잔고 현황]
- 총 평가금액: 20,000,000원
- 순자산금액: 20,000,000원
- 거래가능금액: 5,000,000원

[보유 종목 리스트]
- 삼성전자 (005930) · 3주
  평단가 67,000원 → 평가금액 210,000원
  손익 +9,000원 · 수익률 +4.48%
"""

QUOTE_TEXT = """[삼성전자] 현재가 시세
- 종목코드: 005930
- 현재가: 74,500원
- 전일 대비: +1,000 (1.36%)
"""

# mcp-trading/balance.js formatTruncationNote()가 잘림 시 덧붙이는 안내다. 사유 부분만
# 바뀌고 "조회가 중단되어"는 고정이라, scheduler.is_balance_truncated가 그 리터럴을 본다.
TRUNCATION_NOTE = """
[안내] 페이지 상한(20회)에 도달하여 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.
"""

PROPOSAL_JSON = (
    '{"stock_name": "삼성전자", "stock_code": "005930", "side": "BUY", "quantity": 10, '
    '"order_type": "LIMIT", "price": 74500, "rationale": "실적 개선", "confidence": 0.72}'
)


# ---------------------------------------------------------------------------
# 하드 한도 — 순수 함수
# ---------------------------------------------------------------------------


def test_hard_limits_pass_for_a_conforming_proposal():
    result = evaluate_hard_limits(_proposal(), _snapshot(), OrderLimits(), DailyUsage())

    assert result.passed is True
    assert result.violations == ()
    assert result.as_payload() == {"passed": True, "violations": []}


def test_order_amount_limit_violation():
    # 74,500 × 20 = 1,490,000원 > 100만원
    result = evaluate_hard_limits(_proposal(quantity=20), _snapshot(), OrderLimits(), DailyUsage())

    assert [v.code for v in result.violations] == ["order_amount"]
    assert "1회 주문 한도 초과" in result.violations[0].message


def test_order_amount_limit_passes_at_the_boundary():
    """한도는 초과일 때만 위반이다 — 정확히 한도면 통과."""
    limits = OrderLimits(max_order_amount=745_000)

    result = evaluate_hard_limits(_proposal(), _snapshot(), limits, DailyUsage())

    assert result.passed is True


def test_position_ratio_violation_counts_the_order_being_placed():
    """비중은 '지금'이 아니라 '이 주문 후'로 본다."""
    # (보유 3 + 주문 10) × 74,500 = 968,500 / 총 4,000,000 = 24.2% > 20%
    result = evaluate_hard_limits(
        _proposal(), _snapshot(total_value=4_000_000), OrderLimits(), DailyUsage()
    )

    assert "position_ratio" in [v.code for v in result.violations]


def test_daily_count_limit_violation():
    result = evaluate_hard_limits(
        _proposal(), _snapshot(), OrderLimits(), DailyUsage(order_count=10, order_amount=0)
    )

    assert "daily_count" in [v.code for v in result.violations]


def test_daily_count_limit_passes_with_one_slot_left():
    result = evaluate_hard_limits(
        _proposal(), _snapshot(), OrderLimits(), DailyUsage(order_count=9, order_amount=0)
    )

    assert result.passed is True


def test_daily_amount_limit_violation():
    # 오늘 290만 + 이번 74.5만 = 364.5만 > 300만
    result = evaluate_hard_limits(
        _proposal(), _snapshot(), OrderLimits(), DailyUsage(order_count=1, order_amount=2_900_000)
    )

    assert "daily_amount" in [v.code for v in result.violations]


def test_cash_floor_violation():
    """주문 후 남는 현금이 총 평가금액의 10% 아래로 떨어지면 위반이다."""
    # 현금 2,500,000 - 745,000 = 1,755,000 < 20,000,000 × 10% = 2,000,000
    result = evaluate_hard_limits(
        _proposal(), _snapshot(cash=2_500_000), OrderLimits(), DailyUsage()
    )

    assert "cash_floor" in [v.code for v in result.violations]


def test_insufficient_cash_violation_takes_precedence_over_floor():
    """현금이 주문금액보다 적으면 '잔액 부족'으로 잡고 하한 계산으로 넘어가지 않는다."""
    result = evaluate_hard_limits(
        _proposal(), _snapshot(cash=100_000), OrderLimits(), DailyUsage()
    )

    codes = [v.code for v in result.violations]
    assert "insufficient_cash" in codes
    assert "cash_floor" not in codes


def test_price_gap_violation():
    # 지정가 78,000 vs 현재가 74,500 → 4.7% > 3%
    result = evaluate_hard_limits(
        _proposal(price=78_000, quantity=1), _snapshot(), OrderLimits(), DailyUsage()
    )

    assert "price_gap" in [v.code for v in result.violations]


def test_price_gap_is_not_checked_for_market_orders():
    """시장가에는 지정가가 없다 — 괴리 판정 대상이 아니다."""
    result = evaluate_hard_limits(
        _proposal(order_type="MARKET", price=0, quantity=1),
        _snapshot(),
        OrderLimits(),
        DailyUsage(),
    )

    assert [v.code for v in result.violations] == []


def test_market_order_amount_uses_current_price():
    """시장가 주문금액은 현재가로 환산한다 — price=0을 금액 0으로 읽지 않는다."""
    result = evaluate_hard_limits(
        _proposal(order_type="MARKET", price=0, quantity=20),
        _snapshot(),
        OrderLimits(),
        DailyUsage(),
    )

    assert "order_amount" in [v.code for v in result.violations]


def test_sell_beyond_holdings_violation():
    result = evaluate_hard_limits(
        _proposal(side="SELL", quantity=5), _snapshot(holding_qty=3), OrderLimits(), DailyUsage()
    )

    assert "oversell" in [v.code for v in result.violations]


def test_sell_within_holdings_passes():
    result = evaluate_hard_limits(
        _proposal(side="SELL", quantity=3), _snapshot(holding_qty=3), OrderLimits(), DailyUsage()
    )

    assert result.passed is True


def test_low_confidence_is_soft_and_does_not_reject():
    """확신도는 soft 한도다 — 코드가 거부하지 않고 검증자 참고 신호로만 넘어간다."""
    result = evaluate_hard_limits(
        _proposal(confidence=0.1), _snapshot(), OrderLimits(), DailyUsage()
    )

    assert result.passed is True


def test_all_violations_are_reported_not_just_the_first():
    """사용자가 한 번에 전부 알아야 같은 제안으로 여러 번 왕복하지 않는다."""
    result = evaluate_hard_limits(
        _proposal(quantity=100, price=90_000),
        _snapshot(cash=200_000, total_value=1_000_000),
        OrderLimits(),
        DailyUsage(order_count=9, order_amount=2_900_000),
    )

    codes = {v.code for v in result.violations}
    assert {"order_amount", "price_gap", "daily_amount", "position_ratio"} <= codes


# ---------------------------------------------------------------------------
# 블랙리스트 / 주문 가능 코드 — 같은 자리에서 판정한다
# ---------------------------------------------------------------------------


def test_blacklisted_code_is_rejected():
    violation = check_orderable_code("005930", frozenset({"005930"}))

    assert violation is not None
    assert violation.code == "blacklisted"


def test_blacklist_is_case_insensitive_for_alphanumeric_codes():
    # 영숫자 코드는 어차피 주문 불가라 형태 검사에서 먼저 걸린다 — 그 순서를 고정한다.
    violation = check_orderable_code("0001a0", frozenset({"0001A0"}))

    assert violation is not None
    assert violation.code == "unorderable_code"


def test_non_orderable_code_shape_is_rejected():
    violation = check_orderable_code("F70100026", frozenset())

    assert violation is not None
    assert violation.code == "unorderable_code"


def test_plain_numeric_code_passes_when_not_blacklisted():
    assert check_orderable_code("005930", frozenset({"035420"})) is None


# ---------------------------------------------------------------------------
# 제안 / 스냅샷 파싱
# ---------------------------------------------------------------------------


def test_parse_proposal_reads_a_well_formed_payload():
    proposal, error = parse_proposal(PROPOSAL_JSON)

    assert error == ""
    assert proposal is not None
    assert (proposal.side, proposal.quantity, proposal.price) == ("BUY", 10, 74_500)
    assert proposal.confidence == pytest.approx(0.72)


def test_parse_proposal_accepts_stringy_numbers():
    raw = (
        '{"stock_name": "삼성전자", "stock_code": "005930", "side": "buy", "quantity": "10주", '
        '"order_type": "limit", "price": "74,500원", "rationale": "x", "confidence": "0.7"}'
    )

    proposal, error = parse_proposal(raw)

    assert error == ""
    assert proposal is not None
    assert (proposal.quantity, proposal.price) == (10, 74_500)


def test_parse_proposal_treats_hold_as_no_proposal_not_a_parse_failure():
    raw = '{"side": "HOLD", "rationale": "지금은 관망"}'

    proposal, error = parse_proposal(raw)

    assert proposal is None
    assert "제안하지 않았습니다" in error


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "비어 있어"),
        ("설명만 있고 JSON이 없습니다.", "해석하지 못해"),
        ('{"side": "LONG", "quantity": 1, "price": 1, "confidence": 0.7}', "매매 방향"),
        ('{"side": "BUY", "quantity": 0, "price": 1, "confidence": 0.7}', "주문 수량"),
        ('{"side": "BUY", "quantity": 1, "order_type": "LIMIT", "price": 0, "confidence": 0.7}', "지정가"),
        ('{"side": "BUY", "quantity": 1, "price": 1}', "확신도"),
    ],
    ids=["empty", "no_json", "unknown_side", "bad_qty", "bad_price", "missing_confidence"],
)
def test_parse_proposal_fails_closed(raw: str, expected: str):
    proposal, error = parse_proposal(raw)

    assert proposal is None
    assert expected in error


def test_parse_snapshot_reads_all_four_values():
    snapshot, error = parse_snapshot(QUOTE_TEXT, BALANCE_TEXT, "005930")

    assert error == ""
    assert snapshot == AccountSnapshot(
        current_price=74_500, cash=5_000_000, total_value=20_000_000, holding_qty=3
    )


def test_parse_snapshot_rejects_a_truncated_balance():
    """잘린 잔고는 마커도 있고 파싱도 되지만, 못 본 보유분을 0주로 읽게 만든다.

    그대로 통과시키면 BUY 비중 한도와 SELL 보유량 판정이 동시에 헐거워진다 —
    "빠진 값을 0으로 채우면 한도가 조용히 무력해진다"는 이 모듈의 원칙이 정확히
    이 지점에서 깨진다. 잘림 문구는 mcp-trading/balance.js formatTruncationNote()의
    사유 무관 고정 부분이며 scheduler.is_balance_truncated가 같은 리터럴을 본다.
    """
    truncated = BALANCE_TEXT + TRUNCATION_NOTE

    snapshot, error = parse_snapshot(QUOTE_TEXT, truncated, "005930")

    assert snapshot is None
    assert "끊겨" in error


@pytest.mark.asyncio
async def test_truncated_balance_stops_the_flow_before_the_verifier():
    """잘린 잔고는 한도 판정 이전 단계라 검증자까지 가지 않는다."""
    truncated = BALANCE_TEXT + TRUNCATION_NOTE

    result, verify_calls = await _run(mcp=_mcp_runner(balance=truncated))

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["snapshot_parse"]
    assert verify_calls == []


def test_parse_snapshot_reports_zero_holdings_for_an_unheld_stock():
    """보유 섹션은 읽혔는데 그 종목이 없으면 관측된 0주다."""
    snapshot, error = parse_snapshot(QUOTE_TEXT, BALANCE_TEXT, "035420")

    assert error == ""
    assert snapshot is not None
    assert snapshot.holding_qty == 0


@pytest.mark.parametrize(
    "quote,balance,expected",
    [
        ("현재가 없음", BALANCE_TEXT, "현재가"),
        (QUOTE_TEXT, "잔고를 읽지 못했습니다", "계좌 잔고"),
        (QUOTE_TEXT, "[보유 종목 리스트]\n- 총 평가금액: 100원", "예수금"),
        (QUOTE_TEXT, "[보유 종목 리스트]\n- 거래가능금액: 100원", "총 평가금액"),
    ],
    ids=["no_price", "no_marker", "no_cash", "no_total"],
)
def test_parse_snapshot_fails_closed_on_missing_values(quote, balance, expected):
    """빠진 값을 0으로 채우면 한도가 조용히 무력해진다 — 실패로 끊는다."""
    snapshot, error = parse_snapshot(quote, balance, "005930")

    assert snapshot is None
    assert expected in error


# ---------------------------------------------------------------------------
# 사용량 집계
# ---------------------------------------------------------------------------


def test_load_daily_usage_counts_only_todays_kst_trades():
    class _Row(SimpleNamespace):
        pass

    now = datetime(2026, 5, 20, 10, 0, tzinfo=KST)
    captured = {}

    class _Query:
        def filter(self, criterion):
            captured["filtered"] = True
            return self

        def all(self):
            return [_Row(quantity=2, price=1000.0), _Row(quantity=3, price=2000.0)]

    class _Session:
        def query(self, model):
            return _Query()

        def close(self):
            captured["closed"] = True

    usage = load_daily_usage(lambda: _Session(), now)

    assert usage == DailyUsage(order_count=2, order_amount=8000)
    assert captured == {"filtered": True, "closed": True}


def test_kst_day_start_is_converted_to_utc():
    """KST 00:00은 UTC 전날 15:00이다 — 이 축을 틀리면 집계가 9시간 어긋난다."""
    start = order_assist._kst_day_start_utc(datetime(2026, 5, 20, 1, 0, tzinfo=KST))

    assert start == datetime(2026, 5, 19, 15, 0)
    assert start.tzinfo is None


# ---------------------------------------------------------------------------
# 검증 응답 — fail-closed 4종
# ---------------------------------------------------------------------------


def _verify_transport(monkeypatch, *, body=None, error=None):
    async def fake_post(path, payload, timeout):
        assert path == "/v1/verify-order"
        if error is not None:
            raise error
        return body

    monkeypatch.setattr(order_assist, "_post_json", fake_post)


@pytest.mark.asyncio
async def test_verification_approves_on_matching_verdict(monkeypatch):
    _verify_transport(
        monkeypatch,
        body={"proposal_id": "adv-1", "verdict": "APPROVE", "reason": "이상 없음", "failure": None},
    )

    verdict = await request_verification({}, "adv-1")

    assert verdict == VerifierVerdict(True, "이상 없음", None)


@pytest.mark.asyncio
async def test_verification_fails_closed_on_unparsable_body(monkeypatch):
    _verify_transport(monkeypatch, body="not a dict")

    verdict = await request_verification({}, "adv-1")

    assert verdict.approved is False
    assert verdict.failure == FAILURE_PARSE


@pytest.mark.asyncio
async def test_verification_fails_closed_on_unknown_verdict_value(monkeypatch):
    _verify_transport(monkeypatch, body={"proposal_id": "adv-1", "verdict": "MAYBE"})

    verdict = await request_verification({}, "adv-1")

    assert verdict.approved is False
    assert verdict.failure == FAILURE_PARSE


@pytest.mark.asyncio
async def test_verification_fails_closed_on_proposal_id_mismatch(monkeypatch):
    """id가 어긋나면 APPROVE라도 승인이 아니다 — 다른 제안의 응답일 수 있다."""
    _verify_transport(monkeypatch, body={"proposal_id": "adv-other", "verdict": "APPROVE"})

    verdict = await request_verification({}, "adv-1")

    assert verdict.approved is False
    assert verdict.failure == FAILURE_ID_MISMATCH


@pytest.mark.asyncio
async def test_verification_fails_closed_on_timeout(monkeypatch):
    _verify_transport(monkeypatch, error=httpx.ReadTimeout("timed out"))

    verdict = await request_verification({}, "adv-1")

    assert verdict.approved is False
    assert verdict.failure == FAILURE_TIMEOUT


@pytest.mark.asyncio
async def test_verification_fails_closed_on_http_error(monkeypatch):
    _verify_transport(monkeypatch, error=RuntimeError("NAT /v1/verify-order 응답 502"))

    verdict = await request_verification({}, "adv-1")

    assert verdict.approved is False
    assert verdict.failure == order_assist.FAILURE_HTTP


# ---------------------------------------------------------------------------
# 사용자 메시지 — 검증자 수치가 새어 나오지 않는다
# ---------------------------------------------------------------------------


def test_qualitative_reason_masks_numbers_but_keeps_the_sentence():
    """수치는 가리고 사유는 남긴다 — 문장을 통째로 버리면 사유가 사라진다."""
    reason = "근거가 충분합니다. 현재가는 99,999원으로 보입니다. 리스크는 제한적입니다."

    result = qualitative_reason(reason)

    assert result == "근거가 충분합니다. 현재가는 [수치]원으로 보입니다. 리스크는 제한적입니다."
    assert not any(ch.isdigit() for ch in result)


def test_qualitative_reason_keeps_a_single_sentence_korean_reason():
    """한국어 단문 응답이 통째로 사라지던 회귀 (#299 2차 리뷰).

    _SENTENCE_SPLIT_RE는 구두점+공백이나 개행을 요구하므로 이런 한 문장 응답은
    분할되지 않는다. 문장 단위로 버리면 이 응답 전체가 사라지고, 검증자 거부 경로의
    위반 목록은 "검증자가 보류를 권고했습니다." 한 줄뿐이라 사용자는 사유를 하나도
    못 본다.
    """
    reason = "현재가 74,500원 수준에서 단기 과열로 판단되어 보류를 권고합니다."

    result = qualitative_reason(reason)

    assert result == "현재가 [수치]원 수준에서 단기 과열로 판단되어 보류를 권고합니다."
    assert "단기 과열" in result
    assert not any(ch.isdigit() for ch in result)


def test_qualitative_reason_masks_a_whole_number_run_not_its_digits():
    """자릿수 쉼표가 든 수치가 여러 마스크로 쪼개지지 않는다."""
    assert qualitative_reason("평가금액 12,345,678원") == "평가금액 [수치]원"


def test_qualitative_reason_falls_back_when_nothing_qualitative_remains():
    result = qualitative_reason("74,500 / 3.2 / 10")

    assert result == order_assist._GENERIC_VERDICT_REASON
    assert not any(ch.isdigit() for ch in result)


def test_qualitative_reason_handles_empty_input():
    assert qualitative_reason("") == order_assist._GENERIC_VERDICT_REASON


def test_approval_message_numbers_come_from_the_payload_not_the_verifier():
    from backend.trading_orders import PendingOrder

    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=10,
        price=74_500,
        created_at=MARKET_OPEN_NOW,
        order_type="LIMIT",
        callback_token="tok",
    )
    verdict = VerifierVerdict(True, "현재가 88,888원 기준 적정합니다. 근거가 명확합니다.")

    message = format_approval_message(_proposal(), _snapshot(), order, verdict)

    assert "745,000원" in message  # 74,500 × 10 — 코드가 계산한 값
    assert "74,500원" in message
    assert "88,888" not in message  # 검증자가 지어낸 수치는 실리지 않는다
    assert "근거가 명확합니다." in message
    expires = (MARKET_OPEN_NOW + ORDER_EXPIRES_AFTER).strftime("%H:%M:%S")
    assert expires in message


# ---------------------------------------------------------------------------
# run_order_assist — 전체 흐름
# ---------------------------------------------------------------------------


class FakeCooldown:
    def __init__(self, *, active=False, checked=True):
        self._active = active
        self._checked = checked
        self.marked: list[tuple[str, str | None]] = []
        # 어떤 키로 조회했는지도 남긴다 — 냉각이 별칭으로 뚫리지 않으려면 조회 키와
        # 기록 키가 둘 다 종목코드여야 한다.
        self.checked_keys: list[str] = []

    async def active(self, stock_code, rule_id):
        self.checked_keys.append(stock_code)
        return self._active, self._checked

    async def mark(self, stock_code, rule_id):
        self.marked.append((stock_code, rule_id))


def _mcp_runner(*, quote=QUOTE_TEXT, balance=BALANCE_TEXT, resolved="삼성전자 (005930, KOSPI)"):
    calls = []

    async def runner(server_params, tool_name, arguments):
        calls.append(tool_name)
        if tool_name == "resolve_stock_code":
            return resolved
        if tool_name == "get_stock_quote":
            return quote
        if tool_name == "get_balance":
            return balance
        raise AssertionError(f"unexpected tool: {tool_name}")

    runner.calls = calls
    return runner


class _EmptySession:
    def query(self, model):
        class _Q:
            def filter(self, criterion):
                return self

            def all(self):
                return []

        return _Q()

    def close(self):
        return None


async def _run(
    *,
    proposal_text=PROPOSAL_JSON,
    verdict=None,
    store=None,
    cooldown=None,
    mcp=None,
    limits=None,
    now=MARKET_OPEN_NOW,
    verify_calls=None,
    propose_calls=None,
    stock="삼성전자",
    now_factory=None,
    session_factory=None,
):
    verify_calls = verify_calls if verify_calls is not None else []
    propose_calls = propose_calls if propose_calls is not None else []

    async def propose(prompt):
        propose_calls.append(prompt)
        return proposal_text

    async def verify(payload, proposal_id):
        verify_calls.append((payload, proposal_id))
        return verdict if verdict is not None else VerifierVerdict(True, "근거가 명확합니다.")

    result = await run_order_assist(
        ProposalTrigger(source="telegram", stock=stock, chat_id="123"),
        pending_orders=store if store is not None else InMemoryPendingOrderStore(),
        mcp_runner=mcp if mcp is not None else _mcp_runner(),
        now_factory=now_factory or (lambda: now),
        limits=limits or OrderLimits(),
        cooldown=cooldown or FakeCooldown(),
        session_factory=session_factory or (lambda: _EmptySession()),
        propose=propose,
        verify=verify,
    )
    return result, verify_calls


@pytest.mark.asyncio
async def test_approved_flow_stores_a_pending_order_for_the_existing_confirm_path():
    store = InMemoryPendingOrderStore()

    result, verify_calls = await _run(store=store)

    assert result.status == "approved"
    assert result.order is not None
    assert len(verify_calls) == 1
    # 기존 경로에 그대로 합류한다 — 저장된 대기 주문이 /confirm이 소비하는 그 객체다.
    stored = await store.get("123")
    assert stored == result.order
    assert stored.callback_token  # 확정/취소 버튼이 붙을 토큰
    assert stored.created_at == MARKET_OPEN_NOW
    assert (stored.side, stored.quantity, stored.price) == ("BUY", 10, 74_500)


@pytest.mark.asyncio
async def test_conflict_when_a_pending_order_already_exists():
    """충돌은 조용히 사라지면 안 된다 — 상태와 문장을 모두 돌려준다."""
    from backend.trading_orders import PendingOrder

    store = InMemoryPendingOrderStore()
    await store.set_if_absent(
        "123",
        PendingOrder(
            chat_id="123",
            stock_name="NAVER",
            stock_code="035420",
            side="BUY",
            quantity=1,
            price=200_000,
            created_at=MARKET_OPEN_NOW,
        ),
    )
    mcp = _mcp_runner()

    result, verify_calls = await _run(store=store, mcp=mcp)

    assert result.status == "conflict"
    assert result.message == CONFLICT_MESSAGE
    # 충돌이면 제안도 검증도 하지 않는다 — 슬롯이 없는데 왕복만 버릴 이유가 없다.
    assert mcp.calls == []
    assert verify_calls == []


@pytest.mark.asyncio
async def test_expired_pending_order_does_not_block_a_new_proposal():
    """만료된 대기 주문은 충돌이 아니다 — 안내대로 /confirm해도 되지 않는 막다른 길이 된다.

    앱 만료(60초)와 redis TTL(600초)이 10배 차이라, 확정·취소하지 않은 주문의 키는
    약 9분간 남는다. /buy는 같은 자리에서 만료분을 치우므로 통과하는데 /advise만
    막히던 자리를 고정한다.
    """
    from backend.trading_orders import PendingOrder

    store = InMemoryPendingOrderStore()
    await store.set_if_absent(
        "123",
        PendingOrder(
            chat_id="123",
            stock_name="NAVER",
            stock_code="035420",
            side="BUY",
            quantity=1,
            price=200_000,
            # 61초 전 생성 — ORDER_EXPIRES_AFTER(60초)를 막 넘겼다.
            created_at=MARKET_OPEN_NOW - ORDER_EXPIRES_AFTER - timedelta(seconds=1),
        ),
    )

    result, verify_calls = await _run(store=store)

    assert result.status == "approved"
    assert len(verify_calls) == 1
    # 만료분이 치워지고 새 제안이 그 자리를 차지한다.
    stored = await store.get("123")
    assert stored is not None and stored.stock_code == "005930"


@pytest.mark.asyncio
async def test_expiry_check_reads_a_naive_now_as_kst():
    """tz 없는 now도 KST로 읽는다 — 같은 함수 안의 다른 시각 해석과 갈라지지 않게.

    now_factory 기본값이 datetime.now(KST)라 프로덕션에서는 naive가 들어오지 않는
    잠복 경로다. 다만 보정을 빼면 이 헬퍼만 naive를 시스템 로컬로 읽고,
    is_korean_market_open은 같은 값을 KST로 읽어 한 함수 안에 해석이 두 갈래가 된다.

    아래 30초는 KST로 읽으면 만료 전(유지), UTC로 읽으면 9시간 경과(삭제)라 판정이
    갈린다. 따라서 이 테스트는 로컬 tz가 KST가 아닌 호스트(UTC로 도는 CI 등)에서
    보정 누락을 잡아낸다. KST 호스트에서는 두 해석이 같아져 통과만 한다.
    """
    from backend.trading_orders import PendingOrder

    store = InMemoryPendingOrderStore()
    created_at = datetime(2026, 5, 20, 10, 0, tzinfo=KST)
    await store.set_if_absent(
        "123",
        PendingOrder(
            chat_id="123",
            stock_name="NAVER",
            stock_code="035420",
            side="BUY",
            quantity=1,
            price=200_000,
            created_at=created_at,
        ),
    )
    naive_now = datetime(2026, 5, 20, 10, 0, 30)  # tzinfo 없음

    await order_assist._drop_expired_pending_order(store, "123", naive_now)

    assert await store.get("123") is not None


@pytest.mark.asyncio
async def test_unexpired_pending_order_still_conflicts():
    """만료 정리가 '충돌 검사를 없앤 것'이 되면 안 된다 — 살아 있는 주문은 그대로 막는다."""
    from backend.trading_orders import PendingOrder

    store = InMemoryPendingOrderStore()
    await store.set_if_absent(
        "123",
        PendingOrder(
            chat_id="123",
            stock_name="NAVER",
            stock_code="035420",
            side="BUY",
            quantity=1,
            price=200_000,
            created_at=MARKET_OPEN_NOW - timedelta(seconds=30),
        ),
    )

    result, _ = await _run(store=store)

    assert result.status == "conflict"


@pytest.mark.asyncio
async def test_hard_limit_violation_never_calls_the_verifier():
    """한쪽 방향 잠금: 코드가 거부하면 LLM에게 의견을 물어보지도 않는다."""
    store = InMemoryPendingOrderStore()

    result, verify_calls = await _run(
        limits=OrderLimits(max_order_amount=100_000), store=store
    )

    assert result.status == "rejected"
    assert verify_calls == []
    assert result.verifier_called is False
    assert [v.code for v in result.violations] == ["order_amount"]
    assert await store.get("123") is None


@pytest.mark.asyncio
async def test_verifier_rejection_blocks_the_order():
    store = InMemoryPendingOrderStore()

    result, verify_calls = await _run(
        verdict=VerifierVerdict(False, "근거가 빈약합니다.", None), store=store
    )

    assert result.status == "rejected"
    assert result.verifier_called is True
    assert len(verify_calls) == 1
    assert await store.get("123") is None
    assert "검증 의견: 근거가 빈약합니다." in result.message


@pytest.mark.asyncio
async def test_verifier_failure_reason_numbers_do_not_reach_the_user():
    result, _ = await _run(
        verdict=VerifierVerdict(False, "현재가 12,345원이라 부적절합니다. 근거가 약합니다.")
    )

    assert "12,345" not in result.message
    assert "근거가 약합니다." in result.message


@pytest.mark.asyncio
async def test_cooldown_blocks_before_any_proposal_call():
    """냉각이 아끼는 자원은 제안 왕복이다 — 그 호출까지 가지 않는다.

    종목코드 확인(resolve_stock_code)은 냉각 검사보다 앞이라 한 번 나간다. 냉각 키가
    종목코드여야 이름↔코드 별칭으로 뚫리지 않기 때문이고, 시세·잔고 조회는 아직이다.
    """
    cooldown = FakeCooldown(active=True)
    mcp = _mcp_runner()
    propose_calls = []

    result, verify_calls = await _run(
        cooldown=cooldown, mcp=mcp, propose_calls=propose_calls
    )

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["cooldown"]
    assert propose_calls == []
    assert mcp.calls == ["resolve_stock_code"]
    assert verify_calls == []


@pytest.mark.asyncio
async def test_cooldown_lookup_failure_is_treated_as_cooling():
    """redis가 죽었을 때 한도가 조용히 풀리면 안 된다."""
    result, _ = await _run(cooldown=FakeCooldown(active=True, checked=False))

    assert result.status == "rejected"
    assert "확인하지 못해" in result.violations[0].message


@pytest.mark.asyncio
async def test_cooldown_is_marked_after_a_completed_decision():
    cooldown = FakeCooldown()

    result, _ = await _run(cooldown=cooldown)

    assert result.status == "approved"
    # 사용자가 친 "삼성전자"가 아니라 해석된 종목코드로 건다.
    assert cooldown.marked == [("005930", None)]


@pytest.mark.asyncio
async def test_cooldown_is_keyed_on_the_resolved_code_not_the_typed_text():
    """이름으로 걸린 냉각이 코드 입력으로 뚫리면 안 된다.

    resolve_stock_code는 "삼성전자"와 "005930"을 같은 코드로 해석하므로 두 입력은
    같은 종목이다. 냉각을 사용자가 친 문자열로 걸면 키가 갈라져 "/advise 삼성전자"
    직후의 "/advise 005930"이 그대로 통과하고, "같은 종목에 대한 반복 제안을 막는다"는
    냉각의 목적이 무너진다. 두 입력이 같은 키를 보는 것을 고정한다.
    """
    by_name = FakeCooldown()
    by_code = FakeCooldown()

    await _run(cooldown=by_name, stock="삼성전자")
    await _run(
        cooldown=by_code,
        stock="005930",
        mcp=_mcp_runner(resolved="삼성전자 (005930, KOSPI)"),
    )

    assert by_name.marked == by_code.marked == [("005930", None)]
    # 조회도 같은 키로 나가야 별칭 입력이 냉각 검사에 걸린다.
    assert by_name.checked_keys == by_code.checked_keys == ["005930"]


@pytest.mark.asyncio
async def test_blacklisted_stock_is_rejected_before_the_verifier():
    result, verify_calls = await _run(limits=OrderLimits(blacklist=frozenset({"005930"})))

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["blacklisted"]
    assert verify_calls == []


@pytest.mark.asyncio
async def test_unresolved_stock_echo_is_rejected():
    mcp = _mcp_runner(resolved="999999 (999999, UNKNOWN)")

    result, verify_calls = await _run(mcp=mcp)

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["unresolved_stock"]
    assert verify_calls == []


@pytest.mark.asyncio
async def test_proposal_for_a_different_stock_is_rejected():
    """제안이 다른 종목을 말하면 코드로 고쳐서 진행하지 않는다 — 근거와 주문이 어긋난다."""
    other = PROPOSAL_JSON.replace('"stock_code": "005930"', '"stock_code": "035420"')

    result, verify_calls = await _run(proposal_text=other)

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["proposal_stock_mismatch"]
    assert verify_calls == []


@pytest.mark.asyncio
async def test_snapshot_parse_failure_is_rejected():
    result, verify_calls = await _run(mcp=_mcp_runner(quote="시세 조회 실패"))

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["snapshot_parse"]
    assert verify_calls == []


@pytest.mark.asyncio
async def test_market_closed_is_rejected_before_anything_else():
    mcp = _mcp_runner()

    result, verify_calls = await _run(
        now=datetime(2026, 5, 20, 20, 0, tzinfo=KST), mcp=mcp
    )

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["market_closed"]
    assert mcp.calls == []
    assert verify_calls == []


@pytest.mark.asyncio
async def test_verify_payload_shape_matches_the_verifier_contract():
    """검증 요청의 필드 이름을 고정한다.

    반대편(finus_nat/src/nat_finus_nat/verifier.py)의 요청 모델은 전부
    ``extra="allow"``다 — backend가 필드명을 바꾸거나 오타를 내도 422가 아니라
    **조용히 무시**되고, 검증자는 그 필드의 기본값(0·""·False)으로 판정한다.
    한도 수치가 0으로 읽히는 쪽이 승인 방향이라 이 드리프트는 위험한 방향으로 샌다.

    ``extra="forbid"``로 잡을 수도 있지만 그러면 backend 배포가 앞서는 순간 주문 보조
    경로가 통째로 422로 죽는다(모르는 필드는 무시하고 아는 필드로 판정하는 편이 낫다는
    것이 그 모델들의 설계 의도다). 드리프트를 만드는 쪽은 backend이므로 여기서 잡는다.
    """
    _, verify_calls = await _run()

    payload = verify_calls[0][0]
    assert set(payload) == {
        "proposal_id",
        "proposal",
        "snapshot",
        "limits",
        "usage",
        "hard_check",
    }
    # ProposalPayload
    assert set(payload["proposal"]) == {
        "stock_name",
        "stock_code",
        "side",
        "quantity",
        "order_type",
        "price",
        "rationale",
        "confidence",
    }
    # SnapshotPayload
    assert set(payload["snapshot"]) == {"current_price", "cash", "total_value", "holding_qty"}
    # HardCheckPayload
    assert set(payload["hard_check"]) == {"passed", "violations"}
    # limits/usage는 검증자 쪽이 dict로 받는다(판정에 쓰지 않는 참고 신호라 느슨하다).
    # 그래도 soft 신호가 이름째 사라지면 검증자가 볼 수 없으므로 여기서 고정한다.
    assert "min_confidence_soft" in payload["limits"]
    assert set(payload["usage"]) == {
        "order_count",
        "order_amount",
        "confidence_below_soft_threshold",
    }


@pytest.mark.asyncio
async def test_soft_confidence_signal_is_passed_to_the_verifier():
    """확신도는 코드가 거부하지 않지만 검증자에게는 신호로 전달된다."""
    low = PROPOSAL_JSON.replace('"confidence": 0.72', '"confidence": 0.2')

    result, verify_calls = await _run(proposal_text=low)

    assert result.status == "approved"
    payload = verify_calls[0][0]
    assert payload["usage"]["confidence_below_soft_threshold"] is True
    assert payload["hard_check"] == {"passed": True, "violations": []}
    assert payload["snapshot"]["current_price"] == 74_500


# ---------------------------------------------------------------------------
# 냉각 키 / redis
# ---------------------------------------------------------------------------


def test_cooldown_key_separates_manual_and_rule_triggers():
    keys = RedisKeys()

    assert keys.order_proposal_cooldown("005930", None).endswith(":005930:manual")
    assert keys.order_proposal_cooldown("005930", "rule-7").endswith(":005930:rule-7")


@pytest.mark.asyncio
async def test_proposal_cooldown_marks_with_ttl():
    class _FakeRedis:
        def __init__(self):
            self.sets = []
            self.closed = False

        async def set(self, key, value, ex=None):
            self.sets.append((key, value, ex))

        async def exists(self, key):
            return 0

        async def aclose(self):
            self.closed = True

    client = _FakeRedis()
    cooldown = order_assist.ProposalCooldown(lambda: client, ttl_seconds=3600)

    assert await cooldown.active("005930", None) == (False, True)
    await cooldown.mark("005930", None)

    assert client.sets == [(RedisKeys().order_proposal_cooldown("005930", None), "proposed", 3600)]
    assert client.closed is True


@pytest.mark.asyncio
async def test_proposal_cooldown_reports_lookup_failure():
    class _BrokenRedis:
        async def exists(self, key):
            raise RuntimeError("connection refused")

        async def aclose(self):
            return None

    cooldown = order_assist.ProposalCooldown(lambda: _BrokenRedis())

    assert await cooldown.active("005930", None) == (True, False)


# ---------------------------------------------------------------------------
# 텔레그램 진입점 — 파싱 + 호출 + 전달만
# ---------------------------------------------------------------------------


class _Notifier:
    def __init__(self, chat_id="123"):
        self.chat_id = chat_id
        self.messages = []
        self.reply_markups = []
        self.actions = []

    async def send_text(self, text, *, reply_markup=None):
        self.messages.append(text)
        self.reply_markups.append(reply_markup)
        return True

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)
        return True

    async def answer_callback_query(self, callback_query_id, text=None):
        return True


def _advise_handler(monkeypatch, result, *, captured=None):
    notifier = _Notifier()
    handler = TelegramCommandHandler(notifier=notifier, now_factory=lambda: MARKET_OPEN_NOW)

    async def fake_run(trigger, **kwargs):
        if captured is not None:
            captured.append((trigger, kwargs))
        return result

    monkeypatch.setattr("backend.telegram_commands.run_order_assist", fake_run)

    async def no_progress(text):
        return None

    async def clear(message_id):
        return None

    monkeypatch.setattr(handler, "_send_progress_message", no_progress)
    monkeypatch.setattr(handler, "_clear_progress_message", clear)
    return handler, notifier


@pytest.mark.asyncio
async def test_advise_without_argument_shows_usage(monkeypatch):
    handler, notifier = _advise_handler(monkeypatch, None)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/advise"}})

    assert notifier.messages == [ADVISE_COMMAND_HELP]


@pytest.mark.asyncio
async def test_advise_passes_the_stock_name_and_chat_id(monkeypatch):
    from backend.order_assist import OrderAssistResult

    captured = []
    handler, _ = _advise_handler(
        monkeypatch,
        OrderAssistResult(status="rejected", message="보류"),
        captured=captured,
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/advise 삼성전자"}})

    trigger, kwargs = captured[0]
    assert trigger == ProposalTrigger(source="telegram", stock="삼성전자", chat_id="123")
    # 저장소·MCP·시계는 핸들러가 쓰던 것을 그대로 넘긴다(테스트 더블 주입 경로 유지).
    assert kwargs["pending_orders"] is handler.pending_orders
    assert kwargs["mcp_runner"] is handler.mcp_runner


@pytest.mark.asyncio
async def test_advise_approved_result_gets_the_existing_confirm_buttons(monkeypatch):
    from backend.order_assist import OrderAssistResult
    from backend.trading_orders import PendingOrder

    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=10,
        price=74_500,
        created_at=MARKET_OPEN_NOW,
        callback_token="tok123",
    )
    handler, notifier = _advise_handler(
        monkeypatch,
        OrderAssistResult(status="approved", message="제안 본문", order=order),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/advise 삼성전자"}})

    assert notifier.messages[-1] == "제안 본문"
    assert notifier.reply_markups[-1] == {
        "inline_keyboard": [
            [
                {"text": "✅ 확정", "callback_data": "order:confirm:tok123"},
                {"text": "❌ 취소", "callback_data": "order:cancel:tok123"},
            ]
        ]
    }


@pytest.mark.asyncio
async def test_advise_conflict_message_reaches_the_user(monkeypatch):
    from backend.order_assist import OrderAssistResult

    handler, notifier = _advise_handler(
        monkeypatch, OrderAssistResult(status="conflict", message=CONFLICT_MESSAGE)
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/advise 삼성전자"}})

    assert notifier.messages[-1] == CONFLICT_MESSAGE
    assert notifier.reply_markups[-1] is None


@pytest.mark.asyncio
async def test_advise_failure_is_reported_not_swallowed(monkeypatch):
    handler, notifier = _advise_handler(monkeypatch, None)

    async def boom(trigger, **kwargs):
        raise RuntimeError("NAT 연결 실패")

    monkeypatch.setattr("backend.telegram_commands.run_order_assist", boom)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/advise 삼성전자"}})

    assert "주문 보조 실패" in notifier.messages[-1]


# ---------------------------------------------------------------------------
# #299 2차 리뷰 — Critical 2건과 무커버 가드
# ---------------------------------------------------------------------------


def _usage_session(rows):
    class _Query:
        def filter(self, criterion):
            return self

        def all(self):
            return rows

    class _Session:
        def query(self, model):
            return _Query()

        def close(self):
            return None

    return lambda: _Session()


def test_daily_usage_refuses_to_total_unpriced_trades():
    """단가 0인 행은 "0원짜리 거래"가 아니라 "금액을 모르는 거래"다.

    /buy에서 지정가를 생략하면 price=0, MARKET으로 파싱되고 그 0이 TradeHistory까지
    내려간다. 0으로 더하면 일 거래대금 한도가 시장가 이력에 대해 있는 척만 하는
    한도가 된다 — 집계를 포기하고 상위 경로가 fail-closed로 거부하게 한다.
    """
    rows = [SimpleNamespace(quantity=2, price=1000.0), SimpleNamespace(quantity=10, price=0.0)]

    with pytest.raises(ValueError, match="집계할 수 없습니다"):
        load_daily_usage(_usage_session(rows), MARKET_OPEN_NOW)


def test_daily_usage_totals_normally_when_every_trade_has_a_price():
    usage = load_daily_usage(
        _usage_session([SimpleNamespace(quantity=2, price=1000.0)]), MARKET_OPEN_NOW
    )

    assert usage == DailyUsage(order_count=1, order_amount=2000)


@pytest.mark.asyncio
async def test_unpriced_trade_history_blocks_the_flow_before_the_verifier():
    """집계 실패는 거부다 — 한도를 모른 채 검증자에게 넘기지 않는다."""
    rows = [SimpleNamespace(quantity=10, price=0.0)]

    result, verify_calls = await _run(session_factory=_usage_session(rows))

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["usage_failed"]
    assert verify_calls == []


@pytest.mark.asyncio
async def test_proposal_without_a_stock_code_is_rejected():
    """종목코드를 못 읽은 제안은 대조 없이 통과하면 안 된다 (#299 2차 리뷰).

    parse_proposal은 수량·지정가·확신도를 "못 읽으면 거부"로 처리한다. 종목코드만
    예외를 두면 어느 종목 근거인지 모르는 문장이 확정 버튼과 함께 나간다.
    """
    without_code = PROPOSAL_JSON.replace('"stock_code": "005930", ', "")

    result, verify_calls = await _run(proposal_text=without_code)

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["proposal_stock_mismatch"]
    assert verify_calls == []


@pytest.mark.asyncio
async def test_stock_name_with_parentheses_survives_resolution():
    """이름에 괄호가 든 종목(주문 가능 코드만 178건)이 잘리지 않는다."""
    mcp = _mcp_runner(resolved="KODEX 골드선물(H) (132030, KOSPI)")
    proposal = PROPOSAL_JSON.replace('"stock_code": "005930"', '"stock_code": "132030"')

    result, _ = await _run(mcp=mcp, proposal_text=proposal)

    assert result.status == "approved"
    assert result.order.stock_name == "KODEX 골드선물(H)"


@pytest.mark.asyncio
async def test_market_close_during_verification_stops_the_pending_order():
    """②의 장 운영 판정은 제안·검증 왕복 이전 시각이라 그 사이 장이 닫힐 수 있다.

    _handle_confirm에 장 운영 재검사가 없어, 여기서 막지 않으면 사용자는 눌러도
    브로커 거절만 본다.
    """
    store = InMemoryPendingOrderStore()
    times = iter(
        [
            datetime(2026, 5, 20, 15, 29, tzinfo=KST),
            datetime(2026, 5, 20, 15, 33, tzinfo=KST),
        ]
    )

    result, verify_calls = await _run(store=store, now_factory=lambda: next(times))

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["market_closed"]
    # 검증까지는 갔다 — 막는 지점은 대기 주문 생성 직전이다.
    assert len(verify_calls) == 1
    assert await store.get("123") is None


class _BrokenStore:
    """조회·저장이 던지는 대기 주문 저장소. InMemory 더블은 던지지 않는다."""

    def __init__(self, *, on_has=False, on_set=False):
        self._on_has = on_has
        self._on_set = on_set

    async def get(self, chat_id):
        if self._on_has:
            raise RuntimeError("redis 연결 실패")
        return None

    async def has(self, chat_id):
        if self._on_has:
            raise RuntimeError("redis 연결 실패")
        return False

    async def set_if_absent(self, chat_id, order):
        if self._on_set:
            raise RuntimeError("redis 쓰기 실패")
        return False

    async def delete(self, chat_id):
        return None


@pytest.mark.asyncio
async def test_pending_order_lookup_failure_blocks_the_flow():
    result, verify_calls = await _run(store=_BrokenStore(on_has=True))

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["store_error"]
    assert verify_calls == []


@pytest.mark.asyncio
async def test_pending_order_write_failure_is_rejected_not_swallowed():
    result, verify_calls = await _run(store=_BrokenStore(on_set=True))

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["store_error"]
    assert len(verify_calls) == 1


@pytest.mark.asyncio
async def test_losing_the_slot_race_becomes_a_conflict():
    """제안을 만드는 사이 /buy가 슬롯을 먼저 잡으면 충돌이다 — 조용히 사라지면 안 된다."""
    result, _ = await _run(store=_BrokenStore())

    assert result.status == "conflict"
    assert result.message == CONFLICT_MESSAGE


@pytest.mark.asyncio
async def test_empty_stock_is_rejected_without_any_call():
    mcp = _mcp_runner()

    result, verify_calls = await _run(stock="   ", mcp=mcp)

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["empty_stock"]
    assert mcp.calls == []
    assert verify_calls == []


@pytest.mark.asyncio
async def test_no_cooldown_is_marked_before_the_proposal_roundtrip():
    """냉각을 걸지 않아야 하는 구간을 고정한다.

    오타 한 번(/advise 없는종목)이 그 문자열을 냉각 시간만큼 잠그면 안 된다. 냉각이
    아끼는 자원은 제안 왕복인데, 이 구간은 그것을 쓰기 전이다.
    """
    unresolved = FakeCooldown()
    await _run(cooldown=unresolved, mcp=_mcp_runner(resolved="999999 (999999, UNKNOWN)"))
    assert unresolved.marked == []

    blacklisted = FakeCooldown()
    await _run(cooldown=blacklisted, limits=OrderLimits(blacklist=frozenset({"005930"})))
    assert blacklisted.marked == []

    cooling = FakeCooldown(active=True)
    await _run(cooldown=cooling)
    assert cooling.marked == []


# ---------------------------------------------------------------------------
# request_proposal — NAT {"value": ...} 봉투 계약
#
# 이 계약이 깨지면 모든 /advise가 조용히 거부로 굳는다. 함수를 한 번도 부르지 않으면
# 그 사고를 테스트가 알려주지 못한다 (#299 2차 리뷰).
# ---------------------------------------------------------------------------


def _patch_post_json(monkeypatch, payload=None, error=None):
    calls = []

    async def fake_post(path, body, timeout):
        calls.append((path, body, timeout))
        if error is not None:
            raise error
        return payload

    monkeypatch.setattr(order_assist, "_post_json", fake_post)
    return calls


@pytest.mark.asyncio
async def test_request_proposal_unwraps_the_value_envelope(monkeypatch):
    calls = _patch_post_json(monkeypatch, {"value": "제안 본문입니다."})

    answer = await order_assist.request_proposal("프롬프트", timeout=12.5)

    assert answer == "제안 본문입니다."
    assert calls == [("/v1/propose-order", {"input_message": "프롬프트"}, 12.5)]


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "WORKFLOW_ERROR", "message": "실패", "details": None},
        {"value": 42},
        {},
        [],
        "그냥 문자열",
    ],
    ids=["error_envelope", "non_string_value", "empty", "not_an_object", "bare_string"],
)
@pytest.mark.asyncio
async def test_request_proposal_refuses_anything_but_a_string_value(monkeypatch, payload):
    """봉투가 아니면 답변 텍스트가 아니다 — 진행하지 않는다."""
    _patch_post_json(monkeypatch, payload)

    with pytest.raises(RuntimeError):
        await order_assist.request_proposal("프롬프트")


@pytest.mark.asyncio
async def test_post_json_raises_on_http_error(monkeypatch):
    """4xx/5xx 본문을 정상 응답으로 읽지 않는다."""

    class _Response:
        status_code = 500
        text = "internal error"

        def json(self):
            return {"value": "이건 답변이 아니다"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    with pytest.raises(RuntimeError, match="500"):
        await order_assist._post_json("/v1/propose-order", {}, 5.0)


@pytest.mark.asyncio
async def test_advise_deletes_the_pending_order_when_the_prompt_never_sends(monkeypatch):
    """프롬프트가 안 나갔으면 대기 주문도 남기지 않는다 — 사용자는 그 존재를 모른다."""
    from backend.order_assist import OrderAssistResult
    from backend.trading_orders import PendingOrder

    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=10,
        price=74_500,
        created_at=MARKET_OPEN_NOW,
        callback_token="tok123",
    )
    handler, notifier = _advise_handler(
        monkeypatch, OrderAssistResult(status="approved", message="제안 본문", order=order)
    )
    deleted = []

    async def fail_send(text, *, reply_markup=None):
        return False

    async def record_delete(chat_id):
        deleted.append(chat_id)

    monkeypatch.setattr(notifier, "send_text", fail_send)
    monkeypatch.setattr(handler.pending_orders, "delete", record_delete)

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/advise 삼성전자"}})

    assert deleted == ["123"]
