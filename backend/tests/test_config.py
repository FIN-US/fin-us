import importlib
from pathlib import Path

import backend.config as config
from backend.config import DART_MCP_PARAMS, NEWS_MCP_PARAMS, TRADING_MCP_PARAMS, _stdio_server_params


def test_mcp_stdio_params_pass_filtered_environment_to_child_processes():
    assert isinstance(NEWS_MCP_PARAMS.env, dict)
    assert isinstance(TRADING_MCP_PARAMS.env, dict)
    assert isinstance(DART_MCP_PARAMS.env, dict)


def test_mcp_stdio_params_do_not_pass_parent_only_secrets(monkeypatch):
    monkeypatch.setenv("NAVER_CLIENT_ID", "naver-id")
    monkeypatch.setenv("KIS_API_KEY", "kis-key")
    monkeypatch.setenv("DART_API_KEY", "dart-key")
    monkeypatch.setenv("FIN_US_TRACE_ID", "trace-id")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@db/prod")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")

    params = _stdio_server_params(Path("/tmp/mcp-news"))

    assert params.env["NAVER_CLIENT_ID"] == "naver-id"
    assert params.env["KIS_API_KEY"] == "kis-key"
    assert params.env["DART_API_KEY"] == "dart-key"
    assert params.env["FIN_US_TRACE_ID"] == "trace-id"
    assert "DATABASE_URL" not in params.env
    assert "OPENAI_API_KEY" not in params.env


def test_mcp_stdio_params_pass_order_dedup_ledger_settings(monkeypatch):
    monkeypatch.setenv("KIS_ORDER_DEDUP_PATH", "/var/lib/finus/kis-order-dedup.json")
    monkeypatch.setenv("KIS_ORDER_DEDUP_TTL_MS", "120000")

    params = _stdio_server_params(Path("/opt/mcp-trading"))

    assert params.env["KIS_ORDER_DEDUP_PATH"] == "/var/lib/finus/kis-order-dedup.json"
    assert params.env["KIS_ORDER_DEDUP_TTL_MS"] == "120000"


def test_visualization_url_is_trimmed_and_trailing_slash_preserved(monkeypatch):
    monkeypatch.setenv("VISUALIZATION_URL", " https://finus-visual.example/portfolio/ ")

    reloaded = importlib.reload(config)

    assert reloaded.VISUALIZATION_URL == "https://finus-visual.example/portfolio/"
