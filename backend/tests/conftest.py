import json
from typing import Any

import httpx
import pytest

from backend import services, stock_code, telegram_commands
from backend.trading_orders import TradeRecorder


class RecordedHttpx:
    """``MockTransport``가 받아 둔 요청들 — 테스트의 단언은 여기서 읽는다 (#360)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        # 마지막으로 만들어진 클라이언트의 생성 인자. timeout처럼 요청 객체에는 남지
        # 않지만 계약인 값을 여기서 본다.
        self.client_kwargs: dict[str, Any] = {}

    @property
    def calls(self) -> list[tuple[str, Any]]:
        """요청마다 ``(URL, JSON 본문)``. 본문 없이 보낸 요청은 ``None``이다.

        본문은 프로덕션이 실제로 직렬화해 보낸 바이트에서 읽는다.
        """
        return [
            (str(request.url), json.loads(request.content) if request.content else None)
            for request in self.requests
        ]


@pytest.fixture
def mock_httpx(monkeypatch):
    """``httpx.AsyncClient``가 ``MockTransport``를 타게 만든다 (#360).

    손으로 흉내 낸 ``async def post(self, url, *, json)`` 대역은 프로덕션의 **호출
    시그니처를 복제**한다. 인자가 하나 붙으면 대역이 ``TypeError``를 던지는데, 텔레그램
    관문의 넓은 ``except Exception``이 그걸 전송 실패로 접는다 — 성공 경로 테스트는
    엉뚱한 단언에서 깨지고, 실패 경로 테스트는 틀린 이유로 초록인 채 남는다. finus_nat이
    #357에서 겪은 사고와 같은 양상이다. ``MockTransport``는 조립이 끝난
    ``httpx.Request``를 받으므로 호출 시그니처와 무관하다.

    뼈대는 finus_nat/tests/conftest.py의 ``mock_backend``와 같지만 응답 지정은 다시 썼다.
    backend 쪽은 분할 전송(#313)처럼 한 테스트가 요청을 여러 번 태우고 "n번째에서 실패"를
    세워야 해서, 고정 응답 하나가 아니라 응답 순서를 받는다.

    ``responses``를 요청 순서대로 돌려준다. 마지막 응답은 이후 요청에서 계속 재사용되고,
    비우면 ``{"ok": True}`` 200이다. 반환값은 :class:`RecordedHttpx`.

    패치 대상은 **전역 httpx 모듈**이다(``backend.telegram_notifier.httpx``도
    ``backend.order_assist.httpx``도 같은 객체). 테스트 도중 만들어지는 다른 클라이언트도
    같은 transport를 탄다.

    **시그니처가 아니라 계약으로 지켜야 하는 것**: 구 대역의 ``def __init__(self, *, timeout)``은
    프로덕션이 ``timeout=``을 잃으면 우연히 빨개졌다. 인자를 그대로 흘리는 이 대역에는 그
    우연이 없으므로, 클라이언트 생성 인자를 ``client_kwargs``로 관측해 테스트가 명시적으로
    단언한다 (PR #359 리뷰와 같은 이유).
    """
    # 진짜 클라이언트는 패치 전에 한 번만 잡는다 — _install 안에서 읽으면 두 번째 설치의
    # "진짜"가 첫 번째 래퍼가 된다 (PR #359 리뷰).
    real_client = httpx.AsyncClient

    def _install(*responses: httpx.Response) -> RecordedHttpx:
        recorded = RecordedHttpx()
        templates = list(responses) or [httpx.Response(200, json={"ok": True})]

        def _dispatch(request: httpx.Request) -> httpx.Response:
            recorded.requests.append(request)
            template = templates[min(len(recorded.requests) - 1, len(templates) - 1)]
            # 응답은 요청마다 새로 만든다 — 한 httpx.Response 객체를 여러 요청이 나눠 쓰지
            # 않게 한다.
            return httpx.Response(
                template.status_code, headers=template.headers, content=template.content
            )

        transport = httpx.MockTransport(_dispatch)

        def _client(*args, **kwargs):
            # 프로덕션이 넘기는 인자는 그대로 진짜 AsyncClient에 흘린다 — 그래서 인자가
            # 하나 더 붙어도 이 대역은 손댈 필요가 없다. transport만 우리 것으로 바꾼다.
            recorded.client_kwargs = dict(kwargs)
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _client)
        return recorded

    return _install


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


class _ImplicitTradeLedgerUsed(BaseException):
    """기본 체결 원장이 실제로 쓰였다는 신호 (#259 2단계).

    Exception이 아니라 BaseException이다. 주문 경로의 ``except Exception``이 이걸 삼키면
    "기록 실패" 메시지로 접혀 테스트는 엉뚱한 단언에서 깨지고, 진짜 원인(대역 미주입)이
    화면에서 사라진다.
    """


@pytest.fixture(autouse=True)
def _forbid_implicit_trade_ledger(monkeypatch):
    """핸들러의 기본 체결 원장이 개발자의 backend/finus.db에 쓰는 것을 막는다 (#259).

    TelegramCommandHandler는 ``trade_recorder``를 안 받으면 프로덕션 원장을 만든다 —
    통지 outbox가 그 원장 위에 서므로 "원장 없음"인 배포를 허용할 수 없기 때문이다(근거는
    그 생성자 주석). 그 기본값이 테스트에서 살아 있으면 체결까지 가는 테스트가 실제 SQLite
    파일에 행을 남긴다. 실제로 남겼다.

    대역을 조용히 끼우지 않고 **터지게** 한다. 인메모리로 바꿔치기하면 원장을 단언하지
    않는 테스트가 통지 경로를 반쯤만 검증한 채 통과하고, 그 테스트는 outbox가 깨져도
    아무 말을 하지 않는다.

    핸들러 생성 자체는 막지 않는다 — 원장을 쓰지 않는 테스트가 대부분이고, 그쪽에
    주입을 강요하면 관계없는 130여 곳이 소음으로 바뀐다.
    """

    def _forbidden_session():
        raise _ImplicitTradeLedgerUsed(
            "체결 원장이 실제로 쓰였습니다. TelegramCommandHandler(trade_recorder=...)로 "
            "대역을 주입하세요 (#259)."
        )

    monkeypatch.setattr(
        telegram_commands,
        "_create_trade_recorder",
        lambda: TradeRecorder(_forbidden_session),
    )
