import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import axios from "axios";
import dotenv from "dotenv";

import { buildTokenCachePath } from "./lib/token-cache-path.js";

// Redirect console.log to console.error to prevent breaking MCP JSON-RPC on stdout
console.log = console.error;

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function uniquePaths(candidates) {
  const seen = new Set();
  const out = [];
  for (const candidate of candidates) {
    if (!candidate) continue;
    const resolved = path.resolve(candidate);
    if (seen.has(resolved)) continue;
    seen.add(resolved);
    out.push(resolved);
  }
  return out;
}

function envFileCandidates() {
  const candidates = [];

  const explicit = (process.env.FINUS_ENV_PATH || process.env.FIN_US_ENV_PATH || "").trim();
  if (explicit) {
    candidates.push(explicit);
  }

  for (const rootVar of ["FINUS_ROOT", "FIN_US_ROOT", "FINUS_VENDOR_ROOT"]) {
    const root = (process.env[rootVar] || "").trim();
    if (root) {
      candidates.push(path.join(root, ".env"));
    }
  }

  // Standard layout: fin-us/mcp-trading/index.js -> fin-us/.env
  candidates.push(path.join(__dirname, "..", ".env"));
  candidates.push(path.join(__dirname, ".env"));

  let dir = __dirname;
  for (let depth = 0; depth < 6; depth += 1) {
    candidates.push(path.join(dir, ".env"));
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }

  dir = process.cwd();
  for (let depth = 0; depth < 6; depth += 1) {
    candidates.push(path.join(dir, ".env"));
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }

  return uniquePaths(candidates);
}

function loadFinUsEnv() {
  const loadedFrom = [];
  for (const envPath of envFileCandidates()) {
    if (!fs.existsSync(envPath)) continue;
    const result = dotenv.config({ path: envPath, quiet: true });
    if (result.error) {
      console.error(`mcp-trading: failed to read ${envPath}: ${result.error.message}`);
      continue;
    }
    loadedFrom.push(envPath);
  }

  if (loadedFrom.length === 0) {
    console.error(
      "mcp-trading: no .env found. Create fin-us/.env or set FINUS_ENV_PATH to an env file.",
    );
    return;
  }

  console.error(`mcp-trading: loaded env from ${loadedFrom.join(", ")}`);
}

loadFinUsEnv();

const {
  KIS_API_KEY,
  KIS_API_SECRET,
  KIS_ACCOUNT_NO,
  KIS_URL,
} = process.env;

const KIS_BALANCE_TR_ID = KIS_URL?.includes("openapivts") ? "VTTC8434R" : "TTTC8434R";
const BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance";
const BALANCE_MAX_PAGES = 50;
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
const STOCKS_PATH = path.join(__dirname, "data", "stocks.json");
const TOKEN_TTL_MARGIN_MS = 60_000;
const TOKEN_RATE_LIMIT_CODE = "EGW00133";

function resolveTokenCachePath() {
  return buildTokenCachePath({
    url: KIS_URL,
    apiKey: KIS_API_KEY,
    accountNo: KIS_ACCOUNT_NO,
    explicit: process.env.KIS_TOKEN_CACHE_PATH,
    fallbackDir: path.join(__dirname, "..", ".state"),
  });
}

const TOKEN_CACHE_PATH = resolveTokenCachePath();
let tokenCache = null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function ensureTokenCacheDir() {
  try {
    fs.mkdirSync(path.dirname(TOKEN_CACHE_PATH), { recursive: true });
  } catch (error) {
    console.error(`KIS token cache dir create failed: ${error.message}`);
  }
}

const server = new Server(
  { name: "trading-tool", version: "1.0.0" },
  { capabilities: { tools: {} } },
);

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

function readTokenCacheFromDisk(now, { allowStale = false } = {}) {
  try {
    const text = fs.readFileSync(TOKEN_CACHE_PATH, "utf8");
    const cached = JSON.parse(text);
    const expiresAt = Number(cached?.expiresAt);
    if (
      cached &&
      typeof cached.token === "string" &&
      Number.isFinite(expiresAt) &&
      expiresAt > (allowStale ? now : now + TOKEN_TTL_MARGIN_MS)
    ) {
      return cached;
    }
  } catch (error) {
    if (error.code !== "ENOENT") {
      console.error(`KIS token cache read failed: ${error.message}`);
    }
  }
  return null;
}

