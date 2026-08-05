import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import AdmZip from "adm-zip";
import { XMLParser } from "fast-xml-parser";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { cleanText, resolveCorp } from "./corp-resolver.js";

console.log = console.error;

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootEnvPath = path.join(__dirname, "..", ".env");
const dataDir = path.join(__dirname, "data");
const corpCodeCachePath = path.join(dataDir, "corp-codes.json");

const CORP_CODE_ENDPOINT = "https://opendart.fss.or.kr/api/corpCode.xml";
const DISCLOSURE_LIST_ENDPOINT = "https://opendart.fss.or.kr/api/list.json";
const MAJOR_STOCK_ENDPOINT = "https://opendart.fss.or.kr/api/majorstock.json";
const ELE_STOCK_ENDPOINT = "https://opendart.fss.or.kr/api/elestock.json";
const FINANCIAL_STATEMENT_ENDPOINT = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json";
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 10000;
const DISCLOSURE_DETAIL_TYPES = ["D001", "D002", "D005"];
const EARNINGS_REPORT_CODES = {
  Q1: "11013",
  Q2: "11012",
  Q3: "11014",
  Q4: "11011",
  FY: "11011",
};
const EARNINGS_ACCOUNT_MATCHERS = {
  revenue: {
    label: "매출",
    accountIds: [
      "ifrs-full_Revenue",
      "ifrs-full_RevenueFromContractsWithCustomersExcludingAssessedTax",
    ],
    accountNames: ["매출액", "수익(매출액)", "영업수익"],
  },
  op_profit: {
    label: "영업이익",
    accountIds: ["dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"],
    accountNames: ["영업이익", "영업이익(손실)"],
  },
  net_profit: {
    label: "순이익",
    accountIds: ["ifrs-full_ProfitLoss"],
    accountNames: ["당기순이익", "분기순이익", "반기순이익", "연결당기순이익"],
  },
};

function loadRootEnv() {
  if (!fs.existsSync(rootEnvPath)) return;

  const envText = fs.readFileSync(rootEnvPath, "utf8");
  for (const line of envText.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const eqIndex = trimmed.indexOf("=");
    if (eqIndex === -1) continue;

    const key = trimmed.slice(0, eqIndex).trim();
    let value = trimmed.slice(eqIndex + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (process.env[key] === undefined) process.env[key] = value;
  }
}

loadRootEnv();

const server = new McpServer({ name: "mcp-dart", version: "1.0.0" });

function isMissingCredential(value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return true;

  const lower = normalized.toLowerCase();
  return (
    lower.startsWith("your_") ||
    lower.includes("_here") ||
    lower.includes("placeholder") ||
    lower === "dart_api_key" ||
    /^x+$/.test(lower)
  );
}

function requireDartApiKey() {
  const apiKey = process.env.DART_API_KEY;
  if (isMissingCredential(apiKey)) {
    throw new Error("DART_API_KEY가 설정되지 않았습니다. 루트 .env 파일을 확인하세요.");
  }
  return apiKey.trim();
}

function asArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function formatDate(date) {
  const kst = new Date(date.getTime() + 9 * 60 * 60 * 1000);
  return kst.toISOString().slice(0, 10).replaceAll("-", "");
}

function getQueryWindow() {
  const end = new Date();
  const begin = new Date(end);
  begin.setDate(begin.getDate() - 30);
  return { bgn_de: formatDate(begin), end_de: formatDate(end) };
}

async function fetchWithTimeout(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    return await fetch(url, { signal: controller.signal });
  } finally {
    clearTimeout(timeout);
  }
}

async function fetchJson(endpoint, params) {
  const url = new URL(endpoint);
  for (const [key, value] of Object.entries(params)) {
    url.searchParams.set(key, value);
  }

  const response = await fetchWithTimeout(url);
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error(`OpenDART JSON 파싱 오류(${url.pathname}): ${error.message}`);
  }

  if (!response.ok) {
    const message = payload.message || response.statusText;
    throw new Error(`OpenDART HTTP 오류(${response.status}): ${message}`);
  }

  // OpenDART는 조회 결과 없음도 status=013으로 응답한다. signal 요구사항상 빈 목록으로 처리한다.
  if (payload.status === "013") return { ...payload, list: [] };
  if (payload.status && payload.status !== "000") {
    throw new Error(`OpenDART API 오류(${payload.status}): ${payload.message || "알 수 없는 오류"}`);
  }

  return payload;
}

