// 연속조회 페이지 상한. inquire-balance-rlz-pl 등 다른 조회의 BALANCE_RLZ_PL_MAX_PAGES(mcp-trading/index.js)
// 명명 관례를 따른다. 실제로는 아래 BALANCE_TIME_BUDGET_MS가 먼저 걸리는 경우가 대부분이며,
// 이 값은 KIS가 tr_cont를 계속 "F"/"M"으로 응답하는 이상 상황에 대한 2차 안전장치다.
export const BALANCE_MAX_PAGES = 20;

// get_balance 전체 호출은 backend의 run_mcp_tool이 30초에서 끊는다(backend/services.py:447).
// 여기에 MCP stdio 서브프로세스 기동/핸드셰이크(수백 ms~1초대)와, 예산 초과 판정 이후에도
// 이미 시작된 요청 1회가 kisAxios의 8초 타임아웃(mcp-trading/index.js:72)만큼 더 걸릴 수 있는
// 여유를 더해야 한다. 15초 예산 + 최대 8초 초과분 + 기동 오버헤드(~1-2초)로 상위 30초 한도
// 안에 안전하게 들어오도록 잡았다.
export const BALANCE_TIME_BUDGET_MS = 15_000;

function isContinuationTrCont(value) {
  // index.js의 isKisContinueTrCont()와 동일한 KIS tr_cont 연속조회 판정을 balance.js에서도
  // 그대로 사용하기 위한 로컬 사본이다 (index.js는 부작용을 가진 서버 진입점이라 여기서 import하지 않는다).
  return value === "F" || value === "M";
}

export function buildBalanceParams(accountNo, { ctxAreaFk100 = "", ctxAreaNk100 = "" } = {}) {
  return {
    CANO: accountNo.substring(0, 8),
    ACNT_PRDT_CD: accountNo.substring(8, 10),
    AFHR_FLPR_YN: "N",
    OFL_YN: "",
    INQR_DVSN: "02",
    UNPR_DVSN: "01",
    FUND_STTL_ICLD_YN: "N",
    FNCG_AMT_AUTO_RDPT_YN: "N",
    PRCS_DVSN: "00",
    CTX_AREA_FK100: ctxAreaFk100,
    CTX_AREA_NK100: ctxAreaNk100,
  };
}

/**
 * inquire-balance 연속조회 루프. KIS 호출 자체는 fetchPage로 주입받아
 * (index.js의 kisApiGet 등) balance.js 단독으로 유닛 테스트할 수 있게 한다.
 *
 * fetchPage({ ctxAreaFk100, ctxAreaNk100, trCont }) => Promise<{ body, trCont }>
 *   - body: KIS 응답 바디 (output1/output2/ctx_area_fk100/ctx_area_nk100 포함)
 *   - trCont: 응답 tr_cont 헤더 값
 *
 * 반환: { rows, summary, pages, truncated }
 *   - truncated: null | "max_pages" | "time_budget"
 */
