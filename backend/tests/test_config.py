import importlib
import logging
from pathlib import Path

import pytest

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
    # #130 — mcp-trading의 토큰 캐시 경로(index.js), TR ID 오버라이드(index.js).
    # 이 값들이 전달되지 않으면 컨테이너가 재생성될 때마다 KIS OAuth 토큰을
    # 새로 발급받거나(#124와 동일한 결함) TR ID 오버라이드가 무시된다.
    monkeypatch.setenv("KIS_TOKEN_CACHE_PATH", "/var/lib/finus/kis-token-cache.json")
    monkeypatch.setenv("KIS_TR_ID_DAILY_CCLD", "TTTC0081R")
    monkeypatch.setenv("KIS_TR_ID_BALANCE_RLZ_PL", "TTTC8494R")

    params = _stdio_server_params(Path("/opt/mcp-trading"))

    assert params.env["KIS_TOKEN_CACHE_PATH"] == "/var/lib/finus/kis-token-cache.json"
    assert params.env["KIS_TR_ID_DAILY_CCLD"] == "TTTC0081R"
    assert params.env["KIS_TR_ID_BALANCE_RLZ_PL"] == "TTTC8494R"


def test_mcp_stdio_params_pass_real_order_enabled_and_fail_closed_when_unset(monkeypatch):
    # #129 — mcp-trading/index.js와 order.js의 validateRealOrderGuard는 자식
    # 프로세스의 process.env.KIS_REAL_ORDER_ENABLED를 직접 읽는다. 설정
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
    # 않는 FINUS_KIS_TR_ID_* 오버라이드(mcp-trading/index.js)를 놓치고
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


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("true", True),
        (" true ", True),
        ("TRUE", False),
        ("True", False),
        ("1", False),
        ("yes", False),
        ("y", False),
        ("0", False),
        ("", False),
    ],
)
def test_is_truthy_flag_requires_exact_lowercase_true(raw_value, expected):
    # HIGH2 재발 방지 — mcp-trading/index.js는 KIS_REAL_ORDER_ENABLED를
    # `=== "true"`로만 비교한다(대소문자·다른 철자 불허). backend가 여기서
    # 조금이라도 더 관대하면(예: "TRUE"·"1"·"yes"도 인정) backend 게이트는
    # 통과하고 자식은 거부하는 진단 트랩이 재발한다(#129 최초 증상이 정규화로
    # 한 번 덮였다가, 이번엔 정규화 자체를 없애는 대신 두 기준을 완전히
    # 일치시켜 근본적으로 막는다). _is_truthy_flag가 .lower()나 다른 철자를
    # 다시 허용하도록 바뀌면 이 단언들이 즉시 실패한다.
    assert config._is_truthy_flag(raw_value) is expected


def test_mcp_stdio_params_forward_kis_real_order_enabled_raw_value_unchanged(monkeypatch):
    # backend 게이트(_is_truthy_flag)와 자식 가드가 이제 정확히 같은 기준
    # ("true" 리터럴)이므로, 자식에 넘기는 값은 원본 문자열을 다시 쓸 필요가
    # 없다(정규화 블록 제거). 이 테스트는 정규화를 다시 끼워 넣는 회귀 —
    # 원본과 다른 문자열로 rewrite하는 변경 — 을 잡는다.
    monkeypatch.setenv("KIS_REAL_ORDER_ENABLED", "true")

    params = _stdio_server_params(Path("/opt/mcp-trading"))

    assert params.env["KIS_REAL_ORDER_ENABLED"] == "true"


def test_mcp_stdio_params_forward_non_true_spelling_unchanged_and_blocked_on_both_sides(monkeypatch):
    # backend 게이트와 자식 가드가 이제 동일 기준을 쓰므로, "true"가 아닌
    # 철자("yes" 등)는 두 쪽 모두에서 막혀야 한다 — 자식에는 원본 문자열이
    # 그대로 전달되고("yes" != "true"이므로 자식도 거부), backend 게이트도
    # 같은 이유로 거부한다. 어느 한쪽만 넓히는 뮤테이션(예: _is_truthy_flag를
    # 다시 광범위한 집합으로 되돌리는 것)이 있으면 이 테스트가 잡는다.
    monkeypatch.setenv("KIS_REAL_ORDER_ENABLED", "yes")

    params = _stdio_server_params(Path("/opt/mcp-trading"))

    assert params.env["KIS_REAL_ORDER_ENABLED"] == "yes"
    assert config._is_truthy_flag("yes") is False


def test_visualization_url_is_trimmed_and_trailing_slash_preserved(monkeypatch):
    monkeypatch.setenv("VISUALIZATION_URL", " https://finus-visual.example/portfolio/ ")

    reloaded = importlib.reload(config)

    assert reloaded.VISUALIZATION_URL == "https://finus-visual.example/portfolio/"


# --- #298: 신호 점수 임계값 -------------------------------------------------


@pytest.fixture
def restore_config():
    """env를 바꿔 config를 reload한 테스트가 끝나면 원래 env로 되돌린다.

    importlib.reload는 모듈 객체를 제자리에서 바꾸므로, 되돌리지 않으면 뒤따르는
    테스트가 이 테스트의 env로 계산된 상수를 보게 된다.
    """
    yield
    importlib.reload(config)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", 1),
        ("3", 3),
        (" 2 ", 2),
    ],
)
def test_signal_score_threshold_reads_valid_env(monkeypatch, restore_config, raw, expected):
    monkeypatch.setenv("SIGNAL_SCORE_THRESHOLD", raw)

    assert importlib.reload(config).SIGNAL_SCORE_THRESHOLD == expected


