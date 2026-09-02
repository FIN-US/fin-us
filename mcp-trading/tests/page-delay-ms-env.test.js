import assert from "node:assert/strict";
import test from "node:test";
import { SET_TIMEOUT_MAX_MS, readPageDelayMsEnv } from "../balance.js";

// 이슈 #210: readPageDelayMsEnv는 fetchAllPaged의 pageDelayMs를 재배포 없이 실측치로
// 채울 수 있도록 KIS_<name>/FINUS_KIS_<name> env를 읽는 공용 파서다(balance.js). 여기서는
// index.js의 두 상수(DAILY_CCLD_PAGE_DELAY_MS, BALANCE_RLZ_PL_PAGE_DELAY_MS)가 실제로
// 쓰는 이름 대신 테스트 전용 이름을 써서, 이 파일이 index.js를 import하지 않고도(부작용
// 없이) 파서 자체의 계약을 단독 검증한다.
const TEST_ENV_NAME = "TEST_PAGE_DELAY_MS_FOR_ENV_PARSER_TEST";
const KIS_KEY = `KIS_${TEST_ENV_NAME}`;
const FINUS_KIS_KEY = `FINUS_KIS_${TEST_ENV_NAME}`;

function withEnv(t, values) {
  const originals = new Map();
  for (const key of [KIS_KEY, FINUS_KIS_KEY]) {
    originals.set(key, process.env[key]);
    delete process.env[key];
  }
  for (const [key, value] of Object.entries(values)) {
    process.env[key] = value;
  }
  t.after(() => {
    for (const [key, original] of originals) {
      if (original === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = original;
      }
    }
  });
}

// console.error/log를 가로채고 t.after로 되돌린다. 경고는 stderr로만 나가야 한다 —
// console.log(stdout)는 MCP stdio JSON-RPC 채널이라 한 줄만 새도 프로토콜이 깨진다.
function captureConsole(t) {
  const originalConsoleError = console.error;
  const originalConsoleLog = console.log;
  const errorCalls = [];
  console.error = (...args) => errorCalls.push(args.join(" "));
  console.log = (...args) => {
    originalConsoleError("unexpected console.log:", ...args);
    assert.fail("readPageDelayMsEnv must not write to console.log (MCP stdio JSON-RPC channel)");
  };
  t.after(() => {
    console.error = originalConsoleError;
    console.log = originalConsoleLog;
  });
  return errorCalls;
}

test("readPageDelayMsEnv returns the fallback when neither env var is set (regression guard: unset must not change behavior)", (t) => {
  withEnv(t, {});
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 0);
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 250), 250);
});

test("readPageDelayMsEnv returns the fallback when the env var is empty or whitespace-only", (t) => {
  withEnv(t, { [KIS_KEY]: "" });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 0);

  withEnv(t, { [KIS_KEY]: "   " });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 0);
});

test("readPageDelayMsEnv parses a valid KIS_-prefixed override, trimming whitespace", (t) => {
  withEnv(t, { [KIS_KEY]: "  120  " });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 120);
});

test("readPageDelayMsEnv falls back to the FINUS_KIS_-prefixed override when KIS_ is unset", (t) => {
  withEnv(t, { [FINUS_KIS_KEY]: "80" });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 80);
});

test("readPageDelayMsEnv prefers the KIS_-prefixed override over FINUS_KIS_ when both are set", (t) => {
  withEnv(t, { [KIS_KEY]: "50", [FINUS_KIS_KEY]: "999" });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 50);
});

test("readPageDelayMsEnv accepts 0 as an explicit valid override", (t) => {
  withEnv(t, { [KIS_KEY]: "0" });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 999), 0);
});

test("readPageDelayMsEnv rejects a non-numeric value and falls back, logging a warning to stderr (not stdout)", (t) => {
  withEnv(t, { [KIS_KEY]: "not-a-number" });
  const originalConsoleError = console.error;
  const originalConsoleLog = console.log;
  const errorCalls = [];
  const logCalls = [];
  console.error = (...args) => errorCalls.push(args.join(" "));
  console.log = (...args) => logCalls.push(args.join(" "));
  t.after(() => {
    console.error = originalConsoleError;
    console.log = originalConsoleLog;
  });

  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 42), 42);
  assert.equal(logCalls.length, 0, "invalid input must not be logged via console.log (would break MCP stdio JSON-RPC)");
  assert.equal(errorCalls.length, 1);
  assert.match(errorCalls[0], /TEST_PAGE_DELAY_MS_FOR_ENV_PARSER_TEST/);
  assert.match(errorCalls[0], /42ms/);
});

