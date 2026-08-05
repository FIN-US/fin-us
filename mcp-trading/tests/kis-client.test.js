import assert from "node:assert/strict";
import test from "node:test";
import { kisOrderPost } from "../kis-client.js";

// #163: kisOrderPost (formerly kisPost, index.js) tags a thrown error with exactly one of
// three flags depending on where in the call the failure happened. submitOrder()
// (order-submit.js:17) reads these flags to decide whether the duplicate-order dedup
// guard is safe to release. A wrong flag here means either a duplicate real order goes
// through, or a legitimate order stays blocked for the full TTL. Before this file, kisPost
// was not exported and nothing exercised its production side — only order-submit.js's
// consumption of pre-tagged errors was tested (order.test.js).
//
// Confirmed against origin/main: widening the pre-flight try in the original kisPost so
// it also wraps the order POST (tagging a genuine post-submission failure as
// kisOrderNotSubmitted instead of kisOrderSubmittedMaybe) left the full suite green
// (72/72) — nothing pinned this boundary. The tests below pin each of the three
// flag-producing branches (including the hashkey-response-without-HASH case, which is
// also a kisOrderNotSubmitted producer), the required-dependency guard added for
// index.js's wiring, and the typeof-guard on each catch block.

function fakeAxios({ post }) {
  return { post };
}

test("kisOrderPost: pre-flight failure (getAccessToken) tags kisOrderNotSubmitted, not kisOrderSubmittedMaybe", async () => {
  const preFlightError = new Error("Access Token 발급 실패: EGW00133 초당 거래건수를 초과하였습니다.");
  const getAccessToken = async () => {
    throw preFlightError;
  };
  // The order POST itself must never be reached when pre-flight fails first.
  const kisAxios = fakeAxios({
    post: async () => {
      throw new Error("order POST must not be called when pre-flight fails");
    },
  });

  await assert.rejects(
    () => kisOrderPost({
      kisAxios,
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      appKey: "test-app-key",
      appSecret: "test-app-secret",
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0012U",
      body: {},
      useHashKey: false,
      getAccessToken,
    }),
    (err) => {
      assert.equal(err, preFlightError);
      assert.equal(err.kisOrderNotSubmitted, true);
      assert.equal(err.kisOrderSubmittedMaybe, undefined);
      return true;
    },
  );
});

test("kisOrderPost: pre-flight failure (createKisHashKey via the /uapi/hashkey POST) also tags kisOrderNotSubmitted, not kisOrderSubmittedMaybe", async () => {
  const getAccessToken = async () => "test-token";
  const hashKeyError = new Error("connect ETIMEDOUT");
  let orderPostCalled = false;
  const kisAxios = fakeAxios({
    post: async (url) => {
      if (url.endsWith("/uapi/hashkey")) {
        throw hashKeyError;
      }
      orderPostCalled = true;
      throw new Error("order POST must not be called when hashkey pre-flight fails");
    },
  });

  await assert.rejects(
    () => kisOrderPost({
      kisAxios,
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      appKey: "test-app-key",
      appSecret: "test-app-secret",
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0012U",
      body: {},
      useHashKey: true,
      getAccessToken,
    }),
    (err) => {
      assert.equal(err, hashKeyError);
      assert.equal(err.kisOrderNotSubmitted, true);
      assert.equal(err.kisOrderSubmittedMaybe, undefined);
      return true;
    },
  );
  assert.equal(orderPostCalled, false, "the order POST must never be reached when the hashkey pre-flight fails");
});

// #163 review follow-up (row 4 of the flag table): createKisHashKey's own guard
// (`if (!hashKey) throw ...`) is the one pre-flight branch that was still unpinned.
// Confirmed to redden this test: neutralizing that guard (`if (!hashKey)` -> `if (false)`)
// in kis-client.js leaves the whole suite green, because nothing else notices that
// headers.hashkey ends up undefined — the order POST goes out anyway with a malformed
// header, KIS rejects it, and that misclassifies as kisOrderRejected (which releases the
// dedup guard), reviving the duplicate-order direction #163 is about.
test("kisOrderPost: hashkey pre-flight succeeds (200) but the response body has no HASH field tags kisOrderNotSubmitted, not kisOrderSubmittedMaybe, and the order POST is never sent", async () => {
  const getAccessToken = async () => "test-token";
  let orderPostCalled = false;
  const kisAxios = fakeAxios({
    post: async (url) => {
      if (url.endsWith("/uapi/hashkey")) {
        return { data: {} }; // 200 response, but the body carries no HASH field
      }
      orderPostCalled = true;
      throw new Error("order POST must not be called when the hashkey response is malformed");
    },
  });

  await assert.rejects(
    () => kisOrderPost({
      kisAxios,
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      appKey: "test-app-key",
      appSecret: "test-app-secret",
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0012U",
      body: {},
      useHashKey: true,
      getAccessToken,
    }),
    (err) => {
      assert.match(err.message, /KIS hashkey 발급 실패/);
      assert.equal(err.kisOrderNotSubmitted, true);
      assert.equal(err.kisOrderSubmittedMaybe, undefined);
      return true;
    },
  );
  assert.equal(orderPostCalled, false, "the order POST must never be reached when the hashkey response has no HASH");
});

