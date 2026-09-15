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
    TelegramCommandHandler,
    _telegram_command_parts,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROMPT_PATH = _REPO_ROOT / "finus_nat" / "configs" / "prompts" / "react_kis_chat.md"
_STOCK_MASTER_PATH = _REPO_ROOT / "mcp-trading" / "data" / "stocks.json"
_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# 프롬프트의 "예) /명령 ..." 한 줄. 끝의 설명 없이 명령만 두는 것이 프롬프트의 규칙이다.
_EXAMPLE_RE = re.compile(r"예\) (/\w+(?: \S+)*)")

# 프롬프트에 실린 예시 명령 → 파서가 읽어야 하는 뜻. 새 예시를 넣으면 여기에도 적어야
# 테스트가 통과한다 — 예시마다 사람이 해석을 한 번 확인하게 하려는 것이다.
_EXPECTED_EXAMPLES: dict[str, tuple[str, Any]] = {
    "/buy 삼성전자 10 75000": ("/buy", ("삼성전자", 10, 75000, "LIMIT")),
    "/sell 삼성전자 10": ("/sell", ("삼성전자", 10, 0, "MARKET")),
    "/advise 삼성전자": ("/advise", "삼성전자"),
    # 종목명이 숫자로 끝나는 종목의 회피 — 6자리 종목코드 (#387)
    "/buy 069500 10": ("/buy", ("069500", 10, 0, "MARKET")),
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

    뮤테이션: 프롬프트에 ``예) /buy KODEX 200 10``을 추가하면 red.
    """
    assert sorted(_examples()) == sorted(_EXPECTED_EXAMPLES)


@pytest.mark.parametrize("example", sorted(_EXPECTED_EXAMPLES))
def test_prompt_example_parses_as_the_prompt_says(handler: TelegramCommandHandler, example: str):
    """예시 명령을 봇이 받는 그대로 나눠 실제 파서에 넣는다.

    뮤테이션: ``_parse_order_argument``가 끝 두 숫자를 (지정가, 수량) 순서로 읽게 바꾸면
    지정가 예시가 red. (프롬프트 쪽 예시를 뒤바꾸는 변경은 위 목록 대조 테스트가 잡는다.)
    """
    command, bot_username, argument = _telegram_command_parts(example)
    expected_command, expected = _EXPECTED_EXAMPLES[example]

    assert (command, bot_username) == (expected_command, "")
    if command == "/advise":
        # _handle_advise는 인자 전체를 종목명으로 쓴다.
        assert argument.strip() == expected
    else:
        assert handler._parse_order_argument(argument) == expected


def test_market_order_rule_matches_the_parser(handler: TelegramCommandHandler):
    """프롬프트의 "지정가를 빼면 시장가" 규칙이 파서와 같다."""
    assert "지정가를 빼면 시장가" in _PROMPT
    assert handler._parse_order_argument("삼성전자 10") == ("삼성전자", 10, 0, "MARKET")


def test_trailing_number_trap_is_real_and_the_prompt_warns_about_it(handler: TelegramCommandHandler):
    """종목명이 숫자로 끝나면 끝 숫자가 수량으로 읽힌다 — 프롬프트는 종목코드 입력을 안내한다.

    이 단언의 첫 줄은 **버그를 고정한다**(#387). #387이 파서를 고치면 여기가 빨개지고, 그때
    프롬프트의 회피 문구와 이 테스트를 함께 정리하면 된다. 안내가 필요 없는데 남아 있거나,
    필요한데 빠지는 일을 둘 다 막는다.

    뮤테이션: 프롬프트에서 "6자리 종목코드" 안내 줄을 지우면 red.
    """
    assert handler._parse_order_argument("KODEX 200 10") == ("KODEX", 200, 10, "LIMIT")
    assert "종목명이 숫자로 끝나는 종목(예: KODEX 200)" in _PROMPT
    assert "6자리 종목코드" in _PROMPT


def test_trap_workaround_code_is_the_named_stock():
    """회피 예시의 종목코드가 프롬프트가 든 그 종목(KODEX 200)의 코드다 — 엉뚱한 종목을 안내하지 않는다.

    종목 마스터는 ``/buy``가 종목코드를 해석하는 원천(``mcp-trading/data/stocks.json``)이다.
    """
    stocks = json.loads(_STOCK_MASTER_PATH.read_text(encoding="utf-8"))
    by_code = {s["code"]: s["name"] for s in stocks}
    assert by_code.get("069500") == "KODEX 200"
