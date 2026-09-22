"""#405: 매매일지 조회 묶음 도구(``finus_mcp_trading_diary_snapshot``).

diary_agent의 첫 턴 강제(#399)는 첫 호출 하나만 보장한다. 조회가 도구 세 개로 나뉘어 있을 때는
당일 주문만 조회한 뒤 모델이 잔고 조회로 넘어가지 않아, 거래가 없는 날마다 빈 초안이 됐다.
묶음 도구는 필요한 조회를 코드에서 모두 부른다. 여기서는 그 계약을 고정한다.

- 하위 조회 세 개를 모두, 정해진 인자로 부른다(조회 전용 MCP 도구만).
- 원장에는 하위 조회가 한 건씩 남는다. 일부 실패면 성공한 것만 ``produced_rows``, 전부 실패면
  전부 ``ok=False``이고 Observation도 오류 JSON이다(#406과 같은 모양). 게이트 판정도 그에 따른다.
- 계좌 데이터는 하위 도구명의 ``MASKED_TOOLS`` 등록으로 마스킹된 채 Observation에 들어간다.

MCP 전송은 ``_mcp_trading_call``에서 끊는다. ``isError`` → 오류 JSON 변환은 #406의
``test_mcp_is_error.py``가 진짜 MCP 세션으로 고정하므로, 여기서는 그 변환 결과(오류 JSON 문자열)를
하위 조회의 반환값으로 준다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nat.data_models.api_server import ChatRequest, Message, UserMessageContentRoleType

from nat_finus_nat import finus_api
from nat_finus_nat.agents import _check_tool_enforcement
from nat_finus_nat.finus_api import DATA_TOOL_LEDGER, DataToolLedger
from nat_finus_nat.pii_guard import MASKED_TOOLS, PII_MAPPING, install_mapping_box, unmask_response

_ORDERS_EMPTY = "[당일 주문·체결 내역] 20260922\n- 결과: 해당 조건의 주문·체결이 없습니다."
_ORDERS = "[당일 주문·체결 내역] 20260922\n1. 삼성전자 매수 10주 체결 700,000원"
_BALANCE = "[계좌 요약]\n- 예수금: 1,234,000원\n- 총평가금액: 10,000,000원\n\n[보유 종목]\n- 삼성전자 10주"
_RLZ_PL = "[보유 종목]\n- 삼성전자 10주\n\n[계좌 집계]\n- 실현손익: 12,000원"


def _mcp_error(tool: str) -> str:
    """#406이 ``isError`` 응답을 바꿔 돌려주는 모양 그대로."""
    return finus_api._err_json(finus_api._MCP_TOOL_ERROR, tool=tool, detail="에러 발생: KIS API 호출 실패")


_SUB_LEDGER_NAMES = (
    "finus_mcp_trading_today_orders",
    "finus_mcp_trading_get_balance",
    "finus_mcp_trading_balance_rlz_pl",
)


@pytest.fixture
def mcp_trading(monkeypatch):
    """``_mcp_trading_call`` 대역. ``responses``를 MCP 도구명별로 채우고 ``calls``를 읽는다."""

    class _Fake:
        def __init__(self) -> None:
            self.responses: dict[str, str] = {
                "get_today_daily_orders": _ORDERS,
                "get_balance": _BALANCE,
                "get_balance_rlz_pl": _RLZ_PL,
            }
            self.calls: list[tuple[str, dict]] = []

        async def __call__(self, **kwargs) -> str:
            self.calls.append((kwargs["tool_name"], dict(kwargs["arguments"])))
            return self.responses[kwargs["tool_name"]]

    fake = _Fake()
    monkeypatch.setattr(finus_api, "_mcp_trading_call", fake)
    return fake


@pytest.fixture
def ledger():
    led = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(led)
    try:
        yield led
    finally:
        DATA_TOOL_LEDGER.reset(token)


@pytest.fixture
def mapping_box():
    box = install_mapping_box()
    token = PII_MAPPING.set(box)
    try:
        yield box
    finally:
        PII_MAPPING.reset(token)


async def _snapshot(**inputs) -> str:
    config = finus_api.FinusMcpTradingDiarySnapshotConfig(timeout_sec=10.0)
    async with finus_api.finus_mcp_trading_diary_snapshot(config, None) as info:
        return await info.single_fn(info.input_schema(**inputs))


def _req(text: str) -> ChatRequest:
    return ChatRequest(messages=[Message(role=UserMessageContentRoleType("user"), content=text)])


