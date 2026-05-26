from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from backend.trading_orders import (
    McpTradingOrderGateway,
    OrderExecutionResult,
    PendingOrder,
    TradeRecorder,
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

    async def fake_mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return "should not be called"

    gateway = McpTradingOrderGateway(
        server_params="trading-params",
        mcp_runner=fake_mcp_runner,
        order_env="real",
        real_order_enabled=False,
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
async def test_demo_order_calls_local_mcp_runner_and_returns_normalized_result():
    calls = []

    async def fake_mcp_runner(server_params, tool_name, arguments):
        calls.append(
            {
                "server_params": server_params,
                "tool_name": tool_name,
                "arguments": arguments,
            }
        )
        return '{"msg1":"주문 접수"}'

    gateway = McpTradingOrderGateway(
        server_params="trading-params",
        mcp_runner=fake_mcp_runner,
        order_env="demo",
        real_order_enabled=False,
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
            "server_params": "trading-params",
            "tool_name": "place_order",
            "arguments": {
                "stock_name": "삼성전자",
                "stock_code": "005930",
                "side": "SELL",
                "quantity": 5,
                "price": 76000,
                "order_env": "demo",
            },
        }
    ]
    assert result == OrderExecutionResult(
        stock_code="005930",
        stock_name="삼성전자",
        side="SELL",
        quantity=5,
        price=76000,
        order_type="LIMIT",
        message="주문 접수",
        raw_result='{"msg1":"주문 접수"}',
    )


@pytest.mark.asyncio
async def test_market_order_calls_local_mcp_runner_with_order_type():
    calls = []

    async def fake_mcp_runner(server_params, tool_name, arguments):
        calls.append(
            {
                "server_params": server_params,
                "tool_name": tool_name,
                "arguments": arguments,
            }
        )
        return '{"msg1":"시장가 주문 접수"}'

    gateway = McpTradingOrderGateway(
        server_params="trading-params",
        mcp_runner=fake_mcp_runner,
        order_env="demo",
        real_order_enabled=False,
    )
    order = PendingOrder(
        chat_id="123",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=5,
        price=0,
        order_type="MARKET",
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    result = await gateway.place_order(order)

    assert calls == [
        {
            "server_params": "trading-params",
            "tool_name": "place_order",
            "arguments": {
                "stock_name": "삼성전자",
                "stock_code": "005930",
                "side": "BUY",
                "quantity": 5,
                "price": 0,
                "order_type": "MARKET",
                "order_env": "demo",
            },
        }
    ]
    assert result.order_type == "MARKET"
    assert result.price == 0
