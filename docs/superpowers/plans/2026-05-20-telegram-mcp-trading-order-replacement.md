# Telegram mcp-trading 주문 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace official KIS Trading MCP order execution with local `mcp-trading` order execution while preserving Telegram manual-order UX and safety guards.

**Architecture:** `mcp-trading` becomes the single local MCP boundary for KIS Open API reads and domestic cash orders. Backend `TelegramCommandHandler` keeps parsing, pending-order state, market-hours checks, confirmation, and history recording, but its order gateway calls `run_mcp_tool(TRADING_MCP_PARAMS, "place_order", args)` instead of a remote official MCP transport.

**Tech Stack:** Node.js ESM, `node:test`, MCP JavaScript SDK, axios, Python 3.11+, FastAPI, SQLModel, pytest, pytest-asyncio.

---

## File Structure

- Create `mcp-trading/order.js`
  - Pure order helpers: side normalization, env/url consistency, TR ID selection, account split, KIS order body creation, response formatting.
- Create `mcp-trading/tests/order.test.js`
  - Node unit tests for order helper behavior without network calls.
- Modify `mcp-trading/index.js`
  - Import order helpers, add `kisPost`, add `placeOrder`, expose `place_order` MCP tool, route tool calls.
- Modify `mcp-trading/package.json`
  - Add `test` script using `node --test tests/*.test.js`.
- Modify `backend/trading_orders.py`
  - Remove official KIS MCP remote transport code.
  - Add `McpTradingOrderGateway` that calls the local MCP runner.
- Modify `backend/telegram_commands.py`
  - Replace `OfficialKisMcpOrderGateway` factory with `McpTradingOrderGateway`.
- Modify `backend/config.py`
  - Remove official KIS MCP URL/transport/tool settings.
  - Keep `KIS_ORDER_ENV` and `KIS_REAL_ORDER_ENABLED`.
- Modify `backend/tests/test_trading_orders.py`
  - Replace official remote MCP tests with local gateway tests.
- Modify `backend/tests/test_telegram_commands.py`
  - Update gateway factory tests to expect `McpTradingOrderGateway`.
- Modify `.env.example`
  - Remove official KIS MCP envs.
- Modify `README.md`
  - Document local `mcp-trading` order execution and remove official MCP Docker requirement.

---

## Task 1: mcp-trading Order Helper Tests

**Files:**
- Create: `mcp-trading/order.js`
- Create: `mcp-trading/tests/order.test.js`
- Modify: `mcp-trading/package.json`

- [ ] **Step 1: Write failing Node tests**

Create `mcp-trading/tests/order.test.js`:

```javascript
import assert from "node:assert/strict";
import test from "node:test";
import {
  buildCashOrderBody,
  formatOrderResult,
  selectCashOrderTrId,
  validateOrderEnvMatchesUrl,
} from "../order.js";

test("selectCashOrderTrId maps demo buy and sell to paper TR IDs", () => {
  assert.equal(selectCashOrderTrId({ orderEnv: "demo", side: "BUY" }), "VTTC0012U");
  assert.equal(selectCashOrderTrId({ orderEnv: "demo", side: "SELL" }), "VTTC0011U");
});

test("selectCashOrderTrId maps real buy and sell to production TR IDs", () => {
  assert.equal(selectCashOrderTrId({ orderEnv: "real", side: "BUY" }), "TTTC0012U");
  assert.equal(selectCashOrderTrId({ orderEnv: "real", side: "SELL" }), "TTTC0011U");
});

test("validateOrderEnvMatchesUrl fails closed on env and URL mismatch", () => {
  assert.throws(
    () => validateOrderEnvMatchesUrl({ orderEnv: "demo", kisUrl: "https://openapi.koreainvestment.com:9443" }),
    /모의투자 주문은 모의투자 KIS_URL/,
  );
  assert.throws(
    () => validateOrderEnvMatchesUrl({ orderEnv: "real", kisUrl: "https://openapivts.koreainvestment.com:29443" }),
    /실계좌 주문은 실전 KIS_URL/,
  );
});

test("buildCashOrderBody creates uppercase KIS order body", () => {
  assert.deepEqual(
    buildCashOrderBody({
      accountNo: "1234567801",
      stockCode: "005930",
      quantity: 2,
      price: 70000,
    }),
    {
      CANO: "12345678",
      ACNT_PRDT_CD: "01",
      PDNO: "005930",
      ORD_DVSN: "00",
      ORD_QTY: "2",
      ORD_UNPR: "70000",
      EXCG_ID_DVSN_CD: "SOR",
      SLL_TYPE: "",
      CNDT_PRIC: "",
    },
  );
});

test("formatOrderResult includes order number when present", () => {
  assert.equal(
    formatOrderResult({
      stockName: "삼성전자",
      stockCode: "005930",
      side: "BUY",
      quantity: 1,
      price: 70000,
      data: { output: { ODNO: "12345", ORD_TMD: "101010" }, msg1: "주문이 완료되었습니다" },
    }),
    "[삼성전자] BUY 주문 접수\n- 종목코드: 005930\n- 수량/가격: 1주 / 70,000원\n- 주문번호: 12345\n- 주문시간: 101010\n- 메시지: 주문이 완료되었습니다",
  );
});
```

