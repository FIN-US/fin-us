import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const DEFAULT_ORDER_DEDUP_TTL_MS = 120_000;
const DEFAULT_ORDER_DEDUP_PATH = path.join(os.tmpdir(), "finus-kis-order-dedup.json");

export class DuplicateOrderError extends Error {
  constructor(entry) {
    super("중복 주문 방지를 위해 동일 주문 재요청을 차단했습니다. 잠시 후 주문 내역을 확인하세요.");
    this.name = "DuplicateOrderError";
    this.entry = entry;
  }
}

// reserve()·markSucceeded()·release() 모두 내부적으로 원장을 읽으므로, 이
// 에러는 "새 주문이 차단됨"만을 뜻하지 않는다 - markSucceeded/release에서
// 발생하면 KIS 주문 자체는 이미 끝난 뒤일 수 있다. 그래서 메시지는 호출
// 맥락과 무관하게 참인 사실(경로, 손상 가능성, 삭제하면 복구된다는 것)만
// 담는다. DuplicateOrderError와 마찬가지로 타입을 노출해 호출자가
// instanceof로 구분할 수 있게 한다.
export class LedgerUnreadableError extends Error {
  constructor(filePath, reason) {
    super(
      `KIS 주문 중복 방지 원장을 읽을 수 없습니다: ${filePath}\n` +
        "원장이 손상되었을 수 있습니다. 이 파일을 삭제하면 정상 상태로 복구됩니다.",
    );
    this.name = "LedgerUnreadableError";
    this.filePath = filePath;
    this.reason = reason;
  }
}

export function createOrderDedupKey({
  accountNo,
  orderEnv,
  stockCode,
  side,
  quantity,
  price,
  orderType,
}) {
  const normalizedOrderType = String(orderType ?? "LIMIT").trim().toUpperCase();
  const payload = {
    accountNo: String(accountNo ?? "").trim(),
    orderEnv: String(orderEnv ?? "demo").trim().toLowerCase(),
    stockCode: String(stockCode ?? "").trim(),
    side: String(side ?? "").trim().toUpperCase(),
    quantity: String(quantity ?? "").trim(),
    price: normalizedOrderType === "MARKET" ? "0" : String(price ?? 0).trim(),
    orderType: normalizedOrderType,
  };

  return crypto.createHash("sha256").update(JSON.stringify(payload)).digest("hex");
}

// rename 실패 중 이 코드들만 재시도한다. 이 코드로 실패해도 원장은 멀쩡하고
// 임시 파일에는 완성된 내용이 그대로 있으므로(rename은 이름표만 바꾸는
// 연산), 재시도는 멱등하고 중복 주문 위험이 없다. Docker Compose에서는
// `.:/app` 바인드 마운트로 원장이 Windows 호스트 경로(`.state\`)에 놓이므로
// Defender·검색 인덱서 같은 외부 보유자가 붙잡고 있을 가능성이 실질적이다.
// 읽기 경로·형태 가드는 재시도하지 않는다 - 그쪽은 fail-closed가 맞는 자세다.
//
// EPERM/EBUSY는 어느 플랫폼에서든 외부 보유자로 인한 일시적 실패일 수 있다.
// EACCES는 다르다 - POSIX에서 rename의 EACCES는 "대상 디렉터리에 쓰기 권한
// 없음"이라 결정론적이고, 몇 번을 더 시도해도 나아지지 않는다. README가 직접
// 적어둔 시나리오(backend/Dockerfile에 USER 지시자가 추가되는 순간)가 바로
// 이 경우라, 그 상황에서 재시도하면 모든 주문마다 이벤트 루프를 ~75ms 헛되이
// 블로킹하고 어차피 실패할 신호만 늦춘다 - ENOSPC를 재시도 대상에서 뺀 것과
// 같은 논리다. Windows에서는 MoveFileEx가 보유자 충돌에도 EACCES를 내므로
// 그때만 재시도한다.
//
// 순수 함수로 뽑아둔다: 모듈 상수(RENAME_RETRY_CODES)는 로드 시점의
// process.platform으로 딱 한 번 계산되므로, 실제 OS를 바꾸거나 모듈을 다시
// 불러오지 않고도 이 분기 자체를 직접 테스트할 수 있게 하기 위해서다.
export function renameRetryCodesForPlatform(platform) {
  return new Set(platform === "win32" ? ["EPERM", "EACCES", "EBUSY"] : ["EPERM", "EBUSY"]);
}

const RENAME_RETRY_CODES = renameRetryCodesForPlatform(process.platform);
const RENAME_MAX_ATTEMPTS = 5;
const RENAME_RETRY_BACKOFF_MS = [10, 15, 20, 30];
// 마지막 시도 뒤에는 자지 않으므로 백오프 배열은 시도 수보다 정확히 하나
// 적어야 한다. 어긋나면 #renameLedgerAtomic의 인덱싱이 조용히 틀어지므로
// 여기서 바로 드러낸다.
console.assert(
  RENAME_RETRY_BACKOFF_MS.length === RENAME_MAX_ATTEMPTS - 1,
  `RENAME_RETRY_BACKOFF_MS 길이(${RENAME_RETRY_BACKOFF_MS.length})는 RENAME_MAX_ATTEMPTS-1(${RENAME_MAX_ATTEMPTS - 1})과 같아야 합니다.`,
);

