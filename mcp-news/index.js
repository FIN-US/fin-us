import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

// Redirect console.log to console.error to prevent breaking MCP JSON-RPC on stdout
console.log = console.error;

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootEnvPath = path.join(__dirname, "..", ".env");

function loadRootEnv() {
  if (!fs.existsSync(rootEnvPath)) return;

  const envText = fs.readFileSync(rootEnvPath, "utf8");
  for (const line of envText.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;

    const eqIndex = trimmed.indexOf("=");
    if (eqIndex === -1) continue;

    const key = trimmed.slice(0, eqIndex).trim();
    let value = trimmed.slice(eqIndex + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (!process.env[key]) process.env[key] = value;
  }
}

loadRootEnv();

const NAVER_NEWS_ENDPOINT = "https://openapi.naver.com/v1/search/news.json";
const NAVER_CLIENT_ID = process.env.NAVER_CLIENT_ID;
const NAVER_CLIENT_SECRET = process.env.NAVER_CLIENT_SECRET;
const DEFAULT_DISPLAY_COUNT = 3;
const REQUEST_TIMEOUT_MS = 8000;

const server = new McpServer({ name: "news-tool", version: "1.0.0" });

function isMissingCredential(value) {
  return !value || value.startsWith("your_") || value.includes("_here");
}

function decodeHtml(text) {
  return String(text ?? "")
    .replace(/<[^>]+>/g, "")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ")
    .trim();
}

function formatNewsItem(item) {
  const title = decodeHtml(item.title);
  const description = decodeHtml(item.description);
  const pubDate = decodeHtml(item.pubDate);
  const link = item.link || item.originallink || "";

  const details = [description, pubDate, link].filter(Boolean).join(" | ");
  return details ? `${title} - ${details}` : title;
}

async function fetchMarketNews(stockName) {
  if (isMissingCredential(NAVER_CLIENT_ID) || isMissingCredential(NAVER_CLIENT_SECRET)) {
    throw new Error(
      "NAVER_CLIENT_ID 또는 NAVER_CLIENT_SECRET이 설정되지 않았습니다. 루트 .env 파일을 확인하세요.",
    );
  }

  const url = new URL(NAVER_NEWS_ENDPOINT);
  url.searchParams.set("query", `${stockName} 주식`);
  url.searchParams.set("display", String(DEFAULT_DISPLAY_COUNT));
  url.searchParams.set("start", "1");
  url.searchParams.set("sort", "date");

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const response = await fetch(url, {
      headers: {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
      },
      signal: controller.signal,
    });

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const errorMessage = payload.errorMessage || payload.message || response.statusText;
      throw new Error(`네이버 뉴스 API 오류(${response.status}): ${errorMessage}`);
    }

    const items = Array.isArray(payload.items) ? payload.items : [];
    const news = items.map(formatNewsItem).filter(Boolean).slice(0, DEFAULT_DISPLAY_COUNT);
    return news.length > 0 ? news.join("\n") : `'${stockName}'에 대한 뉴스를 찾지 못했습니다.`;
  } finally {
    clearTimeout(timeout);
  }
}

const stockNameSchema = z.object({
  stock_name: z.string().describe("주식 종목명 (예: 삼성전자, SK하이닉스)"),
});

server.registerTool(
  "get_market_news",
  {
    description: "네이버 뉴스 검색 API로 특정 주식 종목의 최신 뉴스 3개를 가져옵니다.",
    inputSchema: stockNameSchema,
  },
  async ({ stock_name: stockName }) => {
    if (!stockName) {
      return {
        content: [{ type: "text", text: "에러: stock_name 파라미터가 누락되었습니다." }],
        isError: true,
      };
    }

    try {
      const news = await fetchMarketNews(stockName);
      return { content: [{ type: "text", text: news }] };
    } catch (error) {
      return { content: [{ type: "text", text: `에러 발생: ${error.message}` }], isError: true };
    }
  },
);

const transport = new StdioServerTransport();
await server.connect(transport);

console.error("News MCP Server is running...");
