/**
 * corp-resolver.js
 *
 * DART 상장회사 목록(items)에서 주어진 종목명/코드로 법인을 조회합니다.
 *
 * 한계: ETF처럼 DART 법인 목록에 아예 없는 종목은 접미사 정규화로도 해결되지 않습니다
 * (별도 매핑 필요). 이 변경은 우선주·전환주 접미사에 한정됩니다.
 */

/**
 * 문자열 앞뒤 공백 및 내부 연속 공백을 정규화합니다.
 * @param {unknown} value
 * @returns {string}
 */
export function cleanText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

/**
 * 우선주·전환주 접미사 패턴을 제거해 기본 법인명을 추출합니다.
 * 예: "삼성전자우" → "삼성전자", "현대차2우B" → "현대차", "CJ4우(전환)" → "CJ"
 *
 * 패턴: 문자열 끝의 `\d*우B?(\(전환\))?` 만 제거합니다 (중간이 아닌 접미사만).
 * 제거 후 빈 문자열이 되면 undefined를 반환합니다.
 *
 * @param {string} name
 * @returns {string | undefined}
 */
export function stripPreferredStockSuffix(name) {
  // `\s*`: "CJ제일제당 우"처럼 접미사 앞에 공백이 들어간 KRX 종목명 대응.
  // `\d*`: "해성산업1우"·"현대차2우B" 같은 신형우선주 회차 번호.
  const base = name.replace(/\s*\d*우B?(\(전환\))?$/, "").trim();
  if (base.length === 0 || base === name) return undefined; // 제거된 게 없으면 폴백 불필요
  return base;
}

const MAX_LISTED_MATCHES = 5;

function ambiguousMatchError(query, matches) {
  const names = matches
    .slice(0, MAX_LISTED_MATCHES)
    .map((item) => `${item.corp_name}(${item.stock_code}, ${item.corp_code})`)
    .join(", ");
  return new Error(`'${query}'가 여러 회사와 일치합니다: ${names}`);
}

/**
 * DART 상장회사 목록에서 종목명 또는 6자리 종목코드로 법인을 조회합니다.
 *
 * 1. 6자리 숫자 코드이면 stock_code로 완전일치 조회합니다.
 * 2. 이름이면 corp_name 완전일치를 먼저 시도합니다.
 * 3. 완전일치가 0건이면 우선주/전환주 접미사를 제거한 기본명으로 폴백합니다.
 *    - 폴백도 0건이면 원래 query로 에러를 던집니다.
 *    - 폴백이 다중 매칭이면 "여러 회사 일치" 에러를 던집니다.
 *
 * @param {Array<{corp_name: string, stock_code: string, corp_code: string}>} items
 * @param {string} input
 * @returns {{corp_name: string, stock_code: string, corp_code: string}}
 */
export function resolveCorp(items, input) {
  const query = cleanText(input);
  if (!query) throw new Error("stock_name 파라미터가 비어 있습니다.");

  // 6자리 종목코드 경로
  if (/^\d{6}$/.test(query)) {
    const matches = items.filter((item) => item.stock_code === query);
    if (matches.length === 0) {
      throw new Error(`'${query}'와 정확히 일치하는 DART 상장회사 정보를 찾지 못했습니다.`);
    }
    if (matches.length > 1) throw ambiguousMatchError(query, matches);
    return matches[0];
  }

  // 법인명 완전일치 (항상 우선)
  const exactMatches = items.filter((item) => item.corp_name === query);
  if (exactMatches.length === 1) return exactMatches[0];
  if (exactMatches.length > 1) throw ambiguousMatchError(query, exactMatches);

  // 완전일치 0건 → 우선주·전환주 접미사 제거 후 폴백
  const baseName = stripPreferredStockSuffix(query);
  if (baseName !== undefined) {
    const fallbackMatches = items.filter((item) => item.corp_name === baseName);
    if (fallbackMatches.length === 1) return fallbackMatches[0];
    if (fallbackMatches.length > 1) {
      const names = fallbackMatches
        .slice(0, MAX_LISTED_MATCHES)
        .map((item) => `${item.corp_name}(${item.stock_code}, ${item.corp_code})`)
        .join(", ");
      throw new Error(
        `'${query}'의 기본 법인명 '${baseName}'이 여러 회사와 일치합니다: ${names}`,
      );
    }
  }

  // 폴백도 실패 → 원래 query로 에러
  throw new Error(`'${query}'와 정확히 일치하는 DART 상장회사 정보를 찾지 못했습니다.`);
}
