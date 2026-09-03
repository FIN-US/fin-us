"""#266 2단계: `/api/` 와 `/api/v1/ws` 의 정적 API 키 인증.

1단계(nginx 레이트리밋)는 **비용의 뚜껑**이지 접근 제어가 아니었다. 여기서 고정하는
것이 접근 제어다. #266이 표로 남긴 잔여 위험 중 "비브라우저(`curl`·`wscat`)는 열려
있음" 행을 닫는 것이 이 검사이므로, 조용히 무력해지면 이슈가 닫혔다고 착각한 채
그 행이 다시 열린다.

인증은 `FINUS_API_KEY`가 설정된 배포에서만 걸린다(미설정이 기본값 — 근거는
`backend/config.py`의 해당 주석). 그래서 여기서는 **켠 상태**와 **끈 상태** 양쪽을
모두 본다. 끈 상태를 보지 않으면 "언제나 401"이라는 회귀가 통과해 버린다.

`TestClient`를 `with` 없이 쓴다. `with`는 `main.py`의 lifespan을 실행해 `init_db()`·
`start_scheduler()`까지 돌리는데, 그러면 테스트 도중 감시 루프가 실제로 떠서 MCP
호출을 시도하고 `backend/finus.db`가 생긴다(test_websocket_origin.py와 같은 이유).
여기서 부르는 경로는 전부 lifespan 없이 동작한다.
"""

from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from backend.main import API_KEY_HEADER, app, is_authorized_api_call, matches_api_key


_KEY = "test-secret-key"

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NAT_FINUS_API = _REPO_ROOT / "finus_nat" / "src" / "nat_finus_nat" / "finus_api.py"


@pytest.fixture
def auth_on(monkeypatch):
    """이 파일의 대부분이 쓰는 "인증 켜짐" 상태.

    conftest의 autouse 픽스처가 먼저 빈 문자열로 꺼 두므로, 여기서 다시 덮어쓴다.
    """
    monkeypatch.setattr("backend.main.FINUS_API_KEY", _KEY)
    return _KEY


# --- 판정 함수 단위 테스트 -------------------------------------------------


@pytest.mark.parametrize(
    "presented",
    [
        # 접두사·접미사가 붙은 값. startswith/in 같은 느슨한 비교로 바꾸면 통과한다.
        _KEY + "x",
        "x" + _KEY,
        _KEY[:-1],
        # 대소문자만 다른 값. 키는 대소문자를 구분한다.
        _KEY.upper(),
        # 빈 문자열과 헤더 부재. 둘 다 "키를 제시하지 않았다"이다.
        "",
        None,
        # 비ASCII 값. hmac.compare_digest는 str을 받으면 ASCII만 허용하고 그 밖에는
        # TypeError를 낸다. 바이트로 인코딩하지 않으면 이 입력이 False가 아니라 예외가
        # 되고, 미들웨어에서는 401이 아니라 500으로 나간다.
        "틀린 키",
    ],
)
def test_matches_api_key_demands_full_equality(auth_on, presented):
    assert matches_api_key(presented) is False


def test_matches_api_key_accepts_the_configured_key(auth_on):
    assert matches_api_key(_KEY) is True


def test_matches_api_key_is_false_when_no_key_is_configured():
    """키가 설정되지 않은 상태에서는 무엇을 제시하든 대조가 실패합니다.

    "인증이 꺼져 있다"는 판정은 이 함수가 아니라 호출부(is_authorized_api_call)가 한다.
    둘을 한 함수에 섞으면 키 미설정이 "아무 키나 맞음"으로 읽히는 자리가 생기고, 그
    자리는 나중에 인증을 켰을 때 조용히 열려 있다.

    이 테스트가 잡는 mutation: `if not FINUS_API_KEY ...: return False`를 `return True`로
    뒤집는 회귀.
    """
    # conftest의 autouse 픽스처가 이미 꺼 둔 상태다.
    assert matches_api_key("아무 값") is False
    assert matches_api_key(None) is False


def test_is_authorized_api_call_lets_everything_through_when_auth_is_off():
    assert is_authorized_api_call(None) is True
    assert is_authorized_api_call("아무 값") is True


def test_is_authorized_api_call_requires_the_key_when_auth_is_on(auth_on):
    assert is_authorized_api_call(_KEY) is True
    assert is_authorized_api_call(None) is False
    assert is_authorized_api_call("wrong-key") is False
    assert is_authorized_api_call("틀린 키") is False


# --- HTTP 미들웨어 --------------------------------------------------------


@pytest.fixture
def stub_news(monkeypatch):
    """`/api/v1/news`가 MCP 서브프로세스를 띄우지 않게 한다.

    인증을 보는 파일이라 뒤쪽 핸들러가 실제로 무엇을 하는지는 중요하지 않다. 다만
    "통과했다"를 200으로 확인하려면 핸들러가 성공해야 하므로, DB를 타지 않는 라우트
    하나를 골라 그 MCP 호출만 막는다.
    """

    async def _fake_run_mcp_tool(params, tool_name, arguments):
        return "뉴스1"

    monkeypatch.setattr("backend.main.run_mcp_tool", _fake_run_mcp_tool)