@pytest.mark.parametrize("raw", ["0", "4", "-2", "높음", "1.5", ""])
def test_signal_score_threshold_falls_back_to_default_on_bad_env(
    monkeypatch, restore_config, raw
):
    """범위를 벗어난 임계값은 조용히 파이프라인을 망가뜨린다.

    0이면 모든 signal이 유의미해져 2차 필터가 사라지고(비용 폭증), 4 이상이면 어떤
    점수도 통과하지 못해 감시가 영구히 침묵한다(놓침). 둘 다 사고이므로 기본값 2로
    되돌린다.
    """
    monkeypatch.setenv("SIGNAL_SCORE_THRESHOLD", raw)

    assert importlib.reload(config).SIGNAL_SCORE_THRESHOLD == 2


def test_signal_score_threshold_defaults_to_two_when_unset(monkeypatch, restore_config):
    monkeypatch.delenv("SIGNAL_SCORE_THRESHOLD", raising=False)

    assert importlib.reload(config).SIGNAL_SCORE_THRESHOLD == 2


def test_signal_uncertainty_alert_threshold_rejects_negative_and_garbage(
    monkeypatch, restore_config
):
    monkeypatch.setenv("SIGNAL_UNCERTAINTY_ALERT_THRESHOLD", "-1")
    assert importlib.reload(config).SIGNAL_UNCERTAINTY_ALERT_THRESHOLD == 1.0

    monkeypatch.setenv("SIGNAL_UNCERTAINTY_ALERT_THRESHOLD", "높음")
    assert importlib.reload(config).SIGNAL_UNCERTAINTY_ALERT_THRESHOLD == 1.0

    monkeypatch.setenv("SIGNAL_UNCERTAINTY_ALERT_THRESHOLD", "0.75")
    assert importlib.reload(config).SIGNAL_UNCERTAINTY_ALERT_THRESHOLD == 0.75


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan"])
def test_signal_uncertainty_alert_threshold_rejects_non_finite(monkeypatch, restore_config, raw):
    """float()가 받아 주는 inf/nan을 통과시키면 안 된다.

    inf면 "기사 간 평가 엇갈림"이 어떤 표준편차로도 표시되지 않아 기능 하나가 조용히
    꺼진다. nan은 모든 비교가 False라 같은 결과다.
    """
    monkeypatch.setenv("SIGNAL_UNCERTAINTY_ALERT_THRESHOLD", raw)

    assert importlib.reload(config).SIGNAL_UNCERTAINTY_ALERT_THRESHOLD == 1.0


# ---------------------------------------------------------------------------
# 한도 env (#299) — #298 임계값과 **같은** 파서를 쓴다
#
# 병합 과정에서 _float_env가 두 벌이 되어 뒤엣것이 앞엣것을 가린 적이 있다. 그때
# ORDER_* 실수 설정만 isfinite·음수 가드를 잃었고, 어느 쪽이 걸리는지가 정의 순서에
# 좌우됐다. 두 쓰임이 같은 가드를 받는다는 것을 여기서 고정한다.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "-0.5", "20%", ""])
def test_order_ratio_limits_fall_back_on_unusable_values(monkeypatch, restore_config, raw):
    """비율 한도에 inf나 음수가 들어가면 그 한도가 사실상 사라진다.

    inf면 어떤 비중도 초과가 아니고, 음수면 반대로 모든 주문이 걸린다. 둘 다
    "설정이 조용히 한도 하나를 끄는" 유형이라 기본값으로 되돌린다.
    """
    monkeypatch.setenv("ORDER_MAX_POSITION_RATIO", raw)

    assert importlib.reload(config).ORDER_MAX_POSITION_RATIO == 0.20


def test_order_ratio_limit_accepts_a_normal_value(monkeypatch, restore_config):
    monkeypatch.setenv("ORDER_MAX_POSITION_RATIO", "0.35")

    assert importlib.reload(config).ORDER_MAX_POSITION_RATIO == 0.35


def test_order_amount_limit_falls_back_on_a_thousands_separator(monkeypatch, restore_config):
    """쉼표를 넣으면 int()가 실패해 기본값 — 즉 의도한 한도의 두 배가 걸린다."""
    monkeypatch.setenv("ORDER_MAX_ORDER_AMOUNT", "500,000")

    assert importlib.reload(config).ORDER_MAX_ORDER_AMOUNT == 1_000_000


def test_bad_limit_env_is_logged_not_silent(monkeypatch, restore_config, caplog):
    """한도에서는 기본값이 곧 가장 넓은 값이라, 되돌아간 사실이 조용하면 안 된다."""
    monkeypatch.setenv("ORDER_MAX_ORDER_AMOUNT", "500,000")

    with caplog.at_level(logging.WARNING, logger=config.__name__):
        importlib.reload(config)

    assert any("ORDER_MAX_ORDER_AMOUNT" in record.message for record in caplog.records)


def test_bad_signal_threshold_env_is_logged_too(monkeypatch, restore_config, caplog):
    """같은 경고 경로를 #298 임계값도 쓴다 — 파서가 하나이기 때문이다."""
    monkeypatch.setenv("SIGNAL_SCORE_THRESHOLD", "9")

    with caplog.at_level(logging.WARNING, logger=config.__name__):
        reloaded = importlib.reload(config)

    assert reloaded.SIGNAL_SCORE_THRESHOLD == 2
    assert any("SIGNAL_SCORE_THRESHOLD" in record.message for record in caplog.records)