// Atomics.wait로 현재 스레드를 동기적으로 재운다. 이 모듈의 쓰기 경로는
// 전부 동기 API(fs.*Sync)라, 비동기 setTimeout으로는 재시도 사이 간격을 줄
// 수 없다.
function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function parsePositiveInteger(value, fallback, name = "값") {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    // 공백만 있는 값도 빈 문자열과 마찬가지로 "설정하지 않음"으로 취급한다.
    if (value !== undefined && String(value).trim() !== "") {
      console.error(
        `${name}="${value}"는 양의 정수(ms)가 아니어서 기본값 ${fallback}ms를 사용합니다.`,
      );
    }
    return fallback;
  }
  return parsed;
}

export class OrderDedupStore {
  constructor({
    filePath = process.env.KIS_ORDER_DEDUP_PATH || DEFAULT_ORDER_DEDUP_PATH,
    ttlMs = parsePositiveInteger(
      process.env.KIS_ORDER_DEDUP_TTL_MS,
      DEFAULT_ORDER_DEDUP_TTL_MS,
      "KIS_ORDER_DEDUP_TTL_MS",
    ),
    now = () => Date.now(),
  } = {}) {
    this.filePath = filePath;
    this.ttlMs = ttlMs;
    this.now = now;
  }

  reserve(key, request) {
    const now = this.now();
    const ledger = this.#readLedger(now);
    const existing = ledger[key];
    if (existing && Number(existing.expiresAt) > now) {
      throw new DuplicateOrderError(existing);
    }

    const entry = {
      status: "in_flight",
      reservedAt: now,
      expiresAt: now + this.ttlMs,
      request,
    };
    ledger[key] = entry;
    this.#writeLedger(ledger);
    return entry;
  }

  markSucceeded(key, response) {
    const now = this.now();
    const ledger = this.#readLedger(now);
    const existing = ledger[key];
    if (!existing) return;

    ledger[key] = {
      ...existing,
      status: "succeeded",
      completedAt: now,
      response,
    };
    this.#writeLedger(ledger);
  }

  release(key) {
    const ledger = this.#readLedger(this.now());
    if (!ledger[key]) return;
    delete ledger[key];
    this.#writeLedger(ledger);
  }

  // ENOENT만 "원장 없음"으로 조용히 통과시킨다(신규 설치 정상 상태). 그 외
  // 모든 읽기/파싱 실패와 형태 불일치는 던진다 — 이 원장은 중복 주문을 막는
  // 마지막 방어선이라, 손상된 원장을 빈 원장으로 취급하면 중복 주문을
  // 조용히 통과시키게 된다(fail-open). 손상 시에는 사람이 개입할 때까지
  // 모든 주문을 막는 쪽(fail-closed)이 올바른 자세다. (#128)
  #readLedger(now) {
    let ledger;
    try {
      ledger = JSON.parse(fs.readFileSync(this.filePath, "utf8"));
    } catch (error) {
      if (error.code === "ENOENT") {
        return {};
      }
      throw this.#ledgerReadError(error.code || error.name);
    }

    if (!ledger || typeof ledger !== "object" || Array.isArray(ledger)) {
      throw this.#ledgerReadError("INVALID_SHAPE");
    }

