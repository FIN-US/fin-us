import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException

import backend.trading_orders as trading_orders
from backend.trading_orders import (
    OfficialKisMcpOrderGateway,
    OrderExecutionResult,
    PendingOrder,
    TradeRecorder,
    _mcp_first_text_or_error,
    call_official_kis_mcp,
    is_korean_market_open,
)


KST = ZoneInfo("Asia/Seoul")


def test_market_open_during_weekday_regular_session():
    now = datetime(2026, 5, 20, 10, 0, tzinfo=KST)

    assert is_korean_market_open(now) is True


def test_market_closed_before_open():
    now = datetime(2026, 5, 20, 8, 59, tzinfo=KST)

    assert is_korean_market_open(now) is False


def test_market_closed_after_close():
    now = datetime(2026, 5, 20, 15, 31, tzinfo=KST)

    assert is_korean_market_open(now) is False


def test_market_closed_on_weekend():
    now = datetime(2026, 5, 23, 10, 0, tzinfo=KST)

    assert is_korean_market_open(now) is False


def test_trade_recorder_creates_trade_history_and_commits():
    class FakeSession:
        def __init__(self):
            self.added = []
            self.committed = False
            self.closed = False

        def add(self, item):
            self.added.append(item)

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    session = FakeSession()
    recorder = TradeRecorder(lambda: session)
    result = OrderExecutionResult(
        stock_code="005930",
        stock_name="삼성전자",
        side="BUY",
        quantity=3,
        price=75000,
        message="주문 접수",
        raw_result="{}",
    )

    recorder.record(result)

    assert len(session.added) == 1
    trade = session.added[0]
    assert trade.stock_code == "005930"
    assert trade.stock_name == "삼성전자"
    assert trade.trade_type == "BUY"
    assert trade.quantity == 3
    assert trade.price == 75000.0
    assert session.committed is True
    assert session.closed is True


