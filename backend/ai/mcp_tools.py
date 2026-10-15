"""stdio MCP 서버 도구 호출과 그 오류 요약."""

import logging
from typing import Any

from fastapi import HTTPException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)


def _find_http_exception(exc: BaseException) -> HTTPException | None:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            found = _find_http_exception(nested)
            if found is not None:
                return found
    return None


# run_mcp_tool 호출 전체(서브프로세스 기동·핸드셰이크 포함)의 벽시계 상한(초). 무기한 블로킹을
# 막는다. 연속조회 도구에 이보다 짧은 예산을 넘기는 호출자가 이 값에 기대므로
# (scheduler.portfolio._RLZ_PL_TIME_BUDGET_MS, mcp/trading/balance.js의 BALANCE_TIME_BUDGET_MS)
# 바꾸면 그 산정도 함께 본다. 시세 갱신 쪽 관계는 test_scheduler.py가 고정한다(#369).
MCP_TOOL_TIMEOUT_SECONDS = 30.0


async def run_mcp_tool(
    server_params: StdioServerParameters,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    import asyncio

    async def _call() -> str:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                if getattr(result, "isError", False):
                    block = result.content[0] if result.content else None
                    detail = getattr(block, "text", str(block)) if block else "MCP 도구 오류"
                    raise HTTPException(status_code=500, detail=detail)
                if not result.content:
                    return ""
                block = result.content[0]
                return getattr(block, "text", str(block))

    try:
        # MCP 서브프로세스 호출에 타임아웃 적용 — 무기한 블로킹 방지
        return await asyncio.wait_for(_call(), timeout=MCP_TOOL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        logger.error("MCP call_tool timed out for %s", tool_name)
        raise HTTPException(
            status_code=504,
            detail=f"데이터 공급원({tool_name}) 응답 타임아웃 ({MCP_TOOL_TIMEOUT_SECONDS:g}초)",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        http_exc = _find_http_exception(exc)
        if http_exc is not None:
            raise http_exc from exc
        logger.exception("MCP call_tool failed for %s", tool_name)
        raise HTTPException(
            status_code=500,
            detail=f"데이터 공급원({tool_name}) 연결 실패: {exc}",
        ) from exc


def short_error(exc: BaseException) -> str:
    """예외를 사용자 메시지에 넣을 수 있는 짧은 한 줄로 만든다.

    ``run_mcp_tool``이 올리는 HTTPException은 사유가 ``detail``에 들어 있고, 그 값은
    MCP 서브프로세스의 stderr까지 실려 수천 자가 되기도 한다. 그대로 사용자 메시지에
    박으면 텔레그램 4096자 한도에 걸려 **거부 메시지 자체가 전송되지 않는다** — 가장
    알려야 할 순간에 아무 말도 못 하게 되는 것이라 300자에서 자른다.

    텔레그램 핸들러(주문·조회 명령)와 order_assist(주문 보조)가 함께 쓴다. 두 곳이
    각자 자르면 한도가 갈라지므로 예외를 실제로 만들어 올리는 이 모듈에 둔다.
    """
    raw = getattr(exc, "detail", str(exc))
    text = str(raw or "").strip()
    if not text:
        text = exc.__class__.__name__
    return text[:300]