test("kisOrderPost: order POST throws tags kisOrderSubmittedMaybe, not kisOrderNotSubmitted", async () => {
  const getAccessToken = async () => "test-token";
  const submitError = new Error("connect ETIMEDOUT");
  const kisAxios = fakeAxios({
    post: async () => {
      // useHashKey is false here, so the only POST this client makes is the order
      // submission itself.
      throw submitError;
    },
  });

  await assert.rejects(
    () => kisOrderPost({
      kisAxios,
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      appKey: "test-app-key",
      appSecret: "test-app-secret",
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0012U",
      body: {},
      useHashKey: false,
      getAccessToken,
    }),
    (err) => {
      assert.equal(err, submitError);
      assert.equal(err.kisOrderSubmittedMaybe, true);
      assert.equal(err.kisOrderNotSubmitted, undefined);
      return true;
    },
  );
});

test('kisOrderPost: rt_cd !== "0" tags kisOrderRejected, and neither of the other two flags', async () => {
  const getAccessToken = async () => "test-token";
  const kisAxios = fakeAxios({
    post: async () => ({
      data: { rt_cd: "1", msg1: "주문가능금액을 초과하였습니다.", msg_cd: "APBK0952" },
    }),
  });

  await assert.rejects(
    () => kisOrderPost({
      kisAxios,
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      appKey: "test-app-key",
      appSecret: "test-app-secret",
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0012U",
      body: {},
      useHashKey: false,
      getAccessToken,
    }),
    (err) => {
      assert.match(err.message, /주문가능금액을 초과하였습니다\./);
      assert.equal(err.kisOrderRejected, true);
      assert.equal(err.kisOrderNotSubmitted, undefined);
      assert.equal(err.kisOrderSubmittedMaybe, undefined);
      return true;
    },
  );
});

test("kisOrderPost: success (rt_cd === \"0\") returns the KIS response data untagged", async () => {
  const getAccessToken = async () => "test-token";
  const responseData = { rt_cd: "0", output: { ODNO: "0000001234" }, msg1: "주문이 완료되었습니다" };
  const kisAxios = fakeAxios({
    post: async () => ({ data: responseData }),
  });

  const data = await kisOrderPost({
    kisAxios,
    kisUrl: "https://openapivts.koreainvestment.com:29443",
    appKey: "test-app-key",
    appSecret: "test-app-secret",
    pathname: "/uapi/domestic-stock/v1/trading/order-cash",
    trId: "VTTC0012U",
    body: {},
    useHashKey: false,
    getAccessToken,
  });

  assert.deepEqual(data, responseData);
});

test("kisOrderPost: sends bearer token, appkey/appsecret, tr_id, and hashkey headers to the order pathname built from kisUrl, fetching the token before the hashkey POST before the order POST", async () => {
  // Call order matters here, not just header content: if the hashkey POST fired before
  // getAccessToken() (or before whatever pre-flight credential check a real caller does),
  // it would send placeholder/unready credentials to KIS ahead of schedule.
  const callOrder = [];
  const getAccessToken = async () => {
    callOrder.push("getAccessToken");
    return "captured-token";
  };
  let hashKeyCall = null;
  let orderCall = null;
  const kisAxios = fakeAxios({
    post: async (url, body, config) => {
      if (url.endsWith("/uapi/hashkey")) {
        callOrder.push("hashkey");
        hashKeyCall = { url, body, config };
        return { data: { HASH: "captured-hash" } };
      }
      callOrder.push("order");
      orderCall = { url, body, config };
      return { data: { rt_cd: "0", output: {} } };
    },
  });

  await kisOrderPost({
    kisAxios,
    kisUrl: "https://openapivts.koreainvestment.com:29443",
    appKey: "captured-app-key",
    appSecret: "captured-app-secret",
    pathname: "/uapi/domestic-stock/v1/trading/order-cash",
    trId: "VTTC0012U",
    body: { PDNO: "005930" },
    useHashKey: true,
    getAccessToken,
  });

  assert.deepEqual(callOrder, ["getAccessToken", "hashkey", "order"]);
  assert.equal(hashKeyCall.url, "https://openapivts.koreainvestment.com:29443/uapi/hashkey");
  assert.equal(orderCall.url, "https://openapivts.koreainvestment.com:29443/uapi/domestic-stock/v1/trading/order-cash");
  assert.deepEqual(orderCall.body, { PDNO: "005930" });
  assert.equal(orderCall.config.headers.authorization, "Bearer captured-token");
  assert.equal(orderCall.config.headers.appkey, "captured-app-key");
  assert.equal(orderCall.config.headers.appsecret, "captured-app-secret");
  assert.equal(orderCall.config.headers.tr_id, "VTTC0012U");
  assert.equal(orderCall.config.headers.hashkey, "captured-hash");
});

