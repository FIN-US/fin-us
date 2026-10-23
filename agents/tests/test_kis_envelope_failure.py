"""#446: Kis Trading MCP가 성공 모양으로 감싼 실패가 원장에 실패로 남고 에이전트에 오류로 간다.

서버(open-trading-api ``MCP/Kis Trading MCP/tools/base.py``)는 실패를 ``isError``로 올리지 않고
``{"ok": …, "data": …}`` 정상 응답으로 돌려준다. 특히 KIS가 rt_cd≠0으로 거절해도 예제 프로세스가 오류를
출력하고 정상 종료하면 ``success: true``이고, 거절 사유는 ``data`` 텍스트 안에만 있다. 원장은 ``{"error"``
접두어만 실패로 읽으므로 이런 응답이 ``ok=True``·``produced_rows=True``로 남아 게이트가 수치 답변을
통과시켰다.

봉투 문자열은 실측 응답 그대로다(PR #445 실측 C5의 등락률 순위 OPSQ2002, #435 실측의 EGW00201,
같은 실측의 인자 누락 ``TypeError``). 원격 MCP 호출만 대역으로 바꾸고, 호출은 NAT가 부르는 그대로
조회 전용 래퍼의 ``single_fn``으로 한다 — 원장 기록·마스킹까지 프로덕션 경로를 지난다.
"""

from __future__ import annotations

import json

import pytest
from nat.data_models.api_server import ChatRequest, Message, UserMessageContentRoleType

from finus_agents import finus_api
from finus_agents.agents import _check_tool_enforcement
from finus_agents.finus_api import DATA_TOOL_LEDGER, DataToolLedger
from finus_agents.pii_guard import PII_MAPPING, install_mapping_box

_FLUCTUATION_PARAMS = {
    "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20170", "fid_input_iscd": "0000",
    "fid_rank_sort_cls_code": "0000", "fid_input_cnt_1": "20", "fid_prc_cls_code": "0",
    "fid_input_price_1": "10000", "fid_input_price_2": "", "fid_vol_cnt": "0",
    "fid_trgt_cls_code": "000000000", "fid_trgt_exls_cls_code": "0000000000", "fid_div_cls_code": "0",
    "fid_rsfl_rate1": "", "fid_rsfl_rate2": "",
}


def _envelope(inner: dict) -> str:
    """서버가 돌려주는 봉투. 메타데이터 키는 실응답과 같다."""
    base = {"message": "domestic_stock API 호출 완료", "execution_time": "1.15s",
            "temp_dir": "./tmp/1792732264907192_c12e3733dd25464ba7b20d600ccb0efd", "venv_used": True,
            "cleanup_success": True}
    return json.dumps({"ok": True, "data": {**inner, **base}}, ensure_ascii=False, separators=(",", ":"))


# PR #445 실측 C5: 모델이 find_api_detail 설명대로 "0000"을 넣은 호출.
_OPSQ2002 = _envelope({
    "success": True, "api_type": "fluctuation", "params": {**_FLUCTUATION_PARAMS, "env_dv": "demo"},
    "data": (
        "-------------------------------\nError in response:  200  url= /uapi/domestic-stock/v1/ranking/fluctuation\n"
        "rt_cd :  2 / msg_cd :  OPSQ2002 / msg1 :  ERROR INVALID INPUT_FILED_SIZE [FID_RANK_SORT_CLS_CODE] [4]\n"
        "-------------------------------\nEmpty DataFrame\nColumns: []\nIndex: []\n"
    ),
})
# #435 실측: 초당 거래건수 초과. kis_auth APIRespError.printError 형식이고 재시도마다 한 줄씩 쌓인다.
_EGW00201_LINE = ('Error Code : 500 | {"rt_cd":"1","msg1":"초당 거래건수를 초과하였습니다.",'
                  '"msg_cd":"EGW00201","message":"EGW00201"}')
_EGW00201 = _envelope({
    "success": True, "api_type": "inquire_price",
    "params": {"fid_cond_mrkt_div_code": "J", "env_dv": "demo", "fid_input_iscd": "000660"},
    "data": f"{_EGW00201_LINE}\n{_EGW00201_LINE}\nEmpty DataFrame\nColumns: []\nIndex: []\n",
})
# PR #445 실측 C5 첫 호출: 필수 인자를 빼먹어 예제 함수가 TypeError로 끝났다.
_TYPE_ERROR = _envelope({
    "success": False, "api_type": "fluctuation", "params": {"fid_input_price_1": "10000", "env_dv": "demo"},
    "error": ("TypeError: fluctuation() missing 13 required positional arguments: 'fid_cond_mrkt_div_code'\n\n"
              "해결방법: find_api_detail로 API 상세 정보를 확인하세요\n"),
})
# tools/base.py가 설정에 없는 api_type에 돌려주는 응답.
_UNSUPPORTED = json.dumps({"ok": False, "error": "지원하지 않는 API 타입: fluctuation_v2"}, ensure_ascii=False)

