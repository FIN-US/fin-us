import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
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

// ---------------------------------------------------------------------------
// 주문 가능 코드 정책 — 공유 판정표 기반 (#138)
// ---------------------------------------------------------------------------
// 이슈 #138: mcp-trading·backend 공유 판정표. 같은 파일을 backend/tests/test_stock_code.py도
// 읽어 같은 행에 같은 판정을 내는지 확인하므로, 한쪽 계층만 정책을 바꾸면 그 계층의
// 스위트가 red가 된다(docs/issue-138-alnum-stock-code.md §6.2·§6.4-1).
// 아래 테스트 이름은 표로 바꾸기 전(단일 케이스 시절)의 이름을 그대로 둔다 — 이름으로
// 히스토리를 따라가는 경로를 끊지 않기 위해서다. 지금은 각 테스트가 표의 한 구획을
// 담당하며, 구획의 합집합이 표 전체임을 마지막 메타 테스트가 고정한다.
const policyFixture = JSON.parse(
  readFileSync(new URL("fixtures/orderable_code_policy.json", import.meta.url), "utf-8"),
);
const POLICY_CASES = policyFixture.cases;
const POLICY_VERDICTS = new Set(["pass", "reject_unsupported", "reject_shape"]);

// 이 스위트가 표에서 실제로 집어 간 행. takeCases()를 모듈 로드 시점(테스트 등록
// 시점)에 호출하므로, 아래 메타 테스트는 실행 순서나 -t 필터에 영향받지 않는다.
const drivenCodes = new Set();

function takeCases(predicate) {
  const picked = POLICY_CASES.filter(predicate);
  for (const policyCase of picked) {
    drivenCodes.add(policyCase.code);
  }
  return picked;
}

// 기본값(플래그 꺼짐) 열을 두 구획으로 나눈다: 영숫자 전용 메시지로 거절되는 행과
// 그 밖의 행(통과·일반 메시지 거절). 두 구획의 합집합이 표 전체다.
const OFF_UNSUPPORTED_CASES = takeCases((c) => c.flag_off === "reject_unsupported");
const OFF_REMAINING_CASES = takeCases((c) => c.flag_off !== "reject_unsupported");
const ALL_POLICY_CASES = takeCases(() => true);

function describeCase(policyCase) {
  return `${policyCase.code === "" ? "(빈 문자열)" : policyCase.code} — ${policyCase.note}`;
}

// 표의 3값 verdict를 실제 동작으로 환산한다. 두 거절 메시지를 구분해야 "왜 막혔는가"가
// 표에 남는다 — 통과/거절 2값으로 뭉개면 전용 메시지가 사라져도 아무도 모른다.
function verdictOf(code) {
  try {
    const body = buildCashOrderBody({
      accountNo: "1234567801",
      stockCode: code,
      quantity: 1,
      price: 10000,
      orderType: "LIMIT",
    });
    return { verdict: "pass", body };
  } catch (error) {
    const verdict = /이 종목은 현재 주문을 지원하지 않습니다/.test(error.message)
      ? "reject_unsupported"
      : "reject_shape";
    return { verdict, body: null };
  }
}

function assertPolicyColumn(cases, column) {
  assert.ok(cases.length > 0, `${column} 구획이 비어 있다 — 표가 잘못 필터링됐다`);
  for (const policyCase of cases) {
    const expected = policyCase[column];
    // 오타난 verdict가 "거절"로 조용히 취급되지 않게 먼저 막는다.
    assert.ok(
      POLICY_VERDICTS.has(expected),
      `알 수 없는 verdict "${expected}": ${describeCase(policyCase)}`,
    );
    const { verdict, body } = verdictOf(policyCase.code);
    assert.equal(verdict, expected, describeCase(policyCase));
    if (expected === "pass") {
      // 통과 행은 코드가 손상 없이 PDNO에 실리는지까지 본다.
      assert.equal(body.PDNO, policyCase.code, describeCase(policyCase));
    }
  }
}

