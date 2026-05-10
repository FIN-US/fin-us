from backend.config import NEWS_MCP_PARAMS, TRADING_MCP_PARAMS


def test_mcp_stdio_params_pass_environment_to_child_processes():
    assert isinstance(NEWS_MCP_PARAMS.env, dict)
    assert isinstance(TRADING_MCP_PARAMS.env, dict)
