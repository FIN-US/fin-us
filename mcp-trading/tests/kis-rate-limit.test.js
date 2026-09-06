import assert from "node:assert/strict";
import test from "node:test";
import {
  KIS_CLASS_OK,
  KIS_CLASS_OTHER,
  KIS_CLASS_RATE_LIMIT,
  KIS_RATE_LIMIT_MSG_CODES,
  classifyKisError,
  formatKisRequestLog,
  isKisRateLimitError,
  maskLongDigitRuns,
  readKisRequestLogEnv,
} from "../kis-rate-limit.js";

// 이슈 #210: 실계좌 없이는 KIS 유량 제한의 실제 임계치를 잴 수 없다. 여기서 고정하는 것은
// "재려면 반드시 있어야 하는 것" — 유량 제한 판정과, 그 판정을 운영자가 읽을 수 있는 한 줄로
// 만드는 포맷터의 계약이다.

test("EGW00201/EGW00133 msg_cd를 유량 제한으로 분류한다", () => {
  for (const msgCd of KIS_RATE_LIMIT_MSG_CODES) {
    assert.equal(isKisRateLimitError({ msgCd }), true, msgCd);
    assert.equal(classifyKisError({ msgCd }), KIS_CLASS_RATE_LIMIT, msgCd);
  }
  // 목록이 비어 있는 채로 통과하는 상태가 아님을 고정한다.
  assert.deepEqual([...KIS_RATE_LIMIT_MSG_CODES], ["EGW00201", "EGW00133"]);
});

test("msg_cd 대소문자·공백이 섞여도 유량 제한으로 본다", () => {
  assert.equal(isKisRateLimitError({ msgCd: " egw00201 " }), true);
});

test("msg_cd가 없어도 msg1의 '초당 거래건수'로 유량 제한을 잡는다", () => {
  // 공식 msg_cd FAQ에 접근하지 못해 코드 목록이 완전하다는 보장이 없다
  // (docs/issue-138-alnum-stock-code.md:495). 한국어 본문이 두 번째 증거 경로다.
  assert.equal(
    isKisRateLimitError({ msgCd: "EGW99999", msg1: "초당 거래건수를 초과하였습니다." }),
    true,
  );
  assert.equal(isKisRateLimitError({ msg1: "초당 거래건수를 초과하였습니다." }), true);
});

test("유량 제한이 아닌 KIS 오류는 other로 분류한다", () => {
  assert.equal(isKisRateLimitError({ msgCd: "EGW00123", msg1: "기간이 올바르지 않습니다." }), false);
  assert.equal(
    classifyKisError({ msgCd: "EGW00123", msg1: "기간이 올바르지 않습니다." }),
    KIS_CLASS_OTHER,
  );
  assert.equal(isKisRateLimitError(), false);
  assert.equal(isKisRateLimitError({}), false);
});

test("8자리 이상 연속 숫자만 가리고 짧은 숫자는 남긴다", () => {
  // 종목코드 6자리·수량 같은 진짜로 보고 싶은 값은 남아야 한다.
  assert.equal(maskLongDigitRuns("종목 005930 수량 10"), "종목 005930 수량 10");
  assert.equal(maskLongDigitRuns("계좌 50123456"), "계좌 ********");
  assert.equal(maskLongDigitRuns("계좌 5012345601 입니다"), "계좌 ********** 입니다");
  assert.equal(maskLongDigitRuns("1234567"), "1234567");
  assert.equal(maskLongDigitRuns("12345678"), "********");
});

