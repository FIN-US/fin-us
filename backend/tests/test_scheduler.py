import pytest
import asyncio
import json
import pathlib
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from ..main import app
from ..redis_state import RedisSchedulerState
from ..services import SignalScore
from .test_balance_parser import TRUNCATION_NOTES_BY_REASON


class SessionUsedUnexpectedly(BaseException):
    """UnusedSession이 실제로 불렸을 때 던지는 신호.

    ``Exception``이 아니라 ``BaseException``을 상속하는 이유가 이 클래스의 존재
    이유다 (PR #337 리뷰). 유일한 사용처인 :func:`_monitor_signal`은 본문 전체가
    ``except Exception``으로 감싸여 있어, 평범한 ``AssertionError``를 던지면 로그
    한 줄로 삼켜지고 테스트는 뒤쪽 단언이 어긋나서 **간접적으로만** red가 된다 —
    그러면 실패 메시지가 "세션을 썼다"가 아니라 "알림이 안 나갔다"로 나온다.
    ``BaseException``은 그 절을 지나가므로 원인이 그대로 올라온다.
    """


class UnusedSession:
    """세션을 건드리지 않는 경로에 넘기는 자리표시자 (#319).

    아래 _monitor_signal 테스트들은 perform_stock_analysis를 통째로 대체하므로 세션은
    한 번도 쓰이지 않는다. 예전에는 ``object()``를 넘겼는데, 그러면 "쓰이지 않는다"가
    아니라 "무엇이든 통과한다"가 되어 주입 지점의 계약이 이 호출들에서만 사라졌다.

    세 메서드는 ReportSession 계약을 만족하되 불리면 실패한다 — 대체가 풀려 실제
    저장 경로로 새는 날, 호출부의 ``except Exception``에 삼켜지지 않고 그대로
    올라온다(:class:`SessionUsedUnexpectedly` 참고).
    """

    def add(self, instance: object, /) -> None:
        raise SessionUsedUnexpectedly("이 경로는 세션을 쓰지 않아야 한다")

    def commit(self) -> None:
        raise SessionUsedUnexpectedly("이 경로는 세션을 쓰지 않아야 한다")

    def refresh(self, instance: object, /) -> None:
        raise SessionUsedUnexpectedly("이 경로는 세션을 쓰지 않아야 한다")


def _scored(is_significant: bool, score: int | None = None) -> SignalScore:
    """유의성 판정만 고정하는 SignalScore (#298).

    2차 필터는 bool이 아니라 SignalScore를 돌려준다. 점수·근거를 함께 검증하는
    테스트가 아니라면 판정만 지정하고 나머지는 "채점 정보 없음"(null)으로 둔다.
    """
    return SignalScore(
        score=score,
        reason=None,
        uncertainty=None,
        headline_scores=(),
        is_significant=is_significant,
    )


def _resolved_broadcast_mock() -> MagicMock:
    """resolve된 Future를 반환하는 manager.broadcast mock.

    이슈 #229로 Portfolio 동기화가 성공하면 실제로 await manager.broadcast(...)가
    호출된다. 반환 Future를 resolve하지 않으면 (과거에는 broadcast가 전혀 호출되지
    않아 무해했지만) 이제는 await가 영원히 멈춘다. 브로드캐스트 호출 자체를 검증하지
    않는 테스트들이 이 헬퍼를 쓴다.
    """
    mock = MagicMock(return_value=asyncio.Future())
    mock.return_value.set_result(None)
    return mock


class FakeWatchlistRepo:
    def __init__(self, stocks: list[str] | None = None):
        self._stocks = list(stocks or [])

    async def get_watchlist(self) -> list[str]:
        return list(self._stocks)


class FailingWatchlistRepo:
    async def get_watchlist(self) -> list[str]:
        raise RuntimeError("watchlist db unavailable")


class FakeCatalystRepo:
    def __init__(self, due_events=None):
        self.upserted = []
        self.due_events = list(due_events or [])
        self.marked = []

    async def upsert_events(self, events):
        self.upserted.extend(events)
        return list(events)

    async def list_due_for_notification(self, stock_names, *, today):
        self.due_call = (stock_names, today)
        return list(self.due_events)

    async def mark_notification_sent(self, event_id, *, days_until_event):
        self.marked.append((event_id, days_until_event))


class FakeCatalystNotifier:
    def __init__(self, result=True):
        self.result = result
        self.messages = []

    async def send_text(self, text, *, reply_markup=None):
        self.messages.append(text)
        return self.result

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

def test_scheduler_lifecycle(monkeypatch):
    """
    FastAPI 앱의 시작과 종료 시 스케줄러가 시작되고 중지되는지 확인합니다.
    """
    mock_start = MagicMock()
    mock_stop = MagicMock()
    
    # scheduler.py의 함수들을 모킹
    monkeypatch.setattr("backend.main.start_scheduler", mock_start)
    monkeypatch.setattr("backend.main.stop_scheduler", mock_stop)
    
    # TestClient의 컨텍스트 매니저를 사용하여 startup/shutdown 이벤트를 트리거
    with TestClient(app):
        # startup 이벤트 발생 후 start_scheduler가 호출되었어야 함
        mock_start.assert_called_once()
        
    # 컨텍스트를 빠져나오면 shutdown 이벤트 발생 후 stop_scheduler가 호출되었어야 함
    mock_stop.assert_called_once()


def test_default_signal_sources_include_news_and_disclosure():
    from ..scheduler import SIGNAL_SOURCES

    assert [(source.name, source.tool_name) for source in SIGNAL_SOURCES] == [
        ("news", "get_market_news"),
        ("disclosure", "get_disclosure_signal"),
    ]


def test_start_scheduler_runs_market_monitoring_immediately(monkeypatch):
    from .. import scheduler as scheduler_module

    added_jobs = []

    class FakeScheduler:
        running = False
        timezone = None

        def add_job(self, *args, **kwargs):
            added_jobs.append((args, kwargs))

        def start(self):
            self.running = True

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)

    scheduler_module.start_scheduler()

    market_job = next(kwargs for _args, kwargs in added_jobs if kwargs["id"] == "market_monitoring")
    assert market_job["next_run_time"] is not None


def test_start_scheduler_registers_weekday_morning_briefing(monkeypatch):
    from .. import scheduler as scheduler_module

    added_jobs = []

    class FakeScheduler:
        running = False
        timezone = None

        def add_job(self, *args, **kwargs):
            added_jobs.append((args, kwargs))

        def start(self):
            self.running = True

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)

    scheduler_module.start_scheduler()

    briefing_job = next(kwargs for _args, kwargs in added_jobs if kwargs["id"] == "morning_briefing")
    assert briefing_job["day_of_week"] == "mon-fri"
    assert briefing_job["hour"] == 8
    assert briefing_job["minute"] == 30


@pytest.mark.asyncio
async def test_morning_briefing_task_sends_telegram_message(monkeypatch):
    from ..scheduler import morning_briefing_task

    calls = []
    briefing = {
        "market_summary": "미국 증시 상승",
        "watchlist": ["삼성전자: 반도체 뉴스"],
        "trading_ideas": ["삼성전자 눌림목 관찰"],
        "catalysts": ["FOMC 의사록"],
    }

    async def fake_generate_morning_briefing(watchlist):
        calls.append(watchlist)
        return briefing

    mock_format = MagicMock(return_value="모닝 브리핑 메시지")
    mock_send = MagicMock(return_value=asyncio.Future())
    mock_send.return_value.set_result(True)

    monkeypatch.setattr("backend.scheduler.SqliteWatchlistRepo", lambda session_factory: FakeWatchlistRepo(["삼성전자"]))
    monkeypatch.setattr("backend.scheduler.generate_morning_briefing", fake_generate_morning_briefing)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.format_morning_briefing", mock_format)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_text", mock_send)

    # 수준을 명시해 redis를 열지 않는다. 인자 없이 부를 때 저장된 수준을 읽는지는
    # test_morning_briefing_reads_the_saved_level이 따로 고정한다 (#297 자가리뷰).
    await morning_briefing_task(level="beginner")

    assert calls == [["삼성전자"]]
    mock_format.assert_called_once_with(briefing)
    mock_send.assert_called_once_with("모닝 브리핑 메시지")


@pytest.mark.asyncio
async def test_ping_task_execution(monkeypatch):
    """
    ping_task가 실행될 때 WebSocket 브로드캐스트가 호출되는지 확인합니다.
    """
    from ..scheduler import ping_task
    
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)
    
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    
    # 비동기 함수 실행
    await ping_task()
    
    # 브로드캐스트가 호출되었는지 확인
    mock_broadcast.assert_called_once()
    args = mock_broadcast.call_args[0][0]
    assert args["type"] == "SYSTEM_PING"
    assert "Scheduler is alive" in args["message"]

@pytest.mark.asyncio
async def test_monitor_market_task_filtering(monkeypatch):
    """
    Redis 장애 시 fallback 경로에서도 뉴스가 새로운 경우에만 분석을 수행하는지 확인합니다.
    """
    from ..scheduler import SignalSource, monitor_market_task, last_analyzed_news_cache
    
    # 캐시 초기화
    last_analyzed_news_cache.clear()
    
    # 모킹
    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주\n- SK하이닉스 (000660): 2주\n- 현대차 (005380): 1주"
        return f"Latest news for {args['stock_name']}"
    
    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = source
        return _scored(current != last)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked", "details": {"decision": "BUY"}})
    
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    @asynccontextmanager
    async def unavailable_redis_state():
        raise ConnectionError("redis unavailable")
        yield
    
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    # 이 테스트의 관심사는 뉴스 필터링이지 Portfolio 동기화가 아니다.
    # 동기화를 건너뜀(None)으로 고정하면 PORTFOLIO_UPDATE가 나가지 않아
    # 기존 기대값이 그대로 유효하고, 실 DB 상태에도 좌우되지 않는다.
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    def _agent_analysis_calls():
        return [
            c for c in mock_broadcast.call_args_list
            if c.args[0].get("type") == "AGENT_ANALYSIS"
        ]

    # 1. 첫 번째 실행: 뉴스가 처음이므로 분석 수행
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())
    assert mock_perform_analysis.call_count == 3 # 삼성전자, SK하이닉스, 현대차
    assert len(_agent_analysis_calls()) == 3

    # 2. 두 번째 실행: 뉴스가 동일하므로 분석 스킵
    mock_perform_analysis.reset_mock()
    mock_broadcast.reset_mock()
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())
    assert mock_perform_analysis.call_count == 0
    assert len(_agent_analysis_calls()) == 0

    # 3. 세 번째 실행: 뉴스가 변경된 종목이 있으면 해당 종목만 분석
    async def mock_run_mcp_tool_changed(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주\n- SK하이닉스 (000660): 2주\n- 현대차 (005380): 1주"
        if args.get('stock_name') == "삼성전자":
            return "NEW NEWS for Samsung"
        return f"Latest news for {args.get('stock_name')}"

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool_changed)
    mock_perform_analysis.reset_mock()
    mock_broadcast.reset_mock()
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())
    assert mock_perform_analysis.call_count == 1
    assert len(_agent_analysis_calls()) == 1
    assert mock_perform_analysis.call_args[0][0] == "삼성전자"


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.sets: dict[str, set] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, *, ex=None, nx=False):
        _ = ex
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def exists(self, key):
        return 1 if key in self.store else 0

    async def eval(self, script, numkeys, key, token):
        _ = script, numkeys
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0

    async def sadd(self, key, *values):
        if key not in self.sets:
            self.sets[key] = set()
        added = sum(1 for v in values if v not in self.sets[key])
        self.sets[key].update(values)
        return added

    async def srem(self, key, *values):
        if key not in self.sets:
            return 0
        removed = sum(1 for v in values if v in self.sets[key])
        self.sets[key].difference_update(values)
        return removed

    async def smembers(self, key):
        return self.sets.get(key, set())


