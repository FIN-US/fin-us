import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import util from "node:util";
import { buildBalanceParams, fetchAllBalance, formatBalanceReport, formatPercent } from "../balance.js";
import { submitOrder } from "../index.js";
import { DuplicateOrderError, OrderDedupStore } from "../order-dedup.js";
import {
  buildCashOrderBody,
  createCashOrderRequest,
  formatOrderResult,
  selectCashOrderTrId,
  validateOrderEnvMatchesUrl,
} from "../order.js";

test("selectCashOrderTrId maps demo buy and sell to paper TR IDs", () => {
  assert.equal(selectCashOrderTrId({ orderEnv: "demo", side: "BUY" }), "VTTC0012U");
  assert.equal(selectCashOrderTrId({ orderEnv: "demo", side: "SELL" }), "VTTC0011U");
});

test("selectCashOrderTrId maps real buy and sell to production TR IDs", () => {
  assert.equal(selectCashOrderTrId({ orderEnv: "real", side: "BUY" }), "TTTC0012U");
  assert.equal(selectCashOrderTrId({ orderEnv: "real", side: "SELL" }), "TTTC0011U");
});

test("validateOrderEnvMatchesUrl fails closed on env and URL mismatch", () => {
  assert.throws(
    () => validateOrderEnvMatchesUrl({ orderEnv: "demo", kisUrl: "https://openapi.koreainvestment.com:9443" }),
    /모의투자 주문은 모의투자 KIS_URL/,
  );
  assert.throws(
    () => validateOrderEnvMatchesUrl({ orderEnv: "real", kisUrl: "https://openapivts.koreainvestment.com:29443" }),
    /실계좌 주문은 실전 KIS_URL/,
  );
});

test("createCashOrderRequest rejects real order when real-order guard is disabled", () => {
  assert.throws(
    () => createCashOrderRequest({
      accountNo: "1234567801",
      kisUrl: "https://openapi.koreainvestment.com:9443",
      orderEnv: "real",
      side: "BUY",
      stockCode: "005930",
      quantity: 1,
      price: 0,
      orderType: "MARKET",
      realOrderEnabled: false,
    }),
    /KIS_REAL_ORDER_ENABLED=true/,
  );
});

test("createCashOrderRequest allows real order when real-order guard is enabled", () => {
  assert.equal(
    createCashOrderRequest({
      accountNo: "1234567801",
      kisUrl: "https://openapi.koreainvestment.com:9443",
      orderEnv: "real",
      side: "SELL",
      stockCode: "005930",
      quantity: 1,
      price: 70000,
      orderType: "LIMIT",
      realOrderEnabled: true,
    }).trId,
    "TTTC0011U",
  );
});

test("buildBalanceParams includes required KIS inquire-balance fields", () => {
  assert.deepEqual(buildBalanceParams("1234567801"), {
    CANO: "12345678",
    ACNT_PRDT_CD: "01",
    AFHR_FLPR_YN: "N",
    OFL_YN: "",
    INQR_DVSN: "02",
    UNPR_DVSN: "01",
    FUND_STTL_ICLD_YN: "N",
    FNCG_AMT_AUTO_RDPT_YN: "N",
    PRCS_DVSN: "00",
    CTX_AREA_FK100: "",
    CTX_AREA_NK100: "",
  });
});

test("buildBalanceParams threads continuation context for pagination", () => {
  assert.deepEqual(
    buildBalanceParams("1234567801", { ctxAreaFk100: "FK1", ctxAreaNk100: "NK1" }),
    {
      CANO: "12345678",
      ACNT_PRDT_CD: "01",
      AFHR_FLPR_YN: "N",
      OFL_YN: "",
      INQR_DVSN: "02",
      UNPR_DVSN: "01",
      FUND_STTL_ICLD_YN: "N",
      FNCG_AMT_AUTO_RDPT_YN: "N",
      PRCS_DVSN: "00",
      CTX_AREA_FK100: "FK1",
      CTX_AREA_NK100: "NK1",
    },
  );
});

test("fetchAllBalance concatenates holdings across multiple continuation pages", async () => {
  const pages = [
    {
      body: {
        output1: [{ prdt_name: "삼성전자", pdno: "005930" }],
        output2: [{ tot_evlu_amt: "1000000" }],
        ctx_area_fk100: "FK1",
        ctx_area_nk100: "NK1",
      },
      trCont: "F",
    },
    {
      body: {
        output1: [{ prdt_name: "NAVER", pdno: "035420" }],
        output2: [],
        ctx_area_fk100: "",
        ctx_area_nk100: "",
      },
      trCont: "D",
    },
  ];

  const calls = [];
  let callIndex = 0;
  const fetchPage = async ({ ctxAreaFk100, ctxAreaNk100, trCont }) => {
    calls.push({ ctxAreaFk100, ctxAreaNk100, trCont });
    const page = pages[callIndex];
    callIndex += 1;
    return { body: page.body, trCont: page.trCont };
  };

  const result = await fetchAllBalance(fetchPage);

  assert.deepEqual(result.rows.map((row) => row.pdno), ["005930", "035420"]);
  assert.equal(result.pages, 2);
  assert.equal(result.truncated, null);
  // first call has no continuation context yet; second call must carry the
  // ctx_area values and tr_cont="N" returned from the first page.
  assert.deepEqual(calls[0], { ctxAreaFk100: "", ctxAreaNk100: "", trCont: "" });
  assert.deepEqual(calls[1], { ctxAreaFk100: "FK1", ctxAreaNk100: "NK1", trCont: "N" });
});

