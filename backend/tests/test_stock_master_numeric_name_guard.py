"""종목 마스터 "코드 형태 토큰 + 숫자" 이름 판정표를 backend 파서와 대조한다 (PR #392 리뷰).

backend의 /buy·/sell 파서는 이름 자리가 종목코드 하나면 시장가 해석을 만들지 않는다
(telegram_commands._order_argument_readings). 그래서 마스터에 "ABC123 200" 같은 이름·별칭이
생기면 그 종목은 이름으로 시장가 주문할 수 없고 모호한 입력을 되묻지도 못한다. 운영자가
``mcp-trading/scripts/update_stock_master.py``로 마스터를 직접 갱신할 때 스크립트가 쓰기 전에
같은 검사를 하는데, 스크립트는 backend를 import하지 않고 따로 구현한다.

두 구현은 공유 판정표 ``mcp-trading/tests/fixtures/unreadable_numeric_name_policy.json``으로 묶인다.
이 파일은 표가 실제 backend 파서의 동작과 같은지를 본다 — backend를 import해야 하므로 backend 스위트에
둔다. 스크립트 쪽 판정·쓰기 거부 테스트는 backend 의존이 없어
``mcp-trading/tests/test_update_stock_master_numeric_name_guard.py``로 옮겼다(#393에서 CI가
mcp-trading의 파이썬 테스트를 돌리게 되면서, 여기 두었던 이유가 사라졌다).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.telegram_commands import TelegramCommandHandler

_REPO_ROOT = Path(__file__).resolve().parents[2]
_POLICY_PATH = _REPO_ROOT / "mcp-trading" / "tests" / "fixtures" / "unreadable_numeric_name_policy.json"
_CASES = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["cases"]
_CASE_IDS = [case["label"] for case in _CASES]


@pytest.fixture
def handler() -> TelegramCommandHandler:
    return TelegramCommandHandler(
        notifier=SimpleNamespace(chat_id="1"),  # type: ignore[arg-type]
        watchlist_repo=object(),
        catalyst_repo=object(),
    )


def test_policy_table_has_both_verdicts():
    """판정표가 한쪽 판정만 담으면 두 구현이 같은 방향으로 틀려도 드러나지 않는다."""
    assert {case["unreadable"] for case in _CASES} == {True, False}
    for case in _CASES:
        assert case["label"] == " ".join(case["label"].split()), case


@pytest.mark.parametrize("case", _CASES, ids=_CASE_IDS)
def test_backend_parser_agrees_with_the_policy_table(handler: TelegramCommandHandler, case):
    """실제 파서에 ``<label> 1``을 넣어, 그 이름을 종목명으로 쓰는 시장가 해석이 있는지로 판정한다.

    표가 backend 파서의 동작을 그대로 적고 있는지 확인한다. 파서의 분기(코드 형태·수량 판정)가 바뀌면
    여기가 red가 되고, 그때 스크립트 판정도 같이 고쳐야 한다(스크립트 쪽 대조는 mcp-trading 스위트).
    """
    readings = handler._order_argument_readings(f"{case['label']} 1")
    readable = any(
        reading.order_type == "MARKET" and reading.stock_name == case["label"] for reading in readings
    )
    assert (not readable) is case["unreadable"]
