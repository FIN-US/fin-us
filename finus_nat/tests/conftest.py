"""finus_nat 테스트 공용 대역 — backend HTTP 호출을 요청 객체 수준에서 받는다 (#357).

손으로 흉내 낸 ``async def post(self, url, json, headers=None)`` 대역은 프로덕션의
**호출 시그니처를 복제**한다. 인자가 하나 붙는 날 대역마다 ``TypeError``가 나는데,
``finus_api``의 넓은 ``except Exception``이 그 예외를 삼켜 오류 JSON으로 바꾸므로
"저장이 안 됐다"는 얼굴로만 드러난다. 실제로 PR #352가 ``headers`` 인자를 붙이며 그
시점의 대역을 모두 고쳤지만, 병렬로 머지된 #351이 대역을 하나 더 들여와 main이
빨개졌다(PR #356에서 복구).

``httpx.MockTransport``는 조립이 끝난 ``httpx.Request``를 받으므로 호출 시그니처와
무관하다 — 클라이언트 생성 인자든 ``post``/``get`` 인자든 프로덕션이 바꿔도 대역은
그대로다. 단언도 시그니처가 아니라 요청 객체(URL·메서드·본문·헤더)에서 읽는다.

**시그니처가 아니라 계약으로 지켜야 하는 것**: 구 대역의 ``def __init__(self, timeout)``은
필수 위치 인자라서 프로덕션이 ``timeout=``을 잃으면 우연히 빨개졌다. 인자를 그대로
흘리는 이 대역에는 그 우연이 없으므로, 클라이언트 생성 인자를 :attr:`client_kwargs`로
관측해 테스트가 명시적으로 단언한다 (PR #359 리뷰).
"""

import json

import httpx
import pytest

from nat_finus_nat import finus_api


class RecordedBackend:
    """``MockTransport``가 받아 둔 요청들 — 테스트의 단언은 여기서 읽는다."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        # 마지막으로 만들어진 클라이언트의 생성 인자. timeout처럼 요청 객체에는 남지
        # 않지만 계약인 값을 여기서 본다.
        self.client_kwargs: dict = {}

    @property
    def called(self) -> bool:
        return bool(self.requests)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "backend 호출이 한 번도 일어나지 않았다"
        return self.requests[-1]

    @property
    def method(self) -> str:
        return self.last.method

    @property
    def url(self) -> str:
        return str(self.last.url)

    @property
    def json_body(self):
        """마지막 요청의 JSON 본문. 프로덕션이 실제로 직렬화해 보낸 바이트에서 읽는다."""
        return json.loads(self.last.content)

    def header(self, name: str) -> str | None:
        """헤더 값. 붙이지 않았으면 ``None``.

        요청에는 httpx가 스스로 넣는 기본 헤더(host·accept 등)가 함께 실리므로 헤더
        딕셔너리 전체를 비교하지 않고 우리 계약이 정한 헤더만 본다.
        """
        return self.last.headers.get(name)


@pytest.fixture
def mock_backend(monkeypatch):
    """``httpx.AsyncClient``가 ``MockTransport``를 타게 만든다.

    ``payload``(JSON 본문)나 ``status_code``·``text``로 응답을 정한다. 반환값은
    :class:`RecordedBackend`.

    패치 대상은 ``finus_api.httpx``, 즉 **전역 httpx 모듈**이다. 그래서 이 테스트가
    도는 동안 만들어지는 다른 클라이언트(예: ``finus_api``의 remote-MCP용
    ``httpx.AsyncClient()``)도 같은 transport를 타고 ``requests``에 섞인다. 지금
    호출자는 전부 backend 왕복 하나만 태우므로 URL로 걸러내지 않는다 — 한 테스트가 두
    종류의 호출을 태우게 되면 그때 필터를 넣어야 한다.
    """
    # 진짜 클라이언트는 패치 **전에** 한 번만 잡는다. _install 안에서 읽으면 두 번째
    # 호출의 "진짜"가 첫 번째 래퍼가 되어, 안쪽이 첫 transport로 덮어쓴다 — 두 번째
    # RecordedBackend는 요청을 하나도 못 받은 채 `not backend.called`가 공허하게
    # 통과한다 (PR #359 리뷰).
    real_client = finus_api.httpx.AsyncClient

    def _install(payload=None, *, status_code: int = 200, text: str | None = None):
        recorded = RecordedBackend()

        def _dispatch(request: httpx.Request) -> httpx.Response:
            recorded.requests.append(request)
            # 응답은 요청마다 새로 만든다 — httpx.Response는 한 번 읽으면 스트림이 닫힌다.
            if text is not None:
                return httpx.Response(status_code, text=text)
            return httpx.Response(status_code, json=payload if payload is not None else {})

        transport = httpx.MockTransport(_dispatch)

        def _client(*args, **kwargs):
            # 프로덕션이 넘기는 인자는 그대로 진짜 AsyncClient에 흘린다 — 그래서 인자가
            # 하나 더 붙어도 이 대역은 손댈 필요가 없다. transport만 우리 것으로 바꾼다.
            recorded.client_kwargs = dict(kwargs)
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(finus_api.httpx, "AsyncClient", _client)
        return recorded

    return _install
