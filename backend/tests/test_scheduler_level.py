"""스케줄러가 내보내는 메시지에도 /level 설정이 반영되는지 (#297 자가리뷰).

분석 알림만 수준을 읽고 브리핑·촉매 알림은 기본값을 그대로 넘기고 있었다. 사용자 눈에는
"중급으로 바꿨는데 아침 브리핑에만 용어 설명이 붙는다"로 보이고, 설명할 수 없는 차이다.
"""

from contextlib import asynccontextmanager
from datetime import date

import pytest

import backend.scheduler as scheduler_module
from backend.presentation import LEVEL_INTERMEDIATE, TERM_FOOTNOTE_MARK
from backend.watchlist_repo import SqliteWatchlistRepo


class FakeState:
    def __init__(self, level=LEVEL_INTERMEDIATE):
        self.level = level
        self.released = []

    async def get_telegram_user_level(self):
        return self.level

    async def acquire_scheduler_lock(self, job_name="market_monitoring"):
        return "token"

    async def release_lock(self, key, token):
        self.released.append((key, token))

    @property
    def keys(self):
        class _Keys:
            @staticmethod
            def scheduler_lock(job_name):
                return f"finus:scheduler:lock:{job_name}"

        return _Keys()


@pytest.fixture
def fake_redis_state(monkeypatch):
    """scheduler가 여는 redis_state를 가짜로 바꾼다."""

    def _install(state):
        @asynccontextmanager
        async def _factory():
            yield state

        monkeypatch.setattr(scheduler_module, "redis_state", _factory)
        return state

    return _install


class FakeNotifier:
    def __init__(self):
        self.messages = []

    async def send_text(self, text, *, reply_markup=None):
        self.messages.append(text)
        return True


class FakeCatalystRepo:
    def __init__(self, events):
        self._events = events
        self.marked = []

    async def list_due_for_notification(self, watchlist, *, today):
        return list(self._events)

    async def upsert_events(self, events):
        # 실제 repo는 새로 만든 행 목록을 돌려준다. 이 대역이 None을 돌려주던 동안에는
        # 그 어긋남을 cast가 덮고 있었다 (#319).
        return []

    async def mark_notification_sent(self, event_id, *, days_until_event):
        self.marked.append(event_id)


class FakeWatchlistRepo:
    async def get_watchlist(self):
        return ["삼성전자"]


@pytest.mark.asyncio
async def test_morning_briefing_reads_the_saved_level(monkeypatch, fake_redis_state):
    """cron은 인자 없이 부르므로 태스크가 직접 읽어야 한다."""
    fake_redis_state(FakeState(LEVEL_INTERMEDIATE))
    notifier = FakeNotifier()

    async def fake_briefing(watchlist):
        # 사전에 있는 말을 넣어, 수준이 무시되면 각주가 붙어 눈에 띄게 한다.
        return {"market_summary": "예수금과 수급을 확인하세요.", "watchlist": [], "trading_ideas": [], "catalysts": []}

    monkeypatch.setattr(scheduler_module, "SqliteWatchlistRepo", lambda factory: FakeWatchlistRepo())
    monkeypatch.setattr(scheduler_module, "generate_morning_briefing", fake_briefing)
    monkeypatch.setattr(scheduler_module.telegram_notifier, "send_text", notifier.send_text)

    await scheduler_module.morning_briefing_task()

    assert notifier.messages
    assert TERM_FOOTNOTE_MARK not in notifier.messages[-1]


@pytest.mark.asyncio
async def test_morning_briefing_falls_back_when_the_level_cannot_be_read(monkeypatch):
    """수준을 못 읽었다고 브리핑 자체를 거르면 부가 기능이 본 기능을 잡아먹는다."""

    @asynccontextmanager
    async def _broken():
        raise ConnectionError("redis unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(scheduler_module, "redis_state", _broken)
    notifier = FakeNotifier()

    async def fake_briefing(watchlist):
        return {"market_summary": "예수금을 확인하세요.", "watchlist": [], "trading_ideas": [], "catalysts": []}

    monkeypatch.setattr(scheduler_module, "SqliteWatchlistRepo", lambda factory: FakeWatchlistRepo())
    monkeypatch.setattr(scheduler_module, "generate_morning_briefing", fake_briefing)
    monkeypatch.setattr(scheduler_module.telegram_notifier, "send_text", notifier.send_text)

    await scheduler_module.morning_briefing_task()

    # 기본값(초보)으로 떨어져 설명이 붙는 쪽을 택한다.
    assert notifier.messages
    assert f"{TERM_FOOTNOTE_MARK} 예수금: " in notifier.messages[-1]


@pytest.mark.asyncio
async def test_catalyst_alerts_carry_the_saved_level(monkeypatch, fake_redis_state):
    """redis 잠금을 쓰는 바깥 호출이 수준을 읽어 안쪽으로 넘긴다."""
    fake_redis_state(FakeState(LEVEL_INTERMEDIATE))
    notifier = FakeNotifier()
    event = type(
        "Event",
        (),
        {
            "id": 1,
            "stock_name": "삼성전자",
            "event_type": "earnings",
            "event_date": date(2026, 8, 20),
            "description": "실적 발표로 수급이 흔들릴 수 있습니다",
            "days_until_event": 2,
        },
    )()
    repo = FakeCatalystRepo([event])

    monkeypatch.setattr(scheduler_module, "_collect_catalyst_events", lambda *a, **k: _noop())

    await scheduler_module.catalyst_calendar_task(
        watchlist_repo=FakeWatchlistRepo(),
        catalyst_repo=repo,
        notifier=notifier,
        today_factory=lambda: date(2026, 8, 18),
    )

    assert notifier.messages
    assert TERM_FOOTNOTE_MARK not in notifier.messages[-1]


@pytest.mark.asyncio
async def test_a_no_redis_run_does_not_open_redis_for_the_level(monkeypatch):
    """use_redis_lock=False는 "redis를 쓰지 않는다"는 뜻이다. 수준 때문에 열지 않는다."""
    opened = False

    @asynccontextmanager
    async def _tripwire():
        nonlocal opened
        opened = True
        raise AssertionError("redis를 열면 안 된다")
        yield  # pragma: no cover

    monkeypatch.setattr(scheduler_module, "redis_state", _tripwire)
    monkeypatch.setattr(scheduler_module, "_collect_catalyst_events", lambda *a, **k: _noop())
    notifier = FakeNotifier()

    await scheduler_module.catalyst_calendar_task(
        watchlist_repo=FakeWatchlistRepo(),
        catalyst_repo=FakeCatalystRepo([]),
        notifier=notifier,
        today_factory=lambda: date(2026, 8, 18),
        use_redis_lock=False,
    )

    assert opened is False


async def _noop():
    return None
