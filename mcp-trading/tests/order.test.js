import assert from "node:assert/strict";
import test from "node:test";
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

test("buildCashOrderBody rejects nine-char fund codes as unsupported order targets", () => {
  assert.throws(
    () => buildCashOrderBody({
      accountNo: "1234567801",
      stockCode: "F70100026",
      quantity: 1,
      price: 10000,
      orderType: "LIMIT",
    }),
    /stock_code는 6자리 또는 7자리 종목코드여야 합니다/,
  );
});

test(
  "buildCashOrderBody allows alphanumeric and nine-char codes when KIS_ALNUM_STOCK_ORDER_ENABLED=true (#138)",
  (t) => {
    const originalEnvValue = process.env.KIS_ALNUM_STOCK_ORDER_ENABLED;
    t.after(() => {
      if (originalEnvValue === undefined) {
        delete process.env.KIS_ALNUM_STOCK_ORDER_ENABLED;
      } else {
        process.env.KIS_ALNUM_STOCK_ORDER_ENABLED = originalEnvValue;
      }
    });
    process.env.KIS_ALNUM_STOCK_ORDER_ENABLED = "true";

    assert.equal(
      buildCashOrderBody({
        accountNo: "1234567801",
        stockCode: "0001A0",
        quantity: 1,
        price: 10000,
        orderType: "LIMIT",
      }).PDNO,
      "0001A0",
    );
    assert.equal(
      buildCashOrderBody({
        accountNo: "1234567801",
        stockCode: "F70100026",
        quantity: 1,
        price: 10000,
        orderType: "LIMIT",
      }).PDNO,
      "F70100026",
    );
    // 8자는 종목마스터에 0건이라 플래그를 켜도 계속 거절한다(#140과 같은 근거).
    assert.throws(
      () => buildCashOrderBody({
        accountNo: "1234567801",
        stockCode: "12345678",
        quantity: 1,
        price: 10000,
        orderType: "LIMIT",
      }),
      /stock_code는 6~7자 영숫자 또는 9자 코드여야 합니다/,
    );
  },
);

test("buildCashOrderBody keeps rejecting alphanumeric codes when the flag is not exactly \"true\"", (t) => {
  const originalEnvValue = process.env.KIS_ALNUM_STOCK_ORDER_ENABLED;
  t.after(() => {
    if (originalEnvValue === undefined) {
      delete process.env.KIS_ALNUM_STOCK_ORDER_ENABLED;
    } else {
      process.env.KIS_ALNUM_STOCK_ORDER_ENABLED = originalEnvValue;
    }
  });
  // backend/stock_code.py의 _alnum_stock_order_enabled()와 비교 방식을 맞춘다 —
  // 대소문자·공백을 관용하면 두 계층의 판정이 갈릴 수 있다.
  process.env.KIS_ALNUM_STOCK_ORDER_ENABLED = "True";

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
