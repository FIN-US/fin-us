"""스케줄러 룰 트리거의 실행 경로 (#314).

판정 규칙(무엇이 룰을 만족하는가)은 test_order_rules.py가 본다. 여기서 보는 것은 그
판정이 실제로 run_order_assist를 부르는 자리 — 어떤 문턱에서 멈추는지, 승인 결과가
기존 확정 버튼 경로를 그대로 타는지, 거부·충돌을 누구에게 알리는지다.
"""

import asyncio
import logging
from datetime import datetime
from typing import cast

import pytest
from sqlmodel import Session

from ..order_assist import OrderAssistResult
from ..order_rules import RULE_ID, OrderAssistRule, RuleMatch
from ..redis_state import InMemoryPendingOrderStore, RedisSchedulerState
from ..timeutil import KST
from ..trading_orders import ORDER_CONFIRM_CALLBACK, PendingOrder
from ..watchlist_repo import SqliteWatchlistRepo
from .test_scheduler import (
    FakeRedis,
    FakeWatchlistRepo,
    _resolved_broadcast_mock,
    _scored,
)

# 평일 장중. run_order_assist가 자체 검사를 또 하지만, 이 모듈이 검사하는 것은
# run_rule_triggered_proposal이 그 앞에서 자동 트리거를 끊는지다.
OPEN = datetime(2026, 8, 27, 10, 0, tzinfo=KST)  # 목요일
CLOSED = datetime(2026, 8, 27, 20, 0, tzinfo=KST)


class FakeNotifier:
    """send_text만 쓰는 최소 노티파이어. 보낸 것을 그대로 기록한다."""

    def __init__(self, *, enabled: bool = True, chat_id: str = "123", ok: bool = True):
        self.enabled = enabled
        self.chat_id = chat_id
        self._ok = ok
        self.sent: list[tuple[str, dict | None]] = []

    async def send_text(self, text, *, reply_markup=None):
        self.sent.append((text, reply_markup))
        return self._ok


class RecordingPendingOrderStore(InMemoryPendingOrderStore):
    """삭제 호출만 기록한다. 나머지는 인메모리 구현 그대로라 PendingOrderStore를 만족한다."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    async def delete(self, chat_id: str) -> None:
        self.deleted.append(chat_id)
        await super().delete(chat_id)


def _order(chat_id: str = "123") -> PendingOrder:
    return PendingOrder(
        chat_id=chat_id,
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=1,
        price=70000,
        created_at=OPEN,
        order_type="LIMIT",
        callback_token="tok-314",
    )


def _assist_stub(result, *, calls: list | None = None):
    async def _run(trigger, **kwargs):
        if calls is not None:
            calls.append((trigger, kwargs))
        if isinstance(result, Exception):
            raise result
        return result

    return _run


async def _state(mode: str) -> RedisSchedulerState:
    state = RedisSchedulerState(FakeRedis())
    await state.set_telegram_alert_mode(mode)
    return state


_MATCH = RuleMatch(rule_id=RULE_ID, stock="삼성전자", source="disclosure", urgency="critical")


# ---------------------------------------------------------------------------
# 문턱 — 여기서 막히면 제안 왕복을 쓰지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_match_runs_nothing():
    from ..scheduler import run_rule_triggered_proposal

    calls: list = []
    result = await run_rule_triggered_proposal(
        [],
        await _state("all"),
        notifier=FakeNotifier(),
        now_factory=lambda: OPEN,
        assist=_assist_stub(None, calls=calls),
    )

    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_alerts_off_never_creates_a_proposal(caplog):
    """알림을 끈 사용자에게 확정 버튼을 보낼 수는 없고, 승인을 안 보낼 수도 없다.

    두 요구가 양립하지 않으므로 제안 자체를 만들지 않는다. 조용히 대기 주문 슬롯만
    잡히면 사용자가 직접 친 /buy가 영문 모를 충돌로 막힌다.
    """
    from ..scheduler import run_rule_triggered_proposal

    calls: list = []
    with caplog.at_level(logging.INFO, logger="backend.scheduler"):
        result = await run_rule_triggered_proposal(
            [_MATCH],
            await _state("off"),
            notifier=FakeNotifier(),
            now_factory=lambda: OPEN,
            assist=_assist_stub(None, calls=calls),
        )

    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_market_closed_skips_silently():
    """감시는 24시간 돈다. 장 밖의 거부를 매번 알리면 밤새 '거부했습니다'가 쌓인다."""
    from ..scheduler import run_rule_triggered_proposal

    calls: list = []
    notifier = FakeNotifier()
    result = await run_rule_triggered_proposal(
        [_MATCH],
        await _state("all"),
        notifier=notifier,
        now_factory=lambda: CLOSED,
        assist=_assist_stub(None, calls=calls),
    )

    assert result is None
    assert calls == []
    assert notifier.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "notifier",
    [FakeNotifier(enabled=False), FakeNotifier(chat_id="")],
    ids=["disabled", "no_chat_id"],
)
async def test_no_delivery_path_means_no_proposal(notifier):
    """보낼 수 없는 대기 주문을 저장하면 슬롯만 잡고 사용자는 그 존재를 모른다."""
    from ..scheduler import run_rule_triggered_proposal

    calls: list = []
    result = await run_rule_triggered_proposal(
        [_MATCH],
        await _state("all"),
        notifier=notifier,
        now_factory=lambda: OPEN,
        assist=_assist_stub(None, calls=calls),
    )

    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_unreadable_alert_mode_blocks_the_trigger():
    """확인하지 못한 상태를 '허용'으로 읽지 않는다 — #299의 fail-closed와 같은 선이다."""
    from ..scheduler import run_rule_triggered_proposal

    class BrokenState:
        async def get_telegram_alert_mode(self):
            raise RuntimeError("redis down")

    calls: list = []
    result = await run_rule_triggered_proposal(
        [_MATCH],
        BrokenState(),  # type: ignore[arg-type]
        notifier=FakeNotifier(),
        now_factory=lambda: OPEN,
        assist=_assist_stub(None, calls=calls),
    )

    assert result is None
    assert calls == []