function extractOpenDartXmlStatus(parsed) {
  const result = parsed?.result ?? parsed;
  return {
    status: cleanText(result?.status),
    message: cleanText(result?.message),
  };
}

function assertNotOpenDartXmlError(body) {
  const text = body.toString("utf8").trimStart();
  if (!text.startsWith("<") || (!text.includes("<status>") && !text.includes("<message>"))) {
    return;
  }

  const parser = new XMLParser({ parseTagValue: false, trimValues: true });
  const { status, message } = extractOpenDartXmlStatus(parser.parse(text));
  if (status && status !== "000") {
    throw new Error(`OpenDART API 오류(${status}): ${message || "알 수 없는 오류"}`);
  }

  throw new Error("OpenDART corpCode.xml 응답이 ZIP 파일이 아닙니다.");
}

async function readFreshCorpCodeCache() {
  try {
    const text = await fsp.readFile(corpCodeCachePath, "utf8");
    const cache = JSON.parse(text);
    const fetchedAt = new Date(cache.fetched_at).getTime();
    const fresh = Number.isFinite(fetchedAt) && Date.now() - fetchedAt < CACHE_TTL_MS;
    if (fresh && cache.source === CORP_CODE_ENDPOINT && Array.isArray(cache.items)) {
      return cache.items;
    }
  } catch {
    return null;
  }

  return null;
}

async function downloadCorpCodes(apiKey) {
  const url = new URL(CORP_CODE_ENDPOINT);
  url.searchParams.set("crtfc_key", apiKey);

  const response = await fetchWithTimeout(url);
  const body = Buffer.from(await response.arrayBuffer());
  assertNotOpenDartXmlError(body);

  if (!response.ok) {
    throw new Error(`OpenDART corpCode.xml 다운로드 오류(${response.status}): ${response.statusText}`);
  }

  const zip = new AdmZip(body);
  const entry = zip
    .getEntries()
    .find((zipEntry) => path.basename(zipEntry.entryName).toUpperCase() === "CORPCODE.XML");
  if (!entry) {
    throw new Error("OpenDART corpCode.xml ZIP에서 CORPCODE.xml을 찾지 못했습니다.");
  }

  const parser = new XMLParser({ parseTagValue: false, trimValues: true });
  const parsed = parser.parse(entry.getData().toString("utf8"));
  const rows = asArray(parsed?.result?.list);
  const items = rows
    .map((row) => ({
      corp_code: cleanText(row.corp_code),
      corp_name: cleanText(row.corp_name),
      stock_code: cleanText(row.stock_code),
      modify_date: cleanText(row.modify_date),
    }))
    .filter((row) => row.corp_code && row.corp_name && /^\d{6}$/.test(row.stock_code));

  if (items.length === 0) {
    throw new Error("OpenDART corpCode.xml에서 상장회사 종목코드를 파싱하지 못했습니다.");
  }

  await fsp.mkdir(dataDir, { recursive: true });
  await fsp.writeFile(
    corpCodeCachePath,
    `${JSON.stringify({ fetched_at: new Date().toISOString(), source: CORP_CODE_ENDPOINT, items }, null, 2)}\n`,
    "utf8",
  );

  return items;
}

async function getCorpCodes(apiKey) {
  return (await readFreshCorpCodeCache()) || (await downloadCorpCodes(apiKey));
}

async function fetchDisclosureList(apiKey, corp, window) {
  const groups = await Promise.all(
    DISCLOSURE_DETAIL_TYPES.map(async (detailType) => {
      const payload = await fetchJson(DISCLOSURE_LIST_ENDPOINT, {
        crtfc_key: apiKey,
        corp_code: corp.corp_code,
        bgn_de: window.bgn_de,
        end_de: window.end_de,
        pblntf_ty: "D",
        pblntf_detail_ty: detailType,
        page_no: "1",
        page_count: "10",
        sort: "date",
        sort_mth: "desc",
      });
      return asArray(payload.list);
    }),
  );

  return groups
    .flat()
    .sort((a, b) => cleanText(b.rcept_dt).localeCompare(cleanText(a.rcept_dt)))
    .slice(0, 10);
}

async function fetchMajorStock(apiKey, corp) {
  const payload = await fetchJson(MAJOR_STOCK_ENDPOINT, {
    crtfc_key: apiKey,
    corp_code: corp.corp_code,
    page_no: "1",
    page_count: "10",
    sort: "date",
    sort_mth: "desc",
  });
  return asArray(payload.list);
}

