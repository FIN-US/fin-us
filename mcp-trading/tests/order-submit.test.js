import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import util from "node:util";
import { DuplicateOrderError, OrderDedupStore } from "../order-dedup.js";
import { submitOrder } from "../order-submit.js";

// #161: markSucceeded (ledger bookkeeping) sitting inside the same try as the KIS call
// used to make a filesystem write failure look identical to an order failure, deleting
// the duplicate guard for an order KIS had already accepted. submitOrder() (order-submit.js)
// separates "did KIS accept the order" from "did we finish recording it" so a ledger
// write failure can never undo an accepted order.
//
// The release() allowlist recognizes three flags kisOrderPost (kis-client.js) can attach to a
// thrown error: kisOrderRejected (KIS said rt_cd !== "0" — nothing was submitted),
// kisOrderNotSubmitted (token/hashkey pre-flight failed before the order POST ever went
// out — also nothing was submitted), and kisOrderSubmittedMaybe (the order POST itself
// failed — submission status unknown). Only the first two release; the third, and any
// unrecognized error, hold the guard (fail-closed).

function tempDedupLedgerPath(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "finus-submit-order-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 }));
  return path.join(dir, "ledger.json");
}

// Mutation this catches: moving markSucceeded back inside the try (or letting any
// exception from it flow into the shared catch) so that a ledger write failure is
// treated the same as an order failure — release() gets called and/or the success
// result never reaches the caller. This exact scenario is what #161 reported: KIS
// accepted the order, but a later fs error made it look like the order itself failed.
// Confirmed to fail on unmodified origin/main: there, markSucceeded runs inside the
// try, its exception has neither kisOrderSubmittedMaybe nor kisOrderRejected, so
// `!undefined || undefined` is true and release() deletes the guard.
test("submitOrder: a markSucceeded (ledger write) failure after a successful KIS order does not release the dedup guard, and the order result still reaches the caller", async (t) => {
  const store = new OrderDedupStore({ filePath: tempDedupLedgerPath(t), ttlMs: 60_000, now: () => 1_000 });
  const dedupKey = "order-key-mark-succeeded-throws";
  store.reserve(dedupKey, { pathname: "/uapi/domestic-stock/v1/trading/order-cash", trId: "TTTC0012U", body: {} });

  const releaseSpy = t.mock.method(store, "release");
  t.mock.method(store, "markSucceeded", () => {
    throw new Error("ENOSPC: no space left on device, write");
  });
  t.mock.method(console, "error", () => {});

  const kisResponse = { rt_cd: "0", output: { ODNO: "0000001234" } };
  const data = await submitOrder({
    dedupStore: store,
    dedupKey,
    submit: async () => kisResponse,
  });

  assert.equal(data, kisResponse, "the KIS success response must still reach the caller");
  assert.equal(releaseSpy.mock.callCount(), 0, "release() must not run when only bookkeeping failed");
});

// Mutation this catches: any change that deletes or expires the ledger entry when
// markSucceeded fails (e.g. calling release() unconditionally in a finally block, or
// swallowing the error by writing an empty/succeeded-but-then-cleared entry). If the
// entry disappeared, this reserve() would succeed instead of throwing, and a retried
// order would go through — reproducing the duplicate-order bug from #161.
//
// Caveat noted for the PR body, not fixed here: markSucceeded failing leaves the
// original reservedAt/expiresAt untouched, so this only blocks a retry for whatever
// remains of the original TTL window (order-dedup.js:80). A retry after the window
// fully elapses still duplicates — that is pre-existing dedup design, not new here.
test("submitOrder: after a markSucceeded failure, the ledger entry stays in place (in_flight) so a retried reserve() is still blocked as a duplicate within the TTL window", async (t) => {
  const store = new OrderDedupStore({ filePath: tempDedupLedgerPath(t), ttlMs: 60_000, now: () => 1_000 });
  const dedupKey = "order-key-retry-blocked";
  const request = { pathname: "/uapi/domestic-stock/v1/trading/order-cash", trId: "TTTC0012U", body: {} };
  store.reserve(dedupKey, request);

  t.mock.method(store, "markSucceeded", () => {
    throw new Error("EACCES: permission denied, open '/state/order-dedup.json'");
  });
  t.mock.method(console, "error", () => {});

  await submitOrder({
    dedupStore: store,
    dedupKey,
    submit: async () => ({ rt_cd: "0", output: { ODNO: "0000005678" } }),
  });

  assert.throws(
    () => store.reserve(dedupKey, request),
    DuplicateOrderError,
    "the reservation must still be present, blocking a retry of the same order",
  );
});