- [ ] **Step 2: Add minimal helper exports that make tests importable**

Create `mcp-trading/order.js`:

```javascript
export function selectCashOrderTrId() {
  return "";
}

export function validateOrderEnvMatchesUrl() {}

export function buildCashOrderBody() {
  return {};
}

export function formatOrderResult() {
  return "";
}
```

- [ ] **Step 3: Add test script**

Modify `mcp-trading/package.json` scripts:

```json
"scripts": {
  "test": "node --test tests/*.test.js"
}
```

- [ ] **Step 4: Run tests and verify they fail for behavior**

Run:

```bash
npm test --prefix mcp-trading
```

Expected: FAIL with assertion differences for TR ID, payload, or formatted text. Import errors are not expected after Step 2.

- [ ] **Step 5: Implement order helpers**

Replace `mcp-trading/order.js` with:

```javascript
function normalizeSide(side) {
  const normalized = String(side ?? "").trim().toUpperCase();
  if (normalized !== "BUY" && normalized !== "SELL") {
    throw new Error("side는 BUY 또는 SELL이어야 합니다.");
  }
  return normalized;
}

function normalizeOrderEnv(orderEnv) {
  const normalized = String(orderEnv ?? "demo").trim().toLowerCase();
  if (normalized !== "demo" && normalized !== "real") {
    throw new Error("order_env는 demo 또는 real이어야 합니다.");
  }
  return normalized;
}

function assertPositiveInteger(value, fieldName) {
  const number = Number(value);
  if (!Number.isInteger(number) || number <= 0) {
    throw new Error(`${fieldName}는 양의 정수여야 합니다.`);
  }
  return number;
}

export function selectCashOrderTrId({ orderEnv, side }) {
  const env = normalizeOrderEnv(orderEnv);
  const normalizedSide = normalizeSide(side);

  if (env === "demo") {
    return normalizedSide === "BUY" ? "VTTC0012U" : "VTTC0011U";
  }
  return normalizedSide === "BUY" ? "TTTC0012U" : "TTTC0011U";
}

export function validateOrderEnvMatchesUrl({ orderEnv, kisUrl }) {
  const env = normalizeOrderEnv(orderEnv);
  const url = String(kisUrl ?? "");
  const isPaperUrl = url.includes("openapivts");

  if (env === "demo" && !isPaperUrl) {
    throw new Error("모의투자 주문은 모의투자 KIS_URL(openapivts)이 필요합니다.");
  }
  if (env === "real" && isPaperUrl) {
    throw new Error("실계좌 주문은 실전 KIS_URL이 필요합니다.");
  }
}

export function buildCashOrderBody({ accountNo, stockCode, quantity, price }) {
  const account = String(accountNo ?? "").trim();
  const code = String(stockCode ?? "").trim();
  const orderQuantity = assertPositiveInteger(quantity, "quantity");
  const orderPrice = assertPositiveInteger(price, "price");

  if (account.length < 10) {
    throw new Error("KIS_ACCOUNT_NO가 올바르지 않습니다. 계좌번호 앞 8자리와 상품코드 2자리를 붙여 설정하세요.");
  }
  if (!/^\d{6,7}$/.test(code)) {
    throw new Error("stock_code는 6자리 또는 7자리 종목코드여야 합니다.");
  }

  return {
    CANO: account.substring(0, 8),
    ACNT_PRDT_CD: account.substring(8, 10),
    PDNO: code,
    ORD_DVSN: "00",
    ORD_QTY: String(orderQuantity),
    ORD_UNPR: String(orderPrice),
    EXCG_ID_DVSN_CD: "SOR",
    SLL_TYPE: "",
    CNDT_PRIC: "",
  };
}

export function formatOrderResult({ stockName, stockCode, side, quantity, price, data }) {
  const output = data?.output || {};
  const message = data?.msg1 || data?.msg_cd || "주문 요청이 접수되었습니다.";
  const orderNo = output.ODNO || "-";
  const orderTime = output.ORD_TMD || "-";

  return [
    `[${stockName}] ${normalizeSide(side)} 주문 접수`,
    `- 종목코드: ${stockCode}`,
    `- 수량/가격: ${Number(quantity).toLocaleString("ko-KR")}주 / ${Number(price).toLocaleString("ko-KR")}원`,
    `- 주문번호: ${orderNo}`,
    `- 주문시간: ${orderTime}`,
    `- 메시지: ${message}`,
  ].join("\n");
}
```

