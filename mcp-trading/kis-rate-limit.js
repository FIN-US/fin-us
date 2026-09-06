// 이슈 #210: KIS 유량 제한 실측을 "가능하게" 만드는 순수 모듈.
//
// 이 파일은 유량 제한을 제어하지 않는다. 제어(페이지 간 대기·토큰 버킷·백오프)를 설계하려면
// 실제 임계치(초당 호출 수)와 페이지당 지연을 알아야 하는데, 그 숫자가 지금 아무 데도 없다
// (index.js의 90초/15초 예산 주석이 "측정치가 없어"라고 직접 밝힌다). 그래서 여기서 하는 일은
// 딱 두 가지다.
//   (1) KIS 응답이 유량 제한인지 분류한다.
//   (2) 요청 1건을 stderr 한 줄로 구조화해 남긴다 — 운영자가 실계좌에서 그 줄을 모아
//       임계치를 역산할 수 있게.
//
// I/O가 없고 시각도 호출자가 elapsedMs로 넘긴다(balance.js의 `now` 주입과 같은 이음매).
// index.js는 import 시점에 StdioServerTransport를 연결하는 부작용이 있어 테스트가 직접
// import할 수 없으므로, 로직은 전부 이 파일에 두고 index.js에는 배선만 남긴다.

// 유량 제한 msg_cd. 두 갈래를 모두 본다.
// - EGW00201: 한국투자증권 공식 GitHub README가 "초당 거래건수 초과"로 명시한 코드
//   (index.js 상단 조사 주석 참조).
// - EGW00133: 같은 계열의 유량 제한 코드. 지금은 tests/kis-client.test.js의 픽스처 문구로만
//   저장소에 존재하고 프로덕션 코드 어디에서도 감지되지 않는다.
export const KIS_RATE_LIMIT_MSG_CODES = Object.freeze(["EGW00201", "EGW00133"]);

// msg_cd가 아니라 msg1 본문으로도 판정한다. msg_cd 목록이 완전하다는 보장이 없기 때문이다 —
// docs/issue-138-alnum-stock-code.md:495가 기록하듯 공식 msg_cd FAQ 페이지에 접근하지
// 못했고, 그래서 "EGW002xx 계열이 이 둘뿐"이라는 것을 확인할 방법이 없다. 한국어 본문은
// 그 미확인을 메우는 두 번째 증거 경로다(둘 중 하나만 걸려도 유량 제한으로 본다).
export const KIS_RATE_LIMIT_MSG1_MARKER = "초당 거래건수";

export const KIS_CLASS_OK = "ok";
export const KIS_CLASS_RATE_LIMIT = "rate_limit";
export const KIS_CLASS_OTHER = "other";

// 로그 한 줄의 접두사. 운영자가 컨테이너 로그에서 이 토큰으로 grep한다
// (docs/issue-210-rate-limit-observation.md).
export const KIS_REQUEST_LOG_PREFIX = "[kis-req]";

// msg1은 KIS 서버가 준 한국어 문자열이다. 계좌번호(CANO, 8자리)처럼 자릿수가 긴 숫자가
// 그대로 되울려 올 가능성을 배제할 수 없으므로 8자리 이상 연속 숫자는 전부 가린다.
// 8을 경계로 잡은 이유: CANO가 8자리이고, 종목코드(6자리)·수량·가격 같은 진짜로 보고 싶은
// 값들은 대부분 그보다 짧다. 자릿수는 남기고 값만 지운다.
const LONG_DIGIT_RUN = /\d{8,}/g;

// 한 줄 로그에 실을 수 있는 msg1 길이 상한. 넘치면 잘라 낸다 — 길이 자체가 문제라기보다,
// 상한이 없으면 서버가 준 임의 길이 문자열이 로그 한 줄을 통째로 삼킨다.
const MSG1_MAX_LENGTH = 120;

// 토큰형 필드(tr_id, msg_cd, 에러 코드)에 허용하는 문자. 이 밖의 문자가 섞이면 통째로 버린다.
// tr_id·msg_cd는 KIS가 정한 영숫자 코드라 이 집합을 벗어날 이유가 없고, 벗어난 값을 그대로
// 흘리면 공백·개행이 섞여 "한 줄 = 한 요청" 규약이 깨진다.
const SAFE_TOKEN = /^[A-Za-z0-9_.:-]{1,40}$/;

