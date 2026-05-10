# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig
from nat_finus_nat.finus_paths import fin_us_vendor_root


class _FinusVendorTimeout(Protocol):
    vendor_root: str | None
    timeout_sec: float


def _resolve_vendor_root(override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return fin_us_vendor_root()


def _node_deps_ready(server_dir: Path) -> bool:
    sdk = server_dir / "node_modules" / "@modelcontextprotocol" / "sdk"
    return sdk.is_dir()


async def _mcp_call_tool(
    *,
    vendor_root: Path,
    subdir: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_sec: float,
) -> str:
    server_dir = (vendor_root / subdir).resolve()
    script = (server_dir / "index.js").resolve()
    if not script.is_file():
        return json.dumps(
            {
                "error": "mcp_server_script_missing",
                "path": str(script),
                "hint": (
                    f"Expected MCP under {vendor_root}; run the install script "
                    f"(scripts/install_fin_us_mcp.sh from this repository root) "
                    "or set FINUS_VENDOR_ROOT."
                ),
            },
            ensure_ascii=False)

    if not _node_deps_ready(server_dir):
        news = vendor_root / "mcp-news"
        trade = vendor_root / "mcp-trading"
        return json.dumps(
            {
                "error": "mcp_node_dependencies_missing",
                "path": str(server_dir),
                "detail": "ERR_MODULE_NOT_FOUND @modelcontextprotocol/sdk — node_modules not installed.",
                "hint": (
                    f"cd {news} && npm ci; "
                    f"cd {trade} && npm ci"
                ),
            },
            ensure_ascii=False)

    params = StdioServerParameters(command="node", args=[str(script)], cwd=str(server_dir))

    async def _inner() -> str:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                if not result.content:
                    return ""
                block = result.content[0]
                return getattr(block, "text", str(block))

    try:
        return await asyncio.wait_for(_inner(), timeout=timeout_sec)
    except TimeoutError:
        return json.dumps({"error": "mcp_timeout", "tool": tool_name}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc), "tool": tool_name}, ensure_ascii=False)


def _vendor_and_timeout(config: _FinusVendorTimeout) -> tuple[Path, float]:
    return _resolve_vendor_root(config.vendor_root), config.timeout_sec


async def _mcp_news_stock(config: _FinusVendorTimeout, tool_name: str, stock_name: str) -> str:
    vr, timeout = _vendor_and_timeout(config)
    return await _mcp_call_tool(
        vendor_root=vr,
        subdir="mcp-news",
        tool_name=tool_name,
        arguments={"stock_name": stock_name},
        timeout_sec=timeout,
    )


async def _mcp_trading_balance(config: _FinusVendorTimeout) -> str:
    vr, timeout = _vendor_and_timeout(config)
    return await _mcp_call_tool(
        vendor_root=vr,
        subdir="mcp-trading",
        tool_name="get_balance",
        arguments={},
        timeout_sec=timeout,
    )


async def _mcp_trading_stock(config: _FinusVendorTimeout, tool_name: str, stock_name: str) -> str:
    vr, timeout = _vendor_and_timeout(config)
    return await _mcp_call_tool(
        vendor_root=vr,
        subdir="mcp-trading",
        tool_name=tool_name,
        arguments={"stock_name": stock_name},
        timeout_sec=timeout,
    )


class FinusMarketNewsConfig(FunctionBaseConfig, name="finus_market_news"):
    vendor_root: str | None = Field(default=None, description="Override parent dir containing mcp-news and mcp-trading.")
    timeout_sec: float = Field(default=120.0, ge=5.0, le=600.0)


class FinusInvestorTradingConfig(FunctionBaseConfig, name="finus_investor_trading"):
    vendor_root: str | None = Field(default=None)
    timeout_sec: float = Field(default=120.0, ge=5.0, le=600.0)


class FinusResearchReportsConfig(FunctionBaseConfig, name="finus_research_reports"):
    vendor_root: str | None = Field(default=None)
    timeout_sec: float = Field(default=120.0, ge=5.0, le=600.0)


class FinusAccountBalanceConfig(FunctionBaseConfig, name="finus_account_balance"):
    vendor_root: str | None = Field(default=None)
    timeout_sec: float = Field(default=180.0, ge=5.0, le=600.0)


@register_function(config_type=FinusMarketNewsConfig)
async def finus_market_news(config: FinusMarketNewsConfig, _builder: Builder):
    async def get_market_news(stock_name: str) -> str:
        return await _mcp_news_stock(config, "get_market_news", stock_name)

    yield FunctionInfo.from_fn(get_market_news, description=get_market_news.__doc__)


@register_function(config_type=FinusInvestorTradingConfig)
async def finus_investor_trading(config: FinusInvestorTradingConfig, _builder: Builder):
    async def get_investor_trading(stock_name: str) -> str:
        return await _mcp_trading_stock(config, "get_investor_trading", stock_name)

    yield FunctionInfo.from_fn(get_investor_trading, description=get_investor_trading.__doc__)


@register_function(config_type=FinusResearchReportsConfig)
async def finus_research_reports(config: FinusResearchReportsConfig, _builder: Builder):
    async def get_research_reports(stock_name: str) -> str:
        return await _mcp_news_stock(config, "get_research_reports", stock_name)

    yield FunctionInfo.from_fn(get_research_reports, description=get_research_reports.__doc__)


@register_function(config_type=FinusAccountBalanceConfig)
async def finus_account_balance(config: FinusAccountBalanceConfig, _builder: Builder):
    async def get_account_balance(placeholder: str) -> str:
        _ = placeholder
        return await _mcp_trading_balance(config)

    yield FunctionInfo.from_fn(get_account_balance, description=get_account_balance.__doc__)
