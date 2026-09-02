"""수급 요약/상세 테스트 (#297 검수 4차).

가로 표는 텔레그램에서 유지할 수 없다 — 한 행이 접히면 뒷조각이 아무 열에나 떨어지고,
접히는 폭은 읽는 쪽 글자 크기 설정에 달려 있어 우리가 보장할 수 없다. 그래서 세로 나열로
펴고, 길어진 만큼 요약과 상세로 나눈다.
"""

import re

import pytest

from backend.telegram_commands import (
    MARKET_TREND_DETAIL_CALLBACK,
    trend_detail_button_text,
    TelegramCommandHandler,
    _format_trend_detail,
    _format_trend_summary,
    _flow_streak,
    _parse_investor_flows,
)

# mcp-trading/index.js의 getInvestorTrading이 실제로 만드는 모양. 이 문자열이 곧 우리가
# 기대는 계약이라, 저쪽 형식이 바뀌면 여기서 먼저 빨갛게 깨져야 한다.
MCP_TREND_RESPONSE = "\n".join(
    [
        "[삼성전자] 투자자 매매동향",
        "- 종목코드: 005930",
        "- 최근 수급:",
        "20260818 | 개인: -410,123 | 외국인: 1,240,556 | 기관: -830,221",
        "20260817 | 개인: -88,412 | 외국인: 302,118 | 기관: -213,706",
        "20260814 | 개인: 12,004 | 외국인: 550,900 | 기관: -430,880",
        "- 기준 데이터: 한국투자증권 Open API inquire-investor",
    ]
)


class FakeNotifier:
    def __init__(self, chat_id="123"):
        self.chat_id = chat_id
        self.bot_username = ""
        self.messages = []
        self.reply_markups = []
        self.actions = []
        self.callback_answers = []

    async def send_text(self, text, *, reply_markup=None):
        self.messages.append(text)
        self.reply_markups.append(reply_markup)
        return True

    async def send_chat_action(self, action="typing"):
        self.actions.append(action)
        return True

    async def answer_callback_query(self, callback_query_id, text=None):
        self.callback_answers.append((callback_query_id, text))
        return True


class BrokenState:
    def __init__(self):
        raise ConnectionError("redis unavailable")


def _handler(notifier, response=MCP_TREND_RESPONSE):
    calls = []

    async def mcp_runner(server_params, tool_name, arguments):
        calls.append((server_params, tool_name, arguments))
        return response

    handler = TelegramCommandHandler(
        notifier=notifier,
        mcp_runner=mcp_runner,
        state_factory=BrokenState,
    )
    return handler, calls


# ---- 파싱 ----


def test_rows_are_parsed_newest_first():
    flows = _parse_investor_flows(MCP_TREND_RESPONSE)

    assert [flow.date for flow in flows] == ["20260818", "20260817", "20260814"]
    assert flows[0].foreign == 1_240_556
    assert flows[0].institution == -830_221


def test_rows_are_sorted_even_if_the_source_reverses_them():
    """정렬이 뒤집히면 '3일 연속'이 거꾸로 세어진다. 순서를 가정하지 않는다."""
    reversed_rows = "\n".join(reversed(MCP_TREND_RESPONSE.splitlines()))

    flows = _parse_investor_flows(reversed_rows)

    assert [flow.date for flow in flows] == ["20260818", "20260817", "20260814"]


def test_an_unparseable_response_yields_nothing():
    """다른 서비스의 출력 형식에 기대는 파싱이라 깨질 수 있다. 그때는 조용히 비운다."""
    assert _parse_investor_flows("수급 응답") == []


def test_a_row_with_a_missing_value_is_skipped_not_zeroed():
    """없는 데이터를 0으로 채우면 '순매수도 순매도도 아님'이라고 지어내는 것이 된다 (#162)."""
    raw = MCP_TREND_RESPONSE.replace("개인: -88,412", "개인: -")

    assert [flow.date for flow in _parse_investor_flows(raw)] == ["20260818", "20260814"]


# ---- 요약 ----


def test_summary_shows_direction_and_one_day_vertically():
    assert _format_trend_summary("삼성전자", _parse_investor_flows(MCP_TREND_RESPONSE)) == "\n".join(
        [
            "[삼성전자] 투자자 매매동향",
            "외국인 3일 연속 순매수",
            "",
            "08/18",
            "- 개인 -41만주",
            "- 외국인 +124만주",
            "- 기관 -83만주",
        ]
    )


