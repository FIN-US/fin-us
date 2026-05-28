import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import axios from "axios";
import dotenv from "dotenv";
import { z } from "zod";
import { buildBalanceParams, formatBalanceReport } from "./balance.js";
import {
  createCashOrderRequest,
  formatOrderResult,
} from "./order.js";
import { resolveStock } from "./stock-master.js";

// Redirect console.log to console.error to prevent breaking MCP JSON-RPC on stdout
console.log = console.error;

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
dotenv.config({ path: path.join(__dirname, "..", ".env") });

const {
  KIS_API_KEY,
  KIS_API_SECRET,
  KIS_ACCOUNT_NO,
  KIS_URL,
  KIS_REAL_ORDER_ENABLED,
} = process.env;

const KIS_BALANCE_TR_ID = KIS_URL?.includes("openapivts") ? "VTTC8434R" : "TTTC8434R";
const TOKEN_TTL_MARGIN_MS = 60_000;
const TOKEN_CACHE_PATH = process.env.KIS_TOKEN_CACHE_PATH || path.join(
  os.tmpdir(),
  `finus-kis-token-${crypto
    .createHash("sha256")
    .update(`${KIS_URL || ""}:${KIS_API_KEY || ""}`)
    .digest("hex")
    .slice(0, 16)}.json`,
);
let tokenCache = null;

const server = new McpServer({ name: "trading-tool", version: "1.0.0" });

function isMissingCredential(value) {
  return !value || value.startsWith("your_") || value.includes("_here");
}

function requireKisCredentials({ accountRequired = false } = {}) {
  if (isMissingCredential(KIS_API_KEY) || isMissingCredential(KIS_API_SECRET) || isMissingCredential(KIS_URL)) {
    throw new Error("KIS API 설정이 누락되었습니다. 루트 .env의 KIS_API_KEY, KIS_API_SECRET, KIS_URL을 확인하세요.");
  }

  if (accountRequired && (isMissingCredential(KIS_ACCOUNT_NO) || KIS_ACCOUNT_NO.length < 10)) {
    throw new Error("KIS_ACCOUNT_NO가 올바르지 않습니다. 계좌번호 앞 8자리와 상품코드 2자리를 붙여 설정하세요.");
  }
}

function readTokenCache(now) {
  if (tokenCache && tokenCache.expiresAt > now + TOKEN_TTL_MARGIN_MS) {
    return tokenCache.token;
  }

  try {
    const text = fs.readFileSync(TOKEN_CACHE_PATH, "utf8");
    const cached = JSON.parse(text);
    if (
      cached &&
      typeof cached.token === "string" &&
      Number(cached.expiresAt) > now + TOKEN_TTL_MARGIN_MS
    ) {
      tokenCache = cached;
      return cached.token;
    }
  } catch (error) {
    if (error.code !== "ENOENT") {
      console.error(`KIS token cache read failed: ${error.message}`);
    }
  }

  return null;
}

function writeTokenCache(cache) {
  tokenCache = cache;
  try {
    fs.writeFileSync(TOKEN_CACHE_PATH, JSON.stringify(cache), { mode: 0o600 });
  } catch (error) {
    console.error(`KIS token cache write failed: ${error.message}`);
  }
}

async function getAccessToken() {
  requireKisCredentials();

  const now = Date.now();
  const cachedToken = readTokenCache(now);
  if (cachedToken) return cachedToken;

  try {
    const response = await axios.post(`${KIS_URL}/oauth2/tokenP`, {
      grant_type: "client_credentials",
      appkey: KIS_API_KEY,
      appsecret: KIS_API_SECRET,
    });

    const expiresIn = Number(response.data.expires_in || 86_400);
    const cache = {
      token: response.data.access_token,
      expiresAt: now + expiresIn * 1000,
    };
    writeTokenCache(cache);
    return cache.token;
  } catch (error) {
    throw new Error(`Access Token 발급 실패: ${error.response?.data?.msg1 || error.message}`);
  }
}

async function kisGet(pathname, trId, params) {
  const token = await getAccessToken();
  const response = await axios.get(`${KIS_URL}${pathname}`, {
    headers: {
      "Content-Type": "application/json",
      authorization: `Bearer ${token}`,
      appkey: KIS_API_KEY,
      appsecret: KIS_API_SECRET,
      tr_id: trId,
      custtype: "P",
    },
    params,
  });

  const data = response.data;
  if (data.rt_cd !== "0") {
    throw new Error(`KIS API 오류: ${data.msg1 || data.msg_cd || "알 수 없는 오류"}`);
  }
  return data;
}

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

function formatWon(value) {
  if (value === undefined || value === null || value === "") return "-";
  return `${Number(value).toLocaleString("ko-KR")}원`;
}

function formatQuantity(value) {
  if (value === undefined || value === null || value === "") return "-";
  return Number(value).toLocaleString("ko-KR");
}

async function getStockQuote(stockName) {
  const stock = resolveStock(stockName);
  const data = await kisGet(
    "/uapi/domestic-stock/v1/quotations/inquire-price",
    "FHKST01010100",
    {
      FID_COND_MRKT_DIV_CODE: "J",
      FID_INPUT_ISCD: stock.code,
    },
  );

  const output = data.output || {};
  return `
[${stock.name}] 현재가 시세
- 종목코드: ${stock.code}
- 현재가: ${formatWon(output.stck_prpr)}
- 전일 대비: ${output.prdy_vrss || "-"} (${output.prdy_ctrt || "-"}%)
- 거래량: ${output.acml_vol || "-"}
- 시가/고가/저가: ${formatWon(output.stck_oprc)} / ${formatWon(output.stck_hgpr)} / ${formatWon(output.stck_lwpr)}
  `.trim();
}

