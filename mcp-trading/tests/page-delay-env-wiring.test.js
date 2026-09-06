import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

// 이슈 #210: 파서(readPageDelayMsEnv)의 계약은 tests/page-delay-ms-env.test.js가 합성
// 이름으로 검증한다. 그 테스트는 index.js가 실제로 배선한 이름을 전혀 보지 않으므로,
// KIS_DAILY_CCLD_PAGE_DELAY_MS 같은 "운영자가 실제로 설정하는 문자열"이 오타로 바뀌어도
// 아무도 못 잡는다(.env.example만 맞고 코드가 다른 이름을 읽는 상태도 마찬가지다).
//
// 여기서는 소스 텍스트를 grep하는 대신 실제 서버 프로세스에 그 키를 설정해 배선을 끝까지
// 확인한다 — 잘못된 값을 넣으면 index.js가 import 시점에 readPageDelayMsEnv를 호출하고,
// 그 경고에 실제로 읽은 키 이름이 그대로 찍힌다. 이름이 어긋나면 env가 무시되어 경고가
// 아예 나오지 않으므로 테스트가 실패한다.
//
// index.js를 직접 import할 수 없어(StdioServerTransport 연결 부작용) 자식 프로세스로
// 접근한다 — tests/mcp-server.test.js와 같은 이유다. stdin을 닫으면 stdio 트랜스포트가
// 끝나 프로세스가 정상 종료(exit 0)한다.
const SPAWN_TIMEOUT_MS = 20_000;

function runServerWithEnv(env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["index.js"], {
      cwd: process.cwd(),
      env: { ...process.env, ...env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stderr = "";
    let stdout = "";
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`server did not exit within ${SPAWN_TIMEOUT_MS}ms; stderr: ${stderr}`));
    }, SPAWN_TIMEOUT_MS);
    child.on("error", (error) => { clearTimeout(timer); reject(error); });
    child.on("close", (code) => { clearTimeout(timer); resolve({ code, stderr, stdout }); });
    child.stdin.end();
  });
}

// 배선된 두 이름. index.js의 상수 이름이 아니라 운영자가 .env에 적는 키 그대로다.
for (const key of ["KIS_DAILY_CCLD_PAGE_DELAY_MS", "KIS_BALANCE_RLZ_PL_PAGE_DELAY_MS"]) {
  test(`index.js reads ${key} (env name wiring guard)`, async () => {
    const { code, stderr } = await runServerWithEnv({ [key]: "not-a-number" });

    assert.equal(code, 0, `server should exit cleanly; stderr: ${stderr}`);
    assert.ok(
      stderr.includes(key),
      `index.js must read the env var operators actually set (${key}); ` +
        `a typo in the wiring makes the value silently ignored. stderr was: ${stderr}`,
    );
  });

  test(`index.js accepts the FINUS_ prefixed alias for ${key}`, async () => {
    const aliasKey = key.replace(/^KIS_/, "FINUS_KIS_");
    const { code, stderr } = await runServerWithEnv({ [aliasKey]: "not-a-number" });

    assert.equal(code, 0, `server should exit cleanly; stderr: ${stderr}`);
    assert.ok(
      stderr.includes(aliasKey),
      `index.js must also honor ${aliasKey}. stderr was: ${stderr}`,
    );
  });
}

test("index.js rejects a page delay at or above the loop's time budget", async () => {
  // 상한(maxMs)이 실제 호출부에 배선돼 있는지 — 파서에만 있고 index.js가 안 넘기면
  // 90초 지연이 그대로 통과해 연속조회가 항상 1페이지로 잘린다.
  const { code, stderr } = await runServerWithEnv({ KIS_DAILY_CCLD_PAGE_DELAY_MS: "90000" });

  assert.equal(code, 0, `server should exit cleanly; stderr: ${stderr}`);
  assert.ok(
    stderr.includes("KIS_DAILY_CCLD_PAGE_DELAY_MS") && stderr.includes("너무 큽니다"),
    `a delay at the time budget must be rejected with a warning. stderr was: ${stderr}`,
  );
});

test("index.js accepts a valid page delay without warning", async () => {
  // 역방향 가드: 위 테스트들이 "항상 경고가 난다"로 통과하는 상태가 아님을 확인한다.
  const { code, stderr } = await runServerWithEnv({ KIS_DAILY_CCLD_PAGE_DELAY_MS: "150" });

  assert.equal(code, 0, `server should exit cleanly; stderr: ${stderr}`);
  assert.ok(
    !stderr.includes("KIS_DAILY_CCLD_PAGE_DELAY_MS"),
    `a valid delay must not warn. stderr was: ${stderr}`,
  );
});