def _records(ledger: DataToolLedger) -> list[tuple[str, bool, bool, bool]]:
    return [(r.tool_name, r.ok, r.produced_rows, r.empty) for r in ledger.records]


async def test_calls_every_read_only_sub_query_in_order(mcp_trading, ledger):
    """세 조회를 모두 부르고, 부르는 MCP 도구는 조회 도구 세 개뿐이다.

    실전 계좌 응답(모의투자 대체 표지 없음)에서는 실현손익 뒤에 잔고를 따로 조회한다(#408).
    원장 기록 순서는 호출 순서가 바뀐 뒤에도 전과 같다.

    뮤테이션: 잔고 조회(``get_balance``) 줄을 지우면 red — 이 도구가 고치려는 누락 그 자체다.
    """
    await _snapshot()

    assert mcp_trading.calls == [
        ("get_today_daily_orders", {}),
        ("get_balance_rlz_pl", {}),
        ("get_balance", {}),
    ]
    assert [r.tool_name for r in ledger.records] == list(_SUB_LEDGER_NAMES)


async def test_passes_date_and_stock_filters_to_the_sub_queries_that_take_them(mcp_trading, ledger):
    """날짜는 주문·체결에, 종목은 주문·체결·실현손익에 넘긴다. 잔고 조회는 인자가 없다."""
    await _snapshot(trade_date="20260921", stock_name="삼성전자")

    assert mcp_trading.calls == [
        ("get_today_daily_orders", {"trade_date": "20260921", "stock_name": "삼성전자"}),
        ("get_balance_rlz_pl", {"stock_name": "삼성전자"}),
        ("get_balance", {}),
    ]


async def test_no_orders_today_still_brings_the_balance(mcp_trading, ledger):
    """이슈의 시나리오: 당일 주문이 없어도 한 번의 호출로 잔고 데이터가 들어온다.

    원장에는 당일 주문이 빈 결과로, 잔고가 데이터로 남는다. 그래서 게이트는 잔고 수치를 쓴 초안을
    통과시키고, "전부 빈 결과"(``only_empty_reads``) 경로로 초안을 버리지도 않는다.
    """
    mcp_trading.responses["get_today_daily_orders"] = _ORDERS_EMPTY

    observation = await _snapshot()

    assert _records(ledger) == [
        ("finus_mcp_trading_today_orders", True, False, True),
        ("finus_mcp_trading_get_balance", True, True, False),
        ("finus_mcp_trading_balance_rlz_pl", True, True, False),
    ]
    assert ledger.any_success() and not ledger.only_empty_reads()
    assert "해당 조건의 주문·체결이 없습니다." in observation
    assert "[계좌 잔고·보유종목]" in observation
    draft = "Final Answer: 오늘은 거래가 없습니다. 삼성전자 10주 보유, 총평가금액 10,000,000원."
    assert _check_tool_enforcement(draft, ledger, _req("오늘 매매일지 초안 작성해줘")) is False


async def test_partial_failure_keeps_the_rest_and_marks_the_failed_part(mcp_trading, ledger):
    """실현손익만 실패하면 나머지 결과는 그대로 싣고, 실패한 섹션은 #406 오류 JSON을 둔 채 표시한다.

    원장에는 실패한 하위 조회만 ``ok=False``로 남고, 성공한 것은 ``produced_rows``다.
    Observation 전체는 오류 JSON이 아니다 — 그렇게 두면 성공한 잔고까지 실패로 읽힌다.

    뮤테이션: 실패 섹션 표시(``— 조회 실패``)를 지우면 red.
    """
    mcp_trading.responses["get_balance_rlz_pl"] = _mcp_error("get_balance_rlz_pl")

    observation = await _snapshot()

    assert _records(ledger) == [
        ("finus_mcp_trading_today_orders", True, True, False),
        ("finus_mcp_trading_get_balance", True, True, False),
        ("finus_mcp_trading_balance_rlz_pl", False, False, False),
    ]
    assert finus_api._ERROR_JSON_PREFIX_RE.match(observation) is None
    assert "조회 실패: 실현손익" in observation
    failed_section = observation.split("[실현손익 — 조회 실패]\n", 1)[1]
    assert json.loads(failed_section)["error"] == finus_api._MCP_TOOL_ERROR
    assert "[계좌 잔고·보유종목]\n" in observation


