import assert from "node:assert/strict";
import test from "node:test";
import {
  buildOrderableCashParams,
  formatOrderableCashReport,
} from "../orderable-cash.js";

const STOCK = { name: "삼성전자", code: "005930", market: "KOSPI" };

const OUTPUT = {
  ord_psbl_cash: "5000000",
  ord_psbl_sbst: "0",
  ruse_psbl_amt: "0",
  nrcvb_buy_amt: "5000000",
  nrcvb_buy_qty: "67",
  max_buy_amt: "5000000",
  max_buy_qty: "67",
};

test("buildOrderableCashParams splits the account number and pins the exclusion flags", () => {
  assert.deepEqual(
    buildOrderableCashParams("1234567801", { stockCode: "005930" }),
    {
      CANO: "12345678",
      ACNT_PRDT_CD: "01",
      PDNO: "005930",
      ORD_UNPR: "0",
      ORD_DVSN: "01",
      CMA_EVLU_AMT_ICLD_YN: "N",
      OVRS_ICLD_YN: "N",
    },
  );
});

test("buildOrderableCashParams sends the limit price only for LIMIT", () => {
  const limit = buildOrderableCashParams("1234567801", {
    stockCode: "005930",
    orderType: "LIMIT",
    price: 74_500,
  });
  assert.equal(limit.ORD_DVSN, "00");
  assert.equal(limit.ORD_UNPR, "74500");

  // 시장가 기준이면 KIS가 ORD_UNPR을 무시한다. 값을 흘려보내면 리포트의 "기준 주문유형"과
  // 실제 요청이 어긋난 것처럼 읽히므로 0으로 눕힌다.
  const market = buildOrderableCashParams("1234567801", {
    stockCode: "005930",
    orderType: "MARKET",
    price: 74_500,
  });
  assert.equal(market.ORD_DVSN, "01");
  assert.equal(market.ORD_UNPR, "0");
});

test("CMA·해외 포함 플래그는 항상 N이다 — 확인하지 못한 여유를 한도에 얹지 않는다", () => {
  // Y로 바뀌면 주문가능액이 커지고 backend/order_assist.py의 insufficient_cash·
  // cash_floor 두 하드 한도가 그만큼 헐거워진다. 값이 고정이라는 것 자체가 계약이다.
  for (const orderType of ["LIMIT", "MARKET"]) {
    const params = buildOrderableCashParams("1234567801", {
      stockCode: "005930",
      orderType,
      price: 1000,
    });
    assert.equal(params.CMA_EVLU_AMT_ICLD_YN, "N");
    assert.equal(params.OVRS_ICLD_YN, "N");
  }
});

test("formatOrderableCashReport renders the market-basis report", () => {
  assert.equal(
    formatOrderableCashReport({
      output: OUTPUT,
      stock: STOCK,
      trId: "TTTC8908R",
      orderType: "MARKET",
      price: 0,
    }),
    `[주문가능조회] 삼성전자 (005930)
- 조회 TR: TTTC8908R (매수가능조회 v1_국내주식-007, inquire-psbl-order)
- 기준 주문유형: 시장가
- 주문가능금액: 5,000,000원
- 미수없는매수금액: 5,000,000원 | 미수없는매수수량: 67주
- 최대매수금액: 5,000,000원 | 최대매수수량: 67주
- 주문가능대용: 0원 | 재사용가능금액: 0원
- 기준 데이터: 한국투자증권 Open API inquire-psbl-order (CMA·해외 미포함)`,
  );
});

test("formatOrderableCashReport shows the limit price as the basis", () => {
  const text = formatOrderableCashReport({
    output: OUTPUT,
    stock: STOCK,
    trId: "VTTC8908R",
    orderType: "LIMIT",
    price: 74_500,
  });

  assert.match(text, /- 기준 주문유형: 지정가 74,500원/);
  assert.match(text, /VTTC8908R/);
});

// backend/order_assist.py `_CASH_RE`가 읽는 줄이다. 이 정규식이 그쪽 파일의 복제본이라는
// 점이 이 테스트의 전부다 — 라벨을 바꾸면 /advise가 전부 fail-closed 거부로 떨어진다 (#310).
const BACKEND_CASH_RE = /주문가능금액:\s*([\d,]+)\s*원/;

test("주문가능금액 줄은 backend의 _CASH_RE 계약을 만족한다", () => {
  const text = formatOrderableCashReport({
    output: OUTPUT,
    stock: STOCK,
    trId: "TTTC8908R",
    orderType: "MARKET",
    price: 0,
  });

  const match = BACKEND_CASH_RE.exec(text);
  assert.ok(match, "backend _CASH_RE should match the report");
  assert.equal(Number(match[1].replaceAll(",", "")), 5_000_000);
});

test("_CASH_RE가 먼저 만나는 것은 ord_psbl_cash 줄뿐이다", () => {
  // re.search는 문서 전체에서 첫 매치를 집는다. 다른 필드 라벨에 "주문가능금액"이
  // 섞이면 한도 판정이 엉뚱한 값 위에 서게 된다 — 라벨 충돌이 없다는 것을 고정한다.
  const text = formatOrderableCashReport({
    output: { ...OUTPUT, ord_psbl_sbst: "9999999", ruse_psbl_amt: "8888888" },
    stock: STOCK,
    trId: "TTTC8908R",
    orderType: "MARKET",
    price: 0,
  });

  const occurrences = text.split("주문가능금액").length - 1;
  assert.equal(occurrences, 1);
  assert.equal(BACKEND_CASH_RE.exec(text)[1], "5,000,000");
});

test("읽지 못한 주문가능현금은 0이 아니라 '-'로 나가 backend에서 fail-closed로 끊긴다", () => {
  const text = formatOrderableCashReport({
    output: { ...OUTPUT, ord_psbl_cash: "" },
    stock: STOCK,
    trId: "TTTC8908R",
    orderType: "MARKET",
    price: 0,
  });

  assert.match(text, /- 주문가능금액: -/);
  assert.equal(BACKEND_CASH_RE.test(text), false);
});

test("output이 없어도 던지지 않고 전부 '-'로 채운다", () => {
  const text = formatOrderableCashReport({
    output: undefined,
    stock: STOCK,
    trId: "TTTC8908R",
    orderType: "MARKET",
    price: 0,
  });

  assert.match(text, /- 주문가능금액: -/);
  assert.equal(BACKEND_CASH_RE.test(text), false);
});