def test_guarded_path_rejects_a_request_without_the_key(auth_on, stub_news):
    """키 없는 `/api/` 호출은 401로 거부됩니다.

    이 테스트가 잡는 mutation: 미들웨어의 조건을 지우거나 항상 통과로 바꾸는 회귀.
    """
    response = TestClient(app).get("/api/v1/news", params={"stock": "삼성전자"})

    assert response.status_code == 401
    # 본문 모양은 FastAPI의 detail·nginx의 429 본문(#266 1단계)과 같은 계약이다.
    # Unity의 ApiClient.ExtractErrorMessage가 이 키를 읽어 배너에 싣는다.
    assert list(response.json()) == ["detail"]
    assert isinstance(response.json()["detail"], str)


def test_guarded_path_rejects_a_wrong_key(auth_on, stub_news):
    response = TestClient(app).get(
        "/api/v1/news",
        params={"stock": "삼성전자"},
        # 여기서는 ASCII 값을 쓴다. httpx가 헤더 값을 ascii로 인코딩하므로 비ASCII
        # 키는 클라이언트에서 먼저 막혀 서버 판정에 닿지 못한다 — 그쪽 입력은 위
        # 단위 테스트가 본다.
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 401


def test_guarded_path_accepts_the_configured_key(auth_on, stub_news):
    response = TestClient(app).get(
        "/api/v1/news",
        params={"stock": "삼성전자"},
        headers={"X-API-Key": _KEY},
    )
    assert response.status_code == 200


def test_api_stays_open_when_auth_is_off(stub_news):
    """키를 설정하지 않은 배포에서는 헤더 없이도 그대로 열려 있습니다.

    기본값이 "꺼짐"인 것은 의도다(config.FINUS_API_KEY 주석). 이 테스트가 잡는
    mutation: 미들웨어가 `api_auth_enabled()`를 보지 않고 언제나 키를 요구하게 되는
    회귀 — 그러면 `docker compose up`이 그대로 401 화면이 된다.
    """
    response = TestClient(app).get("/api/v1/news", params={"stock": "삼성전자"})
    assert response.status_code == 200


def test_the_guard_runs_before_routing(auth_on):
    """존재하지 않는 `/api/` 경로도 키가 없으면 404가 아니라 401입니다.

    라우팅보다 먼저 걸린다는 성질을 고정한다. 미들웨어 대신 라우트별 Depends로 옮기면
    이 요청이 404가 되어, 키를 모르는 호출자가 어떤 엔드포인트가 있는지 훑을 수 있다.
    키가 있으면 그때는 정상적으로 404가 나온다 — 즉 401이 "경로가 없어서"가 아니다.
    """
    client = TestClient(app)

    assert client.get("/api/v1/이런-라우트는-없다").status_code == 401
    assert (
        client.get("/api/v1/이런-라우트는-없다", headers={"X-API-Key": _KEY}).status_code
        == 404
    )


@pytest.mark.parametrize("path", ["/openapi.json", "/docs"])
def test_the_schema_stays_open_by_decision_not_by_accident(auth_on, path):
    """`/openapi.json`·`/docs`는 인증을 켜도 열려 있습니다 — 의도된 경계입니다.

    둘 다 `/api/` 접두사 밖이라 미들웨어를 타지 않습니다. 8080에서는 nginx의
    `location /`가 `try_files ... =404`로 끝내지만, **8000에 직접 닿는 호출자에게는 전체
    스키마가 그대로 나갑니다**(PR #352 리뷰). 함께 닫지 않은 이유는 `backend/main.py`의
    미들웨어 docstring에 있습니다 — docker-compose가 8000 게시를 남기는 이유로 `/docs`를
    명시하고 있고, 스키마는 이 저장소의 소스 그 자체라 비밀이 아닙니다.

    이 테스트는 보호가 아니라 **결정의 기록**입니다. 나중에 스키마까지 닫기로 하면 여기가
    빨간불이 되어, 그때 사람이 이 판단을 다시 꺼내 보게 됩니다. 반대로 이 테스트가 없으면
    "빠뜨린 것"과 "일부러 둔 것"을 구분할 수 없습니다.
    """
    assert TestClient(app).get(path).status_code == 200


def test_the_nat_client_uses_the_same_header_name():
    """NAT가 쓰는 헤더 이름이 backend의 API_KEY_HEADER와 같은지 소스에서 대조합니다.

    `finus_nat`은 별도 패키지라 backend의 상수를 import할 수 없고, 그래서 `"X-API-Key"`
    리터럴이 두 벌 존재합니다. 양쪽 테스트는 각자 자기 리터럴만 확인하므로 한쪽 이름을
    바꿔도 둘 다 초록불인 채 **인증을 켠 날에야** 매매일지 저장이 401로 드러납니다
    (PR #352 리뷰).

    소스를 문자열로 읽어 고정하는 것은 이 저장소의 선례를 따른 것입니다 —
    `test_compose_ports.py`가 `docker-compose.yml`을, `test_nginx_rate_limit.py`가
    `nginx.conf`를 같은 방식으로 읽습니다. 실행 경계를 넘는 계약은 그쪽에서 import할 수
    없으니 텍스트로라도 묶어 둡니다.
    """
    source = _NAT_FINUS_API.read_text(encoding="utf-8")

    assert f'"{API_KEY_HEADER}"' in source, (
        f"{_NAT_FINUS_API.name}에 backend의 API_KEY_HEADER({API_KEY_HEADER!r})가 없습니다. "
        "한쪽 헤더 이름만 바뀌면 인증을 켠 배포에서 NAT의 매매일지 저장이 401로 떨어집니다."
    )


def test_health_stays_open_so_the_container_can_check_itself(auth_on):
    """`/health`는 인증을 켜도 열려 있습니다.

    compose 헬스체크가 `curl -f http://127.0.0.1:8000/health`로 부른다
    (docker-compose.yml). 여기에 키를 요구하면 컨테이너가 스스로를 unhealthy로 만들고,
    `backend`가 healthy가 되기를 기다리는 서비스까지 함께 멈춘다. 응답에는
    `{"status": "alive"}` 말고 아무것도 없다(#252 리뷰에서 nat_base_url을 뺐다).

    이 테스트가 잡는 mutation: 보호 접두사를 `/api/`에서 `/`로 넓히는 회귀.
    """
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


# --- WebSocket ------------------------------------------------------------


def test_websocket_rejects_an_allowed_origin_without_the_key(auth_on, monkeypatch):
    """Origin이 허용 목록에 있어도 키가 없으면 핸드셰이크가 거부됩니다.

    두 검사는 막는 것이 다르므로 둘 다 필요하다 — Origin 검사(#256)는 키를 모르는
    브라우저를, 키 검사(#266 2단계)는 Origin을 보내지 않거나 위조하는 비브라우저를
    막는다. 이 테스트가 잡는 mutation: 키 검사를 지우거나 Origin 검사 통과를 곧
    인증 통과로 취급하는 회귀.
    """
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    client = TestClient(app)

    # 블록 안에서 아무것도 하지 않는 것이 핵심이다(test_websocket_origin.py와 같은 이유).
    # accept() 전에 close()하면 websocket_connect() 진입 자체가 실패하므로, 거부를
    # accept() 뒤로 옮기는 회귀가 여기서 DID NOT RAISE로 드러난다.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/ws", headers={"origin": "http://localhost:8080"}
        ):
            pass


