import assert from "node:assert/strict";
import test from "node:test";
import {
  describeEarningsPeriod,
  formatEarningsReport,
  formatMoney,
  parseEarningsPeriod,
} from "../earnings.js";

// 합성 OpenDART 행 — 실제 삼성전자 2026 반기보고서(CFS) 원 단위 값과 같은 자릿수.
function incomeRow(accountId, accountName, current, previousQuarter, previousYear) {
  return {
    rcept_no: "20260814003699",
    sj_div: "CIS",
    account_id: accountId,
    account_nm: accountName,
    thstrm_amount: current,
    frmtrm_q_amount: previousQuarter,
    frmtrm_amount: previousYear,
    currency: "KRW",
    ord: "1",
  };
}

const SAMSUNG_ROWS = [
  incomeRow("ifrs-full_Revenue", "매출액", "171,499,470,000,000", "74,566,000,000,000", "300,000,000,000,000"),
  incomeRow("dart_OperatingIncomeLoss", "영업이익", "89,492,412,000,000", "4,676,000,000,000", "30,000,000,000,000"),
  incomeRow("ifrs-full_ProfitLoss", "반기순이익", "351,234,000,000", "(5,000,000,000)", "20,000,000,000,000"),
];
const CORP = { corp_name: "삼성전자", stock_code: "005930", corp_code: "00126380" };

test("formatMoney: 1조 이상은 조 단위 소수 둘째 자리", () => {
  assert.equal(formatMoney(171_499_470_000_000, "KRW"), "171.50조원");
  assert.equal(formatMoney(89_492_412_000_000, "KRW"), "89.49조원");
  assert.equal(formatMoney(1_000_000_000_000, "KRW"), "1.00조원");
  assert.equal(formatMoney(1_234_567_000_000_000, "KRW"), "1,234.57조원");
});

test("formatMoney: 반올림은 사사오입이고 부동소수 오차로 내림되지 않는다", () => {
  // 1.005조를 /1e12*100으로 계산하면 100.49999…가 되어 1.00조로 내림된다.
  assert.equal(formatMoney(1_005_000_000_000, "KRW"), "1.01조원");
  assert.equal(formatMoney(1_004_999_999_999, "KRW"), "1.00조원");
  assert.equal(formatMoney(150_000_000, "KRW"), "1.5억원");
  assert.equal(formatMoney(104_999_999, "KRW"), "1.0억원");
});

test("formatMoney: 1억 이상 1조 미만은 억 단위 소수 첫째 자리", () => {
  assert.equal(formatMoney(351_234_000_000, "KRW"), "3,512.3억원");
  assert.equal(formatMoney(100_000_000, "KRW"), "1.0억원");
  assert.equal(formatMoney(999_940_000_000, "KRW"), "9,999.4억원");
});

test("formatMoney: 억 단위로 반올림해 1조가 되면 조 단위로 올린다", () => {
  assert.equal(formatMoney(999_960_000_000, "KRW"), "9,999.6억원");
  assert.equal(formatMoney(999_996_000_000, "KRW"), "1.00조원");
});

test("formatMoney: 1억 미만은 원 단위 그대로", () => {
  assert.equal(formatMoney(99_999_999, "KRW"), "99,999,999원");
  assert.equal(formatMoney(1_234, "KRW"), "1,234원");
});

test("formatMoney: 음수·0·없는 값", () => {
  assert.equal(formatMoney(-1_250_000_000_000, "KRW"), "-1.25조원");
  assert.equal(formatMoney(-51_200_000_000, "KRW"), "-512.0억원");
  assert.equal(formatMoney(-5_000, "KRW"), "-5,000원");
  assert.equal(formatMoney(0, "KRW"), "0원");
  assert.equal(formatMoney(null, "KRW"), "N/A");
});

test("formatMoney: 통화가 없으면 KRW, 원화가 아니면 통화 코드를 붙인다", () => {
  assert.equal(formatMoney(2_500_000_000_000, ""), "2.50조원");
  assert.equal(formatMoney(2_500_000_000_000, "USD"), "2.50조 USD");
  assert.equal(formatMoney(1_234, "USD"), "1,234 USD");
});