// ---------------------------------------------------------------------------
// 이슈 #210: "값이 파싱된다"와 "그 값이 fetchAllPaged에 실제로 전달된다"는 다른 주장이다.
// 위 테스트들은 전자만 본다 — index.js의 fetchAllPaged 호출에서
// `pageDelayMs: DAILY_CCLD_PAGE_DELAY_MS` 한 줄을 지워도 전부 초록으로 통과한다. 이 이슈가
// 다루는 바로 그 줄에 살아남는 뮤턴트가 있는 셈이다.
//
// 소스를 grep하는 대신 실제 서버 프로세스를 띄우고, KIS_URL을 로컬 스텁으로 돌린 뒤 도구를
// 호출해 스텁이 받은 두 요청의 시각 간격을 잰다. 지연이 배선돼 있지 않으면 간격이 사라진다.
// 서버 기동 방식은 위와 같고(자식 프로세스), 도구 호출은 tests/mcp-server.test.js와 같은
// MCP 클라이언트를 쓴다.
// 두 연속조회 루프 각각의 배선을 본다. 지운 인자가 하나뿐이어도 그 루프의 테스트가 붉어진다.
const PAGED_TOOLS = [
  {
    tool: "get_today_daily_orders",
    envKey: "KIS_DAILY_CCLD_PAGE_DELAY_MS",
    constant: "DAILY_CCLD_PAGE_DELAY_MS",
    kisPath: "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
  },
  {
    tool: "get_balance_rlz_pl",
    envKey: "KIS_BALANCE_RLZ_PL_PAGE_DELAY_MS",
    constant: "BALANCE_RLZ_PL_PAGE_DELAY_MS",
    kisPath: "/uapi/domestic-stock/v1/trading/inquire-balance-rlz-pl",
  },
];
// 지연 자체가 아니라 "지연이 있었는가"를 본다. 스텁은 즉시 응답하므로 두 요청 사이에
// 우연히 이만큼의 간격이 생길 수 없다. setTimeout은 일찍 깨지 않으므로 하한만 본다.
const PROBE_PAGE_DELAY_MS = 600;
const PROBE_MIN_OBSERVED_GAP_MS = 500;
// 자식 프로세스를 띄우고 MCP 핸드셰이크까지 도는 프로브들의 상한. 위 SPAWN_TIMEOUT_MS와
// 같은 이유로 명시한다 — 없으면 스텁이 응답하지 않거나 t.after의 server.close가 걸릴 때
// 테스트 러너가 무한정 매달린다.
const PROBE_TEST_TIMEOUT_MS = 60_000;

function startKisStub(kisPath, {
  rateLimitOnPage = 0,
  rateLimitMsg1 = "초당 거래건수를 초과하였습니다.",
  // 200이 아니면 토큰 발급 POST가 그 상태로 응답한다 — axios가 던지므로
  // getAccessToken의 catch로 들어간다.
  tokenIssueStatus = 200,
  // 그 번째 페이지 GET을 HTTP 500으로 돌려준다 — 같은 이유로 kisApiGet의 catch로 들어간다.
  httpErrorOnPage = 0,
} = {}) {
  const requestTimes = [];
  const server = createServer((req, res) => {
    if (req.method === "POST" && req.url.startsWith("/oauth2/tokenP")) {
      req.resume();
      if (tokenIssueStatus !== 200) {
        // 바디는 일부러 msg_cd/msg1도 error_code/error_description도 아닌 모양으로 둔다.
        // 이 테스트가 보는 것은 "실패해도 줄이 나가는가"이지 실패 바디의 모양이 아니고,
        // 그 모양은 아직 미확인이다(런북 0절).
        res.writeHead(tokenIssueStatus, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ message: "stub token issue failure" }));
        return;
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ access_token: "stub-token", expires_in: 86_400 }));
      return;
    }
    if (req.method === "GET" && req.url.startsWith(kisPath)) {
      requestTimes.push(Date.now());
      const isFirstPage = requestTimes.length === 1;
      if (requestTimes.length === httpErrorOnPage) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ message: "stub upstream failure" }));
        return;
      }
      if (requestTimes.length === rateLimitOnPage) {
        // 유량 제한은 HTTP 200 + rt_cd=1로 온다(런북 2절 `http` 필드 주석). 그래서 axios가
        // 던지지 않고 kisApiGet의 rt_cd 분기가 잡는다 — logKisRequest는 그전에 이미 지났다.
        res.writeHead(200, { "Content-Type": "application/json", tr_cont: "D" });
        res.end(JSON.stringify({ rt_cd: "1", msg_cd: "EGW00201", msg1: rateLimitMsg1 }));
        return;
      }
      res.writeHead(200, {
        "Content-Type": "application/json",
        // "F"면 다음 페이지가 있다는 뜻이고, "D"면 마지막이다(balance.js의 isContinuationTrCont).
        tr_cont: isFirstPage ? "F" : "D",
      });
      res.end(JSON.stringify({
        rt_cd: "0",
        msg_cd: "MCA00000",
        msg1: "정상처리 되었습니다.",
        output1: [],
        output2: {},
        ctx_area_fk100: isFirstPage ? "FK1" : "",
        ctx_area_nk100: isFirstPage ? "NK1" : "",
      }));
      return;
    }
    res.writeHead(404).end();
  });

  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, port: server.address().port, requestTimes });
    });
  });
}

