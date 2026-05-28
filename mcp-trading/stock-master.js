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

export function normalizeStockInput(value) {
  return String(value ?? "").trim();
}

export function resolveStock(stockName, stocks = loadStocks()) {
  const input = normalizeStockInput(stockName);
  if (!input) {
    throw new Error("stock_name 파라미터가 누락되었습니다.");
  }

  if (/^[A-Z0-9]{6,7}$/i.test(input)) {
    const code = input.toUpperCase();
    return { code, name: code, market: "UNKNOWN", aliases: [] };
  }

  const matches = stocks.filter((stock) => {
    const aliases = Array.isArray(stock.aliases) ? stock.aliases : [];
    return stock.name === input || aliases.includes(input);
  });

  if (matches.length === 0) {
    throw new Error(
      `'${input}'의 종목 코드를 찾을 수 없습니다. ` +
        "6자리 종목코드로 직접 입력하거나 mcp-trading/data/stocks.json을 갱신하세요.",
    );
  }

  if (matches.length > 1) {
    const candidates = matches
      .map((stock) => `${stock.name}(${stock.code}, ${stock.market})`)
      .join(", ");
    throw new Error(`'${input}'의 종목 매칭이 모호합니다: ${candidates}. 6자리 종목코드를 직접 입력하세요.`);
  }

  return {
    aliases: [],
    ...matches[0],
  };
}
