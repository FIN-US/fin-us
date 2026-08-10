function normalizeSide(side) {
  const normalized = String(side ?? "").trim().toUpperCase();
  if (normalized !== "BUY" && normalized !== "SELL") {
    throw new Error("side는 BUY 또는 SELL이어야 합니다.");
  }
  return normalized;
}

function normalizeOrderEnv(orderEnv) {
  const normalized = String(orderEnv ?? "demo").trim().toLowerCase();
  if (normalized !== "demo" && normalized !== "real") {
    throw new Error("order_env는 demo 또는 real이어야 합니다.");
  }
  return normalized;
}

function normalizeOrderType(orderType) {
  const normalized = String(orderType ?? "LIMIT").trim().toUpperCase();
  if (normalized !== "LIMIT" && normalized !== "MARKET") {
    throw new Error("order_type은 LIMIT 또는 MARKET이어야 합니다.");
  }
  return normalized;
}

function assertPositiveInteger(value, fieldName) {
  const number = Number(value);
  if (!Number.isInteger(number) || number <= 0) {
    throw new Error(`${fieldName}는 양의 정수여야 합니다.`);
  }
  return number;
}

export function selectCashOrderTrId({ orderEnv, side }) {
  const env = normalizeOrderEnv(orderEnv);
  const normalizedSide = normalizeSide(side);

  if (env === "demo") {
    return normalizedSide === "BUY" ? "VTTC0012U" : "VTTC0011U";
  }
  return normalizedSide === "BUY" ? "TTTC0012U" : "TTTC0011U";
}

export function validateOrderEnvMatchesUrl({ orderEnv, kisUrl }) {
  const env = normalizeOrderEnv(orderEnv);
  const url = String(kisUrl ?? "");
  const isPaperUrl = url.includes("openapivts");

  if (env === "demo" && !isPaperUrl) {
    throw new Error("모의투자 주문은 모의투자 KIS_URL(openapivts)이 필요합니다.");
  }
  if (env === "real" && isPaperUrl) {
    throw new Error("실계좌 주문은 실전 KIS_URL이 필요합니다.");
  }
}

export function validateRealOrderGuard({ orderEnv, realOrderEnabled }) {
  const env = normalizeOrderEnv(orderEnv);
  if (env === "real" && realOrderEnabled !== true) {
    throw new Error("실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다.");
  }
}

export function buildCashOrderBody({ accountNo, stockCode, quantity, price, orderType }) {
  const account = String(accountNo ?? "").trim();
  const code = String(stockCode ?? "").trim();
  const orderQuantity = assertPositiveInteger(quantity, "quantity");
  const normalizedOrderType = normalizeOrderType(orderType);
  const orderPrice = normalizedOrderType === "MARKET" ? 0 : assertPositiveInteger(price, "price");

  if (account.length < 10) {
    throw new Error("KIS_ACCOUNT_NO가 올바르지 않습니다. 계좌번호 앞 8자리와 상품코드 2자리를 붙여 설정하세요.");
  }
  // 아래 두 가드가 함께 "숫자 6~7자만 주문 가능"이라는 하나의 정책을 이룬다.
  // 첫 번째는 영숫자 코드(0001A0 등)에 전용 메시지를 주기 위한 것이고, 실제로 범위를
  // 확정하는 건 두 번째다. 9자 펀드코드(F70100026)는 첫 번째에 매치조차 되지 않고
  // 두 번째에서 거절된다.
  if (/^[0-9A-Z]{6,7}$/i.test(code) && !/^\d{6,7}$/.test(code)) {
    throw new Error("이 종목은 현재 주문을 지원하지 않습니다.");
  }
  // 이 범위가 backend/stock_code.py의 _ORDERABLE_STOCK_CODE_RE(`\A[0-9]{6,7}\Z`)와
  // 쌍을 이룬다. 백엔드가 같은 정책을 복제해 조기 거절하므로, 영숫자 코드 주문 지원을
  // 검토할 때(#138) 위 두 가드와 백엔드 상수를 **모두** 함께 바꿔야 한다. 하나라도 남기면
  // 그 계층에서 조용히 계속 막힌다.
  if (!/^\d{6,7}$/.test(code)) {
    throw new Error("stock_code는 6자리 또는 7자리 종목코드여야 합니다.");
  }

  return {
    CANO: account.substring(0, 8),
    ACNT_PRDT_CD: account.substring(8, 10),
    PDNO: code,
    ORD_DVSN: normalizedOrderType === "MARKET" ? "01" : "00",
    ORD_QTY: String(orderQuantity),
    ORD_UNPR: String(orderPrice),
    EXCG_ID_DVSN_CD: "SOR",
    SLL_TYPE: "",
    CNDT_PRIC: "",
  };
}

export function createCashOrderRequest({
  accountNo,
  kisUrl,
  orderEnv,
  side,
  stockCode,
  quantity,
  price,
  orderType,
  realOrderEnabled = false,
}) {
  validateRealOrderGuard({ orderEnv, realOrderEnabled });
  validateOrderEnvMatchesUrl({ orderEnv, kisUrl });
  return {
    pathname: "/uapi/domestic-stock/v1/trading/order-cash",
    trId: selectCashOrderTrId({ orderEnv, side }),
    body: buildCashOrderBody({
      accountNo,
      stockCode,
      quantity,
      price,
      orderType,
    }),
  };
}

export function formatOrderResult({ stockName, stockCode, side, quantity, price, orderType, data }) {
  const output = data?.output || {};
  const message = data?.msg1 || data?.msg_cd || "주문 요청이 접수되었습니다.";
  const orderNo = output.ODNO || "-";
  const orderTime = output.ORD_TMD || "-";
  const priceText = normalizeOrderType(orderType) === "MARKET"
    ? "시장가"
    : `${Number(price).toLocaleString("ko-KR")}원`;

  return [
    `[${stockName}] ${normalizeSide(side)} 주문 접수`,
    `- 종목코드: ${stockCode}`,
    `- 수량/가격: ${Number(quantity).toLocaleString("ko-KR")}주 / ${priceText}`,
    `- 주문번호: ${orderNo}`,
    `- 주문시간: ${orderTime}`,
    `- 메시지: ${message}`,
  ].join("\n");
}
