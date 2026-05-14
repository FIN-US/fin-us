from fastapi.testclient import TestClient

from backend.main import TRADING_MCP_PARAMS, app


def test_get_news_response_shape(monkeypatch):
    async def mock_run_mcp_tool(params, tool_name, arguments):
        assert tool_name == "get_market_news"
        assert arguments == {"stock_name": "삼성전자"}
        return "뉴스1\n뉴스2\n뉴스3"

    monkeypatch.setattr("backend.main.run_mcp_tool", mock_run_mcp_tool)

    with TestClient(app) as client:
        response = client.get("/api/v1/news", params={"stock": "삼성전자"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "message": None,
        "data": {"stock": "삼성전자", "news": ["뉴스1", "뉴스2", "뉴스3"]},
    }


def test_get_trading_trend_response_shape_and_tool_route(monkeypatch):
    captured = {}

    async def mock_run_mcp_tool(params, tool_name, arguments):
        captured["params"] = params
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        return "[삼성전자] 투자자 매매동향"

    monkeypatch.setattr("backend.main.run_mcp_tool", mock_run_mcp_tool)

    with TestClient(app) as client:
        response = client.get("/api/v1/trading/trend", params={"stock": "삼성전자"})

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "message": None,
        "data": {"stock": "삼성전자", "trend": "[삼성전자] 투자자 매매동향"},
    }
    assert captured["tool_name"] == "get_investor_trading"
    assert captured["arguments"] == {"stock_name": "삼성전자"}
    assert captured["params"] == TRADING_MCP_PARAMS
