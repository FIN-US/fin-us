import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test, { after } from "node:test";
import {
  DuplicateOrderError,
  OrderDedupStore,
  createOrderDedupKey,
} from "../order-dedup.js";

const createdDirs = [];

function tempLedgerPath(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "finus-order-dedup-test-"));
  createdDirs.push(dir);
  t.after(() => fs.rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 }));
  return path.join(dir, "ledger.json");
}

after(() => {
  for (const dir of createdDirs) {
    assert.equal(fs.existsSync(dir), false, `임시 디렉터리가 정리되지 않았습니다: ${dir}`);
  }
});

test("createOrderDedupKey normalizes equivalent order arguments", () => {
  const first = createOrderDedupKey({
    accountNo: "1234567801",
    orderEnv: "DEMO",
    stockCode: "005930",
    side: "buy",
    quantity: 1,
    price: 0,
    orderType: "market",
  });
  const second = createOrderDedupKey({
    accountNo: " 1234567801 ",
    orderEnv: "demo",
    stockCode: "005930",
    side: "BUY",
    quantity: "1",
    price: "0",
    orderType: "MARKET",
  });

  assert.equal(first, second);
});

test("createOrderDedupKey ignores original price for market orders", () => {
  const baseOrder = {
    accountNo: "1234567801",
    orderEnv: "demo",
    stockCode: "005930",
    side: "BUY",
    quantity: 1,
    orderType: "MARKET",
  };

  assert.equal(
    createOrderDedupKey({ ...baseOrder, price: 0 }),
    createOrderDedupKey({ ...baseOrder, price: 50_000 }),
  );
});

test("OrderDedupStore blocks duplicate reservations before TTL expires", (t) => {
  const store = new OrderDedupStore({
    filePath: tempLedgerPath(t),
    ttlMs: 60_000,
    now: () => 1_000,
  });

  store.reserve("same-order", { stockCode: "005930" });

  assert.throws(
    () => store.reserve("same-order", { stockCode: "005930" }),
    DuplicateOrderError,
  );
});

test("OrderDedupStore allows reservation after TTL expires", (t) => {
  let now = 1_000;
  const store = new OrderDedupStore({
    filePath: tempLedgerPath(t),
    ttlMs: 60_000,
    now: () => now,
  });

  store.reserve("same-order", { stockCode: "005930" });
  now = 61_001;

  assert.doesNotThrow(() => store.reserve("same-order", { stockCode: "005930" }));
});

test("OrderDedupStore releases failed reservations", (t) => {
  const store = new OrderDedupStore({
    filePath: tempLedgerPath(t),
    ttlMs: 60_000,
    now: () => 1_000,
  });

  store.reserve("same-order", { stockCode: "005930" });
  store.release("same-order");

  assert.doesNotThrow(() => store.reserve("same-order", { stockCode: "005930" }));
});

test("OrderDedupStore resolves filePath from KIS_ORDER_DEDUP_PATH env var, falling back to os.tmpdir()", (t) => {
  const originalEnvValue = process.env.KIS_ORDER_DEDUP_PATH;
  t.after(() => {
    if (originalEnvValue === undefined) {
      delete process.env.KIS_ORDER_DEDUP_PATH;
    } else {
      process.env.KIS_ORDER_DEDUP_PATH = originalEnvValue;
    }
  });

  const envPath = path.join(os.tmpdir(), "finus-order-dedup-env-override.json");
  process.env.KIS_ORDER_DEDUP_PATH = envPath;
  assert.equal(new OrderDedupStore().filePath, envPath);

  delete process.env.KIS_ORDER_DEDUP_PATH;
  assert.equal(
    new OrderDedupStore().filePath,
    path.join(os.tmpdir(), "finus-kis-order-dedup.json"),
  );
});

test("OrderDedupStore blocks duplicate reservations across separate instances sharing a filePath", (t) => {
  const filePath = tempLedgerPath(t);
  const storeA = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });
  const storeB = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  storeA.reserve("same-order", { stockCode: "005930" });

  assert.throws(
    () => storeB.reserve("same-order", { stockCode: "005930" }),
    DuplicateOrderError,
  );
});