# ---------------------------------------------------------------------------
# 트리거 내용 — 무엇을 run_order_assist에 넘기는가
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_identifies_itself_as_a_scheduler_rule():
    """source·rule_id가 갈려야 수동 /advise와 자동 제안이 서로의 냉각을 잡아먹지 않는다."""
    from ..scheduler import run_rule_triggered_proposal

    calls: list = []
    store = RecordingPendingOrderStore()
    await run_rule_triggered_proposal(
        [_MATCH],
        await _state("urgent"),
        pending_orders=store,
        notifier=FakeNotifier(),
        now_factory=lambda: OPEN,
        assist=_assist_stub(
            OrderAssistResult(status="rejected", message="거부"), calls=calls
        ),
    )

    (trigger, kwargs), = calls
    assert trigger.source == "scheduler_rule"
    assert trigger.rule_id == RULE_ID
    assert trigger.stock == "삼성전자"
    assert trigger.chat_id == "123"
    assert kwargs["pending_orders"] is store


@pytest.mark.asyncio
async def test_trigger_signal_carries_no_numbers():
    """프롬프트 본문에 실린 수치는 도구 호출 없이도 환각 게이트를 통과한다 (#314).

    order_rules 쪽에도 같은 검사가 있지만, 여기서 한 번 더 보는 것은 배선이 어긋나
    신호 본문이 그대로 실려 나가는 경우를 잡기 위해서다.
    """
    from ..scheduler import run_rule_triggered_proposal

    calls: list = []
    await run_rule_triggered_proposal(
        [_MATCH],
        await _state("urgent"),
        pending_orders=RecordingPendingOrderStore(),
        notifier=FakeNotifier(),
        now_factory=lambda: OPEN,
        assist=_assist_stub(
            OrderAssistResult(status="rejected", message="거부"), calls=calls
        ),
    )

    (trigger, _), = calls
    assert trigger.trigger_signal
    assert not any(char.isdigit() for char in trigger.trigger_signal)


@pytest.mark.asyncio
async def test_only_one_proposal_per_cycle_and_it_is_the_most_urgent():
    """대기 주문 슬롯이 하나라 두 번째부터는 conflict로 끝난다 — 왕복만 버린다."""
    from ..scheduler import run_rule_triggered_proposal

    calls: list = []
    await run_rule_triggered_proposal(
        [
            RuleMatch(RULE_ID, "삼성전자", "news", "high"),
            RuleMatch(RULE_ID, "NAVER", "disclosure", "critical"),
        ],
        await _state("urgent"),
        pending_orders=RecordingPendingOrderStore(),
        notifier=FakeNotifier(),
        now_factory=lambda: OPEN,
        assist=_assist_stub(
            OrderAssistResult(status="rejected", message="거부"), calls=calls
        ),
    )

    assert len(calls) == 1
    assert calls[0][0].stock == "NAVER"


