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
  const client = new Client({ name: "mcp-news-test", version: "1.0.0" });
  await client.connect(transport);

  try {
    return await callback(client);
  } finally {
    await client.close();
  }
}

test("registers active news tools with required stock_name schema", async () => {
  await withClient(async (client) => {
    const { tools } = await client.listTools();
    const toolNames = tools.map((tool) => tool.name);

    assert.deepEqual(toolNames, ["get_market_news"]);
    for (const tool of tools) {
      assert.deepEqual(tool.inputSchema.required, ["stock_name"]);
    }
  });
});

test("deprecated tools are not callable from mcp-news", async () => {
  await withClient(async (client) => {
    const result = await client.callTool({
      name: "get_research_reports",
      arguments: { stock_name: "삼성전자" },
    });

    assert.equal(result.isError, true);
    assert.match(result.content[0].text, /Tool get_research_reports not found/);
  });
});