@pytest.mark.parametrize(
    "balance_response",
    [_BALANCE, "", "   \n", _mcp_error("get_balance"), '{"error": "mcp_timeout", "tool": "get_balance"}'],
    ids=["data", "empty", "whitespace", "is_error", "timeout"],
)
async def test_failed_section_marker_agrees_with_the_ledger(mcp_trading, ledger, balance_response):
    """섹션의 ``조회 실패`` 표시와 원장의 ``ok``가 어떤 응답에서도 같은 판정이다 (PR #409 리뷰).

    두 판정이 따로 있으면 한쪽만 바뀌었을 때 원장에는 성공인 섹션이 Observation에는 실패로 찍힌다.

    뮤테이션: ``_format_diary_snapshot``의 판정을 오류 JSON 정규식만 보는 사본으로 바꾸면
    empty·whitespace 케이스가 red.
    """
    mcp_trading.responses["get_balance"] = balance_response

    observation = await _snapshot()

    balance_record = next(r for r in ledger.records if r.tool_name == "finus_mcp_trading_get_balance")
    assert ("[계좌 잔고·보유종목 — 조회 실패]" in observation) is (not balance_record.ok)


async def test_all_failures_are_an_error_observation_and_trip_the_gate(mcp_trading, ledger):
    """전부 실패하면 원장은 전부 ``ok=False``, Observation은 오류 JSON이고, 수치 답변은 게이트에 걸린다.

    단일 조회 도구가 실패했을 때와 같은 모양이라, 오류 JSON 접두어로 실패를 읽는 소비자가 묶음
    도구를 따로 다룰 필요가 없다.

    뮤테이션: 전부 실패 분기를 지워 섹션 텍스트로 돌려주면 red.
    """
    for tool in mcp_trading.responses:
        mcp_trading.responses[tool] = _mcp_error(tool)

    observation = await _snapshot()

    assert _records(ledger) == [(name, False, False, False) for name in _SUB_LEDGER_NAMES]
    payload = json.loads(observation)
    assert payload["error"] == finus_api._DIARY_SNAPSHOT_FAILED
    assert set(payload["failures"]) == {"당일 주문·체결", "계좌 잔고·보유종목", "실현손익"}
    fabricated = "Final Answer: 삼성전자 10주 보유, 총평가금액 10,000,000원입니다."
    assert _check_tool_enforcement(fabricated, ledger, _req("오늘 매매일지 초안 작성해줘")) is True


async def test_account_data_is_masked_before_it_reaches_the_agent(mcp_trading, ledger, mapping_box):
    """계좌 금액은 자리표시자로 바뀐 채 Observation에 들어가고, 응답 경계에서 원값으로 돌아온다.

    하위 결과는 각자의 원장 도구명으로 ``_record_and_mask``를 지나므로 ``MASKED_TOOLS`` 등록을 탄다.

    뮤테이션: 잔고 섹션에 ``_record_and_mask`` 반환값 대신 원문을 실으면 red.
    """
    assert set(_SUB_LEDGER_NAMES) <= MASKED_TOOLS

    observation = await _snapshot()

    for raw in ("1,234,000원", "10,000,000원", "700,000원", "12,000원"):
        assert raw not in observation
    assert mapping_box, "마스킹 매핑이 박스에 쌓이지 않았다"
    assert "1,234,000원" in unmask_response(observation)


# ---------------------------------------------------------------------------
# #408: 모의투자에서 실현손익 응답의 잔고를 재사용해 잔고 TR을 한 번만 보낸다
# ---------------------------------------------------------------------------

_MCP_TRADING_FIXTURES = Path(__file__).resolve().parents[2] / "mcp-trading" / "tests" / "fixtures"
# 모의투자 대체 응답 계약. mcp-trading(PAPER_RLZ_PL_FALLBACK_NOTE)·backend(_RLZ_PL_PAPER_FALLBACK_MARKER)
# 스위트가 같은 파일을 읽는다.
_PAPER_CONTRACT = json.loads((_MCP_TRADING_FIXTURES / "paper_rlz_pl_fallback.json").read_text(encoding="utf-8"))
# 실제 get_balance 출력(balance.js formatBalanceReport, #137 공유 픽스처).
_BALANCE_FIXTURE = json.loads((_MCP_TRADING_FIXTURES / "balance_report.json").read_text(encoding="utf-8"))
_REAL_BALANCE = _BALANCE_FIXTURE["normal"]["expected_text"]
_REAL_BALANCE_EMPTY = _REAL_BALANCE.split("[보유 종목 리스트]")[0] + "[보유 종목 리스트]\n보유 종목이 없습니다."