test("fetchAllBalance stops and reports truncation when the page cap is hit", async () => {
  let calls = 0;
  const fetchPage = async () => {
    calls += 1;
    return {
      body: {
        output1: [{ prdt_name: `종목${calls}`, pdno: String(calls).padStart(6, "0") }],
        output2: calls === 1 ? [{ tot_evlu_amt: "1" }] : [],
        ctx_area_fk100: `FK${calls}`,
        ctx_area_nk100: `NK${calls}`,
      },
      trCont: "F", // KIS keeps signaling "more pages available"
    };
  };

  const result = await fetchAllBalance(fetchPage, { maxPages: 3, timeBudgetMs: 60_000 });

  assert.equal(calls, 3);
  assert.equal(result.pages, 3);
  assert.equal(result.rows.length, 3);
  assert.equal(result.truncated, "max_pages");
});

test("fetchAllBalance stops and reports truncation when the time budget is exceeded", async () => {
  let calls = 0;
  let clock = 0;
  const fetchPage = async () => {
    calls += 1;
    clock += 10_000; // simulate a slow 10s KIS response per page
    return {
      body: {
        output1: [{ prdt_name: `종목${calls}`, pdno: "000000" }],
        output2: [],
        // 실제 연속조회처럼 매 페이지 커서를 돌려준다. 커서가 비면 no_cursor 가드가
        // 먼저 걸려 시간 예산 경로를 타지 못한다.
        ctx_area_fk100: `FK${calls}`,
        ctx_area_nk100: `NK${calls}`,
      },
      trCont: "F",
    };
  };

  const result = await fetchAllBalance(fetchPage, {
    maxPages: 50,
    timeBudgetMs: 15_000,
    now: () => clock,
  });

  assert.equal(calls, 2);
  assert.equal(result.pages, 2);
  assert.equal(result.truncated, "time_budget");
});

test("fetchAllBalance does not latch summary onto an empty first-page output2, and picks up the real summary from a later page", async () => {
  const pages = [
    {
      body: {
        output1: [{ prdt_name: "삼성전자", pdno: "005930" }],
        output2: [], // empty array is truthy in JS; a naive `!summary` check must not latch onto it
        ctx_area_fk100: "FK1",
        ctx_area_nk100: "NK1",
      },
      trCont: "F",
    },
    {
      body: {
        output1: [{ prdt_name: "NAVER", pdno: "035420" }],
        output2: [{ tot_evlu_amt: "1000000" }],
        ctx_area_fk100: "",
        ctx_area_nk100: "",
      },
      trCont: "D",
    },
  ];

  let callIndex = 0;
  const fetchPage = async () => {
    const page = pages[callIndex];
    callIndex += 1;
    return { body: page.body, trCont: page.trCont };
  };

  const result = await fetchAllBalance(fetchPage);

  assert.deepEqual(result.summary, { tot_evlu_amt: "1000000" });
});

test("fetchAllBalance normalizes a non-array output2 object into summary, and formatBalanceReport renders it", async () => {
  const fetchPage = async () => ({
    body: {
      output1: [],
      output2: { tot_evlu_amt: "5000000" }, // KIS sometimes sends a bare object instead of a one-element array
      ctx_area_fk100: "",
      ctx_area_nk100: "",
    },
    trCont: "D",
  });

  const result = await fetchAllBalance(fetchPage);

  assert.deepEqual(result.summary, { tot_evlu_amt: "5000000" });

  // Reproduce index.js:319-321's wrapping of the raw summary before handing it to formatBalanceReport.
  const text = formatBalanceReport(
    { output1: result.rows, output2: result.summary ? [result.summary] : [] },
    { pages: result.pages, truncated: result.truncated },
  );

  assert.match(text, /총 평가금액: 5,000,000원/);
  assert.doesNotMatch(text, /총 평가금액: -\n/);
});

