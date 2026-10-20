// KIS 조회 응답의 종목코드(pdno)를 종목마스터 항목과 맞대는 규칙(#433).
//
// 확정된 사실(2026-10-20 모의투자 실측, #433 코멘트):
//   - ETN은 주문을 `Q530116`처럼 접두사 Q가 붙은 7자 코드로 내지만, 잔고 조회
//     (inquire-balance, VTTC8434R) output1과 일별 주문체결조회(inquire-daily-ccld,
//     VTTC0081R) output1의 `pdno`는 `530116`처럼 Q가 빠진 6자로 온다.
//   - 신형우선주(`00088K`)·신규 영숫자 6자(`0001A0`)·숫자 6자(`005930`)는 보낸 코드
//     그대로 돌아온다.
//   - 응답에는 상품군을 알려 주는 값이 없다. 잔고 output1에는 상품유형 필드 자체가 없고,
//     주문체결 output1의 `prdt_type_cd`는 모든 행에서 빈 문자열이었다. 그래서 "이 행이
//     ETN인가"는 응답만 보고 정할 수 없다.
//
// 그래서 판정 근거는 응답이 아니라 **비교 대상인 마스터 항목**의 증권그룹구분코드
// (`scrt_grp_cls_code`)다. 마스터가 ETN(`EN`)이라고 말하는 항목만 "Q를 뗀 6자"를 같은
// 종목으로 인정한다. 응답 코드에 Q를 덧붙여 추측하지 않는다 — 6자 숫자 응답 코드는 그
// 자체로는 ETN인지 알 수 없으므로, 숫자 6자 마스터 항목(주식)과 비교할 때 응답을 ETN으로
// 오인하는 쪽(Q를 붙이는 쪽)은 막는다. 2026-10-20 마스터 전수 기준 `EN` 368종목은 모두
// `Q`+숫자 6자이고, 그 6자와 같은 코드를 가진 다른 마스터 항목은 0건이다.
//
// 막지 못하는 방향도 있다: 비교 대상이 ETN일 때, 마스터 밖 상품(예: ELW)이 그 ETN의
// 숫자부와 같은 6자 숫자를 쓰면 그 응답 행도 같은 종목으로 잡힌다. 응답에 상품군 값이 없어
// 둘을 가를 수 없다. ELW 등의 코드 대역이 ETN 숫자부와 실제로 겹치는지는 확인하지 못했다.
//
// 비교 전에 양쪽을 trim·대문자로 맞춘다. KIS·마스터 종목코드는 대문자와 숫자만 쓰고 공백이
// 없어, 이 정규화가 서로 다른 두 코드를 하나로 합치지 않는다. backend parse_snapshot이
// 이전부터 대소문자를 무시해 비교했고(holding.code.upper() == stock_code.upper()),
// resolveStock도 코드를 대소문자 무시로 찾으므로 두 구현을 그쪽으로 맞췄다.
//
// 보낸 그대로의 형태(`Q530116`)도 계속 인정한다. 실전 서버와 실현손익 TR
// (inquire-balance-rlz-pl, 모의투자 미지원)의 응답 형태는 실측하지 못했기 때문이다.
//
// 이 규칙의 판정표는 tests/fixtures/kis_response_pdno_policy.json이고, 같은 규칙을 쓰는
// backend/trading/stock_code.py의 kis_response_pdno_matches()와 두 스위트가 함께 읽는다.

export const ETN_SECURITY_GROUP = "EN";
const ETN_CODE_RE = /^Q[0-9]{6}$/;

// 마스터 항목이 ETN이고 코드가 `Q`+숫자 6자일 때만 접두사를 뗀 6자를 돌려준다.
// 그 밖에는 null — 응답 코드가 보낸 코드와 다를 근거가 없는 항목이다.
function normalizeCode(value) {
  return String(value ?? "").trim().toUpperCase();
}

export function etnResponsePdno(stock) {
  const code = normalizeCode(stock?.code);
  if (stock?.scrt_grp_cls_code !== ETN_SECURITY_GROUP || !ETN_CODE_RE.test(code)) {
    return null;
  }
  return code.slice(1);
}

// 응답 행의 pdno가 마스터 항목(stock)과 같은 종목인가.
export function kisResponsePdnoMatches(pdno, stock) {
  const value = normalizeCode(pdno);
  if (!value) return false;
  if (value === normalizeCode(stock?.code)) return true;
  const etnPdno = etnResponsePdno(stock);
  return etnPdno !== null && value === etnPdno;
}

// 일별 주문체결조회의 PDNO 입력값. ETN은 빈 값(전체 조회)을 보내고 응답을
// kisResponsePdnoMatches로 거른다. 모의투자에서 `PDNO=Q500035`는 그 종목의 주문이 있는데도
// 70070000(조회할 내역 없음)을 돌려줬고, `PDNO=500035`는 해당 행을 돌려줬다. 실전 서버가
// 어느 형태를 받는지는 확인하지 못해, 서버 필터에 기대지 않고 받은 행을 직접 거른다.
export function dailyCcldPdnoParam(stock) {
  return etnResponsePdno(stock) === null ? String(stock?.code ?? "") : "";
}
