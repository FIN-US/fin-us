"""KIS Trading MCP 접근 토큰 전달.

서버가 SSE·HTTP 모드에서 MCP_ACCESS_TOKEN을 필수로 요구하므로, 토큰이 설정되면
원격 MCP 세션이 Bearer 헤더를 실어야 한다. 헤더가 빠지면 모든 계좌·시세 조회가
Unauthorized로 실패한다.
"""

from contextlib import asynccontextmanager

import pytest

from finus_agents import finus_api


class _Stop(Exception):
    pass


@pytest.fixture
def recorded(monkeypatch):
    calls: dict[str, dict] = {}

    @asynccontextmanager
    async def fake_sse_client(**kwargs):
        calls["sse"] = kwargs
        raise _Stop
        yield  # pragma: no cover

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            calls["http"] = kwargs

        async def __aenter__(self):
            raise _Stop

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(finus_api, "sse_client", fake_sse_client)
    monkeypatch.setattr(finus_api.httpx, "AsyncClient", FakeAsyncClient)
    return calls


async def _open(transport):
    with pytest.raises(_Stop):
        async with finus_api._remote_mcp_session(
            transport=transport, url="http://kis.test/sse", operation_timeout=30.0,
        ):
            pass


@pytest.mark.parametrize("transport, key", [("sse", "sse"), ("streamable-http", "http")])
async def test_token_is_sent_as_bearer_header(monkeypatch, recorded, transport, key):
    monkeypatch.setenv(finus_api.KIS_TRADING_MCP_TOKEN_ENV, "  secret-token ")

    await _open(transport)

    assert recorded[key]["headers"] == {"Authorization": "Bearer secret-token"}


@pytest.mark.parametrize("transport, key", [("sse", "sse"), ("streamable-http", "http")])
async def test_no_header_without_token(monkeypatch, recorded, transport, key):
    monkeypatch.delenv(finus_api.KIS_TRADING_MCP_TOKEN_ENV, raising=False)

    await _open(transport)

    assert recorded[key]["headers"] == {}