test('fetchAllBalance stops after two calls and reports truncated="no_cursor" when tr_cont keeps signaling more pages but sends no cursor', async () => {
  let calls = 0;
  const fetchPage = async () => {
    calls += 1;
    if (calls === 1) {
      return {
        body: {
          output1: [{ prdt_name: "삼성전자", pdno: "005930" }],
          output2: [{ tot_evlu_amt: "1000000" }],
          ctx_area_fk100: "FK1",
          ctx_area_nk100: "NK1",
        },
        trCont: "F",
      };
    }
    // Pathological case: KIS keeps signaling "more pages available" (tr_cont="F") but never
    // sends a cursor to resume from. Blindly retrying would re-request the same page forever.
    return {
      body: {
        output1: [{ prdt_name: `종목${calls}`, pdno: String(calls).padStart(6, "0") }],
        output2: [],
        ctx_area_fk100: "",
        ctx_area_nk100: "",
      },
      trCont: "F",
    };
  };

  const result = await fetchAllBalance(fetchPage, { maxPages: 20, timeBudgetMs: 60_000 });

  assert.equal(calls, 2);
  assert.equal(result.pages, 2);
  assert.equal(result.truncated, "no_cursor");
});

test('fetchAllBalance stops after two calls and reports truncated="no_cursor" when KIS pads the continuation cursor with spaces instead of leaving it empty', async () => {
  const calls = [];
  let callCount = 0;
  const fetchPage = async ({ ctxAreaFk100, ctxAreaNk100, trCont }) => {
    calls.push({ ctxAreaFk100, ctxAreaNk100, trCont });
    callCount += 1;
    if (callCount === 1) {
      return {
        body: {
          output1: [{ prdt_name: "삼성전자", pdno: "005930" }],
          output2: [{ tot_evlu_amt: "1000000" }],
          // fixed-width KIS response: padded rather than actually empty
          ctx_area_fk100: " FK1 ",
          ctx_area_nk100: "NK1",
        },
        trCont: "F",
      };
    }
    // Pathological case: the cursor KIS hands back is space-padded, not empty.
    // A naive `!ctxNk` check treats padded whitespace as truthy and keeps looping.
    return {
      body: {
        output1: [{ prdt_name: `종목${callCount}`, pdno: String(callCount).padStart(6, "0") }],
        output2: [],
        ctx_area_fk100: "",
        ctx_area_nk100: "   ",
      },
      trCont: "F",
    };
  };

  const result = await fetchAllBalance(fetchPage, { maxPages: 20, timeBudgetMs: 60_000 });

  assert.equal(callCount, 2);
  assert.equal(result.pages, 2);
  assert.equal(result.truncated, "no_cursor");
  // the outgoing cursor to the second call must be trimmed, not just checked by the guard
  assert.deepEqual(calls[1], { ctxAreaFk100: "FK1", ctxAreaNk100: "NK1", trCont: "N" });
});

test('fetchAllBalance stops after two calls and reports truncated="repeated_cursor" when KIS echoes back the same continuation cursor instead of advancing, keeping both pages\' rows', async () => {
  let calls = 0;
  const fetchPage = async () => {
    calls += 1;
    return {
      body: {
        // Both calls echo back the same cursor ("NK1"), so the third call would repeat
        // page 2. Page 2 itself was fetched with a cursor never sent before, so it is a
        // genuinely new page — its distinct holding must survive the truncation.
        output1: [{ prdt_name: `종목${calls}`, pdno: calls === 1 ? "000000" : "000001" }],
        output2: calls === 1 ? [{ tot_evlu_amt: "1" }] : [],
        ctx_area_fk100: "FK1",
        ctx_area_nk100: "NK1",
      },
      trCont: "F",
    };
  };

  const result = await fetchAllBalance(fetchPage, { maxPages: 20, timeBudgetMs: 60_000 });

  assert.equal(calls, 2);
  assert.equal(result.pages, 2);
  assert.equal(result.truncated, "repeated_cursor");
  assert.deepEqual(result.rows.map((row) => row.pdno), ["000000", "000001"]);
});

test('fetchAllBalance detects a period-2 repeated-cursor cycle (A,B,A) that a previous-value-only comparison would miss', async () => {
  const cursors = ["A", "B", "A"];
  let calls = 0;
  const fetchPage = async () => {
    calls += 1;
    const cursor = cursors[calls - 1];
    return {
      body: {
        output1: [{ prdt_name: `종목${calls}`, pdno: String(calls).padStart(6, "0") }],
        output2: calls === 1 ? [{ tot_evlu_amt: "1" }] : [],
        ctx_area_fk100: `FK-${cursor}`,
        ctx_area_nk100: cursor,
      },
      trCont: "F",
    };
  };

  const result = await fetchAllBalance(fetchPage, { maxPages: 20, timeBudgetMs: 60_000 });

  assert.equal(calls, 3);
  assert.equal(result.truncated, "repeated_cursor");
  assert.deepEqual(result.rows.map((row) => row.pdno), ["000001", "000002", "000003"]);
});

