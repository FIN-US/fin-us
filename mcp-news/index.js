import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { chromium } from "playwright";

// Redirect console.log to console.error to prevent breaking MCP JSON-RPC on stdout
console.log = console.error;

const server = new Server(
  { name: "news-tool", version: "1.0.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "get_market_news",
      description: "특정 주식 종목의 최신 뉴스 3개를 네이버에서 가져옵니다.",
      inputSchema: {
        type: "object",
        properties: {
          stock_name: {
            type: "string",
            description: "주식 종목명 (예: 삼성전자, SK하이닉스)",
          },
        },
        required: ["stock_name"],
      },
    },
    {
      name: "get_investor_trading",
      description: "특정 주식 종목의 외국인 및 기관 순매수 현황을 가져옵니다.",
      inputSchema: {
        type: "object",
        properties: {
          stock_name: {
            type: "string",
            description: "주식 종목명 (예: 삼성전자, SK하이닉스)",
          },
        },
        required: ["stock_name"],
      },
    },
    {
      name: "get_research_reports",
      description: "네이버 증권 리서치 페이지에서 특정 종목의 증권사 리포트 목록을 가져옵니다.",
      inputSchema: {
        type: "object",
        properties: {
          stock_name: {
            type: "string",
            description: "주식 종목명 (예: 삼성전자, SK하이닉스)",
          },
        },
        required: ["stock_name"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const stock_name = args?.stock_name;

  if (!stock_name) {
    return {
      content: [{ type: "text", text: "에러: stock_name 파라미터가 누락되었습니다." }],
      isError: true,
    };
  }

  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    });
    const page = await context.newPage();

    if (name === "get_market_news") {
      const searchUrl = `https://search.naver.com/search.naver?where=news&query=${encodeURIComponent(stock_name)}&sm=tab_opt&sort=0`;
      await page.goto(searchUrl, { waitUntil: "load" });

      await Promise.race([
        page.waitForSelector(".news_tit", { timeout: 8000 }),
        page.waitForSelector(".list_news", { timeout: 8000 }),
      ]).catch(() => null);

      const results = await page.evaluate(() => {
        const traditional = Array.from(document.querySelectorAll(".news_tit"))
          .map((t) => t.textContent.trim())
          .filter((t) => t.length > 0);
        if (traditional.length > 0) return traditional.slice(0, 3);
        const listNews = document.querySelector(".list_news");
        if (!listNews) return [];
        const titles = [];
        const links = Array.from(listNews.querySelectorAll('a[target="_blank"]'));
        for (const link of links) {
          const text = link.textContent.trim();
          if (text.length > 10 && text.length < 100 && !text.includes("저장") && !text.includes("바로가기") && !text.endsWith("...") && !link.className.includes("press")) {
            if (!titles.includes(text)) titles.push(text);
          }
          if (titles.length >= 3) break;
        }
        return titles;
      });

      return { content: [{ type: "text", text: results.length > 0 ? results.join("\n") : `'${stock_name}'에 대한 뉴스를 찾지 못했습니다.` }] };

    } else if (name === "get_investor_trading") {
      const searchUrl = `https://search.naver.com/search.naver?query=${encodeURIComponent(stock_name + " 주가")}`;
      await page.goto(searchUrl, { waitUntil: "load" });
      
      const code = await page.evaluate(() => {
        const links = Array.from(document.querySelectorAll("a"));
        for (const link of links) {
          const href = link.getAttribute("href") || "";
          const match = href.match(/code=(\d{6})/);
          if (match) return match[1];
        }
        return null;
      });

      if (!code) {
        return { content: [{ type: "text", text: `'${stock_name}'의 종목 코드를 찾을 수 없습니다.` }], isError: true };
      }

      await page.goto(`https://finance.naver.com/item/frgn.naver?code=${code}`, { waitUntil: "load" });
      const tradingData = await page.evaluate((sName) => {
        const rows = Array.from(document.querySelectorAll("table.type2 tr"))
          .map(tr => Array.from(tr.querySelectorAll("td")).map(td => td.innerText.trim()))
          .filter(cells => cells.length >= 7 && cells[0].match(/^\d{4}\.\d{2}\.\d{2}$/))
          .slice(0, 5);
        
        if (rows.length === 0) return `데이터를 불러올 수 없습니다.`;
        
        let report = `[${sName}] 최근 5일 외국인/기관 매매동향\n`;
        rows.forEach(row => {
          const change = row[2].replace(/\n/g, " ");
          report += `${row[0]} | 종가: ${row[1]} | 변동: ${change} (${row[3]}) | 외인: ${row[5]} | 기관: ${row[6]} | 거래량: ${row[4]}\n`;
        });
        return report;
      }, stock_name);

      return { content: [{ type: "text", text: tradingData }] };

    } else if (name === "get_research_reports") {
      const researchUrl = "https://finance.naver.com/research/company_list.naver";
      await page.goto(researchUrl, { waitUntil: "load" });

      const reports = await page.evaluate((sName) => {
        const rows = Array.from(document.querySelectorAll("table.type_1 tr"))
          .filter(tr => {
            const cells = tr.querySelectorAll("td");
            if (cells.length < 2) return false;
            const target = cells[0].innerText.trim();
            const title = cells[1].innerText.trim();
            return target === sName || title.includes(sName);
          })
          .map(tr => {
            const cells = tr.querySelectorAll("td");
            return {
              stock: cells[0].innerText.trim(),
              title: cells[1].innerText.trim(),
              company: cells[2].innerText.trim(),
              date: cells[4].innerText.trim(),
            };
          })
          .slice(0, 5);

        if (rows.length === 0) return `해당 종목('${sName}')에 대한 최근 리서치 리포트를 찾을 수 없습니다.`;

        let reportText = `[${sName}] 네이버 증권 최근 리서치 리포트\n`;
        rows.forEach(r => {
          reportText += `${r.date} | ${r.company} | ${r.title}\n`;
        });
        return reportText;
      }, stock_name);

      return { content: [{ type: "text", text: reports }] };
    }
  } catch (error) {
    return { content: [{ type: "text", text: `에러 발생: ${error.message}` }], isError: true };
  } finally {
    await browser.close();
  }
  throw new Error("존재하지 않는 도구입니다.");
});

const transport = new StdioServerTransport();
await server.connect(transport);

console.error("News MCP Server is running...");