- [ ] **Step 6: Run tests and syntax check**

Run:

```bash
npm test --prefix mcp-trading
node --check mcp-trading/order.js
```

Expected: PASS for Node tests, syntax check exits 0.

- [ ] **Step 7: Commit**

```bash
git add mcp-trading/order.js mcp-trading/tests/order.test.js mcp-trading/package.json
git commit -m "feat: mcp-trading 주문 payload 헬퍼 추가" \
  -m "- 국내주식 현금 주문 TR ID 선택 헬퍼" \
  -m "- KIS 주문 payload 검증 테스트"
```

---

## Task 2: mcp-trading place_order Tool

**Files:**
- Modify: `mcp-trading/index.js`
- Test: `mcp-trading/tests/order.test.js`

- [ ] **Step 1: Add failing validation test for response shape**

Append to `mcp-trading/tests/order.test.js`:

```javascript
test("formatOrderResult falls back when output is missing", () => {
  assert.equal(
    formatOrderResult({
      stockName: "005930",
      stockCode: "005930",
      side: "SELL",
      quantity: 3,
      price: 68000,
      data: { msg1: "정상처리 되었습니다" },
    }),
    "[005930] SELL 주문 접수\n- 종목코드: 005930\n- 수량/가격: 3주 / 68,000원\n- 주문번호: -\n- 주문시간: -\n- 메시지: 정상처리 되었습니다",
  );
});
```

- [ ] **Step 2: Import helpers in `mcp-trading/index.js`**

Add after the `dotenv` import:

```javascript
import {
  buildCashOrderBody,
  formatOrderResult,
  selectCashOrderTrId,
  validateOrderEnvMatchesUrl,
} from "./order.js";
```

- [ ] **Step 3: Add `kisPost` next to `kisGet`**

Add after `kisGet`:

```javascript
async function kisPost(pathname, trId, body) {
  const token = await getAccessToken();
  const response = await axios.post(`${KIS_URL}${pathname}`, body, {
    headers: {
      "Content-Type": "application/json",
      authorization: `Bearer ${token}`,
      appkey: KIS_API_KEY,
      appsecret: KIS_API_SECRET,
      tr_id: trId,
      custtype: "P",
    },
  });

  const data = response.data;
  if (data.rt_cd !== "0") {
    throw new Error(`KIS API 오류: ${data.msg1 || data.msg_cd || "알 수 없는 오류"}`);
  }
  return data;
}
```

- [ ] **Step 4: Add `placeOrder` function**

Add before `server.setRequestHandler(ListToolsRequestSchema, ...)`:

```javascript
async function placeOrder(args) {
  requireKisCredentials({ accountRequired: true });

  const stockCode = String(args?.stock_code ?? "").trim() || resolveStock(args?.stock_name).code;
  const stockName = String(args?.stock_name ?? "").trim() || stockCode;
  const side = String(args?.side ?? "").trim().toUpperCase();
  const orderEnv = String(args?.order_env ?? "demo").trim().toLowerCase();

  validateOrderEnvMatchesUrl({ orderEnv, kisUrl: KIS_URL });
  const trId = selectCashOrderTrId({ orderEnv, side });
  const body = buildCashOrderBody({
    accountNo: KIS_ACCOUNT_NO,
    stockCode,
    quantity: args?.quantity,
    price: args?.price,
  });

  const data = await kisPost("/uapi/domestic-stock/v1/trading/order-cash", trId, body);
  return formatOrderResult({
    stockName,
    stockCode,
    side,
    quantity: args?.quantity,
    price: args?.price,
    data,
  });
}
```

