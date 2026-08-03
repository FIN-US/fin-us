import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test, { after } from "node:test";
import {
  DEFAULT_ORDER_DEDUP_TTL_MS,
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

  const validCase = ["600000", 600_000]; // 유효한 값은 그대로 사용되고 경고가 없다
  const invalidCases = [
    ["600_000", DEFAULT_ORDER_DEDUP_TTL_MS], // JS 숫자 구분자(_) 습관 - Number()는 이를 파싱하지 못함
    ["10m", DEFAULT_ORDER_DEDUP_TTL_MS], // 단위 접미사 오타
    ["0", DEFAULT_ORDER_DEDUP_TTL_MS], // <= 0 분기
    ["-1", DEFAULT_ORDER_DEDUP_TTL_MS], // <= 0 분기, 음수
    ["60.5", DEFAULT_ORDER_DEDUP_TTL_MS], // !Number.isInteger 분기
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
    assert.ok(
      message.includes(`="${envValue}"`),
      `경고 메시지에서 값의 경계가 따옴표로 표시되어야 합니다: ${message}`,
    );
    assert.ok(
      message.includes(String(DEFAULT_ORDER_DEDUP_TTL_MS)),
      `경고 메시지에 실제 적용되는 기본값이 포함되어야 합니다: ${message}`,
    );
  }

  // 값이 아예 없는 경우(정상적인 기본값 사용)는 조용해야 한다.
  consoleError.mock.resetCalls();
  delete process.env.KIS_ORDER_DEDUP_TTL_MS;
  assert.equal(new OrderDedupStore().ttlMs, DEFAULT_ORDER_DEDUP_TTL_MS, "env=unset");
  assert.equal(
    consoleError.mock.callCount(),
    0,
    "env var가 설정되지 않은 정상 폴백 경로는 조용해야 합니다(console.error 호출 없음)",
  );
});

// #128: 읽기 실패는 fail-closed여야 한다. ENOENT만 예외적으로 "원장 없음"
// (신규 설치)으로 조용히 통과한다. 그 외 파싱 실패·형태 불일치는 전부 던져야
// 하며, 손상된 원장이 빈 원장으로 취급돼 중복 주문을 통과시키는 사고
// (fail-open)를 막는다.

// 잡는 뮤테이션: #readLedger의 `if (error.code === "ENOENT") { return {}; }`를
// 지우거나 `throw`를 빈 객체 반환으로 바꾸면 이 테스트가 실패해야 한다.
test("OrderDedupStore.reserve does not throw on a missing ledger file (fresh install)", (t) => {
  const store = new OrderDedupStore({
    filePath: tempLedgerPath(t),
    ttlMs: 60_000,
    now: () => 1_000,
  });

  assert.doesNotThrow(() => store.reserve("fresh-install", { stockCode: "005930" }));
});

// 잡는 뮤테이션: catch 블록에서 error.code !== "ENOENT"일 때 던지지 않고
// 조용히 넘어가면(원래 코드의 동작) 이 테스트가 실패해야 한다.
test("OrderDedupStore.reserve throws on a 0-byte ledger file", (t) => {
  const filePath = tempLedgerPath(t);
  fs.writeFileSync(filePath, "");
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));
});

// 잡는 뮤테이션: 위와 동일한 catch 분기 - truncated JSON은 SyntaxError를
// 던지므로, catch에서 ENOENT 외 코드일 때 삼키면 실패해야 한다.
test("OrderDedupStore.reserve throws on a truncated JSON ledger file", (t) => {
  const filePath = tempLedgerPath(t);
  fs.writeFileSync(filePath, '{"some-key": {"expiresAt": 999');
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));
});

// 잡는 뮤테이션: 형태 가드(`if (!ledger || typeof ledger !== "object" ||
// Array.isArray(ledger))`)가 `return {}`로 남아 있으면(원래 코드) 이 네
// 케이스 모두 통과해버려 실패해야 한다. "null"/"[]"/"3"은 파싱은 성공하지만
// 형태가 원장(object, non-array)이 아닌 경우들이다.
for (const [label, content] of [
  ["null", "null"],
  ["[]", "[]"],
  ["3", "3"],
]) {
  test(`OrderDedupStore.reserve throws on a wrong-shape ledger file (${label})`, (t) => {
    const filePath = tempLedgerPath(t);
    fs.writeFileSync(filePath, content);
    const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

    assert.throws(() => store.reserve("k", { stockCode: "005930" }));
  });
}

