# Telegram Confirm Fill Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram `/confirm` 성공 후 주문 접수 메시지와 함께 현재 당일 주문·체결 상태를 알린다.

**Architecture:** 기존 Telegram 수동 매매 경로를 유지하고, 주문 성공 뒤 local `mcp-trading`의 `get_today_daily_orders`를 한 번 조회한다. 체결 조회 실패는 주문 성공을 실패로 바꾸지 않고 별도 경고로만 표시한다.

**Tech Stack:** Python backend, pytest-asyncio, local MCP runner, `mcp-trading` KIS order/fill tools

---

### Task 1: Telegram Confirm Status Lookup

**Files:**
- Modify: `backend/tests/test_telegram_commands.py`
- Modify: `backend/telegram_commands.py`

- [ ] **Step 1: Write failing Telegram handler tests**

Add tests near the existing `/confirm` tests:

```python
@pytest.mark.asyncio
async def test_confirm_sends_today_order_status_after_successful_order():
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        if tool_name == "resolve_stock_code":
            return "삼성전자 (005930, KOSPI)"
        if tool_name == "get_stock_quote":
            return "현재가: 74,500원"
        if tool_name == "get_balance":
            return "주문가능금액: 1,000,000원"
        if tool_name == "get_today_daily_orders":
            return "[당일 주문·체결 내역]\n주문번호 123456 | 매수 | 체결"
        raise AssertionError(f"unexpected tool: {tool_name}")

    notifier = FakeNotifier()
    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        order_gateway=FakeOrderGateway(),
        trade_recorder=FakeTradeRecorder(),
        now_factory=lambda: datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/buy 삼성전자 1 75000"}}
    )
    await handler.handle_update({"message": {"chat": {"id": 123}, "text": "/confirm"}})

    assert calls[-1] == (
        TRADING_MCP_PARAMS,
        "get_today_daily_orders",
        {"stock_name": "005930", "ccld_dvsn": "00", "sll_buy_dvsn": "02"},
    )
    assert "주문 완료: 주문 접수" in notifier.messages[-1]
    assert "현재 주문·체결 조회:" in notifier.messages[-1]
    assert "주문번호 123456" in notifier.messages[-1]
```

Also add a failure-path test proving status lookup failure keeps the order success message and trade recording.

- [ ] **Step 2: Verify tests fail**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py -q
```

Expected: the new status lookup tests fail because `_handle_confirm()` does not call `get_today_daily_orders`.

- [ ] **Step 3: Implement minimal status lookup**

In `TelegramCommandHandler._handle_confirm()`, after `place_order()` succeeds and before the final message is sent, call:

```python
await self.mcp_runner(
    TRADING_MCP_PARAMS,
    "get_today_daily_orders",
    {
        "stock_name": order.stock_code,
        "ccld_dvsn": "00",
        "sll_buy_dvsn": "02" if order.side == "BUY" else "01",
    },
)
```

Append the returned text under `현재 주문·체결 조회:`. If the lookup raises, append `주문·체결 조회 실패: ...` and keep the order success path intact.

- [ ] **Step 4: Verify focused tests pass**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py backend/tests/test_trading_orders.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_telegram_commands.py backend/telegram_commands.py
git commit -m "fix: Telegram 주문 체결 상태 알림 추가"
```

Body:

```text
- /confirm 성공 후 당일 주문·체결 조회 추가
- 체결 조회 실패 시 주문 성공 메시지 유지
- Telegram confirm 회귀 테스트 추가
```

### Task 2: Final Verification and PR

**Files:**
- Modify: `.github/PULL_REQUEST_TEMPLATE.md` only as a read source, not edited

- [ ] **Step 1: Run Telegram-focused regression suite**

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py backend/tests/test_scheduler.py -q
```

- [ ] **Step 2: Check diff hygiene**

```bash
git diff --check
git status --short
```

- [ ] **Step 3: Create Korean PR with template**

Use `.github/PULL_REQUEST_TEMPLATE.md` unchanged. Link issue `#106` and include the verification command outputs in the test plan.
