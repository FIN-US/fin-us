"""#400: MCP ``isError`` 응답이 도구 원장에 실패로 남고, 도구 강제 게이트가 그 턴의 수치 답변을 막는다.

mcp-dart는 회사명 매칭 실패를 예외가 아니라 ``isError: true``인 정상 응답으로 돌려준다
(``에러 발생: '…'와 정확히 일치하는 DART 상장회사 정보를 찾지 못했습니다.``). 텍스트만 꺼내
넘기던 시절에는 원장이 이를 ``ok=True, produced_rows=True``로 기록해, 게이트가 "도구로
확인했다"고 보고 수치 답변을 통과시켰다.

**대역은 MCP 서버 쪽에만 둔다.** 클라이언트는 프로덕션과 같은 진짜 ``mcp.ClientSession``이고,
서버는 진짜 ``mcp.server.lowlevel.Server``다 — 둘을 메모리 스트림으로 잇는다. 그래서 테스트가
받는 값은 JSON-RPC 직렬화를 거친 진짜 ``CallToolResult``이고, 세션의 호출 시그니처나 결과
객체 모양을 손으로 복제하지 않는다(#357·#360과 같은 이유). 바꾸는 것은 전송 계층뿐이다:
stdio 경로는 ``stdio_client``를, 원격(KIS pass-through) 경로는 ``_remote_mcp_session``을.

서버 응답은 각 MCP의 Node 소스가 실패 시 돌려주는 형식(``{content: [{type: "text", text}],
isError: true}``)과 문구를 그대로 따른다 — ``_NODE_ERROR_SOURCES``의 계약 테스트가 그 전제를
소스에서 확인한다.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock

import anyio
import pytest
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams
from nat.data_models.api_server import (
    ChatRequest,
    ChatResponse,
    Message,
    Usage,
    UserMessageContentRoleType,
)

from nat_finus_nat import finus_api
from nat_finus_nat.agents import _TOOL_ENFORCEMENT_REJECTION, _check_tool_enforcement, _run_with_gate
from nat_finus_nat.finus_api import DATA_TOOL_LEDGER, DataToolLedger
from nat_finus_nat.order_price_guard import parse_current_price

_REPO_ROOT = Path(__file__).resolve().parents[2]

# mcp-dart/corp-resolver.js의 매칭 실패 문구 + mcp-dart/index.js catch의 "에러 발생: " 접두어.
_DART_NOT_FOUND = "에러 발생: '없는회사'와 정확히 일치하는 DART 상장회사 정보를 찾지 못했습니다."


class _FakeMcpServer:
    """진짜 lowlevel MCP 서버 — 도구 이름별로 정해 둔 ``CallToolResult``를 돌려준다."""

    def __init__(self, results: dict[str, types.CallToolResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict]] = []
        self.server: Server = Server("fake-fin-us-mcp")

        # 클라이언트는 성공 결과의 outputSchema를 확인하려고 list_tools를 부른다 — 진짜 서버처럼 답한다.
        @self.server.list_tools()
        async def _list() -> list[types.Tool]:
            return [types.Tool(name=name, inputSchema={"type": "object"}) for name in self.results]

        @self.server.call_tool()
        async def _call(name: str, arguments: dict) -> types.CallToolResult:
            self.calls.append((name, arguments))
            return self.results[name]

    @asynccontextmanager
    async def streams(self):
        """클라이언트 쪽 (read, write) 스트림을 내주고, 반대편에서 서버를 돌린다."""
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    lambda: self.server.run(
                        server_streams[0],
                        server_streams[1],
                        self.server.create_initialization_options(),
                        raise_exceptions=True,
                    )
                )
                try:
                    yield client_streams
                finally:
                    tg.cancel_scope.cancel()


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


def _ok(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


@pytest.fixture
def vendor_root(tmp_path):
    """``_mcp_call_tool``의 사전 점검(스크립트·node_modules 존재)을 통과하는 가짜 vendor 루트."""
    for sub in ("mcp-dart", "mcp-news", "mcp-trading"):
        server_dir = tmp_path / sub
        (server_dir / "node_modules" / "@modelcontextprotocol" / "sdk").mkdir(parents=True)
        (server_dir / "index.js").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def stdio_server(monkeypatch):
    """stdio 경로의 전송만 메모리 스트림으로 바꾼다. 반환값에 도구 결과를 채워 쓴다."""
    fake = _FakeMcpServer({})

    def _stdio_client(params, *args, **kwargs):  # noqa: ARG001 — 서버 실행 인자는 이 대역과 무관
        return fake.streams()

    monkeypatch.setattr(finus_api, "stdio_client", _stdio_client)
    return fake


@pytest.fixture
def remote_server(monkeypatch):
    """원격(KIS pass-through) 경로의 세션을 메모리 스트림 위의 진짜 ClientSession으로 바꾼다."""
    fake = _FakeMcpServer({})

    @asynccontextmanager
    async def _session(**_kwargs):
        async with fake.streams() as (read, write):
            async with finus_api.ClientSession(read, write) as session:
                await session.initialize()
                yield session

    monkeypatch.setattr(finus_api, "_remote_mcp_session", _session)
    monkeypatch.setenv("FINUS_SKIP_MCP_LIST_TOOLS", "1")
    return fake


@pytest.fixture
def ledger():
    led = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(led)
    try:
        yield led
    finally:
        DATA_TOOL_LEDGER.reset(token)


def _req(text: str) -> ChatRequest:
    return ChatRequest(messages=[Message(role=UserMessageContentRoleType("user"), content=text)])


# ---- stdio 도구 래퍼 — mcp-dart·mcp-news·mcp-trading ----
#
# (NAT 도구 등록 함수, 설정 클래스, 호출 입력, 서버 쪽 MCP 도구명, 원장 이름)
# 래퍼를 NAT가 부르는 그대로 ``FunctionInfo.single_fn``으로 호출한다 — 래퍼가 ``_record_and_mask``에
# 넘기는 원장 이름까지 함께 검증된다.
_STDIO_TOOLS = [
    pytest.param(
        finus_api.finus_earnings_report,
        finus_api.FinusEarningsReportConfig,
        lambda info: info.single_fn(info.input_schema(stock_name="없는회사", period="2025Q1")),
        "get_earnings_report",
        "finus_earnings_report",
        id="mcp-dart-get_earnings_report",
    ),
    pytest.param(
        finus_api.finus_disclosure_signal,
        finus_api.FinusDisclosureSignalConfig,
        lambda info: info.single_fn(info.input_schema(stock_name="없는회사")),
        "get_disclosure_signal",
        "finus_disclosure_signal",
        id="mcp-dart-get_disclosure_signal",
    ),
    pytest.param(
        finus_api.finus_market_news,
        finus_api.FinusMarketNewsConfig,
        lambda info: info.single_fn(info.input_schema(stock_name="삼성전자")),
        "get_market_news",
        "finus_market_news",
        id="mcp-news-get_market_news",
    ),
    pytest.param(
        finus_api.finus_mcp_trading_get_balance,
        finus_api.FinusMcpTradingGetBalanceConfig,
        lambda info: info.single_fn(finus_api.FinusMcpTradingGetBalanceInput()),
        "get_balance",
        "finus_mcp_trading_get_balance",
        id="mcp-trading-get_balance",
    ),
    pytest.param(
        finus_api.finus_mcp_trading_today_orders,
        finus_api.FinusMcpTradingTodayOrdersConfig,
        lambda info: info.single_fn(finus_api.FinusMcpTradingTodayOrdersInput()),
        "get_today_daily_orders",
        "finus_mcp_trading_today_orders",
        id="mcp-trading-get_today_daily_orders",
    ),
    pytest.param(
        finus_api.finus_mcp_trading_balance_rlz_pl,
        finus_api.FinusMcpTradingBalanceRlzPlConfig,
        lambda info: info.single_fn(finus_api.FinusMcpTradingStockNameInput()),
        "get_balance_rlz_pl",
        "finus_mcp_trading_balance_rlz_pl",
        id="mcp-trading-get_balance_rlz_pl",
    ),
]

# 성공 응답 본문 — 각 도구의 빈 결과 리터럴(#209)에 걸리지 않는, 데이터가 있는 응답.
_SUCCESS_BODIES = {
    "get_earnings_report": "[실적 리포트] 삼성전자 2025Q1\n- 매출액: 79조 1,405억원 (YoY +10.1%)",
    "get_disclosure_signal": "[지분공시 signal] 삼성전자\n- 5% 룰 공시 1건",
    "get_market_news": "1. 삼성전자, 1분기 영업이익 6조6천억원",
    "get_balance": "[계좌 요약]\n- 총평가금액: 10,000,000원",
    "get_today_daily_orders": "[당일 주문·체결 내역] 20260922\n1. 삼성전자 매수 10주 체결",
    "get_balance_rlz_pl": "[보유 종목]\n- 삼성전자 10주\n\n[계좌 집계]\n- 실현손익: 12,000원",
}


async def _call_stdio_tool(register_fn, config_cls, invoke, vendor_root) -> str:
    config = config_cls(vendor_root=str(vendor_root), timeout_sec=10.0)
    async with register_fn(config, None) as info:
        return await invoke(info)


@pytest.mark.parametrize(("register_fn", "config_cls", "invoke", "mcp_tool", "ledger_name"), _STDIO_TOOLS)
async def test_stdio_is_error_is_recorded_as_failure(
    stdio_server, vendor_root, ledger, register_fn, config_cls, invoke, mcp_tool, ledger_name
):
    """``isError`` 응답 → 원장 ``ok=False``·``produced_rows=False``, Observation은 오류 JSON.

    뮤테이션: ``_mcp_call_tool_first_text``의 ``isError`` 분기를 지우면 원장이 ``ok=True``가 되어
    모든 파라미터가 실패한다.
    """
    stdio_server.results[mcp_tool] = _error(_DART_NOT_FOUND)

    observation = await _call_stdio_tool(register_fn, config_cls, invoke, vendor_root)

    assert [name for name, _ in stdio_server.calls] == [mcp_tool]
    assert [(r.tool_name, r.ok, r.produced_rows, r.empty) for r in ledger.records] == [
        (ledger_name, False, False, False)
    ]
    assert not ledger.any_success()
    # 에이전트는 MCP가 준 사유를 그대로 읽을 수 있어야 한다 — 오류 코드만 남기지 않는다.
    payload = json.loads(observation)
    assert payload == {"error": finus_api._MCP_TOOL_ERROR, "tool": mcp_tool, "detail": _DART_NOT_FOUND}


@pytest.mark.parametrize(("register_fn", "config_cls", "invoke", "mcp_tool", "ledger_name"), _STDIO_TOOLS)
async def test_stdio_success_is_recorded_as_success(
    stdio_server, vendor_root, ledger, register_fn, config_cls, invoke, mcp_tool, ledger_name
):
    """정상 응답은 종전 그대로 ``ok=True``·``produced_rows=True``이고 본문도 바뀌지 않는다.

    뮤테이션: ``isError`` 판정을 항상 참으로 바꾸면(모든 결과를 오류로 감싸면)
    이 테스트가 실패한다 — 위 테스트만으로는 "무조건 실패" 구현을 거르지 못한다.
    """
    body = _SUCCESS_BODIES[mcp_tool]
    stdio_server.results[mcp_tool] = _ok(body)

    observation = await _call_stdio_tool(register_fn, config_cls, invoke, vendor_root)

    assert [(r.tool_name, r.ok, r.produced_rows) for r in ledger.records] == [(ledger_name, True, True)]
    # 마스킹 대상 도구(잔고류)는 금액이 자리표시자로 바뀌므로, 오류로 감싸지지 않았다는 것만 본다.
    assert not observation.lstrip().startswith('{"error"')


async def test_gate_blocks_numeric_answer_when_the_only_tool_call_was_is_error(stdio_server, vendor_root):
    """이슈의 검증 시나리오: 없는 회사명으로 실적을 조회한 턴에서 수치 답변은 게이트에 막힌다.

    내부 에이전트는 두 시도 모두 실제 도구 래퍼(→ 진짜 MCP 클라이언트 → ``isError`` 응답)를
    부른 뒤 수치를 지어낸다. 원장에 실패만 있으므로 게이트가 두 번 트립하고 결정론적 거절
    문구가 반환돼야 한다.

    뮤테이션: ``isError`` 분기를 지우면 원장이 성공으로 기록되어 지어낸 답변이 그대로 반환된다.
    """
    stdio_server.results["get_earnings_report"] = _error(_DART_NOT_FOUND)
    fabricated = "Final Answer: 없는회사의 2025년 1분기 매출은 1조 2,345억원입니다."

    async def _inner_turn(_message):
        await _call_stdio_tool(
            finus_api.finus_earnings_report,
            finus_api.FinusEarningsReportConfig,
            lambda info: info.single_fn(info.input_schema(stock_name="없는회사", period="2025Q1")),
            vendor_root,
        )
        return ChatResponse.from_string(fabricated, usage=Usage())

    inner = MagicMock()
    inner.ainvoke = _inner_turn

    result = await _run_with_gate(
        inner=inner,
        query="없는회사 2025년 1분기 실적 알려줘",
        chat_request=_req("없는회사 2025년 1분기 실적 알려줘"),
        inner_name="news_agent_react",
    )

    assert result == _TOOL_ENFORCEMENT_REJECTION
    assert [name for name, _ in stdio_server.calls] == ["get_earnings_report", "get_earnings_report"]


async def test_gate_passes_numeric_answer_after_successful_tool_call(stdio_server, vendor_root, ledger):
    """대조군: 같은 도구가 정상 응답을 주면 같은 수치 답변이 게이트를 통과한다."""
    stdio_server.results["get_earnings_report"] = _ok(_SUCCESS_BODIES["get_earnings_report"])

    await _call_stdio_tool(
        finus_api.finus_earnings_report,
        finus_api.FinusEarningsReportConfig,
        lambda info: info.single_fn(info.input_schema(stock_name="삼성전자", period="2025Q1")),
        vendor_root,
    )

    answer = "Final Answer: 삼성전자의 2025년 1분기 매출은 79조 1,405억원입니다."
    assert _check_tool_enforcement(answer, ledger, _req("삼성전자 1분기 실적")) is False


# ---- 원격(KIS pass-through) 경로 ----


async def test_remote_is_error_is_recorded_as_failure(remote_server, ledger):
    """원격 MCP의 ``isError``도 같은 판정이다 — ``_mcp_call_tool_remote``는 같은 추출 함수를 쓴다.

    뮤테이션: ``isError`` 분기를 지우면 원장이 ``ok=True``가 된다.
    """
    remote_server.results["domestic_stock"] = _error("API 호출 실패: 유효하지 않은 종목코드")

    config = finus_api.FinusAccountBalanceReadonlyConfig(
        mcp_url="http://kis-mcp.invalid/mcp", trading_tool_name="domestic_stock"
    )
    async with finus_api.finus_account_balance_readonly(config, None) as info:
        observation = await info.single_fn(
            finus_api.KisTradingMcpCallInput(
                tool_name="domestic_stock",
                api_type="inquire_price",
                params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "999999"},
            )
        )

    assert [name for name, _ in remote_server.calls] == ["domestic_stock"]
    assert [(r.tool_name, r.ok, r.produced_rows) for r in ledger.records] == [
        (finus_api._KIS_BALANCE_LEDGER_NAME, False, False)
    ]
    assert json.loads(observation)["error"] == finus_api._MCP_TOOL_ERROR


async def test_remote_is_error_quote_is_not_read_as_a_current_price(remote_server):
    """지정가 괴리 가드의 현재가 조회도 같은 경로다 — ``isError`` 본문에 가격 모양의 텍스트가
    있어도 현재가로 읽히지 않는다(읽지 못하면 주문을 거부하는 fail-closed 쪽으로 떨어진다).

    뮤테이션: ``isError`` 분기를 지우면 ``stck_prpr: 68900``이 현재가로 읽힌다.
    """
    remote_server.results["domestic_stock"] = _error("조회 실패 (직전 캐시 stck_prpr: 68900)")

    text = await finus_api._mcp_call_tool_remote(
        transport="streamable-http",
        url="http://kis-mcp.invalid/mcp",
        tool_name="domestic_stock",
        arguments={"api_type": "inquire_price", "params": {}},
        timeout_sec=10.0,
    )

    assert parse_current_price(text) is None


# ---- 전제 계약: Node MCP 서버들이 실패를 isError로 돌려준다 ----
#
# 위 테스트는 "실패 = isError"라는 전제 위에 서 있다. 어느 MCP가 실패를 isError 없이 성공
# 텍스트로 돌려주기 시작하면 원장은 다시 성공으로 기록한다. 그 전제를 소스에서 고정한다.
_NODE_ERROR_SOURCES = {
    "mcp-dart/index.js": 2,  # get_disclosure_signal, get_earnings_report의 catch
    "mcp-news/index.js": 1,  # get_market_news의 catch
    "mcp-trading/index.js": 1,  # callTradingTool의 공용 catch
}


@pytest.mark.parametrize(("source", "min_catches"), list(_NODE_ERROR_SOURCES.items()))
def test_node_mcp_tool_catch_blocks_return_is_error(source, min_catches):
    text = (_REPO_ROOT / source).read_text(encoding="utf-8")
    # 도구 핸들러 catch가 "에러 발생: ${error.message}" 본문과 isError: true를 함께 돌려준다.
    catches = text.count("에러 발생: ${error.message}")
    assert catches >= min_catches, f"{source}: 도구 catch 블록 수가 줄었다 ({catches})"
    for chunk in text.split("에러 발생: ${error.message}")[1:]:
        assert "isError: true" in chunk[:200], f"{source}: 에러 본문을 isError 없이 돌려주는 catch가 있다"
