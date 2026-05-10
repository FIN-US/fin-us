import asyncio
import json
from types import SimpleNamespace

from nat_finus_nat import finus_api


def test_mcp_call_tool_passes_environment_to_child_process(monkeypatch, tmp_path):
    server_dir = tmp_path / "mcp-news"
    server_dir.mkdir()
    script = server_dir / "index.js"
    script.write_text("", encoding="utf-8")
    (server_dir / "node_modules" / "@modelcontextprotocol" / "sdk").mkdir(parents=True)

    captured = {}

    class FakeStdioServerParameters:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(finus_api, "StdioServerParameters", FakeStdioServerParameters)
    monkeypatch.setattr(finus_api.os, "environ", {"NAVER_CLIENT_ID": "id"})

    def fake_inner(*args, **kwargs):
        class _Context:
            async def __aenter__(self):
                return (None, None)

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Context()

    class FakeClientSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, tool_name, arguments):
            block = SimpleNamespace(text="ok")
            return SimpleNamespace(content=[block])

    monkeypatch.setattr(finus_api, "stdio_client", fake_inner)
    monkeypatch.setattr(finus_api, "ClientSession", FakeClientSession)

    result = asyncio.run(
        finus_api._mcp_call_tool(
            vendor_root=tmp_path,
            subdir="mcp-news",
            tool_name="get_market_news",
            arguments={"stock_name": "삼성전자"},
            timeout_sec=5,
        )
    )

    assert result == "ok"
    assert captured["env"] == {"NAVER_CLIENT_ID": "id"}
    assert json.dumps(captured["args"])
