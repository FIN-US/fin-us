"""#436: 첫 턴 강제 루프가 실행 전에 거를 KIS 입력 — api_type을 어디에서도 찾을 수 없는 호출.

강제 첫 턴(``tool_choice="required"``)에서 모델이 KIS 도구를 ``{}``로 골랐다. 이 판정은
``KisTradingMcpCallInput.forced_call_rejection``이 하고, 래퍼(``_prepare_kis_trading_mcp_call``)의
api_type 오류 두 갈래와 정확히 같아야 한다. 입력 스키마는 이런 입력도 받는다 — 강제 턴 밖의 같은
호출은 래퍼까지 가서 원장에 실패로 남아야 하기 때문이다(PR #437 리뷰).
"""

import pytest

from finus_agents.finus_api import FinusAccountBalanceConfig
from finus_agents.finus_api import KisTradingMcpCallInput
from finus_agents.finus_api import _prepare_kis_trading_mcp_call

_MISSING_ARGUMENTS = [
    {},
    {"tool_name": "domestic_stock"},
    {"params": {"stock_name": "삼성전자"}},
    {"tool_input": ""},
    "{}",
]
# 묶음은 꺼냈지만 그 안에 api_type이 없다 — 래퍼는 kis_mcp_missing_api_type을 낸다
_MISSING_API_TYPE = [
    {"api_type": "", "params": {"stock_name": "삼성전자"}},
    {"api_type": "   "},
    {"tool_input": '{"params": {}}'},
    {"tool_input": '{"tool_name": "domestic_stock"}'},
]
_VALID = [
    {"api_type": "inquire_price", "params": {"stock_name": "삼성전자"}},
    {"tool_name": "domestic_stock", "api_type": "inquire_balance"},
    '{"api_type": "inquire_price", "params": {"stock_name": "삼성전자"}}',
    # 레거시 tool_input 경로 — 래퍼가 여기서 api_type을 꺼내 쓴다
    {"tool_input": {"api_type": "inquire_price", "params": {"stock_name": "삼성전자"}}},
    {"tool_input": '{"api_type": "inquire_price", "params": {"stock_name": "삼성전자"}}'},
]


def _prepare_error(payload) -> str | None:
    inp = KisTradingMcpCallInput.model_validate(payload)
    config = FinusAccountBalanceConfig.model_construct(trading_tool_name="domestic_stock")
    prepared = _prepare_kis_trading_mcp_call(inp, config)
    if isinstance(prepared, str):
        import json

        return json.loads(prepared)["error"]
    return None


@pytest.mark.parametrize("payload", _MISSING_ARGUMENTS)
def test_rejects_input_the_wrapper_reports_as_missing_arguments(payload):
    """뮤테이션: ``forced_call_rejection``이 늘 None을 돌려주면 red."""
    reason = KisTradingMcpCallInput.model_validate(payload).forced_call_rejection()

    assert reason is not None and reason.startswith("kis_mcp_missing_arguments")
    assert _prepare_error(payload) == "kis_mcp_missing_arguments"


@pytest.mark.parametrize("payload", _MISSING_API_TYPE)
def test_rejects_input_the_wrapper_reports_as_missing_api_type(payload):
    """리뷰 지적 1: 레거시 tool_input에 api_type 없는 묶음이 오면 래퍼는 missing_api_type을 낸다.

    뮤테이션: ``_kis_call_payload``의 missing_api_type 갈래를 지우면(묶음만 있으면 통과) red.
    """
    reason = KisTradingMcpCallInput.model_validate(payload).forced_call_rejection()

    assert reason is not None and reason.startswith("kis_mcp_missing_api_type")
    assert _prepare_error(payload) == "kis_mcp_missing_api_type"


@pytest.mark.parametrize("payload", _VALID)
def test_accepts_every_input_the_wrapper_can_prepare(payload):
    assert KisTradingMcpCallInput.model_validate(payload).forced_call_rejection() is None
    assert _prepare_error(payload) is None


@pytest.mark.parametrize("payload", _MISSING_ARGUMENTS + _MISSING_API_TYPE)
def test_input_schema_still_accepts_calls_without_api_type(payload):
    """리뷰 지적 2: 입력 스키마가 이런 입력을 거부하면 강제 턴 밖의 호출이 래퍼에 닿지 못해
    원장·``tools_used``에서 실패가 사라진다. 판정은 사전 검사에만 둔다."""
    KisTradingMcpCallInput.model_validate(payload)
