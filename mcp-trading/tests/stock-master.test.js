import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { CODE_SHAPE_PATTERN, clearStocksCache, DEFAULT_STOCKS_PATH, getDefaultStocks, resolveStock } from "../stock-master.js";

// Fixture shared by the three alphanumeric-code tests below (🔵3: extracted
// to avoid duplicating the same array literal three times).
const STOCKS_WITH_Q570121 = [
  { code: "Q570121", name: "신한 레버리지 금선물 ETN(H)", market: "KOSDAQ", aliases: [] },
];

test("resolveStock resolves names from the expanded domestic stock master", () => {
  assert.deepEqual(resolveStock("카카오"), {
    code: "035720",
    name: "카카오",
    market: "KOSPI",
    aliases: [],
  });
});

// 🟡1 / updated: numeric codes now go through the master lookup (code
// short-circuit). When the master has NO entry for the code, the isCodeShaped
// echo path must still return UNKNOWN — but we must pass an explicit empty
// array so the test does not accidentally hit the bundled stocks.json (which
// may or may not contain "123456"). Mirrors the pattern used in the
// alphanumeric test below.
test("resolveStock keeps 6 digit stock codes available without master entries", () => {
  assert.deepEqual(resolveStock("123456", []), {
    code: "123456",
    name: "123456",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock keeps alphanumeric KIS stock codes available without master entries", () => {
  // Pass an empty stocks array to simulate a master that contains no entry for
  // this code, so the test does not depend on the bundled stocks.json content.
  assert.deepEqual(resolveStock("0001a0", []), {
    code: "0001A0",
    name: "0001A0",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock guides users to 6/7/9 digit stock codes when a name is missing", () => {
  assert.throws(
    () => resolveStock("없는종목"),
    /6·7·9자리 종목코드로 직접 입력/,
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

// Updated from "does not read the default stock master for direct stock codes":
// numeric codes now go through the master (code short-circuit), so the old
// premise ("read count == 0") is no longer correct. The new invariant is that
// the master is loaded at most once per cache lifetime regardless of how many
// resolveStock calls are made — whether the inputs are names or codes.
test("resolveStock loads the default stock master at most once across multiple calls including numeric codes", () => {
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
    // First call loads the cache; subsequent calls (including a numeric code
    // that hits the master's code short-circuit) must reuse it.
    resolveStock("카카오");
    resolveStock("005930"); // numeric code — now goes through master
    resolveStock("삼성전자");
  } finally {
    fs.readFileSync = originalReadFileSync;
    clearStocksCache();
  }

  assert.equal(readCount, 1);
});

test("resolveStock resolves deduplicated ETN names without ambiguity", () => {
  assert.deepEqual(resolveStock("한투 금선물 ETN"), {
    code: "Q570121",
    name: "한투 금선물 ETN",
    market: "KOSPI",
    aliases: [],
  });
});

// Issue #150: the code-shaped echo (`CODE_SHAPE_PATTERN`) used to run before
// exact name/alias matching, so a listed company whose registered name happens
// to be a 6-7 char alphanumeric string was shadowed by its own name and echoed
// back as an UNKNOWN "code".
// These three still pass after #174 because Step 1 compares stock.code only,
// so a code-SHAPED NAME misses it and Step 2 answers. What they now pin is
// the echo path: if the Step 3 code-shape echo (CODE_SHAPE_PATTERN) is ever
// hoisted in front of name/alias matching, each would fail by returning
// { code: "SIMPAC"/"INVENI"/"WISCOM", market: "UNKNOWN" } instead of the
// real master entry.

test("resolveStock resolves SIMPAC to its real KOSPI code instead of echoing the name as a code", () => {
  assert.deepEqual(resolveStock("SIMPAC"), {
    code: "009160",
    name: "SIMPAC",
    market: "KOSPI",
    aliases: [],
  });
});

test("resolveStock resolves INVENI to its real KOSPI code instead of echoing the name as a code", () => {
  assert.deepEqual(resolveStock("INVENI"), {
    code: "015360",
    name: "INVENI",
    market: "KOSPI",
    aliases: [],
  });
});

test("resolveStock resolves WISCOM to its real KOSPI code instead of echoing the name as a code", () => {
  assert.deepEqual(resolveStock("WISCOM"), {
    code: "024070",
    name: "WISCOM",
    market: "KOSPI",
    aliases: [],
  });
});

test("resolveStock resolves a lowercase code-shaped name case-insensitively, not as an echoed code", () => {
  // The old shortcut regex carried the `/i` flag, so lowercase input echoed
  // back just like the uppercase form. This asserts the fix's name-matching
  // fallback is likewise case-insensitive for code-shaped input, catching a
  // regression where only exact-case name matching was restored.
  assert.deepEqual(resolveStock("simpac"), {
    code: "009160",
    name: "SIMPAC",
    market: "KOSPI",
    aliases: [],
  });
});

// Updated from "still resolves a direct 6 digit stock code that matches no
// master name": 005930 IS in the bundled master (삼성전자), so using the
// default stocks would now return the real entry. Pass an empty array to
// isolate the "not in master" echo path — the same isolation pattern used for
// the alphanumeric test.
test("resolveStock still resolves a direct 6 digit stock code that matches no master entry", () => {
  assert.deepEqual(resolveStock("005930", []), {
    code: "005930",
    name: "005930",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock still resolves a direct alphanumeric ETN code that matches no master name", () => {
  // "Q570121" is a real ETN's own code in the bundled master, so passing the
  // default stocks would now return the real entry (the #158 fix). Pass an
  // empty array to isolate the fallback path: when no master entry exists for
  // a code-shaped input, resolveStock must still echo the code back as UNKNOWN
  // rather than throwing, proving the code-echo fallback was not removed.
  assert.deepEqual(resolveStock("Q570121", []), {
    code: "Q570121",
    name: "Q570121",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock resolves a fund's Korean name to its real 9 char code, unaffected by the shortcut reorder", () => {
  // This resolves the fund by its Korean NAME, so Step 1's code short-circuit
  // misses and Step 2 (name/alias matching) answers. It pins that the
  // reordering did not disturb matching for names containing parentheses.
  // The 9-char CODE input itself is NOT name/alias-only: since #174 Step 1 is
  // length-agnostic, "F70100026" resolves directly — see the
  // "9 char fund code ... present in master" test below.
  assert.deepEqual(resolveStock("한투글로벌넥스트웨이브1(A)"), {
    code: "F70100026",
    name: "한투글로벌넥스트웨이브1(A)",
    market: "KOSPI",
    aliases: [],
  });
});

test("resolveStock does not throw when a caller-supplied stock entry is missing a name", () => {
  // resolveStock(stockName, stocks) accepts a caller-supplied array, so a
  // stock entry without a `name` property is reachable even though the
  // bundled master never omits one. The old `stock.name.toUpperCase()` (no
  // `aliases`-style Array.isArray guard) threw a TypeError here; this pins
  // that a missing `name` is defended the same way a missing/non-array
  // `aliases` already is.
  const stocks = [{ code: "999999", aliases: ["009160"] }];
  assert.deepEqual(resolveStock("SIMPAC", stocks), {
    code: "SIMPAC",
    name: "SIMPAC",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock resolves alphanumeric ETN code directly to its master name and market", () => {
  // Issue #158: code-shaped input that matches stock.code (not stock.name) must
  // return the real name and market from the master, not echo back as UNKNOWN.
  assert.deepEqual(resolveStock("Q570121", STOCKS_WITH_Q570121), {
    code: "Q570121",
    name: "신한 레버리지 금선물 ETN(H)",
    market: "KOSDAQ",
    aliases: [],
  });
});

test("resolveStock resolves alphanumeric ETN code case-insensitively via stock.code match", () => {
  // Lowercase input must match the master entry's code field case-insensitively.
  assert.deepEqual(resolveStock("q570121", STOCKS_WITH_Q570121), {
    code: "Q570121",
    name: "신한 레버리지 금선물 ETN(H)",
    market: "KOSDAQ",
    aliases: [],
  });
});

test("resolveStock echoes back alphanumeric code as UNKNOWN when it is absent from the master", () => {
  // Regression guard: a code-shaped input not present in the caller-supplied
  // stocks array must still fall through to the echo path, returning UNKNOWN.
  assert.deepEqual(resolveStock("Z999999", STOCKS_WITH_Q570121), {
    code: "Z999999",
    name: "Z999999",
    market: "UNKNOWN",
    aliases: [],
  });
});

// 🟡1: numeric codes now return the real name/market when found in the master.
test("resolveStock resolves a numeric stock code to its real name and market when present in master", () => {
  assert.deepEqual(
    resolveStock("005930", [{ code: "005930", name: "삼성전자", market: "KOSPI", aliases: [] }]),
    { code: "005930", name: "삼성전자", market: "KOSPI", aliases: [] },
  );
});

// 🔵2: 9-char fund codes are handled by the code short-circuit (no special
// pattern needed), and the real name/market is returned.
test("resolveStock resolves a 9 char fund code to its real name and market when present in master", () => {
  assert.deepEqual(
    resolveStock("F70100026", [{ code: "F70100026", name: "한투글로벌넥스트웨이브1(A)", market: "KOSPI", aliases: [] }]),
    { code: "F70100026", name: "한투글로벌넥스트웨이브1(A)", market: "KOSPI", aliases: [] },
  );
});

// 🟡2: when a code equals another entry's name, code short-circuit fires
// first so the lookup is unambiguous — no "모호합니다" error is thrown.
test("resolveStock resolves by code first when the input matches both a code and a different entry's name", () => {
  // "AAA111" is both the code of entry A and the name of entry B.
  // The code short-circuit must return entry A without throwing ambiguity.
  const stocks = [
    { code: "AAA111", name: "Entry A", market: "KOSPI", aliases: [] },
    { code: "BBB222", name: "AAA111", market: "KOSDAQ", aliases: [] },
  ];
  assert.deepEqual(resolveStock("AAA111", stocks), {
    code: "AAA111",
    name: "Entry A",
    market: "KOSPI",
    aliases: [],
  });
});

// Case-insensitive code matching: lowercase input matches uppercase code.
test("resolveStock matches numeric code case-insensitively (lowercase input)", () => {
  assert.deepEqual(
    resolveStock("f70100026", [{ code: "F70100026", name: "한투글로벌넥스트웨이브1(A)", market: "KOSPI", aliases: [] }]),
    { code: "F70100026", name: "한투글로벌넥스트웨이브1(A)", market: "KOSPI", aliases: [] },
  );
});

test("exactly 3 master stock names match the code-shaped pattern, and 0 aliases do", () => {
  // This is the issue's own acceptance query, turned into a regression test.
  // If a future stocks.json update adds or removes a name/alias matching
  // CODE_SHAPE_PATTERN, this test documents the change and forces a
  // conscious re-check of the Step 3 echo's position (it must stay behind
  // name/alias matching) rather than a silent reintroduction of the
  // shadowing bug for a new listing. It is also a partial signal for backend/
  // stock_code.py:_looks_like_stock_code — that helper is safe only while every
  // code-shaped master name is digit-free, but CODE_SHAPE_PATTERN only covers
  // 6-7 char shapes and can't catch a digit-bearing 9-char name. The full
  // 6/7/9 range is covered by backend/tests/test_stock_code.py::
  // test_no_master_name_is_shadowed_by_looks_like_stock_code.
  // Uses the implementation's own exported pattern (rather than a hardcoded
  // copy) so this guard can never drift out of sync with what resolveStock
  // actually treats as code-shaped.
  const stocks = getDefaultStocks();
  const codeShapePattern = CODE_SHAPE_PATTERN;

  const nameMatches = stocks.filter((stock) => codeShapePattern.test(stock.name));
  assert.deepEqual(
    nameMatches.map((stock) => [stock.name, stock.code, stock.market]).sort(),
    [
      ["INVENI", "015360", "KOSPI"],
      ["SIMPAC", "009160", "KOSPI"],
      ["WISCOM", "024070", "KOSPI"],
    ],
  );

  let aliasMatchCount = 0;
  for (const stock of stocks) {
    const aliases = Array.isArray(stock.aliases) ? stock.aliases : [];
    aliasMatchCount += aliases.filter((alias) => codeShapePattern.test(alias)).length;
  }
  assert.equal(aliasMatchCount, 0);
});