test("성공 응답은 한 줄 ok 로그가 되고 rateLimited가 아니다", () => {
  const { line, classification, rateLimited, msgCd } = formatKisRequestLog({
    trId: "TTTC0081R",
    elapsedMs: 132.6,
    response: {
      status: 200,
      data: { rt_cd: "0", msg_cd: "MCA00000", msg1: "정상처리 되었습니다." },
    },
  });

  assert.equal(classification, KIS_CLASS_OK);
  assert.equal(rateLimited, false);
  assert.equal(msgCd, "MCA00000");
  assert.equal(line.includes("\n"), false, "한 요청은 반드시 한 줄이어야 한다");
  assert.match(line, /^\[kis-req\] /);
  assert.match(line, /tr_id=TTTC0081R/);
  assert.match(line, /elapsed_ms=133/);
  assert.match(line, /http=200/);
  assert.match(line, /rt_cd=0/);
  assert.match(line, /msg_cd=MCA00000/);
  assert.match(line, /class=ok/);
});

test("줄에 시각과 pid가 실려 프로세스 간 요청 겹침을 볼 수 있다", () => {
  // mcp-trading은 도구 호출마다 새로 뜨는 단명 프로세스이고 자식 stderr가 전부 부모
  // stderr 하나로 합쳐진다. 시각·pid가 줄 안에 없으면 "동시에 뜬 별개 프로세스의 요청이
  // 겹쳤는가"(PR #264 리뷰의 계좌 단위 가설)를 합쳐진 로그에서 되짚을 수 없다.
  const { line } = formatKisRequestLog({
    trId: "TTTC8434R",
    elapsedMs: 10,
    pid: 4242,
    now: () => Date.parse("2026-09-06T01:02:03.004Z"),
    response: { status: 200, data: { rt_cd: "0" } },
  });

  assert.match(line, /ts=2026-09-06T01:02:03\.004Z/);
  assert.match(line, /pid=4242/);
});

test("pid·시각을 못 얻어도 줄 모양은 깨지지 않는다", () => {
  const { line } = formatKisRequestLog({
    trId: "TTTC8434R",
    elapsedMs: 10,
    now: () => Number.NaN,
    response: { status: 200, data: { rt_cd: "0" } },
  });

  assert.match(line, /ts=-/);
  assert.match(line, /pid=-/);
  assert.equal(line.includes("\n"), false);
});

test("rt_cd가 0이 아닌 응답은 유량 제한으로 분류되고 msg_cd가 줄에 남는다", () => {
  // index.js:212-213은 msg1이 있으면 msg_cd를 통째로 버린다. 그래서 EGW00201이 지금까지
  // 로그에 한 번도 남지 않았다 — 이 줄이 그 구멍을 메운다.
  const { line, classification, rateLimited, msgCd } = formatKisRequestLog({
    trId: "TTTC8434R",
    elapsedMs: 41,
    response: {
      status: 200,
      data: {
        rt_cd: "1",
        msg_cd: "EGW00201",
        msg1: "초당 거래건수를 초과하였습니다.",
      },
    },
  });

  assert.equal(classification, KIS_CLASS_RATE_LIMIT);
  assert.equal(rateLimited, true);
  assert.equal(msgCd, "EGW00201");
  assert.match(line, /msg_cd=EGW00201/);
  assert.match(line, /class=rate_limit/);
  assert.match(line, /msg1="초당 거래건수를 초과하였습니다\."/);
});

test("전송 실패(응답 없음)도 한 줄로 남고 axios 에러 코드만 실린다", () => {
  const error = new Error("timeout of 8000ms exceeded");
  error.code = "ECONNABORTED";

  const { line, classification, rateLimited } = formatKisRequestLog({
    trId: "TTTC0081R",
    elapsedMs: 8001,
    error,
  });

  assert.equal(classification, KIS_CLASS_OTHER);
  assert.equal(rateLimited, false);
  assert.match(line, /err=ECONNABORTED/);
  assert.match(line, /http=-/);
  assert.match(line, /msg_cd=-/);
  // error.message는 축적된 문자열이라 무엇이 섞이는지 보장할 수 없어 싣지 않는다.
  assert.equal(line.includes("timeout of 8000ms"), false);
});