test("fetchAllBalance keeps paging when ctx_area_nk100 repeats but ctx_area_fk100 differs, since the outgoing cursor is the FK/NK pair", async () => {
  // 다음 요청에 실리는 커서는 CTX_AREA_FK100/CTX_AREA_NK100 쌍이다(buildBalanceParams).
  // NK만 같고 FK가 다르면 서로 다른 요청이므로 반복이 아니다. 반복 판정을 NK 하나로만
  // 하면 여기서 조기 중단해, 이 함수가 없애려는 조용한 누락을 스스로 만든다.
  const pages = [
    { fk: "FK-1", nk: "NK" },
    { fk: "FK-2", nk: "NK" },
    { fk: "FK-3", nk: "NK" },
  ];
  let calls = 0;
  const fetchPage = async () => {
    calls += 1;
    const page = pages[calls - 1];
    return {
      body: {
        output1: [{ prdt_name: `종목${calls}`, pdno: String(calls).padStart(6, "0") }],
        output2: calls === 1 ? [{ tot_evlu_amt: "1" }] : [],
        ctx_area_fk100: page.fk,
        ctx_area_nk100: page.nk,
      },
      // 4번째 호출은 fixture가 없으므로, 여기까지 왔다면 상한이 아니라 페이지 소진으로 끝난다.
      trCont: calls < pages.length ? "F" : "D",
    };
  };

  const result = await fetchAllBalance(fetchPage, { maxPages: 20, timeBudgetMs: 60_000 });

  assert.equal(calls, 3);
  assert.equal(result.truncated, null);
  assert.deepEqual(result.rows.map((row) => row.pdno), ["000001", "000002", "000003"]);
});

test("fetchAllBalance rethrows when the first page fetch fails, so auth/account errors are not swallowed", async () => {
  const authError = new Error("EGW00123 인증 오류");
  const fetchPage = async () => {
    throw authError;
  };

  await assert.rejects(() => fetchAllBalance(fetchPage), (err) => err === authError);
});

test('fetchAllBalance preserves already-fetched pages and reports truncated="error" when a later page fetch fails', async () => {
  let calls = 0;
  const fetchPage = async () => {
    calls += 1;
    if (calls === 1) {
      return {
        body: {
          output1: [{ prdt_name: "삼성전자", pdno: "005930" }],
          output2: [{ tot_evlu_amt: "1000000" }],
          ctx_area_fk100: "FK1",
          ctx_area_nk100: "NK1",
        },
        trCont: "F",
      };
    }
    throw new Error("일시적 네트워크 오류");
  };

  const result = await fetchAllBalance(fetchPage);

  assert.equal(calls, 2);
  assert.deepEqual(result.rows.map((row) => row.pdno), ["005930"]);
  assert.equal(result.pages, 1);
  assert.equal(result.truncated, "error");
});

test("fetchAllBalance logs only the error message and failing page number to stderr on a swallowed continuation error, never the account number, app key, secret, or bearer token", async (t) => {
  const errorMock = t.mock.method(console, "error", () => {});

  const secretMessage = "초당 거래건수를 초과하였습니다.";
  const secretError = new Error(secretMessage);
  // Mirror an axios error: config.params carries CANO, config.headers carries appkey/
  // appsecret/Authorization. Logging the error object (rather than error.message) would
  // leak all of these to stderr.
  secretError.config = {
    params: { CANO: "87654321" },
    headers: {
      appkey: "test-app-key-secret",
      appsecret: "test-app-secret-value",
      authorization: "Bearer test-bearer-token-value",
    },
  };

  let calls = 0;
  const fetchPage = async () => {
    calls += 1;
    if (calls === 1) {
      return {
        body: {
          output1: [{ prdt_name: "삼성전자", pdno: "005930" }],
          output2: [{ tot_evlu_amt: "1000000" }],
          ctx_area_fk100: "FK1",
          ctx_area_nk100: "NK1",
        },
        trCont: "F",
      };
    }
    throw secretError;
  };

  await fetchAllBalance(fetchPage);

  const logged = errorMock.mock.calls.map((call) => util.format(...call.arguments)).join("\n");

  assert.match(logged, /초당 거래건수를 초과하였습니다\./);
  assert.match(logged, /2페이지/); // the failing page number (pages=1 completed, so page 2 failed)
  assert.doesNotMatch(logged, /87654321/);
  assert.doesNotMatch(logged, /test-app-key-secret/);
  assert.doesNotMatch(logged, /test-app-secret-value/);
  assert.doesNotMatch(logged, /Bearer/);
  assert.doesNotMatch(logged, /test-bearer-token-value/);
});

test("formatBalanceReport omits the truncation note when the fetch was not truncated", () => {
  const text = formatBalanceReport(
    { output1: [], output2: [] },
    { pages: 1, truncated: null },
  );
  assert.doesNotMatch(text, /\[안내\]/);
});