- [ ] **Step 5: Add tool schema**

Add to the tools array:

```javascript
{
  name: "place_order",
  description: "한국투자증권 Open API로 국내 주식 현금 지정가 주문을 실행합니다.",
  inputSchema: {
    type: "object",
    properties: {
      stock_name: { type: "string", description: "주식 종목명 또는 6자리 종목코드" },
      stock_code: { type: "string", description: "KIS API용 종목코드" },
      side: { type: "string", enum: ["BUY", "SELL"], description: "매수 또는 매도" },
      quantity: { type: "integer", minimum: 1, description: "주문 수량" },
      price: { type: "integer", minimum: 1, description: "지정가" },
      order_env: { type: "string", enum: ["demo", "real"], description: "모의투자 또는 실계좌" },
    },
    required: ["stock_code", "side", "quantity", "price", "order_env"],
  },
}
```

- [ ] **Step 6: Route tool call**

Add in `CallToolRequestSchema` handler before the catch block:

```javascript
if (name === "place_order") {
  return { content: [{ type: "text", text: await placeOrder(args) }] };
}
```

- [ ] **Step 7: Run checks**

Run:

```bash
npm test --prefix mcp-trading
node --check mcp-trading/index.js
```

Expected: PASS and syntax check exits 0.

- [ ] **Step 8: Commit**

```bash
git add mcp-trading/index.js mcp-trading/tests/order.test.js
git commit -m "feat: mcp-trading 주문 실행 도구 추가" \
  -m "- place_order MCP 도구 추가" \
  -m "- KIS Open API 현금 지정가 주문 호출"
```

---

## Task 3: Backend Gateway Replacement

**Files:**
- Modify: `backend/trading_orders.py`
- Modify: `backend/telegram_commands.py`
- Modify: `backend/tests/test_trading_orders.py`
- Modify: `backend/tests/test_telegram_commands.py`

- [ ] **Step 1: Replace gateway tests**

In `backend/tests/test_trading_orders.py`, remove official transport-specific tests for SSE/streamable HTTP and add:

```python
@pytest.mark.asyncio
async def test_mcp_trading_order_gateway_calls_place_order():
    calls = []

    async def runner(params, tool_name, arguments):
        calls.append((params, tool_name, arguments))
        return "주문번호 12345"

    order = PendingOrder(
        chat_id="1",
        stock_name="삼성전자",
        stock_code="005930",
        side="BUY",
        quantity=2,
        price=70000,
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=KST),
    )
    gateway = McpTradingOrderGateway(
        server_params="trading-params",
        mcp_runner=runner,
        order_env="demo",
        real_order_enabled=False,
    )

    result = await gateway.place_order(order)

    assert result.message == "주문번호 12345"
    assert result.raw_result == "주문번호 12345"
    assert calls == [
        (
            "trading-params",
            "place_order",
            {
                "stock_name": "삼성전자",
                "stock_code": "005930",
                "side": "BUY",
                "quantity": 2,
                "price": 70000,
                "order_env": "demo",
            },
        )
    ]
```

Also replace imports:

```python
from backend.trading_orders import (
    KST,
    McpTradingOrderGateway,
    OrderExecutionResult,
    PendingOrder,
    TradeRecorder,
    _extract_order_message,
    is_korean_market_open,
)
```

- [ ] **Step 2: Run targeted test and verify failure**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py -q
```

Expected: FAIL because `McpTradingOrderGateway` is not defined.

- [ ] **Step 3: Replace official gateway implementation**

In `backend/trading_orders.py`, remove these official MCP pieces:

- `asyncio`, `httpx`, MCP client imports
- `McpTransport`
- `RemoteMcpRunner`
- `_mcp_first_text_or_error`
- `_sse_connect_timeout`
- `call_official_kis_mcp`
- `_exception_group_contains`
- `_call_official_kis_mcp_inner`
- `OfficialKisMcpOrderGateway`

Add:

```python
McpRunner = Callable[[Any, str, dict[str, Any]], Awaitable[str]]


