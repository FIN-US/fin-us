import pytest
import asyncio
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from ..main import app
from ..redis_state import RedisSchedulerState
from .test_balance_parser import TRUNCATION_NOTES_BY_REASON


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

    await morning_briefing_task()

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
    
    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = source
        return current != last

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
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    monkeypatch.setattr("backend.scheduler.redis_state", unavailable_redis_state)
    
    # 1. 첫 번째 실행: 뉴스가 처음이므로 분석 수행
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())
    assert mock_perform_analysis.call_count == 3 # 삼성전자, SK하이닉스, 현대차
    assert mock_broadcast.call_count == 3
    
    # 2. 두 번째 실행: 뉴스가 동일하므로 분석 스킵
    mock_perform_analysis.reset_mock()
    mock_broadcast.reset_mock()
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())
    assert mock_perform_analysis.call_count == 0
    assert mock_broadcast.call_count == 0
    
    # 3. 세 번째 실행: 뉴스가 변경된 종목이 있으면 해당 종목만 분석
    async def mock_run_mcp_tool_changed(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주\n- SK하이닉스 (000660): 2주\n- 현대차 (005380): 1주"
        if args.get('stock_name') == "삼성전자":
            return "NEW NEWS for Samsung"
        return f"Latest news for {args.get('stock_name')}"
        
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool_changed)
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())
    assert mock_perform_analysis.call_count == 1
    assert mock_broadcast.call_count == 1
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

    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return True

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
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", mock_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, object(), state)

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return True

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
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", mock_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, object(), state)

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return True

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
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", mock_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, object(), state)

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return True

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
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.telegram_notifier.send_analysis_alert", failing_telegram)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await _monitor_signal("삼성전자", source, object(), state)

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return False

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
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert len(DEFAULT_MONITOR_STOCKS) == 4
    assert monitored_stocks == DEFAULT_MONITOR_STOCKS
    assert mock_perform_analysis.call_count == 0
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

    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return True

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
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())
    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert mock_perform_analysis.call_count == 1
    assert mock_broadcast.call_count == 1


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

    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return True

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked"})
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)

    monkeypatch.setattr("backend.scheduler.SIGNAL_SOURCES", sources)
    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert mock_perform_analysis.call_count == 2
    assert [call.kwargs["trigger_source"] for call in mock_perform_analysis.call_args_list] == ["news", "sns"]
    assert [call.kwargs["trigger_signal"] for call in mock_perform_analysis.call_args_list] == [
        "get_market_news: 삼성전자",
        "get_stock_mentions: 삼성전자",
    ]
    assert [call.args[0]["source"] for call in mock_broadcast.call_args_list] == ["news", "sns"]


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

    async def mock_check_significance(stock, current, last, *, source, provider):
        _ = stock, current, last, source, provider
        return True

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
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)

    await asyncio.gather(
        monitor_market_task(watchlist_repo=FakeWatchlistRepo()),
        monitor_market_task(watchlist_repo=FakeWatchlistRepo()),
    )

    assert mock_perform_analysis.call_count == 1
    assert mock_broadcast.call_count == 1


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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo())

    assert monitored_stocks == ["삼성전자"]


@pytest.fixture(autouse=True)
def reset_balance_failure_streak(monkeypatch):
    """모듈 전역 카운터가 테스트 간에 새지 않도록 매 테스트 시작 시 0으로 고정한다."""
    monkeypatch.setattr("backend.scheduler._balance_failure_streak", 0)
    monkeypatch.setattr("backend.scheduler._last_balance_error", None)


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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool_fn)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2


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
