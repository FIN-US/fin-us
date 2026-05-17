import asyncio
import json
from types import SimpleNamespace

from nat_finus_nat import finus_api
from nat_finus_nat import finus_paths


def _write_mcp_script(root, name):
    server_dir = root / name
    server_dir.mkdir(parents=True)
    (server_dir / "index.js").write_text("", encoding="utf-8")


def test_vendor_root_detection_keeps_existing_news_trading_layout(monkeypatch, tmp_path):
    finus_nat_root = tmp_path / "finus_nat"
    (finus_nat_root / "src").mkdir(parents=True)
    _write_mcp_script(tmp_path, "mcp-news")
    _write_mcp_script(tmp_path, "mcp-trading")

    monkeypatch.delenv("FINUS_VENDOR_ROOT", raising=False)
    monkeypatch.setattr(finus_paths, "finus_nat_example_root", lambda: finus_nat_root)

    assert finus_paths.fin_us_vendor_root() == tmp_path


def test_vendor_root_detection_keeps_nested_news_trading_layout(monkeypatch, tmp_path):
    finus_nat_root = tmp_path / "finus_nat"
    finus_home = tmp_path / "fin-us"
    (finus_nat_root / "src").mkdir(parents=True)
    _write_mcp_script(finus_home, "mcp-news")
    _write_mcp_script(finus_home, "mcp-trading")

    monkeypatch.delenv("FINUS_VENDOR_ROOT", raising=False)
    monkeypatch.setattr(finus_paths, "finus_nat_example_root", lambda: finus_nat_root)

    assert finus_paths.fin_us_vendor_root() == finus_home


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
    monkeypatch.setattr(
        finus_api.os,
        "environ",
        {
            "NAVER_CLIENT_ID": "id",
            "KIS_API_KEY": "kis-key",
            "DART_API_KEY": "dart-key",
            "FIN_US_TRACE_ID": "trace-id",
            "DATABASE_URL": "postgresql://user:secret@db/prod",
            "UNRELATED_SECRET": "secret",
        },
    )

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
    assert captured["env"] == {
        "NAVER_CLIENT_ID": "id",
        "KIS_API_KEY": "kis-key",
        "DART_API_KEY": "dart-key",
        "FIN_US_TRACE_ID": "trace-id",
    }
    assert json.dumps(captured["args"])


def test_mcp_dart_stock_routes_to_disclosure_signal_tool(monkeypatch, tmp_path):
    captured = {}

    async def fake_mcp_call_tool(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(finus_api, "_mcp_call_tool", fake_mcp_call_tool)

    config = SimpleNamespace(vendor_root=str(tmp_path), timeout_sec=7)
    result = asyncio.run(finus_api._mcp_dart_stock(config, "삼성전자"))

    assert result == "ok"
    assert captured == {
        "vendor_root": tmp_path,
        "subdir": "mcp-dart",
        "tool_name": "get_disclosure_signal",
        "arguments": {"stock_name": "삼성전자"},
        "timeout_sec": 7,
    }
