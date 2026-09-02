import json
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class BroadcastSocket(Protocol):
    """브로드캐스트가 커넥션에 요구하는 전부 (#319).

    fastapi의 ``WebSocket``을 그대로 선언 타입으로 두면 이 매니저와 아무 관계 없는
    것들(``receive``·``scope``·``client_state`` …)까지 계약이 되어, send_text만 갖춘
    대역이 목록에 들어가지 못한다. 실제로 test_ws_manager가 다섯 자리에서 cast로
    검사를 껐고, cast는 그 자리에서 이름·인자 모양의 어긋남까지 함께 덮는다.

    ``accept``는 여기 없다. 수용은 커넥션을 목록에 넣기 전 한 번뿐인 일이라 아래
    :class:`AcceptingSocket`이 따로 요구한다 — 브로드캐스트만 검증하는 대역이 쓰지도
    않는 메서드를 갖추게 만들면 계약을 좁힌 의미가 없다.

    ``data``를 위치 전용(``/``)으로 둔다. broadcast는 위치 인자로만 부르므로 인자
    이름은 계약이 아니고, 이름까지 고정하면 대역이 ``payload`` 같은 자기 이름을
    쓰지 못한다.
    """

    async def send_text(self, data: str, /) -> None: ...


class AcceptingSocket(BroadcastSocket, Protocol):
    """:meth:`ConnectionManager.connect`가 요구하는 계약.

    수용까지 맡는 자리라 ``accept``가 더 붙는다. 실제 호출부는 main.py의 엔드포인트
    하나이고 거기서는 fastapi의 ``WebSocket``이 그대로 들어온다.
    """

    async def accept(self) -> None: ...


class ConnectionManager:
    """
    WebSocket 연결을 관리하고 실시간 브로드캐스트 기능을 제공합니다.
    """
    def __init__(self):
        self.active_connections: list[BroadcastSocket] = []

    async def connect(self, websocket: AcceptingSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"New WebSocket connection. Total active: {len(self.active_connections)}")

    def disconnect(self, websocket: BroadcastSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"WebSocket disconnected. Remaining: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        """
        접속된 모든 클라이언트에게 JSON 메시지를 전송합니다.
        """
        # 직렬화를 루프 밖에서 한 번만 한다. send_json은 커넥션마다 같은 dict를 다시
        # json.dumps하기도 하지만, 그보다 중요한 것은 실패 지점의 분리다. 직렬화 실패는
        # payload를 만든 쪽의 버그이지 개별 커넥션의 문제가 아닌데, 루프 안에서 터지면
        # 커넥션 수만큼 같은 에러가 찍히고 아래 실패 커넥션 제거가 멀쩡한 소켓을 전부
        # 걷어내 버린다. 여기서 걸러 내면 그 두 가지가 모두 생기지 않는다.
        # 예외를 올리지 않고 로그만 남기는 것은 기존 동작(루프 안 except가 삼킴)과 같다 —
        # 호출처(scheduler)는 브로드캐스트 실패로 감시 루프가 멈추지 않기를 기대한다.
        # separators와 ensure_ascii는 Starlette send_json과 와이어 포맷을 맞추기 위한
        # 것이다. ensure_ascii를 기본값(True)으로 두면 "삼성전자" 같은 한글이 유니코드
        # 이스케이프로 나가 payload가 3배로 부풀고, 같은 엔드포인트의
        # 에코 응답(main.py — 여전히 send_json)과 인코딩이 갈린다. 디코더에는 동등하지만
        # 직렬화 위치를 옮기는 변경이 프로토콜을 건드릴 이유는 없다(PR #261 리뷰).
        try:
            payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.error("WebSocket 브로드캐스트 payload 직렬화 실패: %s", e, exc_info=True)
            return

        # 원본이 아니라 스냅샷을 순회한다. 루프 안에서 await하는 동안 다른 커넥션의
        # 수신 루프가 WebSocketDisconnect를 받아 disconnect()를 부르면 리스트가 줄어드는데,
        # 원본을 순회 중이면 인덱스가 밀려 다음 커넥션 하나가 조용히 건너뛰어진다.
        # 누수가 아니라 "그 클라이언트만 이 브로드캐스트 1건을 못 받는" 증상이라
        # 로그에도 남지 않는다.
        for connection in list(self.active_connections):
            try:
                await connection.send_text(payload)
            except Exception as e:
                # 전송이 실패한 커넥션은 여기서 바로 뺀다. 정상 경로에서는 수신 루프
                # (main.py websocket_endpoint)가 WebSocketDisconnect를 받아 disconnect()를
                # 부르므로 결국 빠지지만, 그 사이의 브로드캐스트마다 같은 죽은 소켓에
                # 전송을 시도하며 에러 로그를 남긴다. disconnect()는 멱등이라 수신 루프가
                # 나중에 다시 불러도 안전하다.
                logger.error(f"Error broadcasting to WebSocket: {e}")
                self.disconnect(connection)

# 싱글톤 인스턴스 생성
manager = ConnectionManager()