// 자식의 stderr는 파이프를 타고 비동기로 도착하므로 callTool이 반환한 직후에는 아직
// 비어 있을 수 있다. 패턴에 맞는 줄이 올 때까지 짧게 기다린다(없으면 null).
async function waitForStderrLine(read, pattern, timeoutMs = 5_000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const found = read().split(/\r?\n/).find((line) => pattern.test(line));
    if (found) return found;
    if (Date.now() >= deadline) return null;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

for (const { tool, envKey, constant, kisPath } of PAGED_TOOLS) {
  test(`index.js passes ${constant} to fetchAllPaged as pageDelayMs (${tool})`, { timeout: PROBE_TEST_TIMEOUT_MS }, async (t) => {
    const { server, port, requestTimes } = await startKisStub(kisPath);
    // 토큰 캐시는 반드시 임시 파일로 돌린다 — 기본 경로를 그대로 쓰면 개발자 머신의 실제
    // 토큰 캐시를 스텁 토큰으로 덮어쓴다.
    const tokenCachePath = path.join(os.tmpdir(), `finus-page-delay-probe-${randomUUID()}.json`);

    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ["index.js"],
      cwd: process.cwd(),
      env: {
        ...process.env,
        // 모의투자(openapivts) URL이면 get_balance_rlz_pl이 잔고 요약으로 대체되어
        // 연속조회 루프에 아예 들어가지 않는다. 스텁 URL은 그 판정에 걸리지 않는다.
        KIS_URL: `http://127.0.0.1:${port}`,
        KIS_API_KEY: "stub-appkey",
        KIS_API_SECRET: "stub-appsecret",
        KIS_ACCOUNT_NO: "5012345601",
        KIS_TOKEN_CACHE_PATH: tokenCachePath,
        [envKey]: String(PROBE_PAGE_DELAY_MS),
      },
    });
    const client = new Client({ name: "page-delay-wiring-test", version: "1.0.0" });
    t.after(async () => {
      await client.close().catch(() => {});
      await new Promise((resolve) => server.close(resolve));
      await rm(tokenCachePath, { force: true });
      await rm(`${tokenCachePath}.lock`, { force: true, recursive: true });
    });

    await client.connect(transport);
    const result = await client.callTool({ name: tool, arguments: {} });

    assert.equal(result.isError, undefined, `tool call failed: ${result.content?.[0]?.text}`);
    assert.equal(
      requestTimes.length,
      2,
      "스텁이 tr_cont=F로 2페이지를 유도했으므로 요청은 정확히 2건이어야 한다",
    );

    const observedGapMs = requestTimes[1] - requestTimes[0];
    assert.ok(
      observedGapMs >= PROBE_MIN_OBSERVED_GAP_MS,
      `index.js must pass ${constant} as fetchAllPaged's pageDelayMs; ` +
        `two pages arrived ${observedGapMs}ms apart with a ${PROBE_PAGE_DELAY_MS}ms delay configured`,
    );
  });

  // -------------------------------------------------------------------------
  // 이슈 #210: 이 PR의 중심 약속은 ".env.example:78 / index.js / 런북 2절이 말하는 대로,
  // 게이트(KIS_REQUEST_LOG)를 꺼도 유량 제한 판정 줄은 항상 나간다"이다. 기본 설정에서
  // 이 PR이 만들어 내는 관측 가능한 출력은 그 줄 하나뿐인데, 지금까지 그것을 고정하는
  // 테스트가 없었다 — logKisRequest가 줄을 실제로 내보내게 만드는 테스트가 하나도 없어서
  // `if (KIS_REQUEST_LOG_ENABLED || rateLimited) console.error(line)`을 통째로 지우거나
  // `||`를 `&&`로 바꿔도 전 스위트가 초록이었다.
  //
  // 그래서 게이트를 명시적으로 꺼 둔 채(개발자 환경이나 루트 .env가 켜 두었을 수 있으므로
  // "설정 안 함"이 아니라 "0"으로 못 박는다) 2페이지에서 EGW00201을 돌려주고, 자식의
  // stderr에 그 줄이 실제로 나왔는지 본다. 2페이지 실패는 fetchAllPaged가 부분 결과로
  // 흡수하므로(balance.js의 `pages === 0` 분기) 도구 호출은 정상 결과를 그대로 돌려준다 —
  // 그 결과를 함께 확인해 stderr 한 줄이 stdout JSON-RPC 프레이밍을 깨지 않는 것도 본다.
  test(`유량 제한 줄은 KIS_REQUEST_LOG를 꺼도 stderr로 나간다 (${tool})`, { timeout: PROBE_TEST_TIMEOUT_MS }, async (t) => {
    const { server, port, requestTimes } = await startKisStub(kisPath, { rateLimitOnPage: 2 });
    const tokenCachePath = path.join(os.tmpdir(), `finus-rate-limit-probe-${randomUUID()}.json`);

    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ["index.js"],
      cwd: process.cwd(),
      // 자식의 stderr를 부모로 흘려보내지 않고 잡는다. 기본값은 "inherit"이라
      // transport.stderr가 null이 된다.
      stderr: "pipe",
      env: {
        ...process.env,
        KIS_URL: `http://127.0.0.1:${port}`,
        KIS_API_KEY: "stub-appkey",
        KIS_API_SECRET: "stub-appsecret",
        KIS_ACCOUNT_NO: "5012345601",
        KIS_TOKEN_CACHE_PATH: tokenCachePath,
        // 게이트를 확실히 끈다. 두 이름을 다 꺼야 한다(kis-rate-limit.js의 readKisRequestLogEnv).
        KIS_REQUEST_LOG: "0",
        FINUS_KIS_REQUEST_LOG: "0",
      },
    });
    const client = new Client({ name: "kis-request-log-test", version: "1.0.0" });
    t.after(async () => {
      await client.close().catch(() => {});
      await new Promise((resolve) => server.close(resolve));
      await rm(tokenCachePath, { force: true });
      await rm(`${tokenCachePath}.lock`, { force: true, recursive: true });
    });

    await client.connect(transport);
    let stderrText = "";
    assert.ok(transport.stderr, "stderr: \"pipe\"를 넘겼으므로 transport.stderr가 있어야 한다");
    transport.stderr.on("data", (chunk) => { stderrText += chunk; });

    const result = await client.callTool({ name: tool, arguments: {} });

    assert.equal(requestTimes.length, 2, "1페이지 정상 + 2페이지 유량 제한으로 요청은 2건이어야 한다");
    // (b) 줄을 내보내도 stdout JSON-RPC 프레이밍은 멀쩡하다 — 2페이지 실패는 부분 결과로 흡수된다.
    assert.equal(result.isError, undefined, `tool call failed: ${result.content?.[0]?.text}`);
    assert.equal(typeof result.content?.[0]?.text, "string", "도구 결과는 파싱 가능한 텍스트여야 한다");

    // (a) 게이트가 꺼져 있어도 유량 제한 줄은 나간다.
    const line = await waitForStderrLine(() => stderrText, /^\[kis-req\] .*class=rate_limit/);
    assert.ok(
      line,
      "게이트를 꺼도 class=rate_limit 줄은 항상 나가야 한다(.env.example, 런북 2절의 약속). " +
        `stderr was: ${stderrText}`,
    );
    assert.match(line, /msg_cd=EGW00201/);

    // 역방향 가드: "게이트를 무시하고 전부 내보낸다"로 통과하는 상태가 아님을 확인한다.
    // 1페이지는 class=ok이고, 게이트가 꺼져 있으므로 그 줄은 나오면 안 된다.
    assert.ok(
      !stderrText.includes("class=ok"),
      `게이트가 꺼져 있으면 정상 요청의 타이밍 줄은 나가지 않아야 한다. stderr was: ${stderrText}`,
    );
  });

  // -------------------------------------------------------------------------
  // 이슈 #210: KIS가 되울려 주는 msg1에 계좌번호가 실려 오고, kisApiGet이 만드는
  // "KIS API 오류: <msg1>" 에러 메시지는 [kis-req] 줄의 마스킹을 통과하지 않은 채
  // 도구 결과 텍스트·balance.js의 stderr 줄·백엔드 short_error(텔레그램)까지 그대로
  // 흘러간다. 1페이지에서 계좌번호가 실린 msg1을 돌려주면 fetchAllPaged가 그대로
  // 전파하므로(pages === 0 분기) 도구 결과 텍스트에서 그 누출을 직접 볼 수 있다.
  test(`KIS 오류 메시지의 긴 숫자는 도구 결과에서도 가려진다 (${tool})`, { timeout: PROBE_TEST_TIMEOUT_MS }, async (t) => {
    const { server, port } = await startKisStub(kisPath, {
      rateLimitOnPage: 1,
      rateLimitMsg1: "초당 거래건수를 초과하였습니다. 계좌 50123456",
    });
    const tokenCachePath = path.join(os.tmpdir(), `finus-msg1-mask-probe-${randomUUID()}.json`);

    const transport = new StdioClientTransport({
      command: process.execPath,
      args: ["index.js"],
      cwd: process.cwd(),
      // 유량 제한 줄이 테스트 출력에 섞이지 않게 잡아 둔다(여기서 읽지는 않는다).
      stderr: "pipe",
      env: {
        ...process.env,
        KIS_URL: `http://127.0.0.1:${port}`,
        KIS_API_KEY: "stub-appkey",
        KIS_API_SECRET: "stub-appsecret",
        KIS_ACCOUNT_NO: "5012345601",
        KIS_TOKEN_CACHE_PATH: tokenCachePath,
      },
    });
    const client = new Client({ name: "msg1-mask-test", version: "1.0.0" });
    t.after(async () => {
      await client.close().catch(() => {});
      await new Promise((resolve) => server.close(resolve));
      await rm(tokenCachePath, { force: true });
      await rm(`${tokenCachePath}.lock`, { force: true, recursive: true });
    });

    await client.connect(transport);
    transport.stderr?.resume();
    const result = await client.callTool({ name: tool, arguments: {} });

    assert.equal(result.isError, true, "1페이지 실패는 그대로 전파되어야 한다");
    const text = result.content?.[0]?.text ?? "";
    assert.ok(text.includes("KIS API 오류: "), `KIS 오류가 전파되어야 한다: ${text}`);
    assert.ok(
      !text.includes("50123456"),
      `KIS API 오류 메시지의 8자리 이상 숫자는 가려져야 한다(계좌번호 누출). 실제: ${text}`,
    );
    assert.ok(text.includes("계좌 ********"), `자릿수는 남기고 값만 지운다. 실제: ${text}`);
  });
}