def test_trade_recorder_rolls_back_and_closes_when_commit_fails():
    class FakeSession:
        def __init__(self):
            self.added = []
            self.rolled_back = False
            self.closed = False

        def add(self, item):
            self.added.append(item)

        def commit(self):
            raise RuntimeError("commit failed")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    session = FakeSession()
    recorder = TradeRecorder(lambda: session)
    result = OrderExecutionResult(
        stock_code="005930",
        stock_name="삼성전자",
        side="BUY",
        quantity=3,
        price=75000,
        message="주문 접수",
        raw_result="{}",
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        recorder.record(result)

    assert len(session.added) == 1
    assert session.rolled_back is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_real_order_without_explicit_opt_in_is_blocked_before_mcp_call():
    calls = []

    async def fake_remote_runner(*, transport, url, tool_name, arguments, timeout_sec):
        calls.append((transport, url, tool_name, arguments, timeout_sec))
        return "should not be called"

    gateway = OfficialKisMcpOrderGateway(
        mcp_url="http://127.0.0.1:3300/sse",
        mcp_transport="sse",
        tool_name="domestic_stock",
        order_env="real",
        real_order_enabled=False,
        remote_runner=fake_remote_runner,
    )
    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=10,
        price=75000,
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    with pytest.raises(HTTPException) as exc_info:
        await gateway.place_order(order)

    assert exc_info.value.status_code == 403
    assert "실계좌 주문" in str(exc_info.value.detail)
    assert calls == []


@pytest.mark.asyncio
async def test_demo_order_calls_mcp_runner_and_returns_normalized_result():
    calls = []

    async def fake_remote_runner(*, transport, url, tool_name, arguments, timeout_sec):
        calls.append(
            {
                "transport": transport,
                "url": url,
                "tool_name": tool_name,
                "arguments": arguments,
                "timeout_sec": timeout_sec,
            }
        )
        return '{"msg1":"주문 접수"}'

    gateway = OfficialKisMcpOrderGateway(
        mcp_url="http://127.0.0.1:3300/sse",
        mcp_transport="sse",
        tool_name="domestic_stock",
        order_env="demo",
        real_order_enabled=False,
        remote_runner=fake_remote_runner,
        timeout_sec=30.0,
    )
    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="SELL",
        quantity=5,
        price=76000,
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    result = await gateway.place_order(order)

    assert calls == [
        {
            "transport": "sse",
            "url": "http://127.0.0.1:3300/sse",
            "tool_name": "domestic_stock",
            "arguments": {
                "api_type": "order_cash",
                "params": {
                    "env_dv": "demo",
                    "pdno": "005930",
                    "ord_dvsn": "01",
                    "ord_qty": "5",
                    "ord_unpr": "76000",
                    "buy_sell": "sell",
                },
            },
            "timeout_sec": 30.0,
        }
    ]
    assert result == OrderExecutionResult(
        stock_code="005930",
        stock_name="삼성전자",
        side="SELL",
        quantity=5,
        price=76000,
        message="주문 접수",
        raw_result='{"msg1":"주문 접수"}',
    )


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeMcpResult:
    def __init__(self, *, content, is_error=False):
        self.content = content
        self.isError = is_error


def test_mcp_first_text_or_error_returns_first_text_block():
    result = _FakeMcpResult(content=[_FakeTextBlock("주문 접수"), _FakeTextBlock("ignored")])

    assert _mcp_first_text_or_error(result) == "주문 접수"


def test_mcp_first_text_or_error_raises_bad_gateway_for_error_result():
    result = _FakeMcpResult(content=[_FakeTextBlock("KIS rejected order")], is_error=True)

    with pytest.raises(HTTPException) as exc_info:
        _mcp_first_text_or_error(result)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "KIS rejected order"


def test_mcp_first_text_or_error_raises_bad_gateway_for_empty_success_content():
    result = _FakeMcpResult(content=[])

    with pytest.raises(HTTPException) as exc_info:
        _mcp_first_text_or_error(result)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "KIS MCP 주문 응답이 비어 있습니다."


@pytest.mark.asyncio
async def test_call_official_kis_mcp_rejects_unsupported_transport_without_network():
    with pytest.raises(HTTPException) as exc_info:
        await call_official_kis_mcp(
            transport="stdio",
            url="http://127.0.0.1:3300/sse",
            tool_name="domestic_stock",
            arguments={},
            timeout_sec=1.0,
        )

    assert exc_info.value.status_code == 500
    assert "지원하지 않는 KIS MCP transport" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_call_official_kis_mcp_wraps_timeout_as_gateway_timeout(monkeypatch):
    async def fake_inner(**_kwargs):
        raise TimeoutError

    monkeypatch.setattr(
        trading_orders,
        "_call_official_kis_mcp_inner",
        fake_inner,
        raising=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        await call_official_kis_mcp(
            transport="sse",
            url="http://127.0.0.1:3300/sse",
            tool_name="domestic_stock",
            arguments={},
            timeout_sec=1.0,
        )

    assert exc_info.value.status_code == 504
    assert exc_info.value.detail == "KIS MCP 주문 요청 시간이 초과되었습니다."


@pytest.mark.asyncio
async def test_call_official_kis_mcp_wraps_httpx_timeout_as_gateway_timeout(monkeypatch):
    async def fake_inner(**_kwargs):
        raise httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(
        trading_orders,
        "_call_official_kis_mcp_inner",
        fake_inner,
        raising=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        await call_official_kis_mcp(
            transport="sse",
            url="http://127.0.0.1:3300/sse",
            tool_name="domestic_stock",
            arguments={},
            timeout_sec=1.0,
        )

    assert exc_info.value.status_code == 504
    assert exc_info.value.detail == "KIS MCP 주문 요청 시간이 초과되었습니다."


@pytest.mark.asyncio
async def test_call_official_kis_mcp_propagates_cancellation(monkeypatch):
    async def fake_inner(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        trading_orders,
        "_call_official_kis_mcp_inner",
        fake_inner,
        raising=False,
    )

    with pytest.raises(asyncio.CancelledError):
        await call_official_kis_mcp(
            transport="sse",
            url="http://127.0.0.1:3300/sse",
            tool_name="domestic_stock",
            arguments={},
            timeout_sec=1.0,
        )


@pytest.mark.asyncio
async def test_call_official_kis_mcp_passes_timeout_to_streamable_http_client(monkeypatch):
    client_timeouts = []
    streamable_calls = []
    session_calls = []

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout
            client_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeStreamableClient:
        def __init__(self, *, url, http_client):
            self.url = url
            self.http_client = http_client

        async def __aenter__(self):
            streamable_calls.append(
                {
                    "url": self.url,
                    "timeout": self.http_client.timeout,
                }
            )
            return "read", "write", lambda: "session-id"

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeClientSession:
        def __init__(self, read, write):
            session_calls.append((read, write))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, tool_name, arguments):
            session_calls.append((tool_name, arguments))
            return _FakeMcpResult(content=[_FakeTextBlock("주문 접수")])

    monkeypatch.setattr(trading_orders.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(trading_orders, "streamable_http_client", FakeStreamableClient)
    monkeypatch.setattr(trading_orders, "ClientSession", FakeClientSession)

    result = await call_official_kis_mcp(
        transport="streamable-http",
        url="http://127.0.0.1:3300/mcp",
        tool_name="domestic_stock",
        arguments={"api_type": "order_cash"},
        timeout_sec=7.5,
    )

    assert result == "주문 접수"
    assert client_timeouts == [7.5]
    assert streamable_calls == [
        {
            "url": "http://127.0.0.1:3300/mcp",
            "timeout": 7.5,
        }
    ]
    assert session_calls == [
        ("read", "write"),
        ("domestic_stock", {"api_type": "order_cash"}),
    ]


@pytest.mark.asyncio
async def test_call_official_kis_mcp_wraps_transport_error_as_bad_gateway(monkeypatch):
    async def fake_inner(**_kwargs):
        raise httpx.TransportError("connection failed")

    monkeypatch.setattr(
        trading_orders,
        "_call_official_kis_mcp_inner",
        fake_inner,
        raising=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        await call_official_kis_mcp(
            transport="sse",
            url="http://127.0.0.1:3300/sse",
            tool_name="domestic_stock",
            arguments={},
            timeout_sec=1.0,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "KIS MCP 주문 요청에 실패했습니다."
