import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

// KIS OAuth 토큰 캐시. MCP 호출 하나마다 mcp-trading node 프로세스가 새로 뜨므로
// (backend/services.py의 run_mcp_tool → stdio_client), 이 캐시는 프로세스 안이 아니라
// 프로세스 "사이"에서 동작해야 한다. 이슈 #324가 잡는 결함이 정확히 그 지점이다:
// 캐시가 비었거나 만료 직후면 동시에 뜬 프로세스들이 각자 /oauth2/tokenP를 쳐서
// KIS의 발급 유량 제한에 걸리고, 걸린 쪽은 `Access Token 발급 실패`로 끝난다.
// /advise 경로에서는 그것이 snapshot_failed 거부 + 재제안 냉각(기본 60분)이 된다.
//
// 두 가지를 한다.
//   1) 발급 직렬화 — 락 파일을 잡은 프로세스 하나만 발급하고, 나머지는 대기하며
//      캐시를 다시 읽는다(락 해제를 기다리지 않는다 — 캐시가 채워지는 즉시 통과).
//   2) 원자적 쓰기 — 임시 파일에 쓰고 rename으로 갈아치워, 다른 프로세스가 잘린
//      JSON을 읽고 캐시 미스로 떨어지는 경로를 없앤다.

// 만료 직전 토큰으로 요청을 태우지 않기 위한 여유. 남은 수명이 이 값 이하면 캐시
// 미스로 취급해 새로 발급한다.
export const DEFAULT_TOKEN_TTL_MARGIN_MS = 60_000;
// 락을 못 잡은 프로세스가 캐시를 기다리는 상한. 발급 요청 자체는 index.js의
// kisAxios 타임아웃(8초)을 넘길 수 없으므로, 그 위로 여유를 둔 10초면 "락 보유자가
// 살아 있는 한" 항상 캐시를 보고 통과한다(보유자가 발급에 실패해 락을 놓는 경우도
// 마찬가지다 — 다음 폴링에서 락을 잡게 되므로 이 상한까지 가지 않는다). 이 상한을
// 넘기면 무락 발급으로 넘어간다(fail-open) — 락 때문에 토큰 발급 자체가 막히는 쪽이
// 더 나쁘기 때문이다.
//
// 상위 시간 예산과의 관계: 이 상한을 통째로 소진하는 경우(보유자가 자기 타임아웃마저
// 넘겨 멈춘 드문 상황)의 최악은 대기 10초 + 자기 발급 8초 = 18초다. MCP 호출 하나의
// 상한은 backend/services.py run_mcp_tool의 30초이므로, 기동 오버헤드(~1-2초)와
// 조회 요청 1회(8초)를 더해도 그 안에 들어온다. get_balance의 15초 예산
// (BALANCE_TIME_BUDGET_MS, mcp-trading/balance.js)은 이보다 먼저 걸릴 수 있는데,
// 그 결과는 실패가 아니라 연속조회 페이지를 덜 가져오는 축소다.
export const DEFAULT_LOCK_WAIT_MS = 10_000;
// 이 시각보다 오래된 락 파일은 죽은 프로세스가 남긴 것으로 보고 걷어낸다.
// 살아 있는 보유자의 최장 보유 시간은 발급 요청 8초 + 캐시 쓰기이므로 15초면
// 살아 있는 보유자를 밀어낼 위험 없이 고아만 걷어낸다.
export const DEFAULT_LOCK_STALE_MS = 15_000;
// 대기 중 캐시를 다시 읽는 간격. 락 보유자가 캐시를 쓴 직후 대기자가 통과하기까지의
// 지연이 이 값이다. 발급 자체가 보통 수백 ms라 50ms면 사람이 느낄 만큼 늘어나지 않고,
// 대기자 하나가 10초 동안 읽는 횟수도 200회에 그친다.
export const DEFAULT_LOCK_POLL_MS = 50;

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class KisTokenCache {
  constructor({
    filePath,
    lockPath = `${filePath}.lock`,
    ttlMarginMs = DEFAULT_TOKEN_TTL_MARGIN_MS,
    lockWaitMs = DEFAULT_LOCK_WAIT_MS,
    lockStaleMs = DEFAULT_LOCK_STALE_MS,
    pollMs = DEFAULT_LOCK_POLL_MS,
    now = () => Date.now(),
    sleep = defaultSleep,
  } = {}) {
    // 값은 전부 생성자 인자로만 받는다. 환경변수 노브를 새로 만들지 않은 이유는
    // 기본값을 바꿔야 할 운영 사례가 아직 없기 때문이다. 필요해지면 KIS_ 접두사로
    // 추가하면 된다 — finus_nat의 _MCP_ENV_ALLOWED_PREFIXES가 그 접두사를 통째로
    // 통과시키므로 NAT 경로까지 자동으로 전달된다.
    this.filePath = filePath;
    this.lockPath = lockPath;
    this.ttlMarginMs = ttlMarginMs;
    this.lockWaitMs = lockWaitMs;
    this.lockStaleMs = lockStaleMs;
    this.pollMs = pollMs;
    this.now = now;
    this.sleep = sleep;
    this.memory = null;
    // 락 파일이 놓일 디렉터리를 만들었는지 여부. 대기 중에는 이 함수가 pollMs마다
    // 불리므로 mkdir을 매번 걸지 않는다.
    this.lockDirEnsured = false;
    // 락 파일에 적어 두는 소유자 식별자. 해제할 때 "내가 잡은 락"인지 확인하는 데만
    // 쓴다(#releaseLock). pid를 쓰지 않는 이유는 캐시 경로가 컨테이너 사이에
    // 공유될 수 있어 pid가 유일하지 않기 때문이다.
    this.owner = crypto.randomUUID();
  }

  // 캐시 히트면 토큰 문자열, 아니면 null. 프로세스 안 메모리를 먼저 보고,
  // 없으면 파일을 읽는다. 파일 읽기 실패는 전부 캐시 미스로 떨어뜨린다 —
  // 이 캐시의 최악은 발급을 한 번 더 하는 것이라 fail-open이 맞는 자세다
  // (fail-closed인 order-dedup.js의 원장과 다르다: 그쪽은 중복 주문이 걸려 있다).
  read(now = this.now()) {
    if (this.memory && this.memory.expiresAt > now + this.ttlMarginMs) {
      return this.memory.token;
    }

    let cached;
    try {
      cached = JSON.parse(fs.readFileSync(this.filePath, "utf8"));
    } catch (error) {
      if (error.code !== "ENOENT") {
        console.error(`KIS token cache read failed: ${error.message}`);
      }
      return null;
    }

    if (
      cached &&
      typeof cached.token === "string" &&
      Number(cached.expiresAt) > now + this.ttlMarginMs
    ) {
      this.memory = { token: cached.token, expiresAt: Number(cached.expiresAt) };
      return this.memory.token;
    }

    return null;
  }

  // 임시 파일 + rename 원자적 쓰기. 부분 쓰기가 다른 프로세스에 노출되지 않게 한다.
  //
  // order-dedup.js의 #writeLedger와 달리 rename 재시도 사다리도, 고아 tmp 스윕도
  // 두지 않는다. 그쪽은 실패가 곧 주문 차단(fail-closed)이라 한 번의 실패도 비싸고
  // 주문마다 쓰기가 일어나지만, 이 캐시의 쓰기는 토큰 수명당 한 번뿐이고 실패해도
  // 이번 프로세스는 메모리의 토큰으로 계속 진행한다(다음 프로세스가 한 번 더 발급할
  // 뿐이다). 그래서 실패는 로그만 남기고 삼킨다 — 캐시 쓰기 실패로 이미 받아둔
  // 토큰을 버리면 그게 더 나쁘다.
  write(cache) {
    this.memory = { token: cache.token, expiresAt: Number(cache.expiresAt) };

    const dir = path.dirname(this.filePath);
    const tmpPath = path.join(
      dir,
      `.${path.basename(this.filePath)}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`,
    );

    try {
      fs.mkdirSync(dir, { recursive: true });
      const fd = fs.openSync(tmpPath, "w", 0o600);
      try {
        // writeSync는 짧은 쓰기를 낼 수 있다. fd를 받는 writeFileSync는 전체가
        // 쓰일 때까지 내부적으로 반복하므로 잘린 캐시가 남지 않는다.
        fs.writeFileSync(fd, JSON.stringify(cache));
        // rename은 메타데이터 순서만 보장한다. fsync 없이 전원이 나가면 rename만
        // 살아남아 빈 파일이 캐시 자리에 놓일 수 있다(파일시스템에 따라). 읽기가
        // fail-open이라 치명적이진 않지만, 한 줄로 막을 수 있는 구멍이다.
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      fs.renameSync(tmpPath, this.filePath);
    } catch (error) {
      // 어느 단계에서 실패했든 임시 파일을 남기지 않는다. 남기면 토큰이 담긴
      // 파일이 그대로 쌓인다.
      try {
        fs.unlinkSync(tmpPath);
      } catch {
        /* openSync 자체가 실패했으면 tmpPath가 없다(ENOENT). 정리 실패로 원인을 가리지 않는다. */
      }
      console.error(`KIS token cache write failed: ${error.message}`);
    }
  }

  // 캐시 히트면 그대로, 아니면 issue()로 발급해 캐시에 넣고 돌려준다.
  // 동시에 뜬 프로세스 중 실제로 issue()를 부르는 것은 락을 잡은 하나뿐이다.
  async getOrIssue(issue) {
    const hit = this.read();
    if (hit) return hit;

    const deadline = this.now() + this.lockWaitMs;
    let held = false;
    for (;;) {
      held = this.#tryAcquireLock();
      if (held) break;

      // 락 보유자가 캐시를 쓰는 즉시 통과한다 — 락이 풀리기를 기다리지 않는다.
      const late = this.read();
      if (late) return late;

      // 대기 상한을 넘겼다. 락을 못 잡았지만 여기서 포기하면 호출자는 토큰 없이
      // 실패한다. 락이 없던 예전 동작(무락 발급)으로 내려가는 쪽이 낫다.
      if (this.now() >= deadline) {
        console.error(`KIS token lock wait timed out (${this.lockWaitMs}ms); issuing without the lock`);
        break;
      }

      await this.sleep(this.pollMs);
    }

    try {
      // 락을 잡기까지 다른 프로세스가 발급을 끝냈을 수 있다. 여기서 한 번 더 읽는
      // 것이 직렬화의 핵심이다 — 이게 없으면 락은 발급을 줄 세우기만 하고
      // 발급 횟수는 그대로다.
      const afterLock = this.read();
      if (afterLock) return afterLock;

      const issued = await issue();
      // 토큰이 없는 결과를 그대로 통과시키면 호출부가 `Bearer undefined`로 조회를
      // 태우고, 원인이 만료·권한 문제로 보이는 KIS 오류로 둔갑한다. 발급 자리에서
      // 끊는다. (캐시에도 쓰지 않는다 — read()가 어차피 문자열이 아닌 토큰을
      // 미스로 보지만, 쓸 이유 자체가 없다.)
      if (typeof issued?.token !== "string" || !issued.token) {
        throw new Error("KIS 토큰 발급 결과에 access token이 없습니다.");
      }

      this.write(issued);
      return issued.token;
    } finally {
      if (held) this.#releaseLock();
    }
  }

  // O_EXCL 생성으로 락을 잡는다. 이미 있으면 낡았을 때만 걷어내고 한 번 더 시도한다.
  // 생성 실패(EEXIST 외)는 "락을 쓸 수 없는 환경"으로 보고 false를 돌려준다 —
  // 호출부가 무락 발급으로 내려가므로 락 파일을 못 만드는 환경에서도 토큰은 나온다.
  #tryAcquireLock({ allowStaleTakeover = true } = {}) {
    try {
      if (!this.lockDirEnsured) {
        fs.mkdirSync(path.dirname(this.lockPath), { recursive: true });
        this.lockDirEnsured = true;
      }
      // 락의 성립 자체는 파일의 존재(O_EXCL 생성 성공)이고, 내용은 두 가지 보조
      // 용도다 — owner는 해제 시 소유권 확인용, pid·at은 사람이 들여다볼 때의 진단용.
      fs.writeFileSync(this.lockPath, JSON.stringify({ owner: this.owner, pid: process.pid, at: this.now() }), {
        flag: "wx",
        mode: 0o600,
      });
      return true;
    } catch (error) {
      if (error.code !== "EEXIST") {
        console.error(`KIS token lock unavailable (${error.code || error.name}): ${error.message}`);
        return false;
      }
    }

    if (!allowStaleTakeover) return false;

    // 낡은 락만 걷어낸다. mtime을 쓰는 이유는 파일 내용(pid·at)이 부분 쓰기나
    // 다른 구현으로 깨져 있어도 판정이 성립하기 때문이다.
    try {
      const ageMs = this.now() - fs.statSync(this.lockPath).mtimeMs;
      if (ageMs <= this.lockStaleMs) return false;
      console.error(`KIS token lock looked stale (${Math.round(ageMs)}ms); taking it over`);
      fs.unlinkSync(this.lockPath);
    } catch {
      // stat/unlink 실패는 대개 다른 프로세스가 방금 락을 푼 경우(ENOENT)다.
      // 어느 쪽이든 아래에서 한 번만 더 잡아 보고, 실패하면 대기로 돌아간다.
    }

    // 재시도는 한 번뿐이다(allowStaleTakeover: false). 여러 프로세스가 같은 낡은
    // 락을 동시에 걷어내면 그중 하나만 잡는데, 여기서 다시 낡음 판정으로 들어가면
    // 방금 잡힌 "살아 있는" 락을 두고 무한히 맴돌 수 있다.
    return this.#tryAcquireLock({ allowStaleTakeover: false });
  }

  // 내가 잡은 락일 때만 지운다. 발급이 lockStaleMs를 넘겨 다른 프로세스가 내 락을
  // 낡은 것으로 걷어내고 자기 락을 세웠다면, 여기서 무심코 지우는 순간 그 프로세스의
  // 상호 배제가 깨진다. 읽기와 unlink 사이가 원자적이진 않지만(그 사이에 락이 다시
  // 바뀔 수 있다) 창이 훨씬 좁아지고, 최악이어도 락이 없던 예전 동작으로 돌아갈 뿐이다.
  #releaseLock() {
    try {
      const held = JSON.parse(fs.readFileSync(this.lockPath, "utf8"));
      if (held?.owner !== this.owner) return;
      fs.unlinkSync(this.lockPath);
    } catch (error) {
      // 낡은 락으로 판정돼 다른 프로세스가 이미 걷어냈을 수 있다(ENOENT).
      if (error.code !== "ENOENT") {
        console.error(`KIS token lock release failed: ${error.message}`);
      }
    }
  }
}
