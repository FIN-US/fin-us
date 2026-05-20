export function buildBalanceParams(accountNo) {
  return {
    CANO: accountNo.substring(0, 8),
    ACNT_PRDT_CD: accountNo.substring(8, 10),
    AFHR_FLPR_YN: "N",
    OFL_YN: "",
    INQR_DVSN: "02",
    UNPR_DVSN: "01",
    FUND_STTL_ICLD_YN: "N",
    FNCG_AMT_AUTO_RDPT_YN: "N",
    PRCS_DVSN: "00",
    CTX_AREA_FK100: "",
    CTX_AREA_NK100: "",
  };
}

export function formatPercent(value) {
  if (value === undefined || value === null || value === "") return "-";
  return `${value}%`;
}
