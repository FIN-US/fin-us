import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx

from nat_finus_nat import finus_api

# path helpers live on finus_api (no separate finus_paths module)
finus_paths = finus_api


def _write_mcp_script(root, name):
    server_dir = root / name
    server_dir.mkdir(parents=True)
    (server_dir / "index.js").write_text("", encoding="utf-8")


def test_vendor_root_detection_keeps_existing_news_trading_layout(monkeypatch, tmp_path):
    finus_nat_root = tmp_path / "finus_nat"
    (finus_nat_root / "src").mkdir(parents=True)
    _write_mcp_script(tmp_path, "mcp-news")
    _write_mcp_script(tmp_path, "mcp-trading")

    monkeypatch.delenv("FINUS_VENDOR_ROOT", raising=False)
    monkeypatch.setattr(finus_paths, "finus_nat_example_root", lambda: finus_nat_root)

    assert finus_paths.fin_us_vendor_root() == tmp_path


def test_vendor_root_detection_keeps_nested_news_trading_layout(monkeypatch, tmp_path):
    finus_nat_root = tmp_path / "finus_nat"
    finus_home = tmp_path / "fin-us"
    (finus_nat_root / "src").mkdir(parents=True)
    _write_mcp_script(finus_home, "mcp-news")
    _write_mcp_script(finus_home, "mcp-trading")

    monkeypatch.delenv("FINUS_VENDOR_ROOT", raising=False)
    monkeypatch.setattr(finus_paths, "finus_nat_example_root", lambda: finus_nat_root)

    assert finus_paths.fin_us_vendor_root() == finus_home


def test_finus_backend_base_url(monkeypatch):
    monkeypatch.delenv("FINUS_BACKEND_URL", raising=False)
    assert finus_api._finus_backend_base_url("") == "http://127.0.0.1:8000"
    monkeypatch.setenv("FINUS_BACKEND_URL", "http://backend:8000/")
    assert finus_api._finus_backend_base_url("") == "http://backend:8000"
    assert finus_api._finus_backend_base_url("http://custom:9000") == "http://custom:9000"


def test_react_tool_input_accepts_empty_action_input_string():
    inp = finus_api.FinusMcpTradingTodayOrdersInput.model_validate("")
    assert inp.trade_date == ""
    assert inp.ccld_dvsn == "00"


def test_react_tool_input_accepts_json_action_input_string():
    inp = finus_api.FinusMcpTradingTodayOrdersInput.model_validate(
        '{"trade_date":"20260529","stock_name":"삼성전자"}'
    )
    assert inp.trade_date == "20260529"
    assert inp.stock_name == "삼성전자"


def test_finus_react_input_converter_maps_str_to_model():
    convert = finus_api._finus_react_input_converter(finus_api.FinusMcpTradingTodayOrdersInput)
    inp = convert('{"ccld_dvsn":"01"}')
    assert isinstance(inp, finus_api.FinusMcpTradingTodayOrdersInput)
    assert inp.ccld_dvsn == "01"


def test_save_diary_input_converter_accepts_multiline_content():
    convert = finus_api._finus_react_input_converter(finus_api.FinusSaveDiaryInput)
    inp = convert('{"title":"T","content":"line1\\nline2"}')
    assert inp.title == "T"
    assert inp.content == "line1\nline2"


def test_save_trading_diary_posts_to_backend(monkeypatch, mock_backend):
    backend = mock_backend({"status": "success", "data": {"id": 1, "title": "T", "content": "C"}})
    # delenv가 아니라 빈 값이다 — dotenv 로딩이 지운 키를 되살릴 수 있고, 빈 값은
    # _finus_backend_headers가 "키 없음"으로 보는 값이라 판정 결과가 같다(PR #352 리뷰).
    monkeypatch.setenv("FINUS_API_KEY", "")

    config = finus_api.FinusSaveDiaryConfig(backend_url="http://test-backend:8000")

    # ``@register_function`` 은 빌더를 ``asynccontextmanager`` 로 감싸고,
    # 노출되는 호출 핸들은 ``FunctionInfo.single_fn`` 이며 입력은 pydantic 스키마 인스턴스다.
    async def run_tool():
        async with finus_api.finus_save_diary(config, None) as info:
            return await info.single_fn(
                finus_api.FinusSaveDiaryInput(title="매매일지 2026-05-24", content="본문")
            )

    result = asyncio.run(run_tool())

    assert backend.method == "POST"
    assert backend.url == "http://test-backend:8000/api/v1/db/diary"
    assert backend.json_body == {"title": "매매일지 2026-05-24", "content": "본문"}
    # 키가 없는 배포에서는 헤더 자체를 붙이지 않는다. 빈 값을 보내면 backend가 그것을
    # "틀린 키"로 보므로(main.matches_api_key), 인증이 꺼진 배포에서까지 401이 된다.
    assert backend.header("X-API-Key") is None
    # 타임아웃은 요청 객체에 남지 않지만 계약이다. 빠지면 httpx 기본값 5초가 적용돼
    # 느린 backend에서 저장이 조용히 실패한다 (PR #359 리뷰).
    assert backend.client_kwargs.get("timeout") == config.timeout_sec
    assert '"id": 1' in result


