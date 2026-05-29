import { formatPercent, formatQuantity, formatWon } from "./formatters.js";

export function formatBalanceRlzPlSummaryBlock(summary) {
  if (!summary) {
    return "";
  }
  return `
[계좌 집계]
- 예수금: ${formatWon(summary.dnca_tot_amt)}
- 총평가금액: ${formatWon(summary.tot_evlu_amt)} | 순자산: ${formatWon(summary.nass_amt)}
- 매입합계: ${formatWon(summary.pchs_amt_smtl_amt)} | 평가손익합계: ${formatWon(summary.evlu_pfls_smtl_amt)}
- 실현손익: ${formatWon(summary.rlzt_pfls)} (${formatPercent(summary.rlzt_erng_rt)})
- 실평가손익: ${formatWon(summary.real_evlu_pfls)} (${formatPercent(summary.real_evlu_pfls_erng_rt)})
- 금일 매수/매도: ${formatWon(summary.thdt_buy_amt)} / ${formatWon(summary.thdt_sll_amt)}
  `.trim();
}

export function formatBalanceRlzPlReport({ rows, summary, pages, trId, stockLabel }) {
  const summaryBlock = formatBalanceRlzPlSummaryBlock(summary);

  if (rows.length === 0) {
    const holdingsNote = stockLabel
      ? `- ${stockLabel} 보유 종목이 없습니다.`
      : "- 보유 종목이 없습니다.";
    return `
[주식잔고조회_실현손익]${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (v1_국내주식-041, inquire-balance-rlz-pl)
${holdingsNote}
${summaryBlock ? `\n${summaryBlock}` : ""}
    `.trim();
  }

  const lines = rows.map((row, index) => {
    const dayTrade =
      `금일 매수 ${formatQuantity(row.thdt_buyqty)}주 / 매도 ${formatQuantity(row.thdt_sll_qty)}주`;
    return [
      `${index + 1}. ${row.prdt_name || "-"} (${row.pdno || "-"}) · ${row.trad_dvsn_name || "-"}`,
      `   보유 ${formatQuantity(row.hldg_qty)}주 | 현재가 ${formatWon(row.prpr)} | 평가 ${formatWon(row.evlu_amt)}`,
      `   평가손익 ${formatWon(row.evlu_pfls_amt)} (${formatPercent(row.evlu_pfls_rt)}) | 매입가 ${formatWon(row.pchs_avg_pric)}`,
      `   ${dayTrade} | 전일대비 ${row.bfdy_cprs_icdc ?? "-"} (${formatPercent(row.fltt_rt)})`,
    ].join("\n");
  });

  return `
[주식잔고조회_실현손익]${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (v1_국내주식-041, inquire-balance-rlz-pl)
- 종목 수: ${rows.length} (${pages}회 API 호출, 연속조회 포함)

${summaryBlock}

[보유 종목]
${lines.join("\n\n")}
  `.trim();
}