// 잘못된 KIS_ORDER_DEDUP_TTL_MS 값은 기본값으로 폴백하되, 사용자가 실제로
// 값을 지정했는데 파싱에 실패한 경우 stderr에 경고를 남긴다(#148).
// 값이 아예 없는 경우(정상적인 기본값 사용)는 조용해야 한다.
test("OrderDedupStore resolves ttlMs from KIS_ORDER_DEDUP_TTL_MS env var, warning on invalid values but staying silent when unset", (t) => {
  const originalEnvValue = process.env.KIS_ORDER_DEDUP_TTL_MS;
  t.after(() => {
    if (originalEnvValue === undefined) {
      delete process.env.KIS_ORDER_DEDUP_TTL_MS;
    } else {
      process.env.KIS_ORDER_DEDUP_TTL_MS = originalEnvValue;
    }
  });

  const consoleError = t.mock.method(console, "error", () => {});

  const DEFAULT_TTL_MS = 120_000;
  const validCase = ["600000", 600_000]; // 유효한 값은 그대로 사용되고 경고가 없다
  const invalidCases = [
    ["600_000", DEFAULT_TTL_MS], // JS 숫자 구분자(_) 습관 - Number()는 이를 파싱하지 못함
    ["10m", DEFAULT_TTL_MS], // 단위 접미사 오타
    ["0", DEFAULT_TTL_MS], // <= 0 분기
    ["-1", DEFAULT_TTL_MS], // <= 0 분기, 음수
    ["60.5", DEFAULT_TTL_MS], // !Number.isInteger 분기
  ];

  // 유효한 값: 파싱된 값을 그대로 쓰고, 경고가 없어야 한다.
  const [validEnvValue, expectedValidTtlMs] = validCase;
  process.env.KIS_ORDER_DEDUP_TTL_MS = validEnvValue;
  assert.equal(new OrderDedupStore().ttlMs, expectedValidTtlMs, `env=${validEnvValue}`);
  assert.equal(
    consoleError.mock.callCount(),
    0,
    `유효한 값(env=${validEnvValue})에는 경고가 없어야 합니다`,
  );

  // 잘못된 값: 기본값으로 폴백하되, 값마다 경고가 한 번씩 발생해야 한다.
  for (const [envValue, expectedTtlMs] of invalidCases) {
    consoleError.mock.resetCalls();
    process.env.KIS_ORDER_DEDUP_TTL_MS = envValue;
    assert.equal(new OrderDedupStore().ttlMs, expectedTtlMs, `env=${envValue}`);
    assert.equal(
      consoleError.mock.callCount(),
      1,
      `잘못된 값(env=${envValue})에는 경고가 한 번 발생해야 합니다`,
    );
    const [message] = consoleError.mock.calls[0].arguments;
    assert.match(
      message,
      /KIS_ORDER_DEDUP_TTL_MS/,
      `경고 메시지에 변수명이 포함되어야 합니다(env=${envValue})`,
    );
    assert.ok(
      message.includes(envValue),
      `경고 메시지에 잘못된 값이 포함되어야 합니다: ${message}`,
    );
  }

  // 값이 아예 없는 경우(정상적인 기본값 사용)는 조용해야 한다.
  consoleError.mock.resetCalls();
  delete process.env.KIS_ORDER_DEDUP_TTL_MS;
  assert.equal(new OrderDedupStore().ttlMs, DEFAULT_TTL_MS, "env=unset");
  assert.equal(
    consoleError.mock.callCount(),
    0,
    "env var가 설정되지 않은 정상 폴백 경로는 조용해야 합니다(console.error 호출 없음)",
  );
});

test("OrderDedupStore stays silent when KIS_ORDER_DEDUP_TTL_MS is set but blank", (t) => {
  const originalEnvValue = process.env.KIS_ORDER_DEDUP_TTL_MS;
  t.after(() => {
    if (originalEnvValue === undefined) {
      delete process.env.KIS_ORDER_DEDUP_TTL_MS;
    } else {
      process.env.KIS_ORDER_DEDUP_TTL_MS = originalEnvValue;
    }
  });

  const consoleError = t.mock.method(console, "error", () => {});

  // 빈 문자열(KIS_ORDER_DEDUP_TTL_MS=)은 .env 파일에서 값을 비워둔 상태와
  // 동일하게 취급한다 - 오타로 값을 지정한 것이 아니라 사실상 미설정과
  // 같은 상태이므로 경고하지 않는다.
  process.env.KIS_ORDER_DEDUP_TTL_MS = "";
  assert.equal(new OrderDedupStore().ttlMs, 120_000, "env=blank");
  assert.equal(
    consoleError.mock.callCount(),
    0,
    "빈 문자열은 미설정과 동일하게 조용히 처리되어야 합니다",
  );
});