// 잡는 뮤테이션: catch 분기가 ENOENT 외 코드를 삼키면(원래 코드) EACCES에서도
// 실패해야 한다. Windows에는 EACCES를 안정적으로 재현할 chmod 동등물이 없어
// fs.readFileSync를 직접 몽키패치해 error.code=EACCES를 던지는 방식으로
// 플랫폼 무관하게 재현한다.
test("OrderDedupStore.reserve throws when the ledger read fails with EACCES", (t) => {
  const filePath = tempLedgerPath(t);
  fs.writeFileSync(filePath, "{}");
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const originalReadFileSync = fs.readFileSync;
  t.mock.method(fs, "readFileSync", (targetPath, ...args) => {
    if (targetPath === filePath) {
      const error = new Error("permission denied");
      error.code = "EACCES";
      throw error;
    }
    return originalReadFileSync(targetPath, ...args);
  });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));
});

// EIO도 EACCES와 동일하게 error.code로 catch 분기를 타므로 같은 몽키패치
// 방식으로 재현한다. 잡는 뮤테이션: 위와 동일.
test("OrderDedupStore.reserve throws when the ledger read fails with EIO", (t) => {
  const filePath = tempLedgerPath(t);
  fs.writeFileSync(filePath, "{}");
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const originalReadFileSync = fs.readFileSync;
  t.mock.method(fs, "readFileSync", (targetPath, ...args) => {
    if (targetPath === filePath) {
      const error = new Error("i/o error");
      error.code = "EIO";
      throw error;
    }
    return originalReadFileSync(targetPath, ...args);
  });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));
});

// 손상된 원장을 삭제하면 주문이 재개돼야 한다(수용 기준). 잡는 뮤테이션: 삭제
// 후에도 예외가 나거나(예: 캐시된 상태를 잘못 참조), 삭제 전에 예외가 나지
// 않으면(위 테스트들이 이미 잡음) 실패해야 한다.
test("OrderDedupStore.reserve resumes after the corrupted ledger file is deleted", (t) => {
  const filePath = tempLedgerPath(t);
  fs.writeFileSync(filePath, "not json");
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));

  fs.rmSync(filePath);
  assert.doesNotThrow(() => store.reserve("k", { stockCode: "005930" }));
});

// 던지는 메시지에는 경로와 조치 방법이 있어야 하고, 원장 내용(CANO 등)이
// 절대 섞이면 안 된다(수용 기준). 잡는 뮤테이션: 메시지에서 this.filePath를
// 빼거나 "삭제" 안내 문구를 빼면 실패해야 한다. CANO 부재 검사는 별도
// 테스트에서 stderr 로그까지 포함해 확인한다.
test("OrderDedupStore.reserve's thrown message contains the ledger path and remedy", (t) => {
  const filePath = tempLedgerPath(t);
  fs.writeFileSync(filePath, "not json");
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  assert.throws(
    () => store.reserve("k", { stockCode: "005930" }),
    (error) => {
      assert.ok(error.message.includes(filePath), `메시지에 경로가 있어야 합니다: ${error.message}`);
      assert.ok(
        error.message.includes("삭제"),
        `메시지에 조치 방법(삭제 안내)이 있어야 합니다: ${error.message}`,
      );
      return true;
    },
  );
});

