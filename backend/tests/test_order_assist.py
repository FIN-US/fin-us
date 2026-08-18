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
        (QUOTE_TEXT, "[보유 종목 리스트]\n- 총 평가금액: 100원", "주문가능금액"),
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


def test_qualitative_reason_drops_sentences_containing_numbers():
    reason = "근거가 충분합니다. 현재가는 99,999원으로 보입니다. 리스크는 제한적입니다."

    assert qualitative_reason(reason) == "근거가 충분합니다. 리스크는 제한적입니다."


def test_qualitative_reason_falls_back_when_every_sentence_has_numbers():
    result = qualitative_reason("총 평가금액 12,345,678원 대비 비중이 3% 입니다.")

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

    async def active(self, stock, rule_id):
        return self._active, self._checked

    async def mark(self, stock, rule_id):
        self.marked.append((stock, rule_id))


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
):
    verify_calls = verify_calls if verify_calls is not None else []

    async def propose(prompt):
        return proposal_text

    async def verify(payload, proposal_id):
        verify_calls.append((payload, proposal_id))
        return verdict if verdict is not None else VerifierVerdict(True, "근거가 명확합니다.")

    result = await run_order_assist(
        ProposalTrigger(source="telegram", stock="삼성전자", chat_id="123"),
        pending_orders=store if store is not None else InMemoryPendingOrderStore(),
        mcp_runner=mcp if mcp is not None else _mcp_runner(),
        now_factory=lambda: now,
        limits=limits or OrderLimits(),
        cooldown=cooldown or FakeCooldown(),
        session_factory=lambda: _EmptySession(),
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
    cooldown = FakeCooldown(active=True)
    mcp = _mcp_runner()

    result, verify_calls = await _run(cooldown=cooldown, mcp=mcp)

    assert result.status == "rejected"
    assert [v.code for v in result.violations] == ["cooldown"]
    assert mcp.calls == []
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
    assert cooldown.marked == [("삼성전자", None)]


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

    assert keys.order_proposal_cooldown("삼성전자", None).endswith(":삼성전자:manual")
    assert keys.order_proposal_cooldown("삼성전자", "rule-7").endswith(":삼성전자:rule-7")


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

    assert await cooldown.active("삼성전자", None) == (False, True)
    await cooldown.mark("삼성전자", None)

    assert client.sets == [(RedisKeys().order_proposal_cooldown("삼성전자", None), "proposed", 3600)]
    assert client.closed is True


@pytest.mark.asyncio
async def test_proposal_cooldown_reports_lookup_failure():
    class _BrokenRedis:
        async def exists(self, key):
            raise RuntimeError("connection refused")

        async def aclose(self):
            return None

    cooldown = order_assist.ProposalCooldown(lambda: _BrokenRedis())

    assert await cooldown.active("삼성전자", None) == (True, False)


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