# ---------------------------------------------------------------------------
# 결과 전달
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_proposal_gets_the_existing_confirm_buttons():
    """자동 트리거라도 확정 버튼 없이는 주문이 나가지 않는다 — #299의 세 번째 원칙."""
    from ..scheduler import run_rule_triggered_proposal

    order = _order()
    notifier = FakeNotifier()
    await run_rule_triggered_proposal(
        [_MATCH],
        await _state("urgent"),
        pending_orders=RecordingPendingOrderStore(),
        notifier=notifier,
        now_factory=lambda: OPEN,
        assist=_assist_stub(
            OrderAssistResult(status="approved", message="제안 본문", order=order)
        ),
    )

    (text, markup), = notifier.sent
    assert "제안 본문" in text
    # 사용자가 요청한 적 없는 메시지다. 첫 줄이 그 사실을 말한다.
    assert "자동 제안" in text
    assert markup is not None
    buttons = markup["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"{ORDER_CONFIRM_CALLBACK}:tok-314"


@pytest.mark.asyncio
async def test_approval_is_sent_even_in_urgent_mode():
    """승인은 알림 모드로 걸러지지 않는다 — 이 시점엔 이미 대기 주문 슬롯이 잡혀 있다."""
    from ..scheduler import run_rule_triggered_proposal

    notifier = FakeNotifier()
    await run_rule_triggered_proposal(
        [_MATCH],
        await _state("urgent"),
        pending_orders=RecordingPendingOrderStore(),
        notifier=notifier,
        now_factory=lambda: OPEN,
        assist=_assist_stub(
            OrderAssistResult(status="approved", message="제안 본문", order=_order())
        ),
    )

    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_undelivered_prompt_clears_the_pending_order():
    """/advise와 같은 처리다 (#247). 프롬프트가 안 나갔으면 사용자는 슬롯의 존재를 모른다."""
    from ..scheduler import run_rule_triggered_proposal

    store = RecordingPendingOrderStore()
    await run_rule_triggered_proposal(
        [_MATCH],
        await _state("urgent"),
        pending_orders=store,
        notifier=FakeNotifier(ok=False),
        now_factory=lambda: OPEN,
        assist=_assist_stub(
            OrderAssistResult(status="approved", message="제안 본문", order=_order())
        ),
    )

    assert store.deleted == ["123"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "conflict"])
async def test_rejection_stays_in_the_log_in_urgent_mode(status):
    """사용자가 존재조차 모르는 시도의 결과다. 10분 주기로 보내면 소음이 된다."""
    from ..scheduler import run_rule_triggered_proposal

    notifier = FakeNotifier()
    await run_rule_triggered_proposal(
        [_MATCH],
        await _state("urgent"),
        pending_orders=RecordingPendingOrderStore(),
        notifier=notifier,
        now_factory=lambda: OPEN,
        assist=_assist_stub(OrderAssistResult(status=status, message="보류했어요")),
    )

    assert notifier.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "conflict"])
async def test_rejection_reaches_the_user_in_all_mode(status):
    from ..scheduler import run_rule_triggered_proposal

    notifier = FakeNotifier()
    await run_rule_triggered_proposal(
        [_MATCH],
        await _state("all"),
        pending_orders=RecordingPendingOrderStore(),
        notifier=notifier,
        now_factory=lambda: OPEN,
        assist=_assist_stub(OrderAssistResult(status=status, message="보류했어요")),
    )

    (text, markup), = notifier.sent
    assert "보류했어요" in text
    # 거부·충돌에는 확정 버튼이 없다. 누를 대기 주문이 없기 때문이다.
    assert markup is None