class McpTradingOrderGateway:
    def __init__(
        self,
        *,
        server_params: Any,
        mcp_runner: McpRunner,
        order_env: Literal["real", "demo"],
        real_order_enabled: bool,
    ):
        self.server_params = server_params
        self.mcp_runner = mcp_runner
        self.order_env = order_env
        self.real_order_enabled = real_order_enabled

    async def place_order(self, order: PendingOrder) -> OrderExecutionResult:
        if self.order_env == "real" and not self.real_order_enabled:
            raise HTTPException(
                status_code=403,
                detail="실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다.",
            )

        raw_result = await self.mcp_runner(
            self.server_params,
            "place_order",
            {
                "stock_name": order.stock_name,
                "stock_code": order.stock_code,
                "side": order.side,
                "quantity": order.quantity,
                "price": order.price,
                "order_env": self.order_env,
            },
        )

        return OrderExecutionResult(
            stock_code=order.stock_code,
            stock_name=order.stock_name,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            message=_extract_order_message(raw_result),
            raw_result=raw_result,
        )
```

- [ ] **Step 4: Replace Telegram factory**

In `backend/telegram_commands.py`, replace imports from config with:

```python
from .config import KIS_ORDER_ENV, KIS_REAL_ORDER_ENABLED, TRADING_MCP_PARAMS
```

Replace trading order import:

```python
from .trading_orders import (
    KST,
    McpTradingOrderGateway,
    PendingOrder,
    TradeRecorder,
    is_korean_market_open,
)
```

Replace `_create_order_gateway`:

```python
def _create_order_gateway() -> McpTradingOrderGateway:
    return McpTradingOrderGateway(
        server_params=TRADING_MCP_PARAMS,
        mcp_runner=run_mcp_tool,
        order_env=KIS_ORDER_ENV,
        real_order_enabled=KIS_REAL_ORDER_ENABLED,
    )
```

- [ ] **Step 5: Update Telegram factory test**

In `backend/tests/test_telegram_commands.py`, replace the official gateway factory test with:

```python
def test_create_order_gateway_uses_local_mcp_trading(monkeypatch):
    captured = {}

    class FakeGateway:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(telegram_commands, "McpTradingOrderGateway", FakeGateway)
    monkeypatch.setattr(telegram_commands, "KIS_ORDER_ENV", "demo")
    monkeypatch.setattr(telegram_commands, "KIS_REAL_ORDER_ENABLED", False)

    gateway = telegram_commands._create_order_gateway()

    assert isinstance(gateway, FakeGateway)
    assert captured == {
        "server_params": TRADING_MCP_PARAMS,
        "mcp_runner": telegram_commands.run_mcp_tool,
        "order_env": "demo",
        "real_order_enabled": False,
    }
```

- [ ] **Step 6: Run focused backend tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_trading_orders.py backend/tests/test_telegram_commands.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/trading_orders.py backend/telegram_commands.py backend/tests/test_trading_orders.py backend/tests/test_telegram_commands.py
git commit -m "refactor: Telegram 주문 실행을 mcp-trading으로 전환" \
  -m "- 공식 KIS MCP 주문 어댑터 제거" \
  -m "- 로컬 mcp-trading place_order 게이트웨이 연결"
```

---

## Task 4: Config and Docs Replacement

**Files:**
- Modify: `backend/config.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-05-20-telegram-manual-trading-design.md`
- Modify: `docs/superpowers/plans/2026-05-20-telegram-manual-trading.md`

- [ ] **Step 1: Remove official MCP config**

In `backend/config.py`, remove:

```python
KIS_TRADING_MCP_URL = os.environ.get(
    "FINUS_KIS_TRADING_MCP_URL",
    "http://host.docker.internal:3300/sse",
).strip()
KIS_TRADING_MCP_TRANSPORT = os.environ.get("FINUS_KIS_TRADING_MCP_TRANSPORT", "sse").strip()
KIS_TRADING_MCP_TOOL_NAME = os.environ.get("FINUS_KIS_TRADING_TOOL_NAME", "domestic_stock").strip()
```

Keep:

```python
KIS_ORDER_ENV = os.environ.get("KIS_ORDER_ENV", "demo").strip().lower()
KIS_REAL_ORDER_ENABLED = os.environ.get("KIS_REAL_ORDER_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
}
```

- [ ] **Step 2: Remove `.env.example` official MCP entries**

Remove:

```dotenv
FINUS_KIS_TRADING_MCP_URL=http://host.docker.internal:3300/sse
FINUS_KIS_TRADING_MCP_TRANSPORT=sse
FINUS_KIS_TRADING_TOOL_NAME=domestic_stock
```

