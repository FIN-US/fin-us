import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test, { after } from "node:test";
import {
  DEFAULT_ORDER_DEDUP_TTL_MS,
  DuplicateOrderError,
  LedgerUnreadableError,
  OrderDedupStore,
  createOrderDedupKey,
  renameRetryCodesForPlatform,
} from "../order-dedup.js";

// RENAME_RETRY_CODES는 모듈 로드 시점의 process.platform으로 한 번만
// 계산되므로, 실제 OS를 바꾸지 않고 그 배선 자체를 검증하려면 process.platform을
// Object.defineProperty로 잠깐 바꾼 뒤 캐시 우회용 쿼리스트링으로 모듈을 새로
// 평가시켜야 한다(order-dedup.js의 top-level 코드가 그 platform 값으로 다시
// 실행된다). import 직후 바로 원래 값으로 되돌리므로, 이후 store 생성·호출은
// 실제 호스트 플랫폼에 영향받지 않는다.
async function importOrderDedupModuleForPlatform(platform) {
  const originalPlatform = process.platform;
  Object.defineProperty(process, "platform", { value: platform, configurable: true });
  try {
    return await import(
      new URL(`../order-dedup.js?platform-test=${platform}-${Date.now()}-${Math.random()}`, import.meta.url)
    );
  } finally {
    Object.defineProperty(process, "platform", { value: originalPlatform, configurable: true });
  }
}

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
// 섞이지 않아야 한다. 잡는 뮤테이션: #ledgerReadError나 catch 분기가
// error.message/원본 텍스트를 그대로 메시지나 로그에 이어붙이면 실패해야
// 한다.
//
// 페이로드 선택 주의: `{"CANO": "...", broken`처럼 유효한 JSON 토큰(따옴표로
// 감싼 값) 뒤에서 깨지는 페이로드는 Node 24(V8)의 JSON.parse 에러 메시지가
// "Expected double-quoted property name in JSON at position N"처럼 위치만
// 보고하고 원문을 에코하지 않는다 - 그 페이로드로는 실제로 원문을 그대로
// 메시지에 이어붙이는 구현이어도 이 테스트가 통과해버려 검증이 공허해진다.
// 반면 유효하지 않은 토큰으로 시작하는 페이로드("notjson...", "CANO=...")는
// V8이 `Unexpected token 'x', "<원문 일부>" is not valid JSON` 형태로 원문을
// 그대로 에코한다(Node v24.18.1, backend/Dockerfile의 setup_24.x로 실측
// 확인). 그래야 "메시지를 그대로 이어붙이는" 뮤테이션이 실제로 걸린다.
for (const [label, buildPayload] of [
  ["notjson prefix", (cano) => `notjson${cano}`],
  ["CANO= prefix", (cano) => `CANO=${cano}`],
]) {
  test(`OrderDedupStore.reserve never leaks ledger contents (CANO) into the thrown error or logs (${label})`, (t) => {
    const filePath = tempLedgerPath(t);
    const leakedCano = "1234567890";
    const payload = buildPayload(leakedCano);
    fs.writeFileSync(filePath, payload);

    // 이 페이로드가 실제로 V8의 SyntaxError 메시지에 leakedCano를 에코하는지
    // 먼저 확인한다 - 이 전제가 깨지면(예: 엔진이 바뀌어 더 이상 에코하지
    // 않으면) 아래 assert.ok(!...)가 항상 참이 되어 테스트가 다시 공허해질
    // 수 있으므로, 전제 자체를 함께 고정한다.
    let parseEchoesPayload = false;
    try {
      JSON.parse(payload);
    } catch (error) {
      parseEchoesPayload = error.message.includes(leakedCano);
    }
    assert.ok(
      parseEchoesPayload,
      `이 테스트의 전제(JSON.parse 에러 메시지가 원문을 에코함)가 깨졌습니다. 페이로드를 갱신하세요: ${payload}`,
    );

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
}

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

// 읽기 실패 시 던지는 에러의 타입을 호출자가 instanceof로 구분할 수 있어야
// 한다(DuplicateOrderError와 동일한 관례). 잡는 뮤테이션: #ledgerReadError가
// LedgerUnreadableError 대신 평범한 Error를 던지면 실패해야 한다.
test("OrderDedupStore.reserve throws a LedgerUnreadableError instance on a corrupted ledger", (t) => {
  const filePath = tempLedgerPath(t);
  fs.writeFileSync(filePath, "not json");
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }), LedgerUnreadableError);
});

