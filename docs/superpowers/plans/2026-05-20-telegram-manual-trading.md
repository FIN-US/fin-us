# Telegram Manual Trading Implementation Plan

> Superseded: 공식 KIS Trading MCP 실행 계획은 로컬 `mcp-trading` 주문 실행 계획으로 대체되었습니다. 현재 기준 문서는 `docs/superpowers/plans/2026-05-20-telegram-mcp-trading-order-replacement.md`입니다.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Telegram `/buy`, `/sell`, `/confirm`, and `/cancel` commands that place confirmed manual stock orders through official KIS Trading MCP and record successful orders in `TradeHistory`.

**Architecture:** Keep local `mcp-trading` for existing read commands and add a narrow backend order gateway for official KIS Trading MCP order execution. `TelegramCommandHandler` owns parsing, pending-order state, market-hours checks, confirmation UX, and calls injected gateway/recorder dependencies so tests stay isolated.

**Tech Stack:** Python 3.11+, FastAPI backend, SQLModel, pytest/pytest-asyncio, MCP Python client, official KIS Trading MCP over SSE or streamable HTTP.

---

## File Structure

- Create `backend/trading_orders.py`
  - Defines `PendingOrder`, `OrderExecutionResult`, `OfficialKisMcpOrderGateway`, `TradeRecorder`, parsing helpers, market-hours helper, and MCP result normalization for order execution.
- Modify `backend/config.py`
  - Adds official KIS MCP URL/transport/tool-name/order environment settings and `KIS_REAL_ORDER_ENABLED`.
- Modify `backend/telegram_commands.py`
  - Adds `/buy`, `/sell`, `/confirm`, `/cancel`, in-memory pending orders, and dependency injection for order gateway/time/recorder.
- Modify `backend/tests/test_telegram_commands.py`
  - Adds focused Telegram command tests for the new flow.
- Create `backend/tests/test_trading_orders.py`
  - Tests gateway guard/config normalization and market-hours/pending-order helpers without real KIS calls.
- Modify `README.md` only if implementation adds new required environment variables.

## Task 1: Trading Order Domain Helpers

**Files:**
- Create: `backend/trading_orders.py`
- Test: `backend/tests/test_trading_orders.py`

- [ ] **Step 1: Write failing tests for market hours and real-order guard**

Add this file:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from backend.trading_orders import (
    OfficialKisMcpOrderGateway,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py -q
```

Expected: FAIL because `backend.trading_orders` does not exist.

- [ ] **Step 3: Implement minimal helper module**

Create `backend/trading_orders.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException


KST = ZoneInfo("Asia/Seoul")
OrderSide = Literal["BUY", "SELL"]
McpTransport = Literal["sse", "streamable-http"]


@dataclass(frozen=True)
class PendingOrder:
    chat_id: str
    stock_name: str
    stock_code: str
    side: OrderSide
    quantity: int
    price: int
    created_at: datetime


@dataclass(frozen=True)
class OrderExecutionResult:
    stock_code: str
    stock_name: str
    side: OrderSide
    quantity: int
    price: int
    message: str
    raw_result: str


RemoteMcpRunner = Callable[..., Awaitable[str]]


def is_korean_market_open(now: datetime | None = None) -> bool:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)
    if current.weekday() >= 5:
        return False
    return time(9, 0) <= current.time() <= time(15, 30)


class OfficialKisMcpOrderGateway:
    def __init__(
        self,
        *,
        mcp_url: str,
        mcp_transport: McpTransport,
        tool_name: str,
        order_env: Literal["real", "demo"],
        real_order_enabled: bool,
        remote_runner: RemoteMcpRunner,
        timeout_sec: float = 180.0,
    ):
        self.mcp_url = mcp_url
        self.mcp_transport = mcp_transport
        self.tool_name = tool_name
        self.order_env = order_env
        self.real_order_enabled = real_order_enabled
        self.remote_runner = remote_runner
        self.timeout_sec = timeout_sec

    async def place_order(self, order: PendingOrder) -> OrderExecutionResult:
        if self.order_env == "real" and not self.real_order_enabled:
            raise HTTPException(
                status_code=403,
                detail="실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다.",
            )

        arguments = {
            "api_type": "order_cash",
            "params": {
                "env_dv": self.order_env,
                "pdno": order.stock_code,
                "ord_dvsn": "01",
                "ord_qty": str(order.quantity),
                "ord_unpr": str(order.price),
                "buy_sell": order.side.lower(),
            },
        }
        raw_result = await self.remote_runner(
            transport=self.mcp_transport,
            url=self.mcp_url,
            tool_name=self.tool_name,
            arguments=arguments,
            timeout_sec=self.timeout_sec,
        )
        message = _extract_order_message(raw_result)
        return OrderExecutionResult(
            stock_code=order.stock_code,
            stock_name=order.stock_name,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            message=message,
            raw_result=raw_result,
        )


