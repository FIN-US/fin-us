export function formatPercent(value) {
  if (value === undefined || value === null || value === "") return "-";
  return `${value}%`;
}

export function formatWon(value) {
  if (value === undefined || value === null || value === "") return "-";
  return `${Number(value).toLocaleString("ko-KR")}원`;
}

export function formatQuantity(value) {
  if (value === undefined || value === null || value === "") return "-";
  return Number(value).toLocaleString("ko-KR");
}

export function isPaperTradingKisUrl(url) {
  return (url || "").includes("openapivts");
}
