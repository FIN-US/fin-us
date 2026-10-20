import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { rm } from "node:fs/promises";
import { createServer } from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

// 이슈 #433: index.js의 두 도구가 kis-pdno.js 규칙을 실제로 쓰는지 끝까지 확인한다.
// index.js는 import하면 stdio 서버가 떠서 직접 부를 수 없으므로(tests/mcp-server.test.js와
// 같은 이유) 자식 프로세스로 띄우고 KIS_URL을 로컬 스텁으로 돌린다 —
// tests/page-delay-env-wiring.test.js와 같은 방식이다. 스텁은 실측 응답 행
// (fixtures/kis_response_pdno_policy.json)을 돌려준다.
const POLICY = JSON.parse(
  readFileSync(new URL("fixtures/kis_response_pdno_policy.json", import.meta.url), "utf-8"),
);
const CCLD_ROWS = POLICY.responses.filter((r) => r.tr_id === "VTTC0081R").map((r) => r.row);
const BALANCE_ROWS = POLICY.responses.filter((r) => r.tr_id === "VTTC8434R").map((r) => r.row);
const TEST_TIMEOUT_MS = 60_000;

// failSecondPage: 첫 페이지에 다음 페이지가 있다고 답하고(tr_cont=F) 두 번째 요청을 HTTP 500으로
// 돌려준다. fetchAllPaged가 첫 페이지만으로 부분 결과(truncated="error")를 만든다.
function startStub(output1, { failSecondPage = false } = {}) {
  const queries = [];
  const server = createServer((req, res) => {
    if (req.method === "POST" && req.url.startsWith("/oauth2/tokenP")) {
      req.resume();
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ access_token: "stub-token", expires_in: 86_400 }));
      return;
    }
    if (req.method === "GET") {
      const url = new URL(req.url, "http://127.0.0.1");
      queries.push({ path: url.pathname, params: Object.fromEntries(url.searchParams) });
      if (failSecondPage && queries.length === 2) {
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ message: "stub upstream failure" }));
        return;
      }
      const hasNext = failSecondPage && queries.length === 1;
      res.writeHead(200, { "Content-Type": "application/json", tr_cont: hasNext ? "F" : "D" });
      res.end(JSON.stringify({
        rt_cd: "0",
        msg_cd: "MCA00000",
        msg1: "정상처리 되었습니다.",
        output1,
        output2: { tot_ord_qty: "99", tot_ccld_qty: "99", tot_ccld_amt: "999999" },
        ctx_area_fk100: hasNext ? "FK1" : "",
        ctx_area_nk100: hasNext ? "NK1" : "",
      }));
      return;
    }
    res.writeHead(404).end();
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port, queries }));
  });
}

async function callWithStub(t, output1, tool, args, stubOptions = {}) {
  const { server, port, queries } = await startStub(output1, stubOptions);
  const tokenCachePath = path.join(os.tmpdir(), `finus-pdno-probe-${randomUUID()}.json`);
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: ["index.js"],
    cwd: process.cwd(),
    stderr: "pipe",
    env: {
      ...process.env,
      // 스텁 URL은 모의투자(openapivts) 판정에 걸리지 않아 get_balance_rlz_pl이 실제 TR 경로를 탄다.
      KIS_URL: `http://127.0.0.1:${port}`,
      KIS_API_KEY: "stub-appkey",
      KIS_API_SECRET: "stub-appsecret",
      KIS_ACCOUNT_NO: "5012345601",
      KIS_TOKEN_CACHE_PATH: tokenCachePath,
    },
  });
  const client = new Client({ name: "kis-pdno-wiring-test", version: "1.0.0" });
  t.after(async () => {
    await client.close().catch(() => {});
    await new Promise((resolve) => server.close(resolve));
    await rm(tokenCachePath, { force: true });
    await rm(`${tokenCachePath}.lock`, { force: true, recursive: true });
  });
  await client.connect(transport);
  const result = await client.callTool({ name: tool, arguments: args });
  assert.equal(result.isError, undefined, `tool call failed: ${result.content?.[0]?.text}`);
  return { text: result.content[0].text, queries };
}

