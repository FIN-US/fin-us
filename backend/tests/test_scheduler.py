import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
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

def test_ping_task_execution(monkeypatch):
    """
    ping_task가 실행될 때 WebSocket 브로드캐스트가 호출되는지 확인합니다.
    """
    import asyncio
    from ..scheduler import ping_task
    
    mock_broadcast = MagicMock(return_value=asyncio.Future())
    mock_broadcast.return_value.set_result(None)
    
    monkeypatch.setattr("backend.scheduler.manager.broadcast", mock_broadcast)
    
    # 비동기 함수 실행
    asyncio.run(ping_task())
    
    # 브로드캐스트가 호출되었는지 확인
    mock_broadcast.assert_called_once()
    args = mock_broadcast.call_args[0][0]
    assert args["type"] == "SYSTEM_PING"
    assert "Scheduler is alive" in args["message"]
