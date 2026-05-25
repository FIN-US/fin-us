import assert from "node:assert/strict";
import path from "node:path";
import { describe, it } from "node:test";

import {
  buildTokenCachePath,
  kisCredentialSuffix,
} from "../lib/token-cache-path.js";

const REAL = {
  url: "https://openapi.koreainvestment.com:9443",
  apiKey: "AAA",
  accountNo: "12345678-01",
};

const PAPER = {
  url: "https://openapivts.koreainvestment.com:29443",
  apiKey: "AAA",
  accountNo: "12345678-01",
};

const OTHER_ACCOUNT = { ...REAL, accountNo: "99999999-99" };

describe("kisCredentialSuffix", () => {
  it("returns a stable 16-char hex digest", () => {
    const a = kisCredentialSuffix(REAL);
    const b = kisCredentialSuffix(REAL);
    assert.equal(a, b);
    assert.match(a, /^[0-9a-f]{16}$/);
  });

  it("differs between real/paper KIS endpoints", () => {
    assert.notEqual(kisCredentialSuffix(REAL), kisCredentialSuffix(PAPER));
  });

  it("differs between accounts on the same endpoint", () => {
    assert.notEqual(kisCredentialSuffix(REAL), kisCredentialSuffix(OTHER_ACCOUNT));
  });

  it("treats missing fields as empty strings without throwing", () => {
    assert.match(kisCredentialSuffix({}), /^[0-9a-f]{16}$/);
    assert.match(kisCredentialSuffix(), /^[0-9a-f]{16}$/);
  });
});

describe("buildTokenCachePath", () => {
  const fallbackDir = "/tmp/finus-state";

  it("falls back to fallbackDir with a credential-keyed filename when explicit is empty", () => {
    const cachePath = buildTokenCachePath({
      ...REAL,
      explicit: "",
      fallbackDir,
    });
    assert.equal(path.dirname(cachePath), fallbackDir);
    assert.match(path.basename(cachePath), /^kis-token-cache-[0-9a-f]{16}\.json$/);
  });

  it("injects the credential suffix into an explicit file path while preserving its directory", () => {
    const explicit = "/app/.state/kis-token-cache.json";
    const cachePath = buildTokenCachePath({
      ...REAL,
      explicit,
      fallbackDir,
    });
    assert.equal(path.dirname(cachePath), "/app/.state");
    assert.match(path.basename(cachePath), /^kis-token-cache-[0-9a-f]{16}\.json$/);
  });

  it("treats explicit paths without an extension as a directory", () => {
    const explicit = "/var/cache/finus";
    const cachePath = buildTokenCachePath({
      ...REAL,
      explicit,
      fallbackDir,
    });
    assert.equal(path.dirname(cachePath), explicit);
    assert.match(path.basename(cachePath), /^kis-token-cache-[0-9a-f]{16}\.json$/);
  });

  it("produces different paths for real vs paper accounts even with the same explicit base", () => {
    const explicit = "/app/.state/kis-token-cache.json";
    const real = buildTokenCachePath({ ...REAL, explicit, fallbackDir });
    const paper = buildTokenCachePath({ ...PAPER, explicit, fallbackDir });
    assert.notEqual(real, paper);
    assert.equal(path.dirname(real), path.dirname(paper));
  });
});
