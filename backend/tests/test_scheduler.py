import pytest
import asyncio
from contextlib import asynccontextmanager
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from ..main import app
from ..redis_state import RedisSchedulerState

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
    await monitor_market_task()
    assert mock_perform_analysis.call_count == 3 # 삼성전자, SK하이닉스, 현대차
    assert mock_broadcast.call_count == 3
    
    # 2. 두 번째 실행: 뉴스가 동일하므로 분석 스킵
    mock_perform_analysis.reset_mock()
    mock_broadcast.reset_mock()
    await monitor_market_task()
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
    await monitor_market_task()
    assert mock_perform_analysis.call_count == 1
    assert mock_broadcast.call_count == 1
    assert mock_perform_analysis.call_args[0][0] == "삼성전자"


class FakeRedis:
    def __init__(self):
        self.store = {}

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

    await monitor_market_task()

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

    await monitor_market_task()
    await monitor_market_task()

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

    await monitor_market_task()

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

    await monitor_market_task()

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

    await asyncio.gather(monitor_market_task(), monitor_market_task())

    assert mock_perform_analysis.call_count == 1
    assert mock_broadcast.call_count == 1