function readTokenCache(now, options = {}) {
  if (tokenCache) {
    const minExpiry = options.allowStale ? now : now + TOKEN_TTL_MARGIN_MS;
    if (tokenCache.expiresAt > minExpiry) {
      return tokenCache.token;
    }
  }

  const cached = readTokenCacheFromDisk(now, options);
  if (cached) {
    tokenCache = cached;
    return cached.token;
  }

  return null;
}

function writeTokenCache(cache) {
  tokenCache = cache;
  try {
    ensureTokenCacheDir();
    fs.writeFileSync(TOKEN_CACHE_PATH, JSON.stringify(cache), { mode: 0o600 });
  } catch (error) {
    console.error(`KIS token cache write failed: ${error.message}`);
  }
}

function isKisTokenRateLimitError(error) {
  const body = error?.response?.data;
  if (!body || error?.response?.status !== 403) return false;
  const code = String(body.error_code || body.msg_cd || "");
  const text = String(body.error_description || body.msg1 || "");
  return code.includes(TOKEN_RATE_LIMIT_CODE) || text.includes("1분당 1회");
}

function loadStocks() {
  const text = fs.readFileSync(STOCKS_PATH, "utf8");
  return JSON.parse(text);
}

function normalizeStockInput(value) {
  return String(value ?? "").trim();
}

function resolveStock(stockName) {
  const input = normalizeStockInput(stockName);
  if (!input) {
    throw new Error("stock_name 파라미터가 누락되었습니다.");
  }

  if (/^\d{6}$/.test(input)) {
    return { code: input, name: input, market: "UNKNOWN" };
  }

  const stocks = loadStocks();
  const matches = stocks.filter((stock) => {
    const aliases = Array.isArray(stock.aliases) ? stock.aliases : [];
    return stock.name === input || aliases.includes(input);
  });

  if (matches.length === 0) {
    throw new Error(`'${input}'의 종목 코드를 찾을 수 없습니다. mcp-trading/data/stocks.json을 갱신하세요.`);
  }

  if (matches.length > 1) {
    const candidates = matches.map((stock) => `${stock.name}(${stock.code}, ${stock.market})`).join(", ");
    throw new Error(`'${input}'의 종목 매칭이 모호합니다: ${candidates}. 6자리 종목코드를 직접 입력하세요.`);
  }

  return matches[0];
}

// 동일 노드 프로세스 안에서 토큰 발급이 동시에 여러 번 일어나 EGW00133 을 자초하지 않도록
// in-flight Promise 를 공유한다. 다른 프로세스 간 race 는 readTokenCache(..., allowStale) 로 처리한다.
let tokenIssueInFlight = null;

async function issueAccessToken(now) {
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
    if (isKisTokenRateLimitError(error)) {
      // diary_agent 등 연속 stdio 호출: 다른 mcp-trading 프로세스가 쓴 캐시 재사용
      for (let attempt = 0; attempt < 4; attempt += 1) {
        const shared = readTokenCache(now, { allowStale: true });
        if (shared) {
          console.error(`KIS token rate-limited; reusing cached token (${TOKEN_CACHE_PATH}).`);
          return shared;
        }
        await sleep(1500);
      }
      const detail = error.response?.data?.error_description || error.response?.data?.msg1 || "1분당 1회";
      throw new Error(
        `Access Token 발급 한도 초과(403, ${TOKEN_RATE_LIMIT_CODE}): ${detail}. 1분 후 다시 시도하세요.`,
      );
    }
    throw new Error(`Access Token 발급 실패: ${error.response?.data?.msg1 || error.response?.data?.error_description || error.message}`);
  }
}

async function getAccessToken() {
  requireKisCredentials();

  const now = Date.now();
  const cachedToken = readTokenCache(now);
  if (cachedToken) return cachedToken;

  if (tokenIssueInFlight) {
    return tokenIssueInFlight;
  }

  tokenIssueInFlight = issueAccessToken(now).finally(() => {
    tokenIssueInFlight = null;
  });
  return tokenIssueInFlight;
}

