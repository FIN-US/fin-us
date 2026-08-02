import logging
from datetime import date
from urllib.parse import quote
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from backend import services


@pytest.fixture(autouse=True)
def _clear_stock_code_cache():
    services._stock_code_cache.clear()
    yield
    services._stock_code_cache.clear()


class FakeSession:
    def add(self, report):
        self.report = report

    def commit(self):
        pass

    def refresh(self, report):
        pass


class MockAsyncClient:
    def __init__(self, response: httpx.Response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self.response


def mock_async_client_factory(response: httpx.Response):
    def factory(*args, **kwargs):
        return MockAsyncClient(response)

    return factory


class FakeStdioClient:
    async def __aenter__(self):
        return "read", "write"

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMcpSession:
    def __init__(self, read, write):
        self.read = read
        self.write = write

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def initialize(self):
        return None

    async def call_tool(self, tool_name, arguments):
        return SimpleNamespace(
            isError=True,
            content=[SimpleNamespace(text="잔고 조회 에러 발생: 인증 실패")],
        )


@pytest.mark.asyncio
async def test_check_signal_significance_uses_generic_source_prompt(monkeypatch):
    prompts = []

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        prompts.append((provider, prompt))
        return "YES"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)

    result = await services.check_signal_significance(
        "삼성전자",
        "긍정 SNS 언급 급증",
        source="sns",
        provider="ollama",
    )

    assert result is True
    assert prompts[0][0] == "ollama"
    assert "signal 출처: sns" in prompts[0][1]
    assert "뉴스 내용" not in prompts[0][1]