def test_websocket_rejects_a_wrong_key(auth_on, monkeypatch):
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/ws?api_key=wrong", headers={"origin": "http://localhost:8080"}
        ):
            pass


def test_websocket_accepts_the_key_as_a_query_parameter(auth_on, monkeypatch):
    """키는 쿼리 파라미터로 받습니다.

    헤더가 아닌 것은 취향이 아니라 제약이다 — 브라우저 WebSocket API에는 커스텀 헤더를
    붙일 자리가 없다(#266 방향 결정 코멘트). 전달 경로가 REST와 다르다는 사실 자체를
    고정해 둔다.
    """
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    client = TestClient(app)

    with client.websocket_connect(
        f"/api/v1/ws?api_key={_KEY}", headers={"origin": "http://localhost:8080"}
    ) as websocket:
        websocket.send_text("ping")
        assert websocket.receive_json() == {"status": "received", "message": "ping"}


def test_websocket_key_does_not_override_the_origin_check(auth_on, monkeypatch):
    """올바른 키를 들고 와도 허용되지 않은 Origin이면 거부됩니다.

    이 테스트가 잡는 mutation: 두 검사를 `or`로 묶어 어느 한쪽만 통과해도 연결되게
    만드는 회귀. 그러면 임의 사이트가 키를 알아낸 순간 브라우저 경로가 다시 열린다.
    """
    monkeypatch.setattr("backend.main.ALLOW_ORIGINS", ["http://localhost:8080"])
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/v1/ws?api_key={_KEY}", headers={"origin": "http://evil.example"}
        ):
            pass


def test_websocket_without_origin_needs_the_key(auth_on):
    """Origin을 보내지 않는 클라이언트(`curl`·`wscat`)는 이제 키를 요구받습니다.

    #266이 표로 남긴 "비브라우저는 열려 있음" 행이 정확히 여기서 닫힌다.
    `is_allowed_ws_origin(None)`은 여전히 True이므로(그 계약은 #256 그대로다),
    이 요청을 막는 것은 키 검사뿐이다.
    """
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/api/v1/ws"):
            pass


def test_websocket_without_origin_stays_open_when_auth_is_off():
    """인증을 끈 배포에서는 #256 이전의 계약이 그대로입니다.

    켜짐/꺼짐 양쪽을 다 보지 않으면 "언제나 거부"라는 회귀가 위 테스트들을 전부
    통과해 버린다.
    """
    with TestClient(app).websocket_connect("/api/v1/ws") as websocket:
        websocket.send_text("ping")
        assert websocket.receive_json() == {"status": "received", "message": "ping"}
