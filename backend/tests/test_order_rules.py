"""스케줄러 룰 트리거의 판정 규칙 (#314).

여기 있는 것은 전부 순수 함수 검사다. 제안을 실제로 태우는 경로는
test_scheduler.py의 run_rule_triggered_proposal 테스트가 본다.
"""

import pytest

from ..order_rules import (
    RULE_ID,
    OrderAssistRule,
    RuleMatch,
    build_trigger_signal,
    format_auto_message,
    in_rule_scope,
    load_rule,
    match_rule,
    most_urgent,
    should_report_rejection,
    should_run,
)


def _rule(
    sources: set[str] | None = None, urgency: set[str] | None = None
) -> OrderAssistRule:
    return OrderAssistRule(
        rule_id=RULE_ID,
        sources=frozenset(sources or {"news", "disclosure"}),
        urgency_levels=frozenset(urgency or {"critical"}),
    )


# ---------------------------------------------------------------------------
# load_rule — 기본은 꺼짐, 빈 조건은 fail-closed
# ---------------------------------------------------------------------------


def test_rule_is_off_unless_explicitly_enabled():
    """사용자가 켠 적 없는데 확정 버튼이 뜨는 것은 어떤 한도로도 되돌릴 수 없다."""
    assert load_rule(enabled=False) is None


def test_enabled_rule_carries_the_configured_conditions():
    rule = load_rule(
        enabled=True,
        sources=frozenset({"disclosure"}),
        urgency_levels=frozenset({"high", "critical"}),
    )

    assert rule is not None
    assert rule.rule_id == RULE_ID
    assert rule.sources == frozenset({"disclosure"})
    assert rule.urgency_levels == frozenset({"high", "critical"})


@pytest.mark.parametrize(
    "sources, urgency",
    [
        (frozenset(), frozenset({"critical"})),
        (frozenset({"news"}), frozenset()),
    ],
)
def test_empty_condition_turns_the_rule_off_not_wide_open(sources, urgency):
    """빈 조건을 '전부 허용'으로 읽으면 설정 실수가 자동 제안을 가장 넓게 여는 방향으로 샌다."""
    assert load_rule(enabled=True, sources=sources, urgency_levels=urgency) is None


# ---------------------------------------------------------------------------
# match_rule
# ---------------------------------------------------------------------------


def test_match_requires_both_source_and_urgency():
    rule = _rule(sources={"disclosure"}, urgency={"critical"})

    matched = match_rule(
        rule, stock="삼성전자", source="disclosure", analysis_data={"urgency": "critical"}
    )
    assert matched == RuleMatch(
        rule_id=RULE_ID, stock="삼성전자", source="disclosure", urgency="critical"
    )

    # 소스가 다르면 긴급도가 맞아도 트리거되지 않는다.
    assert (
        match_rule(rule, stock="삼성전자", source="news", analysis_data={"urgency": "critical"})
        is None
    )
    # 긴급도가 낮으면 소스가 맞아도 트리거되지 않는다.
    assert (
        match_rule(rule, stock="삼성전자", source="disclosure", analysis_data={"urgency": "high"})
        is None
    )


def test_no_rule_means_no_match():
    assert (
        match_rule(None, stock="삼성전자", source="news", analysis_data={"urgency": "critical"})
        is None
    )


@pytest.mark.parametrize("analysis_data", [None, {}, {"urgency": None}, {"urgency": 3}])
def test_unreadable_urgency_does_not_trigger(analysis_data):
    """긴급도를 읽지 못한 분석은 '판정 불가'다. 판정하지 못한 상태로 제안을 만들지 않는다."""
    assert (
        match_rule(_rule(), stock="삼성전자", source="news", analysis_data=analysis_data)
        is None
    )


def test_urgency_is_compared_case_insensitively():
    matched = match_rule(
        _rule(), stock="삼성전자", source="news", analysis_data={"urgency": " CRITICAL "}
    )

    assert matched is not None
    assert matched.urgency == "critical"


# ---------------------------------------------------------------------------
# in_rule_scope
# ---------------------------------------------------------------------------


def test_scope_is_owned_and_watchlist_only():
    assert in_rule_scope("삼성전자", ["삼성전자"], [])
    assert in_rule_scope("NAVER", [], ["NAVER"])
    # 감시 기본 종목(DEFAULT_MONITOR_STOCKS)은 사용자가 고른 종목이 아니다.
    assert not in_rule_scope("SK하이닉스", [], [])


# ---------------------------------------------------------------------------
# most_urgent — 한 주기에 한 건
# ---------------------------------------------------------------------------


def test_most_urgent_wins():
    high = RuleMatch(RULE_ID, "삼성전자", "news", "high")
    critical = RuleMatch(RULE_ID, "NAVER", "disclosure", "critical")

    assert most_urgent([high, critical]) is critical


def test_ties_keep_the_first_seen_so_the_choice_is_deterministic():
    first = RuleMatch(RULE_ID, "삼성전자", "news", "critical")
    second = RuleMatch(RULE_ID, "NAVER", "news", "critical")

    assert most_urgent([first, second]) is first


def test_most_urgent_of_nothing_is_none():
    assert most_urgent([]) is None


# ---------------------------------------------------------------------------
# trigger_signal — 수치를 싣지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["news", "disclosure", "unknown_source"])
def test_trigger_signal_carries_no_numbers(source):
    """이 계약이 깨지면 환각 게이트의 화이트리스트가 조용히 넓어진다.

    /v1/propose-order는 프롬프트 하나가 곧 transcript 전체라, build_proposal_prompt가
    실어 보낸 수치는 도구 호출 없이도 fe_branch를 통과한다. trigger_signal은 수치가 아니라
    "무엇을 확인하라"는 지시 문구여야 한다.
    """
    signal = build_trigger_signal(source)

    assert signal
    assert not any(char.isdigit() for char in signal)


def test_unknown_source_still_gets_an_instruction():
    """소스가 늘어도 지시 문구가 비지 않는다 — 빈 문자열이면 프롬프트에 계기가 사라진다."""
    assert build_trigger_signal("brand_new_source") == build_trigger_signal("another")


# ---------------------------------------------------------------------------
# 통지 정책
# ---------------------------------------------------------------------------


def test_alerts_off_stops_the_trigger_entirely():
    """승인은 모드와 무관하게 보내야 하므로, 알림을 끈 사용자에게는 제안 자체를 만들지 않는다."""
    assert not should_run("off")
    assert should_run("urgent")
    assert should_run("all")


def test_rejections_only_reach_the_user_in_all_mode():
    assert should_report_rejection("all")
    assert not should_report_rejection("urgent")
    assert not should_report_rejection("off")


def test_auto_message_says_it_is_automatic_and_keeps_the_original():
    decorated = format_auto_message("주문 제안 본문")

    assert decorated.endswith("주문 제안 본문")
    assert "자동 제안" in decorated
