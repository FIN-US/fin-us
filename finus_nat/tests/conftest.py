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
"""

import json

import httpx
import pytest

from nat_finus_nat import finus_api


class RecordedBackend:
    """``MockTransport``가 받아 둔 요청들 — 테스트의 단언은 여기서 읽는다."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

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
    """프로덕션의 ``httpx.AsyncClient``가 ``MockTransport``를 타게 만든다.

    ``payload``(JSON 본문)나 ``status_code``·``text``로 응답을 정하고, 요청마다 다른
    응답이 필요하면 ``handler``(``httpx.Request`` -> ``httpx.Response``)를 준다.
    반환값은 :class:`RecordedBackend`.
    """

    def _install(payload=None, *, status_code: int = 200, text: str | None = None, handler=None):
        recorded = RecordedBackend()

        def _respond(request: httpx.Request) -> httpx.Response:
            if handler is not None:
                return handler(request)
            if text is not None:
                return httpx.Response(status_code, text=text)
            return httpx.Response(status_code, json=payload if payload is not None else {})

        def _dispatch(request: httpx.Request) -> httpx.Response:
            recorded.requests.append(request)
            # 응답은 요청마다 새로 만든다 — httpx.Response는 한 번 읽으면 스트림이 닫힌다.
            return _respond(request)

        transport = httpx.MockTransport(_dispatch)
        real_client = finus_api.httpx.AsyncClient

        def _client(*args, **kwargs):
            # 프로덕션이 넘기는 인자는 그대로 진짜 AsyncClient에 흘린다 — 그래서 인자가
            # 하나 더 붙어도 이 대역은 손댈 필요가 없다. transport만 우리 것으로 바꾼다.
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(finus_api.httpx, "AsyncClient", _client)
        return recorded

    return _install
