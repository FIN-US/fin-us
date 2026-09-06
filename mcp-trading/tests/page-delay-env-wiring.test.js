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

function startKisStub(kisPath) {
  const requestTimes = [];
  const server = createServer((req, res) => {
    if (req.method === "POST" && req.url.startsWith("/oauth2/tokenP")) {
      req.resume();
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ access_token: "stub-token", expires_in: 86_400 }));
      return;
    }
    if (req.method === "GET" && req.url.startsWith(kisPath)) {
      requestTimes.push(Date.now());
      const isFirstPage = requestTimes.length === 1;
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

for (const { tool, envKey, constant, kisPath } of PAGED_TOOLS) {
  test(`index.js passes ${constant} to fetchAllPaged as pageDelayMs (${tool})`, async (t) => {
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
}
