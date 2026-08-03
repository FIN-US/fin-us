import importlib
from pathlib import Path

import backend.config as config
from backend.config import DART_MCP_PARAMS, NEWS_MCP_PARAMS, TRADING_MCP_PARAMS, _stdio_server_params


def test_mcp_stdio_params_pass_filtered_environment_to_child_processes():
    assert isinstance(NEWS_MCP_PARAMS.env, dict)
    assert isinstance(TRADING_MCP_PARAMS.env, dict)
    assert isinstance(DART_MCP_PARAMS.env, dict)


def test_mcp_stdio_params_do_not_pass_parent_only_secrets(monkeypatch):
    monkeypatch.setenv("NAVER_CLIENT_ID", "naver-id")
    monkeypatch.setenv("KIS_API_KEY", "kis-key")
    monkeypatch.setenv("DART_API_KEY", "dart-key")
    monkeypatch.setenv("FIN_US_TRACE_ID", "trace-id")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@db/prod")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    # 텔레그램 봇 토큰: mcp-trading/mcp-news/mcp-dart 중 어느 것도 읽지 않는
    # backend 전용 비밀값이다. KIS_/FINUS_KIS_/FIN_US_ 어떤 접두사에도 걸리지
    # 않으므로 자식 프로세스로 새어나가면 안 된다.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")

    params = _stdio_server_params(Path("/tmp/mcp-news"))

    assert params.env["NAVER_CLIENT_ID"] == "naver-id"
    assert params.env["KIS_API_KEY"] == "kis-key"
    assert params.env["DART_API_KEY"] == "dart-key"
    assert params.env["FIN_US_TRACE_ID"] == "trace-id"
    assert "DATABASE_URL" not in params.env
    assert "OPENAI_API_KEY" not in params.env
    assert "TELEGRAM_BOT_TOKEN" not in params.env


def test_mcp_stdio_params_pass_order_dedup_ledger_settings(monkeypatch):
    monkeypatch.setenv("KIS_ORDER_DEDUP_PATH", "/var/lib/finus/kis-order-dedup.json")
    monkeypatch.setenv("KIS_ORDER_DEDUP_TTL_MS", "120000")

    params = _stdio_server_params(Path("/opt/mcp-trading"))

    assert params.env["KIS_ORDER_DEDUP_PATH"] == "/var/lib/finus/kis-order-dedup.json"
    assert params.env["KIS_ORDER_DEDUP_TTL_MS"] == "120000"


def test_mcp_stdio_params_pass_kis_token_cache_and_tr_id_overrides(monkeypatch):
    # #130 — mcp-trading/index.js:60의 토큰 캐시 경로, index.js:46,54의 TR ID
    # 오버라이드. 이 값들이 전달되지 않으면 컨테이너가 재생성될 때마다 KIS
    # OAuth 토큰을 새로 발급받거나(#124와 동일한 결함) TR ID 오버라이드가
    # 무시된다.
    monkeypatch.setenv("KIS_TOKEN_CACHE_PATH", "/var/lib/finus/kis-token-cache.json")
    monkeypatch.setenv("KIS_TR_ID_DAILY_CCLD", "TTTC0081R")
    monkeypatch.setenv("KIS_TR_ID_BALANCE_RLZ_PL", "TTTC8494R")

    params = _stdio_server_params(Path("/opt/mcp-trading"))

    assert params.env["KIS_TOKEN_CACHE_PATH"] == "/var/lib/finus/kis-token-cache.json"
    assert params.env["KIS_TR_ID_DAILY_CCLD"] == "TTTC0081R"
    assert params.env["KIS_TR_ID_BALANCE_RLZ_PL"] == "TTTC8494R"


