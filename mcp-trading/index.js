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
import {
  buildBalanceParams,
  fetchAllBalance,
  fetchAllPaged,
  formatBalanceReport,
  readPageDelayMsEnv,
} from "./balance.js";
import {
  formatPercent,
  formatQuantity,
  formatWon,
  isPaperTradingKisUrl,
} from "./formatters.js";
import { kisOrderPost } from "./kis-client.js";
import { formatKisRequestLog, maskLongDigitRuns, readKisRequestLogEnv } from "./kis-rate-limit.js";
import {
  OrderDedupStore,
  createOrderDedupKey,
} from "./order-dedup.js";
import {
  createCashOrderRequest,
  formatOrderResult,
} from "./order.js";
import { submitOrder } from "./order-submit.js";
import {
  buildOrderableCashParams,
  formatOrderableCashReport,
} from "./orderable-cash.js";
import { resolveStock } from "./stock-master.js";
import { KisTokenCache } from "./token-cache.js";

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
// inquire-daily-ccld 연속조회 전체 시간 예산. 호출자는 NAT 일지 에이전트(diary_agent.yml)이고
// timeout_sec: 120이 상한이다. kisAxios 요청당 8초 타임아웃 + 기동 오버헤드 ~2초를 빼면
// 헤드룸은 ~110초. 측정치가 없어 정상 부하는 알 수 없으나, 90 + 8(진행 중 요청) + 2(기동) =
// 100 < 120이므로 상한 안에 안전하게 들어온다. 90초 초과가 이상 상황이라고 판단하기에
// 충분하다고 본다. 실제 페이지당 지연 측정치가 확보되면 이 값을 재조정해야 한다.
const DAILY_CCLD_TIME_BUDGET_MS = 90_000;
// 이슈 #210: 페이지 간 대기(fetchAllPaged의 pageDelayMs)를 env로 열어둔다 — 값이 코드에
// 박혀 있으면 실측 자체가 매번 코드 변경 + 배포를 요구한다(PR #264 리뷰). 기본값은 0(대기
// 없음) — KIS 유량 제한의 실제 임계(초당 호출 수)를 실계좌 없이는 실측할 수 없어 미확인이며,
// 미확인 상태에서 기본값을 바꾸면 PR #200이 정한 "fetchAllBalance뿐 아니라 이 루프들의
// 관측 가능한 동작도 조용히 바뀌면 안 된다"는 전제를 깬다.
//
// 공식 문서 조사(#210) 결과:
// - 한국투자증권 공식 GitHub(koreainvestment/open-trading-api) README가 오류 코드
//   EGW00201("초당 거래건수 초과")의 존재를 명시하고, "모의투자 계좌는 REST API 호출
//   제한이 낮으니 연속 호출이 많으면 실전투자 계좌를 권장"한다고 정성적으로만 밝힌다 —
//   공식 출처이나 구체적 TPS 수치는 없음.
// - 같은 README가 접근토큰 재발급은 "1분당 1회"로 제한된다고 명시한다(공식, 수치 확인됨).
//   페이지 간 대기와는 별개 제한이지만 토큰 캐시(token-cache.js)가 이미 이 제한을
//   전제로 설계돼 있다는 근거다.
// - 실전투자 "표준 20 TPS"라는 수치는 언론 보도(서울경제 영문판, KIS의 Open API 완화
//   캠페인 관련 기사)에서만 확인했고 apiportal.koreainvestment.com 원문에서 직접
//   인용하지 못했다 — 반공식, 페이지 간 대기 기본값을 바꿀 근거로 쓰기엔 부족하다고 판단.
// - 모의투자의 구체적 TPS 수치, 제한 단위(계좌/앱키/TR ID 중 무엇인지)는 공식 문서에서
//   끝내 확인하지 못했다 — 미확인.
// backend/telegram_commands.py의 `/watch list`가 종목당 1.1초를 쓰지만 git blame(742fa8a,
// 커밋 메시지·코드 모두)에도 그 값의 산출 근거가 없어 그대로 가져다 쓸 근거가 못 된다
// (다른 TR이기도 하다) — 이 값도 미확인으로 남긴다.
//
// 결론: 근거가 정성적 수준이라 기본값 0을 유지한다. 실측치(또는 확정 TPS 문서)가 나오면
// 이 env로 값만 바꿔 넣으면 된다(재배포는 여전히 필요하지만 코드 변경은 불필요).
// 상한은 이 루프의 시간 예산이다 — 예산 이상의 지연은 첫 페이지 직후 예산을 소진시켜
// 연속조회를 항상 1페이지로 잘라버린다(readPageDelayMsEnv 주석 (a)).
const DAILY_CCLD_PAGE_DELAY_MS = readPageDelayMsEnv("DAILY_CCLD_PAGE_DELAY_MS", 0, {
  maxMs: DAILY_CCLD_TIME_BUDGET_MS,
});
const BALANCE_RLZ_PL_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance-rlz-pl";
const BALANCE_RLZ_PL_TR_ID = (() => {
  const override = (process.env.KIS_TR_ID_BALANCE_RLZ_PL || process.env.FINUS_KIS_TR_ID_BALANCE_RLZ_PL || "").trim();
  if (override) return override;
  return "TTTC8494R";
})();
const BALANCE_RLZ_PL_MAX_PAGES = 50;
// inquire-balance-rlz-pl 연속조회 전체 시간 예산. DAILY_CCLD_TIME_BUDGET_MS와 같은 근거.
// 두 TR의 호출자·상한·요청당 타임아웃이 동일하므로 동일한 값을 쓴다.
const BALANCE_RLZ_PL_TIME_BUDGET_MS = 90_000;
// 이슈 #210: DAILY_CCLD_PAGE_DELAY_MS와 같은 이유로 기본값 0, 같은 env 패턴으로 오버라이드.
const BALANCE_RLZ_PL_PAGE_DELAY_MS = readPageDelayMsEnv("BALANCE_RLZ_PL_PAGE_DELAY_MS", 0, {
  maxMs: BALANCE_RLZ_PL_TIME_BUDGET_MS,
});
// 이슈 #210: 요청별 타이밍 로그 게이트. 위 기본값 0을 언제 올려야 하는지는 실측 없이는
// 알 수 없고, 실측은 실계좌에서만 가능하다 — 그 실측을 "가능하게" 만드는 스위치다.
// 기본은 꺼짐: 연속조회 1회가 최대 50페이지이고 그런 도구가 셋이라 상시로 켜면 로그가 잠긴다.
// 유량 제한 분류만은 이 스위치와 무관하게 항상 남긴다(logKisRequest).
const KIS_REQUEST_LOG_ENABLED = readKisRequestLogEnv();
const PSBL_ORDER_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order";
// 매수가능조회 TR ID. 연속조회가 없는 단건 조회라 페이지 상한·시간 예산이 없다.
// 오버라이드 이름은 KIS_TR_ID_DAILY_CCLD / KIS_TR_ID_BALANCE_RLZ_PL과 같은 관례를 따른다
// (finus_nat의 _MCP_ENV_ALLOWED_PREFIXES가 "KIS_" 접두사를 통째로 통과시키므로 NAT
// 경로에도 그대로 전달된다 — finus_nat/tests/test_env_whitelist.py).
const KIS_PSBL_ORDER_TR_ID = (() => {
  const override = (process.env.KIS_TR_ID_PSBL_ORDER || process.env.FINUS_KIS_TR_ID_PSBL_ORDER || "").trim();
  if (override) return override;
  return KIS_URL?.includes("openapivts") ? "VTTC8908R" : "TTTC8908R";
})();
const TOKEN_CACHE_PATH = process.env.KIS_TOKEN_CACHE_PATH || path.join(
  os.tmpdir(),
  `finus-kis-token-${crypto
    .createHash("sha256")
    .update(`${KIS_URL || ""}:${KIS_API_KEY || ""}`)
    .digest("hex")
    .slice(0, 16)}.json`,
);
// 캐시 읽기·쓰기·발급 직렬화는 전부 이 객체가 맡는다(#324). MCP 호출마다 이
// 프로세스가 새로 뜨므로, 캐시는 프로세스 안이 아니라 프로세스 사이에서 동작한다.
const tokenCache = new KisTokenCache({ filePath: TOKEN_CACHE_PATH });
// 주문 경로에서만 쓰는 토큰 락 대기 상한. 조회 경로의 기본값(DEFAULT_LOCK_WAIT_MS,
// 10초)을 그대로 쓰면 상위 시간 예산을 넘긴다 — place_order는 토큰 확보 뒤에도
// hashkey POST와 주문 POST를 각각 치므로(kis-client.js의 kisOrderPost) kisAxios
// 타임아웃 8초짜리 요청이 조회 경로보다 하나 더 붙는다. run_mcp_tool의 상한 30초
// (backend/services.py)에서 기동 ~2초와 그 두 요청 16초를 빼면 토큰 확보에 남는 몫은
// 12초이고, 대기 뒤 자기 발급(최대 8초)까지 감안하면 대기는 4초 이하여야 한다.
// 1초 여유를 더 두어 3초로 잡는다.
//
// 대신 주문 경로는 대기가 짧은 만큼 발급이 겹칠 여지가 남는다. 그 최악은 발급 유량
// 제한에 걸린 주문이 제출 전에 끊기는 것인데(kisOrderPost의 pre-flight,
// kisOrderNotSubmitted), 주문이 제출되지 않았음이 확실한 실패라 중복 주문으로는
// 번지지 않는다. 예산을 넘겨 주문 POST 도중에 잘리는 쪽(제출 여부가 불확실해지는
// kisOrderSubmittedMaybe)이 훨씬 비싸다.
const ORDER_TOKEN_LOCK_WAIT_MS = 3_000;

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