test("formatBalanceReport's truncation note stays outside the holdings section so scheduler parsing is not polluted", () => {
  const text = formatBalanceReport(
    {
      output1: [
        {
          prdt_name: "삼성전자",
          pdno: "005930",
          hldg_qty: "3",
          pchs_avg_pric: "67000",
          evlu_amt: "210000",
          evlu_pfls_amt: "9000",
          evlu_pfls_rt: "4.48",
        },
      ],
      output2: [{ tot_evlu_amt: "210000" }],
    },
    { pages: 20, truncated: "max_pages" },
  );

  assert.match(text, /\[안내\] 페이지 상한\(20회\)에 도달하여/);

  // Mirror backend/scheduler.py's extract_stocks_from_balance(): everything after
  // "[보유 종목 리스트]", split into lines, keep only lines starting with "- ".
  const section = text.split("[보유 종목 리스트]")[1].trim();
  const stockLines = section.split("\n").filter((line) => line.startsWith("- "));

  assert.deepEqual(stockLines, ["- 삼성전자 (005930) · 3주"]);
  assert.ok(stockLines.every((line) => !line.includes("안내") && !line.includes("상한")));
});

test("formatBalanceReport's no_cursor, error, and repeated_cursor truncation notes also stay outside the holdings section", () => {
  for (const truncated of ["no_cursor", "error", "repeated_cursor"]) {
    const text = formatBalanceReport(
      {
        output1: [
          {
            prdt_name: "삼성전자",
            pdno: "005930",
            hldg_qty: "3",
            pchs_avg_pric: "67000",
            evlu_amt: "210000",
            evlu_pfls_amt: "9000",
            evlu_pfls_rt: "4.48",
          },
        ],
        output2: [{ tot_evlu_amt: "210000" }],
      },
      { pages: 2, truncated },
    );

    assert.match(text, /\[안내\]/);

    // Mirror backend/scheduler.py's extract_stocks_from_balance(): everything after
    // "[보유 종목 리스트]", split into lines, keep only lines starting with "- ".
    const section = text.split("[보유 종목 리스트]")[1].trim();
    const stockLines = section.split("\n").filter((line) => line.startsWith("- "));

    assert.deepEqual(stockLines, ["- 삼성전자 (005930) · 3주"]);
    assert.ok(stockLines.every((line) => !line.includes("안내")));
  }
});

test("formatPercent avoids undefined balance rates", () => {
  assert.equal(formatPercent(undefined), "-");
  assert.equal(formatPercent("1.23"), "🔴 ▲ +1.23%");
  assert.equal(formatPercent("-1.23"), "🔵 ▼ -1.23%");
  assert.equal(formatPercent("0.00"), "⚪ 0.00%");
  assert.equal(formatPercent("0.02079000"), "🔴 ▲ +0.02%");
  assert.equal(formatPercent("-0.251"), "🔵 ▼ -0.26%");
});

test("formatBalanceReport displays unsettled cash fields and account return rate", () => {
  assert.equal(
    formatBalanceReport({
      output1: [
        {
          prdt_name: "삼성전자",
          pdno: "005930",
          hldg_qty: "3",
          pchs_avg_pric: "67000",
          evlu_amt: "210000",
          evlu_pfls_amt: "9000",
          evlu_pfls_rt: "4.48",
        },
        {
          prdt_name: "NAVER",
          pdno: "035420",
          hldg_qty: "1",
          pchs_avg_pric: "201000",
          evlu_amt: "200500",
          evlu_pfls_amt: "-500",
          evlu_pfls_rt: "-0.25",
        },
      ],
      output2: [
        {
          tot_evlu_amt: "1210000",
          nass_amt: "1210000",
          evlu_pfls_smtl_amt: "9000",
          asst_icdc_erng_rt: "0.75",
          dnca_tot_amt: "1000000",
          nxdy_excc_amt: "790000",
          prvs_rcdl_excc_amt: "1009000",
          thdt_buy_amt: "210000",
          thdt_sll_amt: "0",
        },
      ],
    }),
    `[계좌 잔고 현황]
- 총 평가금액: 1,210,000원
- 순자산금액: 1,210,000원
- 총 손익: 9,000원 (수익률: 🔴 ▲ +2.23%)
- 거래가능금액: 1,000,000원
- 정산중 금액(가수도): 1,009,000원
- 익일 정산예정금액: 790,000원
- 금일 매수/매도: 210,000원 / 0원

[보유 종목 리스트]
- 삼성전자 (005930) · 3주
  평단가 67,000원 → 평가금액 210,000원
  손익 +9,000원 · 수익률 🔴 ▲ +4.48%

- NAVER (035420) · 1주
  평단가 201,000원 → 평가금액 200,500원
  손익 -500원 · 수익률 🔵 ▼ -0.25%`,
  );
});

