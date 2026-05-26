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

function formatAmount(value) {
  if (value === undefined || value === null || value === "") return "-";
  const number = Number(String(value).replaceAll(",", ""));
  if (!Number.isFinite(number)) {
    return `${value}원`;
  }
  return `${number.toLocaleString("ko-KR")}원`;
}

function formatQuantity(value) {
  if (value === undefined || value === null || value === "") return "-";
  const number = Number(String(value).replaceAll(",", ""));
  if (!Number.isFinite(number)) {
    return String(value);
  }
  return number.toLocaleString("ko-KR");
}

export function formatBalanceReport(data) {
  const summary = data.output2?.[0] || {};
  const holdings = data.output1 || [];

  const stockList = holdings
    .map((h) => {
      const returnRate = h.evlu_pfls_rt || h.evlu_erng_rt;
      return (
        `- ${h.prdt_name} (${h.pdno}): ${formatQuantity(h.hldg_qty)}주 ` +
        `(평가금액: ${formatAmount(h.evlu_amt)}, ` +
        `평가손익: ${formatAmount(h.evlu_pfls_amt)}, ` +
        `수익률: ${formatPercent(returnRate)})`
      );
    })
    .join("\n");

  const accountReturnRate = summary.asst_icdc_erng_rt || summary.evlu_pfls_rt;

  return `
[계좌 잔고 현황]
- 총 평가금액: ${formatAmount(summary.tot_evlu_amt)}
- 순자산금액: ${formatAmount(summary.nass_amt || summary.pchs_amt_smtl_amt)}
- 총 손익: ${formatAmount(summary.evlu_pfls_smtl_amt)} (수익률: ${formatPercent(accountReturnRate)})
- 예수금총액: ${formatAmount(summary.dnca_tot_amt)}
- 가수도정산금액: ${formatAmount(summary.prvs_rcdl_excc_amt)}
- 익일정산금액: ${formatAmount(summary.nxdy_excc_amt)}
- 금일 매수/매도: ${formatAmount(summary.thdt_buy_amt)} / ${formatAmount(summary.thdt_sll_amt)}

[보유 종목 리스트]
${stockList || "보유 종목이 없습니다."}
  `.trim();
}
