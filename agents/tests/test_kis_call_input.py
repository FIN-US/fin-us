"""#436: KIS 래퍼 입력은 api_type을 어디에서도 찾을 수 없으면 유효한 호출이 아니다.

강제 첫 턴(``tool_choice="required"``)에서 모델이 KIS 도구를 ``{}``로 골랐고, 필드가 전부 선택인
입력 스키마는 그것을 받아 줬다. 판정은 ``_prepare_kis_trading_mcp_call``의
``kis_mcp_missing_arguments`` 조건과 같아야 한다 — 래퍼가 처리할 수 있는 입력을 막으면 안 된다.
"""

import pytest
from pydantic import ValidationError

from finus_agents.finus_api import FinusAccountBalanceConfig
from finus_agents.finus_api import KisTradingMcpCallInput
from finus_agents.finus_api import _prepare_kis_trading_mcp_call


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tool_name": "domestic_stock"},
        {"params": {"stock_name": "삼성전자"}},
        {"api_type": "", "params": {"stock_name": "삼성전자"}},
        {"api_type": "   "},
        {"tool_input": ""},
        "{}",
    ],
)
def test_rejects_input_without_api_type(payload):
    """뮤테이션: ``_require_api_type``를 지우면 전부 통과해 red."""
    with pytest.raises(ValidationError, match="api_type이 없어"):
        KisTradingMcpCallInput.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"api_type": "inquire_price", "params": {"stock_name": "삼성전자"}},
        {"tool_name": "domestic_stock", "api_type": "inquire_balance"},
        '{"api_type": "inquire_price", "params": {"stock_name": "삼성전자"}}',
        # 레거시 tool_input 경로 — 래퍼가 여기서 api_type을 꺼내 쓴다
        {"tool_input": {"api_type": "inquire_price", "params": {"stock_name": "삼성전자"}}},
        {"tool_input": '{"api_type": "inquire_price", "params": {"stock_name": "삼성전자"}}'},
    ],
)
def test_accepts_every_input_the_wrapper_can_prepare(payload):
    inp = KisTradingMcpCallInput.model_validate(payload)
    config = FinusAccountBalanceConfig.model_construct(trading_tool_name="domestic_stock")

    prepared = _prepare_kis_trading_mcp_call(inp, config)

    assert not isinstance(prepared, str), prepared
    assert prepared[1]["api_type"] in {"inquire_price", "inquire_balance"}