async function fetchEleStock(apiKey, corp) {
  const payload = await fetchJson(ELE_STOCK_ENDPOINT, {
    crtfc_key: apiKey,
    corp_code: corp.corp_code,
    page_no: "1",
    page_count: "10",
    sort: "date",
    sort_mth: "desc",
  });
  return asArray(payload.list);
}

function disclosureViewerUrl(rceptNo) {
  return rceptNo ? `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${rceptNo}` : "";
}

function defaultEarningsPeriod() {
  const now = new Date();
  const kst = new Date(now.getTime() + 9 * 60 * 60 * 1000);
  const year = kst.getUTCFullYear();
  const month = kst.getUTCMonth() + 1;
  const day = kst.getUTCDate();
  const mmdd = month * 100 + day;

  if (mmdd >= 1115) return `${year}Q3`;
  if (mmdd >= 815) return `${year}Q2`;
  if (mmdd >= 515) return `${year}Q1`;
  return `${year - 1}FY`;
}

function parseEarningsPeriod(period) {
  const normalized = cleanText(period || defaultEarningsPeriod()).toUpperCase().replace(/[-_\s]/g, "");
  const match = normalized.match(/^(\d{4})(Q[1-4]|FY)$/);
  if (!match) {
    throw new Error("period는 YYYYQ1, YYYYQ2, YYYYQ3, YYYYQ4 또는 YYYYFY 형식이어야 합니다.");
  }

  const [, bsnsYear, quarter] = match;
  return {
    period: `${bsnsYear}${quarter}`,
    bsns_year: bsnsYear,
    reprt_code: EARNINGS_REPORT_CODES[quarter],
    is_annual: quarter === "Q4" || quarter === "FY",
  };
}

async function fetchFinancialStatements(apiKey, corp, periodInfo, fsDiv) {
  const payload = await fetchJson(FINANCIAL_STATEMENT_ENDPOINT, {
    crtfc_key: apiKey,
    corp_code: corp.corp_code,
    bsns_year: periodInfo.bsns_year,
    reprt_code: periodInfo.reprt_code,
    fs_div: fsDiv,
  });
  return asArray(payload.list);
}

async function fetchBestFinancialStatements(apiKey, corp, periodInfo) {
  const cfs = await fetchFinancialStatements(apiKey, corp, periodInfo, "CFS");
  if (cfs.length > 0) return { fs_div: "CFS", rows: cfs };
  return { fs_div: "OFS", rows: await fetchFinancialStatements(apiKey, corp, periodInfo, "OFS") };
}

function parseAmount(value) {
  const normalized = cleanText(value).replaceAll(",", "");
  if (!normalized || normalized === "-") return null;
  const negative = normalized.startsWith("(") && normalized.endsWith(")");
  const numeric = Number(normalized.replace(/[()]/g, ""));
  if (!Number.isFinite(numeric)) return null;
  return negative ? -numeric : numeric;
}

function yoyPercent(current, previous) {
  if (current === null || previous === null || previous === 0) return null;
  return Number((((current - previous) / Math.abs(previous)) * 100).toFixed(2));
}

function isIncomeStatementRow(row) {
  return ["IS", "CIS"].includes(cleanText(row.sj_div));
}

function accountScore(row, matcher) {
  if (!isIncomeStatementRow(row)) return -1;
  const accountId = cleanText(row.account_id);
  const accountName = cleanText(row.account_nm);
  if (matcher.accountIds.includes(accountId)) return 100;
  if (matcher.accountNames.includes(accountName)) return 90;
  if (matcher.accountNames.some((name) => accountName.includes(name))) return 70;
  return -1;
}

function pickAccountRow(rows, matcher) {
  return rows
    .map((row) => ({ row, score: accountScore(row, matcher) }))
    .filter((item) => item.score >= 0)
    .sort((a, b) => b.score - a.score || Number(cleanText(a.row.ord) || 9999) - Number(cleanText(b.row.ord) || 9999))[0]
    ?.row ?? null;
}

function extractEarningsAccount(rows, key, periodInfo) {
  const matcher = EARNINGS_ACCOUNT_MATCHERS[key];
  const row = pickAccountRow(rows, matcher);
  if (!row) {
    return {
      label: matcher.label,
      account_name: "",
      current: null,
      previous: null,
      yoy_change: null,
    };
  }

  const current = parseAmount(row.thstrm_amount);
  const previous = parseAmount(periodInfo.is_annual ? row.frmtrm_amount : row.frmtrm_q_amount);
  return {
    label: matcher.label,
    account_name: cleanText(row.account_nm),
    current,
    previous,
    yoy_change: yoyPercent(current, previous),
  };
}