// lockWaitMs는 주문 경로가 좁혀 주입한다(ORDER_TOKEN_LOCK_WAIT_MS). 넘기지 않으면
// token-cache.js의 기본값(10초)을 쓴다.
async function getAccessToken({ lockWaitMs } = {}) {
  requireKisCredentials();

  return tokenCache.getOrIssue(async () => {
    // 만료 시각은 요청 "전" 시각에서 센다. 응답이 늦게 오면 그만큼 캐시 수명을
    // 짧게 잡는 쪽이 안전하다.
    const now = Date.now();
    try {
      const response = await kisAxios.post(`${KIS_URL}/oauth2/tokenP`, {
        grant_type: "client_credentials",
        appkey: KIS_API_KEY,
        appsecret: KIS_API_SECRET,
      });

      const expiresIn = Number(response.data.expires_in || 86_400);
      return {
        token: response.data.access_token,
        expiresAt: now + expiresIn * 1000,
      };
    } catch (error) {
      throw new Error(`Access Token 발급 실패: ${error.response?.data?.msg1 || error.message}`);
    }
  }, { lockWaitMs });
}

// 이슈 #210: KIS 요청 1건을 stderr 한 줄로 남긴다. 줄 만들기는 kis-rate-limit.js가 하고
// (비밀 위생 계약이 그 안에 있다) 여기서는 "언제 내보내는가"만 정한다.
// 유량 제한 분류는 게이트와 무관하게 항상 내보낸다 — 드물게 나고, 이슈 #210이 찾는 신호
// 자체다. 그 밖의 요청별 타이밍 줄은 KIS_REQUEST_LOG로 켤 때만 나간다.
// stdout은 MCP JSON-RPC 채널이므로 console.error(stderr)만 쓴다. 이 stderr는 MCP Python
// SDK가 자식 프로세스를 띄울 때 부모 stderr로 그대로 이어 준다
// (docs/issue-210-rate-limit-observation.md).
function logKisRequest(input) {
  const { line, rateLimited } = formatKisRequestLog({ ...input, pid: process.pid });
  if (KIS_REQUEST_LOG_ENABLED || rateLimited) console.error(line);
}

