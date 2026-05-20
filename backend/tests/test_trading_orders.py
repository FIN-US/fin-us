from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from backend.trading_orders import (
    OfficialKisMcpOrderGateway,
    OrderExecutionResult,
    PendingOrder,
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