test("formatBalanceReport uses profit-loss return rate before asset change rate", () => {
  const text = formatBalanceReport({
    output1: [
      {
        prdt_name: "SK하이닉스",
        pdno: "000660",
        hldg_qty: "1",
        pchs_avg_pric: "2064000",
        evlu_amt: "2268000",
        evlu_pfls_amt: "204000",
        evlu_pfls_rt: "9.88",
      },
    ],
    output2: [
      {
        tot_evlu_amt: "11025741",
        nass_amt: "11025741",
        evlu_pfls_smtl_amt: "204000",
        evlu_pfls_rt: "1.88",
        asst_icdc_erng_rt: "-1.15",
        dnca_tot_amt: "5546116",
        prvs_rcdl_excc_amt: "8757741",
        nxdy_excc_amt: "8064220",
        thdt_buy_amt: "0",
        thdt_sll_amt: "695000",
      },
    ],
  });

  assert.match(text, /총 손익: 204,000원 \(수익률: 🔴 ▲ \+9.88%\)/);
});

test("formatBalanceReport calculates account return rate when API rates conflict with profit", () => {
  const text = formatBalanceReport({
    output1: [
      {
        prdt_name: "SK하이닉스",
        pdno: "000660",
        hldg_qty: "1",
        pchs_avg_pric: "2064000",
        evlu_amt: "2300000",
        evlu_pfls_amt: "236000",
        evlu_pfls_rt: "11.43",
      },
      {
        prdt_name: "삼성전자",
        pdno: "005930",
        hldg_qty: "2",
        pchs_avg_pric: "353875",
        evlu_amt: "713000",
        evlu_pfls_amt: "5250",
        evlu_pfls_rt: "0.74",
      },
    ],
    output2: [
      {
        tot_evlu_amt: "11062891",
        nass_amt: "11062891",
        pchs_amt_smtl_amt: "2771750",
        evlu_pfls_smtl_amt: "241250",
        evlu_pfls_rt: "-0.81",
        asst_icdc_erng_rt: "-0.81",
        dnca_tot_amt: "5546116",
        prvs_rcdl_excc_amt: "8049891",
        nxdy_excc_amt: "8064220",
        thdt_buy_amt: "707750",
        thdt_sll_amt: "695000",
      },
    ],
  });

  assert.match(text, /총 손익: 241,250원 \(수익률: 🔴 ▲ \+8.70%\)/);
});

test("formatBalanceReport highlights negative return rates", () => {
  assert.equal(
    formatBalanceReport({
      output1: [
        {
          prdt_name: "삼성전자",
          pdno: "005930",
          hldg_qty: "3",
          pchs_avg_pric: "66666.67",
          evlu_amt: "190000",
          evlu_pfls_amt: "-10000",
          evlu_pfls_rt: "-5.00",
        },
      ],
      output2: [
        {
          tot_evlu_amt: "1190000",
          nass_amt: "1190000",
          evlu_pfls_smtl_amt: "-10000",
          asst_icdc_erng_rt: "-0.84",
          dnca_tot_amt: "1000000",
        },
      ],
    }),
    `[계좌 잔고 현황]
- 총 평가금액: 1,190,000원
- 순자산금액: 1,190,000원
- 총 손익: -10,000원 (수익률: 🔵 ▼ -5.00%)
- 거래가능금액: 1,000,000원
- 정산중 금액(가수도): -
- 익일 정산예정금액: -
- 금일 매수/매도: - / -

[보유 종목 리스트]
- 삼성전자 (005930) · 3주
  평단가 66,666.67원 → 평가금액 190,000원
  손익 -10,000원 · 수익률 🔵 ▼ -5.00%`,
  );
});

test("buildCashOrderBody creates uppercase KIS order body", () => {
  assert.deepEqual(
    buildCashOrderBody({
      accountNo: "1234567801",
      stockCode: "005930",
      quantity: 2,
      price: 70000,
      orderType: "LIMIT",
    }),
    {
      CANO: "12345678",
      ACNT_PRDT_CD: "01",
      PDNO: "005930",
      ORD_DVSN: "00",
      ORD_QTY: "2",
      ORD_UNPR: "70000",
      EXCG_ID_DVSN_CD: "SOR",
      SLL_TYPE: "",
      CNDT_PRIC: "",
    },
  );
});

test("buildCashOrderBody creates market KIS order body", () => {
  assert.deepEqual(
    buildCashOrderBody({
      accountNo: "1234567801",
      stockCode: "005930",
      quantity: 2,
      price: 0,
      orderType: "MARKET",
    }),
    {
      CANO: "12345678",
      ACNT_PRDT_CD: "01",
      PDNO: "005930",
      ORD_DVSN: "01",
      ORD_QTY: "2",
      ORD_UNPR: "0",
      EXCG_ID_DVSN_CD: "SOR",
      SLL_TYPE: "",
      CNDT_PRIC: "",
    },
  );
});

test("buildCashOrderBody rejects alphanumeric stock codes as unsupported order targets", () => {
  assert.throws(
    () => buildCashOrderBody({
      accountNo: "1234567801",
      stockCode: "0001A0",
      quantity: 1,
      price: 10000,
      orderType: "LIMIT",
    }),
    /이 종목은 현재 주문을 지원하지 않습니다/,
  );
});

