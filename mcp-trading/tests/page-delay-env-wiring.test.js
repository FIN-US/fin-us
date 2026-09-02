import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";

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
