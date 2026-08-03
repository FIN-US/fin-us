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
// (72/72) — nothing pinned this boundary. These three tests each target one of the three
// flag-producing branches.

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
  const kisAxios = fakeAxios({
    post: async (url) => {
      if (url.endsWith("/uapi/hashkey")) {
        throw new Error("connect ETIMEDOUT");
      }
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
      assert.equal(err.kisOrderNotSubmitted, true);
      assert.equal(err.kisOrderSubmittedMaybe, undefined);
      return true;
    },
  );
});

test("kisOrderPost: order POST throws tags kisOrderSubmittedMaybe, not kisOrderNotSubmitted", async () => {
  const getAccessToken = async () => "test-token";
  const submitError = new Error("connect ETIMEDOUT");
  const kisAxios = fakeAxios({
    post: async (url) => {
      // getAccessToken/createKisHashKey are injected/skipped here, so the only POST this
      // client makes is the order submission itself.
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

test("kisOrderPost: sends bearer token, appkey/appsecret, tr_id, and hashkey headers to the order pathname built from kisUrl", async () => {
  const getAccessToken = async () => "captured-token";
  let hashKeyCall = null;
  let orderCall = null;
  const kisAxios = fakeAxios({
    post: async (url, body, config) => {
      if (url.endsWith("/uapi/hashkey")) {
        hashKeyCall = { url, body, config };
        return { data: { HASH: "captured-hash" } };
      }
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

  assert.equal(hashKeyCall.url, "https://openapivts.koreainvestment.com:29443/uapi/hashkey");
  assert.equal(orderCall.url, "https://openapivts.koreainvestment.com:29443/uapi/domestic-stock/v1/trading/order-cash");
  assert.deepEqual(orderCall.body, { PDNO: "005930" });
  assert.equal(orderCall.config.headers.authorization, "Bearer captured-token");
  assert.equal(orderCall.config.headers.appkey, "captured-app-key");
  assert.equal(orderCall.config.headers.appsecret, "captured-app-secret");
  assert.equal(orderCall.config.headers.tr_id, "VTTC0012U");
  assert.equal(orderCall.config.headers.hashkey, "captured-hash");
});