def _paper_rlz_pl(balance_text: str) -> str:
    """mcp-trading ``getBalanceRlzPl`` 모의투자 분기가 돌려주는 모양: ``${balanceText}${note}``."""
    return balance_text + _PAPER_CONTRACT["note"]


def test_marker_matches_the_shared_paper_fallback_contract():
    """NAT 표지가 mcp-trading 대체 안내 문구·backend 표지와 같은 계약이다.

    뮤테이션: ``_PAPER_RLZ_PL_FALLBACK_MARKER``나 ``_PAPER_RLZ_PL_NOTE_PREFIX`` 문구를 바꾸면 red.
    """
    assert finus_api._PAPER_RLZ_PL_FALLBACK_MARKER == _PAPER_CONTRACT["marker"]
    assert _PAPER_CONTRACT["note"].startswith(finus_api._PAPER_RLZ_PL_NOTE_PREFIX)


async def test_paper_fallback_reuses_the_balance_and_skips_the_balance_call(mcp_trading, ledger, mapping_box):
    """모의투자 대체 응답이면 잔고 조회를 부르지 않고, 그 응답의 잔고를 잔고 섹션으로 싣는다.

    잔고 텍스트는 한 번만 실린다(이전에는 잔고 섹션과 실현손익 섹션에 두 번). 원장은 전과 같은 세 건이다.

    뮤테이션: 재사용 분기를 지워 항상 ``get_balance``를 부르면 호출 단언이 red, 실현손익 섹션을
    원문으로 되돌리면 잔고 1회 단언이 red.
    """
    mcp_trading.responses["get_today_daily_orders"] = _ORDERS_EMPTY
    mcp_trading.responses["get_balance_rlz_pl"] = _paper_rlz_pl(_REAL_BALANCE)

    observation = await _snapshot()

    assert [name for name, _ in mcp_trading.calls] == ["get_today_daily_orders", "get_balance_rlz_pl"]
    assert _records(ledger) == [
        ("finus_mcp_trading_today_orders", True, False, True),
        ("finus_mcp_trading_get_balance", True, True, False),
        ("finus_mcp_trading_balance_rlz_pl", True, True, False),
    ]
    balance_section = observation.split("[계좌 잔고·보유종목]\n", 1)[1].split("\n\n[실현손익]", 1)[0]
    assert unmask_response(balance_section) == _REAL_BALANCE
    assert observation.count("[계좌 잔고 현황]") == 1
    rlz_section = observation.split("[실현손익]\n", 1)[1]
    assert _PAPER_CONTRACT["marker"] in rlz_section
    assert "조회 실패" not in observation


async def test_paper_fallback_keeps_the_balance_truncation_note_in_the_balance_section(
    mcp_trading, ledger, mapping_box
):
    """잔고 자체의 잘림 안내(``[안내]``)는 잔고 섹션에 남는다 — 대체 안내 문단만 떼어 낸다.

    뮤테이션: ``rfind``를 ``find``(첫 ``[안내]``)로 바꾸면 red.
    """
    truncated = _BALANCE_FIXTURE["truncated"]["expected_text"]
    mcp_trading.responses["get_balance_rlz_pl"] = _paper_rlz_pl(truncated)

    observation = await _snapshot()

    balance_section = observation.split("[계좌 잔고·보유종목]\n", 1)[1].split("\n\n[실현손익]", 1)[0]
    assert unmask_response(balance_section) == truncated


async def test_paper_fallback_without_note_boundary_uses_the_whole_response_as_balance(
    mcp_trading, ledger, mapping_box
):
    """표지는 있는데 ``\\n\\n[안내]`` 경계가 없으면 응답 전체를 잔고 섹션으로 쓴다 (PR #411 리뷰).

    계약상(공유 픽스처의 note) 도달하지 않는 방어 분기다. 경계를 못 찾았다고 잔고 내용을 잘라
    버리거나 잔고 조회를 다시 부르지 않는다.

    뮤테이션: 경계 없음 분기를 ``return None``으로 바꾸면 red(잔고를 다시 조회한다), 표지 앞에서
    자르게 바꾸면 red(잔고 섹션이 원문과 달라진다).
    """
    raw = _REAL_BALANCE + "\n(모의투자 계좌라 " + _PAPER_CONTRACT["marker"] + ")"
    mcp_trading.responses["get_balance_rlz_pl"] = raw

    observation = await _snapshot()

    assert [name for name, _ in mcp_trading.calls] == ["get_today_daily_orders", "get_balance_rlz_pl"]
    balance_section = observation.split("[계좌 잔고·보유종목]\n", 1)[1].split("\n\n[실현손익]", 1)[0]
    assert unmask_response(balance_section) == raw
    assert "조회 실패" not in observation