@pytest.mark.asyncio
async def test_monitor_signal_sends_telegram_for_urgent_analysis(monkeypatch):
    from ..scheduler import SignalSource, _monitor_signal

    state = RedisSchedulerState(FakeRedis())
    source = SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")

    async def mock_run_mcp_tool(params, name, args):
        _ = params, name, args
        return "urgent signal"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    async def mock_perform_analysis(*args, **kwargs):
        _ = args, kwargs
        return {
            "summary": "긴급",
            "details": {"decision": "HOLD"},
            "telegram_alert": True,
            "urgency": "high",
        }

    mock_telegram = MagicMock(return_value=asyncio.Future())
    mock_telegram.return_value.set_result(True)
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", mock_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, UnusedSession(), state)

    mock_telegram.assert_called_once()
    mock_broadcast.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_signal_sends_telegram_for_all_mode_analysis(monkeypatch):
    from ..scheduler import SignalSource, _monitor_signal

    state = RedisSchedulerState(FakeRedis())
    await state.set_telegram_alert_mode("all")
    source = SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")

    async def mock_run_mcp_tool(params, name, args):
        _ = params, name, args
        return "normal signal"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    async def mock_perform_analysis(*args, **kwargs):
        _ = args, kwargs
        return {
            "summary": "일반 분석",
            "details": {"decision": "HOLD"},
            "telegram_alert": False,
            "urgency": "normal",
        }

    mock_telegram = MagicMock(return_value=asyncio.Future())
    mock_telegram.return_value.set_result(True)
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", mock_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, UnusedSession(), state)

    mock_telegram.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_signal_skips_telegram_when_alert_mode_off(monkeypatch):
    from ..scheduler import SignalSource, _monitor_signal

    state = RedisSchedulerState(FakeRedis())
    await state.set_telegram_alert_mode("off")
    source = SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")

    async def mock_run_mcp_tool(params, name, args):
        _ = params, name, args
        return "urgent signal"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    async def mock_perform_analysis(*args, **kwargs):
        _ = args, kwargs
        return {
            "summary": "긴급",
            "details": {"decision": "HOLD"},
            "telegram_alert": True,
            "urgency": "critical",
        }

    mock_telegram = MagicMock(return_value=asyncio.Future())
    mock_telegram.return_value.set_result(True)
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", mock_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, UnusedSession(), state)

    mock_telegram.assert_not_called()
    mock_broadcast.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_signal_keeps_websocket_when_telegram_fails(monkeypatch):
    from ..scheduler import SignalSource, _monitor_signal

    state = RedisSchedulerState(FakeRedis())
    source = SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")

    async def mock_run_mcp_tool(params, name, args):
        _ = params, name, args
        return "urgent signal"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    async def mock_perform_analysis(*args, **kwargs):
        _ = args, kwargs
        return {
            "summary": "긴급",
            "details": {"decision": "HOLD"},
            "telegram_alert": True,
            "urgency": "critical",
        }

    async def failing_telegram(*args, **kwargs):
        _ = args, kwargs
        raise RuntimeError("telegram down")

    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", failing_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, UnusedSession(), state)

    mock_broadcast.assert_called_once()


@pytest.mark.asyncio
async def test_monitor_market_task_uses_default_stocks_when_balance_empty(monkeypatch):
    """
    보유 종목이 없으면 기본 감시 종목 4개를 대상으로 모니터링합니다.
    """
    from ..scheduler import DEFAULT_MONITOR_STOCKS, SignalSource, monitor_market_task, last_analyzed_news_cache

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    last_analyzed_news_cache.clear()
    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n보유 종목이 없습니다."
        monitored_stocks.append(args["stock_name"])
        return f"Latest news for {args['stock_name']}"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(False)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked"})
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    # 이 테스트의 관심사는 기본 감시 종목 사용 여부이지 Portfolio 동기화가 아니다.
    # 동기화를 건너뜀(None)으로 고정하면 PORTFOLIO_UPDATE가 나가지 않아
    # 기존 기대값이 그대로 유효하고, 실 DB 상태에도 좌우되지 않는다.
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert len(DEFAULT_MONITOR_STOCKS) == 4
    assert monitored_stocks == DEFAULT_MONITOR_STOCKS
    assert mock_perform_analysis.call_count == 0
    # 뉴스 분석이 없고 Portfolio 동기화도 건너뛰므로 브로드캐스트가 전혀 없다.
    assert mock_broadcast.call_count == 0


@pytest.mark.asyncio
async def test_monitor_market_task_uses_redis_hash_to_skip_duplicate_news(monkeypatch):
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "Latest news for Samsung"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked"})
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    # 이 테스트의 관심사는 Redis 해시로 뉴스 중복을 걸러내는지이지 Portfolio 동기화가
    # 아니다. 동기화를 건너뜀(None)으로 고정하면 PORTFOLIO_UPDATE가 나가지 않아
    # AGENT_ANALYSIS만 남는다.
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert mock_perform_analysis.call_count == 1
    analysis_calls = [
        c for c in mock_broadcast.call_args_list
        if c.args[0].get("type") == "AGENT_ANALYSIS"
    ]
    assert len(analysis_calls) == 1


@pytest.mark.asyncio
async def test_monitor_market_task_processes_multiple_signal_sources_independently(monkeypatch):
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())
    sources = [
        SignalSource(name="news", mcp_params=object(), tool_name="get_market_news"),
        SignalSource(name="sns", mcp_params=object(), tool_name="get_stock_mentions"),
    ]

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return f"{name}: {args['stock_name']}"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked"})
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.SIGNAL_SOURCES", sources)
    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert mock_perform_analysis.call_count == 2
    assert [call.kwargs["trigger_source"] for call in mock_perform_analysis.call_args_list] == ["news", "sns"]
    assert [call.kwargs["trigger_signal"] for call in mock_perform_analysis.call_args_list] == [
        "get_market_news: 삼성전자",
        "get_stock_mentions: 삼성전자",
    ]
    # PORTFOLIO_UPDATE(이슈 #229)에는 "source" 키가 없으므로 AGENT_ANALYSIS 브로드캐스트만
    # 걸러서 검사한다. 잔고 응답에 마커가 있고 잘리지 않아 Portfolio 동기화도 성공하지만
    # (mock되지 않음), 이 테스트의 관심사는 신호 소스별 분석 브로드캐스트 순서다.
    analysis_broadcasts = [
        call.args[0] for call in mock_broadcast.call_args_list
        if call.args[0].get("type") == "AGENT_ANALYSIS"
    ]
    assert [payload["source"] for payload in analysis_broadcasts] == ["news", "sns"]


