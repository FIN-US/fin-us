from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client


KST = ZoneInfo("Asia/Seoul")
OrderSide = Literal["BUY", "SELL"]
McpTransport = Literal["sse", "streamable-http"]


@dataclass(frozen=True)
class PendingOrder:
    chat_id: str
    stock_name: str
    stock_code: str
    side: OrderSide
    quantity: int
    price: int
    created_at: datetime


@dataclass(frozen=True)
class OrderExecutionResult:
    stock_code: str
    stock_name: str
    side: OrderSide
    quantity: int
    price: int
    message: str
    raw_result: str


class TradeRecorder:
    def __init__(self, session_factory: Callable[[], Any]):
        self.session_factory = session_factory

    def record(self, result: OrderExecutionResult) -> None:
        from .models import TradeHistory

        session = self.session_factory()
        session.add(
            TradeHistory(
                stock_code=result.stock_code,
                stock_name=result.stock_name,
                trade_type=result.side,
                quantity=result.quantity,
                price=float(result.price),
            )
        )
        session.commit()


RemoteMcpRunner = Callable[..., Awaitable[str]]
_SSE_CONNECT_FLOOR = 5.0
_SSE_CONNECT_CAP = 30.0


def is_korean_market_open(now: datetime | None = None) -> bool:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)

    if current.weekday() >= 5:
        return False
    return time(9, 0) <= current.time() <= time(15, 30)


def _mcp_first_text_or_error(result: Any) -> str:
    blocks = getattr(result, "content", None) or []
    if blocks:
        block0 = blocks[0]
        text = getattr(block0, "text", str(block0))
    else:
        text = ""

    if getattr(result, "isError", False):
        raise HTTPException(
            status_code=502,
            detail=text or "KIS MCP 주문 실행 실패",
        )
    if not str(text or "").strip():
        raise HTTPException(
            status_code=502,
            detail="KIS MCP 주문 응답이 비어 있습니다.",
        )

    return text


def _sse_connect_timeout(operation_timeout: float) -> float:
    return min(_SSE_CONNECT_CAP, max(_SSE_CONNECT_FLOOR, operation_timeout * 0.25))


async def call_official_kis_mcp(
    *,
    transport: str,
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_sec: float,
) -> str:
    try:
        return await asyncio.wait_for(
            _call_official_kis_mcp_inner(
                transport=transport,
                url=url,
                tool_name=tool_name,
                arguments=arguments,
                timeout_sec=timeout_sec,
            ),
            timeout=timeout_sec,
        )
    except asyncio.CancelledError:
        raise
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=504,
            detail="KIS MCP 주문 요청 시간이 초과되었습니다.",
        ) from exc
    except HTTPException:
        raise
    except BaseExceptionGroup as exc:
        if _exception_group_contains(exc, asyncio.CancelledError):
            raise
        if _exception_group_contains(exc, (TimeoutError, httpx.TimeoutException)):
            raise HTTPException(
                status_code=504,
                detail="KIS MCP 주문 요청 시간이 초과되었습니다.",
            ) from exc
        if _exception_group_contains(exc, (httpx.HTTPError, OSError)):
            raise HTTPException(
                status_code=502,
                detail="KIS MCP 주문 요청에 실패했습니다.",
            ) from exc
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise HTTPException(
            status_code=502,
            detail="KIS MCP 주문 요청에 실패했습니다.",
        ) from exc


def _exception_group_contains(
    exc: BaseExceptionGroup,
    types: type[BaseException] | tuple[type[BaseException], ...],
) -> bool:
    for inner in exc.exceptions:
        if isinstance(inner, BaseExceptionGroup):
            if _exception_group_contains(inner, types):
                return True
        elif isinstance(inner, types):
            return True
    return False


async def _call_official_kis_mcp_inner(
    *,
    transport: str,
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_sec: float,
) -> str:
    if transport == "sse":
        conn = _sse_connect_timeout(timeout_sec)
        async with sse_client(url=url, timeout=conn, sse_read_timeout=timeout_sec) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return _mcp_first_text_or_error(await session.call_tool(tool_name, arguments))

    if transport == "streamable-http":
        async with httpx.AsyncClient(timeout=timeout_sec) as http_client:
            async with streamable_http_client(url=url, http_client=http_client) as (read, write, _get_sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return _mcp_first_text_or_error(await session.call_tool(tool_name, arguments))

    raise HTTPException(
        status_code=500,
        detail=f"지원하지 않는 KIS MCP transport입니다: {transport}",
    )


class OfficialKisMcpOrderGateway:
    def __init__(
        self,
        *,
        mcp_url: str,
        mcp_transport: McpTransport,
        tool_name: str,
        order_env: Literal["real", "demo"],
        real_order_enabled: bool,
        remote_runner: RemoteMcpRunner,
        timeout_sec: float = 180.0,
    ):
        self.mcp_url = mcp_url
        self.mcp_transport = mcp_transport
        self.tool_name = tool_name
        self.order_env = order_env
        self.real_order_enabled = real_order_enabled
        self.remote_runner = remote_runner
        self.timeout_sec = timeout_sec

    async def place_order(self, order: PendingOrder) -> OrderExecutionResult:
        if self.order_env == "real" and not self.real_order_enabled:
            raise HTTPException(
                status_code=403,
                detail="실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다.",
            )

        raw_result = await self.remote_runner(
            transport=self.mcp_transport,
            url=self.mcp_url,
            tool_name=self.tool_name,
            arguments={
                "api_type": "order_cash",
                "params": {
                    "env_dv": self.order_env,
                    "pdno": order.stock_code,
                    "ord_dvsn": "01",
                    "ord_qty": str(order.quantity),
                    "ord_unpr": str(order.price),
                    "buy_sell": order.side.lower(),
                },
            },
            timeout_sec=self.timeout_sec,
        )

        return OrderExecutionResult(
            stock_code=order.stock_code,
            stock_name=order.stock_name,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            message=_extract_order_message(raw_result),
            raw_result=raw_result,
        )


def _extract_order_message(raw_result: str) -> str:
    text = str(raw_result or "").strip()
    if not text:
        return "주문 요청이 접수되었습니다."

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]

    if isinstance(data, dict):
        for key in ("msg1", "message", "rt_msg", "output"):
            value = data.get(key)
            if value:
                return str(value)[:500]
    return text[:500]
