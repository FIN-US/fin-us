"""순위 조회 가격 범위의 상·하한 자리를 사용자가 말한 방향에 맞춘다.

2026-10-27 12:10 실제 텔레그램 "30만원 이하 반도체 대형주 추천해줘"에서 KIS MCP 로그로 확인한 호출 순서:

1. ``market_cap {"fid_input_price_2":"300000", …}`` — 방향은 맞았지만 필수 인자 5개가 빠져 TypeError.
2. ``find_api_detail market_cap``
3. ``market_cap {"fid_input_price_1":"300000","fid_input_price_2":"", …}`` — 다시 짠 인자에서 상한이
   하한 자리로 뒤바뀌어 30만원 **이상** 종목(SK하이닉스 1,347,000원 1위)을 받았고, 그대로 추천됐다.

backend 헤더(#468)는 실렸고(KIS에 300000이 갔다) 순위 조회도 했다 — 금액 복원·기록 길이·성향 주입이
아니라 가격 범위 **방향**이 뒤바뀐 것이 원인이다. 여기서 고정하는 것:

- 발화 방향 읽기: "이하·미만·까지"=상한, "이상·초과·부터"=하한, 모르면 비운다.
- 자리 맞춤: 상한 금액이 하한 자리에 오면 상한 자리로 옮기고, 맞출 수 없으면 KIS를 부르지 않는다.
- 최상위 에이전트가 방향 상자를 심는다.
- 순위 TR 필수 인자 기본값: 가격 조건만 넣어도 첫 호출이 성공해 인자를 다시 짜는 우회를 타지 않는다.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from nat.data_models.api_server import ChatRequestOrMessage, ChatResponse, Usage

from finus_agents import agents as agents_mod
from finus_agents import finus_api
from finus_agents.agents import FinusReasoningTraceAgentConfig, finus_reasoning_trace_agent
from finus_agents.finus_api import KisTradingMcpCallInput
from finus_agents.pii_guard import (
    INPUT_AMOUNTS_HEADER,
    PII_MAPPING,
    REQUEST_INPUT_AMOUNT_BOUNDS,
    REQUEST_INPUT_AMOUNTS,
    input_amount_bounds,
    install_mapping_box,
    restore_params_for_kis,
)
from finus_agents.pii_mask import mask_pii


def _masked(text: str) -> tuple[str, list[str], dict[str, str]]:
    """backend `llm_chat` 마스킹 결과 — (발화, 발화 순서대로의 금액 자리표시자, 헤더에 실릴 금액)."""
    masked, mapping = mask_pii(text)
    amounts = {ph: v for ph, v in mapping.items() if ph.startswith("<AMOUNT_")}
    return masked, sorted(amounts, key=masked.index), amounts


@pytest.fixture
def boxes():
    """최상위 에이전트가 심는 세 상자. 테스트가 발화로 채운다."""
    amounts: dict[str, str] = {}
    bounds: dict[str, str] = {}
    tokens = (PII_MAPPING.set(install_mapping_box()), REQUEST_INPUT_AMOUNTS.set(amounts),
              REQUEST_INPUT_AMOUNT_BOUNDS.set(bounds))
    try:
        yield amounts, bounds
    finally:
        REQUEST_INPUT_AMOUNT_BOUNDS.reset(tokens[2])
        REQUEST_INPUT_AMOUNTS.reset(tokens[1])
        PII_MAPPING.reset(tokens[0])


def _load(boxes, text: str) -> list[str]:
    amounts, bounds = boxes
    masked, ordered, header = _masked(text)
    amounts.update(header)
    bounds.update(input_amount_bounds(masked, header))
    return ordered


def _restore(params):
    return restore_params_for_kis(params, tool_name="domestic_stock", api_type="market_cap")


# ---------------------------------------------------------------------------
# 1. 발화 방향 읽기
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("30만원 이하 반도체 대형주 추천해줘", ["upper"]),
    ("30만원 미만 종목", ["upper"]),
    ("30만원까지 살 수 있는 종목", ["upper"]),
    ("30만원 이내 대형주", ["upper"]),
    ("30만원 넘지 않는 종목", ["upper"]),
    ("최대 30만원짜리 종목", ["upper"]),
    ("5만원 이상 종목", ["lower"]),
    ("5만원 초과 종목", ["lower"]),
    ("5만원 넘는 종목", ["lower"]),
    ("5만원부터 볼래", ["lower"]),
    ("최소 5만원 이상인 종목", ["lower"]),
    ("5만원 이상 30만원 이하 종목", ["lower", "upper"]),
    ("5만원~30만원 사이", ["lower", None]),
    ("30만원짜리 반도체주", [None]),
    ("30만원 정도 종목", [None]),
    ("최대 30만원 이상", [None]),  # 두 방향으로 읽힌다 — 지어내지 않는다
])
def test_reads_the_direction_the_user_said(text, expected):
    masked, ordered, header = _masked(text)
    assert len(ordered) == len(expected), masked

    bounds = input_amount_bounds(masked, header)

    assert [bounds.get(ph) for ph in ordered] == expected


# 비교어 뒤에 부정·제외·과거형이 오거나 "까지" 뒤가 도달 동사인 발화. 비교어만 보면 원한 것과 반대
# 방향으로 확정돼, 자리 맞춤이 LLM이 맞게 넣은 자리까지 뒤집는다(PR 리뷰). 방향을 비워야 한다.
_NEGATED_OR_NOT_A_CONDITION = [
    "30만원 이상은 빼고 보여줘",
    "30만원 이상인 건 제외하고",
    "30만원 넘는 건 빼고",
    "10만원 이상 아닌 종목",
    "30만원 이하 말고 대형주",
    "30만원 이하가 아닌 종목",
    "30만원 미만은 제외",
    "30만원 밑으로는 말고",
    "가격 30만원 미만이 아니라 이상",
    "30만원 이하 아니고 이상",
    "작년엔 30만원 이상이었는데 지금 싼 종목",
    "30만원까지 오른 종목",
    "30만원까지 올라간 종목",
    "30만원까지 떨어진 종목",
]


@pytest.mark.parametrize("text", _NEGATED_OR_NOT_A_CONDITION)
def test_negated_excluded_or_past_comparisons_leave_the_direction_unknown(text):
    """뮤테이션: 꼬리 검사를 빼면 이 문장들이 비교어 방향으로 확정돼 red."""
    masked, ordered, header = _masked(text)
    assert len(ordered) == 1, masked

    assert input_amount_bounds(masked, header) == {}


@pytest.mark.parametrize("text", _NEGATED_OR_NOT_A_CONDITION)
@pytest.mark.parametrize("slot", ["fid_input_price_1", "fid_input_price_2"])
def test_alignment_keeps_the_agents_slot_when_the_direction_is_unknown(boxes, text, slot):
    (amount,) = _load(boxes, text)
    params = {"fid_input_price_1": "", "fid_input_price_2": "", slot: amount}

    restored, rejections = _restore(params)

    assert rejections == []
    assert restored[slot] == _restore({slot: amount})[0][slot] != ""
    other = "fid_input_price_2" if slot == "fid_input_price_1" else "fid_input_price_1"
    assert restored[other] == ""


@pytest.mark.parametrize(("text", "expected"), [
    ("30만원 이하 반도체 대형주 추천해줘", "upper"),  # S5 기본 문장
    ("30만원까지 살 수 있는 종목", "upper"),  # "까지" 뒤가 도달 동사가 아니면 상한 그대로
    ("30만원 넘지 않는 종목", "upper"),  # "넘지 않"의 부정은 비교어 자체
    ("30만원 이하 종목 중 반도체 대형주 추천해줬었는데 다시", "upper"),  # 멀리 있는 과거형은 보지 않는다
])
def test_plain_conditions_still_get_a_direction(text, expected):
    masked, ordered, header = _masked(text)

    assert input_amount_bounds(masked, header) == {ordered[0]: expected}


def test_only_amounts_in_the_box_get_a_direction():
    masked, ordered, header = _masked("30만원 이하 종목")
    assert input_amount_bounds(masked, {}) == {}
    assert input_amount_bounds(masked, header) == {ordered[0]: "upper"}


# ---------------------------------------------------------------------------
# 2. 자리 맞춤 — restore_params_for_kis
# ---------------------------------------------------------------------------


class TestAlign:
    def test_cap_in_the_floor_slot_moves_to_the_cap_slot(self, boxes):
        """2026-10-27 실제 호출 모양. 뮤테이션: 자리 맞춤을 빼면 fid_input_price_1=300000(30만원 이상)으로 red."""
        (cap,) = _load(boxes, "30만원 이하 반도체 대형주 추천해줘")

        restored, rejections = _restore({"fid_input_price_1": cap, "fid_input_price_2": "", "fid_vol_cnt": ""})

        assert rejections == []
        assert restored == {"fid_input_price_1": "", "fid_input_price_2": "300000", "fid_vol_cnt": ""}

    def test_missing_cap_slot_is_created(self, boxes):
        (cap,) = _load(boxes, "30만원 미만 종목")

        restored, rejections = _restore({"FID_INPUT_PRICE_1": cap})

        assert rejections == []
        assert restored == {"FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "300000"}

    def test_floor_in_the_cap_slot_moves_to_the_floor_slot(self, boxes):
        (floor,) = _load(boxes, "5만원 이상 종목")

        restored, rejections = _restore({"fid_input_price_1": "", "fid_input_price_2": floor})

        assert rejections == []
        assert restored == {"fid_input_price_1": "50000", "fid_input_price_2": ""}

    def test_swapped_range_is_swapped_back(self, boxes):
        floor, cap = _load(boxes, "5만원 이상 30만원 이하 종목")

        restored, rejections = _restore({"fid_input_price_1": cap, "fid_input_price_2": floor})

        assert rejections == []
        assert restored == {"fid_input_price_1": "50000", "fid_input_price_2": "300000"}

    def test_range_with_one_known_end_is_swapped(self, boxes):
        """"5만원~30만원 사이"는 앞쪽만 방향이 읽힌다. 하한이 상한 자리에 있으면 남은 자리표시자가 상한이다."""
        floor, other = _load(boxes, "5만원~30만원 사이 종목")

        restored, rejections = _restore({"fid_input_price_1": other, "fid_input_price_2": floor})

        assert rejections == []
        assert restored == {"fid_input_price_1": "50000", "fid_input_price_2": "300000"}

    def test_right_slots_are_left_alone(self, boxes):
        floor, cap = _load(boxes, "5만원 이상 30만원 이하 종목")

        restored, rejections = _restore({"fid_input_price_1": floor, "fid_input_price_2": cap})

        assert rejections == []
        assert restored == {"fid_input_price_1": "50000", "fid_input_price_2": "300000"}

    def test_unknown_direction_keeps_the_agents_choice(self, boxes):
        (amount,) = _load(boxes, "30만원짜리 반도체주")

        restored, rejections = _restore({"fid_input_price_1": amount, "fid_input_price_2": ""})

        assert rejections == []
        assert restored == {"fid_input_price_1": "300000", "fid_input_price_2": ""}

    def test_occupied_cap_slot_is_refused(self, boxes):
        """맞는 자리에 다른 숫자가 있으면 어느 쪽이 맞는지 모른다 — 반대 방향으로 조회하지 않고 거부."""
        (cap,) = _load(boxes, "30만원 이하 종목")

        _, rejections = _restore({"fid_input_price_1": cap, "fid_input_price_2": "1000000"})

        assert [(r.field, r.reason) for r in rejections] == [("fid_input_price_1", "price_bound_direction_mismatch")]

    def test_no_direction_box_keeps_previous_behaviour(self, boxes):
        """방향 상자가 비면(구버전 경로) 종전처럼 LLM이 고른 자리 그대로 복원한다."""
        amounts, bounds = boxes
        (cap,) = _load(boxes, "30만원 이하 종목")
        bounds.clear()

        restored, _ = _restore({"fid_input_price_1": cap})

        assert restored == {"fid_input_price_1": "300000"}


# ---------------------------------------------------------------------------
# 3. 호출 경로 — KIS로 나가는 인자
# ---------------------------------------------------------------------------


@pytest.fixture
def remote(monkeypatch):
    calls: list[dict] = []

    async def fake_remote(**kwargs):
        arguments = kwargs["arguments"]
        calls.append(arguments)
        return json.dumps({"ok": True, "data": {
            "success": True, "api_type": arguments["api_type"], "params": arguments["params"],
            "data": "The End\n" + json.dumps([{"hts_kor_isnm": "삼성전자", "stck_prpr": "213500"}], ensure_ascii=False),
        }}, ensure_ascii=False)

    monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake_remote)
    return calls


async def _call(api_type: str, params: dict) -> str:
    config = finus_api.FinusAccountBalanceReadonlyConfig()
    prepared = finus_api._prepare_kis_trading_mcp_call(
        KisTradingMcpCallInput(tool_name="domestic_stock", api_type=api_type, params=params), config,
    )
    if isinstance(prepared, str):
        return prepared
    tool_name, arguments = prepared
    return await finus_api._call_kis_mcp_and_record(tool_name, arguments, config)


class TestCallPath:
    async def test_live_retry_arguments_reach_kis_as_a_cap(self, boxes, remote):
        """실제 3번째 호출 인자 그대로 — KIS에는 상한 300000이 가고 Observation엔 원값이 없다."""
        (cap,) = _load(boxes, "30만원 이하 반도체 대형주 추천해줘")
        live = {"fid_input_price_1": cap, "fid_input_price_2": "", "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20174", "fid_div_cls_code": "0", "fid_input_iscd": "0000",
                "fid_trgt_cls_code": "0", "fid_trgt_exls_cls_code": "0", "fid_vol_cnt": "", "tr_cont": ""}

        observation = await _call("market_cap", live)

        sent = remote[0]["params"]
        assert (sent["fid_input_price_1"], sent["fid_input_price_2"]) == ("", "300000")
        assert "300000" not in observation

    async def test_refused_alignment_never_reaches_kis(self, boxes, remote):
        (cap,) = _load(boxes, "30만원 이하 종목")

        observation = await _call("market_cap", {"fid_input_price_1": cap, "fid_input_price_2": "1000000"})

        assert remote == []
        error = json.loads(observation)
        assert error["rejected"] == [{"field": "fid_input_price_1", "reason": "price_bound_direction_mismatch"}]
        assert "price_bound_direction_mismatch" in error["hint"]
        assert "<AMOUNT_" not in observation

    async def test_cap_only_first_call_has_every_required_rank_argument(self, boxes, remote):
        """가격 조건만 넣은 첫 호출(실제·리허설 모두 이 모양)이 필수 인자 누락 없이 나간다.

        뮤테이션: 순위 TR 기본값을 빼면 fid_cond_scr_div_code 등이 없어 red — KIS MCP에서는 TypeError로
        실패하고 에이전트가 find_api_detail을 거쳐 인자를 다시 짠다(방향이 뒤바뀐 단계).
        """
        (cap,) = _load(boxes, "30만원 이하 반도체 대형주 추천해줘")

        await _call("market_cap", {"fid_input_price_2": cap})

        sent = {k: v for k, v in remote[0]["params"].items() if k != "env_dv"}  # env_dv는 배포 설정이 채운다
        assert sent == {
            "fid_input_price_2": "300000", "fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20174",
            "fid_div_cls_code": "0", "fid_input_iscd": "0000", "fid_trgt_cls_code": "0",
            "fid_trgt_exls_cls_code": "0", "fid_input_price_1": "", "fid_vol_cnt": "",
        }


class TestRankDefaults:
    def test_agent_values_win_and_case_variants_are_not_duplicated(self):
        out = finus_api._adjust_domestic_stock_params(
            "market_cap", {"FID_INPUT_ISCD": "1001", "fid_div_cls_code": "1", "fid_input_price_2": "300000"},
        )

        assert out["FID_INPUT_ISCD"] == "1001" and "fid_input_iscd" not in out
        assert out["fid_div_cls_code"] == "1"
        assert out["fid_cond_scr_div_code"] == "20174"

    def test_volume_power_uses_its_own_screen_code(self):
        assert finus_api._adjust_domestic_stock_params("volume_power", {})["fid_cond_scr_div_code"] == "20168"

    @pytest.mark.parametrize("api_type", ["volume_rank", "fluctuation", "inquire_price"])
    def test_other_trs_are_untouched(self, api_type):
        assert finus_api._adjust_domestic_stock_params(api_type, {"fid_input_price_2": "1"}) == {"fid_input_price_2": "1"}


# ---------------------------------------------------------------------------
# 4. 최상위 배선
# ---------------------------------------------------------------------------


async def test_trace_agent_installs_direction_box(monkeypatch):
    """뮤테이션: ``_response_fn``에서 REQUEST_INPUT_AMOUNT_BOUNDS.set을 빼면 seen이 None으로 red."""
    masked, ordered, header = _masked("30만원 이하 반도체 대형주 추천해줘")
    headers = {INPUT_AMOUNTS_HEADER: json.dumps(header, ensure_ascii=True)}
    monkeypatch.setattr(
        agents_mod, "Context",
        SimpleNamespace(get=lambda: SimpleNamespace(metadata=SimpleNamespace(headers=headers))),
    )
    seen: dict = {}

    class Inner:
        async def ainvoke(self, _request):
            seen["bounds"] = REQUEST_INPUT_AMOUNT_BOUNDS.get()
            return ChatResponse.from_string("답", usage=Usage())

    builder = MagicMock()
    builder.get_function = AsyncMock(return_value=Inner())
    config = FinusReasoningTraceAgentConfig(inner_agent_name="inner")
    async with finus_reasoning_trace_agent(config, builder) as info:
        await info.single_fn(ChatRequestOrMessage(messages=[{"role": "user", "content": masked}]))

    assert seen["bounds"] == {ordered[0]: "upper"}
    assert REQUEST_INPUT_AMOUNT_BOUNDS.get() is None
