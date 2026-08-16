import ast
import asyncio
import inspect
import logging
import pathlib
import re
import typing
from datetime import date
from urllib.parse import quote
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from backend import services


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
async def test_provider_supports_tools_matches_llm_chat_dispatch(monkeypatch):
    """provider_supports_tools()가 llm_chat의 실제 라우팅과 어긋나지 않는지 확인한다.

    이전 버전은 provider_is_tool_backed의 출력을 하드코딩된 기대값 4개와만
    비교했다 - llm_chat 자체의 라우팅이 깨져도(예: openai를 실수로 _llm_nat_chat에
    연결해도) 통과했다. 여기서는 각 provider_key로 실제 services.llm_chat()을
    호출해 내부적으로 어떤 _llm_*_chat 구현이 실행됐는지 관측하고, 그 관측치와
    provider_supports_tools()의 답을 대조한다.

    typing.get_args로 llm_chat의 provider_key Literal을 그대로 열거하므로,
    다섯 번째 provider가 Literal과 dispatch에는 추가됐는데 tool_dispatch_targets에는
    반영되지 않으면 provider_supports_tools()의 반환값과 expected가 어긋나
    마지막 assert에서 실패한다 - 하드코딩된 목록이 아니라 실제 시그니처를
    따라간다.
    """
    invoked: list[str] = []

    async def fake_openai(user_msg):
        invoked.append("openai")
        return "ok"

    async def fake_anthropic(user_msg):
        invoked.append("anthropic")
        return "ok"

    async def fake_ollama(user_msg):
        invoked.append("ollama")
        return "ok"

    async def fake_nat(user_msg, *, conversation_id=None):
        invoked.append("nat")
        return "ok"

    monkeypatch.setattr(services, "_llm_openai_chat", fake_openai)
    monkeypatch.setattr(services, "_llm_anthropic_chat", fake_anthropic)
    monkeypatch.setattr(services, "_llm_ollama_chat", fake_ollama)
    monkeypatch.setattr(services, "_llm_nat_chat", fake_nat)

    annotation = inspect.signature(services.llm_chat).parameters["provider_key"].annotation
    provider_keys = typing.get_args(annotation)
    assert provider_keys, "llm_chat의 provider_key가 Literal이 아니게 되면 이 전제부터 깨진다"

    # llm_chat이 실제로 도구를 쓰는 핸들러로 보내는 내부 구현 이름.
    # provider_key 자체가 아니라 "어디로 라우팅됐는가"로 판정한다.
    tool_dispatch_targets = {"nat"}

    for key in provider_keys:
        invoked.clear()
        await services.llm_chat(key, "테스트")
        assert len(invoked) == 1, f"{key}가 정확히 하나의 _llm_*_chat 구현으로 라우팅되어야 한다"
        dispatched_to = invoked[0]
        expected = dispatched_to in tool_dispatch_targets
        assert services.provider_supports_tools(key) is expected, (
            f"provider_key={key!r}가 실제로는 {dispatched_to!r}로 라우팅되는데 "
            f"provider_supports_tools({key!r})={services.provider_supports_tools(key)}은 "
            f"{expected}와 어긋난다"
        )


@pytest.mark.asyncio
async def test_toolless_providers_pass_no_tools_to_the_model(monkeypatch):
    """provider_supports_tools()가 False인 provider는 실제로 tools 없이 모델을 호출해야 한다."""
    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
            )

    monkeypatch.setattr(
        services,
        "AsyncOpenAI",
        lambda api_key: SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        ),
    )
    monkeypatch.setattr(services, "OPENAI_API_KEY", "test-key")
    await services._llm_openai_chat("hi")
    assert "tools" not in captured, (
        "openai가 tools를 넘기면 provider_supports_tools(False)가 거짓이 된다"
    )


@pytest.mark.asyncio
async def test_perform_stock_analysis_marks_toolless_provider_report_unverified(monkeypatch):
    """도구 없이 호출되는 provider(openai/anthropic/ollama)로 만든 AgentReport는
    provider_supports_tools=False로 저장되고, API 응답 payload에도 같은 값이
    실려야 한다. provider_supports_tools가 항상 True를 반환하도록 뒤집으면(또는
    perform_stock_analysis가 이 값을 무시하면) 이 테스트가 깨진다.

    A (#162): 도구 없는 provider는 decision/confidence_score를 생성하지 않으므로
    AgentReport에 null로 저장되고 API 응답 payload에도 details=None이어야 한다.
    decision=None이 아니라 HOLD/0.5로 채워지면 이 테스트가 깨진다.
    """
    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    session = FakeSession()
    result = await services.perform_stock_analysis("삼성전자", "openai", session)

    assert session.report.provider_supports_tools is False
    assert result["provider"] == "openai"
    assert result["provider_supports_tools"] is False
    # A (#162): 도구 없는 provider는 매매 판단을 생성하지 않는다.
    assert session.report.decision is None
    assert session.report.confidence_score is None
    assert result.get("details") is None


@pytest.mark.asyncio
async def test_perform_stock_analysis_marks_nat_report_tool_supporting(monkeypatch):
    """provider=nat으로 만든 AgentReport는 provider_supports_tools=True로 저장되고,
    API 응답 payload에도 같은 값이 실려야 한다. (#162 수용 기준: provider=nat
    경로의 동작은 불변). _TOOL_CAPABLE_PROVIDERS에서 "nat"이 빠지면 이 테스트가
    깨진다.
    """
    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    session = FakeSession()
    result = await services.perform_stock_analysis("삼성전자", "nat", session)

    assert session.report.provider_supports_tools is True
    assert result["provider"] == "nat"
    assert result["provider_supports_tools"] is True


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


# test_looks_like_stock_code_* → backend/tests/test_stock_code.py (#140)


def test_normalize_stock_input_handles_interleaved_bom_and_whitespace():
    """공백과 BOM(U+FEFF)이 번갈아 나오는 입력도 JS String.trim()처럼 끝까지 벗겨낸다.

    strip()을 세 단계로 나눠 부르는 방식은 한쪽을 벗기다 멈춘 자리에서 그대로
    멈춰서, 공백과 BOM이 교대로 나오는 입력을 끝까지 벗기지 못했다. 문자열
    내부의 BOM은 이 함수가 여전히 지우지 않는다(캐시 키 뭉개짐 방지).
    """
    assert services._normalize_stock_input(" ﻿ ﻿SAMSUNG") == "SAMSUNG"
    assert services._normalize_stock_input("﻿ ﻿005930") == "005930"
    assert services._normalize_stock_input("SAM﻿SUNG") == "SAM﻿SUNG"


@pytest.mark.asyncio
async def test_perform_stock_analysis_falls_back_to_empty_stock_code_on_mcp_failure(
    monkeypatch, caplog
):
    call_count = 0

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def failing_run_mcp_tool(params, tool_name, arguments):
        nonlocal call_count
        call_count += 1
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

    # 예외 경로 결과도 캐시되면 안 된다. cached("") is not None이 True가 되어
    # 두 번째 호출이 MCP를 건너뛰게 되는 회귀를 막는다.
    await services.perform_stock_analysis("삼성전자", "openai", FakeSession())

    assert call_count == 2
    assert services._stock_code_cache == {}


