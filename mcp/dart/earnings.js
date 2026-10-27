/**
 * earnings.js
 *
 * get_earnings_report의 순수 로직 — 기간 해석, 계정 추출, 리포트 문자열 조립.
 * 네트워크·MCP 서버와 떼어 둔 것은 표기(금액 단위·기준 기간)를 단위 테스트로 고정하기 위해서다.
 * index.js를 import하면 stdio 서버가 뜨므로 테스트는 이 모듈만 불러온다.
 */
import { cleanText } from "./corp-resolver.js";

export const EARNINGS_REPORT_CODES = {
  Q1: "11013",
  Q2: "11012",
  Q3: "11014",
  // Q4를 사업보고서(연간)에 매핑하는 문제는 #475에서 다룬다. 여기서는 라벨만 사실대로 "연간"으로 적는다.
  Q4: "11011",
  FY: "11011",
};

const REPORT_NAMES = {
  11013: "1분기보고서",
  11012: "반기보고서",
  11014: "3분기보고서",
  11011: "사업보고서",
};

// 12월 결산 법인 기준 분기 개월. 결산월이 다른 법인은 개월 표기가 어긋나지만, 지금 OpenDART 조회에
// 결산월을 함께 받지 않으므로 대다수(12월 결산)에 맞춘다.
const QUARTER_MONTHS = { Q1: "1~3월", Q2: "4~6월", Q3: "7~9월", Q4: "10~12월" };

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

