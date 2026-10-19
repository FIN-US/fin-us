"""KIS Trading MCP 호출의 모의/실전(env_dv) 고정.

에이전트 프롬프트가 예시로 ``env_dv="real"``을 넣던 탓에, 모의 키만 둔 배포에서 서버가
실전 인증을 시도해 시세·잔고 조회가 전부 실패했다. 래퍼가 배포의 KIS_URL로 정한다.
"""

import pytest

from finus_agents import finus_api

_DEMO_URL = "https://openapivts.koreainvestment.com:29443"
_REAL_URL = "https://openapi.koreainvestment.com:9443"


@pytest.mark.parametrize("url, expected", [(_DEMO_URL, "demo"), (_REAL_URL, "real")])
def test_agent_choice_is_overridden_by_deployment(monkeypatch, url, expected):
    monkeypatch.setenv("KIS_URL", url)
    other = "real" if expected == "demo" else "demo"

    pinned = finus_api._pin_env_dv({"fid_input_iscd": "005930", "env_dv": other})

    assert pinned == {"fid_input_iscd": "005930", "env_dv": expected}


def test_missing_env_dv_is_filled(monkeypatch):
    """비워 두면 서버가 모의로 실행하므로 실전 배포에서도 모의 인증을 탄다."""
    monkeypatch.setenv("KIS_URL", _REAL_URL)

    assert finus_api._pin_env_dv({}) == {"env_dv": "real"}


def test_left_alone_without_kis_url(monkeypatch):
    monkeypatch.delenv("KIS_URL", raising=False)

    assert finus_api._pin_env_dv({"env_dv": "real"}) == {"env_dv": "real"}


def test_prepare_pins_env_dv_for_every_call(monkeypatch):
    """래퍼 진입점(_prepare_kis_trading_mcp_call)이 실제로 고정을 거치는지 확인한다."""
    monkeypatch.setenv("KIS_URL", _DEMO_URL)
    config = finus_api.FinusAccountBalanceReadonlyConfig()
    inp = finus_api.KisTradingMcpCallInput(
        api_type="inquire_price",
        params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930", "env_dv": "real"},
    )

    prepared = finus_api._prepare_kis_trading_mcp_call(inp, config)

    assert not isinstance(prepared, str), prepared
    _tool, arguments = prepared
    assert arguments["params"]["env_dv"] == "demo"