@pytest.mark.asyncio
async def test_perform_stock_analysis_falls_back_to_empty_stock_code_on_extraction_failure(
    monkeypatch, caplog
):
    """resolveStock은 찾을 수 없음/모호함 모두 예외를 던지므로, 추출 실패로 이어지는
    성공 응답은 run_mcp_tool 자체의 `if not result.content: return ""` 경로에서만
    실제로 발생한다. 그래서 스텁도 빈 문자열을 그대로 돌려준다.
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return ""

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    fake_session = FakeSession()
    with caplog.at_level(logging.WARNING, logger=services.logger.name):
        result = await services.perform_stock_analysis("삼성전자", "openai", fake_session)

    assert fake_session.report.stock_code == ""
    assert result is not None
    # "종목코드 추출 실패"(match is None)와 "종목코드 조회 실패"(예외)는 다른 분기다.
    # if match is None 가드가 사라지면 match.group(1)이 AttributeError를 던지고
    # 바깥 except가 그것도 삼켜 stock_code는 여전히 ""가 되므로, stock_code 단정만으로는
    # 이 가드가 사라진 회귀를 잡지 못한다. 로그 문구가 실질적인 증거다.
    assert "종목코드 추출 실패" in caplog.text


@pytest.mark.asyncio
async def test_perform_stock_analysis_extraction_failure_not_cached(monkeypatch):
    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    mcp_calls = []

    async def fake_run_mcp_tool(params, tool_name, arguments):
        mcp_calls.append(arguments)
        return ""

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    await services.perform_stock_analysis("삼성전자", "openai", FakeSession())
    await services.perform_stock_analysis("삼성전자", "openai", FakeSession())

    # 캐시된 값이 없어야 두 번째 호출도 MCP를 다시 탄다. `if cached is not None`이
    # `if cached:`로 바뀌면 캐시된 ""도 참으로 취급되어 이 단정이 깨진다.
    assert len(mcp_calls) == 2
    assert services._stock_code_cache == {}


@pytest.mark.asyncio
async def test_perform_stock_analysis_rejects_digitless_extracted_code(monkeypatch, caplog):
    """SIMPAC(009160)은 실제 KOSPI 상장 종목명이 숫자 없는 6자 영문이다.

    resolveStock은 SIMPAC을 마스터의 종목명으로 매칭하므로(#174 이후에는 코드
    완전일치가 먼저 시도되지만 SIMPAC은 코드가 아니라 이름이라 그 단계에서 빗나가고
    이름 매칭이 잡는다) "SIMPAC (SIMPAC, UNKNOWN)"이 실제 마스터 데이터로 나오는
    경로는 없다. 이 테스트는 그 경로를 가정하지 않는다 — MCP가 어떤 이유로든(마스터
    갱신 지연, 다른 소스 등) 숫자 없는 코드를 돌려줬을 때 `_STOCK_CODE_EXTRACT_RE`가
    그 값을 코드처럼 추출하더라도 `_has_code_digit`이 걸러내는지를 직접
    검증한다. 종목마스터에서 같은 형태인 이름은 INVENI(015360), WISCOM(024070)
    두 개뿐이다.
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "SIMPAC (SIMPAC, UNKNOWN)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    fake_session = FakeSession()
    with caplog.at_level(logging.WARNING, logger=services.logger.name):
        await services.perform_stock_analysis("SIMPAC", "openai", fake_session)

    assert fake_session.report.stock_code == ""
    assert "종목코드 추출 실패" in caplog.text


@pytest.mark.asyncio
async def test_perform_stock_analysis_digitless_extracted_code_not_cached(monkeypatch):
    """이름과 코드가 같은 SIMPAC 응답 대신 서로 다른 응답으로 digit guard의
    no-cache 경로를 실제로 태운다.

    이전 버전은 "SIMPAC (SIMPAC, UNKNOWN)"을 썼는데, 이름==코드라 `_has_code_digit`을
    지워도 에코 스킵(name_part == code, Unit 4)이 먼저 캐싱을 막아 이 테스트가
    `..._does_not_cache_shortcut_echo`와 같은 것을 검증하는 셈이 되어 있었다
    (리뷰어가 뮤테이션으로 확인). 이름이 코드와 다른 "가상종목 (ABCDEF, KOSDAQ)"으로
    바꾸면 에코 스킵은 트리거되지 않고, 숫자 없는 코드를 걸러내는 것은
    `_has_code_digit`뿐이므로 그 가드가 실제로 캐싱을 막는지를 검증하게 된다.
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    mcp_calls = []

    async def fake_run_mcp_tool(params, tool_name, arguments):
        mcp_calls.append(arguments)
        return "가상종목 (ABCDEF, KOSDAQ)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    await services.perform_stock_analysis("가상종목", "openai", FakeSession())
    await services.perform_stock_analysis("가상종목", "openai", FakeSession())

    assert len(mcp_calls) == 2
    assert services._stock_code_cache == {}


@pytest.mark.asyncio
async def test_perform_stock_analysis_resolves_nine_char_fund_code(monkeypatch):
    """회귀 가드용 테스트 — 이 유닛의 변경 전/후 모두 통과한다.

    실제 stocks.json 항목(코드 F70100026, 종목명 "한투글로벌넥스트웨이브1(A)",
    시장 KOSPI)을 실제 응답 포맷 "{name} ({code}, {market})"으로 재현한다.
    이 테스트가 막으려는 것은 추출부에서 `_looks_like_stock_code`를 재사용하려는
    유혹이다. 그 함수의 `{6,7}` 상한은 9자 펀드 코드 75종 전부를 조용히 떨어뜨린다.
    또한 종목명에 괄호가 포함된 경우("...1(A)")에도 코드+쉼표 앵커링이
    올바르게 동작하는지도 함께 고정한다.
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "한투글로벌넥스트웨이브1(A) (F70100026, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    fake_session = FakeSession()
    await services.perform_stock_analysis("한투글로벌넥스트웨이브1(A)", "openai", fake_session)

    assert fake_session.report.stock_code == "F70100026"


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
async def test_perform_stock_analysis_skips_mcp_for_known_master_fund_code(monkeypatch):
    """#151과 별개로 #140의 지름길(9자 펀드 코드)을 회귀시키면 안 된다.

    F70100026은 종목마스터 실제 항목(수용 기준: 존재하는 펀드 코드는 정상 해석).
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def unexpected_run_mcp_tool(params, tool_name, arguments):
        raise AssertionError("마스터에 실재하는 코드는 MCP를 호출하면 안 된다")

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", unexpected_run_mcp_tool)

    fake_session = FakeSession()
    await services.perform_stock_analysis("F70100026", "openai", fake_session)

    assert fake_session.report.stock_code == "F70100026"


@pytest.mark.asyncio
async def test_perform_stock_analysis_master_unknown_code_not_saved_without_verification(
    monkeypatch, caplog
):
    """#151 수용 기준: 마스터에 없는 코드 형태 입력(999999)은 존재 확인 없이
    저장되면 안 된다.

    지름길이 마스터 대조에서 걸러지면 아래 MCP 경로로 흘러간다. 999999는
    stock-master.js CODE_SHAPE_PATTERN에 걸리는 6자 숫자라 Step 3가 예외 없이
    market="UNKNOWN"으로 그대로 에코하므로(존재 검증이 아니라 되돌려주는 것),
    이 에코를 성공으로 오인하면 #151이 재발한다.

    뮤테이션 ①(마스터 대조 제거)이 들어가면 이 테스트는 MCP를 호출하지 않고
    stock_code == "999999"를 저장해 red가 된다.
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    mcp_calls = []

    async def fake_run_mcp_tool(params, tool_name, arguments):
        mcp_calls.append(arguments)
        return "999999 (999999, UNKNOWN)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    fake_session = FakeSession()
    with caplog.at_level(logging.WARNING, logger=services.logger.name):
        await services.perform_stock_analysis("999999", "openai", fake_session)

    # 마스터 대조에서 걸러져 지름길을 포기하고 실제로 MCP 경로를 탔는지 확인한다.
    assert mcp_calls == [{"stock_name": "999999"}]
    # market="UNKNOWN" 에코를 성공으로 오인하지 않고 빈 문자열로 폴백해야 한다.
    assert fake_session.report.stock_code == ""
    assert "종목코드 추출 실패" in caplog.text


