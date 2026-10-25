"""사용자 발화 금액을 조회 인자로 되돌린다 (#468).

"30만원 이하 반도체 대형주 추천해줘"는 backend `llm_chat`이 마스킹해 외부 LLM에는
``<AMOUNT_…> 이하 …``로 들어간다. 에이전트는 값을 몰라 순위 조회에 조건을 넣지 못하거나 scope hex를
숫자로 읽었다(2026-10-25 리허설, ``fid_input_price_2=2975``). 마스킹은 유지하고, 조회 인자에 실린
자리표시자만 이 요청 상자로 되돌려 KIS에 보낸다 — 주문 인자 복원(#338)과 같은 원칙이다.

여기서 고정하는 것:
- 복원: 순위 가격 범위의 사용자 발화 금액 자리표시자가 원 단위 숫자로 KIS에 간다.
- 거부: 다른 요청·이전 턴의 자리표시자, 금액이 아닌 종류, 읽을 수 없는 표기, 주문 인자.
- 단위 정규화: "30만원"→300000 등, 애매한 표기는 거부.
- 외부 LLM 입력에 원값이 없다: LLM이 보는 발화와 Observation(서버가 되비친 params 포함).
"""

import json

import pytest

from nat.data_models.api_server import ChatRequestOrMessage, ChatResponse, Usage
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from finus_agents import agents as agents_mod
from finus_agents import finus_api
from finus_agents.agents import FinusReasoningTraceAgentConfig, finus_reasoning_trace_agent
from finus_agents.finus_api import KisTradingMcpCallInput
from finus_agents.pii_guard import (
    INPUT_AMOUNTS_HEADER,
    KIS_PASSTHROUGH_TOOL,
    PII_MAPPING,
    REQUEST_INPUT_AMOUNTS,
    _as_kis_lookup_amount,
    input_amounts_from_header,
    install_mapping_box,
    mask_lookup_param_echo,
    mask_tool_result,
    restore_params_for_kis,
)
from finus_agents.pii_mask import mask_pii

_UTTERANCE = "30만원 이하 반도체 대형주 추천해줘"


def _masked_utterance(text: str = _UTTERANCE) -> tuple[str, dict[str, str]]:
    """backend `llm_chat`이 하는 마스킹(바이트 복제 모듈)과 헤더에 실을 금액 매핑."""
    masked, mapping = mask_pii(text)
    amounts = {ph: v for ph, v in mapping.items() if ph.startswith("<AMOUNT_")}
    return masked, amounts


@pytest.fixture
def request_boxes():
    """최상위 에이전트가 요청마다 심는 두 상자를 재현한다. 발화 금액 상자는 테스트가 채운다."""
    box = install_mapping_box()
    amounts: dict[str, str] = {}
    t1 = PII_MAPPING.set(box)
    t2 = REQUEST_INPUT_AMOUNTS.set(amounts)
    try:
        yield box, amounts
    finally:
        REQUEST_INPUT_AMOUNTS.reset(t2)
        PII_MAPPING.reset(t1)


def _restore(params):
    return restore_params_for_kis(params, tool_name="domestic_stock", api_type="market_cap")


# ---------------------------------------------------------------------------
# 1. 복원과 거부 — restore_params_for_kis
# ---------------------------------------------------------------------------