def _extract_order_message(raw_result: str) -> str:
    text = str(raw_result or "").strip()
    if not text:
        return "주문 요청이 접수되었습니다."
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]
    if isinstance(data, dict):
        for key in ("msg1", "message", "rt_msg", "output"):
            value = data.get(key)
            if value:
                return str(value)[:500]
    return text[:500]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add backend/trading_orders.py backend/tests/test_trading_orders.py
git commit -m "feat: Telegram 주문 도메인 헬퍼 추가" \
  -m "- 장 운영 시간 판별 추가" \
  -m "- 실계좌 주문 opt-in guard 추가"
```

## Task 2: Official KIS MCP Remote Adapter

**Files:**
- Modify: `backend/config.py`
- Modify: `backend/trading_orders.py`
- Test: `backend/tests/test_trading_orders.py`

- [ ] **Step 1: Write failing tests for config-driven gateway and remote arguments**

Append to `backend/tests/test_trading_orders.py`:

```python
@pytest.mark.asyncio
async def test_demo_order_calls_official_mcp_with_normalized_arguments():
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

    assert result.message == "주문 접수"
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
```

- [ ] **Step 2: Run focused test**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py::test_demo_order_calls_official_mcp_with_normalized_arguments -q
```

Expected: PASS if Task 1 code already includes the minimal adapter. If it fails due to official MCP field names discovered during implementation, update the expected `arguments` and gateway in the same step after confirming with `find_api_detail`.

- [ ] **Step 3: Add backend config settings**

Modify `backend/config.py` near Telegram settings:

```python
KIS_TRADING_MCP_URL = os.environ.get(
    "FINUS_KIS_TRADING_MCP_URL",
    "http://host.docker.internal:3300/sse",
).strip()
KIS_TRADING_MCP_TRANSPORT = os.environ.get("FINUS_KIS_TRADING_MCP_TRANSPORT", "sse").strip()
KIS_TRADING_MCP_TOOL_NAME = os.environ.get(
    "FINUS_KIS_TRADING_TOOL_NAME",
    "domestic_stock",
).strip()
KIS_ORDER_ENV = os.environ.get("KIS_ORDER_ENV", "demo").strip().lower()
KIS_REAL_ORDER_ENABLED = os.environ.get("KIS_REAL_ORDER_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "y",
}
```

- [ ] **Step 4: Implement remote MCP runner**

Add to `backend/trading_orders.py`:

```python
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
```

Add this function below type aliases:

```python
async def call_official_kis_mcp(
    *,
    transport: McpTransport,
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_sec: float,
) -> str:
    if transport == "sse":
        async with sse_client(url, timeout=timeout_sec) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return _mcp_first_text_or_error(result)
    if transport == "streamable-http":
        async with streamable_http_client(url, timeout=timeout_sec) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return _mcp_first_text_or_error(result)
    raise HTTPException(status_code=500, detail=f"지원하지 않는 KIS MCP transport: {transport}")


def _mcp_first_text_or_error(result: Any) -> str:
    block = result.content[0] if getattr(result, "content", None) else None
    text = getattr(block, "text", str(block)) if block else ""
    if getattr(result, "isError", False):
        raise HTTPException(status_code=502, detail=text or "KIS MCP 주문 실행 실패")
    return text
```

- [ ] **Step 5: Run tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add backend/config.py backend/trading_orders.py backend/tests/test_trading_orders.py
git commit -m "feat: 공식 KIS MCP 주문 게이트웨이 추가" \
  -m "- 공식 KIS MCP transport 설정 추가" \
  -m "- 주문 실행 결과 정규화 추가"