// markSucceeded()·release()도 reserve()와 같은 #readLedger를 거치므로, 손상된
// 원장에서는 이 둘도 똑같이 LedgerUnreadableError를 던져야 한다. 이 계약은
// #readLedger가 private이라 공개 메서드를 통한 테스트로만 고정할 수 있는데,
// 지금까지는 reserve()만 다뤄왔다 - 이 계약이 실제로 지켜지는지 아무 테스트도
// 확인하지 않았다.
//
// order-submit.js의 submitOrder()는 이 예외가 던져진다는 것을 전제로
// 설계됐다: markSucceeded 실패는 삼키고 로그만 남겨 항목을 in_flight로 유지하고
// (주문은 이미 KIS에 체결됐으므로 가드를 지우면 재시도가 중복 주문으로
// 이어진다), release는 kisOrderRejected/kisOrderNotSubmitted로 확인된 경우에만
// 부르고 그 실패도 삼켜 항목을 in_flight로 유지한다. 누군가 markSucceeded나
// release 안에 "기록 실패는 무해하니 삼키자"는 try/catch를 추가해 이 예외를
// 여기서 막아버리면, submitOrder의 그 방어가 통째로 무력화되는데도 reserve()만
// 두드리는 기존 테스트는 전부 초록으로 남는다. 잡는 뮤테이션: markSucceeded나
// release가 #readLedger의 예외를 삼키면(또는 손상 여부와 무관하게 조용히
// 반환하면) 해당 테스트가 실패해야 한다.
for (const [label, invoke] of [
  ["markSucceeded", (store) => store.markSucceeded("k", { ODNO: "1" })],
  ["release", (store) => store.release("k")],
]) {
  test(`OrderDedupStore.${label} throws a LedgerUnreadableError on a corrupted ledger`, (t) => {
    const filePath = tempLedgerPath(t);
    fs.writeFileSync(filePath, "not json");
    const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

    assert.throws(() => invoke(store), LedgerUnreadableError);
  });
}

// 고아 임시 파일 정리는 rename 실패뿐 아니라 write/fsync 실패에서도 대칭이어야
// 한다. 잡는 뮤테이션: #writeLedger의 catch가 rename 실패만 잡고 write/fsync
// 실패는 그냥 던지던(정리 없이) 원래 형태로 되돌아가면 이 두 테스트가
// 실패해야 한다. tmpPath는 무작위 접미사를 갖지만 항상 `.tmp`로 끝나므로,
// 원장 디렉터리에서 `.tmp`로 끝나는 파일이 하나도 없어야 한다고 검증한다.
test("OrderDedupStore.reserve cleans up the temp file when fsync fails", (t) => {
  const filePath = tempLedgerPath(t);
  const dir = path.dirname(filePath);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  t.mock.method(fs, "fsyncSync", () => {
    const error = new Error("simulated fsync failure");
    error.code = "EIO";
    throw error;
  });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));

  const leftoverTmp = fs.readdirSync(dir).filter((name) => name.endsWith(".tmp"));
  assert.deepEqual(leftoverTmp, [], `임시 파일이 정리되지 않고 남았습니다: ${leftoverTmp}`);
});

test("OrderDedupStore.reserve cleans up the temp file when the write fails", (t) => {
  const filePath = tempLedgerPath(t);
  const dir = path.dirname(filePath);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const originalWriteFileSync = fs.writeFileSync;
  t.mock.method(fs, "writeFileSync", (fdOrPath, ...rest) => {
    if (typeof fdOrPath === "number") {
      const error = new Error("simulated write failure");
      error.code = "ENOSPC";
      throw error;
    }
    return originalWriteFileSync(fdOrPath, ...rest);
  });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));

  const leftoverTmp = fs.readdirSync(dir).filter((name) => name.endsWith(".tmp"));
  assert.deepEqual(leftoverTmp, [], `임시 파일이 정리되지 않고 남았습니다: ${leftoverTmp}`);
});