test("formatOrderResult includes order number when present", () => {
  assert.equal(
    formatOrderResult({
      stockName: "삼성전자",
      stockCode: "005930",
      side: "BUY",
      quantity: 1,
      price: 70000,
      orderType: "LIMIT",
      data: { output: { ODNO: "12345", ORD_TMD: "101010" }, msg1: "주문이 완료되었습니다" },
    }),
    "[삼성전자] BUY 주문 접수\n- 종목코드: 005930\n- 수량/가격: 1주 / 70,000원\n- 주문번호: 12345\n- 주문시간: 101010\n- 메시지: 주문이 완료되었습니다",
  );
});

test("createCashOrderRequest builds KIS endpoint, TR ID, and body", () => {
  assert.deepEqual(
    createCashOrderRequest({
      accountNo: "1234567801",
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      orderEnv: "demo",
      side: "SELL",
      stockCode: "005930",
      quantity: 3,
      price: 68000,
      orderType: "LIMIT",
    }),
    {
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0011U",
      body: {
        CANO: "12345678",
        ACNT_PRDT_CD: "01",
        PDNO: "005930",
        ORD_DVSN: "00",
        ORD_QTY: "3",
        ORD_UNPR: "68000",
        EXCG_ID_DVSN_CD: "SOR",
        SLL_TYPE: "",
        CNDT_PRIC: "",
      },
    },
  );
});

test("createCashOrderRequest builds market order body", () => {
  assert.deepEqual(
    createCashOrderRequest({
      accountNo: "1234567801",
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      orderEnv: "demo",
      side: "BUY",
      stockCode: "005930",
      quantity: 3,
      price: 0,
      orderType: "MARKET",
    }),
    {
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0012U",
      body: {
        CANO: "12345678",
        ACNT_PRDT_CD: "01",
        PDNO: "005930",
        ORD_DVSN: "01",
        ORD_QTY: "3",
        ORD_UNPR: "0",
        EXCG_ID_DVSN_CD: "SOR",
        SLL_TYPE: "",
        CNDT_PRIC: "",
      },
    },
  );
});

// #161: markSucceeded (ledger bookkeeping) sitting inside the same try as the KIS call
// used to make a filesystem write failure look identical to an order failure, deleting
// the duplicate guard for an order KIS had already accepted. submitOrder() (index.js)
// separates "did KIS accept the order" from "did we finish recording it" so a ledger
// write failure can never undo an accepted order.

function tempDedupLedgerPath(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "finus-submit-order-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 }));
  return path.join(dir, "ledger.json");
}

// Mutation this catches: moving markSucceeded back inside the try (or letting any
// exception from it flow into the shared catch) so that a ledger write failure is
// treated the same as an order failure — release() gets called and/or the success
// result never reaches the caller. This exact scenario is what #161 reported: KIS
// accepted the order, but a later fs error made it look like the order itself failed.
// Confirmed to fail on unmodified origin/main: there, markSucceeded runs inside the
// try, its exception has neither kisOrderSubmittedMaybe nor kisOrderRejected, so
// `!undefined || undefined` is true and release() deletes the guard.
test("submitOrder: a markSucceeded (ledger write) failure after a successful KIS order does not release the dedup guard, and the order result still reaches the caller", async (t) => {
  const store = new OrderDedupStore({ filePath: tempDedupLedgerPath(t), ttlMs: 60_000, now: () => 1_000 });
  const dedupKey = "order-key-mark-succeeded-throws";
  store.reserve(dedupKey, { pathname: "/uapi/domestic-stock/v1/trading/order-cash", trId: "TTTC0012U", body: {} });

  const releaseSpy = t.mock.method(store, "release");
  t.mock.method(store, "markSucceeded", () => {
    throw new Error("ENOSPC: no space left on device, write");
  });
  t.mock.method(console, "error", () => {});

  const kisResponse = { rt_cd: "0", output: { ODNO: "0000001234" } };
  const data = await submitOrder({
    orderDedupStore: store,
    dedupKey,
    submit: async () => kisResponse,
  });

  assert.equal(data, kisResponse, "the KIS success response must still reach the caller");
  assert.equal(releaseSpy.mock.callCount(), 0, "release() must not run when only bookkeeping failed");
});