class TestRestore:
    def test_user_amount_restores_into_rank_price_cap(self, request_boxes):
        """뮤테이션: 조회 분기에서 발화 금액 상자를 보지 않으면 no_mapping_in_this_request로 red."""
        _, amounts = request_boxes
        masked, header_amounts = _masked_utterance()
        amounts.update(header_amounts)
        placeholder = next(iter(header_amounts))
        assert masked.startswith(placeholder)

        restored, rejections = _restore({"fid_input_price_1": "", "fid_input_price_2": placeholder})

        assert rejections == []
        assert restored == {"fid_input_price_1": "", "fid_input_price_2": "300000"}

    def test_range_restores_both_bounds(self, request_boxes):
        _, amounts = request_boxes
        masked, header_amounts = _masked_utterance("5만원 이상 30만원 이하 종목 보여줘")
        amounts.update(header_amounts)
        low, high = sorted(header_amounts, key=masked.index)

        restored, rejections = _restore({"FID_INPUT_PRICE_1": low, "FID_INPUT_PRICE_2": high})

        assert rejections == []
        assert restored == {"FID_INPUT_PRICE_1": "50000", "FID_INPUT_PRICE_2": "300000"}

    def test_placeholder_from_another_request_is_rejected(self, request_boxes):
        """이전 턴·다른 요청의 scope는 이 요청 상자에 없다 — 원값을 지어내지 않고 거부."""
        _, amounts = request_boxes
        amounts.update(_masked_utterance()[1])
        _, other = _masked_utterance()  # 같은 발화라도 다른 요청이면 scope가 다르다
        foreign = next(iter(other))
        assert foreign not in amounts

        _, rejections = _restore({"fid_input_price_2": foreign})

        assert [(r.field, r.reason) for r in rejections] == [("fid_input_price_2", "no_mapping_in_this_request")]

    def test_quantity_placeholder_is_rejected_for_price(self, request_boxes):
        """종류 검사: 가격 범위에는 금액 자리표시자만 — 수량이 금액으로 둔갑하지 않는다."""
        box, _ = request_boxes
        _, mapping = mask_pii("삼성전자 보유 1,234주")
        box.update(mapping)
        qty = next(ph for ph in mapping if ph.startswith("<QTY_"))

        _, rejections = _restore({"fid_input_price_2": qty})

        assert [r.reason for r in rejections] == ["expected_AMOUNT_placeholder_got_QTY"]

    def test_unreadable_amount_is_rejected(self, request_boxes):
        """한글 수사처럼 한 가지로 읽히지 않는 표기는 숫자로 추측하지 않는다."""
        _, amounts = request_boxes
        _, header_amounts = _masked_utterance("삼십만원 이하 종목")
        amounts.update(header_amounts)

        _, rejections = _restore({"fid_input_price_2": next(iter(header_amounts))})

        assert [r.reason for r in rejections] == ["restored_value_is_not_a_number"]

    def test_order_fields_do_not_read_user_amounts(self, request_boxes):
        """주문 인자는 종전대로 도구 결과 박스만 본다 — 발화 금액 상자는 조회 전용이다."""
        _, amounts = request_boxes
        _, header_amounts = _masked_utterance("7만원에 사줘")
        amounts.update(header_amounts)

        _, rejections = restore_params_for_kis(
            {"ORD_UNPR": next(iter(header_amounts))}, tool_name="domestic_stock", api_type="order_cash",
        )

        assert [r.reason for r in rejections] == ["no_mapping_in_this_request"]

    def test_other_numeric_field_still_refuses_placeholders(self, request_boxes):
        """조회 인자 표에 없는 필드(fid_vol_cnt)는 종전대로 받지 않는다(fail-closed)."""
        _, amounts = request_boxes
        _, header_amounts = _masked_utterance()
        amounts.update(header_amounts)

        _, rejections = _restore({"fid_vol_cnt": next(iter(header_amounts))})

        assert [r.reason for r in rejections] == ["field_does_not_accept_placeholders"]


# ---------------------------------------------------------------------------
# 2. 단위 정규화 — _as_kis_lookup_amount
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("original", "expected"), [
    ("30만원", "300000"),
    ("30만 원", "300000"),
    ("300,000원", "300000"),
    ("300000원", "300000"),
    ("₩300,000", "300000"),
    ("KRW 300,000", "300000"),
    ("1.5억원", "150000000"),
    ("3천만원", "30000000"),
    ("3,000만원", "30000000"),
    ("1천5백만원", "15000000"),
    ("2만5천원", "25000"),
    ("3천500원", "3500"),
    ("천만원", "10000000"),
    ("1억 2천만원", "120000000"),
    ("9조1800억원", "9180000000000"),
    ("10,000,000", "10000000"),  # KIS 잔고 자리표시자의 원값 표기(_readable_number)
])
def test_lookup_amount_normalization(original, expected):
    assert _as_kis_lookup_amount(original) == expected


