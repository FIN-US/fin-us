"""지정가 괴리 공유 판정표 — backend 쪽 소비자 (#365).

``finus_nat/tests/test_order_price_guard.py``가 같은 표로 NAT의 pass-through 주문 가드
(``order_price_guard.price_gap_exceeds``)를 돌린다. 판정이 두 계층에 복제돼 있으므로, 표를
공유하지 않으면 한쪽만 바꿔도 아무 테스트도 red가 되지 않는다 — #138의
``orderable_code_policy.json``과 같은 방식이다.

경로는 ``__file__`` 기준 절대 경로라 worktree·CI 양쪽에서 같게 해석된다.
"""
import json
from pathlib import Path

import pytest

from backend.order_assist import (
    AccountSnapshot,
    DailyUsage,
    OrderLimits,
    OrderProposal,
    evaluate_hard_limits,
)

_POLICY_PATH = Path(__file__).resolve().parent / "fixtures" / "price_gap_policy.json"
_CASES = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_evaluate_hard_limits_follows_the_shared_price_gap_table(case):
    """괴리 외 한도는 걸리지 않게 계좌를 넉넉히 두고 ``price_gap`` 위반 여부만 본다.

    뮤테이션: ``evaluate_hard_limits``의 괴리 비교 ``>``를 ``>=``로 바꾸면 경계 행이, ``abs``를
    빼면 ``just_under_lower_boundary`` 행이 red가 된다.
    """
    proposal = OrderProposal(
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=1,
        order_type="LIMIT",
        price=case["price"],
        rationale="분기 실적 개선",
        confidence=0.9,
    )
    snapshot = AccountSnapshot(
        current_price=case["current_price"],
        cash=10**12,
        total_value=10**13,
        holding_qty=0,
    )
    limits = OrderLimits(
        max_order_amount=10**12,
        max_daily_amount=10**12,
        max_position_ratio=1.0,
        min_cash_ratio=0.0,
        max_price_gap_ratio=case["max_price_gap_ratio"],
    )

    codes = [v.code for v in evaluate_hard_limits(proposal, snapshot, limits, DailyUsage()).violations]

    assert ("price_gap" in codes) is case["exceeds"]
    # 괴리 판정만 보는 테스트다 — 다른 한도가 끼면 표가 무엇을 고정하는지 흐려진다.
    assert set(codes) <= {"price_gap"}
