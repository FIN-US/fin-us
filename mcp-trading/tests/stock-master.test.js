import assert from "node:assert/strict";
import test from "node:test";
import { resolveStock } from "../stock-master.js";

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