@pytest.mark.parametrize("original", [
    "삼십만원",       # 한글 수사
    "3.2M원",         # 라틴 크기
    "3.000.000원",    # 점 자리 구분 — 소수로도 읽힌다
    "3 000 000원",    # 공백 자리 구분
    "30,0000원",      # 어긋난 콤마 마디
    "1억20000만원",   # 아래 단위 자리가 만을 넘는다
    "1.5억3천만원",   # 소수와 아래 단위가 섞였다
    "1.23456만원",    # 정수가 아니다
    "12.5원",
    "3천1500원",      # 꼬리가 마지막 단위 자릿수를 넘는다
    "0원",
    "만원",
    "원",
    # 보안 리뷰 탐침(r468/probe.py)
    "1억2천",         # 1억2천만의 줄임말로도 읽힌다 — 글자대로면 100,002,000
    "1억2천원",
    "1억2000원",
    "1조5천원",
    "３０만원",       # 전각 숫자
    "٣٠만원",         # 아랍-인도 숫자
    "3０만원",        # 아스키 숫자 뒤에 전각 숫자가 섞임
    "00030만원",      # 앞자리 0
    "1천05만원",
    "1e5",
    "30만원 이하",
    "30 만원",
])
def test_ambiguous_amounts_are_refused(original):
    assert _as_kis_lookup_amount(original) is None


@pytest.mark.parametrize(("original", "expected"), [
    # 만 뒤 꼬리는 만 아래 단위가 없어 줄여 말할 자리가 없다 — 글자대로 읽는다.
    ("2만5천", "25000"),
    ("1만5000원", "15000"),
    ("1억2천만5천원", "120005000"),
    ("십만원", "100000"),
    ("₩ 30만", "300000"),
    ("  30만원  ", "300000"),
    ("0.5만원", "5000"),
    ("1.0만원", "10000"),
    ("1조", "1000000000000"),
])
def test_probe_amounts_that_read_one_way(original, expected):
    assert _as_kis_lookup_amount(original) == expected


# ---------------------------------------------------------------------------
# 3. 헤더 → 요청 상자 (이번 발화에 묶는다)
# ---------------------------------------------------------------------------


class TestHeader:
    def test_keeps_only_amounts_in_the_latest_utterance(self):
        masked, amounts = _masked_utterance()
        _, previous_turn = _masked_utterance("어제는 50만원 이하로 봤어")
        header = json.dumps({**amounts, **previous_turn}, ensure_ascii=True)

        assert input_amounts_from_header(header, masked) == amounts

    @pytest.mark.parametrize("raw", [
        "not json",
        json.dumps(["<AMOUNT_abcdef_1>"]),
        json.dumps({"<QTY_abcdef_1>": "10"}),
        json.dumps({"<AMOUNT_abcdef_1>": 300000}),
        json.dumps({f"<AMOUNT_abcdef_{i}>": "1원" for i in range(1, 40)}),
    ])
    def test_malformed_header_yields_empty_box(self, raw):
        text = " ".join(f"<AMOUNT_abcdef_{i}>" for i in range(1, 40))
        assert input_amounts_from_header(raw, text) == {}

    def test_any_parse_failure_yields_empty_box(self, monkeypatch):
        """ValueError 밖의 예외(깊은 중첩 JSON의 RecursionError 등)도 요청을 죽이지 않고 빈 상자다.

        뮤테이션: _request_input_amounts의 파싱 부분을 try 밖으로 빼면 RecursionError가 올라와 red.
        """
        masked, amounts = _masked_utterance()
        deep = "[" * 100000 + "]" * 100000
        monkeypatch.setattr(
            agents_mod, "Context",
            SimpleNamespace(get=lambda: SimpleNamespace(metadata=SimpleNamespace(headers={INPUT_AMOUNTS_HEADER: deep}))),
        )
        request = ChatRequestOrMessage(messages=[{"role": "user", "content": masked}])

        assert agents_mod._request_input_amounts(request) == {}

    async def test_trace_agent_installs_box_from_request_header(self, monkeypatch):
        """최상위 에이전트가 헤더를 읽어 상자를 심고, 요청이 끝나면 걷는다.

        뮤테이션: ``_response_fn``에서 REQUEST_INPUT_AMOUNTS.set을 빼면 seen이 None으로 red.
        """
        masked, amounts = _masked_utterance()
        headers = {INPUT_AMOUNTS_HEADER: json.dumps(amounts, ensure_ascii=True)}
        monkeypatch.setattr(
            agents_mod, "Context",
            SimpleNamespace(get=lambda: SimpleNamespace(metadata=SimpleNamespace(headers=headers))),
        )
        seen: dict = {}

        class Inner:
            async def ainvoke(self, _request):
                seen["amounts"] = REQUEST_INPUT_AMOUNTS.get()
                return ChatResponse.from_string("답", usage=Usage())

        builder = MagicMock()
        builder.get_function = AsyncMock(return_value=Inner())
        config = FinusReasoningTraceAgentConfig(inner_agent_name="inner")
        async with finus_reasoning_trace_agent(config, builder) as info:
            await info.single_fn(ChatRequestOrMessage(messages=[{"role": "user", "content": masked}]))

        assert seen["amounts"] == amounts
        assert REQUEST_INPUT_AMOUNTS.get() is None