# 2026-10-23 모의투자 실측: fid_rank_sort_cls_code="0"이면 행이 온다(앞 한 행만 남겼다).
_ROWS = _envelope({
    "success": True, "api_type": "fluctuation",
    "params": {**_FLUCTUATION_PARAMS, "fid_rank_sort_cls_code": "0", "env_dv": "demo"},
    "data": ('The End\n[{"stck_shrn_iscd":"051630","data_rank":"1","hts_kor_isnm":"진양화학",'
             '"stck_prpr":"2360","prdy_vrss":"542","prdy_vrss_sign":"1","prdy_ctrt":"29.81"}]\n'),
})


@pytest.fixture
def ledger():
    led = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(led)
    try:
        yield led
    finally:
        DATA_TOOL_LEDGER.reset(token)


@pytest.fixture
def box():
    token = PII_MAPPING.set(install_mapping_box())
    try:
        yield
    finally:
        PII_MAPPING.reset(token)


@pytest.fixture
def remote(monkeypatch):
    """원격 MCP 호출을 대역으로 바꾼다. ``replies[api_type]``를 돌려주고 받은 인자를 남긴다."""
    state: dict = {"replies": {}, "calls": []}

    async def fake_remote(**kwargs):
        state["calls"].append(kwargs["arguments"])
        return state["replies"][kwargs["arguments"]["api_type"]]

    monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake_remote)
    monkeypatch.setenv("FINUS_SKIP_MCP_LIST_TOOLS", "1")
    return state


async def _call(api_type: str, params: dict) -> str:
    config = finus_api.FinusAccountBalanceReadonlyConfig(
        mcp_url="http://kis-mcp.invalid/sse", trading_tool_name="domestic_stock"
    )
    async with finus_api.finus_account_balance_readonly(config, None) as info:
        return await info.single_fn(
            finus_api.KisTradingMcpCallInput(tool_name="domestic_stock", api_type=api_type, params=params)
        )


@pytest.mark.parametrize(("api_type", "reply", "reason", "msg_cd"), [
    pytest.param("fluctuation", _OPSQ2002, "kis_rejected", "OPSQ2002", id="rt_cd-rejected-OPSQ2002"),
    pytest.param("inquire_price", _EGW00201, "kis_rejected", "EGW00201", id="http-error-EGW00201"),
    pytest.param("fluctuation", _TYPE_ERROR, "call_failed", None, id="success-false-TypeError"),
    pytest.param("fluctuation", _UNSUPPORTED, "mcp_rejected", None, id="ok-false"),
])
async def test_failure_inside_the_envelope_is_recorded_as_failure(remote, ledger, box, api_type, reply, reason, msg_cd):
    """봉투 안의 실패 → 원장 ``ok=False``, Observation은 사유를 담은 오류 JSON.

    뮤테이션: ``_call_kis_mcp_and_record``가 ``_kis_observation``을 거치지 않게 되돌리면 원장이
    ``ok=True``·``produced_rows=True``가 되어 네 파라미터 모두 실패한다(측정해 확인).
    """
    remote["replies"][api_type] = reply
    params = _FLUCTUATION_PARAMS if api_type == "fluctuation" else {"fid_input_iscd": "000660"}

    observation = await _call(api_type, params)

    assert [(r.ok, r.produced_rows) for r in ledger.records] == [(False, False)]
    assert not ledger.any_success()
    payload = json.loads(observation)
    assert (payload["error"], payload["api_type"], payload["reason"]) == (finus_api._KIS_API_FAILED, api_type, reason)
    assert payload.get("msg_cd") == msg_cd
    # 에이전트가 고칠 수 있도록 find_api_detail로 가는 길과 서버가 준 사유가 함께 간다.
    assert "find_api_detail" in payload["hint"]
    assert payload["detail"]


async def test_kis_rejection_reason_reaches_the_agent(remote, ledger, box):
    """KIS 거절 사유(필드 이름·크기)와 에이전트가 보낸 값이 Observation에 그대로 남는다."""
    remote["replies"]["fluctuation"] = _OPSQ2002

    payload = json.loads(await _call("fluctuation", _FLUCTUATION_PARAMS))

    assert payload["rt_cd"] == "2"
    assert payload["msg1"] == "ERROR INVALID INPUT_FILED_SIZE [FID_RANK_SORT_CLS_CODE] [4]"
    assert payload["detail"]["params"]["fid_rank_sort_cls_code"] == "0000"
    # 서버 메타데이터(임시 디렉터리 등)는 사유와 무관해 뺀다.
    assert "temp_dir" not in payload["detail"]