export function disclosureViewerUrl(rceptNo) {
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

export function parseEarningsPeriod(period) {
  const normalized = cleanText(period || defaultEarningsPeriod()).toUpperCase().replace(/[-_\s]/g, "");
  const match = normalized.match(/^(\d{4})(Q[1-4]|FY)$/);
  if (!match) {
    throw new Error("period는 YYYYQ1, YYYYQ2, YYYYQ3, YYYYQ4 또는 YYYYFY 형식이어야 합니다.");
  }

  const [, bsnsYear, quarter] = match;
  return {
    period: `${bsnsYear}${quarter}`,
    bsns_year: bsnsYear,
    quarter,
    reprt_code: EARNINGS_REPORT_CODES[quarter],
    is_annual: quarter === "Q4" || quarter === "FY",
  };
}

/**
 * 금액이 어느 기간의 값인지 사람이 읽는 문장으로 풀어 준다.
 *
 * OpenDART 분기·반기 보고서의 thstrm_amount는 해당 분기 **3개월** 값이고(누적은 thstrm_add_amount),
 * YoY 비교값 frmtrm_q_amount도 전년 같은 분기 3개월 값이다. 사업보고서는 연간 값과 전년 연간
 * 값(frmtrm_amount)이다. "2026Q2"만 적으면 3개월인지 1~6월 누적인지 알 수 없어 답에서 빠지므로
 * 기간·개월 수를 명시한다.
 */
export function describeEarningsPeriod(periodInfo) {
  const year = Number(periodInfo.bsns_year);
  const quarter = periodInfo.quarter;
  const reportName = REPORT_NAMES[periodInfo.reprt_code] || periodInfo.reprt_code;

  if (periodInfo.is_annual) {
    const amountNotes = [`${reportName}의 ${year}년 연간(12개월) 값입니다.`];
    if (quarter === "Q4") {
      amountNotes.push(`4분기(${QUARTER_MONTHS.Q4}) 3개월만의 값이 아니라 연간 값입니다.`);
    }
    return {
      label: `${year}년 연간(1~12월, 12개월)`,
      amount: amountNotes.join(" "),
      comparison: `전년 연간(${year - 1}년, 12개월) 금액 대비입니다.`,
    };
  }

  const quarterNo = quarter.slice(1);
  const months = QUARTER_MONTHS[quarter];
  const amount =
    quarter === "Q1"
      ? `${reportName}의 1분기 3개월 값입니다.`
      : `${reportName}의 ${quarterNo}분기 3개월 값입니다(1월부터 누적한 값이 아님).`;
  return {
    label: `${year}년 ${quarterNo}분기(${months}, 3개월)`,
    amount,
    comparison: `전년 같은 분기(${year - 1}년 ${quarterNo}분기, 3개월) 금액 대비입니다.`,
  };
}

export function parseAmount(value) {
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

const EOK = 100_000_000;
const JO_IN_EOK = 10_000;

function groupDigits(value, fractionDigits) {
  return value.toLocaleString("ko-KR", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

/**
 * 금액을 읽는 단위로 적는다. 1조 이상은 "171.50조원"(소수 둘째 자리), 1억 이상은 "3,512.3억원"
 * (소수 첫째 자리), 그 미만은 원 단위 그대로. 반올림은 절댓값 기준 사사오입이다.
 *
 * 원 단위 15자리("171,499,470,000,000원")를 그대로 주면 LLM이 그 자릿수를 답에 옮겨 읽을 수 없다.
 * 반올림 오차(최대 50억원·500만원)는 표기에만 있고 YoY는 원 단위 원자료로 계산한다.
 * 나눗셈은 정수를 10^10·10^7로 나눠 반올림 자리를 정수부로 끌어올린다 — "/1e12*100"처럼 하면
 * 1.005조가 1.00499…로 떨어져 내림된다.
 */
export function formatMoney(value, currency) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "N/A";
  const code = currency || "KRW";
  const won = code === "KRW";
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  const unitTail = (bigUnit) => (won ? `${bigUnit}원` : `${bigUnit} ${code}`);

  // 억 단위로 먼저 반올림해 보고, 그 값이 1만억(=1조) 이상이면 조 단위로 올린다.
  // abs가 1조 바로 아래(9,999.96억)여도 "10,000.0억원"이 아니라 "1.00조원"이 된다.
  const eokTenths = Math.round(abs / 10_000_000);
  if (eokTenths >= JO_IN_EOK * 10) {
    const joHundredths = Math.round(abs / 10_000_000_000);
    return `${sign}${groupDigits(joHundredths / 100, 2)}${unitTail("조")}`;
  }
  if (abs >= EOK) {
    return `${sign}${groupDigits(eokTenths / 10, 1)}${unitTail("억")}`;
  }
  return `${sign}${groupDigits(abs, 0)}${won ? "원" : ` ${code}`}`;
}

export function formatYoy(value) {
  if (value === null) return "N/A";
  return `${value > 0 ? "+" : ""}${value}%`;
}

export function formatEarningsReport(corp, periodInfo, fsDiv, rows) {
  const basis = describeEarningsPeriod(periodInfo);
  if (rows.length === 0) {
    return [
      `[${corp.corp_name}] DART 실적 리포트`,
      `- 종목코드: ${corp.stock_code}`,
      `- 고유번호: ${corp.corp_code}`,
      `- 조회기간: ${periodInfo.period}`,
      `- 기준 기간: ${basis.label}`,
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
    `- 기준 기간: ${basis.label}`,
    `- 사업연도/보고서: ${periodInfo.bsns_year}/${periodInfo.reprt_code}`,
    `- 재무제표: ${fsDiv}`,
    `- 원문: ${disclosureViewerUrl(firstReceipt) || "N/A"}`,
    "",
    `[주요 실적] ${basis.label} 기준`,
    `- 매출: ${formatMoney(accounts.revenue.current, currency)} | YoY ${formatYoy(accounts.revenue.yoy_change)}`,
    `- 영업이익: ${formatMoney(accounts.op_profit.current, currency)} | YoY ${formatYoy(accounts.op_profit.yoy_change)}`,
    `- 순이익: ${formatMoney(accounts.net_profit.current, currency)} | YoY ${formatYoy(accounts.net_profit.yoy_change)}`,
    "",
    "[비교 기준]",
    `- 금액: ${basis.amount}`,
    `- YoY: ${basis.comparison}`,
    "- 금액 표기: 1조 이상은 조 단위 소수 둘째 자리, 1억 이상은 억 단위 소수 첫째 자리에서 반올림했습니다. YoY는 반올림 전 원 단위 금액으로 계산했습니다.",
    "- 컨센서스 데이터는 OpenDART에서 제공되지 않으므로 별도 시장 기대치 소스가 필요합니다.",
  ].join("\n");
}