// #writeLedger가 fd에 fs.writeSync(반환값 무시)가 아니라 fs.writeFileSync(전체
// 데이터가 쓰일 때까지 내부 반복)를 쓰는지 확인한다. 짧은 쓰기를 OS 수준에서
// 직접 재현하기는 플랫폼 의존적이라, 대신 우리 코드가 실제로 fs.writeFileSync를
// 호출하고 그 한 번의 호출에 완전한 JSON을 통째로 넘기는지를 관찰한다. 잡는
// 뮤테이션: #writeLedger가 fs.writeSync(fd, data)로 되돌아가면(원래 구현)
// fs.writeFileSync가 전혀 호출되지 않아 이 테스트가 실패해야 한다.
test("OrderDedupStore.reserve writes the ledger via fs.writeFileSync on the temp fd, not a bare fs.writeSync", (t) => {
  const filePath = tempLedgerPath(t);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const originalWriteFileSync = fs.writeFileSync;
  const fdWriteCalls = [];
  t.mock.method(fs, "writeFileSync", (fdOrPath, ...rest) => {
    if (typeof fdOrPath === "number") {
      fdWriteCalls.push({ fd: fdOrPath, data: rest[0] });
    }
    return originalWriteFileSync(fdOrPath, ...rest);
  });

  store.reserve("k", { stockCode: "005930" });

  assert.equal(fdWriteCalls.length, 1, "fd 기반 fs.writeFileSync가 정확히 한 번 호출돼야 합니다");
  const parsed = JSON.parse(fdWriteCalls[0].data);
  assert.ok(parsed.k, "한 번의 호출에 완전한 JSON 원장이 통째로 전달돼야 합니다");

  const onDisk = JSON.parse(fs.readFileSync(filePath, "utf8"));
  assert.ok(onDisk.k, "디스크에 기록된 원장이 완전해야 합니다(짧은 쓰기로 잘리지 않음)");
});

// rename의 EPERM/EACCES/EBUSY는 외부 보유자(백신 등)로 인한 일시적 실패일 수
// 있어 재시도해야 한다(권장 사항). 잡는 뮤테이션: #renameLedgerAtomic이
// 재시도 없이 첫 실패에서 바로 던지면 이 테스트가 실패해야 한다 - 세 번째
// 시도에서만 성공하도록 몽키패치했으므로 재시도가 없으면 절대 성공하지 못한다.
test("OrderDedupStore.reserve retries a transient EPERM rename failure and eventually succeeds", (t) => {
  const filePath = tempLedgerPath(t);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const originalRenameSync = fs.renameSync;
  let calls = 0;
  t.mock.method(fs, "renameSync", (src, dest) => {
    calls += 1;
    if (dest === filePath && calls < 3) {
      const error = new Error("simulated transient lock");
      error.code = "EPERM";
      throw error;
    }
    return originalRenameSync(src, dest);
  });

  assert.doesNotThrow(() => store.reserve("k", { stockCode: "005930" }));
  assert.equal(calls, 3, "세 번째 시도에서 성공해야 합니다");
});

// 재시도에도 한계가 있어야 한다 - 영구적인 실패까지 무한정 재시도하면 주문
// 경로가 멈춘다. 잡는 뮤테이션: RENAME_MAX_ATTEMPTS 상한 없이 무한 재시도하면
// (또는 반대로 재시도를 아예 하지 않아 위 테스트가 실패로 이미 잡지만, 이
// 테스트는 "결국은 던진다"는 상한 쪽을 잡는다) 이 테스트가 타임아웃되거나
// 실패해야 한다.
test("OrderDedupStore.reserve gives up and throws after exhausting rename retries on persistent EBUSY", (t) => {
  const filePath = tempLedgerPath(t);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  let calls = 0;
  t.mock.method(fs, "renameSync", () => {
    calls += 1;
    const error = new Error("simulated persistent lock");
    error.code = "EBUSY";
    throw error;
  });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));
  assert.ok(calls >= 2, `재시도가 이뤄져야 합니다 (calls=${calls})`);
});

// ENOSPC 같은 비일시적 에러코드는 재시도 대상이 아니다 - 디스크가 가득 찬
// 상태는 몇 번을 더 시도해도 나아지지 않으므로 즉시 실패해 빠르게 신호를
// 줘야 한다. 잡는 뮤테이션: RENAME_RETRY_CODES 판별 없이 모든 에러코드를
// 재시도하면(과도한 재시도) calls가 1을 초과해 실패해야 한다.
test("OrderDedupStore.reserve does not retry rename failures with non-transient error codes", (t) => {
  const filePath = tempLedgerPath(t);
  const store = new OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  let calls = 0;
  t.mock.method(fs, "renameSync", () => {
    calls += 1;
    const error = new Error("simulated disk full");
    error.code = "ENOSPC";
    throw error;
  });

  assert.throws(() => store.reserve("k", { stockCode: "005930" }));
  assert.equal(calls, 1, "재시도 대상이 아닌 에러코드는 한 번만 시도해야 합니다");
});