```

## Task 3: Telegram Command Parsing And Pending Orders

**Files:**
- Modify: `backend/telegram_commands.py`
- Test: `backend/tests/test_telegram_commands.py`

- [ ] **Step 1: Add failing tests for order command parsing and pending state**

Append to `backend/tests/test_telegram_commands.py`:

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.trading_orders import OrderExecutionResult


KST = ZoneInfo("Asia/Seoul")


class FakeOrderGateway:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result or OrderExecutionResult(
            stock_code="005930",
            stock_name="삼성전자",
            side="BUY",
            quantity=10,
            price=75000,
            message="주문 접수",
            raw_result="주문 접수",
        )
        self.exc = exc

    async def place_order(self, order):
        self.calls.append(order)
        if self.exc:
            raise self.exc
        return self.result


class FakeTradeRecorder:
    def __init__(self):
        self.records = []

    def record(self, result):
        self.records.append(result)


@pytest.mark.asyncio
async def test_buy_command_creates_pending_order_and_prompts_confirmation():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "[삼성전자] 현재가 시세\n- 현재가: 74,500원"
        if tool_name == "get_balance":
            return "[계좌 잔고 현황]\n- 예수금: 1,200,000원"
        raise AssertionError(tool_name)

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10 75000"}})

    assert "삼성전자 매수 주문 확인" in notifier.messages[-1]
    assert "/confirm" in notifier.messages[-1]
    assert "/cancel" in notifier.messages[-1]


@pytest.mark.asyncio
async def test_sell_command_rejects_market_closed():
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        now_factory=lambda: datetime(2026, 5, 20, 8, 59, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/sell 삼성전자 1 75000"}})

    assert notifier.messages[-1] == "주문 불가: 현재 장 운영 시간이 아닙니다. (평일 09:00~15:30)"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py::test_buy_command_creates_pending_order_and_prompts_confirmation backend/tests/test_telegram_commands.py::test_sell_command_rejects_market_closed -q
```

Expected: FAIL because `TelegramCommandHandler` does not accept `now_factory` and does not route `/buy` or `/sell`.

- [ ] **Step 3: Add imports and constants**

Modify `backend/telegram_commands.py` imports:

```python
from datetime import datetime, timedelta
from typing import Any, Callable

from .trading_orders import (
    KST,
    OfficialKisMcpOrderGateway,
    PendingOrder,
    call_official_kis_mcp,
    is_korean_market_open,
)
```

Add constants near existing command help:

```python
BUY_COMMAND_HELP = "사용법: /buy <종목명> <수량> <지정가>"
SELL_COMMAND_HELP = "사용법: /sell <종목명> <수량> <지정가>"
ORDER_EXPIRES_AFTER = timedelta(seconds=60)
```

Update `TELEGRAM_INTERACTIVE_HELP` lines:

```python
"/buy <종목명> <수량> <지정가> - 지정가 매수 주문 준비",
"/sell <종목명> <수량> <지정가> - 지정가 매도 주문 준비",
"/confirm - 대기 주문 확정",
"/cancel - 대기 주문 취소",
```

- [ ] **Step 4: Add handler dependencies and routing**

Extend `TelegramCommandHandler.__init__`:

```python
        order_gateway: Any | None = None,
        trade_recorder: Any | None = None,
        now_factory: Callable[[], datetime] | None = None,
```

Inside `__init__`, assign:

```python
        self.order_gateway = order_gateway
        self.trade_recorder = trade_recorder
        self.now_factory = now_factory or (lambda: datetime.now(KST))
        self.pending_orders: dict[str, PendingOrder] = {}
```

Add routes in `handle_update` before unknown slash handling:

```python
        if self._matches_command(command, bot_username, "/buy"):
            await self._handle_order_command("BUY", argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/sell"):
            await self._handle_order_command("SELL", argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/confirm"):
            await self._handle_confirm(str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/cancel"):
            await self._handle_cancel(str(chat.get("id", "")).strip())
            return
```

- [ ] **Step 5: Implement order parsing and prompt creation**

Add methods to `TelegramCommandHandler`:

```python
    async def _handle_order_command(self, side: str, argument: str, chat_id: str) -> None:
        usage = BUY_COMMAND_HELP if side == "BUY" else SELL_COMMAND_HELP
        parsed = self._parse_order_argument(argument)
        if parsed is None:
            await self._send_text_or_raise(usage)
            return
        stock_name, quantity, price = parsed
        now = self.now_factory()
        if not is_korean_market_open(now):
            await self._send_text_or_raise("주문 불가: 현재 장 운영 시간이 아닙니다. (평일 09:00~15:30)")
            return
        self._drop_expired_pending_order(chat_id, now)
        if chat_id in self.pending_orders:
            await self._send_text_or_raise("이미 대기 중인 주문이 있습니다. /confirm 또는 /cancel로 먼저 처리하세요.")
            return

        await self.notifier.send_chat_action("typing")
        try:
            resolved = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "resolve_stock_code",
                {"stock_name": stock_name},
            )
            stock_code = self._extract_stock_code(str(resolved)) or stock_name
            quote, balance = await asyncio.gather(
                self.mcp_runner(TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": stock_name}),
                self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {}),
            )
        except Exception as exc:
            await self._send_text_or_raise(f"주문 준비 실패: {_short_error(exc)}")
            return

        order = PendingOrder(
            chat_id=chat_id,
            stock_name=stock_name,
            stock_code=stock_code,
            side=side,
            quantity=quantity,
            price=price,
            created_at=now,
        )
        self.pending_orders[chat_id] = order
        await self._send_text_or_raise(self._format_order_prompt(order, str(quote), str(balance)))

    def _parse_order_argument(self, argument: str) -> tuple[str, int, int] | None:
        parts = argument.split()
        if len(parts) != 3:
            return None
        stock_name, quantity_text, price_text = parts
        try:
            quantity = int(quantity_text.replace(",", ""))
            price = int(price_text.replace(",", ""))
        except ValueError:
            return None
        if quantity <= 0 or price <= 0:
            return None
        return stock_name, quantity, price

    def _drop_expired_pending_order(self, chat_id: str, now: datetime) -> None:
        order = self.pending_orders.get(chat_id)
        if order is None:
            return
        if now - order.created_at > ORDER_EXPIRES_AFTER:
            self.pending_orders.pop(chat_id, None)

    def _extract_stock_code(self, resolved: str) -> str | None:
        match = re.search(r"\((\d{6}),", resolved)
        return match.group(1) if match else None

    def _format_order_prompt(self, order: PendingOrder, quote: str, balance: str) -> str:
        side_text = "매수" if order.side == "BUY" else "매도"
        amount = order.quantity * order.price
        lines = [
            f"{order.stock_name} {side_text} 주문 확인",
            f"- 수량: {order.quantity:,}주 x {order.price:,}원 = {amount:,}원",
        ]
        quote_line = self._first_line_containing(quote, "현재가")
        balance_line = self._first_line_containing(balance, "예수금")
        if quote_line:
            lines.append(f"- {quote_line.lstrip('- ').strip()}")
        if balance_line:
            lines.append(f"- {balance_line.lstrip('- ').strip()}")
        lines.append("/confirm 으로 확정, /cancel 로 취소 (60초 내)")
        return "\n".join(lines)

    def _first_line_containing(self, text: str, needle: str) -> str | None:
        for line in text.splitlines():
            if needle in line:
                return line.strip()
        return None
```

Also add `import re` at the top.

- [ ] **Step 6: Run focused tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py::test_buy_command_creates_pending_order_and_prompts_confirmation backend/tests/test_telegram_commands.py::test_sell_command_rejects_market_closed -q
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add backend/telegram_commands.py backend/tests/test_telegram_commands.py
git commit -m "feat: Telegram 주문 준비 명령 추가" \
  -m "- buy/sell 명령 파싱 추가" \
  -m "- 대기 주문 확인 프롬프트 추가"
```

## Task 4: Confirm, Cancel, Trade Recording

**Files:**
- Modify: `backend/trading_orders.py`
- Modify: `backend/telegram_commands.py`
- Test: `backend/tests/test_telegram_commands.py`

- [ ] **Step 1: Add failing tests for confirm/cancel/recording**

Append to `backend/tests/test_telegram_commands.py`:

```python
@pytest.mark.asyncio
async def test_cancel_removes_pending_order():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "- 현재가: 74,500원"
        if tool_name == "get_balance":
            return "- 예수금: 1,200,000원"
        raise AssertionError(tool_name)

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10 75000"}})
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/cancel"}})

    assert "취소" in notifier.messages[-1]
    assert handler.pending_orders == {}


