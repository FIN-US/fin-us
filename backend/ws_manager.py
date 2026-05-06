import logging
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

class ConnectionManager:
    """
    WebSocket 연결을 관리하고 실시간 브로드캐스트 기능을 제공합니다.
    """
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"New WebSocket connection. Total active: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"WebSocket disconnected. Remaining: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """
        접속된 모든 클라이언트에게 JSON 메시지를 전송합니다.
        """
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to WebSocket: {e}")
                # 문제 있는 커넥션은 연결 목록에서 제거 시도 (선택 사항)

# 싱글톤 인스턴스 생성
manager = ConnectionManager()
