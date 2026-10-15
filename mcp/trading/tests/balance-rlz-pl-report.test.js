import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { formatBalanceRlzPlReport, PAPER_RLZ_PL_FALLBACK_NOTE } from "../balance-rlz-pl-report.js";
import { isPaperTradingKisUrl } from "../formatters.js";

const rlzPlFixture = JSON.parse(
  readFileSync(new URL("fixtures/balance_rlz_pl_report.json", import.meta.url), "utf-8"),
);

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

// 이슈 #408: 모의투자 대체 안내 문구 계약. backend(_RLZ_PL_PAPER_FALLBACK_MARKER)와
// NAT(_PAPER_RLZ_PL_FALLBACK_MARKER)가 이 문구의 marker를 매칭하고, NAT는 문구 앞의 잔고 텍스트를
// 떼어 잔고 섹션으로 쓴다. 문구를 소유한 이 스위트가 먼저 red가 되도록 같은 픽스처와 대조한다.
test("PAPER_RLZ_PL_FALLBACK_NOTE matches the shared fallback contract (fixtures/paper_rlz_pl_fallback.json)", () => {
  const contract = JSON.parse(
    readFileSync(new URL("fixtures/paper_rlz_pl_fallback.json", import.meta.url), "utf-8"),
  );
  assert.equal(PAPER_RLZ_PL_FALLBACK_NOTE, contract.note);
  assert.ok(PAPER_RLZ_PL_FALLBACK_NOTE.includes(contract.marker));
  // NAT가 잔고 텍스트와 안내를 가르는 경계다 — 잔고 텍스트 뒤 별도 문단이어야 한다.
  assert.ok(PAPER_RLZ_PL_FALLBACK_NOTE.startsWith("\n\n[안내]"));
});

test("isPaperTradingKisUrl detects mock trading host", () => {
  assert.equal(isPaperTradingKisUrl("https://openapivts.koreainvestment.com:29443"), true);
  assert.equal(isPaperTradingKisUrl("https://openapi.koreainvestment.com:9443"), false);
});

// 이슈 #196: 공유 픽스처 기반 계약 테스트. balance.test.js의 balance_report.json(#137)
// 테스트와 같은 목적·같은 모양이다.
//
// 이 루프가 없으면 계약이 **한 방향으로만** 고정된다: Python 테스트
// (backend/tests/test_scheduler.py)가 같은 파일의 expected_text를 파서에 넣어 검증하므로,
// 포맷터가 바뀌면 JS 스위트는 초록으로 남고 Python만 빨개진다 — 형식을 소유한 쪽이
// 아니라 소비하는 쪽이 깨진다. 아래 단언이 포맷터 변경을 이 파일에서 먼저 잡는다.
// 픽스처를 고칠 때는 Python 쪽 파서 계약도 함께 확인하세요.
for (const key of ["normal", "divergent_price", "no_price", "no_code", "truncated", "empty"]) {
  test(`formatBalanceRlzPlReport output matches shared fixture — ${key} (fixtures/balance_rlz_pl_report.json)`, () => {
    const { input, expected_text } = rlzPlFixture[key];
    assert.equal(formatBalanceRlzPlReport(input), expected_text);
  });
}