test("HTTP 오류 응답의 바디에서도 유량 제한을 잡는다", () => {
  const error = new Error("Request failed with status code 500");
  error.response = {
    status: 500,
    data: { rt_cd: "1", msg_cd: "EGW00133", msg1: "초당 거래건수를 초과하였습니다." },
  };

  const { line, rateLimited } = formatKisRequestLog({ trId: "TTTC8494R", elapsedMs: 12, error });

  assert.equal(rateLimited, true);
  assert.match(line, /http=500/);
  assert.match(line, /msg_cd=EGW00133/);
});

test("이상한 tr_id·elapsedMs는 로그 줄을 깨뜨리지 않고 '-'로 눕는다", () => {
  const { line } = formatKisRequestLog({
    trId: "bad id\nwith newline",
    elapsedMs: Number.NaN,
    response: { status: "200", data: { rt_cd: "0" } },
  });

  assert.equal(line.includes("\n"), false);
  assert.match(line, /tr_id=-/);
  assert.match(line, /elapsed_ms=-/);
  assert.match(line, /http=-/);
});

test("msg1의 개행은 접히고 긴 문자열은 잘린다", () => {
  const { line } = formatKisRequestLog({
    trId: "TTTC0081R",
    elapsedMs: 1,
    response: { status: 200, data: { rt_cd: "1", msg_cd: "EGW00123", msg1: `앞\n뒤 ${"가".repeat(400)}` } },
  });

  assert.equal(line.includes("\n"), false);
  assert.match(line, /msg1="앞 뒤 가+\.\.\."/);
  assert.ok(line.length < 400, `줄이 상한 없이 늘어나면 안 된다: ${line.length}`);
});

// ---------------------------------------------------------------------------
// 비밀 위생. 이 테스트가 이 모듈의 존재 이유 중 절반이다.
// ---------------------------------------------------------------------------

// 실제 axios 에러의 모양 그대로 만든다 — error.config에 params(CANO)와 headers(appkey,
// appsecret, authorization: Bearer)가 통째로 붙어 있다(balance.js:201-206이 기록한 그대로).
function realisticAxiosError() {
  const error = new Error("Request failed with status code 500");
  error.name = "AxiosError";
  error.code = "ERR_BAD_RESPONSE";
  error.config = {
    url: "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
    method: "get",
    headers: {
      "Content-Type": "application/json",
      authorization: "Bearer eyJhbGciOiJIUzI1NiJ9.SUPERSECRETACCESSTOKEN.sig",
      appkey: "PSxxxxAPPKEYxxxxSECRETVALUE",
      appsecret: "APPSECRETyyyyLONGyyyyVALUEyyyy",
      tr_id: "TTTC0081R",
      custtype: "P",
    },
    params: {
      CANO: "50123456",
      ACNT_PRDT_CD: "01",
      INQR_STRT_DT: "20260903",
    },
  };
  error.request = { _header: "GET /uapi ... authorization: Bearer eyJhbGciOiJIUzI1NiJ9.SUPERSECRETACCESSTOKEN.sig" };
  error.response = {
    status: 500,
    headers: { authorization: "Bearer eyJhbGciOiJIUzI1NiJ9.SUPERSECRETACCESSTOKEN.sig" },
    config: error.config,
    data: { rt_cd: "1", msg_cd: "EGW00201", msg1: "초당 거래건수를 초과하였습니다." },
  };
  return error;
}

