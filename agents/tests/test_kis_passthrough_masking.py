"""KIS pass-through 응답 마스킹 (#430).

KIS 응답은 영문 키와 단위 없는 숫자(``"dnca_tot_amt": "10000000"``)라 ``mask_pii``의 금액
인식기에 걸리지 않았다. 그래서 잔고 TR 응답이 마스킹 대상 도구를 지나면서도 원문 그대로
외부 LLM으로 나갔다. 반대로 공개 시세 응답에서는 10자리 상장주식수가 계좌번호로 가려졌다.

픽스처:
- ``kis_trading_mcp_inquire_balance/synthetic_with_holding.json`` — 봉투와 ``output2`` 키는
  모의 계좌 실응답(2026-10-19) 그대로이고 값은 가짜다. 모의 계좌에 보유 종목이 없어
  ``output1`` 행은 KIS inquire-balance 문서의 필드로 채웠다.
- ``kis_trading_mcp_inquire_price/005930_listed.json`` — 시세 실응답(#381).
"""

import json
import re
from pathlib import Path

import pytest

from finus_agents import finus_api, pii_guard
from finus_agents.pii_guard import (
    KIS_PASSTHROUGH_TOOL,
    PII_MAPPING,
    PUBLIC_KIS_QUOTE_APIS,
    install_mapping_box,
    mask_tool_result,
    unmask_response,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_BALANCE = (_FIXTURES / "kis_trading_mcp_inquire_balance" / "synthetic_with_holding.json").read_text(
    encoding="utf-8"
)
_QUOTE = (_FIXTURES / "kis_trading_mcp_inquire_price" / "005930_listed.json").read_text(encoding="utf-8")
_BALANCE_API = ("domestic_stock", "inquire_balance")
_QUOTE_API = ("domestic_stock", "inquire_price")


@pytest.fixture
def box():
    mapping = install_mapping_box()
    token = PII_MAPPING.set(mapping)
    try:
        yield mapping
    finally:
        PII_MAPPING.reset(token)


def _inner(observation: str) -> dict:
    return json.loads(json.loads(observation)["data"]["data"])


def _balance_rows():
    inner = json.loads(json.loads(_BALANCE)["data"]["data"])
    return inner["output1"][0], inner["output2"][0]


def _digits_left(observation: str) -> list[str]:
    """자리표시자를 뺀 결과에 남은 2자리 이상 숫자."""
    return re.findall(r"\d{2,}", pii_guard._SCOPED_PLACEHOLDER_RE.sub("", observation))


def test_balance_leaves_no_numbers_but_the_stock_code(box):
    """검사 대상을 구현의 판정 함수로 고르지 않는다 — 결과 본문에 남은 숫자를 직접 센다."""
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, _BALANCE, kis_api=_BALANCE_API)

    assert _digits_left(json.loads(observation)["data"]["data"]) == ["005930"]
    masked_holding, masked_summary = (_inner(observation)["output1"][0], _inner(observation)["output2"][0])
    assert box[masked_summary["dnca_tot_amt"]] == "4,817,263"
    assert masked_holding["hldg_qty"].startswith("<QTY_")
    # 마디 없이 붙은 수량 필드도 수량이다 — 주문 인자로 되돌릴 때 종류 검사를 통과해야 한다.
    assert masked_holding["thdt_buyqty"].startswith("<QTY_")


def test_identifiers_dates_and_zeros_stay_readable(box):
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, _BALANCE, kis_api=_BALANCE_API)

    masked_holding, masked_summary = (_inner(observation)["output1"][0], _inner(observation)["output2"][0])
    assert masked_holding["pdno"] == "005930"
    assert masked_holding["prdt_name"] == "삼성전자"
    assert masked_summary["cma_evlu_amt"] == "0"


def test_echoed_account_number_is_masked_but_request_codes_are_not(box):
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, _BALANCE, kis_api=_BALANCE_API)

    params = json.loads(observation)["data"]["params"]
    assert "73519024" not in observation
    assert params["cano"].startswith("<ACCOUNT_")
    assert params["inqr_dvsn"] == "01"


def test_masked_balance_round_trips_to_the_user(box):
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, _BALANCE, kis_api=_BALANCE_API)
    placeholder = _inner(observation)["output2"][0]["dnca_tot_amt"]

    assert unmask_response(f"예수금은 {placeholder}원입니다.") == "예수금은 4,817,263원입니다."


@pytest.mark.parametrize("kis_api", [None, ("domestic_stock", "some_future_tr"), ("overseas_stock", "inquire_price")])
def test_unknown_or_unlisted_api_is_masked(box, kis_api):
    """목록 밖은 계좌 TR로 본다(fail-closed). 해외 시세도 목록에 넣기 전까지는 가린다."""
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, _BALANCE, kis_api=kis_api)

    assert "4817263" not in observation


def test_public_quote_passes_through_unchanged(box):
    """시세는 원문 그대로다 — 10자리 상장주식수(lstn_stcn)가 계좌번호로 가려지지 않는다."""
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, _QUOTE, kis_api=_QUOTE_API)

    assert observation == _QUOTE
    assert "<ACCOUNT_" not in observation
    assert box == {}


def test_account_execution_history_is_not_a_public_quote():
    """``inquire_ccnl``(체결가 시세)과 ``inquire_daily_ccld``(계좌 체결내역)는 다른 TR이다."""
    assert ("domestic_stock", "inquire_ccnl") in PUBLIC_KIS_QUOTE_APIS
    assert ("domestic_stock", "inquire_daily_ccld") not in PUBLIC_KIS_QUOTE_APIS


