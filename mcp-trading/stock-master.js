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
const NUMERIC_CODE_PATTERN = /^[0-9]{6,7}$/;

export function resolveStock(stockName, stocks) {
  const input = normalizeStockInput(stockName);
  if (!input) {
    throw new Error("stock_name 파라미터가 누락되었습니다.");
  }

  // Pure numeric input can never be a registered stock name (Korean/English
  // company names always contain at least one non-digit character), so it is
  // safe to treat it as a direct code without paying for a master lookup.
  if (NUMERIC_CODE_PATTERN.test(input)) {
    return { code: input, name: input, market: "UNKNOWN", aliases: [] };
  }

  const isCodeShaped = CODE_SHAPE_PATTERN.test(input);
  // Hoisted out of the filter loop below (recomputing per-entry cost 4,353
  // calls on the default master) and reused by the direct-code fallback
  // further down, since both need the same uppercased value once isCodeShaped
  // is true.
  const upperInput = isCodeShaped ? input.toUpperCase() : input;

  const stockList = stocks ?? getDefaultStocks();
  const matches = stockList.filter((stock) => {
    const aliases = Array.isArray(stock.aliases) ? stock.aliases : [];
    if (stock.name === input || aliases.includes(input)) {
      return true;
    }
    if (!isCodeShaped) {
      return false;
    }
    // A code-shaped input (e.g. a ticker-like company name such as SIMPAC)
    // may be typed in any case, mirroring the case-insensitivity the code
    // shortcut below already provides for direct codes. `name`/`aliases`
    // entries may be missing on a caller-supplied stock list (resolveStock's
    // `stocks` parameter is public), so default to "" before upper-casing
    // instead of letting a bare `undefined`/`null` throw.
    return String(stock.name ?? "").toUpperCase() === upperInput
      || aliases.some((alias) => String(alias ?? "").toUpperCase() === upperInput);
  });

  if (matches.length > 1) {
    const candidates = matches
      .map((stock) => `${stock.name}(${stock.code}, ${stock.market})`)
      .join(", ");
    throw new Error(`'${input}'의 종목 매칭이 모호합니다: ${candidates}. 6자리 종목코드를 직접 입력하세요.`);
  }

  if (matches.length === 1) {
    return {
      aliases: [],
      ...matches[0],
    };
  }

  // Nothing in the master matches by name or alias: only now treat a
  // code-shaped input (e.g. an alphanumeric ETN/fund code) as a direct code.
  if (isCodeShaped) {
    return { code: upperInput, name: upperInput, market: "UNKNOWN", aliases: [] };
  }

  throw new Error(
    `'${input}'의 종목 코드를 찾을 수 없습니다. ` +
      "6자리 종목코드로 직접 입력하거나 mcp-trading/data/stocks.json을 갱신하세요.",
  );
}