test("로그 줄에는 appkey·appsecret·Bearer 토큰·계좌번호가 절대 실리지 않는다", () => {
  const error = realisticAxiosError();
  const { line, rateLimited } = formatKisRequestLog({
    trId: "TTTC0081R",
    elapsedMs: 77,
    error,
  });

  // 판정 자체는 정상적으로 나와야 한다 — "아무것도 안 남겨서 안전"은 통과가 아니다.
  assert.equal(rateLimited, true);
  assert.match(line, /class=rate_limit/);
  assert.match(line, /msg_cd=EGW00201/);

  for (const secret of [
    "SUPERSECRETACCESSTOKEN",
    "eyJhbGciOiJIUzI1NiJ9",
    "PSxxxxAPPKEYxxxxSECRETVALUE",
    "APPSECRETyyyyLONGyyyyVALUEyyyy",
    "Bearer",
    "appkey",
    "appsecret",
    "authorization",
    "50123456",
    "CANO",
    "koreainvestment.com",
  ]) {
    assert.equal(
      line.includes(secret),
      false,
      `로그 줄에 ${secret}이(가) 새어 나갔다: ${line}`,
    );
  }
});

test("KIS가 msg1에 계좌번호를 되울려 줘도 마스킹된다", () => {
  const error = realisticAxiosError();
  error.response.data.msg1 = "계좌 50123456-01 의 초당 거래건수를 초과하였습니다.";

  const { line, rateLimited } = formatKisRequestLog({ trId: "TTTC0081R", elapsedMs: 5, error });

  assert.equal(rateLimited, true);
  assert.equal(line.includes("50123456"), false, `msg1의 계좌번호가 그대로 남았다: ${line}`);
  assert.match(line, /msg1="계좌 \*{8}-01 의 초당 거래건수를 초과하였습니다\."/);
});

// ---------------------------------------------------------------------------
// env 게이트
// ---------------------------------------------------------------------------

const TEST_ENV_NAME = "TEST_REQUEST_LOG_FOR_ENV_PARSER_TEST";
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
      if (original === undefined) delete process.env[key];
      else process.env[key] = original;
    }
  });
}

function captureStderr(t) {
  const originalError = console.error;
  const originalLog = console.log;
  const errors = [];
  console.error = (...args) => errors.push(args.join(" "));
  // stdout은 MCP JSON-RPC 채널이다. 경고가 console.log로 새면 프로토콜이 깨진다.
  console.log = () => {
    throw new Error("console.log(stdout)로 로그를 쓰면 MCP JSON-RPC가 깨진다");
  };
  t.after(() => {
    console.error = originalError;
    console.log = originalLog;
  });
  return errors;
}

test("KIS_ 접두사를 먼저 읽고 FINUS_KIS_ 별칭도 받는다", (t) => {
  withEnv(t, { [KIS_KEY]: "1", [FINUS_KIS_KEY]: "0" });
  assert.equal(readKisRequestLogEnv(TEST_ENV_NAME), true);

  delete process.env[KIS_KEY];
  assert.equal(readKisRequestLogEnv(TEST_ENV_NAME), false);
  process.env[FINUS_KIS_KEY] = "true";
  assert.equal(readKisRequestLogEnv(TEST_ENV_NAME), true);
});

test("미설정이면 기본값(꺼짐)이다", (t) => {
  withEnv(t, {});
  assert.equal(readKisRequestLogEnv(TEST_ENV_NAME), false);
});

test("truthy/falsy 표기를 모두 받는다", (t) => {
  withEnv(t, {});
  for (const value of ["1", "true", "TRUE", "yes", "on", " on "]) {
    process.env[KIS_KEY] = value;
    assert.equal(readKisRequestLogEnv(TEST_ENV_NAME), true, value);
  }
  for (const value of ["0", "false", "no", "off", "OFF"]) {
    process.env[KIS_KEY] = value;
    assert.equal(readKisRequestLogEnv(TEST_ENV_NAME), false, value);
  }
});

test("이상한 값은 stderr 경고 + 기본값으로 되돌린다", (t) => {
  withEnv(t, { [KIS_KEY]: "maybe" });
  const errors = captureStderr(t);

  assert.equal(readKisRequestLogEnv(TEST_ENV_NAME), false);
  assert.equal(errors.length, 1);
  assert.match(errors[0], new RegExp(KIS_KEY));
  assert.match(errors[0], /올바르지 않습니다/);
});