const ABSENT = "-";

/**
 * 8자리 이상 연속 숫자를 같은 길이의 `*`로 가린다.
 * @param {unknown} text
 * @returns {string}
 */
export function maskLongDigitRuns(text) {
  return String(text ?? "").replace(LONG_DIGIT_RUN, (run) => "*".repeat(run.length));
}

function sanitizeToken(value) {
  const text = String(value ?? "").trim();
  return SAFE_TOKEN.test(text) ? text : ABSENT;
}

// msg1을 로그 한 줄에 실을 수 있는 형태로 만든다: 공백류(개행 포함)를 한 칸으로 접고,
// 긴 숫자를 가리고, 길이를 자른다. 실제 출력은 JSON.stringify로 따옴표를 씌워 나가므로
// 여기서 남은 특수문자도 이스케이프된다.
function sanitizeMsg1(value) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  const masked = maskLongDigitRuns(text);
  return masked.length > MSG1_MAX_LENGTH ? `${masked.slice(0, MSG1_MAX_LENGTH)}...` : masked;
}

/**
 * 유량 제한 여부. msg_cd 목록과 msg1 본문 중 하나만 걸려도 참이다(위 상수 주석의 이유).
 * @param {{ msgCd?: unknown, msg1?: unknown }} [input]
 * @returns {boolean}
 */
export function isKisRateLimitError({ msgCd, msg1 } = {}) {
  const code = String(msgCd ?? "").trim().toUpperCase();
  if (KIS_RATE_LIMIT_MSG_CODES.includes(code)) return true;
  return String(msg1 ?? "").includes(KIS_RATE_LIMIT_MSG1_MARKER);
}

/**
 * KIS 오류를 분류한다. 지금 필요한 구분은 "유량 제한인가 아닌가" 하나뿐이므로 두 값만 낸다 —
 * 이 PR이 만들려는 데이터가 유량 제한 발생 빈도·시각이기 때문이다.
 * @param {{ msgCd?: unknown, msg1?: unknown }} [input]
 * @returns {"rate_limit"|"other"}
 */
export function classifyKisError(input = {}) {
  return isKisRateLimitError(input) ? KIS_CLASS_RATE_LIMIT : KIS_CLASS_OTHER;
}

/**
 * KIS 요청 1건을 stderr 한 줄로 만든다.
 *
 * **비밀 위생이 이 함수의 계약이다.** axios 에러 객체는 error.config에 params(CANO 계좌번호)와
 * headers(appkey, appsecret, authorization: Bearer 토큰)를 통째로 들고 있다(balance.js:201-206이
 * 같은 이유로 error.message만 남긴다). 그래서 이 함수는 입력에서 아래 화이트리스트만 읽는다.
 *   response.status / error.response.status          — 숫자
 *   response.data.{rt_cd,msg_cd,msg1} / error.response.data.{...} — KIS가 준 응답 바디
 *   error.code                                       — axios 에러 코드(ECONNABORTED 등)
 * error.config, error.request, error.message, headers, params는 읽지 않는다. error.message를
 * 뺀 이유도 같다 — 축적된 문자열이라 무엇이 섞여 있는지 이 함수가 보장할 수 없다. 대신
 * error.code를 토큰 화이트리스트로 걸러 낸다.
 *
 * @param {object}  input
 * @param {string}  input.trId       tr_id
 * @param {number}  input.elapsedMs  요청 소요 시간(ms). 호출자가 잰다.
 * @param {object} [input.response]  axios 응답 (성공 시)
 * @param {object} [input.error]     axios 에러 (실패 시)
 * @returns {{ line: string, classification: string, rateLimited: boolean, msgCd: string|null }}
 */