@pytest.mark.asyncio
async def test_confirm_executes_gateway_and_records_trade():
    async def mcp_runner(server_params, tool_name, arguments):
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "- 현재가: 74,500원"
        if tool_name == "get_balance":
            return "- 예수금: 1,200,000원"
        raise AssertionError(tool_name)

    gateway = FakeOrderGateway()
    recorder = FakeTradeRecorder()
    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        order_gateway=gateway,
        trade_recorder=recorder,
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 10 75000"}})
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert len(gateway.calls) == 1
    assert len(recorder.records) == 1
    assert "주문 완료" in notifier.messages[-1]
    assert handler.pending_orders == {}
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py::test_cancel_removes_pending_order backend/tests/test_telegram_commands.py::test_confirm_executes_gateway_and_records_trade -q
```

Expected: FAIL because `_handle_cancel` and `_handle_confirm` are not implemented.

- [ ] **Step 3: Add trade recorder**

Append to `backend/trading_orders.py`:

```python
class TradeRecorder:
    def __init__(self, session_factory: Callable[[], Any]):
        self.session_factory = session_factory

    def record(self, result: OrderExecutionResult) -> None:
        from .models import TradeHistory

        with self.session_factory() as session:
            trade = TradeHistory(
                stock_code=result.stock_code,
                stock_name=result.stock_name,
                trade_type=result.side,
                quantity=result.quantity,
                price=float(result.price),
            )
            session.add(trade)
            session.commit()
```

- [ ] **Step 4: Implement confirm/cancel methods**

Add to `TelegramCommandHandler`:

```python
    async def _handle_cancel(self, chat_id: str) -> None:
        self._drop_expired_pending_order(chat_id, self.now_factory())
        if self.pending_orders.pop(chat_id, None) is None:
            await self._send_text_or_raise("취소할 대기 주문이 없습니다.")
            return
        await self._send_text_or_raise("대기 주문을 취소했습니다.")

    async def _handle_confirm(self, chat_id: str) -> None:
        now = self.now_factory()
        self._drop_expired_pending_order(chat_id, now)
        order = self.pending_orders.get(chat_id)
        if order is None:
            await self._send_text_or_raise("확정할 대기 주문이 없습니다.")
            return
        if self.order_gateway is None:
            await self._send_text_or_raise("주문 실행 설정이 준비되지 않았습니다.")
            return

        await self.notifier.send_chat_action("typing")
        try:
            result = await self.order_gateway.place_order(order)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 실패: {_short_error(exc)}")
            return

        if self.trade_recorder is not None:
            self.trade_recorder.record(result)
        self.pending_orders.pop(chat_id, None)
        await self._send_text_or_raise(f"주문 완료: {result.message}")
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py::test_cancel_removes_pending_order backend/tests/test_telegram_commands.py::test_confirm_executes_gateway_and_records_trade -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add backend/trading_orders.py backend/telegram_commands.py backend/tests/test_telegram_commands.py
git commit -m "feat: Telegram 주문 확정 및 취소 추가" \
  -m "- confirm 주문 실행 흐름 추가" \
  -m "- TradeHistory 기록 연결 추가"
```

## Task 5: Wire Production Dependencies

**Files:**
- Modify: `backend/telegram_commands.py`
- Test: `backend/tests/test_telegram_commands.py`

- [ ] **Step 1: Add imports for production dependencies**

Modify `backend/telegram_commands.py` imports:

```python
from .config import (
    KIS_ORDER_ENV,
    KIS_REAL_ORDER_ENABLED,
    KIS_TRADING_MCP_TOOL_NAME,
    KIS_TRADING_MCP_TRANSPORT,
    KIS_TRADING_MCP_URL,
    TRADING_MCP_PARAMS,
)
from sqlmodel import Session
from .database import engine
from .trading_orders import TradeRecorder
```

- [ ] **Step 2: Add factory helpers**

Add module-level helpers in `backend/telegram_commands.py` before `TelegramCommandPoller`:

```python
def _create_order_gateway() -> OfficialKisMcpOrderGateway:
    transport = KIS_TRADING_MCP_TRANSPORT
    if transport not in {"sse", "streamable-http"}:
        transport = "sse"
    order_env = "real" if KIS_ORDER_ENV == "real" else "demo"
    return OfficialKisMcpOrderGateway(
        mcp_url=KIS_TRADING_MCP_URL,
        mcp_transport=transport,
        tool_name=KIS_TRADING_MCP_TOOL_NAME,
        order_env=order_env,
        real_order_enabled=KIS_REAL_ORDER_ENABLED,
        remote_runner=call_official_kis_mcp,
    )


