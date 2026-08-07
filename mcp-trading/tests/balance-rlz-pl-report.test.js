import assert from "node:assert/strict";
import test from "node:test";
import { formatBalanceRlzPlReport } from "../balance-rlz-pl-report.js";
import { isPaperTradingKisUrl } from "../formatters.js";

test("formatBalanceRlzPlReport includes account summary when holdings are empty", () => {
  const text = formatBalanceRlzPlReport({
    rows: [],
    summary: {
      dnca_tot_amt: "1000",
      tot_evlu_amt: "0",
      nass_amt: "1000",
      pchs_amt_smtl_amt: "0",
      evlu_pfls_smtl_amt: "0",
      rlzt_pfls: "50000",
      rlzt_erng_rt: "12.5",
      real_evlu_pfls: "0",
      real_evlu_pfls_erng_rt: "0",
      thdt_buy_amt: "0",
      thdt_sll_amt: "100000",
    },
    pages: 1,
    trId: "TTTC8494R",
    stockLabel: "",
  });

  assert.match(text, /보유 종목이 없습니다/);
  assert.match(text, /실현손익: 50,000원 \(12.5%\)/);
  assert.doesNotMatch(text, /\[보유 종목\]\s*$/m);
});

test("formatBalanceRlzPlReport: rows가 없어도 truncated가 있으면 잘림 안내를 표시한다 (수용 기준: rows.length===0 잘림)", () => {
  // 수용 기준: "잘림 표시가 rows.length === 0인 경우에도 나올 것 — 안 그러면
  // '해당 조건의 보유 종목이 없습니다'를 사실로 단언하게 된다."
  const text = formatBalanceRlzPlReport({
    rows: [],
    summary: null,
    pages: 3,
    truncated: "time_budget",
    trId: "TTTC8494R",
    stockLabel: "",
  });

  assert.match(text, /\[안내\]/);
  assert.match(text, /조회가 중단되어/);
  // 수용 기준: "잘림 안내가 '- '로 시작하지 않을 것"
  // '[안내]' 줄 자체가 '- '로 시작하지 않아야 한다 (리포트의 다른 줄과 혼동 방지)
  const noteLine = text.split("\n").find((l) => l.includes("[안내]"));
  assert.ok(noteLine, "[안내] 줄이 있어야 함");
  assert.ok(!noteLine.startsWith("- "), `잘림 안내가 "- "로 시작하면 안 됨: ${noteLine}`);
});

test("formatBalanceRlzPlReport: rows가 있을 때도 잘림 안내가 마지막에 붙는다", () => {
  const text = formatBalanceRlzPlReport({
    rows: [
      {
        prdt_name: "삼성전자",
        pdno: "005930",
        trad_dvsn_name: "현금",
        hldg_qty: "1",
        prpr: "70000",
        evlu_amt: "70000",
        evlu_pfls_amt: "3000",
        evlu_pfls_rt: "4.48",
        pchs_avg_pric: "67000",
        thdt_buyqty: "0",
        thdt_sll_qty: "0",
        bfdy_cprs_icdc: "500",
        fltt_rt: "0.72",
      },
    ],
    summary: null,
    pages: 2,
    truncated: "no_cursor",
    trId: "TTTC8494R",
    stockLabel: "",
  });

  assert.match(text, /\[안내\]/);
  assert.match(text, /조회가 중단되어/);
  // 안내가 "[보유 종목]" 섹션 내부가 아닌 뒤에 와야 한다
  const holdingsSection = text.split("[보유 종목]")[1];
  assert.ok(holdingsSection, "[보유 종목] 섹션이 있어야 함");
  const stockLines = holdingsSection.split("\n").filter((l) => l.startsWith("- "));
  assert.ok(stockLines.every((l) => !l.includes("안내")), "안내 문구가 '- ' 줄에 섞이면 안 됨");
});

test("isPaperTradingKisUrl detects mock trading host", () => {
  assert.equal(isPaperTradingKisUrl("https://openapivts.koreainvestment.com:29443"), true);
  assert.equal(isPaperTradingKisUrl("https://openapi.koreainvestment.com:9443"), false);
});