function formatMoney(value, currency) {
  if (value === null) return "N/A";
  return `${value.toLocaleString("ko-KR")} ${currency || "KRW"}`;
}

function formatYoy(value) {
  if (value === null) return "N/A";
  return `${value > 0 ? "+" : ""}${value}%`;
}

function formatEarningsReport(corp, periodInfo, fsDiv, rows) {
  if (rows.length === 0) {
    return [
      `[${corp.corp_name}] DART 실적 리포트`,
      `- 종목코드: ${corp.stock_code}`,
      `- 고유번호: ${corp.corp_code}`,
      `- 조회기간: ${periodInfo.period}`,
      "- 조회 결과: OpenDART 재무제표 데이터가 없습니다.",
    ].join("\n");
  }

  const currency = cleanText(rows.find((row) => cleanText(row.currency))?.currency) || "KRW";
  const accounts = {
    revenue: extractEarningsAccount(rows, "revenue", periodInfo),
    op_profit: extractEarningsAccount(rows, "op_profit", periodInfo),
    net_profit: extractEarningsAccount(rows, "net_profit", periodInfo),
  };
  const firstReceipt = cleanText(rows.find((row) => cleanText(row.rcept_no))?.rcept_no);

  return [
    `[${corp.corp_name}] DART 실적 리포트`,
    `- 종목코드: ${corp.stock_code}`,
    `- 고유번호: ${corp.corp_code}`,
    `- 조회기간: ${periodInfo.period}`,
    `- 사업연도/보고서: ${periodInfo.bsns_year}/${periodInfo.reprt_code}`,
    `- 재무제표: ${fsDiv}`,
    `- 원문: ${disclosureViewerUrl(firstReceipt) || "N/A"}`,
    "",
    "[주요 실적]",
    `- 매출: ${formatMoney(accounts.revenue.current, currency)} | YoY ${formatYoy(accounts.revenue.yoy_change)}`,
    `- 영업이익: ${formatMoney(accounts.op_profit.current, currency)} | YoY ${formatYoy(accounts.op_profit.yoy_change)}`,
    `- 순이익: ${formatMoney(accounts.net_profit.current, currency)} | YoY ${formatYoy(accounts.net_profit.yoy_change)}`,
    "",
    "[비교 기준]",
    periodInfo.is_annual
      ? "- YoY는 전기 사업보고서 금액 기준입니다."
      : "- YoY는 전기 동분기 금액 기준입니다.",
    "- 컨센서스 데이터는 OpenDART에서 제공되지 않으므로 별도 시장 기대치 소스가 필요합니다.",
  ].join("\n");
}

function formatDisclosureItem(item) {
  const parts = [
    cleanText(item.rcept_dt),
    cleanText(item.report_nm),
    cleanText(item.rcept_no) ? `접수번호 ${cleanText(item.rcept_no)}` : "",
    cleanText(item.flr_nm) ? `제출인 ${cleanText(item.flr_nm)}` : "",
    disclosureViewerUrl(cleanText(item.rcept_no)),
  ].filter(Boolean);
  return `- ${parts.join(" | ")}`;
}

function formatMajorStockItem(item) {
  const parts = [
    cleanText(item.rcept_dt),
    cleanText(item.repror) ? `보고자: ${cleanText(item.repror)}` : "",
    cleanText(item.report_tp) ? `보고구분: ${cleanText(item.report_tp)}` : "",
    cleanText(item.stkrt) ? `보유비율: ${cleanText(item.stkrt)}%` : "",
    cleanText(item.stkrt_irds) ? `변동비율: ${cleanText(item.stkrt_irds)}%` : "",
    cleanText(item.report_resn) ? `보고사유: ${cleanText(item.report_resn)}` : "",
    cleanText(item.rcept_no) ? `접수번호 ${cleanText(item.rcept_no)}` : "",
  ].filter(Boolean);
  return `- ${parts.join(" | ")}`;
}