test("readPageDelayMsEnv rejects a negative value and falls back to the default", (t) => {
  withEnv(t, { [KIS_KEY]: "-10" });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 0);
});

test("readPageDelayMsEnv rejects Infinity/NaN-producing input", (t) => {
  withEnv(t, { [KIS_KEY]: "Infinity" });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 5), 5);
});

test("readPageDelayMsEnv does not let a whitespace-only KIS_ value mask the FINUS_KIS_ override", (t) => {
  // `KIS_ || FINUS_KIS_`로 고르면 공백만인 KIS_ 값이 truthy라 FINUS_KIS_를 가린 채
  // trim 결과가 빈 문자열이 되어 fallback으로 떨어진다(빈 문자열일 때는 통과한다).
  withEnv(t, { [KIS_KEY]: "   ", [FINUS_KIS_KEY]: "70" });
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 70);
});

test("readPageDelayMsEnv names the KIS_-prefixed key in the warning, not the bare name", (t) => {
  // 운영자가 설정한 문자열과 로그의 문자열이 같아야 grep으로 찾을 수 있다.
  withEnv(t, { [KIS_KEY]: "nope" });
  const errorCalls = captureConsole(t);
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 0);
  assert.equal(errorCalls.length, 1);
  assert.ok(
    errorCalls[0].includes(KIS_KEY),
    `warning must name the resolved key ${KIS_KEY}, got: ${errorCalls[0]}`,
  );
});

test("readPageDelayMsEnv names the FINUS_KIS_-prefixed key in the warning when that is the one it read", (t) => {
  withEnv(t, { [FINUS_KIS_KEY]: "nope" });
  const errorCalls = captureConsole(t);
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 0);
  assert.equal(errorCalls.length, 1);
  assert.ok(
    errorCalls[0].includes(FINUS_KIS_KEY),
    `warning must name the resolved key ${FINUS_KIS_KEY}, got: ${errorCalls[0]}`,
  );
});

test("readPageDelayMsEnv rejects a delay equal to maxMs and falls back (would truncate every fetch to one page)", (t) => {
  // 시간 예산 이상의 지연은 첫 페이지 직후 budgetExhausted()를 걸어 연속조회를 항상
  // 1페이지로 잘라버린다 — 유량 제한을 위해 늘린 값이 정반대로 동작한다.
  withEnv(t, { [KIS_KEY]: "90000" });
  const errorCalls = captureConsole(t);
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0, { maxMs: 90_000 }), 0);
  assert.equal(errorCalls.length, 1);
  assert.ok(errorCalls[0].includes(KIS_KEY), errorCalls[0]);
  assert.match(errorCalls[0], /90000ms/);
});

test("readPageDelayMsEnv rejects a delay above maxMs and falls back", (t) => {
  withEnv(t, { [KIS_KEY]: "120000" });
  const errorCalls = captureConsole(t);
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0, { maxMs: 90_000 }), 0);
  assert.equal(errorCalls.length, 1);
});

test("readPageDelayMsEnv accepts a delay just below maxMs", (t) => {
  withEnv(t, { [KIS_KEY]: "89999" });
  const errorCalls = captureConsole(t);
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0, { maxMs: 90_000 }), 89_999);
  assert.equal(errorCalls.length, 0);
});

test("readPageDelayMsEnv rejects setTimeout-overflowing values even without an explicit maxMs", (t) => {
  // Node setTimeout은 2^31-1ms를 넘는 지연을 오버플로 경고와 함께 1ms로 접는다 —
  // 상한이 없으면 "지연을 크게 키운" 설정이 사실상 "지연 없음"이 된다.
  withEnv(t, { [KIS_KEY]: String(SET_TIMEOUT_MAX_MS + 1) });
  const errorCalls = captureConsole(t);
  assert.equal(readPageDelayMsEnv(TEST_ENV_NAME, 0), 0);
  assert.equal(errorCalls.length, 1);
  assert.ok(errorCalls[0].includes(KIS_KEY), errorCalls[0]);
});