// 원장 내용(CANO 포함)이 던지는 메시지에도, console.error 로그에도 절대
// 섞이지 않아야 한다. 형태 불일치 원장에 CANO 평문을 심어 확인한다. 잡는
// 뮤테이션: #ledgerReadError나 catch 분기가 error.message/원본 텍스트를
// 그대로 메시지나 로그에 이어붙이면 실패해야 한다.
test("OrderDedupStore.reserve never leaks ledger contents (CANO) into the thrown error or logs", (t) => {
  const filePath = tempLedgerPath(t);
  const leakedCano = "1234567890";
  // 형태는 유효하지만(object, non-array) JSON.parse 자체는 성공하는 손상
  // 케이스가 아니라, 파싱이 실패하되 그 원문에 CANO가 들어있는 케이스로
  // "메시지에 원문 일부가 새어나가는지"를 검증한다.
  fs.writeFileSync(filePath, `{"CANO": "${leakedCano}", broken`);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const consoleError = t.mock.method(console, "error", () => {});

  assert.throws(
    () => store.reserve("k", { stockCode: "005930" }),
    (error) => {
      assert.ok(
        !error.message.includes(leakedCano),
        `던지는 메시지에 원장 내용(CANO)이 섞이면 안 됩니다: ${error.message}`,
      );
      return true;
    },
  );

  for (const call of consoleError.mock.calls) {
    for (const arg of call.arguments) {
      assert.ok(
        !String(arg).includes(leakedCano),
        `console.error 로그에 원장 내용(CANO)이 섞이면 안 됩니다: ${arg}`,
      );
    }
  }
});

// 쓰기 실패는 주문을 차단해야 한다(수용 기준). #writeLedger가 fs.renameSync를
// 쓰므로, rename 실패를 몽키패치로 재현한다. 잡는 뮤테이션: #writeLedger가
// rename 실패를 삼키거나(try/catch로 무시), rename 대신 원자적이지 않은
// writeFileSync로 되돌리면(에러를 던지지 않는 경로가 생기면) 실패해야 한다.
test("OrderDedupStore.reserve blocks the order when the ledger rename fails", (t) => {
  const filePath = tempLedgerPath(t);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const originalRenameSync = fs.renameSync;
  t.mock.method(fs, "renameSync", (src, dest) => {
    if (dest === filePath) {
      const error = new Error("simulated rename failure");
      error.code = "EPERM";
      throw error;
    }
    return originalRenameSync(src, dest);
  });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));
});

// 원자적 쓰기가 같은 디렉터리에 임시 파일을 만들고 rename으로 갈아치우는지
// 확인한다(코드가 아니라 파일시스템 관찰로 검증). 잡는 뮤테이션: #writeLedger가
// tmpPath 대신 this.filePath에 직접 fs.writeFileSync를 쓰도록 되돌리면(원래
// 구현), rename 몽키패치가 호출되지 않아 이 테스트가 실패해야 한다.
test("OrderDedupStore.reserve writes the ledger via a same-directory temp file + rename", (t) => {
  const filePath = tempLedgerPath(t);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const dir = path.dirname(filePath);
  const renamedPairs = [];
  const originalRenameSync = fs.renameSync;
  t.mock.method(fs, "renameSync", (src, dest) => {
    renamedPairs.push([src, dest]);
    return originalRenameSync(src, dest);
  });

  store.reserve("k", { stockCode: "005930" });

  assert.equal(renamedPairs.length, 1, "renameSync가 정확히 한 번 호출돼야 합니다");
  const [src, dest] = renamedPairs[0];
  assert.equal(dest, filePath);
  assert.equal(path.dirname(src), dir, "임시 파일은 원장과 같은 디렉터리에 있어야 합니다");
  assert.notEqual(src, filePath, "임시 파일 경로는 최종 경로와 달라야 합니다");
  assert.equal(fs.existsSync(filePath), true);
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

  // 공백만 있는 값(docker-compose.yml의 environment: 블록이나 셸 export로
  // 전달될 수 있다 - dotenv는 트림하지만 이 경로들은 그대로 전달한다)도
  // 빈 문자열과 같은 의도이므로 조용히 처리되어야 한다.
  consoleError.mock.resetCalls();
  process.env.KIS_ORDER_DEDUP_TTL_MS = "   ";
  assert.equal(new OrderDedupStore().ttlMs, 120_000, "env=whitespace-only");
  assert.equal(
    consoleError.mock.callCount(),
    0,
    "공백만 있는 값은 미설정과 동일하게 조용히 처리되어야 합니다",
  );
});