// Mutation this catches: dropping the `error.kisOrderRejected === true` check (e.g.
// releasing unconditionally, or never releasing at all). This is unchanged behavior
// from before #161: KIS explicitly said rt_cd !== "0", so nothing was submitted and
// the guard must be released for a legitimate retry.
test("submitOrder: releases the dedup guard when KIS rejects the order (rt_cd !== \"0\") — unchanged behavior", async (t) => {
  const releaseCalls = [];
  const store = {
    release(key) {
      releaseCalls.push(key);
    },
    markSucceeded() {
      throw new Error("markSucceeded should not run when submit() throws");
    },
  };

  const rejected = new Error("KIS API 오류: 주문가능금액을 초과하였습니다.");
  rejected.kisOrderRejected = true;

  await assert.rejects(
    () => submitOrder({ dedupStore: store, dedupKey: "order-key-rejected", submit: async () => { throw rejected; } }),
    (err) => err === rejected,
  );
  assert.deepEqual(releaseCalls, ["order-key-rejected"]);
});

// Mutation this catches: dropping the `error.kisOrderNotSubmitted === true` allowlist
// entry added after review of #161. getAccessToken() and createKisHashKey() both run
// before the order POST in kisOrderPost (kis-client.js); a failure there — expired credentials,
// a rate-limited token endpoint, a cold token cache after a container restart — means
// the order was never sent. Without this class, that dedup key sits reserved for the
// full TTL and DuplicateOrderError tells the user to "check the order shortly" for an
// order that provably never existed, with no escape except changing quantity/price.
test("submitOrder: releases the dedup guard when the KIS POST never went out (kisOrderNotSubmitted, e.g. token/hashkey pre-flight failure)", async (t) => {
  const releaseCalls = [];
  const store = {
    release(key) {
      releaseCalls.push(key);
    },
    markSucceeded() {
      throw new Error("markSucceeded should not run when submit() throws");
    },
  };

  const notSubmitted = new Error("Access Token 발급 실패: EGW00133 초당 거래건수를 초과하였습니다.");
  notSubmitted.kisOrderNotSubmitted = true;

  await assert.rejects(
    () => submitOrder({ dedupStore: store, dedupKey: "order-key-not-submitted", submit: async () => { throw notSubmitted; } }),
    (err) => err === notSubmitted,
  );
  assert.deepEqual(releaseCalls, ["order-key-not-submitted"]);
});

// Mutation this catches: releasing whenever `kisOrderSubmittedMaybe` is absent, which
// is exactly the old `!error.kisOrderSubmittedMaybe` fail-open logic this issue removes.
// Unchanged behavior from before #161: the axios POST itself failed, so whether KIS
// received the order is unknown — releasing here could let a duplicate through while
// the original order might still be live, so the guard must stay (fail-closed).
test("submitOrder: does not release the dedup guard when the axios POST fails and submission status is unknown — unchanged behavior", async (t) => {
  const releaseCalls = [];
  const store = {
    release(key) {
      releaseCalls.push(key);
    },
  };

  const submitFailure = new Error("connect ETIMEDOUT");
  submitFailure.kisOrderSubmittedMaybe = true;

  await assert.rejects(
    () => submitOrder({ dedupStore: store, dedupKey: "order-key-submit-unknown", submit: async () => { throw submitFailure; } }),
    (err) => err === submitFailure,
  );
  assert.deepEqual(releaseCalls, [], "an unknown submission outcome must not release the guard");
});

