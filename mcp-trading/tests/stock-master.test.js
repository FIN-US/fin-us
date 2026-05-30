import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { clearStocksCache, DEFAULT_STOCKS_PATH, resolveStock } from "../stock-master.js";

test("resolveStock resolves names from the expanded domestic stock master", () => {
  assert.deepEqual(resolveStock("카카오"), {
    code: "035720",
    name: "카카오",
    market: "KOSPI",
    aliases: [],
  });
});

test("resolveStock keeps 6 digit stock codes available without master entries", () => {
  assert.deepEqual(resolveStock("123456"), {
    code: "123456",
    name: "123456",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock keeps alphanumeric KIS stock codes available without master entries", () => {
  assert.deepEqual(resolveStock("0001a0"), {
    code: "0001A0",
    name: "0001A0",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock guides users to 6 digit stock codes when a name is missing", () => {
  assert.throws(
    () => resolveStock("없는종목"),
    /6자리 종목코드로 직접 입력/,
  );
});

test("resolveStock reads the default stock master once per process cache", () => {
  clearStocksCache();
  const originalReadFileSync = fs.readFileSync;
  let readCount = 0;

  fs.readFileSync = function readFileSync(filePath, ...args) {
    if (filePath === DEFAULT_STOCKS_PATH) {
      readCount += 1;
    }
    return originalReadFileSync.call(this, filePath, ...args);
  };

  try {
    resolveStock("카카오");
    resolveStock("삼성전자");
  } finally {
    fs.readFileSync = originalReadFileSync;
    clearStocksCache();
  }

  assert.equal(readCount, 1);
});

test("resolveStock does not read the default stock master for direct stock codes", () => {
  clearStocksCache();
  const originalReadFileSync = fs.readFileSync;
  let readCount = 0;

  fs.readFileSync = function readFileSync(filePath, ...args) {
    if (filePath === DEFAULT_STOCKS_PATH) {
      readCount += 1;
    }
    return originalReadFileSync.call(this, filePath, ...args);
  };

  try {
    assert.deepEqual(resolveStock("123456"), {
      code: "123456",
      name: "123456",
      market: "UNKNOWN",
      aliases: [],
    });
  } finally {
    fs.readFileSync = originalReadFileSync;
    clearStocksCache();
  }

  assert.equal(readCount, 0);
});

test("resolveStock resolves deduplicated ETN names without ambiguity", () => {
  assert.deepEqual(resolveStock("한투 금선물 ETN"), {
    code: "Q570121",
    name: "한투 금선물 ETN",
    market: "KOSPI",
    aliases: [],
  });
});
