import assert from "node:assert/strict";
import test from "node:test";
import { readPageDelayMsEnv } from "../balance.js";

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
