import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  DEFAULT_LOCK_STALE_MS,
  DEFAULT_LOCK_WAIT_MS,
  DEFAULT_TOKEN_TTL_MARGIN_MS,
  KisTokenCache,
} from "../token-cache.js";

const MODULE_URL = new URL("../token-cache.js", import.meta.url);
const HOUR_MS = 3_600_000;

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "finus-token-cache-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 }));
  return dir;
}

function tempCachePath(t) {
  return path.join(tempDir(t), "token.json");
}

// 테스트가 실제 시계에 흔들리지 않도록 now를 직접 쥔다. 시간이 흘러야 하는
// 테스트(대기 상한·낡은 락)는 이 시계를 쓰지 않고 실제 시계로 돈다.
function fixedClock(startMs = 1_000_000) {
  return { now: () => startMs };
}

async function waitUntil(predicate, { timeoutMs = 5_000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (predicate()) return;
    assert.ok(Date.now() < deadline, "조건이 시간 안에 성립하지 않았습니다");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

test("read()는 파일에 든 유효한 토큰을 돌려주고 메모리에 올린다", (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  fs.writeFileSync(filePath, JSON.stringify({ token: "cached", expiresAt: clock.now() + HOUR_MS }));

  const cache = new KisTokenCache({ filePath, now: clock.now });
  assert.equal(cache.read(), "cached");

  // 두 번째 읽기는 파일을 다시 보지 않아야 한다. 파일을 지워도 같은 값이 나오면
  // 메모리 캐시가 실제로 동작한 것이다.
  fs.rmSync(filePath);
  assert.equal(cache.read(), "cached");
});

// 잡는 뮤테이션: TTL 여유(ttlMarginMs)를 빼면 만료 직전 토큰을 히트로 판정해
// 이 테스트가 실패한다.
test("read()는 남은 수명이 TTL 여유 이하인 토큰을 미스로 본다", (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  const cache = new KisTokenCache({ filePath, now: clock.now });

  fs.writeFileSync(
    filePath,
    JSON.stringify({ token: "expiring", expiresAt: clock.now() + DEFAULT_TOKEN_TTL_MARGIN_MS - 1 }),
  );
  assert.equal(cache.read(), null);

  fs.writeFileSync(
    filePath,
    JSON.stringify({ token: "fresh", expiresAt: clock.now() + DEFAULT_TOKEN_TTL_MARGIN_MS + 1 }),
  );
  assert.equal(cache.read(), "fresh");
});

// 이 캐시의 읽기는 fail-open이다 — 손상된 캐시는 발급을 한 번 더 하게 만들 뿐이라
// 던지지 않는다(중복 주문이 걸린 order-dedup.js의 원장과 반대 자세다).
test("read()는 잘린 JSON·형태 불일치·파일 없음을 모두 캐시 미스로 떨어뜨린다", (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  const cache = new KisTokenCache({ filePath, now: clock.now });
  // 손상된 캐시를 읽으면 진단 로그가 나간다. 테스트 출력만 조용히 시킨다.
  t.mock.method(console, "error", () => {});

  assert.equal(cache.read(), null, "파일이 없으면 미스여야 합니다");

  fs.writeFileSync(filePath, '{"token":"trunc');
  assert.equal(cache.read(), null, "잘린 JSON은 미스여야 합니다");

  fs.writeFileSync(filePath, JSON.stringify({ token: 42, expiresAt: clock.now() + HOUR_MS }));
  assert.equal(cache.read(), null, "토큰이 문자열이 아니면 미스여야 합니다");

  fs.writeFileSync(filePath, JSON.stringify({ token: "no-expiry" }));
  assert.equal(cache.read(), null, "만료 시각이 없으면 미스여야 합니다");
});

// 잡는 뮤테이션: 임시 파일 + rename을 예전처럼 캐시 경로 직접 쓰기로 되돌리면,
// 쓰기 도중 원래 경로가 0바이트로 잘려 아래 검증이 깨진다.
test("write()는 임시 파일에 쓴 뒤 rename으로 갈아치운다", (t) => {
  const filePath = tempCachePath(t);
  const dir = path.dirname(filePath);
  const clock = fixedClock();
  const cache = new KisTokenCache({ filePath, now: clock.now });

  fs.writeFileSync(filePath, JSON.stringify({ token: "old", expiresAt: clock.now() + HOUR_MS }));

  const originalRename = fs.renameSync;
  let contentAtRename = null;
  t.mock.method(fs, "renameSync", (from, to) => {
    contentAtRename = fs.readFileSync(filePath, "utf8");
    return originalRename(from, to);
  });

  cache.write({ token: "new", expiresAt: clock.now() + HOUR_MS });

  assert.deepEqual(JSON.parse(contentAtRename), {
    token: "old",
    expiresAt: clock.now() + HOUR_MS,
  });
  assert.equal(JSON.parse(fs.readFileSync(filePath, "utf8")).token, "new");
  assert.deepEqual(
    fs.readdirSync(dir).filter((name) => name.endsWith(".tmp")),
    [],
    "임시 파일이 남으면 안 됩니다",
  );
});

// 쓰기 실패는 삼킨다 — 이미 받아둔 토큰을 캐시 쓰기 실패 때문에 버리면 안 된다.
// 잡는 뮤테이션: write()의 catch를 throw로 바꾸면 이 테스트가 실패한다.
test("write()는 rename이 실패해도 던지지 않고 임시 파일도 남기지 않는다", (t) => {
  const filePath = tempCachePath(t);
  const dir = path.dirname(filePath);
  const clock = fixedClock();
  const cache = new KisTokenCache({ filePath, now: clock.now });

  t.mock.method(fs, "renameSync", () => {
    const error = new Error("rename failed");
    error.code = "EACCES";
    throw error;
  });
  t.mock.method(console, "error", () => {});

  assert.doesNotThrow(() => cache.write({ token: "t", expiresAt: clock.now() + HOUR_MS }));
  assert.deepEqual(
    fs.readdirSync(dir).filter((name) => name.endsWith(".tmp")),
    [],
    "실패한 쓰기의 임시 파일이 남으면 안 됩니다",
  );
  // 파일에는 못 남겼어도 이번 프로세스는 메모리의 토큰으로 계속 간다.
  assert.equal(cache.read(), "t");
});

test("getOrIssue()는 캐시 히트면 발급하지 않는다", async (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  fs.writeFileSync(filePath, JSON.stringify({ token: "cached", expiresAt: clock.now() + HOUR_MS }));

  let issued = 0;
  const cache = new KisTokenCache({ filePath, now: clock.now });
  const token = await cache.getOrIssue(async () => {
    issued += 1;
    return { token: "new", expiresAt: clock.now() + HOUR_MS };
  });

  assert.equal(token, "cached");
  assert.equal(issued, 0);
});

test("getOrIssue()는 발급 결과를 캐시 파일에 남기고 락을 회수한다", async (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  const cache = new KisTokenCache({ filePath, now: clock.now });

  const token = await cache.getOrIssue(async () => ({
    token: "issued",
    expiresAt: clock.now() + HOUR_MS,
  }));

  assert.equal(token, "issued");
  assert.equal(JSON.parse(fs.readFileSync(filePath, "utf8")).token, "issued");
  assert.equal(fs.existsSync(`${filePath}.lock`), false, "락 파일이 남으면 안 됩니다");
});

// 발급이 실패하면 락을 반드시 놓아야 한다. 놓지 않으면 다음 호출이 대기 상한
// (기본 10초)을 통째로 소진한 뒤에야 발급을 시도한다.
test("getOrIssue()는 발급이 던져도 락을 놓는다", async (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  const cache = new KisTokenCache({ filePath, now: clock.now });

  await assert.rejects(
    cache.getOrIssue(async () => {
      throw new Error("Access Token 발급 실패");
    }),
    /발급 실패/,
  );
  assert.equal(fs.existsSync(`${filePath}.lock`), false, "실패해도 락은 회수되어야 합니다");
});

// 토큰 없는 발급 결과를 통과시키면 호출부가 `Bearer undefined`로 조회를 태우고,
// 원인이 만료·권한 문제로 보이는 KIS 오류로 둔갑한다. 발급 자리에서 끊어야 한다.
test("getOrIssue()는 토큰 없는 발급 결과를 캐시에 쓰지 않고 던진다", async (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  const cache = new KisTokenCache({ filePath, now: clock.now });

  await assert.rejects(
    cache.getOrIssue(async () => ({ token: undefined, expiresAt: clock.now() + HOUR_MS })),
    /access token이 없습니다/,
  );
  assert.equal(fs.existsSync(filePath), false, "캐시 파일을 만들면 안 됩니다");
  assert.equal(fs.existsSync(`${filePath}.lock`), false, "락은 회수되어야 합니다");
});

// 만료 시각이 숫자가 아니면(KIS의 expires_in이 쓰레기 값이면 index.js의 Number()가
// NaN을 낸다) 캐시에는 expiresAt: null이 남고 read()가 그 캐시를 영원히 미스로 본다.
// 락은 멀쩡히 도는데 매 호출이 발급을 치는, #324 이전 상태로 조용히 되돌아간다.
test("getOrIssue()는 만료 시각이 숫자가 아닌 발급 결과를 캐시에 쓰지 않고 던진다", async (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  const cache = new KisTokenCache({ filePath, now: clock.now });

  await assert.rejects(
    cache.getOrIssue(async () => ({ token: "t", expiresAt: Number("abc") })),
    /만료 시각이 숫자가 아닙니다/,
  );
  assert.equal(fs.existsSync(filePath), false, "캐시 파일을 만들면 안 됩니다");
  assert.equal(fs.existsSync(`${filePath}.lock`), false, "락은 회수되어야 합니다");
});

// 이슈 #324의 본체. 같은 캐시 파일을 보는 두 호출자가 동시에 들어와도 발급은
// 한 번뿐이어야 한다. 잡는 뮤테이션: 락을 걷어내면 두 번 발급돼 실패한다.
test("동시에 들어온 두 호출자 중 하나만 발급하고 나머지는 캐시로 통과한다", async (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  const gate = deferred();
  let issued = 0;

  // 인스턴스를 둘로 나누는 이유는 프로세스 두 개를 흉내 내기 위해서다 —
  // 하나를 공유하면 메모리 캐시가 경합 자체를 가려 버린다.
  const makeCache = () => new KisTokenCache({ filePath, now: clock.now, pollMs: 5 });
  const issue = async () => {
    issued += 1;
    await gate.promise;
    return { token: `token-${issued}`, expiresAt: clock.now() + HOUR_MS };
  };

  const first = makeCache().getOrIssue(issue);
  // 첫 호출이 락을 잡고 발급에 들어간 뒤에 두 번째를 태운다 — 그래야 "대기하다
  // 캐시로 통과하는" 경로를 실제로 지난다.
  await waitUntil(() => issued === 1);
  const second = makeCache().getOrIssue(issue);

  gate.resolve();
  assert.deepEqual(await Promise.all([first, second]), ["token-1", "token-1"]);
  assert.equal(issued, 1, "발급은 한 번만 일어나야 합니다");
});

// 락을 잡지 못한 채 대기 상한을 넘기면 무락 발급으로 내려간다(fail-open).
// 락 때문에 토큰 발급 자체가 막히는 쪽이 더 나쁘기 때문이다.
test("대기 상한을 넘기면 락 없이라도 발급한다", async (t) => {
  const filePath = tempCachePath(t);
  const lockPath = `${filePath}.lock`;
  // 방금 만들어진 남의 락 — 낡음 판정에 걸리지 않는다.
  fs.writeFileSync(lockPath, JSON.stringify({ owner: "someone-else", pid: 1, at: Date.now() }));
  t.mock.method(console, "error", () => {});

  // 대기 상한은 흘러가는 시계로만 넘길 수 있으므로 여기서는 고정 시계를 쓰지 않는다.
  const cache = new KisTokenCache({
    filePath,
    lockWaitMs: 30,
    pollMs: 5,
    lockStaleMs: HOUR_MS,
  });
  const token = await cache.getOrIssue(async () => ({
    token: "issued-without-lock",
    expiresAt: Date.now() + HOUR_MS,
  }));

  assert.equal(token, "issued-without-lock");
  assert.equal(
    JSON.parse(fs.readFileSync(lockPath, "utf8")).owner,
    "someone-else",
    "무락 발급은 남의 락을 건드리면 안 됩니다",
  );
});

// 락 파일을 아예 만들 수 없는 환경(쓰기 불가 마운트 등)에서는 기다려도 달라지는 것이
// 없다. 대기 상한을 소진하면 그 환경의 모든 MCP 호출이 10초씩 늦어지고 그동안 폴링
// 로그가 수백 줄 나간다. 잡는 뮤테이션: #tryAcquireLock이 EEXIST와 그 외 실패를 같은
// 값으로 뭉뚱그리면(예전 구현) sleep이 불려 이 테스트가 실패한다.
test("락을 만들 수 없는 환경이면 기다리지 않고 바로 발급한다", async (t) => {
  const filePath = tempCachePath(t);
  const lockPath = `${filePath}.lock`;
  const clock = fixedClock();
  t.mock.method(console, "error", () => {});

  const originalWrite = fs.writeFileSync;
  t.mock.method(fs, "writeFileSync", (target, data, options) => {
    if (target === lockPath) {
      const error = new Error("read-only file system");
      error.code = "EROFS";
      throw error;
    }
    return originalWrite(target, data, options);
  });

  let slept = 0;
  const cache = new KisTokenCache({
    filePath,
    now: clock.now,
    sleep: async () => {
      slept += 1;
    },
  });
  const token = await cache.getOrIssue(async () => ({
    token: "issued-without-lock",
    expiresAt: clock.now() + HOUR_MS,
  }));

  assert.equal(token, "issued-without-lock");
  assert.equal(slept, 0, "락을 만들 수 없는 환경에서는 한 번도 대기하면 안 됩니다");
});

// 대기 상한은 호출마다 좁힐 수 있어야 한다. 주문 경로가 그 좁힌 값을 쓴다
// (index.js의 ORDER_TOKEN_LOCK_WAIT_MS). 잡는 뮤테이션: getOrIssue가 인자를 무시하고
// 인스턴스 기본값(여기서는 1시간)을 쓰면 이 테스트가 타임아웃으로 죽는다.
test("getOrIssue()의 lockWaitMs 인자가 인스턴스 기본값을 덮어쓴다", async (t) => {
  const filePath = tempCachePath(t);
  const lockPath = `${filePath}.lock`;
  fs.writeFileSync(lockPath, JSON.stringify({ owner: "someone-else", pid: 1, at: Date.now() }));
  t.mock.method(console, "error", () => {});

  const cache = new KisTokenCache({
    filePath,
    lockWaitMs: HOUR_MS,
    lockStaleMs: HOUR_MS,
    pollMs: 5,
  });
  const token = await cache.getOrIssue(
    async () => ({ token: "issued-without-lock", expiresAt: Date.now() + HOUR_MS }),
    { lockWaitMs: 30 },
  );

  assert.equal(token, "issued-without-lock");
});

// 고아 락 인수는 대기가 끝나기 전에 성립해야 의미가 있다. 임계가 대기 상한보다 크면
// 락을 처음 만난 호출은 인수를 시도조차 못 하고 대기만 버린 뒤 무락 발급으로 내려간다.
// 잡는 뮤테이션: DEFAULT_LOCK_STALE_MS를 다시 대기 상한 이상으로 올리면 실패한다.
test("기본 낡음 임계는 기본 대기 상한보다 작다", () => {
  assert.ok(
    DEFAULT_LOCK_STALE_MS < DEFAULT_LOCK_WAIT_MS,
    `낡음 임계(${DEFAULT_LOCK_STALE_MS}ms)는 대기 상한(${DEFAULT_LOCK_WAIT_MS}ms)보다 작아야 합니다`,
  );
});

// 대기 루프는 pollMs마다 read()를 부른다. 캐시가 손상돼 있으면 그때마다 같은 실패
// 로그가 나가 10초 대기 하나에 200줄이 쌓인다. 잡는 뮤테이션: 로그 1회 플래그를 빼면
// 호출 수만큼 찍혀 실패한다.
test("손상된 캐시 읽기 실패 로그는 인스턴스당 한 번만 나간다", (t) => {
  const filePath = tempCachePath(t);
  const clock = fixedClock();
  fs.writeFileSync(filePath, '{"token":"trunc');
  const errors = t.mock.method(console, "error", () => {});

  const cache = new KisTokenCache({ filePath, now: clock.now });
  for (let i = 0; i < 5; i += 1) {
    assert.equal(cache.read(), null);
  }

  assert.equal(errors.mock.callCount(), 1, "같은 읽기 실패를 반복해서 찍으면 안 됩니다");
});

// SIGKILL·OOM으로 죽은 프로세스가 남긴 락 때문에 발급이 영구히 막히면 안 된다.
test("낡은 락은 걷어내고 발급한다", async (t) => {
  const filePath = tempCachePath(t);
  const lockPath = `${filePath}.lock`;
  fs.writeFileSync(lockPath, JSON.stringify({ owner: "dead-process", pid: 1, at: 0 }));
  const past = new Date(Date.now() - 60_000);
  fs.utimesSync(lockPath, past, past);
  t.mock.method(console, "error", () => {});

  // 낡음 판정은 파일 mtime(실제 시계)과 비교하므로 여기서는 고정 시계를 쓰지 않는다.
  const cache = new KisTokenCache({
    filePath,
    lockStaleMs: 1_000,
    lockWaitMs: 100,
    pollMs: 5,
  });
  const token = await cache.getOrIssue(async () => ({
    token: "after-takeover",
    expiresAt: Date.now() + HOUR_MS,
  }));

  assert.equal(token, "after-takeover");
  assert.equal(fs.existsSync(lockPath), false, "인수한 락도 끝에는 회수되어야 합니다");
});

// 내 발급이 길어져 남이 내 락을 인수했다면, 내가 끝나면서 그 락을 지우면 안 된다.
// 지우는 순간 인수한 쪽의 상호 배제가 깨진다. 잡는 뮤테이션: #releaseLock에서
// owner 확인을 빼면 이 테스트가 실패한다.
test("내 락이 아니면 해제하지 않는다", async (t) => {
  const filePath = tempCachePath(t);
  const lockPath = `${filePath}.lock`;
  t.mock.method(console, "error", () => {});

  const slowGate = deferred();
  const takeoverGate = deferred();
  let slowIssuing = false;
  let takeoverIssuing = false;

  const slow = new KisTokenCache({ filePath });
  const slowCall = slow.getOrIssue(async () => {
    slowIssuing = true;
    await slowGate.promise;
    return { token: "slow", expiresAt: Date.now() + HOUR_MS };
  });
  await waitUntil(() => slowIssuing);

  // 인수하는 쪽은 락을 낡은 것으로 보도록 임계를 0으로 둔다.
  const taker = new KisTokenCache({ filePath, lockStaleMs: 0, lockWaitMs: 1_000, pollMs: 5 });
  const takerCall = taker.getOrIssue(async () => {
    takeoverIssuing = true;
    await takeoverGate.promise;
    return { token: "taker", expiresAt: Date.now() + HOUR_MS };
  });
  await waitUntil(() => takeoverIssuing);
  const takerOwner = JSON.parse(fs.readFileSync(lockPath, "utf8")).owner;

  // 느린 쪽이 끝난다. 인수자의 락은 그대로 있어야 한다.
  slowGate.resolve();
  await slowCall;
  assert.equal(
    JSON.parse(fs.readFileSync(lockPath, "utf8")).owner,
    takerOwner,
    "인수자의 락을 지우면 안 됩니다",
  );

  takeoverGate.resolve();
  await takerCall;
  assert.equal(fs.existsSync(lockPath), false, "인수자는 자기 락을 회수해야 합니다");
});

// 이슈 #324의 재현 조건 그대로다: 캐시가 빈 상태에서 mcp-trading 프로세스가
// 동시에 여러 개 뜬다. 한 프로세스 안의 동시성으로는 프로세스 사이의 경합을
// 재현할 수 없으므로(메모리 캐시가 가려 준다) 실제로 자식 프로세스를 띄운다.
// 락이 없던 구현에서는 이 테스트의 tokenP 요청 수가 자식 수만큼(5) 나온다.
test("동시에 뜬 5개 프로세스가 tokenP를 한 번만 친다", async (t) => {
  const dir = tempDir(t);
  const cachePath = path.join(dir, "token.json");
  const childPath = path.join(dir, "child.mjs");

  let requests = 0;
  const server = http.createServer((req, res) => {
    requests += 1;
    const issued = requests;
    // 발급이 순간에 끝나면 뒤에 뜬 프로세스가 이미 채워진 캐시를 보게 돼 경합
    // 자체가 재현되지 않는다. 실제 KIS 응답 지연을 흉내 내 창을 벌린다.
    setTimeout(() => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ access_token: `token-${issued}`, expires_in: 86_400 }));
    }, 200);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const tokenUrl = `http://127.0.0.1:${server.address().port}/oauth2/tokenP`;

  fs.writeFileSync(
    childPath,
    [
      `import { KisTokenCache } from ${JSON.stringify(MODULE_URL.href)};`,
      "const [cachePath, tokenUrl] = process.argv.slice(2);",
      "const cache = new KisTokenCache({ filePath: cachePath });",
      "const token = await cache.getOrIssue(async () => {",
      '  const response = await fetch(tokenUrl, { method: "POST" });',
      "  const body = await response.json();",
      "  return { token: body.access_token, expiresAt: Date.now() + body.expires_in * 1000 };",
      "});",
      "process.stdout.write(token);",
    ].join("\n"),
  );

  const runChild = () =>
    new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [childPath, cachePath, tokenUrl], {
        stdio: ["ignore", "pipe", "pipe"],
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk) => {
        stdout += chunk;
      });
      child.stderr.on("data", (chunk) => {
        stderr += chunk;
      });
      child.on("error", reject);
      child.on("close", (code) => {
        if (code !== 0) {
          reject(new Error(`자식 프로세스가 ${code}로 종료했습니다: ${stderr}`));
          return;
        }
        resolve(stdout);
      });
    });

  const tokens = await Promise.all(Array.from({ length: 5 }, runChild));

  assert.equal(requests, 1, `tokenP 요청은 한 번이어야 합니다(실제 ${requests}회)`);
  assert.deepEqual([...new Set(tokens)], ["token-1"], "모든 프로세스가 같은 토큰을 써야 합니다");
  assert.equal(fs.existsSync(`${cachePath}.lock`), false, "락 파일이 남으면 안 됩니다");
  assert.deepEqual(
    fs.readdirSync(dir).filter((name) => name.endsWith(".tmp")),
    [],
    "임시 파일이 남으면 안 됩니다",
  );
});
