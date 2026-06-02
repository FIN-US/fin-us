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
  const client = new Client({ name: "mcp-dart-test", version: "1.0.0" });
  await client.connect(transport);

  try {
    return await callback(client);
  } finally {
    await client.close();
  }
}

test("registers disclosure signal tool with required stock_name schema", async () => {
  await withClient(async (client) => {
    const { tools } = await client.listTools();

    const disclosureTool = tools.find((tool) => tool.name === "get_disclosure_signal");
    assert.ok(disclosureTool);
    assert.deepEqual(disclosureTool.inputSchema.required, ["stock_name"]);
  });
});

test("registers earnings report tool with stock_name and optional period schema", async () => {
  await withClient(async (client) => {
    const { tools } = await client.listTools();

    const earningsTool = tools.find((tool) => tool.name === "get_earnings_report");
    assert.ok(earningsTool);
    assert.deepEqual(earningsTool.inputSchema.required, ["stock_name"]);
    assert.ok(earningsTool.inputSchema.properties.period);
  });
});
