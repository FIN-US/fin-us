import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

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


def test_save_trading_diary_posts_to_backend(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "success", "data": {"id": 1, "title": "T", "content": "C"}}

    class FakeClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(finus_api.httpx, "AsyncClient", FakeClient)
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

    assert captured["url"] == "http://test-backend:8000/api/v1/db/diary"
    assert captured["json"] == {"title": "매매일지 2026-05-24", "content": "본문"}
    # 키가 없는 배포에서는 헤더 자체를 붙이지 않는다. 빈 값을 보내면 backend가 그것을
    # "틀린 키"로 보므로(main.matches_api_key), 인증이 꺼진 배포에서까지 401이 된다.
    assert captured["headers"] == {}
    assert '"id": 1' in result


def test_save_trading_diary_sends_the_api_key_header(monkeypatch):
    """backend가 인증을 켠 배포에서는 `X-API-Key`를 실어 보냅니다 (#266 2단계).

    NAT는 컴포즈 내부에서 backend를 부르는 비브라우저 클라이언트다. 이 헤더가 빠지면
    매매일지 저장이 401로 떨어지는데, 그 실패는 에이전트 Observation 안에서만 보여서
    운영 중에 알아채기 어렵다.

    헤더 이름은 backend/main.py의 API_KEY_HEADER와 같아야 한다 — 어긋나면 인증을 켠
    날에야 드러난다. 이 테스트가 잡는 mutation: headers 인자를 다시 빼거나 이름을
    바꾸는 회귀.
    """
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "success", "data": {"id": 7}}

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers=None):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(finus_api.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("FINUS_API_KEY", "s3cr3t-key")

    config = finus_api.FinusSaveDiaryConfig(backend_url="http://test-backend:8000")

    async def run_tool():
        async with finus_api.finus_save_diary(config, None) as info:
            return await info.single_fn(
                finus_api.FinusSaveDiaryInput(title="제목", content="본문")
            )

    asyncio.run(run_tool())

    assert captured["headers"] == {"X-API-Key": "s3cr3t-key"}


def test_list_trading_diaries_sends_the_api_key_header(monkeypatch):
    """조회 쪽도 같은 헤더를 붙입니다 — 한쪽만 붙이면 그쪽만 401이 된다."""
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "success", "data": []}

    class FakeClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(finus_api.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("FINUS_API_KEY", "s3cr3t-key")

    config = finus_api.FinusListDiariesConfig(backend_url="http://test-backend:8000")

    async def run_tool():
        async with finus_api.finus_list_diaries(config, None) as info:
            return await info.single_fn(finus_api.FinusListDiariesInput())

    asyncio.run(run_tool())

    assert captured["headers"] == {"X-API-Key": "s3cr3t-key"}


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