    // 알려진 미해결 결함(#128 "본문에 없던 결함 2건", 의도적으로 남겨둠):
    // 1) 만료 정리(여기)와 쓰기(#writeLedger)가 결합돼 있어, 이 호출이 만료된
    //    항목을 걸러낸 결과를 markSucceeded/release가 그대로 저장하면 무관한
    //    활성 예약까지 지워질 수 있다. 동시성이 필요해 현재 배포 불변식(단일
    //    writer) 아래서는 도달 불가. 원자적 쓰기로는 닫히지 않는다 - 키별
    //    파일로 나눠야 닫힌다.
    // 2) KIS_ORDER_DEDUP_TTL_MS를 KIS 응답 지연보다 짧게 설정하면, 이 필터가
    //    markSucceeded 호출 전에 이미 항목을 걷어내 성공 기록이 조용히
    //    사라진다. 기본값(120초)에서는 도달 불가.
    return Object.fromEntries(
      Object.entries(ledger).filter(([, entry]) => Number(entry?.expiresAt) > now),
    );
  }

  // 원장 항목에는 CANO(계좌번호)가 평문으로 들어 있으므로, 던지는 메시지와
  // 로그 모두 원장 내용(entry, error.message 포함 - JSON.parse 실패 메시지는
  // 엔진에 따라 입력값 일부를 포함할 수 있다)을 절대 담지 않는다. 경로와
  // 조치 방법만 담는다.
  #ledgerReadError(reason) {
    console.error(`KIS order dedup ledger read failed (${reason}): ${this.filePath}`);
    return new LedgerUnreadableError(this.filePath, reason);
  }

  // 같은 디렉터리에 임시 파일을 만들고 fsync한 뒤 rename으로 경로만
  // 갈아치우는 원자적 쓰기. 디렉터리 fsync는 하지 않는다(Windows에서 EPERM).
  // 임시 파일 실패나 rename 실패는 그대로 던져 reserve()를 통해 주문을
  // 차단한다 — 쓰기 경로는 기존부터 fail-closed였고 이 변경으로도 유지된다.
  //
  // 읽기 fail-closed 없이 이것만 넣으면 새로운 fail-open이 생긴다: rename은
  // 파일이 아니라 디렉터리 쓰기 권한만 있으면 되므로, 읽을 수 없는 손상된
  // 원장이 조용히 빈 원장으로 교체될 수 있다. 그래서 두 수정은 함께 간다.
  //
  // open→write→fsync→rename 전체를 하나의 try로 감싼다. 어느 단계에서
  // 실패하든(rename뿐 아니라 write·fsync도) 임시 파일이 남아서는 안 된다 -
  // 원장과 같은 내용(CANO 포함)을 담은 파일이 남으면 그 자체로 유출 표면이고,
  // ENOSPC처럼 디스크가 가득 찬 상황에서는 실패한 시도마다 고아 파일이 쌓여
  // 원인을 더 악화시킨다.
  #writeLedger(ledger) {
    const dir = path.dirname(this.filePath);
    fs.mkdirSync(dir, { recursive: true });

    const tmpPath = path.join(
      dir,
      `.${path.basename(this.filePath)}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`,
    );

    try {
      const fd = fs.openSync(tmpPath, "w", 0o600);
      try {
        // writeSync는 시스템 콜 한 번이라 반환값(실제로 쓰인 바이트 수)을
        // 무시하면 짧은 쓰기가 조용히 잘린 원장을 남길 수 있다. writeFileSync는
        // fd를 받아도 전체 데이터가 쓰일 때까지 내부적으로 반복한다. 짧은
        // 쓰기로 잘린 원장은 이 변경의 fail-closed 읽기와 만나 사람이 지울
        // 때까지 모든 주문을 막아버리므로, 여기서 확실히 막는다.
        fs.writeFileSync(fd, JSON.stringify(ledger));
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      this.#renameLedgerAtomic(tmpPath);
    } catch (error) {
      try {
        fs.unlinkSync(tmpPath);
      } catch {
        // 정리 실패는 무시한다 - openSync 자체가 실패했다면 tmpPath가 애초에
        // 없어 ENOENT가 나는 정상 경로이고, 그 외의 경우에도 정리 실패로
        // 원래 실패 원인을 가리면 안 된다.
      }
      throw error;
    }
  }

  // EPERM/EACCES/EBUSY는 대상을 붙잡고 있는 외부 프로세스(백신, 검색
  // 인덱서 등)가 원인일 가능성이 크고, 이 코드는 대상 파일을 열어둔 채로 두지
  // 않는다(#readLedger는 매번 새로 열고 닫는다) - 그래서 짧은 재시도가
  // 안전하다. rename은 이름표만 바꾸는 연산이라 실패해도 tmpPath에는 완성된
  // 내용이 그대로 남아 재시도는 멱등하고, 원장 자체는 손상되지 않는다(그
  // 다음 항상 성공하는 것도 아니지만, 실패 시 tmpPath 정리는 호출자
  // (#writeLedger)의 catch가 맡는다). 읽기·형태 가드는 재시도하지 않는다 -
  // 그쪽은 fail-closed가 맞는 자세다.
  #renameLedgerAtomic(tmpPath) {
    for (let attempt = 1; attempt <= RENAME_MAX_ATTEMPTS; attempt += 1) {
      try {
        fs.renameSync(tmpPath, this.filePath);
        return;
      } catch (error) {
        const isLastAttempt = attempt === RENAME_MAX_ATTEMPTS;
        if (!RENAME_RETRY_CODES.has(error.code) || isLastAttempt) {
          throw error;
        }
        sleepSync(RENAME_RETRY_BACKOFF_MS[attempt - 1]);
      }
    }

    // 루프는 항상 위의 return이나 throw로 끝난다. 여기 도달했다면
    // RENAME_MAX_ATTEMPTS가 1 미만으로 잘못 바뀐 것이다. 조용히 undefined를
    // 반환하면 #writeLedger가 이를 "쓰기 성공"으로 오인해, fail-closed가
    // 존재 이유인 모듈에서 유일하게 조용히 통과하는 경로가 생긴다.
    throw new Error(`RENAME_MAX_ATTEMPTS(${RENAME_MAX_ATTEMPTS})가 1 이상이어야 합니다.`);
  }
}