async def test_rows_stay_a_success_and_unchanged(remote, ledger, box):
    """대조군: 행이 온 응답은 종전 그대로 성공이고 본문도 바뀌지 않는다.

    뮤테이션: 봉투가 있으면 무조건 실패로 바꾸게 하면 이 테스트가 실패한다 — 위 테스트만으로는
    "무조건 실패" 구현을 거르지 못한다.
    """
    remote["replies"]["fluctuation"] = _ROWS

    observation = await _call("fluctuation", {**_FLUCTUATION_PARAMS, "fid_rank_sort_cls_code": "0"})

    assert observation == _ROWS
    assert [(r.ok, r.produced_rows) for r in ledger.records] == [(True, True)]


def _req(text: str) -> ChatRequest:
    return ChatRequest(messages=[Message(role=UserMessageContentRoleType("user"), content=text)])


@pytest.mark.parametrize(("reply", "trips"), [(_OPSQ2002, True), (_ROWS, False)], ids=["rejected", "rows"])
async def test_gate_blocks_numbers_when_the_only_ranking_call_was_rejected(remote, ledger, box, reply, trips):
    """이슈의 경로: 등락률 순위가 KIS에 거절된 턴에서 지어낸 수치 답변은 게이트에 걸린다.

    뮤테이션: 정규화를 되돌리면 거절 응답이 성공으로 기록되어 게이트가 통과시킨다(``trips=True`` 쪽 red).
    """
    remote["replies"]["fluctuation"] = reply
    await _call("fluctuation", _FLUCTUATION_PARAMS)

    answer = "Final Answer: 오늘 많이 오른 1만원 이상 종목은 알멕(71,700원, +1.27%)입니다."
    assert _check_tool_enforcement(answer, ledger, _req("오늘 많이 오른 종목 중 1만원 이상 추천해줘")) is trips


async def test_rejected_account_tr_does_not_leak_the_account_number(remote, ledger, box):
    """계좌 TR의 거절도 오류 JSON으로 바뀐 뒤 마스킹된다 — detail에 되비친 계좌번호가 남지 않는다."""
    account = "73519024"
    remote["replies"]["inquire_balance"] = _envelope({
        "success": True, "api_type": "inquire_balance", "params": {"cano": account, "acnt_prdt_cd": "01"},
        "data": ("Error in response:  200  url= /uapi/domestic-stock/v1/trading/inquire-balance\n"
                 f"rt_cd :  1 / msg_cd :  OPSQ0002 / msg1 :  계좌 {account} 확인\n"),
    })

    observation = await _call("inquire_balance", {})

    assert json.loads(observation)["reason"] == "kis_rejected"
    assert account not in observation


# ---- find_api_detail 설명 정정 ----

def _api_detail(rank_sort_description: str) -> str:
    """서버의 ``find_api_detail`` 응답(configs/domestic_stock.json의 fluctuation 항목에서 두 필드만)."""
    return json.dumps({"ok": True, "data": {
        "tool_name": "domestic_stock", "api_type": "fluctuation", "name": "국내주식 등락률 순위",
        "params": {
            "fid_rank_sort_cls_code": {"name": "fid_rank_sort_cls_code", "type": "str", "required": True,
                                       "default_value": None, "description": rank_sort_description},
            "fid_prc_cls_code": {"name": "fid_prc_cls_code", "type": "str", "required": True,
                                 "default_value": None, "description": "가격 구분 코드 (0: 전체)"},
        },
    }}, ensure_ascii=False)


async def test_find_api_detail_corrects_the_upstream_rank_sort_description(remote, ledger, box):
    """upstream 설명 "(0000: 등락률순)"을 KIS 규격(한 자리, 0:상승율순 …)으로 고쳐 돌려준다.

    뮤테이션: 정정 표에서 fluctuation 항목을 빼면 "0000" 설명이 그대로 가서 red.
    """
    remote["replies"]["find_api_detail"] = _api_detail("순위 정렬 구분 코드 (0000: 등락률순)")

    observation = await _call("find_api_detail", {"api_type": "fluctuation"})

    params = json.loads(observation)["data"]["params"]
    described = params["fid_rank_sort_cls_code"]["description"]
    assert "0000: 등락률순" not in described
    assert "0:상승율순 1:하락율순" in described
    # 다른 필드는 손대지 않는다.
    assert params["fid_prc_cls_code"]["description"] == "가격 구분 코드 (0: 전체)"
    assert [(r.ok, r.produced_rows) for r in ledger.records] == [(True, True)]


async def test_find_api_detail_of_other_trs_is_unchanged(remote, ledger, box):
    """정정 표에 없는 TR의 설명은 원문 그대로다."""
    reply = _api_detail("순위 정렬 구분 코드").replace('"api_type": "fluctuation"', '"api_type": "news_title"')
    remote["replies"]["find_api_detail"] = reply

    assert await _call("find_api_detail", {"api_type": "news_title"}) == reply
