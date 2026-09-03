import httpx
import pytest

from backend import services, stock_code


@pytest.fixture
def failing_telegram_client():
    """텔레그램 호출을 상태 오류로 실패시키는 가짜 httpx 클라이언트 팩토리 (#257).

    호출부가 만든 URL을 그대로 받아 httpx 응답을 세우고 raise_for_status를 태운다.
    URL을 테스트가 지어내지 않는 것이 핵심이다 — 토큰이 실제로 URL에 실려 나가고
    예외 메시지에 들어앉는 경로를 그대로 통과시켜야 리댁션을 검증한 것이 된다.

    전송(telegram_notifier)과 폴링(telegram_commands) 양쪽이 같은 팩토리를 쓴다.
    """

    def _factory(status_code, body):
        class FakeResponse:
            def __init__(self, url):
                self._inner = httpx.Response(
                    status_code, json=body, request=httpx.Request("POST", url)
                )

            def raise_for_status(self):
                self._inner.raise_for_status()

            def json(self):
                return body

        class FakeAsyncClient:
            def __init__(self, *, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def post(self, url, **kwargs):
                return FakeResponse(url)

        return FakeAsyncClient

    return _factory


@pytest.fixture(autouse=True)
def _clear_stock_code_cache():
    # 이 픽스처가 막는 것은 캐시 오염(테스트 간 종목코드 캐시 누수)뿐이다.
    # 어떤 테스트가 run_mcp_tool을 몽키패치하지 않아 실제 MCP stdio 서브프로세스를
    # 띄우는 회귀는 이 픽스처로 잡히지 않고, `-s` 없이는 그 서브프로세스 실행 자체도
    # 화면에 보이지 않는다. 실제 방어는 test_database.py의 run_mcp_tool 몽키패치다.
    services._stock_code_cache.clear()
    yield
    services._stock_code_cache.clear()


@pytest.fixture(autouse=True)
def _clear_master_code_cache():
    # #151: _is_known_master_code의 지연 로딩 캐시도 테스트 간에 새면 안 된다 —
    # 한 테스트가 _TRADING_MCP_DIR을 몽키패치해 로드 실패(fail-open)를
    # 캐시해 버리면, 몽키패치가 되돌아간 뒤에도 다음 테스트가 그 실패 상태를
    # 그대로 물려받아 실제 마스터 대조를 건너뛰게 된다.
    # _master_codes_cache_path는 _master_codes_cache가 None이면 어차피 무시되지만
    # (첫 조건에서 단락 평가), 둘을 짝으로 리셋해 상태를 명확히 남긴다.
    stock_code._master_codes_cache = None
    stock_code._master_codes_cache_path = None
    yield
    stock_code._master_codes_cache = None
    stock_code._master_codes_cache_path = None


@pytest.fixture(autouse=True)
def _api_key_auth_off_by_default(monkeypatch):
    """API 인증(#266 2단계)을 끈 상태를 테스트의 기본값으로 고정한다.

    backend/config.py는 레포 루트의 실제 `.env`를 load_dotenv로 읽는다. 그래서 개발자가
    자기 `.env`에 FINUS_API_KEY를 채워 두면 config.FINUS_API_KEY가 그 값이 되고,
    `/api/`를 부르는 기존 테스트(test_market_data_routes 등)가 **그 머신에서만** 401로
    떨어진다. 테스트 결과가 작업 트리 밖의 파일에 좌우되는 상태를 만들지 않는다.

    인증 자체를 보는 테스트(test_api_key_auth.py)는 같은 이름을 다시 monkeypatch해서
    켠다 — 나중에 적용한 patch가 이기고, 되돌리기는 LIFO라 여기 값도 함께 복원된다.
    """
    import backend.main as main

    monkeypatch.setattr(main, "FINUS_API_KEY", "")