# ---------------------------------------------------------------------------
# 4. 호출 경로 — KIS에는 원값, 외부 LLM(Observation)에는 자리표시자
# ---------------------------------------------------------------------------

_RANK_ROWS = [
    {"mksc_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자", "stck_prpr": "213500", "data_rank": "1"},
    {"mksc_shrn_iscd": "000990", "hts_kor_isnm": "DB하이텍", "stck_prpr": "61200", "data_rank": "2"},
]


@pytest.fixture
def echoing_remote(monkeypatch):
    """Kis Trading MCP처럼 받은 params를 봉투에 되비친다(2026-10-25 리허설 응답 모양)."""
    calls: list[dict] = []

    async def fake_remote(**kwargs):
        arguments = kwargs["arguments"]
        calls.append(arguments)
        return json.dumps({"ok": True, "data": {
            "success": True, "api_type": arguments["api_type"], "params": arguments["params"],
            "message": "domestic_stock API 호출 완료",
            "data": "The End\n" + json.dumps(_RANK_ROWS, ensure_ascii=False),
        }}, ensure_ascii=False)

    monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake_remote)
    return calls


async def _call_rank(params: dict) -> str:
    config = finus_api.FinusAccountBalanceReadonlyConfig()
    prepared = finus_api._prepare_kis_trading_mcp_call(
        KisTradingMcpCallInput(tool_name="domestic_stock", api_type="market_cap", params=params), config,
    )
    if isinstance(prepared, str):
        return prepared
    tool_name, arguments = prepared
    return await finus_api._call_kis_mcp_and_record(tool_name, arguments, config)


class TestCallPath:
    async def test_kis_gets_the_number_and_llm_sees_only_the_placeholder(self, request_boxes, echoing_remote):
        """외부 LLM 입력 = 마스킹된 발화 + Observation. 둘 다 원값이 없어야 한다.

        뮤테이션: _call_kis_mcp_and_record에서 mask_lookup_param_echo를 빼면 Observation에
        ``"fid_input_price_2": "300000"``이 되비쳐 red.
        """
        _, amounts = request_boxes
        masked, header_amounts = _masked_utterance()
        amounts.update(header_amounts)
        placeholder = next(iter(header_amounts))

        observation = await _call_rank({"fid_input_price_1": "", "fid_input_price_2": placeholder})

        # KIS에는 원 단위 숫자가 갔다.
        assert echoing_remote[0]["params"]["fid_input_price_2"] == "300000"
        # LLM이 보는 발화에는 금액 원문이 없다.
        assert "30만" not in masked and placeholder in masked
        # Observation에는 되비친 조건이 자리표시자로 남고, 원값은 없다.
        echoed = json.loads(observation)["data"]["params"]
        assert echoed["fid_input_price_2"] == placeholder
        assert "300000" not in observation and "30만" not in observation
        # 공개 시세 행은 그대로 보인다.
        assert "213500" in observation and "삼성전자" in observation

    async def test_rejected_placeholder_never_reaches_kis(self, request_boxes, echoing_remote):
        _, other = _masked_utterance()  # 이 요청 상자에 없는 scope

        observation = await _call_rank({"fid_input_price_2": next(iter(other))})

        assert echoing_remote == []
        error = json.loads(observation)
        assert error["error"] == "kis_param_placeholder_rejected"
        assert error["rejected"] == [{"field": "fid_input_price_2", "reason": "no_mapping_in_this_request"}]
        assert "<AMOUNT_" not in observation  # 자리표시자 원문도 Observation에 싣지 않는다(#338)