async def test_paper_account_without_holdings_or_orders_still_passes_the_gate(mcp_trading, ledger):
    """보유종목·당일 주문이 모두 없는 모의투자 계좌에서도 잔고 수치 초안이 게이트를 지난다.

    재사용한 잔고를 원장에 ``finus_mcp_trading_get_balance``로 남기는 이유다. 실현손익 기록만 남기면
    그 텍스트는 "보유 종목이 없습니다."에 ``[계좌 집계]``가 없어 빈 결과로 잡히고, 당일 주문도
    빈 결과라 ``only_empty_reads``가 된다 — 예수금이 있는데도 초안이 막힌다.

    뮤테이션: 재사용 분기에서 잔고 기록을 빼면 red.
    """
    mcp_trading.responses["get_today_daily_orders"] = _ORDERS_EMPTY
    mcp_trading.responses["get_balance_rlz_pl"] = _paper_rlz_pl(_REAL_BALANCE_EMPTY)

    await _snapshot()

    assert _records(ledger) == [
        ("finus_mcp_trading_today_orders", True, False, True),
        ("finus_mcp_trading_get_balance", True, True, False),
        ("finus_mcp_trading_balance_rlz_pl", True, False, True),
    ]
    assert ledger.any_success() and not ledger.only_empty_reads()
    draft = "Final Answer: 오늘은 거래가 없습니다. 보유 종목은 없고 예수금은 1,000,000원입니다."
    assert _check_tool_enforcement(draft, ledger, _req("오늘 매매일지 초안 작성해줘")) is False


@pytest.mark.parametrize(
    "rlz_response",
    [
        _mcp_error("get_balance_rlz_pl"),
        # 표지가 오류 detail에 섞여 와도 대체 응답이 아니다 — 잔고로 쓰지 않는다.
        finus_api._err_json(finus_api._MCP_TOOL_ERROR, tool="get_balance_rlz_pl", detail=_paper_rlz_pl(_REAL_BALANCE)),
        '{"error": "mcp_timeout", "tool": "get_balance_rlz_pl"}',
        "",
    ],
    ids=["is_error", "marker_inside_error", "timeout", "empty"],
)
async def test_failed_rlz_pl_falls_back_to_a_separate_balance_call(mcp_trading, ledger, rlz_response):
    """실현손익이 실패하면 잔고를 따로 조회해 잔고 섹션을 채운다. 실현손익만 실패로 남는다.

    뮤테이션: ``_split_paper_rlz_pl_fallback``의 성공 판정(``_is_ok_tool_result``)을 지우면
    marker_inside_error가 red(오류 JSON을 잔고로 싣고 잔고 조회를 건너뛴다).
    """
    mcp_trading.responses["get_balance_rlz_pl"] = rlz_response

    observation = await _snapshot()

    assert [name for name, _ in mcp_trading.calls] == [
        "get_today_daily_orders",
        "get_balance_rlz_pl",
        "get_balance",
    ]
    assert _records(ledger)[1] == ("finus_mcp_trading_get_balance", True, True, False)
    assert _records(ledger)[2][:2] == ("finus_mcp_trading_balance_rlz_pl", False)
    assert "[계좌 잔고·보유종목]\n" in observation
    assert "조회 실패: 실현손익" in observation


async def test_reused_balance_is_masked(mcp_trading, ledger, mapping_box):
    """재사용한 잔고도 잔고 도구명으로 마스킹을 지나 평문 금액이 Observation에 없다.

    뮤테이션: 재사용 분기에서 잔고 섹션에 ``_record_and_mask`` 반환값 대신 원문을 실으면 red.
    """
    mcp_trading.responses["get_balance_rlz_pl"] = _paper_rlz_pl(_REAL_BALANCE)

    observation = await _snapshot()

    for raw in ("1,210,000원", "1,000,000원", "210,000원", "200,500원"):
        assert raw not in observation
    assert "1,000,000원" in unmask_response(observation)
