// 뉴스 검색 결과에서 조회한 종목을 실제로 다루는 기사를 고른다 (#448).
//
// 네이버 뉴스 검색(`<종목> 주식`, 최신순)의 앞 3건을 그대로 쓰면, 종목명이 기자 이메일·URL의 도메인에만
// 걸린 기사(예: NAVER → "…@naver.com")나 다른 종목이 주인공인 시황 기사가 올라온다. 에이전트는 그 기사의
// 종목으로 조회를 옮기고 답도 그 종목 위주로 썼다(NAVER 질의 5/5가 이에이트 뉴스로 답함).
// 그래서 넉넉히 받아, 제목에 종목명이 있는 기사 → 요약에만 있는 기사 순으로 고른다(같은 순위 안에서는 최신순 유지).
// 종목명을 언급한 기사가 하나도 없으면(예: 6자리 코드로 조회) 예전처럼 최신 기사를 돌려준다.

// 이메일·URL·도메인 안의 문자열은 종목 언급으로 치지 않는다.
const NON_TEXT_RE =
  /[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?:\/\/\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|kr)\b/gi;

export function mentionsStock(text, stockName) {
  const name = String(stockName ?? "").trim().toLowerCase();
  if (!name) return false;
  return String(text ?? "").replace(NON_TEXT_RE, " ").toLowerCase().includes(name);
}

// items: [{ title, description, ... }] (HTML 태그를 벗긴 문자열, 최신순)
export function pickStockNews(items, stockName, limit) {
  const tiered = items.map((item, index) => {
    let tier = 2;
    if (mentionsStock(item.title, stockName)) tier = 0;
    else if (mentionsStock(item.description, stockName)) tier = 1;
    return { item, tier, index };
  });
  const mentioned = tiered.filter((entry) => entry.tier < 2);
  if (mentioned.length === 0) return items.slice(0, limit);
  return mentioned
    .sort((a, b) => a.tier - b.tier || a.index - b.index)
    .slice(0, limit)
    .map((entry) => entry.item);
}