function withAlnumFlag(t, value) {
  const originalEnvValue = process.env.KIS_ALNUM_STOCK_ORDER_ENABLED;
  t.after(() => {
    if (originalEnvValue === undefined) {
      delete process.env.KIS_ALNUM_STOCK_ORDER_ENABLED;
    } else {
      process.env.KIS_ALNUM_STOCK_ORDER_ENABLED = originalEnvValue;
    }
  });
  if (value === undefined) {
    delete process.env.KIS_ALNUM_STOCK_ORDER_ENABLED;
  } else {
    process.env.KIS_ALNUM_STOCK_ORDER_ENABLED = value;
  }
}

test("buildCashOrderBody rejects alphanumeric stock codes as unsupported order targets", (t) => {
  // 기본값에서 영숫자 6~7자(0001A0·00088K·Q500020 등)가 전용 메시지로 거절되는 행들.
  withAlnumFlag(t, undefined);
  assertPolicyColumn(OFF_UNSUPPORTED_CASES, "flag_off");
});

test("buildCashOrderBody rejects nine-char fund codes as unsupported order targets", (t) => {
  // 기본값 열의 나머지 — 9자 코드(F70100026·J0036221D)는 첫 가드에 매치조차 되지 않아
  // 일반 형태 메시지로 거절되고, 숫자 6~7자는 통과한다. 이름은 히스토리 유지를 위해
  // 그대로 두되 담당 범위는 "전용 메시지가 아닌 나머지 전부"다.
  withAlnumFlag(t, undefined);
  assertPolicyColumn(OFF_REMAINING_CASES, "flag_off");
});

test(
  "buildCashOrderBody allows alphanumeric and nine-char codes when KIS_ALNUM_STOCK_ORDER_ENABLED=true (#138)",
  (t) => {
    withAlnumFlag(t, "true");
    assertPolicyColumn(ALL_POLICY_CASES, "flag_on");
  },
);

test("buildCashOrderBody keeps rejecting alphanumeric codes when the flag is not exactly \"true\"", (t) => {
  // backend/stock_code.py의 _alnum_stock_order_enabled()와 비교 방식을 맞춘다 —
  // 대소문자·공백을 관용하면 두 계층의 판정이 갈릴 수 있다. "True"는 꺼진 것으로 보므로
  // 표의 flag_off 열 전체가 그대로 성립해야 한다.
  withAlnumFlag(t, "True");
  assertPolicyColumn(ALL_POLICY_CASES, "flag_off");
});

test("orderable code policy fixture rows are all exercised by this suite (#138)", () => {
  // 표에 행을 추가했는데 한쪽 계층이 조용히 무시하는 것을 막는 메타 테스트다.
  // 파일을 다시 읽어 비교하므로, 위 구획 필터가 어떤 행을 빠뜨리면 여기서 잡힌다.
  const fresh = JSON.parse(
    readFileSync(new URL("fixtures/orderable_code_policy.json", import.meta.url), "utf-8"),
  );
  const freshCodes = fresh.cases.map((c) => c.code);
  assert.equal(new Set(freshCodes).size, freshCodes.length, "판정표에 중복 code 행이 있다");
  assert.deepEqual([...drivenCodes].sort(), [...freshCodes].sort());

  // 형식별 대표 코드와 경계 케이스는 표에서 사라지면 안 된다
  // (docs/issue-138-alnum-stock-code.md §6.4-1·§6.4-4).
  for (const required of ["005930", "0001A0", "Q500020", "F70100026", "12345678", "００５９３０"]) {
    assert.ok(freshCodes.includes(required), `판정표에서 대표 코드 ${required}가 사라졌다`);
  }

  for (const policyCase of fresh.cases) {
    assert.ok(
      typeof policyCase.note === "string" && policyCase.note.length > 0,
      `${policyCase.code} 행에 note가 없다 — 표는 정책을 설명해야 한다`,
    );
  }
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