def test_echo_masking_leaves_other_fields_alone(request_boxes):
    _, amounts = request_boxes
    _, header_amounts = _masked_utterance()
    amounts.update(header_amounts)
    placeholder = next(iter(header_amounts))
    nested = json.dumps({"data": json.dumps({"params": {"fid_input_price_2": "300000"},
                                             "rows": [{"stck_prpr": "300000"}]})})

    out = mask_lookup_param_echo(nested, {"fid_input_price_2": "300000"})

    assert placeholder in out
    assert '\\"stck_prpr\\": \\"300000\\"' in out  # 같은 숫자라도 시세 필드는 공개 데이터라 남는다


@pytest.fixture
def cap_placeholder(request_boxes):
    _, amounts = request_boxes
    _, header_amounts = _masked_utterance()
    amounts.update(header_amounts)
    return next(iter(header_amounts))


class TestEchoForms:
    """되비침 표기가 달라도 같은 정규화값이면 가리고, 결과 JSON은 유효해야 한다(보안 리뷰 4)."""

    @pytest.mark.parametrize("raw", [
        {"fid_input_price_2": 300000},          # 숫자 타입 — 따옴표 없이 바꾸면 JSON이 깨진다
        {"fid_input_price_2": "300000"},
        {"fid_input_price_2": "300000.0"},
        {"fid_input_price_2": "300,000"},
        {"fid_input_price_2": ["300000"]},
        {"fid_input_price_2": [300000]},
        {"FID_INPUT_PRICE_2": "300000"},
    ])
    def test_json_forms_are_masked_and_stay_valid(self, cap_placeholder, raw):
        """뮤테이션: 숫자 타입에 따옴표를 씌우지 않으면 json.loads가 실패해 red."""
        text = json.dumps({"params": raw, "rows": [{"stck_prpr": "300000"}]})

        out = mask_lookup_param_echo(text, {"fid_input_price_2": "300000"})

        parsed = json.loads(out)
        echoed = next(iter(parsed["params"].values()))
        assert (echoed[0] if isinstance(echoed, list) else echoed) == cap_placeholder
        assert parsed["rows"][0]["stck_prpr"] == "300000"  # 시세 필드는 공개 데이터라 남는다

    @pytest.mark.parametrize("raw", [
        json.dumps({"detail": json.dumps({"params": {"fid_input_price_2": 300000}})}),
        json.dumps({"detail": json.dumps(json.dumps({"params": {"fid_input_price_2": "300000"}}))}),
    ])
    def test_nested_json_strings_stay_valid(self, cap_placeholder, raw):
        out = mask_lookup_param_echo(raw, {"fid_input_price_2": "300000"})

        inner = json.loads(json.loads(out)["detail"])
        if isinstance(inner, str):
            inner = json.loads(inner)
        assert inner["params"]["fid_input_price_2"] == cap_placeholder

    @pytest.mark.parametrize("raw", [
        "{'FID_INPUT_PRICE_2': '300000'}",
        "{'fid_input_price_2': 300000}",
        "fid_input_price_2=300000&x=1",
    ])
    def test_text_forms_are_masked(self, cap_placeholder, raw):
        out = mask_lookup_param_echo(raw, {"fid_input_price_2": "300000"})
        assert "300000" not in out and cap_placeholder in out

    def test_different_value_in_the_same_field_is_left_alone(self, cap_placeholder):
        text = json.dumps({"params": {"fid_input_price_2": "3000000"}})
        assert mask_lookup_param_echo(text, {"fid_input_price_2": "300000"}) == text