def test_save_trading_diary_sends_the_api_key_header(monkeypatch, mock_backend):
    """backend가 인증을 켠 배포에서는 `X-API-Key`를 실어 보냅니다 (#266 2단계).

    NAT는 컴포즈 내부에서 backend를 부르는 비브라우저 클라이언트다. 이 헤더가 빠지면
    매매일지 저장이 401로 떨어지는데, 그 실패는 에이전트 Observation 안에서만 보여서
    운영 중에 알아채기 어렵다.

    헤더 이름은 backend/main.py의 API_KEY_HEADER와 같아야 한다 — 어긋나면 인증을 켠
    날에야 드러난다. 이 테스트가 잡는 mutation: headers 인자를 다시 빼거나 이름을
    바꾸는 회귀.
    """
    backend = mock_backend({"status": "success", "data": {"id": 7}})
    monkeypatch.setenv("FINUS_API_KEY", "s3cr3t-key")

    config = finus_api.FinusSaveDiaryConfig(backend_url="http://test-backend:8000")

    async def run_tool():
        async with finus_api.finus_save_diary(config, None) as info:
            return await info.single_fn(
                finus_api.FinusSaveDiaryInput(title="제목", content="본문")
            )

    asyncio.run(run_tool())

    assert backend.header("X-API-Key") == "s3cr3t-key"


def test_list_trading_diaries_sends_the_api_key_header(monkeypatch, mock_backend):
    """조회 쪽도 같은 헤더를 붙입니다 — 한쪽만 붙이면 그쪽만 401이 된다."""
    backend = mock_backend({"status": "success", "data": []})
    monkeypatch.setenv("FINUS_API_KEY", "s3cr3t-key")

    config = finus_api.FinusListDiariesConfig(backend_url="http://test-backend:8000")

    async def run_tool():
        async with finus_api.finus_list_diaries(config, None) as info:
            return await info.single_fn(finus_api.FinusListDiariesInput())

    asyncio.run(run_tool())

    assert backend.method == "GET"
    assert backend.header("X-API-Key") == "s3cr3t-key"
    # 저장 쪽과 마찬가지로 조회 쪽 타임아웃도 여기서 고정한다 — 프로덕션은 두 곳에서
    # 따로 넘기므로 한쪽만 잃는 회귀가 가능하다.
    assert backend.client_kwargs.get("timeout") == config.timeout_sec


def test_mcp_call_tool_passes_environment_to_child_process(monkeypatch, tmp_path):
    server_dir = tmp_path / "mcp-news"
    server_dir.mkdir()
    script = server_dir / "index.js"
    script.write_text("", encoding="utf-8")
    (server_dir / "node_modules" / "@modelcontextprotocol" / "sdk").mkdir(parents=True)

    captured = {}

    class FakeStdioServerParameters:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(finus_api, "StdioServerParameters", FakeStdioServerParameters)
    monkeypatch.setattr(
        finus_api.os,
        "environ",
        {
            "NAVER_CLIENT_ID": "id",
            "KIS_API_KEY": "kis-key",
            "DART_API_KEY": "dart-key",
            "FIN_US_TRACE_ID": "trace-id",
            "DATABASE_URL": "postgresql://user:secret@db/prod",
            "UNRELATED_SECRET": "secret",
        },
    )

    def fake_inner(*args, **kwargs):
        class _Context:
            async def __aenter__(self):
                return (None, None)

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Context()

    class FakeClientSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, tool_name, arguments):
            block = SimpleNamespace(text="ok")
            return SimpleNamespace(content=[block])

    monkeypatch.setattr(finus_api, "stdio_client", fake_inner)
    monkeypatch.setattr(finus_api, "ClientSession", FakeClientSession)

    result = asyncio.run(
        finus_api._mcp_call_tool(
            vendor_root=tmp_path,
            subdir="mcp-news",
            tool_name="get_market_news",
            arguments={"stock_name": "삼성전자"},
            timeout_sec=5,
        )
    )

    assert result == "ok"
    assert captured["env"] == {
        "NAVER_CLIENT_ID": "id",
        "KIS_API_KEY": "kis-key",
        "DART_API_KEY": "dart-key",
        "FIN_US_TRACE_ID": "trace-id",
    }
    assert json.dumps(captured["args"])