test("get_balance_rlz_pl(stock_name=ETN)이 Q 없는 응답 pdno 행을 코드로 찾는다", { timeout: TEST_TIMEOUT_MS }, async (t) => {
  // 이름 일치(prdt_name === stock.name)로도 걸러지는 분기가 있어, 코드 비교만 보려고 ETN 행의
  // 이름을 지운다. 실현손익 TR은 모의투자 미지원이라 실측 행이 없어 잔고 실측 행을 쓴다
  // (두 TR 모두 output1이 pdno·prdt_name을 싣는다).
  const etnRow = { ...BALANCE_ROWS.find((r) => r.pdno === "530116"), prdt_name: "" };
  const prefRow = BALANCE_ROWS.find((r) => r.pdno === "00088K");
  const { text } = await callWithStub(t, [etnRow, prefRow], "get_balance_rlz_pl", { stock_name: "Q530116" });

  assert.match(text, /\(530116\)/, text);
  assert.doesNotMatch(text, /00088K/, text);
});

test("get_today_daily_orders(stock_name=ETN)는 서버 필터를 비우고 받은 행을 코드로 거른다", { timeout: TEST_TIMEOUT_MS }, async (t) => {
  const { text, queries } = await callWithStub(t, CCLD_ROWS, "get_today_daily_orders", { stock_name: "Q530116" });

  // 모의투자 실측: PDNO=Q500035는 70070000(내역 없음)을 돌려줬다.
  assert.equal(queries.length, 1);
  assert.equal(queries[0].params.PDNO, "");
  assert.match(text, /건수: 2건/, text);
  assert.match(text, /삼성 인버스 2X 나스닥 100 TR ETN \(530116\)/, text);
  assert.doesNotMatch(text, /00088K|500035/, text);
  // 전체 조회의 output2 집계는 다른 종목이 섞인 값이라 내보내지 않는다.
  assert.doesNotMatch(text, /집계\(output2\)/, text);
});

test("get_today_daily_orders(stock_name=신형우선주)는 지금처럼 코드를 서버 필터로 보낸다", { timeout: TEST_TIMEOUT_MS }, async (t) => {
  const prefRows = CCLD_ROWS.filter((r) => r.pdno === "00088K");
  const { text, queries } = await callWithStub(t, prefRows, "get_today_daily_orders", { stock_name: "00088K" });

  assert.equal(queries[0].params.PDNO, "00088K");
  assert.match(text, /집계\(output2\)/, text);
});

// ETN 로컬 필터는 전체 조회 결과를 거르므로, 다음 페이지가 잘리면 그 뒤의 ETN 행을 못 봤을 수
// 있다. 필터가 결과를 다시 만들 때 잘림 정보(truncated·pages)를 떨어뜨리면, 잘린 조회가
// "해당 조건의 주문·체결이 없습니다"나 완전한 목록처럼 읽힌다.
test("get_today_daily_orders(stock_name=ETN): 로컬 필터 뒤에도 페이지 잘림 안내가 남는다", { timeout: TEST_TIMEOUT_MS }, async (t) => {
  const { text, queries } = await callWithStub(
    t,
    CCLD_ROWS,
    "get_today_daily_orders",
    { stock_name: "Q530116" },
    { failSecondPage: true },
  );

  assert.equal(queries.length, 2, "첫 페이지 다음 요청이 실패해야 잘림이 생긴다");
  assert.match(text, /삼성 인버스 2X 나스닥 100 TR ETN \(530116\)/, text);
  assert.match(text, /\[안내\] 연속조회 중 오류가 발생하여/, text);
});

test("get_today_daily_orders(stock_name=ETN): 걸러 낸 행이 없어도 잘림 안내가 남는다", { timeout: TEST_TIMEOUT_MS }, async (t) => {
  // 첫 페이지에 이 ETN 행이 없고 다음 페이지는 실패했다 — "없습니다"만 단언하면 안 된다.
  const prefRows = CCLD_ROWS.filter((r) => r.pdno === "00088K");
  const { text } = await callWithStub(
    t,
    prefRows,
    "get_today_daily_orders",
    { stock_name: "Q530116" },
    { failSecondPage: true },
  );

  assert.match(text, /해당 조건의 주문·체결이 없습니다/, text);
  assert.match(text, /\[안내\] 연속조회 중 오류가 발생하여/, text);
});
