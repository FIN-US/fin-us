import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const DEFAULT_STOCKS_PATH = path.join(__dirname, "data", "stocks.json");

export function loadStocks(stocksPath = DEFAULT_STOCKS_PATH) {
  const text = fs.readFileSync(stocksPath, "utf8");
  return JSON.parse(text);
}

let cachedStocks;

export function clearStocksCache() {
  cachedStocks = undefined;
}

export function getDefaultStocks() {
  if (cachedStocks === undefined) {
    cachedStocks = loadStocks();
  }
  return cachedStocks;
}

export function normalizeStockInput(value) {
  return String(value ?? "").trim();
}

export const CODE_SHAPE_PATTERN = /^[A-Z0-9]{6,7}$/i;

export function resolveStock(stockName, stocks) {
  const input = normalizeStockInput(stockName);
  if (!input) {
    throw new Error("stock_name 파라미터가 누락되었습니다.");
  }

  const upperInput = input.toUpperCase();
  const stockList = stocks ?? getDefaultStocks();

  // Step 1: Try exact code match first (length-agnostic, case-insensitive).
  // This short-circuits before name/alias filtering so that:
  //   - numeric codes (e.g. 005930) return the real name/market from the master
  //   - 9-char fund codes (e.g. F70100026) are handled without a special pattern
  //   - alphanumeric codes (e.g. Q570121) are resolved correctly
  //   - a code that happens to equal another entry's name is unambiguous
  // Codes are unique in the master, so there is no ambiguity risk.
  const codeHit = stockList.find((s) => String(s.code ?? "").toUpperCase() === upperInput);
  if (codeHit) {
    return { aliases: [], ...codeHit };
  }

  // Step 2: Name / alias matching (case-sensitive for Korean names; case-
  // insensitive for code-shaped inputs that may be company names like SIMPAC).
  const isCodeShaped = CODE_SHAPE_PATTERN.test(input);

  const matches = stockList.filter((stock) => {
    const aliases = Array.isArray(stock.aliases) ? stock.aliases : [];
    if (stock.name === input || aliases.includes(input)) {
      return true;
    }
    if (!isCodeShaped) {
      return false;
    }
    // A code-shaped input may be a ticker-like company name (e.g. SIMPAC).
    // Match case-insensitively against name and aliases only — stock.code
    // matching is already handled by the short-circuit above.
    return String(stock.name ?? "").toUpperCase() === upperInput
      || aliases.some((alias) => String(alias ?? "").toUpperCase() === upperInput);
  });

  if (matches.length > 1) {
    const candidates = matches
      .map((stock) => `${stock.name}(${stock.code}, ${stock.market})`)
      .join(", ");
    throw new Error(`'${input}'의 종목 매칭이 모호합니다: ${candidates}. 6·7·9자리 종목코드를 직접 입력하세요.`);
  }

  if (matches.length === 1) {
    return {
      aliases: [],
      ...matches[0],
    };
  }

  // Step 3: Nothing matched by name or alias.
  // A code-shaped input (6-7 alphanumeric chars) that is absent from the
  // master is echoed back as an UNKNOWN direct code.
  if (isCodeShaped) {
    return { code: upperInput, name: upperInput, market: "UNKNOWN", aliases: [] };
  }

  // A numeric-only code that is not in the master also reaches here (the old
  // NUMERIC_CODE_PATTERN early-return is gone; the code short-circuit above
  // handles found entries; the isCodeShaped echo handles the not-found case).

  throw new Error(
    `'${input}'의 종목 코드를 찾을 수 없습니다. ` +
      "6·7·9자리 종목코드로 직접 입력하거나 mcp-trading/data/stocks.json을 갱신하세요.",
  );
}