@pytest.mark.asyncio
async def test_perform_stock_analysis_bom_prefixed_name_caches_under_normalized_key(
    monkeypatch,
):
    """BOM(U+FEFF)이 붙은 "종목명" 입력은 에코 스킵(위 테스트)에 걸리지 않는다.

    JS String.trim()은 BOM을 지우지만 Python str.strip()은 그대로 두므로,
    "﻿삼성전자"는 MCP 호출까지는 가지만 stock-master.js 쪽에서는 정확한 이름 매칭으로
    끝나 응답의 종목명("삼성전자")이 코드("005930")와 다르다. 즉 에코 스킵 조건을
    통과해 캐싱 경로를 탄다. 이때 캐시 키가 정규화되지 않으면 BOM 개수만큼 서로 다른
    키가 쌓인다 — 이 테스트는 정규화로 그것이 하나의 키로 합쳐짐을 고정한다.
    """

    mcp_calls = []

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        mcp_calls.append(arguments)
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    await services.perform_stock_analysis("﻿삼성전자", "openai", FakeSession())

    assert len(mcp_calls) == 1
    assert services._stock_code_cache == {"삼성전자": "005930"}

    # 서로 다른 BOM 변형도 같은 정규화 키로 수렴해야 MCP를 다시 타지 않는다.
    await services.perform_stock_analysis("﻿﻿삼성전자", "openai", FakeSession())

    assert len(mcp_calls) == 1
    assert services._stock_code_cache == {"삼성전자": "005930"}


@pytest.mark.asyncio
async def test_perform_stock_analysis_interior_bom_name_not_served_from_unrelated_cache(
    monkeypatch,
):
    """내부 BOM 정규화(구 버전의 replace 방식)에 대한 회귀 가드.

    "삼성﻿전자"의 BOM은 문자열 중간에 있다. JS String.trim()은 양끝만 자르므로
    stock-master.js가 실제로 받는 질의 문자열도 BOM을 포함한 그대로다. 정규화가
    내부 BOM까지 지우면(replace) 이 입력이 "삼성전자" 캐시 엔트리와 충돌해, 결과가
    무관한 이전 요청 이력에 좌우된다. 정규화는 양끝만 다뤄야 이 입력이 그 캐시
    엔트리를 재사용하지 않고 매번 MCP로 간다.
    """
    mcp_calls = []

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        mcp_calls.append(arguments)
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    # "삼성전자" 캐시 엔트리를 먼저 채운다.
    await services.perform_stock_analysis("삼성전자", "openai", FakeSession())
    assert services._stock_code_cache == {"삼성전자": "005930"}
    assert len(mcp_calls) == 1

    # 내부에 BOM이 낀 입력은 위 캐시 엔트리를 재사용하면 안 되고 MCP를 다시 타야 한다.
    interior_bom_session = FakeSession()
    await services.perform_stock_analysis("삼성﻿전자", "openai", interior_bom_session)

    assert len(mcp_calls) == 2
    assert mcp_calls[1] == {"stock_name": "삼성﻿전자"}
    assert interior_bom_session.report.stock_code == "005930"


