"""체결 통지 outbox (#259 2단계).

주문 경로가 체결을 원장에 남기고(notified_at = null) 통지가 나가면 그 자리를 채운다.
채워지지 않은 행은 scheduler.trade_notification_task가 다음 주기에 다시 알린다. 여기서
고정하는 것은 그 창의 양쪽 경계(너무 최근·너무 오래)와 "성공했을 때만 마킹한다"는
불변식이다 — 그게 깨지면 outbox가 없애려던 무응답이 중복 배달로 바뀔 뿐이다.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine

from backend import scheduler as scheduler_module
from backend.models import TradeHistory
from backend.presentation import split_for_telegram
from backend.trading_orders import _extract_order_message
from backend.redis_state import SCHEDULER_LOCK_TTL_SEC
from backend.trade_notification_repo import (
    PendingTradeNotification,
    SqliteTradeNotificationRepo,
    mark_trade_notified,
)
from backend.trading_orders import OrderExecutionResult, TradeRecorder

NOW = datetime(2026, 5, 20, 6, 0, 0, tzinfo=timezone.utc)
GRACE = timedelta(seconds=60)
MAX_AGE = timedelta(hours=24)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return lambda: Session(engine)


def _add_trade(session_factory, *, minutes_ago, notified=False):
    """tz 없는 UTC로 체결 행 하나를 넣는다 — 실제 컬럼이 그 축이다."""
    trade_date = (NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
    with session_factory() as session:
        trade = TradeHistory(
            stock_code="005930",
            stock_name="삼성전자",
            trade_type="BUY",
            quantity=1,
            price=75000,
            trade_date=trade_date,
            notified_at=trade_date if notified else None,
        )
        session.add(trade)
        session.commit()
        session.refresh(trade)
        assert trade.id is not None
        return trade.id


async def _list(repo, *, limit=10):
    return await repo.list_unnotified(now=NOW, grace=GRACE, max_age=MAX_AGE, limit=limit)


def _notified_at(session_factory, trade_id):
    with session_factory() as session:
        trade = session.get(TradeHistory, trade_id)
        assert trade is not None
        return trade.notified_at


# ---------------------------------------------------------------------------
# 저장소: 창의 양쪽 경계
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unnotified_trade_outside_the_grace_is_due(session_factory):
    trade_id = _add_trade(session_factory, minutes_ago=5)
    repo = SqliteTradeNotificationRepo(session_factory)

    due = await _list(repo)

    assert [item.id for item in due] == [trade_id]


@pytest.mark.asyncio
async def test_trade_inside_the_grace_is_left_alone(session_factory):
    """주문 경로가 기록 → 전송 → 마킹 순서라 방금 넣은 행은 아직 전송 중일 수 있다.

    이 유예가 없으면 outbox가 주문 경로와 경합해 같은 체결을 두 번 보낸다.
    """
    _add_trade(session_factory, minutes_ago=0.5)
    repo = SqliteTradeNotificationRepo(session_factory)

    assert await _list(repo) == []


@pytest.mark.asyncio
async def test_trade_older_than_the_window_is_not_redelivered(session_factory):
    """며칠 지난 체결을 지금 알리는 것은 복구가 아니라 소음이다.

    걸러도 notified_at은 null로 남는다 — 통지되지 않았다는 사실 자체는 원장에 보존된다.
    """
    trade_id = _add_trade(session_factory, minutes_ago=60 * 25)
    repo = SqliteTradeNotificationRepo(session_factory)

    assert await _list(repo) == []
    assert _notified_at(session_factory, trade_id) is None


@pytest.mark.asyncio
async def test_already_notified_trade_is_not_listed(session_factory):
    _add_trade(session_factory, minutes_ago=5, notified=True)
    repo = SqliteTradeNotificationRepo(session_factory)

    assert await _list(repo) == []


@pytest.mark.asyncio
async def test_due_trades_come_oldest_first_and_respect_the_limit(session_factory):
    """오래된 것부터 보낸다. 배치 상한에 걸려도 가장 오래 기다린 통지가 먼저 나간다.

    삽입 순서를 일부러 섞는다. 오래된 순으로 넣으면 id 순서와 시각 순서가 같아져,
    정렬을 통째로 지워도 이 단언이 통과한다.
    """
    middle = _add_trade(session_factory, minutes_ago=20)
    _add_trade(session_factory, minutes_ago=10)
    oldest = _add_trade(session_factory, minutes_ago=30)
    repo = SqliteTradeNotificationRepo(session_factory)

    due = await _list(repo, limit=2)

    assert [item.id for item in due] == [oldest, middle]


def test_mark_trade_notified_keeps_the_first_notification_time(session_factory):
    """이미 통지된 행은 덮지 않는다 — 처음 시각이 살아남아야 지연이 뒤로 밀리지 않는다."""
    trade_id = _add_trade(session_factory, minutes_ago=30)
    first = NOW - timedelta(minutes=20)

    mark_trade_notified(session_factory, trade_id, notified_at=first)
    mark_trade_notified(session_factory, trade_id, notified_at=NOW)

    assert _notified_at(session_factory, trade_id) == first.replace(tzinfo=None)


def _sample_result(price=75000):
    return OrderExecutionResult(
        stock_code="005930",
        stock_name="삼성전자",
        side="BUY",
        quantity=1,
        price=price,
        message="주문 접수",
        raw_result="{}",
    )


def test_recorder_leaves_the_row_unnotified(session_factory):
    """기록만으로는 통지가 아니다. 이 null이 곧 outbox의 대기열이다."""
    trade_id = TradeRecorder(session_factory).record(_sample_result())

    assert _notified_at(session_factory, trade_id) is None


def test_recorder_marks_the_row_it_returned(session_factory):
    """record가 돌려준 id가 곧 통지 멱등 키다."""
    recorder = TradeRecorder(session_factory)
    trade_id = recorder.record(_sample_result())

    recorder.mark_notified(trade_id, notified_at=NOW)

    assert _notified_at(session_factory, trade_id) == NOW.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 재배달 작업
# ---------------------------------------------------------------------------


class FakeNotifier:
    def __init__(self, *, results=None, enabled=True):
        self.enabled = enabled
        self.messages = []
        self._results = list(results or [])

    async def send_text(self, text, *, reply_markup=None):
        self.messages.append(text)
        return self._results.pop(0) if self._results else True


class FakeRepo:
    def __init__(self, pending, *, mark_error=None):
        self.pending = pending
        self.mark_error = mark_error
        self.marked = []
        self.limits = []

    async def list_unnotified(self, *, now, grace, max_age, limit):
        self.limits.append(limit)
        return self.pending[:limit]

    async def mark_notified(self, trade_id, *, notified_at):
        if self.mark_error is not None:
            raise self.mark_error
        self.marked.append(trade_id)


def _pending(trade_id, *, price=75000):
    return PendingTradeNotification(
        id=trade_id,
        stock_code="005930",
        stock_name="삼성전자",
        trade_type="BUY",
        quantity=2,
        price=price,
        trade_date=(NOW - timedelta(minutes=10)).replace(tzinfo=None),
    )


async def _run_task(repo, notifier):
    await scheduler_module.trade_notification_task(
        repo=repo,
        notifier=notifier,
        now_factory=lambda: NOW,
        use_redis_lock=False,
    )


@pytest.mark.asyncio
async def test_task_sends_and_marks_the_pending_trade():
    repo = FakeRepo([_pending(7)])
    notifier = FakeNotifier()

    await _run_task(repo, notifier)

    assert repo.marked == [7]
    assert len(notifier.messages) == 1
    message = notifier.messages[0]
    # 재전송임을 밝힌다 — 마킹 전에 죽으면 이미 받은 통지가 한 번 더 나갈 수 있고,
    # 그때 이 줄이 없으면 사용자는 주문이 두 번 나간 것으로 읽는다.
    assert "재전송" in message
    assert "삼성전자 (005930)" in message
    assert "매수 2주" in message
    # 체결 시각은 KST로 읽는다. tz 없는 UTC를 그대로 찍으면 9시간 이른 시각이 나간다.
    assert "2026-05-20 14:50 KST" in message


@pytest.mark.asyncio
async def test_task_does_not_mark_when_the_send_fails():
    """마킹하지 않는 것이 곧 재시도다 — 다음 주기가 같은 행을 다시 집는다."""
    repo = FakeRepo([_pending(7)])
    notifier = FakeNotifier(results=[False])

    await _run_task(repo, notifier)

    assert repo.marked == []


@pytest.mark.asyncio
async def test_task_stops_the_batch_at_the_first_send_failure():
    """실패의 지배적 원인은 채팅 단위 rate limit이다. 계속 보내면 ban만 늘린다."""
    repo = FakeRepo([_pending(7), _pending(8), _pending(9)])
    notifier = FakeNotifier(results=[True, False, True])

    await _run_task(repo, notifier)

    assert repo.marked == [7]
    assert len(notifier.messages) == 2


@pytest.mark.asyncio
async def test_task_keeps_going_when_marking_fails():
    """전송은 이미 나갔다. 마킹 실패로 예외가 새면 스케줄러 잡이 죽는다."""
    repo = FakeRepo([_pending(7)], mark_error=RuntimeError("db locked"))
    notifier = FakeNotifier()

    await _run_task(repo, notifier)

    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_task_sends_nothing_when_telegram_is_disabled():
    """보낼 곳이 없으면 마킹도 하지 않는다 — 다시 켜지면 창 안의 체결은 그때 나간다."""
    repo = FakeRepo([_pending(7)])
    notifier = FakeNotifier(enabled=False)

    await _run_task(repo, notifier)

    assert notifier.messages == []
    assert repo.marked == []


@pytest.mark.asyncio
async def test_batch_is_capped_so_one_cycle_cannot_flood_the_chat():
    """채팅당 초당 ~1건 제한이 있어 한 번에 쏟으면 429를 스스로 만든다."""
    repo = FakeRepo([_pending(index) for index in range(1, 30)])
    notifier = FakeNotifier()

    await _run_task(repo, notifier)

    assert repo.limits == [scheduler_module.TRADE_NOTIFY_BATCH_LIMIT]
    assert len(notifier.messages) == scheduler_module.TRADE_NOTIFY_BATCH_LIMIT


@pytest.mark.asyncio
async def test_unpriced_trade_is_not_reported_as_a_zero_won_order():
    """price = 0은 0원 거래가 아니라 금액 모름이다 (#309)."""
    repo = FakeRepo([_pending(7, price=0)])
    notifier = FakeNotifier()

    await _run_task(repo, notifier)

    assert "단가 미상" in notifier.messages[0]
    assert "0원" not in notifier.messages[0]


# ---------------------------------------------------------------------------
# 실행 락
# ---------------------------------------------------------------------------


class FakeSchedulerState:
    """redis_state가 여는 상태 객체의 대역. 잡별 TTL과 해제를 관찰한다."""

    def __init__(self, *, token: str | None = "token"):
        self.token = token
        self.acquired = []
        self.released = []

    async def acquire_scheduler_lock(self, job_name="market_monitoring", *, ttl_sec=None):
        self.acquired.append((job_name, ttl_sec))
        return self.token

    async def release_lock(self, key, token):
        self.released.append((key, token))

    @property
    def keys(self):
        class _Keys:
            @staticmethod
            def scheduler_lock(job_name):
                return f"finus:scheduler:lock:{job_name}"

        return _Keys()


@pytest.fixture()
def fake_redis_state(monkeypatch):
    def _install(state):
        @asynccontextmanager
        async def _factory():
            yield state

        monkeypatch.setattr(scheduler_module, "redis_state", _factory)
        return state

    return _install


@pytest.mark.asyncio
async def test_task_takes_a_short_lived_lock_and_releases_it(fake_redis_state):
    """잡별 TTL을 실제로 넘긴다 (#259 2단계).

    기본값(SCHEDULER_LOCK_TTL_SEC, 30분)이 그대로 쓰이면 락 누수 한 번이 30분 정지가
    되는데, 이 잡에서 정지는 곧 "체결됐는데 아무 말도 없는" 시간이다. 락 분기는 이
    테스트가 유일하게 지나간다 — 나머지는 use_redis_lock=False로 우회한다.
    """
    state = fake_redis_state(FakeSchedulerState())
    repo = FakeRepo([_pending(7)])
    notifier = FakeNotifier()

    await scheduler_module.trade_notification_task(
        repo=repo, notifier=notifier, now_factory=lambda: NOW
    )

    assert state.acquired == [
        ("trade_notification", scheduler_module.TRADE_NOTIFY_LOCK_TTL_SECONDS)
    ]
    assert scheduler_module.TRADE_NOTIFY_LOCK_TTL_SECONDS < SCHEDULER_LOCK_TTL_SEC
    # 잡을 실제로 돌았고, 끝나면 락을 놓는다.
    assert repo.marked == [7]
    assert state.released == [("finus:scheduler:lock:trade_notification", "token")]


@pytest.mark.asyncio
async def test_task_skips_when_another_worker_holds_the_lock(fake_redis_state):
    """락을 못 잡으면 아무것도 보내지 않는다 — 잡았을 때만 보내는 것이 락의 전부다."""
    fake_redis_state(FakeSchedulerState(token=None))
    repo = FakeRepo([_pending(7)])
    notifier = FakeNotifier()

    await scheduler_module.trade_notification_task(
        repo=repo, notifier=notifier, now_factory=lambda: NOW
    )

    assert notifier.messages == []
    assert repo.marked == []


# ---------------------------------------------------------------------------
# TRADE_NOTIFY_GRACE가 기대는 전제
# ---------------------------------------------------------------------------


def test_fill_notification_is_always_a_single_telegram_part():
    """체결 통지가 한 조각이어야 TRADE_NOTIFY_GRACE의 근거가 선다 (#259 2단계).

    send_text는 조각마다 요청을 따로 치고 각 요청에 httpx 타임아웃 10초가 붙는다. 즉
    주문 경로의 기록 → 마킹 사이 상한은 10초 × 조각 수다. 조각이 7개면 60초 유예를 넘겨
    outbox가 전송 중인 행을 집고, 그것이 이 상수가 막으려던 바로 그 중복이다.

    한 조각인 근거는 _extract_order_message가 모든 분기에서 500자로 자른다는 것이다.
    그 상한을 걷어내면 이 테스트가 깨진다 — 그때는 유예를 함께 올려야 한다.
    """
    message = _extract_order_message("주" * 5000)

    assert len(message) <= 500
    assert len(split_for_telegram(f"주문 완료: {message}")) == 1