def _create_trade_recorder() -> TradeRecorder:
    return TradeRecorder(lambda: Session(engine))
```

Modify `TelegramCommandPoller.__init__` to create the default handler with production dependencies:

```python
self.handler = handler or TelegramCommandHandler(
    notifier=notifier,
    order_gateway=_create_order_gateway(),
    trade_recorder=_create_trade_recorder(),
)
```

- [ ] **Step 3: Run command tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit Task 5**

```bash
git add backend/telegram_commands.py backend/tests/test_telegram_commands.py
git commit -m "feat: Telegram 주문 실행 의존성 연결" \
  -m "- 공식 KIS MCP 주문 게이트웨이 기본 연결" \
  -m "- TradeHistory recorder 기본 연결"
```

## Task 6: Documentation And Final Verification

**Files:**
- Modify: `README.md`
- Test: focused backend tests and syntax checks

- [ ] **Step 1: Document environment variables**

Add to README setup/configuration section:

```markdown
### Telegram manual order execution

Telegram `/buy` and `/sell` prepare manual limit orders. `/confirm` executes the pending order through official KIS Trading MCP.

Required for order execution:

- `FINUS_KIS_TRADING_MCP_URL`: official KIS Trading MCP URL, for example `http://host.docker.internal:3300/sse`
- `FINUS_KIS_TRADING_MCP_TRANSPORT`: `sse` or `streamable-http`
- `FINUS_KIS_TRADING_TOOL_NAME`: default `domestic_stock`
- `KIS_ORDER_ENV`: `demo` or `real`
- `KIS_REAL_ORDER_ENABLED`: must be `true` before real-account order execution is allowed
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py backend/tests/test_scheduler.py -q
```

Expected: PASS.

- [ ] **Step 3: Run broader backend tests if focused tests pass**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests -q
```

Expected: PASS or only pre-existing unrelated failures. Investigate any failure touching Telegram, scheduler, config, services, or database.

- [ ] **Step 4: Run diff checks**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors. `git status --short` should show only files intentionally changed for issue #43 and the staged spec/plan docs.

- [ ] **Step 5: Commit Task 6**

```bash
git add README.md
git commit -m "docs: Telegram 수동 주문 설정 문서화" \
  -m "- 공식 KIS MCP 주문 실행 환경 변수 정리"
```

## Task 7: Official KIS MCP Field Verification Before Real Use

**Files:**
- Modify only if verification reveals different field names: `backend/trading_orders.py`
- Test: `backend/tests/test_trading_orders.py`

- [ ] **Step 1: Query official MCP schema in a running environment**

With official KIS Trading MCP running, call `find_api_detail` for the order API through the same remote MCP transport configured for the backend.

Expected detail to confirm:

- domestic stock tool name is `domestic_stock`
- domestic cash limit order `api_type`
- side parameter name/value for buy and sell
- environment parameter name/value for demo and real
- required account fields, if the MCP does not inject them
- limit-order division value

- [ ] **Step 2: Reconcile implementation with schema**

If the official MCP reports field names different from Task 1 assumptions, update the gateway's `arguments` builder and the expected test dictionary in `test_demo_order_calls_official_mcp_with_normalized_arguments`.

- [ ] **Step 3: Run gateway tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit schema reconciliation if code changed**

```bash
git add backend/trading_orders.py backend/tests/test_trading_orders.py
git commit -m "fix: 공식 KIS MCP 주문 파라미터 정합성 반영" \
  -m "- find_api_detail 결과 기준 주문 payload 조정"
```

Skip this commit if no code changed.

## Final Validation

- [ ] Run focused tests:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py backend/tests/test_scheduler.py -q
```

- [ ] Run backend tests:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests -q
```

- [ ] Verify staged/committed scope:

```bash
git status --short
git log --oneline -8
```

- [ ] If commits are blocked by the local environment, leave the exact staged scope visible with:

```bash
git status --short --branch
git diff --stat
git diff --cached --stat
```