// Mutation this catches: reverting the allowlist inversion, or widening it beyond the
// two named flags. This is the deliberate behavior flip from #161: an error carrying
// none of kisOrderRejected/kisOrderNotSubmitted/kisOrderSubmittedMaybe (e.g. a bug in
// application code unrelated to the KIS call) must default to NOT releasing — the old
// code's `!error.kisOrderSubmittedMaybe` fell through to release() here instead.
test("submitOrder: does not release the dedup guard for an error with none of the three recognized flags (inversion of pre-#161 behavior, which released here)", async (t) => {
  const releaseCalls = [];
  const store = {
    release(key) {
      releaseCalls.push(key);
    },
  };

  const unrecognizedError = new TypeError("Cannot read properties of undefined (reading 'output')");

  await assert.rejects(
    () => submitOrder({ dedupStore: store, dedupKey: "order-key-unrecognized", submit: async () => { throw unrecognizedError; } }),
    (err) => err === unrecognizedError,
  );
  assert.deepEqual(releaseCalls, [], "an unrecognized error must default to fail-closed (no release)");
});

// Mutation this catches: letting release()'s own exception propagate out of the catch
// block unguarded, which would replace the original KIS rejection error and destroy
// the diagnostic (per issue's "also look at" section on release()).
test("submitOrder: a release() failure during a KIS rejection does not replace the original KIS error", async (t) => {
  const consoleError = t.mock.method(console, "error", () => {});
  const store = {
    release() {
      throw new Error("EIO: i/o error, write");
    },
  };

  const rejected = new Error("KIS API 오류: 모의투자 장운영시간이 아닙니다.");
  rejected.kisOrderRejected = true;

  await assert.rejects(
    () => submitOrder({ dedupStore: store, dedupKey: "order-key-release-fails", submit: async () => { throw rejected; } }),
    (err) => err === rejected,
    "the original KIS rejection must still be the thrown error, not release()'s fs error",
  );
  const logged = consoleError.mock.calls.map((call) => util.format(...call.arguments)).join("\n");
  assert.match(logged, /EIO: i\/o error, write/);
});

// Mutation this catches: dereferencing `error.message` unguarded in the markSucceeded
// catch block. A ledger write path could reject with a non-Error value (e.g. a plain
// string thrown by some intermediary); `error.message` on a string is undefined, and on
// null/undefined it throws a TypeError from inside the one block whose whole contract
// is "must never throw", which would destroy the already-accepted order result.
test("submitOrder: a non-Error thrown by markSucceeded (e.g. a plain string) does not crash, and the order result still reaches the caller", async (t) => {
  const consoleError = t.mock.method(console, "error", () => {});
  const store = {
    markSucceeded() {
      throw "disk full"; // eslint-disable-line no-throw-literal -- simulating a non-Error rejection
    },
  };

  const kisResponse = { rt_cd: "0", output: { ODNO: "0000009999" } };
  const data = await submitOrder({ dedupStore: store, dedupKey: "order-key-non-error-mark", submit: async () => kisResponse });

  assert.equal(data, kisResponse, "the KIS success response must still reach the caller");
  const logged = consoleError.mock.calls.map((call) => util.format(...call.arguments)).join("\n");
  assert.match(logged, /disk full/);
});

// Mutation this catches: dereferencing `error.kisOrderRejected` or `releaseError.message`
// unguarded when submit() itself rejects with a non-Error value (e.g. `throw null`,
// which real code should never do, but a defensive guard must still not crash on it).
test("submitOrder: a non-Error value thrown by submit() (e.g. null) does not crash and defaults to not releasing", async (t) => {
  const releaseCalls = [];
  const store = {
    release(key) {
      releaseCalls.push(key);
    },
  };

  await assert.rejects(
    () => submitOrder({ dedupStore: store, dedupKey: "order-key-null-throw", submit: async () => { throw null; } }), // eslint-disable-line no-throw-literal
    (err) => err === null,
  );
  assert.deepEqual(releaseCalls, [], "a non-Error rejection must default to fail-closed (no release)");
});
