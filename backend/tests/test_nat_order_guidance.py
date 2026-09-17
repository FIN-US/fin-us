"""NAT 채팅의 주문 안내 문구가 실제 텔레그램 파서와 맞는지 고정한다 (#380).

#380으로 채팅 ``trading_agent``·``monitoring_agent``는 주문을 내지 않고, 주문 요청을 받으면
텔레그램 ``/buy``·``/sell``·``/advise`` 명령을 안내한다. 안내 문구는
``finus_nat/configs/prompts/react_kis_chat.md``에 있고, 문법의 주인은 이 저장소의
``backend/telegram_commands.py``다. 두 곳이 따로 움직이면 에이전트가 **파서가 다르게 읽는
명령**을 사용자에게 알려 주게 된다 — 확정 화면에서 잡히긴 하지만 사용자는 엉뚱한 주문
확인을 받는다.

그래서 이 테스트는 문구를 복사해 두지 않고 **양쪽을 직접 읽는다**: 사용법 줄은 backend의
``*_COMMAND_HELP`` 상수에서, 예시 명령은 프롬프트 파일에서 뽑아 실제 파서에 넣는다.
backend 테스트에 둔 이유는 파서를 import해 **돌려 봐야** 해서다(NAT 스위트는 backend 의존성이
없다). 프롬프트 파일은 레포 상대 경로로 읽는다 — NAT 쪽 테스트가 backend 판정표
(``backend/tests/fixtures/price_gap_policy.json``)를 읽는 것과 같은 방식이다.

#387부터 파서는 위치로 가능한 해석을 모두 내고, 어느 해석인지는 종목 마스터가 정한다. 그래서
예시는 "해석 목록 중 종목명이 실제 마스터(``mcp-trading/data/stocks.json``)에 있는 것이 기대한
해석 **하나뿐**"으로 확인한다. 마스터 대조는 resolveStock의 완전 일치 규칙(코드·이름·별칭)만
흉내 낸다 — 예시 종목은 그 규칙으로 충분하다.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.telegram_commands import (
    ADVISE_COMMAND_HELP,
    BUY_COMMAND_HELP,
    SELL_COMMAND_HELP,
    OrderReading,
    TelegramCommandHandler,
    _telegram_command_parts,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPT_PATH = _REPO_ROOT / "finus_nat" / "configs" / "prompts" / "react_kis_chat.md"
_STOCK_MASTER_PATH = _REPO_ROOT / "mcp-trading" / "data" / "stocks.json"
_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")
_STOCKS = json.loads(_STOCK_MASTER_PATH.read_text(encoding="utf-8"))
_MASTER_KEYS = {
    key
    for stock in _STOCKS
    for key in [stock["code"], stock["name"], *(stock.get("aliases") or [])]
}

# 프롬프트의 "예) /명령 ..." 한 줄. 끝의 설명 없이 명령만 두는 것이 프롬프트의 규칙이다.
_EXAMPLE_RE = re.compile(r"예\) (/\w+(?: \S+)*)")

# 프롬프트에 실린 예시 명령 → 파서가 읽어야 하는 뜻. 새 예시를 넣으면 여기에도 적어야
# 테스트가 통과한다 — 예시마다 사람이 해석을 한 번 확인하게 하려는 것이다.
_EXPECTED_EXAMPLES: dict[str, tuple[str, Any]] = {
    "/buy 삼성전자 10 75000": ("/buy", OrderReading("삼성전자", 10, 75000, "LIMIT")),
    "/sell 삼성전자 10": ("/sell", OrderReading("삼성전자", 10, 0, "MARKET")),
    "/advise 삼성전자": ("/advise", "삼성전자"),
    # 종목명이 숫자로 끝나는 종목 — 이름 그대로 입력해도 마스터가 가려 읽는다 (#387)
    "/buy KODEX 200 10": ("/buy", OrderReading("KODEX 200", 10, 0, "MARKET")),
    # 봇이 모호하다고 되물을 때 쓰는 종목코드 입력 (#387)
    "/buy 069500 10": ("/buy", OrderReading("069500", 10, 0, "MARKET")),
}


@pytest.fixture
def handler() -> TelegramCommandHandler:
    # 파서만 쓴다. 저장소·원장은 건드리지 않으므로 기본 SQLite 저장소를 만들지 않게 대역을 준다.
    return TelegramCommandHandler(
        notifier=SimpleNamespace(chat_id="1"),  # type: ignore[arg-type]
        watchlist_repo=object(),
        catalyst_repo=object(),
    )


def _examples() -> list[str]:
    return _EXAMPLE_RE.findall(_PROMPT)


def _readings_in_master(handler: TelegramCommandHandler, argument: str) -> list[OrderReading]:
    return [
        reading
        for reading in handler._order_argument_readings(argument)
        if reading.stock_name in _MASTER_KEYS
    ]


@pytest.mark.parametrize(
    "help_text", [BUY_COMMAND_HELP, SELL_COMMAND_HELP, ADVISE_COMMAND_HELP], ids=["buy", "sell", "advise"]
)
def test_prompt_states_the_usage_line_the_bot_itself_prints(help_text: str):
    """사용법 줄(`/buy <종목명> <수량> [지정가]` 등)은 봇의 도움말 상수와 글자 그대로 같다.

    뮤테이션: 프롬프트의 ``/buy <종목명> <수량> [지정가]``를 ``/buy <종목명> <지정가> <수량>``으로
    바꾸면 red.
    """
    usage = help_text.removeprefix("사용법: ")
    assert usage != help_text, f"도움말 상수 형식이 바뀌었습니다: {help_text!r}"
    assert usage in _PROMPT


def test_every_example_in_the_prompt_is_listed_here():
    """프롬프트의 예시와 이 파일의 기대값 표가 같은 집합이다 — 예시가 검증 없이 늘지 않는다.

    뮤테이션: 프롬프트에 ``예) /buy KODEX 200 10 30000``을 추가하면 red.
    """
    assert sorted(_examples()) == sorted(_EXPECTED_EXAMPLES)


@pytest.mark.parametrize("example", sorted(_EXPECTED_EXAMPLES))
def test_prompt_example_parses_as_the_prompt_says(handler: TelegramCommandHandler, example: str):
    """예시 명령을 봇이 받는 그대로 나눠 실제 파서에 넣는다.

    파서의 해석 중 종목명이 마스터에 있는 것이 기대한 해석 하나뿐이어야 한다 — 봇은 그 해석으로
    주문 확인을 만든다(둘 이상이면 되묻는다).

    뮤테이션: ``_order_argument_readings``가 끝 두 숫자를 (지정가, 수량) 순서로 읽게 바꾸면
    지정가 예시가 red, 시장가 해석을 빼면 ``KODEX 200`` 예시가 red. (프롬프트 쪽 예시를 뒤바꾸는
    변경은 위 목록 대조 테스트가 잡는다.)
    """
    command, bot_username, argument = _telegram_command_parts(example)
    expected_command, expected = _EXPECTED_EXAMPLES[example]

    assert (command, bot_username) == (expected_command, "")
    if command == "/advise":
        # _handle_advise는 인자 전체를 종목명으로 쓴다.
        assert argument.strip() == expected
    else:
        assert _readings_in_master(handler, argument) == [expected]


def test_market_order_rule_matches_the_parser(handler: TelegramCommandHandler):
    """프롬프트의 "지정가를 빼면 시장가" 규칙이 파서와 같다."""
    assert "지정가를 빼면 시장가" in _PROMPT
    assert handler._order_argument_readings("삼성전자 10") == [
        OrderReading("삼성전자", 10, 0, "MARKET")
    ]


def test_prompt_no_longer_warns_the_trailing_number_trap_and_explains_the_fallback(
    handler: TelegramCommandHandler,
):
    """숫자로 끝나는 종목명은 이름 그대로 안내하고, 봇이 되물을 때만 종목코드로 안내한다 (#387).

    #380 때 이 자리는 버그("끝 숫자가 수량으로 읽힌다")를 고정했다. #387이 파서를 고쳐 그 안내가
    틀린 말이 됐으므로 반대로 고정한다 — 옛 경고가 남거나, 되묻기 안내가 빠지는 것을 둘 다 막는다.

    뮤테이션: 프롬프트에 옛 문장("종목명 끝의 숫자가 수량으로 읽힙니다")을 되살리면 red, 되묻기
    안내 줄을 지우면 red.
    """
    assert _readings_in_master(handler, "KODEX 200 10") == [
        OrderReading("KODEX 200", 10, 0, "MARKET")
    ]
    assert "종목명 끝의 숫자가 수량으로 읽힙니다" not in _PROMPT
    assert "종목명이 숫자로 끝나는 종목(예: KODEX 200)도 종목명 그대로" in _PROMPT
    assert "입력이 두 가지로 읽혀 주문을 만들지 않았다" in _PROMPT
    assert "6자리 종목코드" in _PROMPT


def test_trap_workaround_code_is_the_named_stock():
    """종목코드 예시가 프롬프트가 든 그 종목(KODEX 200)의 코드다 — 엉뚱한 종목을 안내하지 않는다.

    종목 마스터는 ``/buy``가 종목코드를 해석하는 원천(``mcp-trading/data/stocks.json``)이다.
    """
    by_code = {s["code"]: s["name"] for s in _STOCKS}
    assert by_code.get("069500") == "KODEX 200"