def test_numbers_in_error_text_before_json_are_masked(box):
    """KIS 거절 응답은 본문 앞에 오류 텍스트가 붙는다. 그 안의 숫자도 가린다."""
    envelope = json.loads(_BALANCE)
    envelope["data"]["data"] = "Error in response: 200 rt_cd : 1 / dnca 4817263\n" + envelope["data"]["data"]

    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, json.dumps(envelope, ensure_ascii=False), kis_api=_BALANCE_API)

    assert "4817263" not in observation


@pytest.fixture
def remote(monkeypatch):
    replies = {"inquire_balance": _BALANCE, "inquire_price": _QUOTE}

    async def fake_remote(**kwargs):
        return replies[kwargs["arguments"]["api_type"]]

    monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake_remote)


@pytest.mark.parametrize("api_type, raw, masked", [
    ("inquire_balance", _BALANCE, True),
    ("inquire_price", _QUOTE, False),
])
async def test_call_path_passes_the_kis_api_to_masking(box, remote, api_type, raw, masked):
    """KIS 호출이 모두 지나는 _call_kis_mcp_and_record가 (도구, api_type)을 넘긴다."""
    config = finus_api.FinusAccountBalanceReadonlyConfig()
    params = {"fid_input_iscd": "005930"} if api_type == "inquire_price" else {}

    observation = await finus_api._call_kis_mcp_and_record(
        "domestic_stock", {"api_type": api_type, "params": params}, config,
    )

    assert (observation != raw) is masked


# ---- 정상 봉투가 아닌 결과 (PR #432 리뷰) ----

_ACCOUNT_NO = "73519024"
_CASH = "4817263"


@pytest.mark.parametrize("result", [
    # 서버 실패 봉투: 본문 없이 되비친 인자와 stderr만 온다.
    json.dumps({"ok": True, "data": {"success": False, "params": {"cano": _ACCOUNT_NO, "acnt_prdt_cd": "01"},
                                     "error": f"Traceback ... dnca {_CASH}"}}),
    json.dumps({"ok": False, "error": f"API 실행 중 오류: cano={_ACCOUNT_NO} cash {_CASH}"}),
    # 래퍼 isError 경로: 봉투 원문이 detail 문자열에 실린다.
    json.dumps({"error": "mcp_tool_error", "detail": json.dumps(
        {"ok": True, "data": {"params": {"cano": _ACCOUNT_NO}, "data": json.dumps([{"dnca_tot_amt": _CASH}])}})}),
    # JSON이 아닌 결과, 글자에 붙은 숫자
    f"Error: account {_ACCOUNT_NO} KRW{_CASH}",
    # JSON number 값
    json.dumps({"ok": True, "data": {"data": [{"dnca_tot_amt": int(_CASH)}]}}),
    # 단위·공백이 섞인 문자열 값
    json.dumps({"ok": True, "data": {"data": [{"memo": f"{int(_CASH):,} 원", "acct": f"계좌 {_ACCOUNT_NO}"}]}}),
])
def test_non_standard_results_do_not_leak(box, result):
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, result, kis_api=_BALANCE_API)

    assert _ACCOUNT_NO not in observation
    assert _CASH not in observation.replace(",", "")


def test_wrapper_hint_text_stays_readable(box):
    """래퍼가 쓴 안내 문구는 숫자가 있어도 가리지 않는다."""
    hint = "최대 50페이지까지 조회합니다."
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, json.dumps({"error": "x", "hint": hint}, ensure_ascii=False))

    assert json.loads(observation)["hint"] == hint


def test_order_identifiers_keep_their_leading_zeros(box):
    """주문번호·지점번호·종목번호는 번호다. 가리지 않고, 콤마를 넣어 망가뜨리지도 않는다."""
    body = [{"odno": "0000117057", "orgn_odno": "0000117001", "ord_gno_brno": "06010",
             "ovrs_pdno": "00700", "ord_qty": "10", "tot_ccld_amt": "2235000"}]
    result = json.dumps({"ok": True, "data": {"data": json.dumps(body)}})

    row = _inner(mask_tool_result(KIS_PASSTHROUGH_TOOL, result, kis_api=("domestic_stock", "inquire_daily_ccld")))[0]

    assert (row["odno"], row["orgn_odno"], row["ord_gno_brno"], row["ovrs_pdno"]) == (
        "0000117057", "0000117001", "06010", "00700")
    assert box[row["tot_ccld_amt"]] == "2,235,000"


def test_masked_quantity_and_price_restore_into_order_params(box):
    """잔고에서 본 수량·현재가 자리표시자를 주문 인자로 되돌리면 KIS가 받는 숫자가 된다(#338)."""
    observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, _BALANCE, kis_api=_BALANCE_API)
    holding = _inner(observation)["output1"][0]

    restored, rejections = pii_guard.restore_params_for_kis(
        {"PDNO": "005930", "ORD_QTY": holding["thdt_buyqty"], "ORD_UNPR": holding["prpr"]},
        tool_name="domestic_stock", api_type="order_cash",
    )

    assert rejections == []
    assert restored == {"PDNO": "005930", "ORD_QTY": "3", "ORD_UNPR": "223500"}


def test_unknown_number_with_leading_zeros_restores_verbatim(box):
    """모르는 필드라 가리지만, 앞자리가 0인 값은 번호일 수 있어 콤마 없이 원문으로 되돌린다."""
    result = json.dumps({"ok": True, "data": {"data": json.dumps([{"rsvn_ord_seq": "00700"}])}})

    row = _inner(mask_tool_result(KIS_PASSTHROUGH_TOOL, result, kis_api=("domestic_stock", "order_resv_ccnl")))[0]

    assert box[row["rsvn_ord_seq"]] == "00700"
