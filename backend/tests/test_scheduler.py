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
    """
    from sqlmodel import select
    from ..models import Portfolio
    from ..scheduler import _sync_portfolio_from_balance

    text = _make_balance_text(("삼성전자", "005930", 10, 70000))
    _sync_portfolio_from_balance(text, portfolio_session)

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

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

    async def mock_check_significance(stock, current, last, *, source, provider):
        return False

    monkeypatch.setattr("backend.scheduler.redis_state", fake_redis_state)
    monkeypatch.setattr(
        "backend.scheduler.SIGNAL_SOURCES",
        [SignalSource(name="news", mcp_params=object(), tool_name="get_market_news")],
    )
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler._sync_portfolio_from_balance", mock_sync_portfolio)
    monkeypatch.setattr("backend.scheduler.check_signal_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", MagicMock(return_value=asyncio.Future()))

    await monitor_market_task(watchlist_repo=FakeWatchlistRepo(["카카오"]))

    assert sync_calls == []


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

    마커 가드가 "실제 0건"을 막아서는 안 됩니다.
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
    _sync_portfolio_from_balance(empty_holdings_text, portfolio_session)

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