Ensure the remaining order settings are:

```dotenv
KIS_ORDER_ENV=demo
KIS_REAL_ORDER_ENABLED=false
```

- [ ] **Step 3: Update README manual trading section**

Replace the official MCP paragraph with:

```markdown
`/buy`와 `/sell`은 60초 동안 유효한 지정가 주문 확인 대기를 만들고, `/confirm`은 로컬 `mcp-trading`의 `place_order` 도구를 통해 한국투자증권 Open API 현금 주문을 제출합니다. `/cancel`은 Telegram 확인 대기만 취소하며 이미 증권사에 제출된 주문은 취소하지 않습니다. `/balance`, `/quote`, `/trend` 조회 명령도 같은 로컬 `mcp-trading`을 사용합니다. 실계좌 주문은 `KIS_ORDER_ENV=real`과 `KIS_REAL_ORDER_ENABLED=true`가 모두 설정되어야 실행됩니다.
```

- [ ] **Step 4: Mark old spec and plan as superseded**

At the top of `docs/superpowers/specs/2026-05-20-telegram-manual-trading-design.md`, add:

```markdown
> Superseded: 주문 실행 provider는 공식 KIS Trading MCP가 아니라 로컬 `mcp-trading`으로 전환되었습니다. 현재 기준 문서는 `docs/superpowers/specs/2026-05-20-telegram-mcp-trading-order-replacement-design.md`입니다.
```

At the top of `docs/superpowers/plans/2026-05-20-telegram-manual-trading.md`, add:

```markdown
> Superseded: 공식 KIS Trading MCP 실행 계획은 로컬 `mcp-trading` 주문 실행 계획으로 대체되었습니다. 현재 기준 문서는 `docs/superpowers/plans/2026-05-20-telegram-mcp-trading-order-replacement.md`입니다.
```

- [ ] **Step 5: Run config/docs checks**

Run:

```bash
rg -n "FINUS_KIS_TRADING|KIS_TRADING_MCP|OfficialKisMcp|official KIS Trading MCP|공식 KIS MCP" backend README.md .env.example docs/superpowers
git diff --check
```

Expected: matches only in superseded historical paragraphs or no matches in runtime files; `git diff --check` exits 0.

- [ ] **Step 6: Commit**

```bash
git add backend/config.py .env.example README.md docs/superpowers/specs/2026-05-20-telegram-manual-trading-design.md docs/superpowers/plans/2026-05-20-telegram-manual-trading.md
git commit -m "docs: mcp-trading 주문 실행 설정 정리" \
  -m "- 공식 KIS MCP 환경 변수 제거" \
  -m "- Telegram 수동 주문 문서 기준 전환"
```

---

## Task 5: Final Verification

**Files:**
- Verify only; commit fixes only if a command exposes a real issue.

- [ ] **Step 1: Run Node checks**

Run:

```bash
npm test --prefix mcp-trading
node --check mcp-trading/index.js
node --check mcp-trading/order.js
```

Expected: all commands pass.

- [ ] **Step 2: Run focused Telegram tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py backend/tests/test_scheduler.py backend/tests/test_trading_orders.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run full backend tests**

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests -q
```

Expected: full suite passes with the existing skipped tests unchanged.

- [ ] **Step 4: Run final diff checks**

Run:

```bash
git diff --check
git status --short
git log --oneline -n 8
```

Expected: no whitespace errors; status is clean after commits; recent log shows atomic replacement commits after the previous official-MCP commits.

- [ ] **Step 5: Prepare PR note**

Use this summary in the PR body or follow-up comment:

```markdown
## 변경 요약

- 공식 KIS Trading MCP 주문 실행 경로를 로컬 `mcp-trading` `place_order` 도구로 대체
- Telegram 수동 주문 UX, pending-order TTL, 실계좌 enable guard, 주문 성공 이력 기록 유지
- 공식 KIS MCP Docker/remote env 설정 제거

## 검증

- `npm test --prefix mcp-trading`
- `node --check mcp-trading/index.js`
- `node --check mcp-trading/order.js`
- `UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests/test_telegram_commands.py backend/tests/test_telegram_notifier.py backend/tests/test_scheduler.py backend/tests/test_trading_orders.py -q`
- `UV_CACHE_DIR=.uv-cache uv run --project backend pytest backend/tests -q`
- `git diff --check`
```