def test_mcp_dart_stock_routes_to_disclosure_signal_tool(monkeypatch, tmp_path):
    captured = {}

    async def fake_mcp_call_tool(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(finus_api, "_mcp_call_tool", fake_mcp_call_tool)

    result = asyncio.run(
        finus_api._mcp_dart_stock(
            vendor_root=str(tmp_path),
            timeout_sec=7,
            stock_name="삼성전자",
        )
    )

    assert result == "ok"
    assert captured == {
        "vendor_root": Path(str(tmp_path)).expanduser().resolve(),
        "subdir": "mcp-dart",
        "tool_name": "get_disclosure_signal",
        "arguments": {"stock_name": "삼성전자"},
        "timeout_sec": 7,
    }


def test_mcp_dart_earnings_stock_routes_to_earnings_report_tool(monkeypatch, tmp_path):
    captured = {}

    async def fake_mcp_call_tool(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(finus_api, "_mcp_call_tool", fake_mcp_call_tool)

    result = asyncio.run(
        finus_api._mcp_dart_earnings_stock(
            vendor_root=str(tmp_path),
            timeout_sec=7,
            stock_name="삼성전자",
            period="2025Q1",
        )
    )

    assert result == "ok"
    assert captured == {
        "vendor_root": Path(str(tmp_path)).expanduser().resolve(),
        "subdir": "mcp-dart",
        "tool_name": "get_earnings_report",
        "arguments": {"stock_name": "삼성전자", "period": "2025Q1"},
        "timeout_sec": 7,
    }


# ---------------------------------------------------------------------------
# #358 — 코딩 실수가 API 실패로 위장되지 않는다
# ---------------------------------------------------------------------------
#
# ``finus_api``의 넓은 ``except Exception``은 ``TypeError``·``AttributeError`` 같은
# 코딩 실수까지 잡아 ``mcp_call_failed``·``diary_api_request_failed``로 바꿨다. PR #356의
# 원인이 정확히 이 경로였다 — 테스트 대역의 시그니처 불일치가 던진 ``TypeError``가
# "저장이 안 됐다"는 오류 JSON으로 위장돼 진단이 비쌌다. 운영에서는 같은 위장이 스택
# 없이 남아 원인 추적을 막는다.
#
# 방침 B(잡되 ``logger.exception``으로 스택 보존) + 오류 코드 구분을 세 지점
# (MCP 호출·일지 저장·일지 조회)에 같이 적용했고, 아래 테스트가 그 셋을 함께 고정한다.
# 한 곳만 고치면 나머지 두 곳에 같은 위장이 남으므로 지점마다 테스트를 둔다.
#
# 잡는 것 자체는 유지한다 — 도구 경계에서 예외가 탈출하면 ReAct 루프가 끊기고, 그것이
# 오해를 부르는 Observation보다 운영상 더 큰 실패다.

_BUG_MESSAGE = "post() got an unexpected keyword argument 'headers'"


def _install_client(monkeypatch, handler):
    """``httpx.AsyncClient``가 *handler*를 쓰는 ``MockTransport``를 타게 한다.

    ``conftest``의 ``mock_backend``는 정상 응답을 돌려주는 대역이라 예외를 던질 수 없다.
    여기서 던지는 예외는 프로덕션의 ``await client.post(...)`` 안에서 난 것과 같은
    자리에서 올라온다 — 예외를 손으로 조립해 ``except`` 블록에 밀어 넣는 것보다 실제
    경로에 가깝다.
    """
    real_client = finus_api.httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(finus_api.httpx, "AsyncClient", _client)


def _raise_coding_bug(_request):
    raise TypeError(_BUG_MESSAGE)


def _assert_disguise_is_gone(observation: str, caplog, *, api_error: str) -> dict:
    """코딩 실수가 (1) API 실패 코드로 위장되지 않고 (2) 스택으로 드러나는지 본다."""
    payload = json.loads(observation)
    assert payload["error"] == finus_api._TOOL_INTERNAL_ERROR, (
        f"코딩 실수가 {payload['error']!r}로 위장됐다"
    )
    assert payload["error"] != api_error
    assert payload["exception"] == "TypeError"
    # 에이전트가 같은 호출을 반복하지 않도록 무엇이 일어났는지 알려 준다.
    assert "hint" in payload
    # 방침 B의 본체 — 로그에 스택이 남는다. 스택이 없으면 운영에서 원인 추적이 막힌다.
    with_stack = [r for r in caplog.records if r.exc_info is not None]
    assert with_stack, "logger.exception이 호출되지 않아 스택이 남지 않았다"
    assert any(r.exc_info[0] is TypeError for r in with_stack)
    return payload


def test_mcp_call_type_error_is_not_disguised_as_api_failure(caplog):
    """MCP 호출 경로(#358의 첫 지점) — 코딩 실수가 ``mcp_call_failed``가 되지 않는다."""

    async def inner():
        raise TypeError(_BUG_MESSAGE)

    with caplog.at_level(logging.ERROR, logger=finus_api.logger.name):
        observation = asyncio.run(
            finus_api._run_mcp_timed(inner, tool_name="domestic_stock", timeout_sec=5)
        )

    payload = _assert_disguise_is_gone(observation, caplog, api_error="mcp_call_failed")
    assert payload["tool"] == "domestic_stock"


def test_mcp_call_type_error_inside_an_exception_group_is_not_disguised(caplog):
    """anyio task group을 쓰는 MCP 클라이언트는 코딩 실수도 그룹에 싸서 올린다.

    그룹을 풀지 않으면 위 분류가 **이 경로에서만** 통하지 않는다 — 실 운영의 MCP
    호출은 대부분 이쪽으로 온다.
    """

    async def inner():
        raise BaseExceptionGroup("mcp", [TypeError(_BUG_MESSAGE)])

    with caplog.at_level(logging.ERROR, logger=finus_api.logger.name):
        observation = asyncio.run(
            finus_api._run_mcp_timed(inner, tool_name="domestic_stock", timeout_sec=5)
        )

    assert json.loads(observation)["error"] == finus_api._TOOL_INTERNAL_ERROR


def test_mcp_call_real_api_failure_keeps_its_error_code(caplog):
    """반대 방향 — 진짜 외부 실패가 내부 오류로 위장되면 그것도 같은 종류의 결함이다."""

    async def inner():
        raise httpx.ConnectError("connection refused")

    with caplog.at_level(logging.ERROR, logger=finus_api.logger.name):
        observation = asyncio.run(
            finus_api._run_mcp_timed(inner, tool_name="domestic_stock", timeout_sec=5)
        )

    assert json.loads(observation)["error"] == "mcp_call_failed"
    # 진짜 실패에도 스택은 남긴다 — 방침 B는 "잡되 조용히 삼키지 않는다"이다.
    assert any(r.exc_info is not None for r in caplog.records)


def test_save_diary_type_error_is_not_disguised_as_api_failure(monkeypatch, caplog):
    """일지 저장 경로(#358의 둘째 지점) — PR #356이 실제로 겪은 그 위장이다."""
    _install_client(monkeypatch, _raise_coding_bug)
    config = finus_api.FinusSaveDiaryConfig(backend_url="http://test-backend:8000")

    async def run_tool():
        async with finus_api.finus_save_diary(config, None) as info:
            return await info.single_fn(
                finus_api.FinusSaveDiaryInput(title="매매일지", content="본문")
            )

    with caplog.at_level(logging.ERROR, logger=finus_api.logger.name):
        observation = asyncio.run(run_tool())

    _assert_disguise_is_gone(observation, caplog, api_error="diary_api_request_failed")


def test_list_diaries_type_error_is_not_disguised_as_api_failure(monkeypatch, caplog):
    """일지 조회 경로(#358의 셋째 지점) — 한 곳만 고치면 나머지에 같은 위장이 남는다."""
    _install_client(monkeypatch, _raise_coding_bug)
    config = finus_api.FinusListDiariesConfig(backend_url="http://test-backend:8000")

    async def run_tool():
        async with finus_api.finus_list_diaries(config, None) as info:
            return await info.single_fn(finus_api.FinusListDiariesInput())

    with caplog.at_level(logging.ERROR, logger=finus_api.logger.name):
        observation = asyncio.run(run_tool())

    _assert_disguise_is_gone(observation, caplog, api_error="diary_api_request_failed")


def test_diary_real_request_failure_keeps_its_error_code(monkeypatch, caplog):
    """진짜 네트워크 실패는 종전대로 ``diary_api_request_failed``로 남는다."""

    def _refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    _install_client(monkeypatch, _refuse)
    config = finus_api.FinusSaveDiaryConfig(backend_url="http://test-backend:8000")

    async def run_tool():
        async with finus_api.finus_save_diary(config, None) as info:
            return await info.single_fn(
                finus_api.FinusSaveDiaryInput(title="매매일지", content="본문")
            )

    with caplog.at_level(logging.ERROR, logger=finus_api.logger.name):
        observation = asyncio.run(run_tool())

    assert json.loads(observation)["error"] == "diary_api_request_failed"
