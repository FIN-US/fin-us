import { formatQuantity, formatWon } from "./formatters.js";

/**
 * 매수가능조회(inquire-psbl-order, v1_국내주식-007) 요청 파라미터.
 *
 * 이 TR이 필요한 이유는 이슈 #310이다. inquire-balance의 `dnca_tot_amt`(예수금총금액)는
 * 미수·증거금·미결제 정산이 반영되지 않은 값이라 "지금 낼 수 있는 현금"이 아니다.
 * 실제 주문가능현금은 이 TR의 `ord_psbl_cash` 하나뿐이고, backend/order_assist.py의
 * 하드 한도 두 개(insufficient_cash, cash_floor)가 그 값 위에 선다.
 *
 * PDNO는 선택이 아니라 필수다 — KIS가 종목별 증거금률로 가능수량을 계산하기 때문이다.
 * `ord_psbl_cash` 자체는 계좌 단위 값이지만, 종목 없이 이 TR을 부를 방법은 없다.
 *
 * 두 포함 여부 플래그는 모두 "N"으로 고정한다. 포함(`Y`)하면 주문가능액이 **커지고**
 * 그만큼 위 두 한도가 헐거워진다. 확인하지 못한 여유를 한도에 얹지 않는 것이
 * order_assist 전체의 fail-closed 방향과 같다.
 * - CMA_EVLU_AMT_ICLD_YN: CMA 평가금액 포함 여부
 * - OVRS_ICLD_YN: 해외 포함 여부
 *
 * ORD_DVSN은 지정가 "00" / 시장가 "01"이다(mcp-trading/order.js의 주문 TR과 같은 코드
 * 체계). 시장가 기준일 때 ORD_UNPR은 KIS가 무시하므로 "0"을 보낸다.
 *
 * @param {string} accountNo 계좌번호 10자리(앞 8자리 + 상품코드 2자리)
 * @param {object} options
 * @param {string} options.stockCode 6자리 종목코드
 * @param {number} [options.price] 지정가(원). orderType이 LIMIT일 때만 실린다
 * @param {"LIMIT"|"MARKET"} [options.orderType] 기준 주문유형 (기본 MARKET)
 */
export function buildOrderableCashParams(accountNo, { stockCode, price = 0, orderType = "MARKET" } = {}) {
  const ordDvsn = orderType === "LIMIT" ? "00" : "01";
  return {
    CANO: accountNo.substring(0, 8),
    ACNT_PRDT_CD: accountNo.substring(8, 10),
    PDNO: stockCode,
    ORD_UNPR: ordDvsn === "00" ? String(price) : "0",
    ORD_DVSN: ordDvsn,
    CMA_EVLU_AMT_ICLD_YN: "N",
    OVRS_ICLD_YN: "N",
  };
}

/**
 * 매수가능조회 결과 리포트.
 *
 * "- 주문가능금액: 1,000,000원" 줄은 backend/order_assist.py의 `_CASH_RE`가 직접 읽는
 * 출력 계약이다. 라벨을 바꾸면 그쪽에서 cash=None으로 떨어져 /advise가 전부 거부된다
 * (조용히 헐거워지지는 않는다 — fail-closed). 아래 tests/orderable-cash.test.js가 이
 * 줄의 모양을 고정한다.
 *
 * 라벨이 겹치지 않게 주의해야 한다: `_CASH_RE`는 "주문가능금액:"을 문서 어디서든 찾는
 * 첫 매치로 읽으므로, 다른 필드에 "주문가능금액"이 들어간 라벨을 붙이면 안 된다.
 * (그래서 `ord_psbl_sbst`는 "주문가능대용", 수량 계열은 "…수량"으로만 적는다.)
 *
 * 값을 읽지 못하면 formatWon/formatQuantity가 "-"를 내고, 그 줄은 `_CASH_RE`에
 * 매치되지 않아 역시 fail-closed로 끊긴다.
 */
export function formatOrderableCashReport({ output, stock, trId, orderType, price }) {
  const data = output || {};
  const basis = orderType === "LIMIT" ? `지정가 ${formatWon(price)}` : "시장가";

  return `
[주문가능조회] ${stock.name} (${stock.code})
- 조회 TR: ${trId} (매수가능조회 v1_국내주식-007, inquire-psbl-order)
- 기준 주문유형: ${basis}
- 주문가능금액: ${formatWon(data.ord_psbl_cash)}
- 미수없는매수금액: ${formatWon(data.nrcvb_buy_amt)} | 미수없는매수수량: ${formatQuantity(data.nrcvb_buy_qty)}주
- 최대매수금액: ${formatWon(data.max_buy_amt)} | 최대매수수량: ${formatQuantity(data.max_buy_qty)}주
- 주문가능대용: ${formatWon(data.ord_psbl_sbst)} | 재사용가능금액: ${formatWon(data.ruse_psbl_amt)}
- 기준 데이터: 한국투자증권 Open API inquire-psbl-order (CMA·해외 미포함)
  `.trim();
}