// 계측 지점을 fetchAllPaged가 아니라 여기(kisApiGet)에 두는 것은 의도적이다. 이 프로세스가
// KIS를 치는 여섯 경로가 전부 이 함수를 지나므로(연속조회 두 루프 + getBalance + 단건
// kisGet들), 서로 다른 프로세스에서 뜬 도구 호출들의 요청이 시각 순으로 한 줄씩 남는다 —
// PR #264 리뷰가 제기한 "유량 제한이 계좌 단위인가"를 확인하려면 그 겹침을 봐야 한다.
// fetchAllPaged 안에 두면 단건 조회가 통째로 빠지고, balance.js도 건드려야 한다.
async function kisApiGet(pathname, trId, params, { trCont = "" } = {}) {
  const token = await getAccessToken();
  const startedAt = Date.now();
  let response;
  try {
    response = await kisAxios.get(`${KIS_URL}${pathname}`, {
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
  } catch (error) {
    logKisRequest({ trId, elapsedMs: Date.now() - startedAt, error });
    throw error;
  }
  logKisRequest({ trId, elapsedMs: Date.now() - startedAt, response });

  const data = response.data;
  if (data.rt_cd !== "0") {
    // msg1이 있으면 이 메시지가 msg_cd를 버린다. 그래도 여기서 error에 msg_cd를 따로
    // 태깅하지는 않는다 — 읽는 쪽이 없어서 죽은 값이 되기 때문이다(kis-client.js의
    // kisOrderRejected는 order-submit.js:17이 실제로 읽는다). 버려지던 msg_cd는 이 PR이
    // 붙인 위 `[kis-req]` 줄의 `msg_cd=` 필드가 이미 싣고 나간다.
    //
    // msg1에는 maskLongDigitRuns를 씌운다. KIS가 되울려 주는 msg1에 계좌번호가 실려 오고
    // (관측된 예: "... 계좌 50123456"), 이 error.message는 그대로 여러 갈래로 새어 나간다 —
    // balance.js:206의 stderr 줄, MCP 도구 결과 텍스트, backend services.short_error를
    // 거쳐 텔레그램까지. [kis-req] 줄만 가리고 정작 원본 메시지가 안 가려지면 마스킹이
    // 반쪽이 된다. 여기 한 군데를 가리면 그 하위 소비자가 전부 함께 닫힌다.
    // (kis-client.js:125의 같은 모양 주문 경로와 도구 결과 전반의 마스킹은 #230/#231
    //  PII 작업의 몫이라 이 PR 범위 밖이다.)
    throw new Error(`KIS API 오류: ${maskLongDigitRuns(data.msg1) || data.msg_cd || "알 수 없는 오류"}`);
  }

  const nextTrCont = response.headers?.tr_cont || response.headers?.["tr-cont"] || "";
  return { body: data, trCont: nextTrCont };
}

async function kisGet(pathname, trId, params) {
  const { body } = await kisApiGet(pathname, trId, params);
  return body;
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

async function getOrderableCash(args = {}) {
  requireKisCredentials({ accountRequired: true });

  const stock = resolveStock(args?.stock_name);
  const orderType = String(args?.order_type ?? "MARKET").trim().toUpperCase();
  if (!["LIMIT", "MARKET"].includes(orderType)) {
    throw new Error("order_type은 LIMIT 또는 MARKET 중 하나여야 합니다.");
  }

  // 지정가 기준일 때만 단가가 의미를 갖는다. 시장가 기준이면 KIS가 ORD_UNPR을 무시하므로
  // 여기서도 0으로 눕힌다 — 사용자가 넘긴 값을 리포트의 "기준 주문유형"에 시장가라고
  // 적어 놓고 계산에는 쓰는 어긋남을 만들지 않는다.
  const rawPrice = args?.price ?? 0;
  const price = Number(rawPrice);
  if (!Number.isInteger(price) || price < 0) {
    throw new Error("price는 0 이상의 정수여야 합니다.");
  }
  const basisPrice = orderType === "LIMIT" ? price : 0;

  const data = await kisGet(
    PSBL_ORDER_PATH,
    KIS_PSBL_ORDER_TR_ID,
    buildOrderableCashParams(KIS_ACCOUNT_NO, {
      stockCode: stock.code,
      price: basisPrice,
      orderType,
    }),
  );

  return formatOrderableCashReport({
    output: data.output,
    stock,
    trId: KIS_PSBL_ORDER_TR_ID,
    orderType,
    price: basisPrice,
  });
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
      getAccessToken: () => getAccessToken({ lockWaitMs: ORDER_TOKEN_LOCK_WAIT_MS }),
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

  const { rows, summary, pages, truncated } = await fetchAllPaged(
    ({ ctxAreaFk100, ctxAreaNk100, trCont }) => kisApiGet(
      DAILY_CCLD_PATH,
      KIS_DAILY_CCLD_TR_ID,
      { ...baseParams, CTX_AREA_FK100: ctxAreaFk100, CTX_AREA_NK100: ctxAreaNk100 },
      { trCont },
    ),
    {
      maxPages: DAILY_CCLD_MAX_PAGES,
      timeBudgetMs: DAILY_CCLD_TIME_BUDGET_MS,
      label: "일별 주문체결 연속조회",
      pageDelayMs: DAILY_CCLD_PAGE_DELAY_MS,
    },
  );

  return { date, rows, summary, pages, truncated, trId: KIS_DAILY_CCLD_TR_ID };
}

// 잘림 안내 문구. "- "로 시작하면 파싱 오류를 낼 수 있으므로 반드시 "[안내]"로 시작한다.
// rows.length === 0인 경우에도 출력해야 한다 — 그렇지 않으면 잘림 때문에 빈 결과가 나온 상황을
// "해당 조건의 주문·체결이 없습니다"로 사실로 단언하게 된다.
function formatPaginationTruncationNote(truncated, pages, subject) {
  if (!truncated) return "";
  const reasons = {
    max_pages: `페이지 상한(${pages}회)에 도달하여`,
    time_budget: "조회 시간 예산을 초과하여",
    no_cursor: "연속조회 커서가 오지 않아",
    repeated_cursor: "동일한 연속조회 커서가 반복되어",
    error: "연속조회 중 오류가 발생하여",
  };
  const reason = reasons[truncated] || "연속조회가 완료되지 않아";
  return `\n\n[안내] ${reason} 조회가 중단되어 일부 ${subject}이(가) 누락되었을 수 있습니다. 실제 내역은 별도로 확인하세요.`;
}

function formatDailyOrderCcldReport({ date, rows, summary, pages, truncated, trId, stockLabel }) {
  const truncationNote = formatPaginationTruncationNote(truncated, pages, "주문·체결 내역");
  if (rows.length === 0) {
    return `
[당일 주문·체결 내역] ${date}${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (주식일별주문체결조회 v1_국내주식-005)
- 결과: 해당 조건의 주문·체결이 없습니다.${truncationNote}
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

${lines.join("\n\n")}${truncationNote}
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

  const { rows, summary, pages, truncated } = await fetchAllPaged(
    ({ ctxAreaFk100, ctxAreaNk100, trCont }) => kisApiGet(
      BALANCE_RLZ_PL_PATH,
      BALANCE_RLZ_PL_TR_ID,
      { ...baseParams, CTX_AREA_FK100: ctxAreaFk100, CTX_AREA_NK100: ctxAreaNk100 },
      { trCont },
    ),
    {
      maxPages: BALANCE_RLZ_PL_MAX_PAGES,
      timeBudgetMs: BALANCE_RLZ_PL_TIME_BUDGET_MS,
      label: "실현손익 연속조회",
      pageDelayMs: BALANCE_RLZ_PL_PAGE_DELAY_MS,
    },
  );

  return { rows, summary, pages, truncated, trId: BALANCE_RLZ_PL_TR_ID };
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
const orderableCashSchema = z.object({
  stock_name: z.string().describe("주식 종목명 또는 6자리 종목코드"),
  order_type: z.enum(["LIMIT", "MARKET"]).optional().describe("가능수량 계산 기준. 기본 MARKET"),
  price: z.number().int().min(0).optional().describe("지정가. order_type이 LIMIT일 때만 사용"),
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

    if (name === "get_orderable_cash") {
      return { content: [{ type: "text", text: await getOrderableCash(args) }] };
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

server.registerTool(
  "get_orderable_cash",
  {
    description:
      "매수가능조회(v1_국내주식-007)로 주문가능현금(ord_psbl_cash)과 최대매수금액·수량을 조회합니다. "
      + "get_balance의 예수금(dnca_tot_amt)과 달리 미수·증거금·미결제 정산이 반영된 값입니다.",
    inputSchema: orderableCashSchema,
  },
  async (args) => callTradingTool("get_orderable_cash", args),
);

const transport = new StdioServerTransport();
await server.connect(transport);

console.error("Trading MCP Server is running...");