def test_a_run_is_counted_only_while_the_direction_holds():
    """방향이 한 번 끊기면 거기서 센다. 계산일 뿐 판단이 아니다."""
    flows = _parse_investor_flows(MCP_TREND_RESPONSE)
    assert _flow_streak(flows, "foreign") == 3

    broken = _parse_investor_flows(
        MCP_TREND_RESPONSE.replace("외국인: 302,118", "외국인: -302,118")
    )
    assert _flow_streak(broken, "foreign") == 1


def test_the_headline_picks_the_longest_run():
    """외국인 흐름이 끊기면 3일 내리 순매도한 기관이 머리줄을 가져간다."""
    raw = MCP_TREND_RESPONSE.replace("외국인: 302,118", "외국인: -302,118")

    assert _format_trend_summary("삼성전자", _parse_investor_flows(raw)).splitlines()[1] == "기관 3일 연속 순매도"


def test_summary_rounds_but_detail_does_not():
    summary = _format_trend_summary("삼성전자", _parse_investor_flows(MCP_TREND_RESPONSE))
    detail = _format_trend_detail("삼성전자", _parse_investor_flows(MCP_TREND_RESPONSE))

    assert "-41만주" in summary
    assert "-410,123" not in summary
    # 정확한 값이 필요한 사람은 버튼을 누른 사람이다.
    assert "-410,123" in detail
    assert "만주" not in detail


def test_detail_lists_every_day():
    detail = _format_trend_detail("삼성전자", _parse_investor_flows(MCP_TREND_RESPONSE))

    assert detail.splitlines()[0] == "[삼성전자] 투자자 매매동향 3일 상세"
    assert detail.count("- 외국인 ") == 3
    for date in ("08/18", "08/17", "08/14"):
        assert date in detail


def test_every_value_line_carries_the_list_marker():
    """접힌 뒤에도 항목 경계가 남으려면 값 줄이 전부 표시로 시작해야 한다."""
    for text in (
        _format_trend_summary("삼성전자", _parse_investor_flows(MCP_TREND_RESPONSE)),
        _format_trend_detail("삼성전자", _parse_investor_flows(MCP_TREND_RESPONSE)),
    ):
        for line in text.splitlines():
            # 값이 실린 줄만 본다. 머리줄("외국인 3일 연속 순매수")에는 부호 붙은 수가 없다.
            if re.search(r"[+-][\d,]+", line):
                assert line.startswith("- "), line


# ---- 버튼 ----


@pytest.mark.asyncio
async def test_trend_offers_a_detail_button_first():
    notifier = FakeNotifier()
    handler, _ = _handler(notifier)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/trend 삼성전자"}}
    )

    rows = notifier.reply_markups[-1]["inline_keyboard"]
    assert rows[0][0]["text"] == trend_detail_button_text(3)
    assert rows[0][0]["callback_data"].startswith(f"{MARKET_TREND_DETAIL_CALLBACK}:")
    assert notifier.messages[-1].startswith("[삼성전자] 투자자 매매동향")


@pytest.mark.asyncio
async def test_the_detail_button_sends_the_full_table_as_a_new_message():
    notifier = FakeNotifier()
    handler, calls = _handler(notifier)

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/trend 삼성전자"}}
    )
    await handler.handle_update(
        {
            "callback_query": {
                "id": "detail",
                "data": notifier.reply_markups[-1]["inline_keyboard"][0][0]["callback_data"],
                "message": {"chat": {"id": 123}},
            }
        }
    )

    # 같은 조회를 다시 한다. 원문을 들고 있으려면 만료·용량을 관리할 저장소가 하나 더 는다.
    assert [call[1] for call in calls] == ["get_investor_trading"] * 2
    assert notifier.callback_answers == [("detail", None)]
    assert len(notifier.messages) == 2
    assert notifier.messages[-1].startswith("[삼성전자] 투자자 매매동향 3일 상세")
    # 상세 아래에 상세 버튼을 또 두지 않는다.
    detail_rows = notifier.reply_markups[-1]["inline_keyboard"]
    assert all(
        trend_detail_button_text(3) != button["text"]
        for row in detail_rows
        for button in row
    )


@pytest.mark.asyncio
async def test_an_unparseable_response_still_reaches_the_user():
    """형식이 바뀌면 요약이 사라질 뿐 조회 자체는 계속 된다."""
    notifier = FakeNotifier()
    handler, _ = _handler(notifier, response="알 수 없는 형식의 응답")

    await handler.handle_update(
        {"message": {"chat": {"id": 123}, "text": "/trend 삼성전자"}}
    )

    assert notifier.messages[-1].startswith("알 수 없는 형식의 응답")