async function kisApiGet(pathname, trId, params, { trCont = "" } = {}) {
  const token = await getAccessToken();
  const response = await axios.get(`${KIS_URL}${pathname}`, {
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

// KIS Open API 응답 헤더 `tr_cont` 의 의미:
//   F / M = 다음 페이지 존재 (요청 시 `tr_cont`="N" 으로 이어 호출)
//   D / E = 마지막 페이지
//   ""(공백) = 단일 페이지
// 일부 TR 은 가이드와 미묘하게 다르게 D/E 외 값을 “끝” 으로 흘리는 경우가 있으니,
// 명시적으로 “계속” 값(F/M)일 때만 다음 페이지를 받아오도록 한다.
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

function assertRealAccountForBalanceRlzPl() {
  if (KIS_URL?.includes("openapivts")) {
    throw new Error(
      "주식잔고조회_실현손익(v1_국내주식-041)은 KIS 모의투자 API를 지원하지 않습니다. 실전 KIS_URL을 사용하세요.",
    );
  }
}

function inquireBalanceBaseParams() {
  return {
    CANO: KIS_ACCOUNT_NO.substring(0, 8),
    ACNT_PRDT_CD: KIS_ACCOUNT_NO.substring(8, 10),
    AFHR_FLPR_YN: "N",
    OFL_YN: "",
    INQR_DVSN: "02",
    UNPR_DVSN: "01",
    FUND_STTL_ICLD_YN: "N",
    FNCG_AMT_AUTO_RDPT_YN: "N",
    PRCS_DVSN: "01",
  };
}

async function fetchAllInquireBalance() {
  requireKisCredentials({ accountRequired: true });

  const baseParams = inquireBalanceBaseParams();
  const rows = [];
  let summary = null;
  let trCont = "";
  let ctxFk = "";
  let ctxNk = "";
  let pages = 0;

  while (pages < BALANCE_MAX_PAGES) {
    const { body, trCont: respTrCont } = await kisApiGet(
      BALANCE_PATH,
      KIS_BALANCE_TR_ID,
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

  return { rows, summary, pages, trId: KIS_BALANCE_TR_ID };
}

function formatStockHoldingsReport({ rows, summary, pages, trId, stockLabel }) {
  if (rows.length === 0) {
    return `
[보유 종목 조회]${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (v1_국내주식-006, inquire-balance)
- 보유 종목이 없습니다.
    `.trim();
  }

  const lines = rows.map((row, index) => {
    const dayTrade =
      `금일 매수 ${formatQuantity(row.thdt_buyqty)}주 / 매도 ${formatQuantity(row.thdt_sll_qty)}주`;
    return [
      `${index + 1}. ${row.prdt_name || "-"} (${row.pdno || "-"}) · ${row.trad_dvsn_name || "-"}`,
      `   보유 ${formatQuantity(row.hldg_qty)}주 | 주문가능 ${formatQuantity(row.ord_psbl_qty)}주`,
      `   현재가 ${formatWon(row.prpr)} | 평가 ${formatWon(row.evlu_amt)} | 매입가 ${formatWon(row.pchs_avg_pric)} (매입금 ${formatWon(row.pchs_amt)})`,
      `   평가손익 ${formatWon(row.evlu_pfls_amt)} (${formatPercent(row.evlu_pfls_rt)}) | 전일대비 ${row.bfdy_cprs_icdc ?? "-"} (${formatPercent(row.fltt_rt)})`,
      `   ${dayTrade}`,
    ].join("\n");
  });

  const summaryBlock = summary
    ? `
[계좌 요약]
- 예수금: ${formatWon(summary.dnca_tot_amt)} | D+1 ${formatWon(summary.nxdy_excc_amt)} | D+2 ${formatWon(summary.prvs_rcdl_excc_amt)}
- 유가평가: ${formatWon(summary.scts_evlu_amt)} | 총평가: ${formatWon(summary.tot_evlu_amt)} | 순자산: ${formatWon(summary.nass_amt)}
- 매입합계: ${formatWon(summary.pchs_amt_smtl_amt)} | 평가손익합계: ${formatWon(summary.evlu_pfls_smtl_amt)}
- 금일 매수/매도: ${formatWon(summary.thdt_buy_amt)} / ${formatWon(summary.thdt_sll_amt)}
    `.trim()
    : "";

  return `
[보유 종목 조회]${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (v1_국내주식-006, inquire-balance)
- 종목 수: ${rows.length} (${pages}회 API 호출, 연속조회 포함)

${summaryBlock}

[보유 종목]
${lines.join("\n\n")}
  `.trim();
}

async function getStockHoldings({ stock_name: stockName } = {}) {
  const result = await fetchAllInquireBalance();
  let { rows } = result;
  let stockLabel = "";

  if (stockName) {
    const stock = resolveStock(stockName);
    stockLabel = stock.name;
    rows = rows.filter((row) => row.pdno === stock.code || row.prdt_name === stock.name);
  }

  return formatStockHoldingsReport({ ...result, rows, stockLabel });
}

async function fetchAllBalanceRlzPl() {
  requireKisCredentials({ accountRequired: true });
  assertRealAccountForBalanceRlzPl();

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

function formatPercent(value) {
  if (value === undefined || value === null || value === "") return "-";
  return `${value}%`;
}

function formatBalanceRlzPlReport({ rows, summary, pages, trId, stockLabel }) {
  if (rows.length === 0) {
    return `
[주식잔고조회_실현손익]${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (v1_국내주식-041, inquire-balance-rlz-pl)
- 보유 종목이 없습니다.
    `.trim();
  }

  const lines = rows.map((row, index) => {
    const dayTrade =
      `금일 매수 ${formatQuantity(row.thdt_buyqty)}주 / 매도 ${formatQuantity(row.thdt_sll_qty)}주`;
    return [
      `${index + 1}. ${row.prdt_name || "-"} (${row.pdno || "-"}) · ${row.trad_dvsn_name || "-"}`,
      `   보유 ${formatQuantity(row.hldg_qty)}주 | 현재가 ${formatWon(row.prpr)} | 평가 ${formatWon(row.evlu_amt)}`,
      `   평가손익 ${formatWon(row.evlu_pfls_amt)} (${formatPercent(row.evlu_pfls_rt)}) | 매입가 ${formatWon(row.pchs_avg_pric)}`,
      `   ${dayTrade} | 전일대비 ${row.bfdy_cprs_icdc ?? "-"} (${formatPercent(row.fltt_rt)})`,
    ].join("\n");
  });

  const summaryBlock = summary
    ? `
[계좌 집계]
- 예수금: ${formatWon(summary.dnca_tot_amt)}
- 총평가금액: ${formatWon(summary.tot_evlu_amt)} | 순자산: ${formatWon(summary.nass_amt)}
- 매입합계: ${formatWon(summary.pchs_amt_smtl_amt)} | 평가손익합계: ${formatWon(summary.evlu_pfls_smtl_amt)}
- 실현손익: ${formatWon(summary.rlzt_pfls)} (${formatPercent(summary.rlzt_erng_rt)})
- 실평가손익: ${formatWon(summary.real_evlu_pfls)} (${formatPercent(summary.real_evlu_pfls_erng_rt)})
- 금일 매수/매도: ${formatWon(summary.thdt_buy_amt)} / ${formatWon(summary.thdt_sll_amt)}
    `.trim()
    : "";

  return `
[주식잔고조회_실현손익]${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (v1_국내주식-041, inquire-balance-rlz-pl)
- 종목 수: ${rows.length} (${pages}회 API 호출, 연속조회 포함)

${summaryBlock}

[보유 종목]
${lines.join("\n\n")}
  `.trim();
}

async function getBalanceRlzPl({ stock_name: stockName } = {}) {
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
  const { rows: holdings, summary = {} } = await fetchAllInquireBalance();

  const stockList = holdings
    .map(
      (h) =>
        `- ${h.prdt_name || "-"} (${h.pdno || "-"}): ${formatQuantity(h.hldg_qty)}주 ` +
        `(평가금액: ${formatWon(h.evlu_amt)})`,
    )
    .join("\n");

  return `
[계좌 잔고 현황]
- 총 평가금액: ${formatWon(summary.tot_evlu_amt)}
- 순자산금액: ${formatWon(summary.nass_amt ?? summary.pchs_amt_smtl_amt)}
- 총 손익: ${formatWon(summary.evlu_pfls_smtl_amt)}
- 예수금: ${formatWon(summary.dnca_tot_amt)}

[보유 종목 리스트]
${stockList || "보유 종목이 없습니다."}
  `.trim();
}

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "get_balance",
      description: "한국투자증권 계좌의 현재 잔고 및 자산 현황을 조회합니다.",
      inputSchema: {
        type: "object",
        properties: {},
      },
    },
    {
      name: "resolve_stock_code",
      description: "종목명 또는 6자리 종목코드를 KIS API용 6자리 종목코드로 변환합니다.",
      inputSchema: {
        type: "object",
        properties: {
          stock_name: {
            type: "string",
            description: "주식 종목명 또는 6자리 종목코드",
          },
        },
        required: ["stock_name"],
      },
    },
    {
      name: "get_stock_quote",
      description: "한국투자증권 Open API로 국내 주식 현재가 시세를 조회합니다.",
      inputSchema: {
        type: "object",
        properties: {
          stock_name: {
            type: "string",
            description: "주식 종목명 또는 6자리 종목코드",
          },
        },
        required: ["stock_name"],
      },
    },
    {
      name: "get_investor_trading",
      description: "한국투자증권 Open API로 외국인/기관/개인 투자자 매매동향을 조회합니다.",
      inputSchema: {
        type: "object",
        properties: {
          stock_name: {
            type: "string",
            description: "주식 종목명 또는 6자리 종목코드",
          },
        },
        required: ["stock_name"],
      },
    },
    {
      name: "get_today_daily_orders",
      description:
        "당일(KST) 국내주식 주문·체결 내역을 전부 조회합니다. KIS 주식일별주문체결조회(inquire-daily-ccld, v1_국내주식-005)를 연속조회로 paginate합니다.",
      inputSchema: {
        type: "object",
        properties: {
          trade_date: {
            type: "string",
            description: "조회일(YYYYMMDD). 생략 시 당일(KST).",
          },
          stock_name: {
            type: "string",
            description: "특정 종목만 조회할 때 종목명 또는 6자리 코드. 생략 시 전체 종목.",
          },
          ccld_dvsn: {
            type: "string",
            description: "체결구분: 00 전체, 01 체결, 02 미체결. 기본 00.",
          },
          sll_buy_dvsn: {
            type: "string",
            description: "매도매수구분: 00 전체, 01 매도, 02 매수. 기본 00.",
          },
        },
      },
    },
    {
      name: "get_stock_holdings",
      description:
        "주식잔고조회: 계좌 보유 종목·수량·평가손익·주문가능수량을 조회합니다 (inquire-balance, v1_국내주식-006). 연속조회로 전체 보유 종목을 paginate합니다.",
      inputSchema: {
        type: "object",
        properties: {
          stock_name: {
            type: "string",
            description: "특정 종목만 볼 때 종목명 또는 6자리 코드. 생략 시 전체 보유 종목.",
          },
        },
      },
    },
    {
      name: "get_balance_rlz_pl",
      description:
        "주식잔고조회_실현손익: 체결기준 잔고, 종목별 평가손익, 계좌 실현손익·실평가손익을 조회합니다 (inquire-balance-rlz-pl, v1_국내주식-041). 모의투자 미지원.",
      inputSchema: {
        type: "object",
        properties: {
          stock_name: {
            type: "string",
            description: "특정 종목만 볼 때 종목명 또는 6자리 코드. 생략 시 전체 보유 종목.",
          },
        },
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

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

    if (name === "get_stock_holdings") {
      return { content: [{ type: "text", text: await getStockHoldings(args ?? {}) }] };
    }

    if (name === "get_today_daily_orders") {
      return { content: [{ type: "text", text: await getTodayDailyOrders(args ?? {}) }] };
    }

    if (name === "get_balance_rlz_pl") {
      return { content: [{ type: "text", text: await getBalanceRlzPl(args ?? {}) }] };
    }
  } catch (error) {
    const prefix = name === "get_balance" ? "잔고 조회 중" : `${name} 실행 중`;
    return {
      content: [{ type: "text", text: `${prefix} 에러 발생: ${error.message}` }],
      isError: true,
    };
  }

  throw new Error("존재하지 않는 도구입니다.");
});

const transport = new StdioServerTransport();
await server.connect(transport);

console.error("Trading MCP Server is running...");
