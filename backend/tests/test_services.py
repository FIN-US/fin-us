import logging

import httpx
import pytest
from fastapi import HTTPException

from backend import services


class MockAsyncClient:
    def __init__(self, response: httpx.Response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self.response


def mock_async_client_factory(response: httpx.Response):
    def factory(*args, **kwargs):
        return MockAsyncClient(response)

    return factory


@pytest.mark.asyncio
async def test_llm_nat_chat_logs_raw_response(monkeypatch, caplog):
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "분석 결과"}}]},
    )
    monkeypatch.setattr(
        services.httpx,
        "AsyncClient",
        mock_async_client_factory(response),
    )

    with caplog.at_level(logging.INFO, logger=services.logger.name):
        result = await services._llm_nat_chat("삼성전자 분석")

    assert result == "분석 결과"
    assert "NAT raw response: status_code=200" in caplog.text
    assert '"content":"분석 결과"' in caplog.text


@pytest.mark.asyncio
async def test_llm_nat_chat_logs_json_parse_failure(monkeypatch, caplog):
    response = httpx.Response(200, text="not-json")
    monkeypatch.setattr(
        services.httpx,
        "AsyncClient",
        mock_async_client_factory(response),
    )

    with caplog.at_level(logging.INFO, logger=services.logger.name):
        with pytest.raises(HTTPException) as exc_info:
            await services._llm_nat_chat("삼성전자 분석")

    assert exc_info.value.status_code == 502
    assert "NAT raw response: status_code=200 body=not-json" in caplog.text
    assert "Failed to parse NAT response JSON: status_code=200 body=not-json" in caplog.text