// ---------------------------------------------------------------------------
// 이슈 #210: 위 프로브들은 전부 **성공 경로**만 지난다. KIS는 유량 제한을 HTTP 200 +
// rt_cd=1로 돌려주므로(런북 2절의 `http` 필드) axios가 던지지 않고, 그래서 index.js의 두
// catch 안에 있는 logKisRequest — getAccessToken의 것과 kisApiGet의 것 — 는 어떤 테스트에도
// 닿지 않았다. 둘을 통째로 지워도 전 스위트가 초록이었다.
//
// 그중 getAccessToken의 catch는 이 PR의 OAuth 분기가 노리는 바로 그 호출부다. 분기의
// 분류는 tests/kis-rate-limit.test.js가 고정했지만, 그 자리에서 줄이 실제로 **나가는지**는
// 아무것도 고정하지 않았다.
//
// 그래서 전송 계층 실패를 실제로 일으킨다 — 스텁이 non-2xx를 돌려주면 axios가 던지고
// catch가 열린다. 게이트는 켠다: 전송 실패는 class=other라 게이트가 꺼져 있으면 설계대로
// 안 나간다(그 반대 방향은 위 유량 제한 프로브가 이미 고정한다). 덤으로 이 두 테스트가
// 게이트의 정방향(켜면 유량 제한이 아닌 줄도 나간다)도 함께 고정한다.
const { tool: CATCH_PROBE_TOOL, kisPath: CATCH_PROBE_KIS_PATH } = PAGED_TOOLS[0];