// POSIX에서 rename의 EACCES는 "대상 디렉터리에 쓰기 권한 없음"으로
// 결정론적이라 재시도 대상이 아니어야 하고(ENOSPC와 같은 논리), Windows에서는
// MoveFileEx가 보유자 충돌에도 EACCES를 내므로 재시도 대상에 남아 있어야
// 한다. 이 분기를 순수 함수로 뽑아 실제 OS와 무관하게 직접 고정한다. 잡는
// 뮤테이션: renameRetryCodesForPlatform이 플랫폼과 무관하게 항상 같은 집합을
// 돌려주면(예: 예전처럼 EACCES를 모든 플랫폼에 포함) 이 테스트가 실패해야
// 한다.
test("renameRetryCodesForPlatform includes EACCES only on win32", () => {
  assert.deepEqual(
    [...renameRetryCodesForPlatform("win32")].sort(),
    ["EACCES", "EBUSY", "EPERM"],
    "win32은 EACCES도 재시도 대상이어야 합니다",
  );

  for (const posixPlatform of ["linux", "darwin", "freebsd"]) {
    assert.deepEqual(
      [...renameRetryCodesForPlatform(posixPlatform)].sort(),
      ["EBUSY", "EPERM"],
      `${posixPlatform}은 EACCES를 재시도하면 안 됩니다`,
    );
  }
});

// 위 순수 함수 테스트는 분기 자체는 고정하지만, 모듈 최상단의
// RENAME_RETRY_CODES가 실제로 그 함수 결과를 쓰는지, 그리고
// #renameLedgerAtomic이 그 집합을 실제로 참조하는지는 별도로 확인해야 한다 -
// 그래서 process.platform을 바꾼 채로 모듈을 새로 불러와 실제 쓰기 경로를
// 끝까지 태운다. 잡는 뮤테이션: EACCES를 모든 플랫폼에서 재시도하도록
// 되돌리면(원래 구현) "linux" 케이스에서 calls가 1을 넘어야 할 이 assert가
// 깨져야 한다.
test("OrderDedupStore.reserve does not retry a rename EACCES on a POSIX platform (simulated linux)", async (t) => {
  const filePath = tempLedgerPath(t);
  const mod = await importOrderDedupModuleForPlatform("linux");
  const store = new mod.OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  let calls = 0;
  t.mock.method(fs, "renameSync", () => {
    calls += 1;
    const error = new Error("simulated EACCES on target directory");
    error.code = "EACCES";
    throw error;
  });

  // rename 실패는 (읽기 실패와 달리) LedgerUnreadableError로 감싸지 않고
  // fs.renameSync가 던진 원본 에러를 그대로 전파한다 - 여기서는 그 사실이
  // 아니라 "재시도 여부"만 확인한다.
  assert.throws(() => store.reserve("k", { stockCode: "005930" }));
  assert.equal(calls, 1, "linux에서는 rename EACCES를 재시도하면 안 됩니다");
});

// win32에서는 EACCES도 여전히 재시도돼야 한다(회귀 방지) - 실제 호스트가
// win32가 아니어도(예: CI가 Linux 러너라도) 이 테스트가 항상 같은 결과를
// 내도록 process.platform을 명시적으로 고정해서 검증한다.
test("OrderDedupStore.reserve retries a rename EACCES on win32 and eventually succeeds", async (t) => {
  const filePath = tempLedgerPath(t);
  const mod = await importOrderDedupModuleForPlatform("win32");
  const store = new mod.OrderDedupStore({ filePath, ttlMs: 60_000, now: () => 1_000 });

  const originalRenameSync = fs.renameSync;
  let calls = 0;
  t.mock.method(fs, "renameSync", (src, dest) => {
    calls += 1;
    if (dest === filePath && calls < 2) {
      const error = new Error("simulated transient EACCES (e.g. AV lock)");
      error.code = "EACCES";
      throw error;
    }
    return originalRenameSync(src, dest);
  });

  assert.doesNotThrow(() => store.reserve("k", { stockCode: "005930" }));
  assert.equal(calls, 2, "win32에서는 rename EACCES가 재시도돼 두 번째 시도에서 성공해야 합니다");
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