async function getInvestorTrading(stockName) {
  const stock = resolveStock(stockName);
  const data = await kisGet(
    "/uapi/domestic-stock/v1/quotations/inquire-investor",
    "FHKST01010900",
    {
      FID_COND_MRKT_DIV_CODE: "J",
      FID_INPUT_ISCD: stock.code,
    },
  );

  const rows = Array.isArray(data.output) ? data.output.slice(0, 5) : [data.output || {}];
  const lines = rows.map((row) => {
    const date = row.stck_bsop_date || "기준일 미상";
    return (
      `${date} | 개인: ${formatQuantity(row.prsn_ntby_qty)} | ` +
      `외국인: ${formatQuantity(row.frgn_ntby_qty)} | 기관: ${formatQuantity(row.orgn_ntby_qty)}`
    );
  });

  return `
[${stock.name}] 투자자 매매동향
- 종목코드: ${stock.code}
- 최근 수급:
${lines.join("\n")}
- 기준 데이터: 한국투자증권 Open API inquire-investor
  `.trim();
}

async function getBalance() {
  requireKisCredentials({ accountRequired: true });

  const data = await kisGet(
    "/uapi/domestic-stock/v1/trading/inquire-balance",
    KIS_BALANCE_TR_ID,
    buildBalanceParams(KIS_ACCOUNT_NO),
  );

  return formatBalanceReport(data);
}

async function placeOrder(args) {
  requireKisCredentials({ accountRequired: true });

  const stockCode = String(args?.stock_code ?? "").trim() || resolveStock(args?.stock_name).code;
  const stockName = String(args?.stock_name ?? "").trim() || stockCode;
  const side = String(args?.side ?? "").trim().toUpperCase();
  const quantity = args?.quantity;
  const orderType = String(args?.order_type ?? "LIMIT").trim().toUpperCase();
  const price = args?.price ?? 0;
  const orderEnv = String(args?.order_env ?? "demo").trim().toLowerCase();
  const request = createCashOrderRequest({
    accountNo: KIS_ACCOUNT_NO,
    kisUrl: KIS_URL,
    orderEnv,
    side,
    stockCode,
    quantity,
    price,
    orderType,
    realOrderEnabled: KIS_REAL_ORDER_ENABLED === "true",
  });

  const data = await kisPost(request.pathname, request.trId, request.body);
  return formatOrderResult({
    stockName,
    stockCode,
    side,
    quantity,
    price,
    orderType,
    data,
  });
}

const stockNameSchema = z.object({
  stock_name: z.string().describe("주식 종목명 또는 6자리 종목코드"),
});
const placeOrderSchema = z.object({
  stock_name: z.string().optional().describe("주식 종목명 또는 6자리 종목코드"),
  stock_code: z.string().describe("KIS API용 종목코드"),
  side: z.enum(["BUY", "SELL"]).describe("매수 또는 매도"),
  quantity: z.number().int().min(1).describe("주문 수량"),
  price: z.number().int().min(0).optional().describe("지정가. 시장가 주문은 0 또는 생략"),
  order_type: z.enum(["LIMIT", "MARKET"]).optional().describe("지정가 또는 시장가"),
  order_env: z.enum(["demo", "real"]).describe("모의투자 또는 실계좌"),
});

async function callTradingTool(name, args = {}) {
  try {
    if (name === "get_balance") {
      return { content: [{ type: "text", text: await getBalance() }] };
    }

    if (name === "resolve_stock_code") {
      const stock = resolveStock(args?.stock_name);
      return {
        content: [{ type: "text", text: `${stock.name} (${stock.code}, ${stock.market})` }],
      };
    }

    if (name === "get_stock_quote") {
      return { content: [{ type: "text", text: await getStockQuote(args?.stock_name) }] };
    }

    if (name === "get_investor_trading") {
      return { content: [{ type: "text", text: await getInvestorTrading(args?.stock_name) }] };
    }

    if (name === "place_order") {
      return { content: [{ type: "text", text: await placeOrder(args) }] };
    }
  } catch (error) {
    const prefix = name === "get_balance" ? "잔고 조회 중" : `${name} 실행 중`;
    return {
      content: [{ type: "text", text: `${prefix} 에러 발생: ${error.message}` }],
      isError: true,
    };
  }

  throw new Error("존재하지 않는 도구입니다.");
}

server.registerTool(
  "get_balance",
  {
    description: "한국투자증권 계좌의 현재 잔고 및 자산 현황을 조회합니다.",
    inputSchema: z.object({}),
  },
  async () => callTradingTool("get_balance"),
);

server.registerTool(
  "resolve_stock_code",
  {
    description: "종목명 또는 6자리 종목코드를 KIS API용 6자리 종목코드로 변환합니다.",
    inputSchema: stockNameSchema,
  },
  async (args) => callTradingTool("resolve_stock_code", args),
);

server.registerTool(
  "get_stock_quote",
  {
    description: "한국투자증권 Open API로 국내 주식 현재가 시세를 조회합니다.",
    inputSchema: stockNameSchema,
  },
  async (args) => callTradingTool("get_stock_quote", args),
);

server.registerTool(
  "get_investor_trading",
  {
    description: "한국투자증권 Open API로 외국인/기관/개인 투자자 매매동향을 조회합니다.",
    inputSchema: stockNameSchema,
  },
  async (args) => callTradingTool("get_investor_trading", args),
);

server.registerTool(
  "place_order",
  {
    description: "한국투자증권 Open API로 국내 주식 현금 주문을 실행합니다.",
    inputSchema: placeOrderSchema,
  },
  async (args) => callTradingTool("place_order", args),
);

const transport = new StdioServerTransport();
await server.connect(transport);

console.error("Trading MCP Server is running...");
