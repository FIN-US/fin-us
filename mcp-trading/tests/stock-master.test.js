import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { clearStocksCache, DEFAULT_STOCKS_PATH, getDefaultStocks, resolveStock } from "../stock-master.js";

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

// Issue #150: the code-shaped shortcut (`/^[A-Z0-9]{6,7}$/i`) used to run
// before exact name/alias matching, so a listed company whose registered
// name happens to be a 6-7 char alphanumeric string was shadowed by its own
// name and echoed back as an UNKNOWN "code". These three tests catch a
// regression to that ordering: if the shortcut is moved back in front of
// name/alias matching, each of these would fail by returning
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

test("resolveStock still resolves a direct 6 digit stock code that matches no master name", () => {
  // Guards the shortcut's real purpose: numeric codes must keep working
  // without ever touching the master (see the read-count test below), even
  // after reordering the checks.
  assert.deepEqual(resolveStock("005930"), {
    code: "005930",
    name: "005930",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock still resolves a direct alphanumeric ETN code that matches no master name", () => {
  // "Q570121" is a real ETN's own code, distinct from resolving it by name
  // (covered above). It must still fall through to the code shortcut once
  // name/alias matching finds nothing, proving the reorder didn't remove the
  // fallback entirely.
  assert.deepEqual(resolveStock("Q570121"), {
    code: "Q570121",
    name: "Q570121",
    market: "UNKNOWN",
    aliases: [],
  });
});

test("resolveStock resolves a fund's Korean name to its real 9 char code, unaffected by the shortcut reorder", () => {
  // 9 char fund codes (e.g. F70100026) fall outside the shortcut's {6,7}
  // pattern entirely and can only be reached via name/alias matching. This
  // pins that the reordering did not disturb matching for names containing
  // parentheses or codes longer than the shortcut's range.
  assert.deepEqual(resolveStock("한투글로벌넥스트웨이브1(A)"), {
    code: "F70100026",
    name: "한투글로벌넥스트웨이브1(A)",
    market: "KOSPI",
    aliases: [],
  });
});

test("exactly 3 master stock names match the code-shaped pattern, and 0 aliases do", () => {
  // This is the issue's own acceptance query, turned into a regression test.
  // If a future stocks.json update adds or removes a name/alias matching
  // /^[A-Za-z0-9]{6,7}$/, this test documents the change and forces a
  // conscious re-check of the shortcut-ordering fix rather than a silent
  // reintroduction of the shadowing bug for a new listing.
  const stocks = getDefaultStocks();
  const codeShapePattern = /^[A-Za-z0-9]{6,7}$/;

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
