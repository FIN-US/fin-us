"""전송 최종 실패의 메트릭과 체결 통지 정지 알람 (#259 5단계).

정지 추적기는 판정만 한다 — 무엇을 어디로 보낼지는 scheduler.trade_notification_task의
몫이고 그쪽은 test_trade_notification.py가 본다. 여기서 고정하는 것은 알람이 **정지 한
건에 한 번** 울린다는 것과, 그 정지가 어떻게 끝났는지를 구분한다는 것이다. 실패마다
울리는 알람은 이 단계 전의 error 줄과 다를 게 없다.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.delivery_alarm import (
    DELIVERY_FAILURE_KINDS,
    DeliveryMetrics,
    OutboxStallTracker,
    StallCandidate,
    delivery_metrics,
)

NOW = datetime(2026, 5, 20, 6, 0, 0, tzinfo=timezone.utc)
ALARM_AFTER = timedelta(minutes=10)
MAX_AGE = timedelta(hours=24)


def _candidate(trade_id=7, *, trade_age=timedelta(minutes=5)):
    return StallCandidate(trade_id=trade_id, trade_date=(NOW - trade_age).replace(tzinfo=None))


def _observe(tracker, *, at, failed=None, delivered=()):
    return tracker.observe(
        failed_head=failed,
        delivered_ids=set(delivered),
        now=NOW + at,
        alarm_after=ALARM_AFTER,
        max_age=MAX_AGE,
    )


# ---------------------------------------------------------------------------
# 정지 판정
# ---------------------------------------------------------------------------


def test_alarm_fires_once_when_the_same_head_keeps_failing():
    """임계를 넘는 주기에 한 번 울리고, 정지가 이어져도 다시 울리지 않는다."""
    tracker = OutboxStallTracker()
    head = _candidate()

    assert _observe(tracker, at=timedelta(0), failed=head) is None
    assert _observe(tracker, at=timedelta(minutes=9), failed=head) is None

    event = _observe(tracker, at=timedelta(minutes=10), failed=head)
    assert event is not None
    assert event.kind == "stalled"
    assert event.trade_id == 7
    assert event.failing_for == timedelta(minutes=10)

    assert _observe(tracker, at=timedelta(minutes=11), failed=head) is None
    assert _observe(tracker, at=timedelta(hours=3), failed=head) is None


def test_threshold_counts_from_the_first_failure_not_from_the_trade_time():
    """봇이 몇 시간 꺼져 있다 돌아와 첫 재배달이 한 번 실패한 것은 정지가 아니다.

    체결 시각으로부터의 나이로 재면 이 첫 실패에서 곧바로 울린다.
    """
    tracker = OutboxStallTracker()
    old_head = _candidate(trade_age=timedelta(hours=3))

    assert _observe(tracker, at=timedelta(0), failed=old_head) is None
    assert _observe(tracker, at=timedelta(minutes=1), failed=old_head) is None


def test_head_that_recovers_before_the_alarm_ends_silently():
    """울리지 않은 정지는 끝날 때도 조용하다 — 일시 장애가 복구 알림을 만들면 소음이다."""
    tracker = OutboxStallTracker()
    head = _candidate()

    _observe(tracker, at=timedelta(0), failed=head)
    assert _observe(tracker, at=timedelta(minutes=3), delivered=[7]) is None
    assert tracker.snapshot() is None


def test_a_new_head_restarts_the_clock():
    """앞 행이 나가고 다음 행이 실패하기 시작하면 그 행의 시간은 처음부터 잰다.

    대기열은 움직였다. 앞 행이 9분 실패했다고 다음 행의 첫 실패가 곧바로 알람이 되면,
    서로 다른 두 번의 짧은 장애가 한 번의 긴 정지로 읽힌다.
    """
    tracker = OutboxStallTracker()

    _observe(tracker, at=timedelta(0), failed=_candidate(7))
    _observe(tracker, at=timedelta(minutes=9), failed=_candidate(7))
    assert _observe(tracker, at=timedelta(minutes=10), failed=_candidate(8), delivered=[7]) is None
    assert _observe(tracker, at=timedelta(minutes=19), failed=_candidate(8)) is None

    event = _observe(tracker, at=timedelta(minutes=20), failed=_candidate(8))
    assert event is not None and (event.kind, event.trade_id) == ("stalled", 8)


def test_alarmed_head_that_gets_delivered_reports_delivered():
    tracker = OutboxStallTracker()
    head = _candidate()
    _observe(tracker, at=timedelta(0), failed=head)
    _observe(tracker, at=timedelta(minutes=10), failed=head)

    event = _observe(tracker, at=timedelta(minutes=15), delivered=[7])

    assert event is not None
    assert (event.kind, event.trade_id) == ("delivered", 7)
    assert event.failing_for == timedelta(minutes=15)
    assert tracker.snapshot() is None


def test_alarmed_head_that_leaves_the_window_reports_expired():
    """배달되지 않은 채 재배달 창을 벗어난 것은 복구가 아니라 포기다. 둘을 같은 말로 부르지 않는다."""
    tracker = OutboxStallTracker()
    head = _candidate(trade_age=timedelta(hours=23, minutes=50))
    _observe(tracker, at=timedelta(0), failed=head)
    _observe(tracker, at=timedelta(minutes=10), failed=head)

    # 체결 후 24시간 1분. 목록에서 빠져 이번 주기에는 실패도 배달도 없다.
    event = _observe(tracker, at=timedelta(minutes=11))

    assert event is not None
    assert event.kind == "expired"


def test_alarmed_head_that_disappears_inside_the_window_reports_cleared():
    """이 잡이 보내지 않았는데 사라졌다 — 배달됐다고 말할 근거가 없다."""
    tracker = OutboxStallTracker()
    head = _candidate()
    _observe(tracker, at=timedelta(0), failed=head)
    _observe(tracker, at=timedelta(minutes=10), failed=head)

    event = _observe(tracker, at=timedelta(minutes=11))

    assert event is not None
    assert event.kind == "cleared"


def test_ending_one_stall_and_starting_another_yields_only_the_ending():
    """한 주기에 이벤트는 하나다. 새 행은 추적만 시작하고 자기 임계를 처음부터 기다린다."""
    tracker = OutboxStallTracker()
    _observe(tracker, at=timedelta(0), failed=_candidate(7))
    _observe(tracker, at=timedelta(minutes=10), failed=_candidate(7))

    event = _observe(tracker, at=timedelta(minutes=11), failed=_candidate(8), delivered=[7])

    assert event is not None and event.kind == "delivered"
    snapshot = tracker.snapshot()
    assert snapshot is not None
    assert (snapshot["trade_id"], snapshot["alarmed"]) == (8, False)


# ---------------------------------------------------------------------------
# 메트릭
# ---------------------------------------------------------------------------


def test_record_failure_counts_per_kind_and_leaves_a_countable_line(caplog):
    metrics = DeliveryMetrics(now_factory=lambda: NOW)

    with caplog.at_level(logging.WARNING, logger="backend.delivery_alarm"):
        metrics.record_failure("fill_redelivery", trade_id=7)
        metrics.record_failure("fill_redelivery", trade_id=8)
        metrics.record_failure("settled_send")

    assert metrics.count("fill_redelivery") == 2
    assert metrics.count("settled_send") == 1
    assert metrics.count("fill_mark") == 0
    lines = [record.getMessage() for record in caplog.records]
    assert lines == [
        "[delivery-fail] kind=fill_redelivery count=1 trade_id=7",
        "[delivery-fail] kind=fill_redelivery count=2 trade_id=8",
        "[delivery-fail] kind=settled_send count=1",
    ]


def test_redelivery_failure_is_counted_once_per_trade(caplog):
    """재배달은 1분마다 같은 행을 다시 시도한다 (PR #375 리뷰).

    주기마다 세면 24시간 막힌 체결 한 건이 약 1440건으로 부푼다. 반면 마킹 실패는 매번
    사용자 화면에 중복 메시지 한 건을 만들므로 발생마다 센다 — 둘을 같은 규칙으로 묶으면
    한쪽 단위가 깨진다.
    """
    clock = [NOW]
    metrics = DeliveryMetrics(now_factory=lambda: clock[0])

    with caplog.at_level(logging.WARNING, logger="backend.delivery_alarm"):
        metrics.record_failure("fill_redelivery", trade_id=7)
        clock[0] = NOW + timedelta(minutes=5)
        metrics.record_failure("fill_redelivery", trade_id=7)
        metrics.record_failure("fill_mark", trade_id=7)
        metrics.record_failure("fill_mark", trade_id=7)

    assert metrics.count("fill_redelivery") == 1
    assert metrics.count("fill_mark") == 2
    # grep 한 줄이 곧 한 건이어야 로그 집계와 API 값이 같다.
    assert len([r for r in caplog.records if "[delivery-fail]" in r.getMessage()]) == 3
    # 횟수는 그대로여도 "아직 실패 중"은 시각이 말한다.
    failures = metrics.snapshot()["failures"]
    assert isinstance(failures, dict)
    assert failures["fill_redelivery"]["last_failed_at"] == clock[0].isoformat()


def test_snapshot_carries_when_counting_started():
    """0을 "실패 없음"으로 읽기 전에 언제부터 센 값인지 볼 수 있어야 한다."""
    metrics = DeliveryMetrics(now_factory=lambda: NOW)
    metrics.record_failure("fill_mark", trade_id=3)

    snapshot = metrics.snapshot()

    assert snapshot["started_at"] == NOW.isoformat()
    failures = snapshot["failures"]
    assert isinstance(failures, dict)
    assert set(failures) == set(DELIVERY_FAILURE_KINDS)
    assert failures["fill_mark"] == {"count": 1, "last_failed_at": NOW.isoformat()}
    assert failures["settled_send"] == {"count": 0, "last_failed_at": None}


def test_delivery_status_endpoint_exposes_the_counts():
    from backend.main import app

    delivery_metrics.record_failure("fill_notify", trade_id=1)

    response = TestClient(app).get("/api/v1/system/delivery")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["failures"]["fill_notify"]["count"] == 1
    assert data["trade_outbox_stall"] is None
