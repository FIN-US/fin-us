"""#256: /api/v1/ws 핸드셰이크의 Origin 허용목록 검사.

Starlette의 CORSMiddleware는 WebSocket 핸드셰이크에 적용되지 않아, ALLOW_ORIGINS로
HTTP를 조여도 WS는 그대로 열려 있었다(Cross-Site WebSocket Hijacking). 그 비대칭을
없앤 검사가 다시 깨지지 않도록, 같은 함수(is_allowed_ws_origin)를 읽는 엔드포인트
양쪽에서 본다.

#246에서 CORSMiddleware를 제거한 뒤로는 ALLOW_ORIGINS의 소비자가 이 검사 하나뿐이다.
즉 여기가 빨간불이 되지 않으면 그 목록이 아무 데도 쓰이지 않는 설정처럼 보인다.
"""

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from backend.main import app, is_allowed_ws_origin


# --- 판정 함수 단위 테스트 -------------------------------------------------
#
# 엔드포인트 테스트만 두면 ALLOW_ORIGINS를 monkeypatch해야 하는데, main.py가 config에서
# 이름을 직접 import해 와 패치 지점이 모듈 전역 하나로 고정된다. 판정 로직 자체는 함수로
# 분리해 두고 여기서 직접 본다.


def test_missing_origin_is_allowed():
    """Origin 헤더가 없으면 허용합니다.

    Origin은 브라우저가 붙이는 헤더이고 CSWSH는 브라우저 공격이다. 없음을 거부로 다루면
    curl·wscat·헬스체크 같은 비브라우저 클라이언트만 끊기고 위협은 그대로 남는다.

    이 테스트가 잡는 mutation: `if origin is None: return True`를 `return False`로
    뒤집거나 아예 지워 "Origin 없음"이 거부로 떨어지는 회귀.
    """
    assert is_allowed_ws_origin(None) is True


def test_listed_origin_is_allowed(monkeypatch):
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    assert is_allowed_ws_origin("http://localhost:8080") is True


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        # 스킴만 다른 경우. 문자열 완전 일치이므로 https는 별개 오리진이다.
        "https://localhost:8080",
        # 포트만 다른 경우.
        "http://localhost:9090",
        # 허용 오리진을 접두사로 갖는 오리진. startswith 같은 느슨한 비교로 바꾸면
        # 통과해 버린다.
        "http://localhost:8080.evil.example",
        # 빈 문자열은 "없음"이 아니다. None만 통과시킨다는 계약을 고정한다.
        "",
    ],
)
def test_unlisted_origin_is_rejected(monkeypatch, origin):
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    assert is_allowed_ws_origin(origin) is False


def test_wildcard_allows_any_origin(monkeypatch):
    """ALLOW_ORIGINS에 "*"가 있으면 전체 허용합니다.

    "*"는 전체 허용이다. 제거된 CORSMiddleware가 쓰던 관례를 그대로 따른다(#246) —
    표기의 의미만 바꾸면 기존 `.env`가 조용히 다른 뜻이 되기 때문이다.
    해석이 갈리면 HTTP는 열려 있는데 WS만 막히는 상태가 되어 원인을 찾기 어렵다.
    """
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["*"])
    assert is_allowed_ws_origin("http://evil.example") is True


# --- 엔드포인트 통합 테스트 -------------------------------------------------
#
# TestClient를 with 없이 쓴다. with는 main.py의 lifespan을 실행해 init_db()·
# start_scheduler()까지 돌리는데, 그러면 테스트 도중 monitor_market_task가 실제로 떠서
# MCP 호출을 시도하고 backend/finus.db가 생긴다. websocket_connect는 lifespan 없이도
# 동작하므로 이 파일에는 필요 없다(PR #261 리뷰 🔵2).


def test_websocket_rejects_disallowed_origin(monkeypatch):
    """허용되지 않은 Origin은 핸드셰이크 자체가 거부됩니다.

    이 테스트가 잡는 mutation: 거부 처리를 manager.connect(websocket) 뒤로 옮기는 회귀.
    그러면 핸드셰이크가 완성돼 커넥션이 잠시 active_connections에 들어가고, 그사이
    브로드캐스트가 나갈 수 있다.
    """
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    client = TestClient(app)

    # 블록 안에서 receive_text()를 부르지 않는 것이 이 검사의 핵심이다. accept() 전에
    # close()하면 핸드셰이크가 완성되지 않아 websocket_connect() 진입 자체가 실패한다.
    # 거부를 accept() 뒤로 옮기면 진입은 성공하고 이 pytest.raises가 DID NOT RAISE로
    # 떨어진다. receive_text()를 부르면 accept 후 거부에서도 거기서 예외가 나므로
    # pytest.raises가 두 경우를 구분하지 못한다 — 지키려는 성질이 순서인데 순서를 보지
    # 못하게 된다(PR #261 리뷰 🟡1).
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/ws", headers={"origin": "http://evil.example"}
        ):
            pass


def test_websocket_accepts_allowed_origin(monkeypatch):
    """허용 목록에 있는 Origin은 정상 연결되고 에코 응답을 받습니다."""
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    client = TestClient(app)

    with client.websocket_connect(
        "/api/v1/ws", headers={"origin": "http://localhost:8080"}
    ) as websocket:
        websocket.send_text("ping")
        assert websocket.receive_json() == {"status": "received", "message": "ping"}


def test_websocket_rejection_does_not_register_connection(monkeypatch):
    """거부된 연결은 ConnectionManager에 등록되지 않습니다.

    active_connections에 남으면 이후 broadcast가 죽은 소켓으로 계속 전송을 시도한다.
    """
    from backend.ws_manager import manager

    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    client = TestClient(app)
    before = len(manager.active_connections)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/ws", headers={"origin": "http://evil.example"}
        ):
            pass

    assert len(manager.active_connections) == before