@pytest.mark.asyncio
async def test_run_mcp_tool_raises_tool_level_error_detail(monkeypatch):
    monkeypatch.setattr(services, "stdio_client", lambda server_params: FakeStdioClient())
    monkeypatch.setattr(services, "ClientSession", FakeMcpSession)

    with pytest.raises(HTTPException) as exc_info:
        await services.run_mcp_tool(
            SimpleNamespace(),
            "get_balance",
            {},
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "잔고 조회 에러 발생: 인증 실패"


@pytest.mark.asyncio
async def test_perform_stock_analysis_includes_trigger_signal(monkeypatch):
    prompts = []

    captured_cids = []

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        prompts.append((provider, prompt))
        captured_cids.append(conversation_id)
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    await services.perform_stock_analysis(
        "삼성전자",
        "nat",
        FakeSession(),
        trigger_source="sns",
        trigger_signal="SNS mentions spiked after earnings guidance",
    )

    assert prompts[0][0] == "nat"
    encoded_stock = quote("삼성전자", safe="")
    assert captured_cids == [f"sns:{encoded_stock}:{date.today().isoformat()}"]
    assert "분석 트리거 데이터 출처: sns" in prompts[0][1]
    assert "SNS mentions spiked after earnings guidance" in prompts[0][1]
    assert '"source_signals"' in prompts[0][1]


@pytest.mark.asyncio
async def test_perform_stock_analysis_resolves_stock_code_via_mcp(monkeypatch):
    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    mcp_calls = []

    async def fake_run_mcp_tool(params, tool_name, arguments):
        mcp_calls.append((tool_name, arguments))
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    fake_session = FakeSession()
    await services.perform_stock_analysis("삼성전자", "openai", fake_session)

    assert fake_session.report.stock_code == "005930"
    assert fake_session.report.stock_name == "삼성전자"
    assert mcp_calls == [("resolve_stock_code", {"stock_name": "삼성전자"})]


@pytest.mark.asyncio
async def test_perform_stock_analysis_resolves_alphanumeric_stock_code(monkeypatch):
    """KIS 종목코드는 6자리 숫자만이 아니다.

    코스닥 스팩·리츠 등 종목마스터의 약 18%가 0001A0 같은 영숫자 코드다.
    숫자 6자리만 인정하면 MCP가 정확히 돌려준 코드를 백엔드가 버리게 된다.
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    mcp_calls = []

    async def fake_run_mcp_tool(params, tool_name, arguments):
        mcp_calls.append((tool_name, arguments))
        return "덕양에너젠 (0001A0, KOSDAQ)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    fake_session = FakeSession()
    await services.perform_stock_analysis("덕양에너젠", "openai", fake_session)

    assert fake_session.report.stock_code == "0001A0"
    assert mcp_calls == [("resolve_stock_code", {"stock_name": "덕양에너젠"})]


def test_looks_like_stock_code_accepts_alphanumeric_and_rejects_plain_names():
    assert services._looks_like_stock_code("005930")
    assert services._looks_like_stock_code("0001A0")
    # 숫자가 없는 6~7자 영문은 종목명일 가능성이 높으므로 코드로 보지 않는다.
    assert not services._looks_like_stock_code("SAMSUNG")
    assert not services._looks_like_stock_code("삼성전자")


@pytest.mark.asyncio
async def test_perform_stock_analysis_falls_back_to_empty_stock_code_on_mcp_failure(
    monkeypatch, caplog
):
    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def failing_run_mcp_tool(params, tool_name, arguments):
        raise HTTPException(status_code=500, detail="종목코드 조회 도구 오류")

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", failing_run_mcp_tool)

    fake_session = FakeSession()
    with caplog.at_level(logging.WARNING, logger=services.logger.name):
        result = await services.perform_stock_analysis("삼성전자", "openai", fake_session)

    # 종목코드 조회 실패해도 리포트 저장은 정상적으로 진행되어야 한다.
    assert fake_session.report.stock_code == ""
    assert fake_session.report.stock_name == "삼성전자"
    assert result is not None
    assert "종목코드 조회 실패" in caplog.text


@pytest.mark.asyncio
async def test_perform_stock_analysis_skips_mcp_when_stock_is_already_code(monkeypatch):
    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def unexpected_run_mcp_tool(params, tool_name, arguments):
        raise AssertionError("이미 종목코드가 주어진 경우 MCP를 호출하면 안 된다")

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", unexpected_run_mcp_tool)

    fake_session = FakeSession()
    await services.perform_stock_analysis("005930", "openai", fake_session)

    assert fake_session.report.stock_code == "005930"


@pytest.mark.asyncio
async def test_perform_stock_analysis_caches_resolved_stock_code(monkeypatch):
    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    mcp_calls = []

    async def fake_run_mcp_tool(params, tool_name, arguments):
        mcp_calls.append(arguments)
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    await services.perform_stock_analysis("삼성전자", "openai", FakeSession())
    await services.perform_stock_analysis("삼성전자", "openai", FakeSession())

    assert len(mcp_calls) == 1


@pytest.mark.asyncio
async def test_generate_morning_briefing_collects_market_watchlist_and_strategy(monkeypatch):
    calls = []
    prompts = []

    async def fake_run_mcp_tool(params, tool_name, arguments):
        calls.append((params, tool_name, arguments))
        if tool_name == "get_market_news":
            return f"{arguments['stock_name']} 뉴스"
        if tool_name == "get_balance":
            return "[보유 종목 리스트]\n- 삼성전자 (005930): 10주"
        if tool_name == "get_investor_trading":
            return f"{arguments['stock_name']} 외국인 순매수"
        raise AssertionError(tool_name)

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        prompts.append((provider, prompt, conversation_id))
        return (
            '{"market_summary":"전일 미국 증시 상승",'
            '"watchlist":["삼성전자: 외국인 순매수"],'
            '"trading_ideas":["삼성전자 눌림목 관찰"],'
            '"catalysts":["오늘 CPI 발표"]}'
        )

    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)
    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)

    briefing = await services.generate_morning_briefing(["삼성전자", "NAVER"])

    assert briefing == {
        "market_summary": "전일 미국 증시 상승",
        "watchlist": ["삼성전자: 외국인 순매수"],
        "trading_ideas": ["삼성전자 눌림목 관찰"],
        "catalysts": ["오늘 CPI 발표"],
    }
    assert [call[1:] for call in calls] == [
        ("get_market_news", {"stock_name": "미국 증시"}),
        ("get_balance", {}),
        ("get_market_news", {"stock_name": "삼성전자"}),
        ("get_investor_trading", {"stock_name": "삼성전자"}),
        ("get_market_news", {"stock_name": "NAVER"}),
        ("get_investor_trading", {"stock_name": "NAVER"}),
    ]
    assert prompts[0][0] == "nat"
    assert prompts[0][2] == f"morning-briefing:{date.today().isoformat()}"
    assert "Strategy Planner" in prompts[0][1]
    assert "NAVER 뉴스" in prompts[0][1]


def test_analysis_from_nat_text_backfills_source_signals():
    data = services.analysis_from_nat_text(
        (
            '{"summary":"요약",'
            '"details":{"decision":"HOLD","confidence_score":0.5,'
            '"reason":"근거","target_stock":"삼성전자"},'
            '"source_news":["기존 뉴스"],'
            '"trading_trend":null}'
        ),
        "삼성전자",
    )

    assert data["source_news"] == ["기존 뉴스"]
    assert data["source_signals"] == ["기존 뉴스"]


def test_analysis_from_nat_text_defaults_telegram_urgency_fields():
    data = services.analysis_from_nat_text(
        (
            '{"summary":"요약",'
            '"details":{"decision":"HOLD","confidence_score":0.5,'
            '"reason":"근거","target_stock":"삼성전자"},'
            '"source_news":["기존 뉴스"],'
            '"source_signals":["기존 signal"],'
            '"trading_trend":null}'
        ),
        "삼성전자",
    )

    assert data["urgency"] == "normal"
    assert data["urgency_reason"] is None
    assert data["telegram_alert"] is False


def test_find_http_exception_unwraps_nested_exception_group():
    http_exc = HTTPException(status_code=500, detail="잔고 조회 에러 발생: 인증 실패")
    grouped = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [
            ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [http_exc],
            )
        ],
    )

    assert services._find_http_exception(grouped) is http_exc


def test_analysis_from_nat_text_parses_telegram_urgency_fields():
    data = services.analysis_from_nat_text(
        (
            '{"summary":"긴급 요약",'
            '"details":{"decision":"SELL","confidence_score":0.8,'
            '"reason":"규제 리스크","target_stock":"삼성전자"},'
            '"source_news":["뉴스"],'
            '"source_signals":["signal"],'
            '"trading_trend":null,'
            '"urgency":"critical",'
            '"urgency_reason":"거래정지 위험",'
            '"telegram_alert":true}'
        ),
        "삼성전자",
    )

    assert data["urgency"] == "critical"
    assert data["urgency_reason"] == "거래정지 위험"
    assert data["telegram_alert"] is True


def test_analysis_from_nat_text_parses_nested_json_object():
    raw = (
        '{"summary":"도구 인증 설정 오류로 실시간 분석 불가",'
        '"details":{"decision":"HOLD","confidence_score":0.12,'
        '"reason":"뉴스 도구 인증 오류","target_stock":"삼성전자"},'
        '"source_news":[],"trading_trend":null}'
    )

    result = services.analysis_from_nat_text(raw, "삼성전자")

    assert result["summary"] == "도구 인증 설정 오류로 실시간 분석 불가"
    assert result["details"]["decision"] == "HOLD"
    assert result["details"]["confidence_score"] == 0.12
    assert result["details"]["reason"] == "뉴스 도구 인증 오류"
    assert result["details"]["target_stock"] == "삼성전자"
    assert result["source_news"] == []
    assert result["trading_trend"] is None


def test_analysis_from_nat_text_extracts_json_object_from_wrapped_text():
    raw = (
        "분석 결과입니다.\n"
        '{"summary":"요약",'
        '"details":{"decision":"BUY","confidence_score":0.7,'
        '"reason":"수급 개선","target_stock":"삼성전자"},'
        '"source_news":["헤드라인"],"trading_trend":"외국인 순매수"}'
    )

    result = services.analysis_from_nat_text(raw, "삼성전자")

    assert result["summary"] == "요약"
    assert result["details"]["decision"] == "BUY"
    assert result["source_news"] == ["헤드라인"]
    assert result["trading_trend"] == "외국인 순매수"


@pytest.mark.asyncio
async def test_llm_nat_chat_logs_bounded_response_preview(monkeypatch, caplog):
    long_content = "분석 결과" + ("x" * 1200)
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": long_content}}]},
    )
    monkeypatch.setattr(
        services.httpx,
        "AsyncClient",
        mock_async_client_factory(response),
    )

    with caplog.at_level(logging.DEBUG, logger=services.logger.name):
        result = await services._llm_nat_chat("삼성전자 분석")

    assert result == long_content
    assert "NAT response received: status_code=200 body_length=" in caplog.text
    assert "NAT response body preview:" in caplog.text
    assert "x" * 900 not in caplog.text


@pytest.mark.asyncio
async def test_llm_nat_chat_logs_json_parse_failure(monkeypatch, caplog):
    response = httpx.Response(200, text="not-json")
    monkeypatch.setattr(
        services.httpx,
        "AsyncClient",
        mock_async_client_factory(response),
    )

    with caplog.at_level(logging.DEBUG, logger=services.logger.name):
        with pytest.raises(HTTPException) as exc_info:
            await services._llm_nat_chat("삼성전자 분석")

    assert exc_info.value.status_code == 502
    assert "NAT response received: status_code=200 body_length=8" in caplog.text
    assert "Failed to parse NAT response JSON: status_code=200" in caplog.text
    assert "NAT response body preview: not-json" in caplog.text