test("전송 실패한 토큰 발급도 [kis-req] 줄을 남긴다 (getAccessToken의 catch)", { timeout: PROBE_TEST_TIMEOUT_MS }, async (t) => {
  const { server, port, requestTimes } = await startKisStub(CATCH_PROBE_KIS_PATH, { tokenIssueStatus: 503 });
  const tokenCachePath = path.join(os.tmpdir(), `finus-token-catch-probe-${randomUUID()}.json`);

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: ["index.js"],
    cwd: process.cwd(),
    stderr: "pipe",
    env: {
      ...process.env,
      KIS_URL: `http://127.0.0.1:${port}`,
      KIS_API_KEY: "stub-appkey",
      KIS_API_SECRET: "stub-appsecret",
      KIS_ACCOUNT_NO: "5012345601",
      KIS_TOKEN_CACHE_PATH: tokenCachePath,
      KIS_REQUEST_LOG: "1",
    },
  });
  const client = new Client({ name: "token-catch-emit-test", version: "1.0.0" });
  t.after(async () => {
    await client.close().catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    await rm(tokenCachePath, { force: true });
    await rm(`${tokenCachePath}.lock`, { force: true, recursive: true });
  });

  await client.connect(transport);
  assert.ok(transport.stderr, "stderr: \"pipe\"를 넘겼으므로 transport.stderr가 있어야 한다");
  let stderrText = "";
  transport.stderr.on("data", (chunk) => { stderrText += chunk; });

  const result = await client.callTool({ name: CATCH_PROBE_TOOL, arguments: {} });

  assert.equal(result.isError, true, "토큰 발급이 실패하면 도구 호출도 실패해야 한다");
  assert.equal(requestTimes.length, 0, "토큰이 없으면 조회 GET은 아예 나가지 않는다");

  const line = await waitForStderrLine(() => stderrText, /^\[kis-req\] .*tr_id=tokenP/);
  assert.ok(
    line,
    "getAccessToken의 catch도 [kis-req] 줄을 내보내야 한다 — 이 줄이 없으면 토큰 발급 " +
      `실패는 로그에 아예 남지 않는다. stderr was: ${stderrText}`,
  );
  assert.match(line, /http=503/);
  assert.match(line, /class=other/);
});