function formatEleStockItem(item) {
  const relation = [
    cleanText(item.isu_exctv_rgist_at),
    cleanText(item.isu_exctv_ofcps),
    cleanText(item.isu_main_shrholdr),
  ].filter(Boolean);
  const parts = [
    cleanText(item.rcept_dt),
    cleanText(item.repror) ? `보고자: ${cleanText(item.repror)}` : "",
    relation.length > 0 ? `직위/관계: ${relation.join(", ")}` : "",
    cleanText(item.sp_stock_lmp_cnt) ? `소유주식수: ${cleanText(item.sp_stock_lmp_cnt)}` : "",
    cleanText(item.sp_stock_lmp_irds_cnt) ? `증감: ${cleanText(item.sp_stock_lmp_irds_cnt)}` : "",
    cleanText(item.sp_stock_lmp_rate) ? `소유비율: ${cleanText(item.sp_stock_lmp_rate)}%` : "",
    cleanText(item.rcept_no) ? `접수번호 ${cleanText(item.rcept_no)}` : "",
  ].filter(Boolean);
  return `- ${parts.join(" | ")}`;
}

function formatSection(items, formatter, emptyLine) {
  return items.length > 0 ? items.map(formatter).join("\n") : `- ${emptyLine}`;
}

function formatDisclosureSignal(corp, window, disclosures, majorStocks, eleStocks) {
  return [
    `[${corp.corp_name}] DART 지분공시 signal`,
    `- 종목코드: ${corp.stock_code}`,
    `- 고유번호: ${corp.corp_code}`,
    `- 조회기간: ${window.bgn_de}~${window.end_de}`,
    "",
    "[최신 공시]",
    formatSection(disclosures, formatDisclosureItem, "최근 30일 내 지분공시 목록이 없습니다."),
    "",
    "[5% 룰 대량보유 요약]",
    formatSection(majorStocks, formatMajorStockItem, "OpenDART 대량보유 상황보고 데이터가 없습니다."),
    "",
    "[임원 주요주주 거래 요약]",
    formatSection(eleStocks, formatEleStockItem, "OpenDART 임원ㆍ주요주주 소유보고 데이터가 없습니다."),
  ].join("\n");
}

async function getDisclosureSignal(stockName) {
  const apiKey = requireDartApiKey();
  const corp = resolveCorp(await getCorpCodes(apiKey), stockName);
  const window = getQueryWindow();
  const [disclosures, majorStocks, eleStocks] = await Promise.all([
    fetchDisclosureList(apiKey, corp, window),
    fetchMajorStock(apiKey, corp),
    fetchEleStock(apiKey, corp),
  ]);

  return formatDisclosureSignal(corp, window, disclosures, majorStocks, eleStocks);
}

async function getEarningsReport(stockName, period) {
  const apiKey = requireDartApiKey();
  const corp = resolveCorp(await getCorpCodes(apiKey), stockName);
  const periodInfo = parseEarningsPeriod(period);
  const { fs_div: fsDiv, rows } = await fetchBestFinancialStatements(apiKey, corp, periodInfo);

  return formatEarningsReport(corp, periodInfo, fsDiv, rows);
}

server.registerTool(
  "get_disclosure_signal",
  {
    description: "OpenDART 공식 API로 5% 룰 및 임원/주요주주 지분공시 signal을 조회합니다.",
    inputSchema: z.object({
      stock_name: z.string().describe("주식 종목명 또는 6자리 종목코드"),
    }),
  },
  async (args) => {
    const stockName = args?.stock_name;
    if (!stockName) {
      return {
        content: [{ type: "text", text: "에러: stock_name 파라미터가 누락되었습니다." }],
        isError: true,
      };
    }

    try {
      const signal = await getDisclosureSignal(stockName);
      return { content: [{ type: "text", text: signal }] };
    } catch (error) {
      return { content: [{ type: "text", text: `에러 발생: ${error.message}` }], isError: true };
    }
  },
);

server.registerTool(
  "get_earnings_report",
  {
    description: "OpenDART 공식 API로 분기/연간 실적 주요 계정과 YoY 변화를 조회합니다.",
    inputSchema: z.object({
      stock_name: z.string().describe("주식 종목명 또는 6자리 종목코드"),
      period: z
        .string()
        .optional()
        .describe("조회 기간. 예: 2025Q1, 2025Q2, 2025Q3, 2025FY. 생략 시 최근 공시 가능 기간을 추정합니다."),
    }),
  },
  async (args) => {
    const stockName = args?.stock_name;
    if (!stockName) {
      return {
        content: [{ type: "text", text: "에러: stock_name 파라미터가 누락되었습니다." }],
        isError: true,
      };
    }

    try {
      const report = await getEarningsReport(stockName, args?.period);
      return { content: [{ type: "text", text: report }] };
    } catch (error) {
      return { content: [{ type: "text", text: `에러 발생: ${error.message}` }], isError: true };
    }
  },
);

const transport = new StdioServerTransport();
await server.connect(transport);

console.error("DART MCP Server is running...");