test("describeEarningsPeriod: 분기 보고서는 해당 분기 3개월 값으로 적는다", () => {
  const q2 = describeEarningsPeriod(parseEarningsPeriod("2026Q2"));
  assert.equal(q2.label, "2026년 2분기(4~6월, 3개월)");
  assert.equal(q2.amount, "반기보고서의 2분기 3개월 값입니다(1월부터 누적한 값이 아님).");
  assert.equal(q2.comparison, "전년 같은 분기(2025년 2분기, 3개월) 금액 대비입니다.");

  assert.equal(describeEarningsPeriod(parseEarningsPeriod("2026Q1")).label, "2026년 1분기(1~3월, 3개월)");
  assert.equal(describeEarningsPeriod(parseEarningsPeriod("2025Q3")).label, "2025년 3분기(7~9월, 3개월)");
});

test("describeEarningsPeriod: Q4·FY는 사업보고서 연간 값이라 연간으로 적는다", () => {
  const fy = describeEarningsPeriod(parseEarningsPeriod("2025FY"));
  assert.equal(fy.label, "2025년 연간(1~12월, 12개월)");
  assert.equal(fy.amount, "사업보고서의 2025년 연간(12개월) 값입니다.");
  assert.equal(fy.comparison, "전년 연간(2024년, 12개월) 금액 대비입니다.");

  // Q4를 요청해도 계산은 연간(#475에서 다룸) — 라벨이 4분기 3개월로 오해되지 않게 적는다.
  const q4 = describeEarningsPeriod(parseEarningsPeriod("2025Q4"));
  assert.equal(q4.label, "2025년 연간(1~12월, 12개월)");
  assert.match(q4.amount, /4분기\(10~12월\) 3개월만의 값이 아니라 연간 값입니다/);
});

test("formatEarningsReport: 반기보고서 리포트에 조·억 표기와 기준 기간이 실린다", () => {
  const report = formatEarningsReport(CORP, parseEarningsPeriod("2026Q2"), "CFS", SAMSUNG_ROWS);
  const lines = report.split("\n");

  assert.ok(lines.includes("- 기준 기간: 2026년 2분기(4~6월, 3개월)"));
  assert.ok(lines.includes("[주요 실적] 2026년 2분기(4~6월, 3개월) 기준"));
  assert.ok(lines.includes("- 매출: 171.50조원 | YoY +130%"));
  assert.ok(lines.includes("- 영업이익: 89.49조원 | YoY +1813.87%"));
  // 순이익 전기 값 "(5,000,000,000)"은 음수 — YoY는 원 단위 원자료로 계산된다.
  assert.ok(lines.includes("- 순이익: 3,512.3억원 | YoY +7124.68%"));
  assert.ok(lines.includes("- 금액: 반기보고서의 2분기 3개월 값입니다(1월부터 누적한 값이 아님)."));
  assert.ok(lines.includes("- YoY: 전년 같은 분기(2025년 2분기, 3개월) 금액 대비입니다."));
  // 원 단위 15자리가 남아 있으면 LLM이 그대로 옮긴다.
  assert.doesNotMatch(report, /\d{1,3}(?:,\d{3}){4,}/);
});

test("formatEarningsReport: 연간은 전기 사업보고서 금액으로 YoY를 낸다", () => {
  const report = formatEarningsReport(CORP, parseEarningsPeriod("2025FY"), "CFS", SAMSUNG_ROWS);
  assert.match(report, /- 기준 기간: 2025년 연간\(1~12월, 12개월\)/);
  assert.match(report, /- 매출: 171\.50조원 \| YoY -42\.83%/);
});

test("formatEarningsReport: 데이터가 없어도 기준 기간을 적는다", () => {
  const report = formatEarningsReport(CORP, parseEarningsPeriod("2026Q2"), "OFS", []);
  assert.match(report, /- 기준 기간: 2026년 2분기\(4~6월, 3개월\)/);
  assert.match(report, /OpenDART 재무제표 데이터가 없습니다/);
});