// Mutation this catches: any change that deletes or expires the ledger entry when
// markSucceeded fails (e.g. calling release() unconditionally in a finally block, or
// swallowing the error by writing an empty/succeeded-but-then-cleared entry). If the
// entry disappeared, this reserve() would succeed instead of throwing, and a retried
// order would go through — reproducing the duplicate-order bug from #161.
test("submitOrder: after a markSucceeded failure, the ledger entry stays in place (in_flight) so a retried reserve() is still blocked as a duplicate", async (t) => {
  const store = new OrderDedupStore({ filePath: tempDedupLedgerPath(t), ttlMs: 60_000, now: () => 1_000 });
  const dedupKey = "order-key-retry-blocked";
  const request = { pathname: "/uapi/domestic-stock/v1/trading/order-cash", trId: "TTTC0012U", body: {} };
  store.reserve(dedupKey, request);

  t.mock.method(store, "markSucceeded", () => {
    throw new Error("EACCES: permission denied, open '/state/order-dedup.json'");
  });
  t.mock.method(console, "error", () => {});

  await submitOrder({
    orderDedupStore: store,
    dedupKey,
    submit: async () => ({ rt_cd: "0", output: { ODNO: "0000005678" } }),
  });

  assert.throws(
    () => store.reserve(dedupKey, request),
    DuplicateOrderError,
    "the reservation must still be present, blocking a retry of the same order",
  );
});

// Mutation this catches: dropping the `error.kisOrderRejected === true` check (e.g.
// releasing unconditionally, or never releasing at all). This is unchanged behavior
// from before #161: KIS explicitly said rt_cd !== "0", so nothing was submitted and
// the guard must be released for a legitimate retry.
test("submitOrder: releases the dedup guard when KIS rejects the order (rt_cd !== \"0\") — unchanged behavior", async (t) => {
  const releaseCalls = [];
  const store = {
    release(key) {
      releaseCalls.push(key);
    },
    markSucceeded() {
      throw new Error("markSucceeded should not run when submit() throws");
    },
  };

  const rejected = new Error("KIS API 오류: 주문가능금액을 초과하였습니다.");
  rejected.kisOrderRejected = true;

  await assert.rejects(
    () => submitOrder({ orderDedupStore: store, dedupKey: "order-key-rejected", submit: async () => { throw rejected; } }),
    (err) => err === rejected,
  );
  assert.deepEqual(releaseCalls, ["order-key-rejected"]);
});

// Mutation this catches: releasing whenever `kisOrderSubmittedMaybe` is absent, which
// is exactly the old `!error.kisOrderSubmittedMaybe` fail-open logic this issue removes.
// Unchanged behavior from before #161: the axios POST itself failed, so whether KIS
// received the order is unknown — releasing here could let a duplicate through while
// the original order might still be live, so the guard must stay (fail-closed).
test("submitOrder: does not release the dedup guard when the axios POST fails and submission status is unknown — unchanged behavior", async (t) => {
  const releaseCalls = [];
  const store = {
    release(key) {
      releaseCalls.push(key);
    },
  };

  const submitFailure = new Error("connect ETIMEDOUT");
  submitFailure.kisOrderSubmittedMaybe = true;

  await assert.rejects(
    () => submitOrder({ orderDedupStore: store, dedupKey: "order-key-submit-unknown", submit: async () => { throw submitFailure; } }),
    (err) => err === submitFailure,
  );
  assert.deepEqual(releaseCalls, [], "an unknown submission outcome must not release the guard");
});

// Mutation this catches: reverting the allowlist inversion. Old code released whenever
// `kisOrderSubmittedMaybe` was falsy, so ANY exception without that flag (including one
// with neither flag, e.g. a getAccessToken()/hashkey failure before the KIS POST even
// ran) fell through to release(). This is the deliberate behavior flip from #161: an
// unrecognized error must now default to NOT releasing.
test("submitOrder: does not release the dedup guard for an error with neither kisOrderRejected nor kisOrderSubmittedMaybe (inversion of pre-#161 behavior, which released here)", async (t) => {
  const releaseCalls = [];
  const store = {
    release(key) {
      releaseCalls.push(key);
    },
  };

  const unrecognizedError = new Error("Access Token 발급 실패: EGW00133");

  await assert.rejects(
    () => submitOrder({ orderDedupStore: store, dedupKey: "order-key-unrecognized", submit: async () => { throw unrecognizedError; } }),
    (err) => err === unrecognizedError,
  );
  assert.deepEqual(releaseCalls, [], "an unrecognized error must default to fail-closed (no release)");
});

// Mutation this catches: letting release()'s own exception propagate out of the catch
// block unguarded, which would replace the original KIS rejection error and destroy
// the diagnostic (per issue's "also look at" section on release()).
test("submitOrder: a release() failure during a KIS rejection does not replace the original KIS error", async (t) => {
  const consoleError = t.mock.method(console, "error", () => {});
  const store = {
    release() {
      throw new Error("EIO: i/o error, write");
    },
  };

  const rejected = new Error("KIS API 오류: 모의투자 장운영시간이 아닙니다.");
  rejected.kisOrderRejected = true;

  await assert.rejects(
    () => submitOrder({ orderDedupStore: store, dedupKey: "order-key-release-fails", submit: async () => { throw rejected; } }),
    (err) => err === rejected,
    "the original KIS rejection must still be the thrown error, not release()'s fs error",
  );
  const logged = consoleError.mock.calls.map((call) => util.format(...call.arguments)).join("\n");
  assert.match(logged, /EIO: i\/o error, write/);
});