def test_mcp_stdio_params_pass_real_order_enabled_and_fail_closed_when_unset(monkeypatch):
    # #129 — mcp-trading/index.js:41,344와 order.js:56-61의 실계좌 주문 가드는
    # 자식 프로세스의 process.env.KIS_REAL_ORDER_ENABLED를 직접 읽는다. 설정
    # 시에는 반드시 통과해야 하고(그렇지 않으면 .env=true여도 Docker에서 항상
    # 차단된다), 미설정 시에는 os.environ에 키 자체가 없으므로 자식에도
    # 전달되지 않아 fail-closed(주문 차단)가 유지되어야 한다.
    monkeypatch.setenv("KIS_REAL_ORDER_ENABLED", "true")
    params_enabled = _stdio_server_params(Path("/opt/mcp-trading"))
    assert params_enabled.env["KIS_REAL_ORDER_ENABLED"] == "true"

    monkeypatch.delenv("KIS_REAL_ORDER_ENABLED", raising=False)
    params_unset = _stdio_server_params(Path("/opt/mcp-trading"))
    assert "KIS_REAL_ORDER_ENABLED" not in params_unset.env


def test_mcp_stdio_params_pass_finus_kis_tr_id_overrides_but_not_other_finus_vars(monkeypatch):
    # #130 — backend/config.py의 접두사는 기존 FIN_US_(밑줄 포함)와 겹치지
    # 않는 FINUS_KIS_TR_ID_* 오버라이드(mcp-trading/index.js:46,54)를 놓치고
    # 있었다. FINUS_KIS_ 접두사로 이 변수들만 통과시키고, KIS_와 무관한 다른
    # FINUS_* 변수(backend/NAT 전용, mcp-trading이 읽지 않음)는 여전히
    # 차단되어야 접두사 경계가 필요 이상으로 넓어지지 않는다.
    monkeypatch.setenv("FINUS_KIS_TR_ID_DAILY_CCLD", "TTTC0081R")
    monkeypatch.setenv("FINUS_KIS_TR_ID_BALANCE_RLZ_PL", "TTTC8494R")
    monkeypatch.setenv("FINUS_BACKEND_URL", "http://backend:8000")

    params = _stdio_server_params(Path("/opt/mcp-trading"))

    assert params.env["FINUS_KIS_TR_ID_DAILY_CCLD"] == "TTTC0081R"
    assert params.env["FINUS_KIS_TR_ID_BALANCE_RLZ_PL"] == "TTTC8494R"
    assert "FINUS_BACKEND_URL" not in params.env


def test_mcp_stdio_params_forward_any_kis_prefixed_variable_by_mechanism(monkeypatch):
    # 드리프트 방지 테스트. 개별 키 이름을 나열하는 테스트는 화이트리스트가
    # 다시 개별 나열 방식으로 되돌아가면(즉 #124/#127/#129/#130이 반복될
    # 새로운 KIS_* 변수가 mcp-trading에 추가되면) 그 이름을 목록에 추가해야만
    # 통과하므로 같은 드리프트를 재인코딩할 뿐이다. 이 테스트는 목록에
    # 존재한 적 없는 합성 변수명을 사용해 "KIS_로 시작하면 전달된다"는
    # 접두사 매칭 메커니즘 자체를 검증한다 — 누군가 _MCP_ENV_ALLOWED_PREFIXES에서
    # "KIS_"를 제거하거나 개별 키 나열로 되돌리면(뮤테이션) 이 테스트는
    # 어떤 이름 목록 갱신 없이도 즉시 실패한다.
    monkeypatch.setenv("KIS_FUTURE_VARIABLE_NOT_YET_INVENTED", "future-value")

    params = _stdio_server_params(Path("/opt/mcp-trading"))

    assert params.env["KIS_FUTURE_VARIABLE_NOT_YET_INVENTED"] == "future-value"


def test_visualization_url_is_trimmed_and_trailing_slash_preserved(monkeypatch):
    monkeypatch.setenv("VISUALIZATION_URL", " https://finus-visual.example/portfolio/ ")

    reloaded = importlib.reload(config)

    assert reloaded.VISUALIZATION_URL == "https://finus-visual.example/portfolio/"