export async function fetchAllBalance(fetchPage, {
  maxPages = BALANCE_MAX_PAGES,
  timeBudgetMs = BALANCE_TIME_BUDGET_MS,
  now = () => Date.now(),
} = {}) {
  const rows = [];
  let summary = null;
  let trCont = "";
  let ctxFk = "";
  let ctxNk = "";
  let pages = 0;
  let truncated = null;
  const startedAt = now();

  while (true) {
    if (pages >= maxPages) {
      truncated = "max_pages";
      break;
    }
    if (pages > 0 && now() - startedAt >= timeBudgetMs) {
      truncated = "time_budget";
      break;
    }

    // fetchPage에 이번에 넘긴 커서를 기억해 둔다. 응답이 이 값을 그대로 되돌려주면
    // (repeated_cursor 가드) 같은 페이지가 반복 조회되고 있다는 뜻이다.
    const sentNk = ctxNk;

    let body;
    let respTrCont;
    try {
      ({ body, trCont: respTrCont } = await fetchPage({
        ctxAreaFk100: ctxFk,
        ctxAreaNk100: ctxNk,
        trCont,
      }));
    } catch (error) {
      // 첫 페이지 실패는 인증·계좌 설정 오류일 수 있어 원인을 숨기지 않고 그대로 전파한다.
      // 2페이지 이후 실패는 이미 확보한 페이지를 버리지 않고 부분 결과 + 잘림 표시로 반환한다.
      // (누락 방지가 목적인데 일시적 오류 1회로 잔고 전체가 사라지면 방향이 반대다.)
      if (pages === 0) throw error;
      truncated = "error";
      break;
    }

    const pageRows = Array.isArray(body.output1) ? body.output1 : [];
    rows.push(...pageRows);
    if (!summary) {
      // index.js의 fetchAllBalanceRlzPl과 동일하게 단일 객체로 정규화한다.
      // 빈 배열/undefined면 summary를 확정하지 않아, 요약이 뒤 페이지에 오는 경우를 놓치지 않는다.
      const candidate = Array.isArray(body.output2) ? body.output2[0] : body.output2;
      if (candidate) summary = candidate;
    }

    // KIS는 ctx_area_* 필드를 고정폭으로 반환해 값이 없을 때 빈 문자열 대신
    // 공백으로 채워진 문자열("          ")을 줄 수 있다. 공백 문자열은 truthy라
    // trim 없이 두면 아래 !ctxNk 가드를 통과해 버려 같은 페이지를 재요청하게 된다.
    ctxFk = String(body.ctx_area_fk100 ?? "").trim();
    ctxNk = String(body.ctx_area_nk100 ?? "").trim();
    pages += 1;

    if (!isContinuationTrCont(respTrCont)) {
      break;
    }
    if (!ctxNk) {
      // 연속 신호는 왔는데 이어받을 커서가 없다. 그대로 재요청하면 KIS가 초기 조회로 해석해
      // 같은 페이지를 상한까지 반복 조회한다. 중단하되 누락 가능성은 알린다.
      truncated = "no_cursor";
      break;
    }
    if (sentNk && ctxNk === sentNk) {
      // 같은 커서를 되돌려받았다. 그대로 재요청하면 동일 페이지를 상한까지 반복 조회하고
      // 리포트에 같은 종목이 여러 번 실릴 수 있다. pdno 기준 중복 제거는 하지 않는다 —
      // KIS가 페이지마다 동일한 pdno를 돌려주는 경우(예: 시간 예산 테스트 픽스처)가 있어
      // 서로 다른 실제 보유 종목을 하나로 합쳐버릴 위험이 있다. 대신 중단하고 알린다.
      truncated = "repeated_cursor";
      break;
    }
    trCont = "N";
  }

  return { rows, summary, pages, truncated };
}

export function formatPercent(value) {
  if (value === undefined || value === null || value === "") return "-";
  const numeric = Number(String(value).replace("%", ""));
  if (!Number.isFinite(numeric)) {
    return `${value}%`;
  }
  const formatted = (Math.floor(numeric * 100) / 100).toFixed(2);
  if (numeric > 0) {
    return `🔴 ▲ +${formatted}%`;
  }
  if (numeric < 0) {
    return `🔵 ▼ ${formatted}%`;
  }
  return `⚪ ${formatted}%`;
}

function parseNumber(value) {
  if (value === undefined || value === null || value === "") return null;
  const number = Number(String(value).replaceAll(",", ""));
  return Number.isFinite(number) ? number : null;
}

function formatAmount(value) {
  if (value === undefined || value === null || value === "") return "-";
  const number = parseNumber(value);
  if (!Number.isFinite(number)) {
    return `${value}원`;
  }
  return `${number.toLocaleString("ko-KR")}원`;
}

function formatQuantity(value) {
  if (value === undefined || value === null || value === "") return "-";
  const number = parseNumber(value);
  if (!Number.isFinite(number)) {
    return String(value);
  }
  return number.toLocaleString("ko-KR");
}

