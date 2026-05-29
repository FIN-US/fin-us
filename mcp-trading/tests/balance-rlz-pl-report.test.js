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

test("isPaperTradingKisUrl detects mock trading host", () => {
  assert.equal(isPaperTradingKisUrl("https://openapivts.koreainvestment.com:29443"), true);
  assert.equal(isPaperTradingKisUrl("https://openapi.koreainvestment.com:9443"), false);
});
