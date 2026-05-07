import pytest
import asyncio
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from ..main import app

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
    monitor_market_task가 뉴스가 새로운 경우에만 분석을 수행하는지 확인합니다.
    """
    from ..scheduler import monitor_market_task, last_analyzed_news_cache
    
    # 캐시 초기화
    last_analyzed_news_cache.clear()
    
    # 모킹
    async def mock_run_mcp_tool(params, name, args):
        if name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주\n- SK하이닉스 (000660): 2주\n- 현대차 (005380): 1주"
        return f"Latest news for {args['stock_name']}"
    
    async def mock_check_significance(stock, current, last, provider):
        return current != last

    mock_perform_analysis = MagicMock(return_value=asyncio.Future())
    mock_perform_analysis.return_value.set_result({"summary": "Mocked", "details": {"decision": "BUY"}})
    
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)
    
    monkeypatch.setattr("backend.scheduler.run_mcp_tool", mock_run_mcp_tool)
    monkeypatch.setattr("backend.scheduler.check_news_significance", mock_check_significance)
    monkeypatch.setattr("backend.scheduler.perform_stock_analysis", mock_perform_analysis)
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    
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
