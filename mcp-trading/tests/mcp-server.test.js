import assert from "node:assert/strict";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

async function withClient(callback) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: ["index.js"],
    cwd: process.cwd(),
  });
  const client = new Client({ name: "mcp-trading-test", version: "1.0.0" });
  await client.connect(transport);

  try {
    return await callback(client);
  } finally {
    await client.close();
  }
}

function toolByName(tools, name) {
  const tool = tools.find((candidate) => candidate.name === name);
  assert.ok(tool, `${name} tool should be registered`);
  return tool;
}

test("registers trading tools with preserved required schemas", async () => {
  await withClient(async (client) => {
    const { tools } = await client.listTools();

    assert.deepEqual(tools.map((tool) => tool.name), [
      "get_balance",
      "resolve_stock_code",
      "get_stock_quote",
      "get_investor_trading",
      "place_order",
      "get_today_daily_orders",
      "get_balance_rlz_pl",
      "get_orderable_cash",
    ]);
    assert.deepEqual(toolByName(tools, "get_balance").inputSchema.required ?? [], []);
    assert.deepEqual(toolByName(tools, "get_orderable_cash").inputSchema.required, ["stock_name"]);
    assert.deepEqual(toolByName(tools, "resolve_stock_code").inputSchema.required, ["stock_name"]);
    assert.deepEqual(toolByName(tools, "get_stock_quote").inputSchema.required, ["stock_name"]);
    assert.deepEqual(toolByName(tools, "get_investor_trading").inputSchema.required, ["stock_name"]);
    assert.deepEqual(toolByName(tools, "place_order").inputSchema.required, [
      "stock_code",
      "side",
      "quantity",
      "order_env",
    ]);
    assert.match(toolByName(tools, "place_order").description, /자동 재시도하지 마세요/);
  });
});

// 이슈 #369: time_budget_ms는 backend 스케줄러(run_mcp_tool 30초)가 연속조회 예산을 낮추려고
// 넘기는 인자다. 도구 스키마는 LLM에도 노출되므로 줄이는 방향만 받는다 — 상한과 기본값이
// 같은 90초(NAT 120초 호출자 기준)다. 생략 가능해야 stock_name만 넘기는 NAT 경로
// (finus_nat의 finus_api.py)가 그대로다.
test("get_balance_rlz_pl exposes time_budget_ms as an optional reduce-only integer", async () => {
  await withClient(async (client) => {
    const { tools } = await client.listTools();
    const { inputSchema } = toolByName(tools, "get_balance_rlz_pl");
    const prop = inputSchema.properties?.time_budget_ms;

    assert.ok(prop, `time_budget_ms가 스키마에 있어야 한다: ${JSON.stringify(inputSchema)}`);
    assert.equal(prop.type, "integer");
    assert.equal(prop.minimum, 1000);
    assert.equal(prop.maximum, 90000, "상한은 NAT 기준 기본 예산이다 — 늘리는 방향은 막는다");
    assert.equal(prop.default, 90000, "생략하면 현행 90초 예산이 그대로여야 한다");
    assert.ok(
      !(inputSchema.required ?? []).includes("time_budget_ms"),
      "생략 가능해야 NAT 경로가 바뀌지 않는다",
    );
  });
});

test("resolve_stock_code still works through the registered MCP tool", async () => {
  await withClient(async (client) => {
    const result = await client.callTool({
      name: "resolve_stock_code",
      arguments: { stock_name: "삼성전자" },
    });

    assert.equal(result.isError, undefined);
    assert.match(result.content[0].text, /삼성전자 \(005930/);
  });
});
