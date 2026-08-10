import inspect
import logging
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


def test_looks_like_stock_code_accepts_alphanumeric_and_rejects_plain_names():
    assert services._looks_like_stock_code("005930")
    assert services._looks_like_stock_code("0001A0")
    # 숫자가 없는 6~7자 영문은 종목명일 가능성이 높으므로 코드로 보지 않는다.
    assert not services._looks_like_stock_code("SAMSUNG")
    assert not services._looks_like_stock_code("삼성전자")


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

    dict urgency는 _json_objects_from_text의 reversed 순서 때문에
    내부 객체가 먼저 후보로 선택되어 summary가 원본 JSON이 되는 선행 문제
    (Improvement #3)가 있다. 이는 이 PR 이전부터 존재하는 문제이므로
    이 테스트는 summary 내용이 아닌 '예외 없이 반환된다'만 고정한다.
    urgency 결과는 내부 객체에 urgency 키가 없어 'normal'이 된다.
    """
    import json

    raw = json.dumps(
        {"summary": "삼성전자 요약", "source_news": ["뉴스1"], "urgency": {"level": "high"}}
    )
    result = services._analysis_from_toolless_text(raw)
    assert isinstance(result, dict), "dict urgency여도 예외 없이 dict를 반환해야 한다"
    assert result["urgency"] == "normal"


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