@pytest.mark.asyncio
async def test_assist_failure_does_not_break_the_monitoring_cycle(caplog):
    from ..scheduler import run_rule_triggered_proposal

    notifier = FakeNotifier()
    with caplog.at_level(logging.ERROR, logger="backend.scheduler"):
        result = await run_rule_triggered_proposal(
            [_MATCH],
            await _state("all"),
            pending_orders=RecordingPendingOrderStore(),
            notifier=notifier,
            now_factory=lambda: OPEN,
            assist=_assist_stub(RuntimeError("nat down")),
        )

    assert result is None
    assert notifier.sent == []
    assert any("자동 제안 실행 중 오류" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _monitor_signal — 판정만 하고 제안은 만들지 않는다
# ---------------------------------------------------------------------------


def _monitor_patches(monkeypatch, urgency: str):
    async def mock_run_mcp_tool(params, name, args):
        _ = params, name, args
        return "signal"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    async def mock_perform_analysis(*args, **kwargs):
        _ = args, kwargs
        return {"summary": "분석", "telegram_alert": False, "urgency": urgency}

    broadcast = asyncio.Future()
    broadcast.set_result(None)

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", lambda *a, **k: broadcast)


@pytest.mark.asyncio
async def test_monitor_signal_reports_a_rule_match(monkeypatch):
    from ..scheduler import SignalSource, _monitor_signal

    _monitor_patches(monkeypatch, "critical")
    source = SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")
    rule = OrderAssistRule(
        rule_id=RULE_ID, sources=frozenset({"news"}), urgency_levels=frozenset({"critical"})
    )

    match = await _monitor_signal(
        "삼성전자", source, cast(Session, object()), RedisSchedulerState(FakeRedis()), rule=rule
    )

    assert match == RuleMatch(RULE_ID, "삼성전자", "news", "critical")


@pytest.mark.asyncio
async def test_monitor_signal_reports_nothing_without_a_rule(monkeypatch):
    """룰이 꺼져 있거나 이 종목이 대상 밖이면 호출부가 rule=None을 넘긴다."""
    from ..scheduler import SignalSource, _monitor_signal

    _monitor_patches(monkeypatch, "critical")
    source = SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")

    match = await _monitor_signal(
        "삼성전자", source, cast(Session, object()), RedisSchedulerState(FakeRedis()), rule=None
    )

    assert match is None


# ---------------------------------------------------------------------------
# 감시 주기 배선 — 어떤 종목에 룰이 걸리는가
# ---------------------------------------------------------------------------


def _market_task_patches(monkeypatch, balance_text: str) -> list[RuleMatch]:
    """감시 주기 한 번을 태우고, 룰 대상으로 수집된 신호를 돌려준다."""
    from contextlib import asynccontextmanager

    from ..scheduler import SignalSource

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        _ = params
        return balance_text if name == "get_balance" else "signal"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    async def mock_perform_analysis(*args, **kwargs):
        _ = args, kwargs
        return {"summary": "분석", "telegram_alert": False, "urgency": "critical"}

    collected: list[RuleMatch] = []

    async def fake_trigger(matches, st, **kwargs):
        _ = st, kwargs
        collected.extend(matches)
        return None

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", lambda *a, **k: None)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())
    monkeypatch.setattr("backend.scheduler.run_rule_triggered_proposal", fake_trigger)
    monkeypatch.setattr(
        "backend.scheduler.load_rule",
        lambda: OrderAssistRule(
            rule_id=RULE_ID,
            sources=frozenset({"news"}),
            urgency_levels=frozenset({"critical"}),
        ),
    )
    return collected


@pytest.mark.asyncio
async def test_rule_covers_owned_and_watchlist_stocks(monkeypatch):
    from ..scheduler import monitor_market_task

    collected = _market_task_patches(
        monkeypatch, "[보유 종목 리스트]\n- 삼성전자 (005930) · 10주"
    )

    await monitor_market_task(watchlist_repo=cast(SqliteWatchlistRepo, FakeWatchlistRepo(["카카오"])))

    assert [m.stock for m in collected] == ["삼성전자", "카카오"]


@pytest.mark.asyncio
async def test_default_monitor_stocks_never_trigger_the_rule(monkeypatch):
    """보유·관심 종목이 둘 다 비면 감시는 기본 종목으로 떨어진다 (DEFAULT_MONITOR_STOCKS).

    그건 사용자가 고른 종목이 아니라 감시 공백을 메우는 값이다. 거기에 자동 제안을 걸면
    관심을 표한 적도 없는 종목의 확정 버튼이 사용자에게 뜬다 — 수동 /advise에는 없던
    종류의 사고다. 이 테스트가 잡는 mutation: in_rule_scope 검사를 빼고 감시 대상 전체에
    룰을 거는 회귀.
    """
    from ..scheduler import monitor_market_task

    collected = _market_task_patches(monkeypatch, "[보유 종목 리스트]\n")

    await monitor_market_task(watchlist_repo=cast(SqliteWatchlistRepo, FakeWatchlistRepo()))

    assert collected == []