test("전송 실패한 조회도 [kis-req] 줄을 남긴다 (kisApiGet의 catch)", { timeout: PROBE_TEST_TIMEOUT_MS }, async (t) => {
  const { server, port, requestTimes } = await startKisStub(CATCH_PROBE_KIS_PATH, { httpErrorOnPage: 1 });
  const tokenCachePath = path.join(os.tmpdir(), `finus-get-catch-probe-${randomUUID()}.json`);

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: ["index.js"],
    cwd: process.cwd(),
    stderr: "pipe",
    env: {
      ...process.env,
      KIS_URL: `http://127.0.0.1:${port}`,
      KIS_API_KEY: "stub-appkey",
      KIS_API_SECRET: "stub-appsecret",
      KIS_ACCOUNT_NO: "5012345601",
      KIS_TOKEN_CACHE_PATH: tokenCachePath,
      KIS_REQUEST_LOG: "1",
    },
  });
  const client = new Client({ name: "get-catch-emit-test", version: "1.0.0" });
  t.after(async () => {
    await client.close().catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    await rm(tokenCachePath, { force: true });
    await rm(`${tokenCachePath}.lock`, { force: true, recursive: true });
  });

  await client.connect(transport);
  assert.ok(transport.stderr, "stderr: \"pipe\"를 넘겼으므로 transport.stderr가 있어야 한다");
  let stderrText = "";
  transport.stderr.on("data", (chunk) => { stderrText += chunk; });

  const result = await client.callTool({ name: CATCH_PROBE_TOOL, arguments: {} });

  assert.equal(result.isError, true, "1페이지가 전송 계층에서 실패하면 그대로 전파된다");
  assert.ok(requestTimes.length >= 1, "조회 GET이 스텁에 도달해야 한다");

  // 토큰 발급은 성공했으므로 http=500 줄의 주인은 조회뿐이다. tr_id로 한 번 더 못 박는다.
  const line = await waitForStderrLine(() => stderrText, /^\[kis-req\] .*http=500/);
  assert.ok(
    line,
    "kisApiGet의 catch도 [kis-req] 줄을 내보내야 한다 — 이 줄이 없으면 전송 계층에서 끊긴 " +
      `요청(타임아웃·연결 실패)이 로그에 통째로 빠진다. stderr was: ${stderrText}`,
  );
  assert.ok(!line.includes("tr_id=tokenP"), `조회 줄이어야 한다. 실제: ${line}`);
  assert.match(line, /class=other/);
});