@pytest.mark.asyncio
async def test_perform_stock_analysis_bom_prefixed_code_skips_mcp(monkeypatch):
    """BOM 붙은 종목코드는 정규화 후 MCP 호출 없이 바로 반환되어야 한다.

    입력 판정(_looks_like_stock_code)이 정규화 이전의 원본 문자열에 적용되면
    "﻿005930"은 코드로 인식되지 못해 MCP를 호출한다. stock-master.js 지름길이
    응답을 그대로 돌려줘 에코 스킵으로 캐싱은 막히지만, 동일 입력이 올 때마다
    매번 새 MCP 서브프로세스가 뜨는 문제(run_mcp_tool의 30초 타임아웃이 상한인 지연)가
    남는다. 정규화를 판정보다 앞에 두면 MCP 호출 자체가 없어야 한다.
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def unexpected_run_mcp_tool(params, tool_name, arguments):
        raise AssertionError("BOM 붙은 종목코드는 정규화 후 MCP를 호출하면 안 된다")

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", unexpected_run_mcp_tool)

    fake_session = FakeSession()
    await services.perform_stock_analysis("﻿005930", "openai", fake_session)

    assert fake_session.report.stock_code == "005930"


@pytest.mark.asyncio
async def test_perform_stock_analysis_stock_code_cache_is_capped(monkeypatch, caplog):
    """상한 도달 시 조용히 쓰기를 건너뛰지 않고, 최초(가장 오래된) 항목을 축출한
    뒤 로그를 남기고 새 항목을 넣는다. dict는 삽입 순서를 보존하므로
    ``next(iter(...))``가 FIFO로 가장 오래된 키를 가리킨다.
    """

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "새종목 (999999, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    services._stock_code_cache.update(
        {f"기존종목{i}": f"{i:06d}" for i in range(services._STOCK_CODE_CACHE_MAX)}
    )

    fake_session = FakeSession()
    with caplog.at_level(logging.WARNING, logger=services.logger.name):
        await services.perform_stock_analysis("새종목", "openai", fake_session)

    assert fake_session.report.stock_code == "999999"
    # 상한을 유지한 채 가장 오래된 항목("기존종목0")은 축출되고 새 항목은 들어와야 한다.
    assert len(services._stock_code_cache) == services._STOCK_CODE_CACHE_MAX
    assert "새종목" in services._stock_code_cache
    assert "기존종목0" not in services._stock_code_cache
    assert "종목코드 캐시 상한 도달" in caplog.text


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


# ── A (#162): 도구 없는 provider 출력 범위 축소 ──────────────────────────────


def test_build_toolless_prompt_does_not_request_decision_or_confidence_score():
    """도구 없는 provider용 프롬프트는 BUY/SELL/HOLD나 confidence_score를
    요청해서는 안 된다. 이 두 단어가 프롬프트에 포함되면 모델이 매매 판단을
    지어낼 수 있다 — 프롬프트를 제거하면 이 테스트가 깨진다.
    """
    prompt = services._build_toolless_prompt("삼성전자", "")
    # JSON 포맷으로 매매 판단을 요청하는 표현이 없어야 한다.
    # 설명 문구에 "BUY/SELL/HOLD"가 언급될 수 있으므로 JSON 키·값 형식으로만 확인한다.
    assert '"decision"' not in prompt
    assert '"confidence_score"' not in prompt
    # JSON 포맷 내에서 BUY/SELL/HOLD 옵션 지정("BUY"|"SELL"|"HOLD")이 없어야 한다.
    assert '"BUY"' not in prompt
    assert '"SELL"' not in prompt


def test_build_nat_prompt_includes_decision_and_confidence_score():
    """NAT 프롬프트는 도구를 갖춘 경로 전용이므로 BUY/SELL/HOLD 판단과
    confidence_score를 요청해야 한다. 이 두 항목이 빠지면 NAT 응답을
    파싱하는 analysis_from_nat_text가 결과를 얻지 못한다 — nat 경로의
    동작이 불변이어야 한다는 수용 기준(#162)을 이 테스트가 고정한다.
    """
    prompt = services._build_nat_prompt("삼성전자", "")
    assert '"decision"' in prompt
    assert '"confidence_score"' in prompt
    assert '"source_signals"' in prompt


@pytest.mark.asyncio
async def test_toolless_provider_does_not_generate_decision(monkeypatch):
    """도구 없는 provider(openai/anthropic/ollama)가 perform_stock_analysis를
    통해 저장하는 AgentReport의 decision/confidence_score는 반드시 None이어야
    한다. _build_toolless_prompt 대신 _build_nat_prompt를 쓰거나 파싱 후
    fallback 값("HOLD"/0.5)을 채우면 이 테스트가 깨진다.
    """
    captured_prompts: list[str] = []

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        captured_prompts.append(prompt)
        return "plain analysis"

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    for provider in ("openai", "anthropic", "ollama"):
        captured_prompts.clear()
        session = FakeSession()
        result = await services.perform_stock_analysis("삼성전자", provider, session)

        # DB
        assert session.report.decision is None, (
            f"provider={provider}: decision은 None이어야 한다 (받은 값: {session.report.decision!r})"
        )
        assert session.report.confidence_score is None, (
            f"provider={provider}: confidence_score는 None이어야 한다"
        )
        # API 응답
        assert result.get("details") is None, (
            f"provider={provider}: details는 None이어야 한다 (받은 값: {result.get('details')!r})"
        )
        # 프롬프트에 매매 판단 형식이 없어야 함
        assert len(captured_prompts) == 1
        assert '"decision"' not in captured_prompts[0], (
            f"provider={provider}: 도구 없는 프롬프트에 '\"decision\"'이 포함되면 안 된다"
        )


@pytest.mark.asyncio
async def test_nat_provider_decision_is_preserved(monkeypatch):
    """provider=nat은 도구를 갖춘 경로이므로 BUY/SELL/HOLD 판단·신뢰도 점수를
    생성하고 AgentReport에 저장해야 한다. (#162 수용 기준: provider=nat 경로의
    동작은 불변.) _build_nat_prompt 대신 _build_toolless_prompt를 쓰거나
    decision을 null로 저장하면 이 테스트가 깨진다.
    """
    nat_response = (
        '{"summary":"삼성전자 분석",'
        '"details":{"decision":"BUY","confidence_score":0.8,"reason":"수급 개선","target_stock":"삼성전자"},'
        '"source_news":["헤드라인"],"source_signals":["signal"],"trading_trend":"외국인 순매수",'
        '"urgency":"normal","urgency_reason":null,"telegram_alert":false}'
    )

    async def fake_llm_chat(provider, prompt, *, conversation_id=None):
        return nat_response

    async def fake_run_mcp_tool(params, tool_name, arguments):
        return "삼성전자 (005930, KOSPI)"

    monkeypatch.setattr(services, "llm_chat", fake_llm_chat)
    monkeypatch.setattr(services, "run_mcp_tool", fake_run_mcp_tool)

    session = FakeSession()
    result = await services.perform_stock_analysis("삼성전자", "nat", session)

    assert session.report.decision == "BUY"
    assert session.report.confidence_score == pytest.approx(0.8)
    assert result["details"]["decision"] == "BUY"
    assert result["provider_supports_tools"] is True


def test_analysis_from_toolless_text_always_returns_none_details():
    """_analysis_from_toolless_text가 반환하는 dict의 details는 항상 None이어야
    한다. JSON에 details 키가 있어도 무시해야 한다 — 모델이 프롬프트를 무시하고
    details를 출력해도 파싱 단계에서 걸러내야 한다.
    """
    # 정상 JSON
    result = services._analysis_from_toolless_text(
        '{"summary":"요약","source_news":[],"trading_trend":null}'
    )
    assert result["details"] is None

    # JSON에 details가 있어도 무시
    result = services._analysis_from_toolless_text(
        '{"summary":"요약","details":{"decision":"BUY","confidence_score":0.9,'
        '"reason":"강세","target_stock":"삼성전자"},"source_news":[]}'
    )
    assert result["details"] is None

    # JSON 파싱 실패 (평문)
    result = services._analysis_from_toolless_text("plain text response")
    assert result["details"] is None
    assert result["summary"] == "plain text response"


# ── #211: urgency 정규화 — 재현 케이스 4종 ───────────────────────────────────


def test_analysis_from_toolless_text_urgency_valid_high_preserves_summary_and_news():
    """urgency가 유효값("high")이면 summary·source_news가 그대로 보존된다.

    urgency 정규화를 제거(원래대로 data.get("urgency","normal"))해도 이 케이스는
    통과하므로, 뮤테이션 가드로서 아래 null/medium 케이스와 함께 동작한다.

    부수 가드: 이 테스트가 urgency=="high"를 단언하므로,
    _URGENCY_LEVELS 파생이 깨지면(Literal 변경 등) 이 테스트가 red가 된다.
    즉 이 테스트를 지우면 _URGENCY_LEVELS 스키마-파생의 안전망도 함께 사라진다.
    """
    raw = '{"summary":"삼성전자 요약","source_news":["뉴스1"],"urgency":"high"}'
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "삼성전자 요약"
    assert result["source_news"] == ["뉴스1"]
    assert result["urgency"] == "high"


def test_analysis_from_toolless_text_urgency_null_preserves_summary_and_news():
    """urgency=null 이면 summary·source_news를 버리지 않고, urgency는 "normal"로 정규화한다.

    urgency 정규화를 제거하면 ValidationError → continue → 폴백으로 원본 JSON이
    summary에 노출되어 이 테스트가 red가 된다.
    """
    raw = '{"summary":"삼성전자 요약","source_news":["뉴스1"],"urgency":null}'
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "삼성전자 요약", "urgency=null이 원본 JSON을 summary에 노출해서는 안 된다"
    assert result["source_news"] == ["뉴스1"]
    assert result["urgency"] == "normal"


def test_analysis_from_toolless_text_urgency_unsupported_string_preserves_summary_and_news():
    """urgency가 미지원 문자열("medium")이면 "normal"로 정규화하고 summary·source_news를 보존한다.

    urgency 정규화를 제거하면 ValidationError → continue → 폴백으로 원본 JSON이
    summary에 노출되어 이 테스트가 red가 된다.
    """
    raw = '{"summary":"삼성전자 요약","source_news":["뉴스1"],"urgency":"medium"}'
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "삼성전자 요약", "urgency=medium이 원본 JSON을 summary에 노출해서는 안 된다"
    assert result["source_news"] == ["뉴스1"]
    assert result["urgency"] == "normal"


def test_analysis_from_toolless_text_urgency_key_absent_defaults_to_normal():
    """urgency 키가 없으면 기본값 "normal"이 적용되고 summary·source_news가 보존된다."""
    raw = '{"summary":"삼성전자 요약","source_news":["뉴스1"]}'
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "삼성전자 요약"
    assert result["source_news"] == ["뉴스1"]
    assert result["urgency"] == "normal"


def test_analysis_from_toolless_text_fallback_source_signals_is_empty_list():
    """JSON 파싱 실패 시 폴백의 source_signals는 None이 아니라 []여야 한다.

    이 테스트가 잡는 mutation: 폴백에서 `source_signals=[]` 제거.
    source_signals를 명시하지 않으면 AnalysisReport 기본값 None이 나가고,
    NAT 경로(source_signals=None → source_news 되채움 보정)와 비대칭이 생긴다.
    """
    result = services._analysis_from_toolless_text("JSON이 없는 순수 텍스트 응답")
    assert result["source_signals"] == [], (
        "폴백 경로의 source_signals는 None이 아니라 []여야 한다"
    )


# ── #211 리뷰 반영: 비문자열 urgency TypeError 회귀 방지 ────────────────────────


import pytest


@pytest.mark.parametrize(
    "urgency_value",
    [
        ["high"],  # list — frozenset 멤버십 검사에서 TypeError 유발 (핵심 버그)
        3,         # int
        True,      # bool
        False,     # bool
        None,      # null (urgency 키 값이 null인 경우, 키 부재와 구분)
    ],
)
def test_analysis_from_toolless_text_urgency_non_string_does_not_raise(urgency_value):
    """urgency가 list/int/bool/null이어도 TypeError 없이 normal로 정규화되고 summary가 보존된다.

    이 테스트가 잡는 mutation: `isinstance(raw_urgency, str)` 가드 제거.
    가드 없이 `raw_urgency in _URGENCY_LEVELS`만 쓰면 list가 frozenset
    멤버십 검사에서 TypeError: unhashable type: 'list'를 일으켜
    /api/v1/analyze가 500이 된다. origin/main에서는 같은 입력이 폴백으로
    열화될 뿐 살아남으므로 이 TypeError는 이 PR이 만든 회귀다.
    """
    import json

    raw = json.dumps(
        {"summary": "삼성전자 요약", "source_news": ["뉴스1"], "urgency": urgency_value}
    )
    result = services._analysis_from_toolless_text(raw)
    assert result["urgency"] == "normal", (
        f"urgency={urgency_value!r}는 비문자열이므로 'normal'로 정규화되어야 한다"
    )
    assert result["summary"] == "삼성전자 요약", (
        "비문자열 urgency가 summary를 원본 JSON으로 오염시켜서는 안 된다"
    )


def test_analysis_from_toolless_text_urgency_dict_does_not_raise():
    """urgency가 dict여도 TypeError 없이 정상 반환된다.

    #218 수정 후: summary 키를 가진 바깥 객체가 우선 선택된다.
    urgency는 {"level":"high"} dict를 받고, isinstance(..., str) 검사를
    통과하지 못해 "normal"로 정규화된다. summary·source_news는 보존된다.
    """
    import json

    raw = json.dumps(
        {"summary": "삼성전자 요약", "source_news": ["뉴스1"], "urgency": {"level": "high"}}
    )
    result = services._analysis_from_toolless_text(raw)
    assert isinstance(result, dict), "dict urgency여도 예외 없이 dict를 반환해야 한다"
    assert result["urgency"] == "normal"
    # #218: 중첩된 urgency dict가 후보로 선택되어 summary를 오염시키면 안 된다
    assert result["summary"] == "삼성전자 요약"
    assert result["source_news"] == ["뉴스1"]


# ── #211 리뷰 반영: urgency 하향 시 urgency_reason·telegram_alert 모순 방지 ──


def test_analysis_from_toolless_text_normalized_urgency_clears_reason_and_alert():
    """urgency가 정규화(하향)되면 urgency_reason·telegram_alert도 함께 버린다.

    urgency="medium"(미지원)→"normal" 정규화 시 urgency_reason·telegram_alert를
    그대로 두면 "Urgency: normal - 즉시 대응 필요"처럼 모순된 문구가 나간다.
    이 테스트가 잡는 mutation: `normalized` 조건 제거(reason/alert 무조건 통과).
    """
    raw = (
        '{"summary":"삼성전자 요약","source_news":["뉴스1"],'
        '"urgency":"medium","urgency_reason":"delisting imminent","telegram_alert":true}'
    )
    result = services._analysis_from_toolless_text(raw)
    assert result["urgency"] == "normal"
    assert result["urgency_reason"] is None, (
        "urgency 하향 시 urgency_reason도 None이어야 한다"
    )
    assert result["telegram_alert"] is False, (
        "urgency 하향 시 telegram_alert도 False여야 한다"
    )


def test_analysis_from_toolless_text_valid_urgency_preserves_reason_and_alert():
    """urgency가 유효값이면 urgency_reason·telegram_alert을 그대로 통과시킨다."""
    raw = (
        '{"summary":"삼성전자 요약","source_news":["뉴스1"],'
        '"urgency":"high","urgency_reason":"거래정지 위험","telegram_alert":true}'
    )
    result = services._analysis_from_toolless_text(raw)
    assert result["urgency"] == "high"
    assert result["urgency_reason"] == "거래정지 위험"
    assert result["telegram_alert"] is True


@pytest.mark.parametrize("raw_json", [
    # urgency 키 자체가 없는 경우
    '{"summary":"삼성전자 요약","source_news":["뉴스1"],"urgency_reason":"거래정지 위험","telegram_alert":true}',
    # urgency=null (키는 있지만 값이 null)인 경우
    '{"summary":"삼성전자 요약","source_news":["뉴스1"],"urgency":null,"urgency_reason":"거래정지 위험","telegram_alert":true}',
])
def test_analysis_from_toolless_text_absent_urgency_preserves_reason_and_alert(raw_json):
    """urgency 키가 없거나 null이면 하향이 아니므로 urgency_reason·telegram_alert을 보존한다.

    이 테스트가 잡는 regression: `normalized = urgency != raw_urgency`에서
    `raw_urgency is not None` 가드가 빠지면, urgency 키가 없을 때도 normalized=True가
    되어 urgency_reason·telegram_alert이 부당하게 버려진다.
    """
    result = services._analysis_from_toolless_text(raw_json)
    assert result["urgency"] == "normal"
    assert result["urgency_reason"] == "거래정지 위험", (
        "urgency 키 부재·null은 하향이 아니므로 urgency_reason을 보존해야 한다"
    )
    assert result["telegram_alert"] is True, (
        "urgency 키 부재·null은 하향이 아니므로 telegram_alert을 보존해야 한다"
    )


# ── #218/#222: 중첩 JSON 객체 — span 기반 필터 + summary 키 우선 선택 ──────────


def test_analysis_from_toolless_text_nested_json_uses_outer_summary():
    """중첩 JSON 객체가 있어도 바깥 객체에서 올바른 요약을 뽑는다. (#218, #222)

    #222 span 기반 수정 이후: _json_objects_from_text의 consumed_until 가드가
    안쪽 {"pe":12}를 후보 목록에서 제외하므로 바깥 객체만 시도된다.

    이 테스트가 잡는 mutation: _json_objects_from_text의 중첩 건너뛰기 제거
    (index < consumed_until 조건 삭제). 제거하면 안쪽 {"pe":12}가 다시 후보로
    올라와 summary가 원본 JSON 전체로, source_news가 []로 나와 두 단정 모두 실패한다.
    """
    raw = '{"summary":"삼성전자 요약","source_news":["뉴스1"],"detail":{"pe":12}}'
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "삼성전자 요약", (
        "중첩 JSON이 있을 때 원본 JSON 전체가 summary로 노출되어서는 안 된다"
    )
    assert result["source_news"] == ["뉴스1"]


def test_analysis_from_toolless_text_no_summary_json_still_falls_back():
    """관련 없는 JSON(summary 키 없음)만 있으면 원문 전체가 summary로 사용된다.

    {"count":3} 같은 객체는 summary 키가 없으므로 루프 안에서
    `data.get("summary") or text[:8000]` 폴백이 동작해 원본 텍스트를 summary로 채운다.
    ValidationError는 발생하지 않는다 — AnalysisReport 생성 인수가 모두 강제 변환되기 때문이다.
    """
    raw = '{"count":3,"items":["a","b","c"]}'
    result = services._analysis_from_toolless_text(raw)
    # 루프 내 폴백: data.get("summary") or text[:8000] → 원문 전체가 summary
    assert result["summary"] == raw
    assert result["source_news"] == []
    assert result["details"] is None


def test_analysis_from_toolless_text_invalid_candidate_is_skipped():
    """스키마 검증에 실패하는 후보는 건너뛰고 유효한 후보를 쓴다.

    summary 우선 정렬이 `except ValidationError: continue` 의도를 깨지 않는지 확인한다.
    trading_trend는 `str | None`이라 정수를 넣으면 ValidationError가 난다.

    이 테스트가 잡는 mutation: `except ValidationError: continue` 제거.
    건너뛰기가 없으면 정수 trading_trend로 ValidationError가 전파되어 실패한다.
    """
    raw = (
        '{"summary":"유효 요약","source_news":["뉴스1"],"trading_trend":"상승"} '
        '{"summary":"깨진 후보","trading_trend":12}'
    )
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "유효 요약"
    assert result["trading_trend"] == "상승"


# ── #222: span 기반 수정 — 세 호출부 각각 중첩 케이스 고정 ─────────────────────


def test_analysis_from_toolless_text_inner_summary_drift_prevented():
    """안쪽 객체에도 summary가 있으면 sort 방어선을 뚫는 드리프트 증상을 재현한다. (#222)

    PR #219의 sort는 summary 보유 여부만 보므로, 안쪽 객체도 summary를 가지면
    reversed 순서(안쪽 우선)를 그대로 살린다. span 수정으로 안쪽 객체 자체가
    후보에 오르지 않아 이 경로가 완전히 차단된다.

    이 테스트가 잡는 mutation: _json_objects_from_text의 중첩 건너뛰기 제거
    (index < consumed_until 조건 삭제). 제거하면 안쪽 {"summary":"INNER"}가
    candidates 선두에 오고 sort로도 걸러지지 않아 summary="INNER", source_news=[]가
    된다.
    """
    raw = '{"summary":"OUTER","source_news":["뉴스1"],"detail":{"summary":"INNER"}}'
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "OUTER", (
        "안쪽 summary가 바깥 summary를 덮어써서는 안 된다"
    )
    assert result["source_news"] == ["뉴스1"], (
        "안쪽 객체가 선택되면 source_news가 []로 유실된다"
    )


def test_analysis_from_nat_text_nested_inner_summary_no_drift():
    """analysis_from_nat_text 경로에서도 안쪽 summary가 바깥을 덮어쓰지 않는다. (#222)

    이 테스트가 잡는 mutation: _json_objects_from_text의 중첩 건너뛰기 제거.
    제거하면 details 없는 안쪽 {"summary":"INNER"} 객체가 먼저 선택돼
    details=None이 되거나 summary="INNER"가 된다.
    """
    raw = (
        '{"summary":"OUTER NAT 요약",'
        '"details":{"decision":"BUY","confidence_score":0.7,'
        '"reason":"수급 개선","target_stock":"삼성전자"},'
        '"source_news":["헤드라인"],'
        '"detail":{"summary":"INNER","pe":12}}'
    )
    result = services.analysis_from_nat_text(raw, "삼성전자")
    assert result["summary"] == "OUTER NAT 요약", (
        "안쪽 summary가 바깥 summary를 덮어써서는 안 된다"
    )
    assert result["details"]["decision"] == "BUY"
    assert result["source_news"] == ["헤드라인"]


def test_morning_briefing_from_text_nested_object_fields_filled():
    """_morning_briefing_from_text 경로에서 중첩 객체가 있어도 브리핑 필드가 채워진다. (#222)

    _json_objects_from_text의 top/nested 우선순위 방식으로 최상위 브리핑 객체가
    먼저 시도된다. 브리핑 키를 가진 최상위 객체가 선택되고 내부 {"pe":12}는 무시된다.

    이 테스트가 잡는 mutation: `_BRIEFING_KEYS` 검사 제거와 함께 중첩 객체가
    먼저 오도록 순서를 뒤집는 경우. 검사 없이 {"pe":12}가 먼저 선택되면
    모든 브리핑 필드가 빈 값으로 반환된다.
    """
    raw = (
        '{"market_summary":"오늘 시장 요약",'
        '"watchlist":["삼성전자"],'
        '"trading_ideas":["눌림목 관찰"],'
        '"catalysts":["CPI 발표"],'
        '"detail":{"pe":12}}'
    )
    result = services._morning_briefing_from_text(raw)
    assert result["market_summary"] == "오늘 시장 요약", (
        "중첩 객체가 있을 때 market_summary가 빈 문자열이 되어서는 안 된다"
    )
    assert result["watchlist"] == ["삼성전자"]
    assert result["trading_ideas"] == ["눌림목 관찰"]
    assert result["catalysts"] == ["CPI 발표"]


# ── #222: sort 방어선 — span 필터로도 못 막는 독립 최상위 JSON 순서 ────────────


def test_analysis_from_toolless_text_sort_guard_for_independent_top_level_jsons():
    """독립적인 최상위 JSON 2개에서 summary 없는 객체가 reversed 선두에 올 때 sort가 막는다.

    span 기반 필터는 중첩 객체만 제거한다 — 텍스트에서 공백으로 분리된 두 JSON은
    둘 다 최상위 객체이므로 둘 다 후보에 남는다. summary 없는 {"count":3}이
    텍스트 뒤에 위치하면 reversed 후 candidates[0]이 되고, sort 없이는
    data.get("summary") or text[:8000] 폴백이 원문 전체를 summary로 채운다.

    이 테스트가 잡는 mutation: `_candidates.sort(key=lambda d: "summary" not in d)` 제거.
    sort를 지우면 {"count":3}이 먼저 시도되어 summary가 원문 전체로,
    source_news가 []로 나와 두 단정 모두 실패한다.
    """
    # summary 있는 객체가 앞, summary 없는 객체가 뒤 → reversed 후 summary 없는 쪽이 선두
    raw = '{"summary":"유효 요약","source_news":["뉴스1"]} {"count":3}'
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "유효 요약", (
        "summary 없는 후보가 reversed 선두에 와도 sort가 summary 있는 후보를 먼저 시도해야 한다"
    )
    assert result["source_news"] == ["뉴스1"]


# ── #224 리뷰 반영: envelope(래퍼) 회귀 — 중첩 후순위 방식으로 폴백 ──────────────


def test_analysis_from_toolless_text_envelope_wrapper_fallback():
    """래퍼 JSON 안에 실제 페이로드가 있을 때 안쪽 객체로 폴백한다. (#224)

    _json_objects_from_text가 최상위를 top·중첩을 nested로 분리해
    [reversed(top) + reversed(nested)] 순으로 반환한다.
    sort가 summary 없는 래퍼를 뒤로 밀어 안쪽 후보가 먼저 시도된다.

    수정 전(제거 방식): 중첩 객체가 후보에서 사라져 래퍼만 남았고
    summary가 원본 JSON 전체 문자열, source_news=[]가 됐다.

    이 테스트가 잡는 mutation: top/nested 분리 제거(전부 top으로 처리) +
    sort 제거. 둘 다 지우면 래퍼가 선두에 남아 summary가 원문 전체가 된다.
    """
    raw = '{"response":{"summary":"삼성전자 요약","source_news":["뉴스1"]}}'
    result = services._analysis_from_toolless_text(raw)
    assert result["summary"] == "삼성전자 요약", (
        "래퍼 JSON 안의 summary가 올바르게 추출되어야 한다"
    )
    assert result["source_news"] == ["뉴스1"]


def test_analysis_from_nat_text_envelope_wrapper_fallback():
    """analysis_from_nat_text 경로에서 래퍼 JSON 안의 페이로드로 폴백한다. (#224)

    analysis_from_nat_text는 except ValueError로 재시도하므로 래퍼 객체(summary·
    source_news 미포함)가 ValidationError를 내고 안쪽 후보로 넘어간다.

    이 테스트가 잡는 mutation: top/nested 분리 제거. 제거하면 안쪽 객체가
    후보에 오르지 않아 except ValueError 폴백이 래퍼만 시도하고 결국
    원문 전체를 summary로 쓰는 텍스트 폴백 경로로 떨어진다.
    """
    raw = (
        '{"response":{"summary":"NAT 요약",'
        '"details":{"decision":"BUY","confidence_score":0.8,'
        '"reason":"수급 개선","target_stock":"삼성전자"},'
        '"source_news":["헤드라인"]}}'
    )
    result = services.analysis_from_nat_text(raw, "삼성전자")
    assert result["summary"] == "NAT 요약", (
        "래퍼 JSON 안의 summary가 올바르게 추출되어야 한다"
    )
    assert result["details"]["decision"] == "BUY"
    assert result["source_news"] == ["헤드라인"]


def test_morning_briefing_from_text_envelope_wrapper_fallback():
    """_morning_briefing_from_text 경로에서 래퍼 JSON 안의 브리핑으로 폴백한다. (#224)

    _BRIEFING_KEYS 검사가 래퍼 객체(브리핑 키 미포함)를 건너뛰고
    nested의 안쪽 객체로 폴백한다.

    이 테스트가 잡는 mutation: `if not any(key in data for key in _BRIEFING_KEYS): continue`
    제거. 검사가 없으면 래퍼 객체가 선택돼 모든 브리핑 필드가 빈 값이 된다.
    """
    raw = (
        '{"briefing":{"market_summary":"오늘 시장 요약",'
        '"watchlist":["삼성전자"],'
        '"trading_ideas":["눌림목 관찰"],'
        '"catalysts":["CPI 발표"]}}'
    )
    result = services._morning_briefing_from_text(raw)
    assert result["market_summary"] == "오늘 시장 요약", (
        "래퍼 JSON 안의 market_summary가 올바르게 추출되어야 한다"
    )
    assert result["watchlist"] == ["삼성전자"]
    assert result["trading_ideas"] == ["눌림목 관찰"]
    assert result["catalysts"] == ["CPI 발표"]


def test_morning_briefing_from_text_skips_non_briefing_candidates():
    """브리핑 키를 하나도 갖지 않은 후보는 건너뛰고 유효한 후보를 반환한다. (#224)

    독립 최상위 JSON이 2개인 상황에서 무관한 JSON({"count":3})이 reversed
    선두에 와도 _BRIEFING_KEYS 검사가 건너뛰어 올바른 브리핑 후보를 선택한다.

    이 테스트가 잡는 mutation: `if not any(key in data for key in _BRIEFING_KEYS): continue`
    제거. 검사가 없으면 {"count":3}이 선택돼 모든 브리핑 필드가 빈 값이 된다.
    """
    # 브리핑 키 없는 객체가 앞, 브리핑 있는 객체가 뒤 → reversed 후 무관한 쪽이 선두
    raw = (
        '{"market_summary":"시장 요약","watchlist":["삼성전자"],'
        '"trading_ideas":[],"catalysts":[]} {"count":3}'
    )
    result = services._morning_briefing_from_text(raw)
    assert result["market_summary"] == "시장 요약", (
        "브리핑 키 없는 후보가 reversed 선두에 와도 건너뛰고 올바른 후보를 써야 한다"
    )
    assert result["watchlist"] == ["삼성전자"]


# ──────────────────────────────────────────────────────────────────────────
# PII 마스킹 계층 통합 테스트 (#230, F-17/NFR-05)
#
# backend/pii_mask.py 자체의 recognizer/왕복 무손실 단위 테스트는
# backend/tests/test_pii_mask.py에 있다. 여기서는 llm_chat()이 그 모듈을
# 실제로 호출하는지 - 요청·응답 경계에서의 배선 - 만 확인한다.
# ──────────────────────────────────────────────────────────────────────────

# 자리표시자는 <KIND_{scope}_{n}> 형태이고 scope는 mask_pii 호출마다 새로 뽑는 6자리
# hex nonce다(backend/pii_mask.py의 _Counter docstring 참고). 정확한 문자열을 단언할 수
# 없으므로 nonce만 지워 <AMOUNT_1> 형태로 정규화한 뒤 단언한다.
_SCOPED_PLACEHOLDER_RE = re.compile(r"<(ACCOUNT|AMOUNT|QTY)_[0-9a-f]{6}_(\d+)>")


def _norm(text: str) -> str:
    """자리표시자의 호출별 nonce를 지워 <AMOUNT_1> 형태로 정규화한다(단언 가독성 유지용)."""
    return _SCOPED_PLACEHOLDER_RE.sub(r"<\1_\2>", text)


@pytest.mark.asyncio
async def test_llm_chat_masks_pii_before_dispatching_to_provider(monkeypatch):
    """llm_chat이 provider 구현을 부르기 전에 mask_pii를 거쳐야 한다.

    이 테스트가 잡는 mutation: llm_chat에서 mask_pii 호출을 제거. 제거하면
    provider가 받는 문자열에 원본 계좌번호·금액·수량이 그대로 남는다.
    """
    captured: list[str] = []

    async def fake_openai(user_msg):
        captured.append(user_msg)
        return "ok"

    monkeypatch.setattr(services, "_llm_openai_chat", fake_openai)

    pii_text = "12345678-01 계좌, 삼성전자 3주, 평가금액 12,345,000원"
    await services.llm_chat("openai", pii_text)

    assert len(captured) == 1
    sent = captured[0]
    assert "12345678-01" not in sent, "계좌번호가 마스킹되지 않고 그대로 전송됐다"
    assert "12,345,000원" not in sent, "금액이 마스킹되지 않고 그대로 전송됐다"
    normalized = _norm(sent)
    assert "<ACCOUNT_1>" in normalized
    assert "<AMOUNT_1>" in normalized
    assert "<QTY_1>" in normalized


@pytest.mark.asyncio
async def test_llm_chat_unmasks_response_placeholders(monkeypatch):
    """llm_chat이 provider 응답의 자리표시자를 원값으로 역치환해 반환해야 한다.

    이 테스트가 잡는 mutation: llm_chat에서 unmask_pii 호출을 제거. 제거하면
    호출자가 <AMOUNT_1> 같은 자리표시자를 그대로 받아 분석 결과에 노출된다.
    """

    async def fake_openai(user_msg):
        # provider가 자리표시자를 그대로 되읊는 상황을 흉내낸다(상대 비교 답변 등).
        # 자리표시자에는 호출별 nonce가 붙으므로 하드코딩하지 않고 받은 프롬프트에서
        # 실제로 나간 자리표시자를 뽑아 되읊는다.
        first, second = (m.group(0) for m in _SCOPED_PLACEHOLDER_RE.finditer(user_msg))
        return f"요약: {first}은 {second}보다 작습니다."

    monkeypatch.setattr(services, "_llm_openai_chat", fake_openai)

    pii_text = "평가금액 12,345,000원, 총자산 45,678,000원"
    result = await services.llm_chat("openai", pii_text)

    assert result == "요약: 12,345,000원은 45,678,000원보다 작습니다."
    assert "<AMOUNT" not in result


@pytest.mark.asyncio
async def test_llm_chat_unmask_fails_open_on_unknown_placeholder(monkeypatch):
    """provider가 존재하지 않는 자리표시자를 지어내도 llm_chat이 예외 없이
    나머지 원문을 최대한 살려 반환해야 한다.

    이 테스트가 잡는 mutation: unmask_pii(또는 그 호출부)가 매핑에 없는 자리표시자를
    만났을 때 예외를 던지도록 바뀌는 경우 - 그러면 이 테스트가 예외로 실패해야 한다.

    두 갈래를 함께 넣는다:
    - 형식은 유효하지만 매핑에 없는 자리표시자(<AMOUNT_deadbe_9> - scope가 이번 호출과 다름):
      내부 토큰이 사용자에게 노출되지 않도록 _FALLBACK_LABEL 중립 문구로 치환된다.
    - scope 없는 구형식(<AMOUNT_9>): _PLACEHOLDER_RE에 매치되지 않아 원문 그대로 남는다.
    """

    async def fake_openai(user_msg):
        # 둘 다 이번 호출의 매핑에 없는 자리표시자다.
        return "총자산은 <AMOUNT_deadbe_9>이고 예수금은 <AMOUNT_9>입니다."

    monkeypatch.setattr(services, "_llm_openai_chat", fake_openai)

    result = await services.llm_chat("openai", "평가금액 12,345,000원")

    # <AMOUNT_deadbe_9>: 형식 유효, 매핑에 없음 -> 중립 문구로 치환
    assert "이전에 언급된 금액" in result
    assert "<AMOUNT_deadbe_9>" not in result, "내부 토큰이 사용자 화면에 노출됐다"
    # <AMOUNT_9>: _PLACEHOLDER_RE에 매치 안 됨 -> 원문 그대로
    assert "<AMOUNT_9>" in result


@pytest.mark.asyncio
async def test_llm_chat_masking_does_not_leak_between_concurrent_calls():
    """llm_chat을 asyncio.gather로 동시에 호출해도 서로 다른 PII 매핑이 섞이지 않아야 한다.

    mask_pii/unmask_pii의 mapping이 모듈 전역이 아니라 llm_chat 호출마다 지역
    변수로 생성된다는 것을 확인한다 - 스케줄러가 병렬로 llm_chat을 부르는 구조이므로
    전역에 두면 동시 요청의 매핑이 서로 덮어써진다.
    """

    # 서로 다른 provider 함수를 붙여 두 호출을 구분한다(둘 다 openai를 쓰면 monkeypatch가
    # 서로를 덮어써 어느 호출이 무엇을 받았는지 구분할 수 없다).
    calls: dict[str, str] = {}

    def make_capturing(name, delay):
        async def _inner(user_msg):
            await asyncio.sleep(delay)  # 다른 호출과 인터리빙되도록 강제로 양보한다
            calls[name] = user_msg
            return user_msg  # 받은 그대로 되돌려 마스킹된 텍스트를 관측한다

        return _inner

    from backend import services as services_module

    orig_openai = services_module._llm_openai_chat
    orig_anthropic = services_module._llm_anthropic_chat
    try:
        services_module._llm_openai_chat = make_capturing("a", 0.02)
        services_module._llm_anthropic_chat = make_capturing("b", 0)

        text_a = "평가금액 11,111,111원"
        text_b = "평가금액 22,222,222원"

        result_a, result_b = await asyncio.gather(
            services_module.llm_chat("openai", text_a),
            services_module.llm_chat("anthropic", text_b),
        )

        # 각 호출이 받은 마스킹된 텍스트에 서로의 원본 금액이 섞이지 않아야 한다.
        assert "11,111,111원" not in calls["b"]
        assert "22,222,222원" not in calls["a"]
        # 각 호출의 최종 결과(역치환 후)는 자기 자신의 원본 금액으로 복원되어야 한다.
        assert result_a == text_a
        assert result_b == text_b
    finally:
        services_module._llm_openai_chat = orig_openai
        services_module._llm_anthropic_chat = orig_anthropic


@pytest.mark.asyncio
async def test_placeholders_do_not_collide_across_calls_in_same_conversation(monkeypatch):
    """같은 conversation_id로 연속 호출할 때 이전 턴 자리표시자가 현재 턴 값으로 복원되면 안 된다.

    NAT 경로는 마스킹 매핑과 달리 대화 히스토리를 **호출을 넘어 서버 쪽에 유지**한다:
    backend/services.py:648-653이 messages에 현재 메시지 1건만 보내고 conversation-id
    헤더로 세션을 식별하면, finus_nat/src/nat_finus_nat/agents.py:643-665의
    _load_history()가 SQLite chat_messages에서 과거 턴을 읽어 오고 같은 파일
    696-732가 `history + chat_request.messages`로 합쳐 라우터에 넘긴다
    (finus_nat/configs/router.yml:58-61, max_history_messages: 30). conversation_id는
    호출 간 재사용된다(backend/telegram_commands.py:1355의 f"telegram:{chat_id}"는
    그 사용자의 모든 메시지가 같은 스레드).

    따라서 NAT은 이전 턴의 자리표시자를 응답에 인용할 수 있다. 자리표시자 이름이
    호출마다 <AMOUNT_1>부터 다시 시작하면 그 인용이 **현재 턴의 다른 값으로 조용히**
    복원된다 - unmask_pii의 fail-open은 "매핑에 없는" 자리표시자만 보호하는데 이
    케이스는 매핑에 "있으면서 값이 틀린" 경우라 방어선이 통하지 않는다.

    이 테스트가 잡는 mutation: _Counter에서 scope(호출별 nonce)를 제거해 자리표시자를
    다시 `<{kind}_{n}>` 형태로 되돌리는 변경. 그러면 아래 turn-1 자리표시자가 turn-2
    매핑에 존재하게 되어 "5,000,000원"으로 복원되고 이 단언이 실패한다.
    """
    seen: list[str] = []

    async def echo_prev(user_msg, *, conversation_id=None):
        # 턴 1에서 NAT이 본 자리표시자를 턴 2 응답에서 그대로 인용하는 상황.
        seen.append(user_msg)
        # 자리표시자 형식과 무관하게(nonce 유무 모두) 턴 1의 자리표시자를 뽑는다 —
        # nonce를 제거하는 mutation에서도 이 fake가 깨지지 않고 아래 단언이 red가 되도록.
        turn1_placeholder = re.search(r"<AMOUNT[^>]*>", seen[0]).group(0)
        return f"앞서 말씀하신 {turn1_placeholder} 기준으로는"

    monkeypatch.setattr(services, "_llm_nat_chat", echo_prev)

    await services.llm_chat("nat", "잔고 12,345,000원", conversation_id="telegram:1")
    result = await services.llm_chat("nat", "5,000,000원 더", conversation_id="telegram:1")

    assert "5,000,000원" not in result, "이전 턴 자리표시자가 현재 턴 값으로 잘못 복원됐다"
    assert "12,345,000원" not in result, "이전 턴 매핑이 호출을 넘어 살아 있으면 안 된다"


def test_no_bypass_of_llm_chat_masking_layer():
    """provider별 구현(_llm_openai_chat 등)은 llm_chat() 밖에서 직접 호출되면 안 된다.

    _llm_openai_chat/_llm_anthropic_chat/_llm_ollama_chat/_llm_nat_chat은 마스킹되지
    않은 원문을 그대로 외부로 보낸다 - llm_chat()만이 mask_pii를 거친 뒤 이 함수들을
    호출해야 한다. AST로 backend/services.py 전체를 스캔해, 이 함수들이 llm_chat 함수
    본문 밖에서 쓰이면(=마스킹 계층을 우회하는 새 경로) 이 테스트가 실패한다.

    호출(ast.Call)뿐 아니라 **이름 참조(ast.Name)** 를 검사하므로 다음 우회 경로까지
    잡는다:
    - 별칭 대입: `_direct = _llm_openai_chat` 후 `await _direct(msg)`
      (ast.Call의 함수명이 `_direct`라 호출만 보면 탐지되지 않는다)
    - 콜백 전달: `dispatch(_llm_nat_chat)`처럼 호출하지 않고 넘기는 경우
    - 모듈 최상위(함수 밖) 사용: func_stack이 비면 "<module>"로 기록해 llm_chat이
      아닌 스코프로 잡힌다

    ast.Attribute 분기(`services._llm_openai_chat()`처럼 속성 접근으로 부르는 형태)는
    visit_Name으로 잡히지 않으므로 visit_Call에 남겨 둔다.

    스캔 범위: backend/*.py (tests/ 제외). _llm_*_chat은 services.py 전용 구현이므로
    다른 backend 파일에서 참조되는 순간 곧바로 마스킹 우회가 된다.
    """
    backend_dir = pathlib.Path(inspect.getfile(services)).parent
    provider_fns = {
        "_llm_openai_chat",
        "_llm_anthropic_chat",
        "_llm_ollama_chat",
        "_llm_nat_chat",
    }
    callers: dict[str, set[str]] = {fn: set() for fn in provider_fns}

    class _CallerVisitor(ast.NodeVisitor):
        def __init__(self):
            self.func_stack: list[str] = []

        def _enter(self, node):
            self.func_stack.append(node.name)
            self.generic_visit(node)
            self.func_stack.pop()

        def visit_FunctionDef(self, node):
            self._enter(node)

        def visit_AsyncFunctionDef(self, node):
            self._enter(node)

        def _record(self, name):
            if name in provider_fns:
                callers[name].add(self.func_stack[-1] if self.func_stack else "<module>")

        def visit_Name(self, node):
            # 호출이 아닌 참조(별칭 대입·콜백 전달)도 우회 경로가 될 수 있다.
            # 직접 호출 `_llm_openai_chat(...)`도 func가 ast.Name이라 여기서 잡힌다.
            self._record(node.id)
            self.generic_visit(node)

        def visit_Call(self, node):
            # `services._llm_openai_chat()`처럼 속성 접근으로 부르는 형태는 ast.Name이
            # 아니므로 visit_Name이 잡지 못한다 - 이 분기를 남겨 둔다.
            if isinstance(node.func, ast.Attribute):
                self._record(node.func.attr)
            self.generic_visit(node)

    visitor = _CallerVisitor()
    for py_file in sorted(backend_dir.glob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        visitor.visit(tree)

    for fn_name, caller_set in callers.items():
        assert caller_set == {"llm_chat"}, (
            f"{fn_name}이 llm_chat() 밖({caller_set})에서도 참조/호출됩니다. "
            "마스킹 계층(mask_pii/unmask_pii)을 우회하는 새 호출 경로가 생긴 것으로 보입니다 — "
            "PII가 마스킹되지 않은 채 외부 LLM provider로 나갈 수 있습니다."
        )