// #163 review follow-up: kisOrderPost turned nine implicit index.js module-scope reads
// into nine explicit call-site parameters. A wiring mistake at the one production call
// site (index.js) — e.g. dropping `getAccessToken` from the object literal — used to
// surface only as a generic TypeError from deep inside the pre-flight try, which still
// gets classified kisOrderNotSubmitted (fails closed, safe direction) but gives no hint
// what actually broke. This guard converts that into a named error identifying exactly
// which dependency is missing, and protects every future caller, not just index.js's one
// call site.
test("kisOrderPost: throws a named error identifying the missing dependency when a required argument is not supplied, before making any HTTP call", async () => {
  const validArgs = {
    kisAxios: fakeAxios({ post: async () => { throw new Error("no HTTP call should happen"); } }),
    kisUrl: "https://openapivts.koreainvestment.com:29443",
    appKey: "test-app-key",
    appSecret: "test-app-secret",
    pathname: "/uapi/domestic-stock/v1/trading/order-cash",
    trId: "VTTC0012U",
    body: {},
    useHashKey: false,
    getAccessToken: async () => "test-token",
  };

  for (const missing of ["kisAxios", "kisUrl", "appKey", "appSecret", "pathname", "trId", "body", "useHashKey", "getAccessToken"]) {
    const args = { ...validArgs, [missing]: undefined };
    await assert.rejects(
      () => kisOrderPost(args),
      (err) => {
        assert.match(err.message, new RegExp(`'${missing}'`));
        assert.equal(err.kisOrderNotSubmitted, undefined, "the dependency guard must fire before pre-flight, not be classified as a KIS failure");
        assert.equal(err.kisOrderSubmittedMaybe, undefined);
        assert.equal(err.kisOrderRejected, undefined);
        return true;
      },
      `omitting '${missing}' should throw a named error mentioning it`,
    );
  }
});

// The typeof-error guard on each catch block (kisOrderNotSubmitted and
// kisOrderSubmittedMaybe) exists so tagging a caught error never itself throws when the
// error is a primitive (ES modules are strict mode; assigning a property to a string/
// number/null throws TypeError). These two tests give that guard coverage: without it
// (i.e. under an `if (true)` mutation of either guard), a bare-string throw would be
// replaced by a TypeError from the property assignment itself, and these assertions on
// the original string would fail.
test("kisOrderPost: a bare string thrown by getAccessToken propagates unchanged instead of crashing on the tagging assignment", async () => {
  const getAccessToken = async () => {
    throw "token service unavailable"; // eslint-disable-line no-throw-literal -- exercising the primitive-error guard
  };
  const kisAxios = fakeAxios({
    post: async () => { throw new Error("no HTTP call should happen"); },
  });

  await assert.rejects(
    () => kisOrderPost({
      kisAxios,
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      appKey: "test-app-key",
      appSecret: "test-app-secret",
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0012U",
      body: {},
      useHashKey: false,
      getAccessToken,
    }),
    (err) => {
      assert.equal(err, "token service unavailable");
      return true;
    },
  );
});

test("kisOrderPost: a bare string thrown by the order POST propagates unchanged instead of crashing on the tagging assignment", async () => {
  const getAccessToken = async () => "test-token";
  const kisAxios = fakeAxios({
    post: async () => {
      throw "socket hang up"; // eslint-disable-line no-throw-literal -- exercising the primitive-error guard
    },
  });

  await assert.rejects(
    () => kisOrderPost({
      kisAxios,
      kisUrl: "https://openapivts.koreainvestment.com:29443",
      appKey: "test-app-key",
      appSecret: "test-app-secret",
      pathname: "/uapi/domestic-stock/v1/trading/order-cash",
      trId: "VTTC0012U",
      body: {},
      useHashKey: false,
      getAccessToken,
    }),
    (err) => {
      assert.equal(err, "socket hang up");
      return true;
    },
  );
});