class TestEchoOnFailureAndPrivatePaths:
    """오류 봉투 detail과 비공개 TR 경로에서도 복원값이 되비쳐 나가지 않는다(보안 리뷰 5)."""

    @pytest.mark.parametrize("envelope", [
        # call_failed — 예제 함수 실행 실패, 서버가 params와 stderr를 되비친다.
        lambda p: {"ok": True, "data": {"success": False, "api_type": "market_cap", "params": p,
                                         "error": "TypeError: bad argument"}},
        # kis_rejected — 성공 봉투인데 data에 KIS 거절 표지가 있다.
        lambda p: {"ok": True, "data": {"success": True, "api_type": "market_cap", "params": p,
                                         "data": "Error in response: 200 rt_cd : 1 / msg_cd : OPSQ2002 / msg1 : ERROR INVALID INPUT"}},
    ])
    async def test_wrapper_error_detail_is_masked(self, request_boxes, monkeypatch, envelope):
        _, amounts = request_boxes
        _, header_amounts = _masked_utterance()
        amounts.update(header_amounts)
        placeholder = next(iter(header_amounts))

        async def fake_remote(**kwargs):
            return json.dumps(envelope(kwargs["arguments"]["params"]), ensure_ascii=False)

        monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake_remote)

        observation = await _call_rank({"fid_input_price_2": placeholder})

        error = json.loads(observation)
        assert error["error"] == "kis_api_failed"
        assert error["detail"]["params"]["fid_input_price_2"] == placeholder
        assert "300000" not in observation

    async def test_private_tr_path_is_masked(self, request_boxes, echoing_remote):
        """공개 시세 목록 밖 TR(volume_power)은 계좌 TR처럼 가리지만, params 되비침은 계좌 필드만 가린다 —
        그래서 여기서도 재마스킹이 있어야 원값이 남지 않는다."""
        _, amounts = request_boxes
        _, header_amounts = _masked_utterance()
        amounts.update(header_amounts)
        placeholder = next(iter(header_amounts))
        config = finus_api.FinusAccountBalanceReadonlyConfig()
        tool_name, arguments = finus_api._prepare_kis_trading_mcp_call(
            KisTradingMcpCallInput(tool_name="domestic_stock", api_type="volume_power",
                                   params={"fid_input_price_2": placeholder}), config,
        )

        observation = await finus_api._call_kis_mcp_and_record(tool_name, arguments, config)

        assert echoing_remote[0]["params"]["fid_input_price_2"] == "300000"
        assert json.loads(observation)["data"]["params"]["fid_input_price_2"] == placeholder
        assert "300000" not in observation


class TestRejectedKey:
    """거부 목록 키는 최상위 래퍼 오류에서만 그대로 둔다(보안 리뷰 1)."""

    def test_rejected_inside_a_kis_row_is_still_masked(self):
        """뮤테이션: rejected를 _KIS_META_KEYS로 되돌리면 행 안의 숫자가 평문으로 남아 red."""
        result = json.dumps({"ok": True, "data": {"data": json.dumps([{"rejected": "1234567890"}])}})
        box = install_mapping_box()
        token = PII_MAPPING.set(box)
        try:
            observation = mask_tool_result(KIS_PASSTHROUGH_TOOL, result, kis_api=("domestic_stock", "inquire_balance"))
        finally:
            PII_MAPPING.reset(token)
        assert "1234567890" not in observation

    async def test_top_level_rejection_list_stays_readable(self, request_boxes, echoing_remote):
        _, other = _masked_utterance()

        observation = await _call_rank({"fid_input_price_2": next(iter(other))})

        assert json.loads(observation)["rejected"][0]["field"] == "fid_input_price_2"


def test_tool_guide_tells_agents_to_pass_placeholders_verbatim():
    guide = finus_api._KIS_TRADING_TOOL_GUIDE
    assert "<AMOUNT_" in guide and "그대로" in guide and "추측" in guide
