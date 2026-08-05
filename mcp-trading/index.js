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
import { formatBalanceRlzPlReport } from "./balance-rlz-pl-report.js";
import { buildBalanceParams, fetchAllBalance, formatBalanceReport } from "./balance.js";
import {
  formatPercent,
  formatQuantity,
  formatWon,
  isPaperTradingKisUrl,
} from "./formatters.js";
import { kisOrderPost } from "./kis-client.js";
import {
  OrderDedupStore,
  createOrderDedupKey,
} from "./order-dedup.js";
import {
  createCashOrderRequest,
  formatOrderResult,
} from "./order.js";
import { submitOrder } from "./order-submit.js";
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
const KIS_DAILY_CCLD_TR_ID = (() => {
  const override = (process.env.KIS_TR_ID_DAILY_CCLD || process.env.FINUS_KIS_TR_ID_DAILY_CCLD || "").trim();
  if (override) return override;
  return KIS_URL?.includes("openapivts") ? "VTTC0081R" : "TTTC0081R";
})();
const DAILY_CCLD_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld";
const DAILY_CCLD_MAX_PAGES = 50;
const BALANCE_RLZ_PL_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance-rlz-pl";
const BALANCE_RLZ_PL_TR_ID = (() => {
  const override = (process.env.KIS_TR_ID_BALANCE_RLZ_PL || process.env.FINUS_KIS_TR_ID_BALANCE_RLZ_PL || "").trim();
  if (override) return override;
  return "TTTC8494R";
})();
const BALANCE_RLZ_PL_MAX_PAGES = 50;
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
// KIS API 호출용 axios 인스턴스 — 8초 타임아웃으로 무기한 블로킹 방지
const kisAxios = axios.create({ timeout: 8000 });
const orderDedupStore = new OrderDedupStore();

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
    const response = await kisAxios.post(`${KIS_URL}/oauth2/tokenP`, {
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

async function kisApiGet(pathname, trId, params, { trCont = "" } = {}) {
  const token = await getAccessToken();
  const response = await kisAxios.get(`${KIS_URL}${pathname}`, {
    headers: {
      "Content-Type": "application/json",
      authorization: `Bearer ${token}`,
      appkey: KIS_API_KEY,
      appsecret: KIS_API_SECRET,
      tr_id: trId,
      tr_cont: trCont,
      custtype: "P",
    },
    params,
  });

  const data = response.data;
  if (data.rt_cd !== "0") {
    throw new Error(`KIS API 오류: ${data.msg1 || data.msg_cd || "알 수 없는 오류"}`);
  }

  const nextTrCont = response.headers?.tr_cont || response.headers?.["tr-cont"] || "";
  return { body: data, trCont: nextTrCont };
}

async function kisGet(pathname, trId, params) {
  const { body } = await kisApiGet(pathname, trId, params);
  return body;
}

function isKisContinueTrCont(value) {
  return value === "F" || value === "M";
}

function todayKstYmd() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const year = parts.find((p) => p.type === "year")?.value;
  const month = parts.find((p) => p.type === "month")?.value;
  const day = parts.find((p) => p.type === "day")?.value;
  return `${year}${month}${day}`;
}

function normalizeYmd(value, fallback) {
  const text = String(value ?? "").trim();
  if (!text) return fallback;
  if (!/^\d{8}$/.test(text)) {
    throw new Error("trade_date는 YYYYMMDD 형식이어야 합니다.");
  }
  return text;
}

function formatOrderTime(value) {
  const text = String(value ?? "").trim();
  if (!text || text.length < 6) return text || "-";
  return `${text.slice(0, 2)}:${text.slice(2, 4)}:${text.slice(4, 6)}`;
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

  const { rows, summary, pages, truncated } = await fetchAllBalance(
    ({ ctxAreaFk100, ctxAreaNk100, trCont }) => kisApiGet(
      "/uapi/domestic-stock/v1/trading/inquire-balance",
      KIS_BALANCE_TR_ID,
      buildBalanceParams(KIS_ACCOUNT_NO, { ctxAreaFk100, ctxAreaNk100 }),
      { trCont },
    ),
  );

  return formatBalanceReport(
    { output1: rows, output2: summary ? [summary] : [] },
    { pages, truncated },
  );
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
  const dedupKey = createOrderDedupKey({
    accountNo: KIS_ACCOUNT_NO,
    orderEnv,
    stockCode,
    side,
    quantity,
    price,
    orderType,
  });

  orderDedupStore.reserve(dedupKey, {
    pathname: request.pathname,
    trId: request.trId,
    body: request.body,
  });

  const data = await submitOrder({
    dedupStore: orderDedupStore,
    dedupKey,
    submit: () => kisOrderPost({
      kisAxios,
      kisUrl: KIS_URL,
      appKey: KIS_API_KEY,
      appSecret: KIS_API_SECRET,
      pathname: request.pathname,
      trId: request.trId,
      body: request.body,
      useHashKey: true,
      getAccessToken,
    }),
  });
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

async function fetchAllDailyOrderCcld({
  tradeDate,
  stockCode = "",
  ccldDvsn = "00",
  sllBuyDvsn = "00",
}) {
  requireKisCredentials({ accountRequired: true });

  const date = normalizeYmd(tradeDate, todayKstYmd());
  const baseParams = {
    CANO: KIS_ACCOUNT_NO.substring(0, 8),
    ACNT_PRDT_CD: KIS_ACCOUNT_NO.substring(8, 10),
    INQR_STRT_DT: date,
    INQR_END_DT: date,
    SLL_BUY_DVSN_CD: sllBuyDvsn,
    INQR_DVSN: "00",
    PDNO: stockCode,
    CCLD_DVSN: ccldDvsn,
    ORD_GNO_BRNO: "",
    ODNO: "",
    INQR_DVSN_3: "00",
    INQR_DVSN_1: "",
  };

  const rows = [];
  let summary = null;
  let trCont = "";
  let ctxFk = "";
  let ctxNk = "";
  let pages = 0;

  while (pages < DAILY_CCLD_MAX_PAGES) {
    const { body, trCont: respTrCont } = await kisApiGet(
      DAILY_CCLD_PATH,
      KIS_DAILY_CCLD_TR_ID,
      {
        ...baseParams,
        CTX_AREA_FK100: ctxFk,
        CTX_AREA_NK100: ctxNk,
      },
      { trCont },
    );

    const pageRows = Array.isArray(body.output1) ? body.output1 : [];
    rows.push(...pageRows);
    if (body.output2 && !summary) {
      summary = Array.isArray(body.output2) ? body.output2[0] : body.output2;
    }

    ctxFk = body.ctx_area_fk100 || "";
    ctxNk = body.ctx_area_nk100 || "";
    pages += 1;

    if (!isKisContinueTrCont(respTrCont)) {
      break;
    }
    trCont = "N";
  }

  return { date, rows, summary, pages, trId: KIS_DAILY_CCLD_TR_ID };
}

function formatDailyOrderCcldReport({ date, rows, summary, pages, trId, stockLabel }) {
  if (rows.length === 0) {
    return `
[당일 주문·체결 내역] ${date}${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (주식일별주문체결조회 v1_국내주식-005)
- 결과: 해당 조건의 주문·체결이 없습니다.
    `.trim();
  }

  const lines = rows.map((row, index) => {
    const side = row.sll_buy_dvsn_cd_name || row.sll_buy_dvsn_cd || "-";
    const status = row.cncl_yn === "Y" ? "취소" : (Number(row.rmn_qty || 0) > 0 ? "미체결잔량" : "체결");
    return [
      `${index + 1}. ${row.prdt_name || "-"} (${row.pdno || "-"})`,
      `   주문번호 ${row.odno || "-"} | ${side} | ${status}`,
      `   주문 ${formatQuantity(row.ord_qty)}주 @ ${formatWon(row.ord_unpr)} (${formatOrderTime(row.ord_tmd)})`,
      `   체결 ${formatQuantity(row.tot_ccld_qty)}주 / 잔량 ${formatQuantity(row.rmn_qty)}주 | 평균 ${formatWon(row.avg_prvs)} | 금액 ${formatWon(row.tot_ccld_amt)}`,
    ].join("\n");
  });

  const summaryLines = summary
    ? `- 집계(output2): 총주문수량 ${formatQuantity(summary.tot_ord_qty)}주 | 총체결수량 ${formatQuantity(summary.tot_ccld_qty)}주 | 총체결금액 ${formatWon(summary.tot_ccld_amt)}`
    : "";

  return `
[당일 주문·체결 내역] ${date}${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (주식일별주문체결조회 v1_국내주식-005)
- 건수: ${rows.length}건 (${pages}회 API 호출, 연속조회 포함)
${summaryLines}

${lines.join("\n\n")}
  `.trim();
}

async function getTodayDailyOrders({
  trade_date: tradeDate,
  stock_name: stockName,
  ccld_dvsn: ccldDvsn = "00",
  sll_buy_dvsn: sllBuyDvsn = "00",
} = {}) {
  let stockCode = "";
  let stockLabel = "";
  if (stockName) {
    const stock = resolveStock(stockName);
    stockCode = stock.code;
    stockLabel = stock.name;
  }

  const normalizedCcld = String(ccldDvsn || "00").trim();
  const normalizedSide = String(sllBuyDvsn || "00").trim();
  if (!["00", "01", "02"].includes(normalizedCcld)) {
    throw new Error("ccld_dvsn은 00(전체), 01(체결), 02(미체결) 중 하나여야 합니다.");
  }
  if (!["00", "01", "02"].includes(normalizedSide)) {
    throw new Error("sll_buy_dvsn은 00(전체), 01(매도), 02(매수) 중 하나여야 합니다.");
  }

  const result = await fetchAllDailyOrderCcld({
    tradeDate: normalizeYmd(tradeDate, todayKstYmd()),
    stockCode,
    ccldDvsn: normalizedCcld,
    sllBuyDvsn: normalizedSide,
  });

  return formatDailyOrderCcldReport({ ...result, stockLabel });
}

async function fetchAllBalanceRlzPl() {
  requireKisCredentials({ accountRequired: true });

  const baseParams = {
    CANO: KIS_ACCOUNT_NO.substring(0, 8),
    ACNT_PRDT_CD: KIS_ACCOUNT_NO.substring(8, 10),
    AFHR_FLPR_YN: "N",
    OFL_YN: "",
    INQR_DVSN: "02",
    UNPR_DVSN: "01",
    FUND_STTL_ICLD_YN: "N",
    FNCG_AMT_AUTO_RDPT_YN: "N",
    PRCS_DVSN: "01",
    COST_ICLD_YN: "N",
  };

  const rows = [];
  let summary = null;
  let trCont = "";
  let ctxFk = "";
  let ctxNk = "";
  let pages = 0;

  while (pages < BALANCE_RLZ_PL_MAX_PAGES) {
    const { body, trCont: respTrCont } = await kisApiGet(
      BALANCE_RLZ_PL_PATH,
      BALANCE_RLZ_PL_TR_ID,
      {
        ...baseParams,
        CTX_AREA_FK100: ctxFk,
        CTX_AREA_NK100: ctxNk,
      },
      { trCont },
    );

    const pageRows = Array.isArray(body.output1) ? body.output1 : [];
    rows.push(...pageRows);
    if (body.output2 && !summary) {
      summary = Array.isArray(body.output2) ? body.output2[0] : body.output2;
    }

    ctxFk = body.ctx_area_fk100 || "";
    ctxNk = body.ctx_area_nk100 || "";
    pages += 1;

    if (!isKisContinueTrCont(respTrCont)) {
      break;
    }
    trCont = "N";
  }

  return { rows, summary, pages, trId: BALANCE_RLZ_PL_TR_ID };
}

async function getBalanceRlzPl({ stock_name: stockName } = {}) {
  if (isPaperTradingKisUrl(KIS_URL)) {
    const balanceText = await getBalance();
    const note =
      "\n\n[안내] 모의투자(openapivts) 계좌는 실현손익 TR(v1_국내주식-041)을 지원하지 않아 잔고 요약으로 대체했습니다.";
    return `${balanceText}${note}`;
  }

  const result = await fetchAllBalanceRlzPl();
  let { rows } = result;
  let stockLabel = "";

  if (stockName) {
    const stock = resolveStock(stockName);
    stockLabel = stock.name;
    rows = rows.filter((row) => row.pdno === stock.code || row.prdt_name === stock.name);
  }

  return formatBalanceRlzPlReport({ ...result, rows, stockLabel });
}

const stockNameSchema = z.object({
  stock_name: z.string().describe("주식 종목명 또는 6자리 종목코드"),
});
const optionalStockNameSchema = z.object({
  stock_name: z.string().optional().describe("주식 종목명 또는 6자리 종목코드"),
});
const todayOrdersSchema = z.object({
  trade_date: z.string().optional().describe("조회일 YYYYMMDD. 생략 시 당일(KST)"),
  stock_name: z.string().optional().describe("종목명 또는 6자리 종목코드"),
  ccld_dvsn: z.enum(["00", "01", "02"]).optional().describe("00: 전체, 01: 체결, 02: 미체결"),
  sll_buy_dvsn: z.enum(["00", "01", "02"]).optional().describe("00: 전체, 01: 매도, 02: 매수"),
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

    if (name === "get_today_daily_orders") {
      return { content: [{ type: "text", text: await getTodayDailyOrders(args) }] };
    }

    if (name === "get_balance_rlz_pl") {
      return { content: [{ type: "text", text: await getBalanceRlzPl(args) }] };
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
    description: "한국투자증권 Open API로 국내 주식 현금 주문을 실행합니다. 동일 주문은 중복 방지 TTL 동안 차단되므로 자동 재시도하지 마세요.",
    inputSchema: placeOrderSchema,
  },
  async (args) => callTradingTool("place_order", args),
);

server.registerTool(
  "get_today_daily_orders",
  {
    description:
      "당일(KST) 국내주식 주문·체결 내역을 조회합니다. 연속조회로 전체 건을 수집합니다.",
    inputSchema: todayOrdersSchema,
  },
  async (args) => callTradingTool("get_today_daily_orders", args),
);

server.registerTool(
  "get_balance_rlz_pl",
  {
    description:
      "주식잔고조회_실현손익(v1_국내주식-041)으로 보유·평가·실현손익을 조회합니다. 실전 계좌 전용.",
    inputSchema: optionalStockNameSchema,
  },
  async (args) => callTradingTool("get_balance_rlz_pl", args),
);

const transport = new StdioServerTransport();
await server.connect(transport);

console.error("Trading MCP Server is running...");
