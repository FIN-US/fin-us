import { formatPercent, formatQuantity, formatWon } from "./formatters.js";

// 모의투자(openapivts) 대체 안내 문구. index.js의 getBalanceRlzPl가 모의투자에서 실현손익 TR 대신
// getBalance 결과를 돌려줄 때 **그 뒤에 그대로 이어 붙인다**(`${balanceText}${note}`).
// ⚠️ 이 문구의 부분 문자열 "잔고 요약으로 대체했습니다"를 두 곳이 매칭한다.
//   - backend/scheduler.py의 _RLZ_PL_PAPER_FALLBACK_MARKER — 모의투자 대체 응답 감지(#367).
//   - finus_nat finus_api.py의 _PAPER_RLZ_PL_FALLBACK_MARKER — 매매일지 조회 묶음이 이 응답의
//     잔고를 잔고 섹션으로 재사용하고 잔고 조회를 따로 부르지 않는다(#408). 문구 앞의 잔고
//     텍스트를 떼어 쓰므로 "\n\n[안내]"로 시작하는 마지막 문단이어야 한다.
// 문구를 바꾸면 backend 감지와 NAT 재사용이 조용히 꺼진다(NAT는 잔고 TR을 다시 두 번 부른다).
// 세 곳이 같은 문자열을 쓰는지는 tests/fixtures/paper_rlz_pl_fallback.json을 JS·backend·NAT
// 스위트가 함께 읽어 고정한다 — 바꿔야 한다면 그 픽스처와 두 상수를 같이 고친다.
export const PAPER_RLZ_PL_FALLBACK_NOTE =
  "\n\n[안내] 모의투자(openapivts) 계좌는 실현손익 TR(v1_국내주식-041)을 지원하지 않아 잔고 요약으로 대체했습니다.";

// 잘림 안내 문구. "- "로 시작하면 파싱 오류를 낼 수 있으므로 반드시 "[안내]"로 시작한다.
// rows.length === 0인 경우에도 출력해야 한다 — 그렇지 않으면 잘림 때문에 빈 결과가 나온 상황을
// "보유 종목이 없습니다"로 사실로 단언하게 된다.
function formatRlzPlTruncationNote(truncated, pages) {
  if (!truncated) return "";
  const reasons = {
    max_pages: `페이지 상한(${pages}회)에 도달하여`,
    time_budget: "조회 시간 예산을 초과하여",
    no_cursor: "연속조회 커서가 오지 않아",
    repeated_cursor: "동일한 연속조회 커서가 반복되어",
    error: "연속조회 중 오류가 발생하여",
  };
  const reason = reasons[truncated] || "연속조회가 완료되지 않아";
  return `\n\n[안내] ${reason} 조회가 중단되어 일부 실현손익 내역이 누락되었을 수 있습니다. 실제 내역은 별도로 확인하세요.`;
}

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

export function formatBalanceRlzPlReport({ rows, summary, pages, truncated, trId, stockLabel }) {
  const summaryBlock = formatBalanceRlzPlSummaryBlock(summary);
  const truncationNote = formatRlzPlTruncationNote(truncated, pages);

  if (rows.length === 0) {
    const holdingsNote = stockLabel
      ? `- ${stockLabel} 보유 종목이 없습니다.`
      : "- 보유 종목이 없습니다.";
    return `
[주식잔고조회_실현손익]${stockLabel ? ` / ${stockLabel}` : ""}
- 조회 TR: ${trId} (v1_국내주식-041, inquire-balance-rlz-pl)
${holdingsNote}
${summaryBlock ? `\n${summaryBlock}` : ""}${truncationNote}
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
${lines.join("\n\n")}${truncationNote}
  `.trim();
}