@pytest.mark.asyncio
async def test_agent_analysis_broadcast_carries_no_analysis_body(monkeypatch):
    """#256: AGENT_ANALYSIS는 갱신 신호만 싣고 분석 전문은 싣지 않습니다.

    WebSocket에는 인증이 없다(#266). Origin 허용목록 검사(#256)로 브라우저발 CSWSH는 막지만
    Origin 헤더를 보내지 않는 비브라우저 클라이언트는 여전히 붙을 수 있으므로,
    PORTFOLIO_UPDATE(#229)와 같이 신호만 보내고 클라이언트가 /api/v1/db/reports를
    재조회하게 한다.

    이 테스트가 잡는 mutation: payload에 "data": analysis_data를 되살리는 회귀.
    키 이름만 보지 않고 분석 본문 문자열이 payload 어디에도 없는지까지 확인해,
    "content" 등 다른 키로 같은 내용을 실어 보내는 변형도 함께 잡는다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())
    secret = "이 문장은 WebSocket으로 나가면 안 됩니다"

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return f"{name}: {args['stock_name']}"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": secret})
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    # 텔레그램 알림에는 분석 전문이 그대로 가야 한다 — 인증된 채널이고, 이 이슈가 좁힌
    # 것은 WS payload뿐이다. 실제 전송만 막고 인자는 아래에서 확인한다.
    mock_alert = MagicMock(return_value=asyncio.Future())
    mock_alert.return_value.set_result(None)
    monkeypatch.setattr("backend.scheduler._send_telegram_alert_if_needed", mock_alert)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    analysis_broadcasts = [
        call.args[0] for call in mock_broadcast.call_args_list
        if call.args[0].get("type") == "AGENT_ANALYSIS"
    ]
    assert len(analysis_broadcasts) == 1
    payload = analysis_broadcasts[0]

    assert payload == {
        "type": "AGENT_ANALYSIS",
        "stock": "삼성전자",
        "source": "news",
        "reason": "significant_change_detected",
    }
    assert secret not in json.dumps(payload, ensure_ascii=False)

    # 분석 전문 자체는 계속 만들어져 텔레그램 알림으로 전달된다. 이 단언이 없으면
    # perform_stock_analysis 호출을 통째로 지워도 위 단언들이 그대로 통과한다.
    assert mock_alert.call_args.args[2] == {"summary": secret}


@pytest.mark.asyncio
async def test_monitor_market_task_skips_when_scheduler_lock_is_held(monkeypatch):
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())
    await state.acquire_scheduler_lock()

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    mock_run_mcp_tool = MagicMock(return_value=asyncio.Future())
    mock_run_mcp_tool.return_value.set_result("[보유 종목 리스트]\n- 삼성전자 (005930): 10주")

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    mock_run_mcp_tool.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_monitor_market_task_runs_once_with_scheduler_lock(monkeypatch):
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        _ = params, args
        await asyncio.sleep(0)
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "Latest news for Samsung"

    async def mock_score_signal(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return _scored(True)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked"})
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    # 이 테스트의 관심사는 스케줄러 락에 의한 동시 실행 방지이지 Portfolio 동기화가
    # 아니다. 동기화를 건너뜀(None)으로 고정하면 PORTFOLIO_UPDATE가 나가지 않아
    # AGENT_ANALYSIS만 남는다.
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    await asyncio.gather(
        monitor_market_task(watchlist_repo=FakeWatchlistRepo()),
        monitor_market_task(watchlist_repo=FakeWatchlistRepo()),
    )

    assert mock_perform_analysis.call_count == 1
    # 스케줄러 락으로 실제 실행되는 것은 한 쪽뿐이다.
    analysis_calls = [
        c for c in mock_broadcast.call_args_list
        if c.args[0].get("type") == "AGENT_ANALYSIS"
    ]
    assert len(analysis_calls) == 1


@pytest.mark.asyncio
async def test_monitor_market_task_includes_watchlist_stocks(monkeypatch):
    """관심 종목이 있으면 보유 종목과 합쳐서 모니터링합니다."""
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        monitored_stocks.append(args["stock_name"])
        return f"news for {args['stock_name']}"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["NAVER"]))

    assert "삼성전자" in monitored_stocks
    assert "NAVER" in monitored_stocks


@pytest.mark.asyncio
async def test_monitor_market_task_continues_owned_stock_when_watchlist_fails(monkeypatch):
    """관심 종목 조회 실패가 보유 종목 모니터링까지 막지 않습니다."""
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        monitored_stocks.append(args["stock_name"])
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FailingWatchlistRepo())

    assert monitored_stocks == ["삼성전자"]


@pytest.mark.asyncio
async def test_monitor_market_task_deduplicates_watchlist_and_owned_stocks(monkeypatch):
    """보유 종목과 관심 종목이 겹쳐도 중복 모니터링하지 않습니다."""
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        monitored_stocks.append(args["stock_name"])
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["삼성전자"]))

    assert monitored_stocks.count("삼성전자") == 1


@pytest.mark.asyncio
async def test_monitor_market_task_uses_default_stocks_only_when_both_empty(monkeypatch):
    """보유 종목도 관심 종목도 없을 때만 기본 종목으로 fallback합니다."""
    from ..scheduler import DEFAULT_MONITOR_STOCKS, SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n보유 종목이 없습니다."
        monitored_stocks.append(args["stock_name"])
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert monitored_stocks == DEFAULT_MONITOR_STOCKS


@pytest.mark.asyncio
async def test_monitor_market_task_watchlist_alone_skips_default_fallback(monkeypatch):
    """보유 종목은 없어도 관심 종목이 있으면 기본 종목을 사용하지 않습니다."""
    from ..scheduler import DEFAULT_MONITOR_STOCKS, SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n보유 종목이 없습니다."
        monitored_stocks.append(args["stock_name"])
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

    assert monitored_stocks == ["카카오"]
    for default_stock in DEFAULT_MONITOR_STOCKS:
        assert default_stock not in monitored_stocks


@pytest.mark.asyncio
async def test_catalyst_calendar_task_collects_disclosure_events_for_watchlist(monkeypatch):
    """관심 종목의 DART signal을 촉매 이벤트로 저장합니다."""
    from ..scheduler import catalyst_calendar_task

    repo = FakeCatalystRepo()
    notifier = FakeCatalystNotifier()
    calls = []

    async def mock_run_mcp_tool(params, name, args):
        calls.append((name, args))
        return "\n".join(
            [
                "[최신 공시]",
                "- 2026-01-28 | 분기보고서 접수 | 접수번호 202601280001",
                "- 2026-01-29 | 현금ㆍ현물배당 결정 | 접수번호 202601290001",
                "- 2026-01-30 | 정기주주총회결과 | 접수번호 202601300001",
            ]
        )

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)

    await catalyst_calendar_task(
        watchlist_repo=FakeWatchlistRepo(["삼성전자"]),
        catalyst_repo=repo,
        notifier=notifier,
        today_factory=lambda: date(2026, 1, 28),
        use_redis_lock=False,
    )

    assert calls == [("get_disclosure_signal", {"stock_name": "삼성전자"})]
    assert [(event.event_type, event.event_date, event.description) for event in repo.upserted] == [
        ("earnings", date(2026, 1, 28), "분기보고서 접수"),
        ("dividend", date(2026, 1, 29), "현금ㆍ현물배당 결정"),
        ("agm", date(2026, 1, 30), "정기주주총회결과"),
    ]


@pytest.mark.asyncio
async def test_catalyst_calendar_task_sends_and_marks_d1_d0_alerts(monkeypatch):
    """D-1/D-0 촉매 이벤트 알림을 보내고 성공한 알림만 발송 처리합니다."""
    from ..scheduler import catalyst_calendar_task

    repo = FakeCatalystRepo(
        [
            SimpleNamespace(
                id=1,
                stock_name="삼성전자",
                event_type="earnings",
                event_date=date(2026, 1, 28),
                description="분기 실적 발표",
                days_until_event=0,
            ),
            SimpleNamespace(
                id=2,
                stock_name="NAVER",
                event_type="agm",
                event_date=date(2026, 1, 29),
                description="정기 주주총회",
                days_until_event=1,
            ),
        ]
    )
    notifier = FakeCatalystNotifier()

    async def mock_run_mcp_tool(params, name, args):
        return "[최신 공시]\n- 최근 공시가 없습니다."

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)

    await catalyst_calendar_task(
        watchlist_repo=FakeWatchlistRepo(["삼성전자", "NAVER"]),
        catalyst_repo=repo,
        notifier=notifier,
        today_factory=lambda: date(2026, 1, 28),
        use_redis_lock=False,
    )

    assert repo.due_call == (["삼성전자", "NAVER"], date(2026, 1, 28))
    assert len(notifier.messages) == 2
    assert "D-Day" in notifier.messages[0]
    assert "삼성전자" in notifier.messages[0]
    assert "D-1" in notifier.messages[1]
    assert "NAVER" in notifier.messages[1]
    assert repo.marked == [(1, 0), (2, 1)]


@pytest.mark.asyncio
async def test_catalyst_calendar_task_does_not_mark_failed_telegram_alert(monkeypatch):
    """Telegram 전송 실패 시 같은 촉매 이벤트를 재시도할 수 있게 남깁니다."""
    from ..scheduler import catalyst_calendar_task

    repo = FakeCatalystRepo(
        [
            SimpleNamespace(
                id=3,
                stock_name="삼성전자",
                event_type="earnings",
                event_date=date(2026, 1, 29),
                description="분기 실적 발표",
                days_until_event=1,
            )
        ]
    )

    async def mock_run_mcp_tool(params, name, args):
        return "[최신 공시]\n- 최근 공시가 없습니다."

    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)

    await catalyst_calendar_task(
        watchlist_repo=FakeWatchlistRepo(["삼성전자"]),
        catalyst_repo=repo,
        notifier=FakeCatalystNotifier(result=False),
        today_factory=lambda: date(2026, 1, 28),
        use_redis_lock=False,
    )

    assert repo.marked == []


def test_start_scheduler_registers_catalyst_calendar_job(monkeypatch):
    from .. import scheduler as scheduler_module

    added_jobs = []

    class FakeScheduler:
        running = False
        timezone = None

        def add_job(self, *args, **kwargs):
            added_jobs.append((args, kwargs))

        def start(self):
            self.running = True

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "scheduler", fake_scheduler)

    scheduler_module.start_scheduler()

    job_ids = [kwargs["id"] for _args, kwargs in added_jobs]
    assert "catalyst_calendar" in job_ids


# mcp-trading/balance.js의 잘림 안내 문구는 test_balance_parser.py가 이미 사유별로
# 재현해 두었으므로(TRUNCATION_NOTES_BY_REASON), 여기서 다시 베끼지 않고 재사용한다.
# 같은 JS 리터럴이 Python 두 곳에 복사되면 한쪽만 갱신되는 드리프트가 생긴다(이슈 #136).
# 이 문구가 [보유 종목 리스트] 뒤에 그대로 붙어 오는 것이 스케줄러가 받는 형태다.
TRUNCATED_BALANCE_TEXT = (
    "[보유 종목 리스트]\n"
    "- 삼성전자 (005930): 10주"
    + TRUNCATION_NOTES_BY_REASON["max_pages"]
)


@pytest.mark.asyncio
async def test_monitor_market_task_logs_warning_when_balance_truncated(monkeypatch, caplog):
    """잔고 연속조회가 잘리면 스케줄러가 경고 로그를 남깁니다 (이슈 #136).

    이 테스트가 잡는 mutation: _monitor_market_task에서 is_balance_truncated() 호출과
    logger.warning()을 통째로 빠뜨리거나, 조건을 뒤집는(예: `if not is_balance_truncated(...)`)
    변경. origin/main에는 이 경고 자체가 없으므로 origin/main 기준으로는 반드시 실패한다.
    """
    import logging
    from .. import scheduler as scheduler_module
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return TRUNCATED_BALANCE_TEXT
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
        await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert "잔고 연속조회가 잘려" in caplog.text


@pytest.mark.asyncio
async def test_monitor_market_task_no_warning_when_balance_not_truncated(monkeypatch, caplog):
    """정상(잘리지 않은) 잔고 응답에서는 매 10분 주기마다 경고가 울리지 않습니다 (이슈 #136).

    이 테스트가 잡는 mutation: is_balance_truncated()가 항상 True를 반환하도록 뒤집히거나,
    logger.warning()이 조건 없이 항상 호출되는 회귀("늑대가 왔다" 오탐/알림 피로).
    """
    import logging
    from .. import scheduler as scheduler_module
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        monitored_stocks.append(args["stock_name"])
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
        await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert "잔고 연속조회가 잘려" not in caplog.text
    assert monitored_stocks == ["삼성전자"]  # 정상 경로를 끝까지 지났음을 함께 고정


@pytest.mark.asyncio
async def test_monitor_market_task_still_watches_stocks_returned_despite_truncation(monkeypatch):
    """잔고 연속조회가 잘려도, 잘리기 전에 이미 확보한 보유 종목은 계속 감시합니다 (이슈 #136).

    잘림은 감시를 "성능 저하(degrade)"시켜야지 "중단(disable)"시켜서는 안 된다는
    수용 기준을 고정한다. 이 테스트가 잡는 mutation: 잘림 감지 시 owned_stocks를
    비우거나 stocks_to_monitor 조립을 통째로 건너뛰는 회귀.
    """
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return TRUNCATED_BALANCE_TEXT
        monitored_stocks.append(args["stock_name"])
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert monitored_stocks == ["삼성전자"]


@pytest.fixture(autouse=True)
def reset_balance_failure_streak(monkeypatch):
    """모듈 전역 카운터가 테스트 간에 새지 않도록 매 테스트 시작 시 0으로 고정한다.

    시세 조회 카운터(#196)도 함께 고정한다. 잔고 카운터와 같은 모듈 전역이고, 억제
    조건이 "첫 실패 / 원인 변경 / 6회마다"라 앞 테스트가 남긴 값에 따라 같은 코드가
    로그를 남기기도 하고 안 남기기도 한다.
    """
    monkeypatch.setattr("backend.scheduler._balance_failure_streak", 0)
    monkeypatch.setattr("backend.scheduler._last_balance_error", None)
    monkeypatch.setattr("backend.scheduler._quote_failure_streak", 0)
    monkeypatch.setattr("backend.scheduler._last_quote_error", None)


def _make_balance_failure_mocks(monkeypatch, mock_run_mcp_tool_fn):
    """fake_redis_state / SIGNAL_SOURCES / check_significance / manager.broadcast 패치 헬퍼.

    이 PR(#185)에서 추가한 잔고 조회 실패 관련 테스트에서 반복되는 셋업 4종을 공통화한다.
    mock_run_mcp_tool_fn만 테스트별로 달라지므로 매개변수로 받는다.
    """
    from ..scheduler import SignalSource

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    async def tool_dispatch(params, name, args):
        # 시세 조회(#196)는 잔고와 같은 KIS 장애를 함께 겪는다고 본다. 이 헬퍼를 쓰는
        # 테스트들은 "잔고 장애 중의 로그 억제"(#185)를 재는데, 시세 도구를 그대로 두면
        # 각 테스트의 목이 그 이름에도 "news"를 돌려주고, 그 문자열이 실현손익 리포트
        # 계약을 어겨 매 주기 error가 하나씩 더 쌓인다 — 세는 대상이 오염된다.
        if name == "get_balance_rlz_pl":
            raise RuntimeError("KIS 시세 조회 실패")
        return await mock_run_mcp_tool_fn(params, name, args)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", tool_dispatch)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    return state


@pytest.mark.asyncio
async def test_monitor_market_task_still_monitors_watchlist_when_balance_raises(monkeypatch):
    """get_balance가 예외를 던져도 관심 종목의 뉴스·공시 감시는 계속됩니다 (이슈 #185).

    KIS(mcp-trading) 장애가 그와 무관한 SIGNAL_SOURCES(mcp-news/mcp-dart) 감시까지
    번지면 안 된다는 수용 기준을 고정한다. 이 테스트가 잡는 mutation: get_balance를
    감싸는 자체 try/except를 제거해 예외가 태스크 전체 except로 튀는 회귀(그러면
    _monitor_signal이 한 번도 불리지 않아 monitored_stocks가 비게 된다).
    """
    from ..scheduler import monitor_market_task

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            raise RuntimeError("KIS 잔고 조회 실패")
        monitored_stocks.append(args["stock_name"])
        return "news"

    _make_balance_failure_mocks(monkeypatch, mock_run_mcp_tool)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

    assert monitored_stocks == ["카카오"]


@pytest.mark.asyncio
async def test_monitor_market_task_skips_when_balance_fails_and_no_watchlist(monkeypatch):
    """get_balance가 예외를 던지고 관심 종목도 없으면 이번 주기 감시를 건너뜁니다 (이슈 #185, PR #190).

    보유 종목이 "없는" 게 아니라 "모르는" 상태이므로 DEFAULT_MONITOR_STOCKS 폴백을 쓰면
    사용자와 무관한 종목의 알림이 나갈 수 있다. 이 테스트가 잡는 mutation: balance_ok
    체크를 제거해 장애 중에도 DEFAULT_MONITOR_STOCKS 폴백이 걸리는 회귀.
    """
    from ..scheduler import monitor_market_task

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            raise RuntimeError("KIS 잔고 조회 실패")
        monitored_stocks.append(args["stock_name"])
        return "news"

    _make_balance_failure_mocks(monkeypatch, mock_run_mcp_tool)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert monitored_stocks == []


@pytest.mark.asyncio
async def test_monitor_market_task_logs_balance_failure_once_per_outage(monkeypatch, caplog):
    """잔고 조회가 계속 실패해도 error 로그는 장애당 1회만 남고, 복구 시 집계됩니다 (이슈 #185).

    10분 주기 잡이므로 매 주기 error가 반복되면 알림 피로로 진짜 장애가 묻힌다는
    수용 기준을 고정한다. 이 테스트가 잡는 mutation: 연속 실패 카운터를 없애고 매번
    logger.error를 부르는 회귀, 또는 복구 시 카운터를 리셋하지 않아 다음 장애의 첫
    실패가 error로 보고되지 않는 회귀.
    """
    import logging
    from .. import scheduler as scheduler_module
    from ..scheduler import monitor_market_task

    balance_should_fail = True

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            if balance_should_fail:
                raise RuntimeError("KIS 잔고 조회 실패")
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "news"

    _make_balance_failure_mocks(monkeypatch, mock_run_mcp_tool)

    with caplog.at_level(logging.DEBUG, logger=scheduler_module.logger.name):
        # 같은 장애가 3주기 연속되는 상황
        for _ in range(3):
            await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1

        # 복구 주기: 누적 실패 횟수를 집계해 알리고 카운터를 리셋한다
        balance_should_fail = False
        await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

        # 두 번째 장애: 카운터가 리셋됐다면 첫 실패가 다시 error로 남아야 한다
        balance_should_fail = True
        await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

    assert "잔고 조회가 복구되었습니다" in caplog.text
    assert "3회" in caplog.text

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    # 두 번째 장애의 error가 "1회 연속"이어야 카운터가 실제로 리셋된 것이다.
    # 개수만 세면 부족하다: 리셋이 사라져도 _last_balance_error 리셋이 원인 변경으로
    # 오인돼 error가 그대로 2건 남기 때문에, 누적 횟수까지 함께 고정한다.
    assert "1회 연속" in errors[1].getMessage()
    # 장애당 몇 건 안 남는 error이므로 스택 트레이스가 함께 실려야 진단이 가능하다.
    assert all(r.exc_info is not None for r in errors)


@pytest.mark.asyncio
async def test_monitor_market_task_logs_error_when_failure_cause_changes(monkeypatch, caplog):
    """잔고 조회 실패 원인이 바뀌면 카운터에 관계없이 다시 error를 남깁니다 (PR #190).

    타임아웃 → 인증 실패 등 원인 변경은 운영 대응이 달라지므로 즉시 알려야 한다는
    수용 기준을 고정한다. 이 테스트가 잡는 mutation: signature != _last_balance_error
    조건을 제거해 원인이 바뀌어도 debug만 남기는 회귀.
    """
    import logging
    from .. import scheduler as scheduler_module
    from ..scheduler import monitor_market_task

    call_count = [0]

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("타임아웃")
            raise ValueError("인증 실패")
        return "news"

    _make_balance_failure_mocks(monkeypatch, mock_run_mcp_tool)

    with caplog.at_level(logging.DEBUG, logger=scheduler_module.logger.name):
        # 1차 실패: RuntimeError → error
        await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))
        # 2차 실패: ValueError (원인 변경) → error 다시 발생해야 한다
        await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2


@pytest.mark.asyncio
async def test_monitor_market_task_reescalates_error_after_six_cycles(monkeypatch, caplog):
    """잔고 조회 실패가 6회 연속되면 1시간 주기로 다시 error를 남깁니다 (PR #190).

    알림 피로 방지와 "아직 장애 중" 신호의 균형을 고정한다. 이 테스트가 잡는
    mutation: _balance_failure_streak % 6 == 0 조건을 제거해 6회차 이후 error가
    완전히 사라지는 회귀.
    """
    import logging
    from .. import scheduler as scheduler_module
    from ..scheduler import monitor_market_task

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            raise RuntimeError("KIS 장애")
        return "news"

    _make_balance_failure_mocks(monkeypatch, mock_run_mcp_tool)

    with caplog.at_level(logging.DEBUG, logger=scheduler_module.logger.name):
        for _ in range(6):
            await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    # streak=1 → error, streak=2~5 → debug, streak=6 (% 6 == 0) → error
    assert len(errors) == 2


# ---------------------------------------------------------------------------
# Portfolio 동기화 테스트 (이슈 #122)
# ---------------------------------------------------------------------------

def _make_balance_text(*holdings):
    """(name, code, qty, avg_price) 튜플 목록으로 get_balance 텍스트 픽스처를 합성합니다.

    mcp-trading/balance.js의 formatBalanceReport()가 생성하는 형식을 그대로 재현합니다.
    formatAmount(pchs_avg_pric) = toLocaleString("ko-KR"): 정수는 "N,NNN원",
    소수는 "N,NNN.NN원" 형태. avg_price가 정수가 아니면 소수점을 그대로 보존한다.
    mcp-trading/tests/balance.test.js가 "평단가 66,666.67원"을 단언하므로 소수 케이스도
    픽스처가 통과시켜야 한다(Critical 1 참고).
    """
    lines = []
    for name, code, qty, avg_price in holdings:
        # formatAmount(toLocaleString("ko-KR")): 정수면 정수 포맷, 소수면 소수 포맷
        if avg_price == int(avg_price):
            avg_str = f"{int(avg_price):,}"
        else:
            avg_str = f"{avg_price:,}"
        evlu = int(avg_price * qty)
        lines.append(
            f"- {name} ({code}) · {qty}주\n"
            f"  평단가 {avg_str}원 → 평가금액 {evlu:,}원\n"
            f"  손익 +0원 · 수익률 ⚪ 0.00%"
        )
    stock_list = "\n\n".join(lines) if lines else "보유 종목이 없습니다."
    return (
        "[계좌 잔고 현황]\n"
        "- 총 평가금액: 0원\n"
        "\n"
        f"[보유 종목 리스트]\n{stock_list}"
    )


@pytest.fixture(name="portfolio_session")
def portfolio_session_fixture():
    """Portfolio 동기화 단위 테스트용 인메모리 SQLite 세션."""
    from sqlmodel import SQLModel, create_engine
    from sqlmodel.pool import StaticPool
    from sqlmodel import Session as _Session
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    with _Session(eng) as session:
        yield session


def test_parse_balance_holdings_extracts_name_code_qty_avg():
    """_parse_balance_holdings가 이름·코드·수량·평단가를 올바르게 파싱합니다.

    이 테스트가 잡는 mutation: _QTY_RE 또는 _AVG_PRICE_RE를 제거해
    quantity=0 또는 avg_price=0.0만 반환하는 회귀.
    """
    from ..scheduler import _parse_balance_holdings

    text = _make_balance_text(
        ("삼성전자", "005930", 10, 70000),
        ("SK하이닉스", "000660", 5, 200000),
    )
    holdings = _parse_balance_holdings(text)

    assert len(holdings) == 2
    assert holdings[0].name == "삼성전자"
    assert holdings[0].code == "005930"
    assert holdings[0].quantity == 10
    assert holdings[0].avg_price == 70000.0
    assert holdings[1].name == "SK하이닉스"
    assert holdings[1].code == "000660"
    assert holdings[1].quantity == 5
    assert holdings[1].avg_price == 200000.0


def test_parse_balance_holdings_with_real_fixture():
    """REAL_BALANCE_TEXT(balance.js 실제 출력 픽스처)에서 올바르게 파싱됩니다."""
    from ..scheduler import _parse_balance_holdings
    # 크로스모듈 import 이유: test_balance_parser 가 공유 픽스처를 로드하는 유일한
    # 진입점임을 명시해, 양쪽 모듈이 같은 expected_text 를 사용함을 보장합니다.
    # 수량·평단가 커버리지는 test_balance_parser.TestSharedFixture.
    # test_fixture_normal_round_trips_qty_and_avg_price 에서도 독립적으로 제공됩니다.
    from .test_balance_parser import REAL_BALANCE_TEXT

    holdings = _parse_balance_holdings(REAL_BALANCE_TEXT)

    assert len(holdings) == 2
    assert holdings[0].name == "삼성전자"
    assert holdings[0].code == "005930"
    assert holdings[0].quantity == 3
    assert holdings[0].avg_price == 67000.0
    assert holdings[1].name == "NAVER"
    assert holdings[1].code == "035420"
    assert holdings[1].quantity == 1
    assert holdings[1].avg_price == 201000.0


def test_parse_balance_holdings_empty_returns_empty_list():
    from ..scheduler import _parse_balance_holdings

    assert _parse_balance_holdings("보유 종목이 없습니다.") == []
    assert _parse_balance_holdings(_make_balance_text()) == []


def test_sync_portfolio_writes_holdings(portfolio_session):
    """_sync_portfolio_from_balance가 Portfolio 행을 실제로 씁니다.

    이 테스트가 잡는 mutation: _sync_portfolio_from_balance 호출을 제거하거나
    session.add() 없이 반환하는 회귀 → rows가 비어 assert len==1이 실패한다.
    반환값도 함께 검증한다: 동기화 성공 시 None이 아니라 삽입한 종목 수를
    반환해야 monitor_market_task의 PORTFOLIO_UPDATE 브로드캐스트 판단(이슈 #229,
    `sync_result is not None`)이 성립한다 — 성공 시 무조건 None을 반환하는 회귀는
    이 assert로 잡힌다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    text = _make_balance_text(("삼성전자", "005930", 10, 70000))
    result = _sync_portfolio_from_balance(text, portfolio_session)

    assert result == 1
    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1
    assert rows[0].stock_code == "005930"
    assert rows[0].stock_name == "삼성전자"
    assert rows[0].quantity == 10
    assert rows[0].avg_price == 70000.0
    # current_price는 get_balance output1에 현재가 필드가 없어 항상 null(이슈 #122)
    assert rows[0].current_price is None


def test_sync_portfolio_removes_stale_holdings(portfolio_session):
    """보유하지 않게 된 종목이 동기화 후 제거됩니다.

    이 테스트가 잡는 mutation: delete 없이 add만 하는 회귀 → stale 행이
    남아 assert len==1이 실패한다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    # 초기: 삼성전자 + SK하이닉스
    portfolio_session.add(Portfolio(stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000))
    portfolio_session.add(Portfolio(stock_code="000660", stock_name="SK하이닉스", quantity=5, avg_price=200000))
    portfolio_session.commit()

    # 이후 잔고에 삼성전자만 남음
    text = _make_balance_text(("삼성전자", "005930", 10, 70000))
    _sync_portfolio_from_balance(text, portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1
    assert rows[0].stock_code == "005930"


def test_sync_portfolio_upserts_instead_of_replacing_rows(portfolio_session):
    """같은 종목을 두 번 동기화해도 행이 중복되지 않고 기존 행이 갱신됩니다 (#196).

    이 테스트가 잡는 mutation: (1) upsert를 전량 DELETE + INSERT로 되돌리면 id가
    바뀌어 id 단언이 실패한다. (2) 기존 행을 찾기만 하고 잔고 값을 반영하지 않는
    no-op upsert는 quantity·avg_price 단언이 잡는다. (3) 기존 행을 찾지 않고 늘
    새 행을 add하는 회귀는 len(rows) == 1이 잡는다.

    id를 42로 명시해 심는 이유: SQLModel의 id는 SQLite의 rowid이고 AUTOINCREMENT가
    없어, 테이블을 비운 뒤 다시 넣으면 rowid가 1부터 재사용된다. 기본 id(1)로
    심으면 전량 교체 뮤턴트도 우연히 id == 1을 돌려줘 단언이 통과해버린다.
    빈 테이블에서 절대 나올 수 없는 값을 심어야 이 단언이 판별력을 갖는다.
    (1차 리뷰 🟡4가 지적한 "Portfolio.id를 참조하는 테이블이 생기면 참조가 매
    주기 끊긴다"를 그대로 고정하는 단언이기도 하다.)
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    portfolio_session.add(Portfolio(
        id=42, stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000
    ))
    portfolio_session.commit()

    # 같은 종목, 수량·평단가만 바뀐 다음 주기 잔고
    _sync_portfolio_from_balance(_make_balance_text(("삼성전자", "005930", 15, 72000)), portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1
    assert rows[0].id == 42  # 행이 교체되지 않고 갱신됐다
    assert rows[0].quantity == 15
    assert rows[0].avg_price == 72000.0


def test_sync_portfolio_preserves_fields_absent_from_balance(portfolio_session):
    """잔고 응답에 없는 필드(current_price)가 재동기화 후에도 보존됩니다 (#196).

    **이 파일에서 upsert 전환의 핵심 단언이다.** #196이 시세 조회를 붙이면
    current_price에 값이 들어가는데, 동기화가 전량 교체면 10분 주기마다 그 값이
    null로 되돌아가고 PR #204의 price_known 플래그가 "시세를 채웠는데도 모름"으로
    잘못 나간다. 원인 추적이 어려운 형태의 회귀라 지금 미리 고정한다.

    이 테스트가 잡는 mutation: 전량 DELETE + INSERT로 되돌리기, 또는 upsert
    루프에서 row.current_price = None을 함께 쓰기 → 둘 다 current_price가 null이
    되어 마지막 단언이 실패한다.

    current_price를 실제로 채우는 프로덕션 경로는 아직 없다 — get_balance
    (inquire-balance, TTTC8434R) output1에 현재가 필드가 있는지가 실계좌 실측
    전까지 미확인이기 때문이다(#196). 그래서 여기서는 그 경로가 생긴 뒤의 상태를
    DB에 직접 써서 재현한다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    text = _make_balance_text(("삼성전자", "005930", 10, 70000))
    _sync_portfolio_from_balance(text, portfolio_session)

    # #196이 시세를 채운 뒤의 상태를 재현한다
    row = portfolio_session.exec(select(Portfolio)).one()
    row.current_price = 75000.0
    portfolio_session.add(row)
    portfolio_session.commit()

    # 다음 주기의 동기화: 잔고에는 current_price가 없다
    _sync_portfolio_from_balance(text, portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1
    assert rows[0].current_price == 75000.0


def test_sync_portfolio_preserves_price_only_for_still_held_stocks(portfolio_session):
    """보존과 삭제가 한 동기화 안에서 함께 성립합니다 (#196).

    upsert 전환이 "아무것도 지우지 않게" 되어버리는 반대 방향의 회귀를 잡는다:
    stale 삭제 루프를 제거하면 SK하이닉스 행이 남아 len(rows) == 1이 실패한다.
    동시에 삼성전자의 current_price 단언이, 삭제를 살리려고 전량 교체로 되돌리는
    회귀를 잡는다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    portfolio_session.add(Portfolio(
        stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000, current_price=75000.0
    ))
    portfolio_session.add(Portfolio(
        stock_code="000660", stock_name="SK하이닉스", quantity=5, avg_price=200000, current_price=210000.0
    ))
    portfolio_session.commit()

    # 삼성전자만 남은 잔고
    _sync_portfolio_from_balance(_make_balance_text(("삼성전자", "005930", 10, 70000)), portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1                      # SK하이닉스는 삭제됐다
    assert rows[0].stock_code == "005930"
    assert rows[0].current_price == 75000.0    # 남은 종목의 시세는 보존됐다


def test_sync_portfolio_collapses_duplicate_stock_code_rows(portfolio_session):
    """같은 stock_code 행이 둘 이상이면 한 행으로 수렴합니다 (#196).

    Portfolio.stock_code에는 유일성 제약이 없어(models.py는 index=True만 선언)
    중복 행이 존재할 수 있는 상태를 스키마가 막지 못한다. 중복을 정리하지 않으면
    upsert가 어느 행을 갱신했는지에 따라 /api/v1/portfolio 응답이 달라져
    비결정적이 된다.

    이 테스트가 잡는 mutation: 삭제 루프에서 rows[1:] 정리를 빼고 stale 종목만
    지우도록 되돌리는 회귀 → 중복 행이 남아 len(rows) == 1이 실패한다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    portfolio_session.add(Portfolio(stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000))
    portfolio_session.add(Portfolio(stock_code="005930", stock_name="삼성전자", quantity=99, avg_price=1))
    portfolio_session.commit()

    _sync_portfolio_from_balance(_make_balance_text(("삼성전자", "005930", 10, 70000)), portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1
    assert rows[0].quantity == 10


def test_sync_portfolio_collapses_duplicate_codes_within_one_response(portfolio_session):
    """한 잔고 응답에 같은 코드가 두 번 와도 행이 하나만 남습니다 (#196).

    위 테스트는 **DB에 이미 있던** 중복 행을 다루지만, 이 테스트는 동기화가
    **스스로 만들어 내는** 중복을 다룬다. 빈 테이블에서 시작하므로 첫 항목은
    else 분기(신규 삽입)를 타고, 그때 새 행을 existing에 등록하지 않으면 두 번째
    항목이 또 하나의 새 행을 만든다. 그 행들은 existing에 없어 삭제 루프도
    지나치므로 테이블에 2행이 남는다.

    KIS output1은 pdno가 키라 정상 응답에는 중복이 없지만, 스키마가 막아주지
    않는 이상 이 함수가 스스로 불변식("코드당 1행")을 깨뜨려서는 안 된다.

    이 테스트가 잡는 mutation: 신규 삽입 분기의 existing[h.code] = [row] 등록을
    제거 → len(rows)가 2가 되어 실패한다. 등록은 하되 재방문 시 갱신을 하지 않는
    회귀는 quantity·avg_price 단언이 잡는다(마지막 항목의 값으로 수렴해야 한다).
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    text = _make_balance_text(
        ("삼성전자", "005930", 10, 70000),
        ("삼성전자", "005930", 5, 71000),
    )
    returned = _sync_portfolio_from_balance(text, portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1
    assert rows[0].quantity == 5          # 마지막 항목으로 갱신됐다
    assert rows[0].avg_price == 71000.0
    # 반환값은 잔고 응답의 항목 수라 행 수와 갈릴 수 있다(docstring 참고).
    # #229의 PORTFOLIO_UPDATE holdings_count 호환을 위해 len(holdings)를 유지한다.
    assert returned == 2


def test_sync_portfolio_skips_when_balance_truncated(portfolio_session):
    """잔고 연속조회가 잘리면 기존 Portfolio 데이터를 파괴하지 않습니다.

    이 테스트가 잡는 mutation: is_balance_truncated 가드를 제거하면 잘린 잔고로
    전량 교체가 일어나 기존 종목이 사라져 assert len==2가 실패한다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance
    from .test_balance_parser import TRUNCATION_NOTES_BY_REASON

    # 기존 데이터: 삼성전자 + SK하이닉스
    portfolio_session.add(Portfolio(stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000))
    portfolio_session.add(Portfolio(stock_code="000660", stock_name="SK하이닉스", quantity=5, avg_price=200000))
    portfolio_session.commit()

    # 잘린 잔고: 삼성전자만 있는 척
    truncated_text = (
        _make_balance_text(("삼성전자", "005930", 10, 70000)).rstrip()
        + TRUNCATION_NOTES_BY_REASON["max_pages"]
    )
    _sync_portfolio_from_balance(truncated_text, portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    # 잘린 잔고 → 동기화 건너뜀 → 기존 2개 유지
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_monitor_market_task_syncs_portfolio_when_balance_succeeds(monkeypatch):
    """잔고 조회 성공 시 monitor_market_task가 Portfolio 동기화를 호출합니다.

    이 테스트가 잡는 mutation: _monitor_market_task에서 _sync_portfolio_from_balance
    호출을 제거하는 회귀 → sync_calls가 비어 assert len==1이 실패한다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())
    sync_calls = []

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return _make_balance_text(("삼성전자", "005930", 10, 70000))
        return "news"

    def mock_sync_portfolio(balance_text, session, **kwargs):
        sync_calls.append(balance_text)

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert len(sync_calls) == 1


@pytest.mark.asyncio
async def test_monitor_market_task_does_not_sync_when_balance_fails(monkeypatch):
    """잔고 조회 실패 시 Portfolio 동기화를 호출하지 않습니다 (기존 데이터 보호).

    이 테스트가 잡는 mutation: balance_ok 가드를 제거해 실패 시에도
    _sync_portfolio_from_balance가 호출되는 회귀 → sync_calls가 비어야 하는데
    1개가 들어가 assert sync_calls==[] 가 실패한다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())
    sync_calls = []

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            raise RuntimeError("KIS 장애")
        return "news"

    def mock_sync_portfolio(balance_text, session, **kwargs):
        sync_calls.append(balance_text)

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

    assert sync_calls == []


# ---------------------------------------------------------------------------
# 이슈 #229: Portfolio 동기화 성공 시 WebSocket PORTFOLIO_UPDATE 브로드캐스트
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_monitor_market_task_broadcasts_portfolio_update_on_sync_success(monkeypatch):
    """Portfolio 동기화 성공 시 PORTFOLIO_UPDATE가 정확히 1회, 올바른 형태로 브로드캐스트됩니다.

    _sync_portfolio_from_balance 자체의 동기화 로직은 test_sync_portfolio_writes_holdings
    등에서 이미 검증하므로, 여기서는 그 함수를 mock_broadcast 패턴과 동일하게
    monkeypatch해 호출처(monitor_market_task)의 브로드캐스트 판단만 격리해서 검증한다
    (실 DB의 portfolio 테이블 존재 여부에 테스트 결과가 좌우되지 않게 하기 위함).

    이 테스트가 잡는 mutation: 호출처가 PORTFOLIO_UPDATE 브로드캐스트를 하지 않거나
    holdings_count/broadcast_at 형태를 잘못 실으면 아래 단언이 실패한다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    @asynccontextmanager
    async def unavailable_redis_state():
        raise ConnectionError("redis unavailable")
        yield

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return _make_balance_text(("삼성전자", "005930", 10, 70000))
        return "news"

    def mock_sync_portfolio(balance_text, session, **kwargs):
        return 3

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    mock_broadcast.assert_called_once()
    payload = mock_broadcast.call_args[0][0]
    assert payload["type"] == "PORTFOLIO_UPDATE"
    assert payload["holdings_count"] == 3
    # broadcast_at은 ISO 8601로 파싱 가능해야 한다.
    datetime.fromisoformat(payload["broadcast_at"])
    # 보유 내역 원본(종목명·수량 등)은 인증 없는 WebSocket으로 나가면 안 된다(#266).
    assert "holdings" not in payload
    assert "stocks" not in payload


@pytest.mark.asyncio
async def test_monitor_market_task_broadcasts_portfolio_update_when_holdings_become_zero(monkeypatch):
    """보유 종목이 0건으로 동기화되어도 PORTFOLIO_UPDATE를 브로드캐스트합니다.

    _sync_portfolio_from_balance는 실제 보유 0건이면 None이 아니라 0을 반환한다
    (test_sync_portfolio_allows_genuinely_empty_holdings). 호출처가 `sync_result
    is not None` 대신 `sync_result`(truthy)로 판단하면 0은 falsy라 이 브로드캐스트가
    빠진다 — 마지막 남은 보유 종목이 전량 매도된 순간 클라이언트에 신호가 가지 않는
    회귀다. 이 테스트가 잡는 mutation: 호출처의 None 판단을 truthy 판단으로 바꾸면
    mock_broadcast.assert_called_once()가 실패한다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    @asynccontextmanager
    async def unavailable_redis_state():
        raise ConnectionError("redis unavailable")
        yield

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return _make_balance_text(("삼성전자", "005930", 10, 70000))
        return "news"

    def mock_sync_portfolio(balance_text, session, **kwargs):
        return 0

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    mock_broadcast.assert_called_once()
    payload = mock_broadcast.call_args[0][0]
    assert payload["type"] == "PORTFOLIO_UPDATE"
    assert payload["holdings_count"] == 0


@pytest.mark.asyncio
async def test_monitor_market_task_no_portfolio_broadcast_when_balance_truncated(monkeypatch):
    """잔고 연속조회가 잘린 경우 PORTFOLIO_UPDATE를 브로드캐스트하지 않습니다.

    _sync_portfolio_from_balance를 mock하지 않고 실제 함수를 그대로 태워, 잘림
    가드가 DB 접근 이전에 None을 반환해 브로드캐스트로 이어지지 않음을 검증한다.

    이 테스트가 잡는 mutation: 호출처가 sync_result 값과 무관하게 항상 브로드캐스트하면
    잘린 잔고에서도 PORTFOLIO_UPDATE가 나가 아래 단언이 실패한다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    @asynccontextmanager
    async def unavailable_redis_state():
        raise ConnectionError("redis unavailable")
        yield

    truncated_text = (
        _make_balance_text(("삼성전자", "005930", 10, 70000)).rstrip()
        + TRUNCATION_NOTES_BY_REASON["max_pages"]
    )

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return truncated_text
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    broadcast_types = [call.args[0].get("type") for call in mock_broadcast.call_args_list]
    assert "PORTFOLIO_UPDATE" not in broadcast_types


@pytest.mark.asyncio
async def test_monitor_market_task_no_portfolio_broadcast_when_marker_absent(monkeypatch):
    """잔고 응답에 [보유 종목 리스트] 마커가 없으면 PORTFOLIO_UPDATE를 브로드캐스트하지 않습니다.

    이 테스트가 잡는 mutation: 마커 부재 가드를 우회해 브로드캐스트하면
    아래 단언이 실패한다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    @asynccontextmanager
    async def unavailable_redis_state():
        raise ConnectionError("redis unavailable")
        yield

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "응답을 가져왔지만 종목 섹션이 없습니다."
        return "news"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    broadcast_types = [call.args[0].get("type") for call in mock_broadcast.call_args_list]
    assert "PORTFOLIO_UPDATE" not in broadcast_types


@pytest.mark.asyncio
async def test_monitor_market_task_survives_portfolio_broadcast_failure(monkeypatch):
    """PORTFOLIO_UPDATE 브로드캐스트가 예외를 던져도 monitor_market_task는 계속 진행합니다.

    기존 동기화 예외 격리(logger.error(..., "감시는 계속"))와 같은 수준으로 브로드캐스트
    예외도 격리해야 한다. 이 테스트가 잡는 mutation: 브로드캐스트 예외를 격리하지 않으면
    monitor_market_task가 예외를 전파해 await 자체가 실패한다.

    참고: 현재 ConnectionManager.broadcast는 커넥션별 send_text를 루프 안 try/except로
    삼켜 스스로 예외를 던지지 않으므로, 이 시나리오는 실제
    운영 경로에서는 아직 발생할 수 없다. 이 테스트는 monitor_market_task 쪽 방어
    코드가 지키는 계약을 문서화하는 성격이며, ws_manager의 예외 처리가 바뀌면
    (예: 커넥션이 아니라 broadcast 자체가 실패를 전파하도록 바뀌면) 실효를 갖는다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    @asynccontextmanager
    async def unavailable_redis_state():
        raise ConnectionError("redis unavailable")
        yield

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return _make_balance_text(("삼성전자", "005930", 10, 70000))
        return "news"

    def mock_sync_portfolio(balance_text, session, **kwargs):
        return 1

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    async def failing_broadcast(payload):
        raise RuntimeError("websocket down")

    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", failing_broadcast)

    # 예외 없이 완료되어야 한다.
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())


# ---------------------------------------------------------------------------
# Critical 1: 소수점 평단가 파싱 테스트
# ---------------------------------------------------------------------------

def test_parse_balance_holdings_accepts_fractional_avg_price():
    """balance.js formatAmount는 정수가 아닌 평단가에 소수점을 붙인다.
    mcp-trading/tests/balance.test.js가 "평단가 66,666.67원"을 리터럴로 단언하므로
    이것은 가정이 아니라 계약이다.

    이 테스트가 잡는 mutation: _AVG_PRICE_RE를 원래 r"평단가\\s+([\\d,]+)원"으로
    되돌리면 "66,666"까지 먹고 다음 글자가 "."라 매치 실패 → avg_price=0.0으로 red.
    """
    from ..scheduler import _parse_balance_holdings

    text = (
        "[보유 종목 리스트]\n"
        "- 삼성전자 (005930) · 3주\n"
        "  평단가 66,666.67원 → 평가금액 190,000원\n"
        "  손익 -10,000원 · 수익률 🔵 ▼ -5.00%"
    )
    holdings = _parse_balance_holdings(text)
    assert len(holdings) == 1
    assert holdings[0].avg_price == pytest.approx(66666.67)


def test_make_balance_text_fixture_passes_fractional_avg_price():
    """_make_balance_text 픽스처 자체가 소수 평단가를 올바르게 렌더링하는지 확인합니다.
    픽스처가 int(avg_price)만 쓰면 "66,666원"이 돼 매치되고 파싱값이 66666.0으로
    틀린 반올림이 일어납니다.

    이 테스트가 잡는 mutation: _make_balance_text에서 avg_str을 f"{int(avg_price):,}"
    로 되돌리면 "66,666.67" 대신 "66,666"이 생성 → 파싱값이 66666.0 ≠ approx(66666.67)
    으로 red.
    """
    from ..scheduler import _parse_balance_holdings

    text = _make_balance_text(("삼성전자", "005930", 3, 66666.67))
    assert "66,666.67원" in text, f"픽스처에 소수 평단가가 없습니다: {text!r}"
    holdings = _parse_balance_holdings(text)
    assert len(holdings) == 1
    assert holdings[0].avg_price == pytest.approx(66666.67)


# ---------------------------------------------------------------------------
# Critical 2: 마커 부재 시 동기화 건너뜀 테스트
# ---------------------------------------------------------------------------

def test_sync_portfolio_skips_when_marker_absent(portfolio_session):
    """[보유 종목 리스트] 마커가 없는 응답에서 기존 Portfolio 데이터를 파괴하지 않습니다.

    이 테스트가 잡는 mutation: _BALANCE_HOLDINGS_MARKER 가드를 제거하면 빈 holdings로
    전량 교체가 일어나 기존 1개 행이 사라져 assert len==1이 실패한다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    portfolio_session.add(Portfolio(stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000))
    portfolio_session.commit()

    # 마커 없는 응답 — "보유 0건"이 아니라 "응답을 읽지 못함"
    no_marker_text = "응답을 가져왔지만 종목 섹션이 없습니다."
    _sync_portfolio_from_balance(no_marker_text, portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1, "마커 부재 시 기존 데이터가 보존되어야 합니다"


def test_sync_portfolio_skips_empty_balance_text(portfolio_session):
    """빈 문자열(run_mcp_tool이 MCP content 없을 때 반환하는 값)에서
    기존 Portfolio 데이터를 파괴하지 않습니다.

    이 테스트가 잡는 mutation: _BALANCE_HOLDINGS_MARKER 가드를 제거하면 ""를 성공
    응답으로 처리해 전량 교체 → 기존 데이터 소실 → assert len==1이 실패한다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    portfolio_session.add(Portfolio(stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000))
    portfolio_session.commit()

    _sync_portfolio_from_balance("", portfolio_session)

    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 1, "빈 응답 시 기존 데이터가 보존되어야 합니다"


def test_sync_portfolio_allows_genuinely_empty_holdings(portfolio_session):
    """마커는 있지만 보유 종목이 없는 응답("보유 종목이 없습니다.")은
    실제 0건으로 처리해 기존 데이터를 전량 삭제합니다.

    마커 가드가 "실제 0건"을 막아서는 안 됩니다. 반환값도 0이어야 한다 — None을
    반환하면 monitor_market_task가 "동기화를 건너뛴 것"으로 오인해(이슈 #229)
    보유 0건으로 갱신됐다는 PORTFOLIO_UPDATE를 브로드캐스트하지 않게 된다.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    portfolio_session.add(Portfolio(stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000))
    portfolio_session.commit()

    # balance.js가 보유 0건일 때 실제로 생성하는 텍스트(balance.js:273-274)
    empty_holdings_text = (
        "[계좌 잔고 현황]\n"
        "- 총 평가금액: 0원\n"
        "\n"
        "[보유 종목 리스트]\n"
        "보유 종목이 없습니다."
    )
    result = _sync_portfolio_from_balance(empty_holdings_text, portfolio_session)

    assert result == 0, "실제 보유 0건 동기화는 None이 아니라 0을 반환해야 합니다"
    rows = portfolio_session.exec(select(Portfolio)).all()
    assert len(rows) == 0, "실제 보유 0건이면 기존 데이터를 삭제해야 합니다"


# ---------------------------------------------------------------------------
# Critical 3: price_known 플래그 테스트
# ---------------------------------------------------------------------------

def test_portfolio_endpoint_includes_price_known_flag(client):
    """/api/v1/portfolio 응답의 각 holding에 price_known 플래그가 포함됩니다.

    이 테스트가 잡는 mutation: holdings.append에서 "price_known" 키를 제거하면
    응답에 없어 assert "price_known" in holding이 실패한다.
    """
    from sqlmodel import Session, select
    from ..database import engine
    from ..models import Portfolio
    from datetime import datetime, timezone

    with Session(engine) as session:
        for row in session.exec(select(Portfolio)).all():
            session.delete(row)
        session.add(Portfolio(
            stock_code="005930",
            stock_name="삼성전자",
            quantity=10,
            avg_price=70000,
            current_price=None,
            updated_at=datetime.now(timezone.utc),
        ))
        session.commit()

    try:
        resp = client.get("/api/v1/portfolio")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["holdings"]) == 1
        holding = data["holdings"][0]
        assert "price_known" in holding, "price_known 플래그가 응답에 없습니다"
        # current_price가 None이므로 price_known은 False여야 한다
        assert holding["price_known"] is False
        assert "return_rate_known" in holding, "return_rate_known 플래그가 응답에 없습니다"
        # current_price가 None이면 수익률도 계산 불가
        assert holding["return_rate_known"] is False
        assert "total_asset_is_estimate" in data
        assert data["total_asset_is_estimate"] is True
        assert "total_return_rate_known" in data
        assert data["total_return_rate_known"] is False
    finally:
        with Session(engine) as session:
            for row in session.exec(select(Portfolio)).all():
                session.delete(row)
            session.commit()

# --- #298: 신호 점수가 분석까지 흘러가는지 ---------------------------------


@pytest.mark.asyncio
async def test_monitor_signal_passes_signal_score_to_analysis(monkeypatch):
    """2차 필터가 매긴 점수가 perform_stock_analysis까지 도달해야 한다.

    스케줄러가 score_signal의 결과를 그대로 넘기지 않으면 점수는 로그에만 남고
    DB·알림에서 사라진다. 여기서는 필터를 monkeypatch하지 않고 llm_chat만
    갈아끼워 실제 채점 경로를 태운다.
    """
    from backend import services
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "삼성전자, 대형 수주 공시\n증권가 목표주가 상향"

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return '{"score": 3, "reason": "대형 수주 공시", "headline_scores": [3, 2]}'

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked"})
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert mock_perform_analysis.call_count == 1
    passed = mock_perform_analysis.call_args.kwargs["signal_score"]
    assert passed is not None
    assert passed.score == 3
    assert passed.reason == "대형 수주 공시"
    assert passed.headline_scores == (3, 2)
    assert passed.uncertainty == 0.5


@pytest.mark.asyncio
async def test_monitor_signal_passes_none_when_scoring_fails_open(monkeypatch):
    """채점에 실패하면 분석은 계속하되(fail-open) 점수는 null로 넘어가야 한다.

    실패를 0점으로 넘기면 "모델이 중립이라고 판단함"이라는 없던 사실이 DB에 남는다.
    """
    from backend import services
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "삼성전자 관련 뉴스"

    async def exploding_llm_chat(provider, prompt, *, conversation_id=None):
        raise RuntimeError("ollama 연결 실패")

    monkeypatch.setattr(services, "llm_chat", exploding_llm_chat)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked"})
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    # fail-open: 분석은 수행된다
    assert mock_perform_analysis.call_count == 1
    passed = mock_perform_analysis.call_args.kwargs["signal_score"]
    assert passed is not None
    assert passed.is_significant is True
    assert passed.score is None


class FakeFilteredSignalRepo:
    """걸러진 신호 기록을 메모리에 모으는 repo (#304)."""

    def __init__(self, fail: bool = False):
        self.recorded = []
        self.purged = []
        self._fail = fail

    async def record(self, **kwargs):
        # 진짜 repo와 같이 아무것도 돌려주지 않는다 — 반환값에 기대는 호출부가
        # 생기면 fake만 통과하고 프로덕션에서 None이 되는 차이가 난다.
        if self._fail:
            raise RuntimeError("filtered signal db unavailable")
        self.recorded.append(kwargs)

    async def purge_expired(self, retention_days, *, now=None):
        if self._fail:
            raise RuntimeError("filtered signal db unavailable")
        self.purged.append(retention_days)
        return 7


def _filtered_scored(score: int, reason: str | None = None, uncertainty=None) -> SignalScore:
    """임계값 미만으로 걸러진 채점 결과."""
    return SignalScore(
        score=score,
        reason=reason,
        uncertainty=uncertainty,
        headline_scores=(),
        is_significant=False,
    )


@pytest.mark.asyncio
async def test_monitor_signal_records_filtered_score(monkeypatch):
    """임계값 미만으로 걸러진 신호의 점수가 구조화돼 남아야 한다 (#304).

    이 값이 로그에만 남으면 grep은 되지만 집계가 안 돼 임계값을 조정할 근거가 없다.
    """
    from ..config import SIGNAL_SCORE_THRESHOLD
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "삼성전자 관련 뉴스"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _filtered_scored(1, reason="단순 시황 나열", uncertainty=0.5)

    mock_perform_analysis = MagicMock()
    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    repo = FakeFilteredSignalRepo()
    await monitor_market_task(
        watchlist_repo=FakeWatchlistRepo(), filtered_signal_repo=repo
    )

    assert mock_perform_analysis.call_count == 0
    assert repo.recorded == [
        {
            "stock_name": "삼성전자",
            "source": "news",
            "score": 1,
            "threshold": SIGNAL_SCORE_THRESHOLD,
            "reason": "단순 시황 나열",
            "uncertainty": 0.5,
        }
    ]


@pytest.mark.asyncio
async def test_monitor_signal_does_not_record_passing_score(monkeypatch):
    """통과한 신호는 이 테이블이 아니라 AgentReport에 남는다 — 두 번 세면 안 된다."""
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "삼성전자 대형 수주"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(True, score=3)

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked"})

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    repo = FakeFilteredSignalRepo()
    await monitor_market_task(
        watchlist_repo=FakeWatchlistRepo(), filtered_signal_repo=repo
    )

    assert mock_perform_analysis.call_count == 1
    assert repo.recorded == []


@pytest.mark.asyncio
async def test_monitor_signal_does_not_record_unscored_signal(monkeypatch):
    """채점 자체가 없었던 신호(빈 본문·직전과 동일)는 기록하지 않는다.

    점수 없는 행은 분포에 보탤 정보가 없는데, 감시 루프가 10분마다 도는 탓에
    그런 행만으로 테이블이 가득 찬다.
    """
    from ..scheduler import SignalSource, monitor_market_task

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "삼성전자 관련 뉴스"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    repo = FakeFilteredSignalRepo()
    await monitor_market_task(
        watchlist_repo=FakeWatchlistRepo(), filtered_signal_repo=repo
    )

    assert repo.recorded == []


@pytest.mark.asyncio
async def test_monitor_signal_continues_when_recording_fails(monkeypatch):
    """기록이 실패해도 감시는 다음 종목까지 계속돼야 한다.

    이 값은 사후 분석용이다. 여기서 예외가 올라오면 DB 문제 하나가 그 주기의 감시를
    통째로 멈춘다 — 놓침 방지(REQ-04)와 정면으로 충돌한다.
    """
    from .. import scheduler as scheduler_module
    from ..scheduler import SignalSource, monitor_market_task

    # 이 테스트는 기록을 실패시키므로 연속 실패 카운터를 올린다. monkeypatch로 깔아 두면
    # 종료 시 원래 값으로 되돌아가, 뒤에 도는 억제·복구 테스트가 이 테스트의 잔재를
    # 물려받지 않는다 (실행 순서에 의존하는 상태를 남기지 않는다).
    monkeypatch.setattr(scheduler_module, "_filtered_signal_failure_streak", 0)
    monkeypatch.setattr(scheduler_module, "_last_filtered_signal_error", None)

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    monitored_stocks = []

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        monitored_stocks.append(args["stock_name"])
        return f"{args['stock_name']} 관련 뉴스"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _filtered_scored(0, reason="중립")

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    await monitor_market_task(
        watchlist_repo=FakeWatchlistRepo(["NAVER"]),
        filtered_signal_repo=FakeFilteredSignalRepo(fail=True),
    )

    assert monitored_stocks == ["삼성전자", "NAVER"]


@pytest.mark.asyncio
async def test_purge_filtered_signals_task_deletes_expired_rows():
    from ..config import FILTERED_SIGNAL_RETENTION_DAYS
    from ..scheduler import purge_filtered_signals_task

    repo = FakeFilteredSignalRepo()

    await purge_filtered_signals_task(filtered_signal_repo=repo)

    assert repo.purged == [FILTERED_SIGNAL_RETENTION_DAYS]


@pytest.mark.asyncio
async def test_purge_filtered_signals_task_swallows_failure():
    """정리 실패가 스케줄러 잡을 죽이지 않는다 — 다음 주기에 같은 조건으로 다시 지운다."""
    from ..scheduler import purge_filtered_signals_task

    await purge_filtered_signals_task(
        filtered_signal_repo=FakeFilteredSignalRepo(fail=True)
    )


def test_start_scheduler_registers_purge_job(monkeypatch):
    """정리 잡이 등록되지 않으면 보존 정책이 조용히 사라진다."""
    from .. import scheduler as scheduler_module

    registered = []

    class FakeScheduler:
        running = False
        timezone = scheduler_module.scheduler.timezone

        def add_job(self, func, trigger, **kwargs):
            registered.append(kwargs.get("id"))

        def start(self):
            pass

    monkeypatch.setattr(scheduler_module, "scheduler", FakeScheduler())
    scheduler_module.start_scheduler()

    assert "purge_filtered_signals" in registered


@pytest.mark.asyncio
async def test_monitor_signal_records_through_real_repo(monkeypatch):
    """덕타이핑 fake가 아니라 실제 repo에 넣어 행이 실제로 생기는지 본다 (#304 리뷰).

    _record_filtered_signal은 기록 실패를 삼키고 로그만 남긴다. 그래서 호출 계약이
    어긋나면(키워드 이름 하나만 달라져도 TypeError다) fake만 쓰는 테스트는 초록인 채
    프로덕션만 조용히 무기록이 된다. 이 테스트만 그 간극을 덮는다.
    """
    from sqlmodel import SQLModel, Session, create_engine, select
    from sqlmodel.pool import StaticPool

    from ..config import SIGNAL_SCORE_THRESHOLD
    from ..filtered_signal_repo import SqliteFilteredSignalRepo
    from ..models import FilteredSignal
    from ..scheduler import SignalSource, monitor_market_task

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    repo = SqliteFilteredSignalRepo(lambda: Session(engine))

    state = RedisSchedulerState(FakeRedis())

    @asynccontextmanager
    async def fake_redis_state():
        yield state

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        return "삼성전자 관련 뉴스"

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _filtered_scored(1, reason="단순 시황 나열", uncertainty=0.5)

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())
    monkeypatch.setattr(
        "backend.scheduler._sync_portfolio_from_balance",
        lambda balance_text, session, **kwargs: None,
    )

    await monitor_market_task(
        watchlist_repo=FakeWatchlistRepo(), filtered_signal_repo=repo
    )

    with Session(engine) as session:
        rows = session.exec(select(FilteredSignal)).all()
    assert len(rows) == 1
    assert rows[0].stock_name == "삼성전자"
    assert rows[0].source == "news"
    assert rows[0].score == 1
    assert rows[0].threshold == SIGNAL_SCORE_THRESHOLD
    assert rows[0].reason == "단순 시황 나열"
    assert rows[0].uncertainty == 0.5


@pytest.mark.asyncio
async def test_repeated_recording_failures_are_logged_once_per_period(monkeypatch, caplog):
    """같은 원인의 기록 실패는 첫 건과 주기마다만 error로 남는다 (#304 리뷰).

    기록은 (종목 × 소스)마다 시도하므로, 억제가 없으면 DB 장애 하나가 한 주기에
    수십 건의 동일한 error를 쌓아 정작 중요한 장애를 묻는다 — 잔고 조회 실패(#185)가
    이미 같은 이유로 카운터를 두고 있다.
    """
    import logging

    from .. import scheduler as scheduler_module
    from ..scheduler import _record_filtered_signal

    monkeypatch.setattr(scheduler_module, "_filtered_signal_failure_streak", 0)
    monkeypatch.setattr(scheduler_module, "_last_filtered_signal_error", None)

    repo = FakeFilteredSignalRepo(fail=True)
    scored = _filtered_scored(1, reason="중립")

    with caplog.at_level(logging.DEBUG, logger="backend.scheduler"):
        for _ in range(scheduler_module._FILTERED_SIGNAL_ERROR_LOG_PERIOD):
            await _record_filtered_signal(repo, "삼성전자", "news", scored)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    # 첫 실패 1건 + 주기(20회)째 1건.
    assert len(errors) == 2
    assert scheduler_module._filtered_signal_failure_streak == (
        scheduler_module._FILTERED_SIGNAL_ERROR_LOG_PERIOD
    )


@pytest.mark.asyncio
async def test_recording_recovery_reports_missed_count(monkeypatch, caplog):
    """복구되면 그동안 몇 건을 놓쳤는지 남긴다.

    기록되지 않은 신호는 어디에도 흔적이 없다. 놓친 건수를 남기지 않으면 나중에
    분포에 구멍이 뚫린 구간을 알아볼 방법이 없다.
    """
    import logging

    from .. import scheduler as scheduler_module
    from ..scheduler import _record_filtered_signal

    monkeypatch.setattr(scheduler_module, "_filtered_signal_failure_streak", 0)
    monkeypatch.setattr(scheduler_module, "_last_filtered_signal_error", None)

    scored = _filtered_scored(1, reason="중립")
    for _ in range(3):
        await _record_filtered_signal(
            FakeFilteredSignalRepo(fail=True), "삼성전자", "news", scored
        )

    with caplog.at_level(logging.WARNING, logger="backend.scheduler"):
        await _record_filtered_signal(
            FakeFilteredSignalRepo(), "삼성전자", "news", scored
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "3건" in warnings[0].getMessage()
    assert scheduler_module._filtered_signal_failure_streak == 0


# ---------------------------------------------------------------------------
# 이슈 #196: get_balance_rlz_pl 텍스트에서 시세를 읽어 Portfolio에 쓴다
# ---------------------------------------------------------------------------

# mcp-trading·backend 공유 픽스처(#196). balance_report.json(#137)과 같은 목적이며,
# 경로도 같은 방식으로 __file__ 기준 절대 경로라 worktree·CI 양쪽에서 동일하게 풀린다.
#
# 고정은 **양방향**이다: mcp-trading/tests/balance-rlz-pl-report.test.js가 같은 파일의
# input을 formatter에 넣어 expected_text와 바이트 단위로 비교하므로, formatter가 바뀌면
# 형식을 소유한 JS 스위트가 먼저 깨진다. 이 파일을 고칠 때는 양쪽을 함께 돌린다.
_RLZ_PL_FIXTURE_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "mcp-trading" / "tests" / "fixtures" / "balance_rlz_pl_report.json"
)
_RLZ_PL_FIXTURE = json.loads(_RLZ_PL_FIXTURE_PATH.read_text(encoding="utf-8"))


def _rlz_pl_text(case: str) -> str:
    return _RLZ_PL_FIXTURE[case]["expected_text"]


def test_parse_rlz_pl_quotes_reads_price_per_code():
    """공유 픽스처의 정상 응답에서 코드별 현재가를 읽는다.

    같은 pdno(005930)가 현금·신용 두 행으로 나뉘어 있는데, prpr은 종목의 시장
    가격이라 두 행이 같다. 코드를 키로 첫 행만 채택하므로 결과는 2건이어야 한다.

    이 테스트가 잡는 mutation: 코드 중복을 걸러내지 않고 매번 덮어쓰거나 더하는 회귀,
    _RLZ_PL_PRICE_RE를 매입가/평가금액 줄까지 훑도록 넓히는 회귀(그러면 삼성전자
    현재가가 70,000이나 712,000으로 잡힌다).
    """
    from ..scheduler import _parse_rlz_pl_quotes

    quotes = _parse_rlz_pl_quotes(_rlz_pl_text("normal"))

    assert quotes == {"005930": 71200.0, "035420": 201500.0}


def test_parse_rlz_pl_quotes_omits_holdings_without_price():
    """시세를 읽을 수 없는 두 형태 모두 결과에서 빠진다.

    (1) prpr이 null이라 "현재가 -"로 나온 행(000000): 정규식이 매치되지 않는다.
    (2) prpr이 "0"이라 "현재가 0원"으로 나온 행(123456): 정규식은 **매치된다**.
        formatWon은 undefined·null·""에만 "-"를 내므로(formatters.js:7-10) "0"은
        그대로 "0원"이 되고, 값을 보고 거르지 않으면 0.0이 신선한 현재가로 저장된다.
        KIS는 거래정지·장 시작 전·신규 상장 종목에 prpr=0을 흔히 준다.

    어느 쪽이든 0으로 읽으면 없던 시세가 생긴다(#122의 "0과 모름 구분"). 빠진 종목은
    기존 값이 그대로 남고 TTL이 지나면 스스로 "모름"이 된다.

    이 테스트가 잡는 mutation: 파싱 실패 시 0.0을 채우는 회귀(000000이 0.0으로
    들어온다), 그리고 `if price <= 0` 가드 제거(123456이 0.0으로 들어온다). 둘 다
    딕셔너리 비교가 실패한다.
    """
    from ..scheduler import _parse_rlz_pl_quotes

    quotes = _parse_rlz_pl_quotes(_rlz_pl_text("no_price"))

    assert quotes == {"005930": 71200.0}
    # 0원 행이 "시세 0원"으로 살아 들어오지 않았음을 코드로도 못 박는다.
    assert "123456" not in quotes


def test_parse_rlz_pl_quotes_warns_when_duplicate_rows_disagree(caplog):
    """같은 코드의 두 행이 다른 현재가를 들고 오면 첫 행을 쓰되 경고를 남긴다.

    "prpr은 매매구분과 무관한 시장 가격이라 행마다 같다"는 것은 추론이지 관측이
    아니다 — TTTC8494R 실계좌 응답을 아직 아무도 보지 못했다. 실측 전까지 그 전제를
    반증할 수 있는 경로는 프로덕션 로그뿐이므로, 어긋난 값을 조용히 버리면 증거가
    사라진다.

    이 테스트가 잡는 mutation: 중복 행을 값도 읽지 않고 곧바로 continue하는 회귀
    (경고가 사라진다), 또는 첫 행 대신 마지막 행을 채택하는 회귀(99999가 들어온다).
    """
    import logging

    from ..scheduler import _parse_rlz_pl_quotes

    with caplog.at_level(logging.WARNING, logger="backend.scheduler"):
        quotes = _parse_rlz_pl_quotes(_rlz_pl_text("divergent_price"))

    assert quotes == {"005930": 71200.0}
    diverged = [r for r in caplog.records if "엇갈립니다" in r.getMessage()]
    assert len(diverged) == 1, "엇갈린 현재가는 정확히 한 번 경고로 남아야 한다"
    message = diverged[0].getMessage()
    # 채택값과 버린 값이 모두 로그에 남아야 반증 자료로 쓸 수 있다.
    assert "005930" in message
    assert "71200" in message
    assert "99999" in message


def test_sync_portfolio_prices_writes_price_and_stamp(portfolio_session):
    """시세와 갱신 시각을 함께 쓴다. 둘 중 하나만 쓰면 게이트가 무너진다.

    이 테스트가 잡는 mutation: row.price_updated_at 대입 제거(값만 새로워지고 나이는
    영영 null이라 API가 계속 "모름"으로 내린다), row.current_price 대입 제거,
    또는 반환값을 갱신 행 수 대신 상수로 바꾸는 회귀.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_prices_from_rlz_pl

    before = datetime.now(timezone.utc)
    portfolio_session.add(
        Portfolio(stock_code="005930", stock_name="삼성전자", quantity=10, avg_price=70000)
    )
    portfolio_session.add(
        Portfolio(stock_code="035420", stock_name="NAVER", quantity=2, avg_price=202500)
    )
    portfolio_session.commit()

    updated = _sync_portfolio_prices_from_rlz_pl(_rlz_pl_text("normal"), portfolio_session)

    assert updated == 2
    rows = {r.stock_code: r for r in portfolio_session.exec(select(Portfolio)).all()}
    assert rows["005930"].current_price == 71200.0
    assert rows["035420"].current_price == 201500.0
    for row in rows.values():
        assert row.price_updated_at is not None
        # SQLite는 오프셋을 저장하지 않으므로 읽어 온 값이 naive일 수 있다.
        stamped = row.price_updated_at
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        assert stamped >= before


def test_sync_portfolio_prices_leaves_unquoted_rows_untouched(portfolio_session):
    """응답에 없는 종목의 시세와 스탬프는 손대지 않는다.

    지우면 일시적 누락이 곧바로 "시세 없음"이 되고, 스탬프만 갱신하면 낡은 값이
    신선하다고 단언된다. 둘 다 하지 않는 것이 옳다 — TTL이 알아서 내려 준다.

    이 테스트가 잡는 mutation: 응답에 없는 행의 current_price를 None으로 지우는 회귀,
    또는 모든 행에 price_updated_at을 찍는 회귀.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_prices_from_rlz_pl

    old_stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    portfolio_session.add(
        Portfolio(
            stock_code="035720",
            stock_name="카카오",
            quantity=3,
            avg_price=42000,
            current_price=41000,
            price_updated_at=old_stamp,
        )
    )
    portfolio_session.commit()

    updated = _sync_portfolio_prices_from_rlz_pl(_rlz_pl_text("normal"), portfolio_session)

    assert updated == 0
    row = portfolio_session.exec(select(Portfolio)).one()
    assert row.current_price == 41000.0
    stamped = row.price_updated_at
    assert stamped is not None
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    assert stamped == old_stamp


@pytest.mark.parametrize(
    "case,expected",
    [
        # 잘림: 부분 응답이므로 아무것도 쓰지 않는다.
        ("truncated", None),
        # 보유 0건: 응답은 정상이고 쓸 시세가 없을 뿐이므로 None이 아니라 0이다.
        ("empty", 0),
    ],
)
def test_sync_portfolio_prices_guards_from_fixture(portfolio_session, case, expected):
    """잘림·보유 0건 응답에서 기존 시세를 그대로 보존한다.

    두 경우의 반환값이 다른 것이 요점이다. None은 "응답을 믿을 수 없어 건너뜀"이고
    0은 "응답은 읽었고 갱신할 것이 없었음"이다 — 호출처가 둘을 구분할 수 있어야
    "시세를 못 얻는 상태"와 "빈 계좌"가 같은 로그로 뭉개지지 않는다.

    이 테스트가 잡는 mutation: 잘림 가드 제거(삼성전자 시세가 71,200으로 덮인다),
    보유 0건을 None으로 처리하는 회귀.
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_prices_from_rlz_pl

    old_stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    portfolio_session.add(
        Portfolio(
            stock_code="005930",
            stock_name="삼성전자",
            quantity=10,
            avg_price=70000,
            current_price=60000,
            price_updated_at=old_stamp,
        )
    )
    portfolio_session.commit()

    assert _sync_portfolio_prices_from_rlz_pl(_rlz_pl_text(case), portfolio_session) == expected

    row = portfolio_session.exec(select(Portfolio)).one()
    assert row.current_price == 60000.0, "가드가 뚫려 기존 시세가 덮였습니다"


def test_sync_portfolio_prices_skips_when_marker_absent(portfolio_session, caplog):
    """[보유 종목] 섹션이 없는 응답은 계약 위반으로 보고 아무것도 쓰지 않는다.

    빈 계좌 문구조차 없으면 "보유 0건"이 아니라 "응답을 읽지 못함"이다. run_mcp_tool은
    MCP content가 비면 예외 대신 ""를 돌려주므로 이 경로는 실재한다.

    이 테스트가 잡는 mutation: 마커 부재 가드 제거 → 파서가 {}를 돌려주고 반환값이
    None 대신 0이 된다.
    """
    import logging
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_prices_from_rlz_pl

    portfolio_session.add(
        Portfolio(
            stock_code="005930",
            stock_name="삼성전자",
            quantity=10,
            avg_price=70000,
            current_price=60000,
        )
    )
    portfolio_session.commit()

    with caplog.at_level(logging.ERROR, logger="backend.scheduler"):
        result = _sync_portfolio_prices_from_rlz_pl("응답은 왔지만 종목 섹션이 없습니다.", portfolio_session)

    assert result is None
    assert portfolio_session.exec(select(Portfolio)).one().current_price == 60000.0
    assert [r for r in caplog.records if r.levelno == logging.ERROR], (
        "응답을 읽지 못한 것은 운영자가 알아야 할 계약 위반이다"
    )


def test_sync_portfolio_prices_treats_paper_fallback_as_no_quote(portfolio_session, caplog):
    """모의투자 대체 응답은 error가 아니라 "이번 주기 시세 없음"이다.

    get_balance_rlz_pl은 실전 계좌 전용이라 모의투자에서는 index.js가 get_balance
    요약으로 대체한다. 그 응답에는 [보유 종목] 마커가 없으므로, 이 분기가 없으면
    모의투자 배포에서 10분마다 error가 쌓인다 — 그 배포에서는 정상 동작인데도.

    이 테스트가 잡는 mutation: 모의투자 가드 제거 또는 마커 가드 뒤로 이동 →
    ERROR 레코드가 생겨 아래 단언이 실패한다.
    """
    import logging
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_prices_from_rlz_pl

    portfolio_session.add(
        Portfolio(
            stock_code="005930",
            stock_name="삼성전자",
            quantity=10,
            avg_price=70000,
            current_price=60000,
        )
    )
    portfolio_session.commit()

    # index.js:588-594가 실제로 붙이는 문구. get_balance 리포트 뒤에 안내가 따라온다.
    paper_text = (
        _make_balance_text(("삼성전자", "005930", 10, 70000))
        + "\n\n[안내] 모의투자(openapivts) 계좌는 실현손익 TR(v1_국내주식-041)을 "
        "지원하지 않아 잔고 요약으로 대체했습니다."
    )

    with caplog.at_level(logging.DEBUG, logger="backend.scheduler"):
        result = _sync_portfolio_prices_from_rlz_pl(paper_text, portfolio_session)

    assert result is None
    assert portfolio_session.exec(select(Portfolio)).one().current_price == 60000.0
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "모의투자 배포에서는 매 주기의 정상 동작이므로 error가 아니다"
    )


def test_sync_portfolio_from_balance_preserves_price_columns(portfolio_session):
    """잔고 동기화는 시세 컬럼을 읽지도 쓰지도 않는다 (#196의 경로 분리).

    이 불변식이 깨지면 10분 주기 잔고 동기화가 방금 채운 시세를 지우거나, 시세를
    갱신하지 않은 채 스탬프만 새로 찍어 낡은 값을 신선한 것으로 만든다.

    이 테스트가 잡는 mutation: _sync_portfolio_from_balance가 current_price 또는
    price_updated_at에 대입하는 회귀(신규 행 경로·기존 행 경로 어느 쪽이든).
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    stamp = datetime(2026, 9, 3, tzinfo=timezone.utc)
    portfolio_session.add(
        Portfolio(
            stock_code="005930",
            stock_name="삼성전자",
            quantity=10,
            avg_price=70000,
            current_price=71200,
            price_updated_at=stamp,
        )
    )
    portfolio_session.commit()

    # 수량·평단가가 바뀐 새 잔고. 시세 컬럼은 응답에 없으므로 그대로여야 한다.
    _sync_portfolio_from_balance(
        _make_balance_text(("삼성전자", "005930", 15, 70500)), portfolio_session
    )

    row = portfolio_session.exec(select(Portfolio)).one()
    assert row.quantity == 15
    assert row.avg_price == 70500.0
    assert row.current_price == 71200.0, "잔고 동기화가 시세를 덮었습니다"
    stamped = row.price_updated_at
    assert stamped is not None, "잔고 동기화가 시세 갱신 시각을 지웠습니다"
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    assert stamped == stamp, "잔고 동기화가 시세 갱신 시각을 새로 찍었습니다"


@pytest.mark.asyncio
async def test_monitor_market_task_refreshes_prices_after_balance_sync(monkeypatch):
    """감시 주기가 잔고 동기화 **뒤에** 시세 갱신을 호출한다 (#196).

    순서가 계약이다. 시세 갱신이 앞서면, 이번 주기에 새로 편입된 종목은 아직 행이
    없어 건너뛰어지고 한 주기(10분) 동안 price_known=False로 남는다.

    이 테스트가 잡는 mutation: _refresh_portfolio_prices 호출 제거, 또는 잔고 동기화
    앞으로 옮기는 회귀(call_order가 뒤집혀 실패한다).
    """
    from ..scheduler import SignalSource, monitor_market_task

    call_order: list[str] = []

    @asynccontextmanager
    async def unavailable_redis_state():
        raise ConnectionError("redis unavailable")
        yield

    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return _make_balance_text(("삼성전자", "005930", 10, 70000))
        if name == "get_balance_rlz_pl":
            return _rlz_pl_text("normal")
        return "news"

    def mock_sync_portfolio(balance_text, session, **kwargs):
        call_order.append("balance")
        return 1

    def mock_sync_prices(report_text, session):
        call_order.append("quote")
        assert report_text == _rlz_pl_text("normal"), (
            "시세 갱신이 get_balance_rlz_pl 응답이 아닌 다른 텍스트를 받았다"
        )
        return 1

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_prices_from_rlz_pl", mock_sync_prices)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert call_order == ["balance", "quote"]


@pytest.mark.asyncio
async def test_monitor_market_task_keeps_balance_sync_when_quote_tool_fails(monkeypatch, caplog):
    """시세 도구가 실패해도 잔고 동기화와 감시는 그대로 돈다 (#196).

    get_balance_rlz_pl은 실전 계좌 전용이라 배포에 따라 매 주기 실패하는 것이 정상이다.
    그 실패가 잔고 동기화를 막으면, 이미 동작하던 기능이 새 기능 때문에 멈춘다.
    실패는 error가 아니라 warning이다 — 감시도 주문도 막지 않기 때문이다.

    이 테스트가 잡는 mutation: _refresh_portfolio_prices의 try/except 제거(예외가
    _monitor_market_task 밖으로 나가 잔고 동기화 뒤 로직이 끊긴다), 또는 시세 갱신을
    잔고 동기화 앞·같은 try 안으로 옮기는 회귀.
    """
    import logging
    from ..scheduler import SignalSource, monitor_market_task

    sync_calls: list[str] = []
    # 도구 호출 순서를 그대로 남긴다. "감시가 계속 돌았다"를 score_signal 호출로 재면
    # 감시 루프가 동일 signal 중복 제거에서 멈추는 경로와 섮여 묻힌다 — 이 테스트가 재려는 것은
    # 그것이 아니라 "시세 실패 뒤에도 신호 조회까지 도달했는가"다.
    tool_calls: list[str] = []

    @asynccontextmanager
    async def unavailable_redis_state():
        raise ConnectionError("redis unavailable")
        yield

    async def mock_run_mcp_tool(params, name, args):
        tool_calls.append(name)
        if name == "get_balance":
            return _make_balance_text(("삼성전자", "005930", 10, 70000))
        if name == "get_balance_rlz_pl":
            raise RuntimeError("실전 계좌 전용 도구 거부")
        return "news"

    def mock_sync_portfolio(balance_text, session, **kwargs):
        sync_calls.append(balance_text)
        return 1

    async def mock_score_signal(stock, current, last, *, source, provider):
        return _scored(False)

    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler.score_signal", mock_score_signal)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", _resolved_broadcast_mock())

    with caplog.at_level(logging.DEBUG, logger="backend.scheduler"):
        await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert len(sync_calls) == 1, "시세 도구 실패가 잔고 동기화를 막았습니다"
    assert tool_calls == ["get_balance", "get_balance_rlz_pl", "get_market_news"], (
        "시세 도구 실패 뒤에도 신호 조회까지 진행되어야 합니다"
    )
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "시세를 못 얻는 것은 감시·주문 어느 것도 막지 않으므로 error가 아니다"
    )