function formatSignedAmount(value) {
  const formatted = formatAmount(value);
  const number = parseNumber(value);
  if (!Number.isFinite(number) || number <= 0) {
    return formatted;
  }
  return `+${formatted}`;
}

function calculatePurchaseAmount(holdings) {
  return holdings.reduce((total, holding) => {
    const averagePrice = parseNumber(holding.pchs_avg_pric);
    const quantity = parseNumber(holding.hldg_qty);
    if (!Number.isFinite(averagePrice) || !Number.isFinite(quantity)) {
      return total;
    }
    return total + averagePrice * quantity;
  }, 0);
}

function calculateAccountReturnRate(summary, holdings) {
  const profit = parseNumber(summary.evlu_pfls_smtl_amt);
  const summaryPurchaseAmount = parseNumber(summary.pchs_amt_smtl_amt);
  const purchaseAmount = summaryPurchaseAmount > 0
    ? summaryPurchaseAmount
    : calculatePurchaseAmount(holdings);

  if (Number.isFinite(profit) && purchaseAmount > 0) {
    return (profit / purchaseAmount) * 100;
  }
  return summary.evlu_pfls_rt || summary.asst_icdc_erng_rt;
}

// 잘림 안내 문구. 주의: "- "로 시작하면 backend/scheduler.py의 extract_stocks_from_balance()가
// [보유 종목 리스트] 섹션의 "- "로 시작하는 줄을 종목명으로 오인식한다. 이 문구는 반드시
// "- " 없이, [보유 종목 리스트] 섹션이 끝난 뒤 별도 문단으로만 붙여야 한다.
function formatTruncationNote(truncated, pages) {
  if (!truncated) return "";
  const reasons = {
    max_pages: `페이지 상한(${pages}회)을 초과하여`,
    time_budget: "조회 시간 예산을 초과하여",
    no_cursor: "연속조회 커서가 오지 않아",
    repeated_cursor: "동일한 연속조회 커서가 반복되어",
    error: "연속조회 중 오류가 발생하여",
  };
  const reason = reasons[truncated] || "연속조회가 완료되지 않아";
  return `\n\n[안내] ${reason} 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.`;
}

export function formatBalanceReport(data, { pages, truncated } = {}) {
  const summary = data.output2?.[0] || {};
  const holdings = data.output1 || [];

  const stockList = holdings
    .map((h) => {
      const returnRate = h.evlu_pfls_rt || h.evlu_erng_rt;
      return (
        `- ${h.prdt_name} (${h.pdno}) · ${formatQuantity(h.hldg_qty)}주\n` +
        `  평단가 ${formatAmount(h.pchs_avg_pric)} → 평가금액 ${formatAmount(h.evlu_amt)}\n` +
        `  손익 ${formatSignedAmount(h.evlu_pfls_amt)} · 수익률 ${formatPercent(returnRate)}`
      );
    })
    .join("\n\n");

  const accountReturnRate = calculateAccountReturnRate(summary, holdings);

  return `
[계좌 잔고 현황]
- 총 평가금액: ${formatAmount(summary.tot_evlu_amt)}
- 순자산금액: ${formatAmount(summary.nass_amt || summary.pchs_amt_smtl_amt)}
- 총 손익: ${formatAmount(summary.evlu_pfls_smtl_amt)} (수익률: ${formatPercent(accountReturnRate)})
- 거래가능금액: ${formatAmount(summary.dnca_tot_amt)}
- 정산중 금액(가수도): ${formatAmount(summary.prvs_rcdl_excc_amt)}
- 익일 정산예정금액: ${formatAmount(summary.nxdy_excc_amt)}
- 금일 매수/매도: ${formatAmount(summary.thdt_buy_amt)} / ${formatAmount(summary.thdt_sll_amt)}

[보유 종목 리스트]
${stockList || "보유 종목이 없습니다."}${formatTruncationNote(truncated, pages)}
  `.trim();
}
