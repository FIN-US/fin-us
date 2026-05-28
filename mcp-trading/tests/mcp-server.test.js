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
      "get_today_daily_orders",
      "get_balance_rlz_pl",
    ]);
    assert.deepEqual(toolByName(tools, "get_balance").inputSchema.required ?? [], []);
    assert.deepEqual(toolByName(tools, "resolve_stock_code").inputSchema.required, ["stock_name"]);
    assert.deepEqual(toolByName(tools, "get_stock_quote").inputSchema.required, ["stock_name"]);
    assert.deepEqual(toolByName(tools, "get_investor_trading").inputSchema.required, ["stock_name"]);
    assert.deepEqual(toolByName(tools, "get_today_daily_orders").inputSchema.required ?? [], []);
    assert.deepEqual(toolByName(tools, "get_balance_rlz_pl").inputSchema.required ?? [], []);
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