export function formatKisRequestLog({ trId, elapsedMs, response = null, error = null } = {}) {
  const body = response?.data ?? error?.response?.data ?? null;
  const rtCd = body && typeof body === "object" ? body.rt_cd : undefined;
  const rawMsgCd = body && typeof body === "object" ? body.msg_cd : undefined;
  const rawMsg1 = body && typeof body === "object" ? body.msg1 : undefined;

  const httpStatus = response?.status ?? error?.response?.status;
  const failed = Boolean(error) || (rtCd !== undefined && String(rtCd) !== "0");
  const classification = failed
    ? classifyKisError({ msgCd: rawMsgCd, msg1: rawMsg1 })
    : KIS_CLASS_OK;

  const fields = [
    `tr_id=${sanitizeToken(trId)}`,
    `elapsed_ms=${Number.isFinite(elapsedMs) && elapsedMs >= 0 ? Math.round(elapsedMs) : ABSENT}`,
    `http=${Number.isInteger(httpStatus) ? httpStatus : ABSENT}`,
    `rt_cd=${sanitizeToken(rtCd)}`,
    // msg_cd는 조건 없이 남긴다. 유량 제한 판정의 1차 증거이고, KIS가 정한 영숫자 코드라
    // 계정 정보가 실릴 자리가 아니다. index.js:212-213이 msg1이 있으면 msg_cd를 버리는
    // 바람에 지금까지 이 코드가 로그에 한 번도 남지 않았다.
    `msg_cd=${sanitizeToken(rawMsgCd)}`,
    `class=${classification}`,
  ];

  const errCode = sanitizeToken(error?.code);
  if (errCode !== ABSENT) fields.push(`err=${errCode}`);

  const msg1 = sanitizeMsg1(rawMsg1);
  if (msg1) fields.push(`msg1=${JSON.stringify(msg1)}`);

  return {
    line: `${KIS_REQUEST_LOG_PREFIX} ${fields.join(" ")}`,
    classification,
    rateLimited: classification === KIS_CLASS_RATE_LIMIT,
    msgCd: typeof rawMsgCd === "string" && rawMsgCd.trim() ? rawMsgCd.trim() : null,
  };
}

const TRUE_VALUES = new Set(["1", "true", "yes", "on"]);
const FALSE_VALUES = new Set(["0", "false", "no", "off"]);

/**
 * 요청별 타이밍 로그 on/off env. balance.js의 readPageDelayMsEnv와 같은 관례를 쓴다 —
 * `KIS_<NAME>`을 먼저 보고, 없으면 `FINUS_KIS_<NAME>`(둘 다 trim 후 첫 비어 있지 않은 값).
 * 값이 이상하면 조용히 삼키지 않고 stderr에 경고를 남기고 기본값으로 돌아간다(같은 파서 관례).
 *
 * 게이트를 두는 이유: 연속조회 1회가 최대 50페이지(DAILY_CCLD_MAX_PAGES)이고 그런 도구가
 * 셋이라 요청별 줄을 상시로 켜면 로그가 잠긴다. 유량 제한 분류는 이 게이트와 무관하게 항상
 * 남긴다(index.js) — 드물게 나고, 그게 이슈 #210이 찾는 신호 자체다.
 *
 * @param {string}  [name]     env 접미 이름
 * @param {boolean} [fallback] 기본값
 * @returns {boolean}
 */
export function readKisRequestLogEnv(name = "REQUEST_LOG", fallback = false) {
  let key = null;
  let raw = "";
  for (const candidate of [`KIS_${name}`, `FINUS_KIS_${name}`]) {
    const value = (process.env[candidate] || "").trim();
    if (value) {
      key = candidate;
      raw = value;
      break;
    }
  }
  if (!raw) return fallback;

  const normalized = raw.toLowerCase();
  if (TRUE_VALUES.has(normalized)) return true;
  if (FALSE_VALUES.has(normalized)) return false;

  // stdout은 MCP JSON-RPC 채널이라 console.log를 쓰면 프로토콜이 깨진다. stderr만 쓴다.
  console.error(
    `${key} 환경변수 값이 올바르지 않습니다(${JSON.stringify(raw)}) — ${[...TRUE_VALUES].join("/")} 또는 ${[...FALSE_VALUES].join("/")} 중 하나여야 합니다. 기본값 ${fallback}을 사용합니다.`,
  );
  return fallback;
}
