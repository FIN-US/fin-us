"""#380: 조회 전용 래퍼의 국내주식 허용 목록을 upstream 판정표로 고정한다.

채팅 ``trading_agent``·``monitoring_agent``가 전체 권한 도구에서 조회 전용 래퍼로 옮겨 오면서,
접두사(``inquire_``·``search_``) 밖의 국내주식 조회 TR(순위·수급·재무 등)을 정확 값 목록
``_READONLY_DOMESTIC_STOCK_API_EXACT``로 넓혔다. 그 목록의 근거가 판정표
``fixtures/kis_domestic_stock_tr_verdicts.json``이다(생성기 ``finus_nat/scripts/kis_tr_verdicts.py``,
판정 규칙은 그 docstring).

이 파일이 지키는 것:

- (a) 판정표의 ``read`` TR은 국내주식에서 전부 허용된다.
- (b) 주문 5종·웹소켓 구독·목록에 없는 임의의 쓰기형 api_type은 어느 상품에서도 막힌다.
- 판정표와 허용 목록이 **양방향으로** 같다 — 판정표에 없는 값을 목록에 넣거나, ``read`` 행을
  빠뜨리면 빨개진다.
- 국내주식 전용 값은 다른 상품(tool_name)에서 막힌다.
- 래퍼가 실제로 이 판정을 태우고, 새로 열린 조회의 결과도 마스킹된다.
"""

import json
import re
from pathlib import Path

import pytest

from nat_finus_nat import finus_api
from nat_finus_nat.finus_api import (
    _READONLY_API_ALLOWLIST_EXACT,
    _READONLY_API_ALLOWLIST_PREFIXES,
    _READONLY_DOMESTIC_STOCK_API_EXACT,
    _READONLY_TOOL_ALLOWLIST,
    DATA_TOOL_LEDGER,
    DataToolLedger,
    _is_readonly_api_type,
)
from nat_finus_nat.pii_guard import MASKED_TOOLS, PII_MAPPING, install_mapping_box

_VERDICTS_PATH = Path(__file__).parent / "fixtures" / "kis_domestic_stock_tr_verdicts.json"
_CHAT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "configs" / "prompts" / "react_kis_chat.md"
_TABLE = json.loads(_VERDICTS_PATH.read_text(encoding="utf-8"))
_ROWS: list[dict] = _TABLE["trs"]
_BY_VERDICT: dict[str, list[str]] = {}
for _row in _ROWS:
    _BY_VERDICT.setdefault(_row["verdict"], []).append(_row["api_type"])

_READ_TRS = _BY_VERDICT.get("read", [])
_WRITE_TRS = _BY_VERDICT.get("write", [])
_WEBSOCKET_TRS = _BY_VERDICT.get("websocket", [])

# 판정표가 아니라 이슈 본문(#380)에서 온 기대값이다. 판정표를 다시 만들었는데 쓰기 TR 집합이
# 달라졌다면 규칙이 새 주문 TR을 찾은 것이니, 사람이 보고 이 상수를 고친다.
_EXPECTED_WRITE_TRS = {
    "order_cash", "order_credit", "order_resv", "order_resv_rvsecncl", "order_rvsecncl",
}

# Kis Trading MCP의 특수 api_type — TR이 아니라 판정표에 없다(``tools/base.py``).
_SPECIAL_DOMESTIC_ENTRIES = {"find_stock_code"}

_OTHER_ASSET_CLASSES = sorted(_READONLY_TOOL_ALLOWLIST - {"domestic_stock"})


def _covered_without_domestic_list(api_type: str) -> bool:
    """#66의 상품 무관 규칙(접두사·정확 값)만으로 이미 허용되는가."""
    return api_type.startswith(_READONLY_API_ALLOWLIST_PREFIXES) or api_type in _READONLY_API_ALLOWLIST_EXACT


# ---------------------------------------------------------------------------
# 판정표 자체
# ---------------------------------------------------------------------------

def test_verdict_table_covers_the_upstream_snapshot_without_ambiguity():
    """판정표가 upstream 국내주식 TR을 빠짐없이, 애매한 판정 없이 담는다.

    ``ambiguous`` 행이 생기면 규칙이 가르지 못한 TR이 있다는 뜻이다 — 허용하지 않고(fail-closed)
    사람이 본다.
    """
    names = [r["api_type"] for r in _ROWS]
    assert len(names) == len(set(names)), "판정표에 같은 api_type이 두 번 있습니다"
    assert len(names) == 156, "upstream 스냅샷(examples_llm/domestic_stock)의 TR 수와 다릅니다"
    assert set(_BY_VERDICT) <= {"read", "write", "websocket"}, (
        f"규칙이 가르지 못한 TR: {_BY_VERDICT.get('ambiguous')}"
    )
    assert set(_WRITE_TRS) == _EXPECTED_WRITE_TRS


# ---------------------------------------------------------------------------
# (a) 조회 TR은 허용 / (b) 쓰기·웹소켓·임의 쓰기형은 차단
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("api_type", _READ_TRS)
def test_every_upstream_read_tr_is_allowed_on_domestic_stock(api_type: str):
    """(a) 판정표의 조회 TR은 국내주식에서 전부 허용된다.

    뮤테이션: ``_is_readonly_api_type``의 국내주식 분기를 ``return False``로 바꾸면 접두사 밖의
    85개 행이 red. 목록에서 한 줄(예: ``volume_rank``)을 빼면 그 행이 red.
    """
    assert _is_readonly_api_type(api_type, tool_name="domestic_stock") is True


@pytest.mark.parametrize("tool_name", sorted(_READONLY_TOOL_ALLOWLIST))
@pytest.mark.parametrize("api_type", _WRITE_TRS + _WEBSOCKET_TRS)
def test_write_and_websocket_trs_are_blocked_everywhere(api_type: str, tool_name: str):
    """(b) 주문 5종과 웹소켓 구독은 어느 상품에서도 조회로 판정되지 않는다.

    뮤테이션: 국내주식 목록에 ``order_cash``를 넣으면 red.
    """
    assert _is_readonly_api_type(api_type, tool_name=tool_name) is False


@pytest.mark.parametrize("api_type", [
    "order",                   # 해외주식 주문 TR 이름 — 국내주식에서 이름이 겹쳐도 막는다
    "order_cash_v2",           # 주문 TR의 가상 후속판
    "place_order",
    "cancel_all_orders",
    "transfer_cash",           # 계좌 이체 같은 가상의 쓰기 TR
    "volume_rank_and_order",   # 허용 값을 접두사로 가진 가상의 쓰기 TR — 정확 값 판정 확인
    "ORDER_CASH",
    "  order_rvsecncl  ",
], ids=lambda x: x.strip() or "<blank>")
def test_unlisted_write_like_api_types_are_blocked(api_type: str):
    """(b) 목록에 없는 임의의 쓰기형 api_type은 막힌다 — 정확 값 판정이지 접두사 판정이 아니다.

    뮤테이션: 국내주식 목록 판정을 ``lower.startswith(tuple(_READONLY_DOMESTIC_STOCK_API_EXACT))``
    같은 접두사 판정으로 느슨하게 하면 ``volume_rank_and_order``가 red.
    """
    assert _is_readonly_api_type(api_type, tool_name="domestic_stock") is False


# ---------------------------------------------------------------------------
# 판정표 ↔ 허용 목록 양방향 대조
# ---------------------------------------------------------------------------

def test_domestic_allowlist_is_exactly_the_uncovered_read_trs():
    """허용 목록 = 판정표의 ``read`` 중 #66 규칙으로 덮이지 않는 것 + 특수 api_type.

    - 목록에만 있는 값: 판정표 근거 없이 넓힌 것이다(예: 쓰기 TR을 실수로 추가).
    - 판정표에만 있는 값: 조회인데 채팅에서 막힌다(#380 결정의 전제를 되돌린다).
    - #66 규칙으로 이미 덮이는 값이 목록에 있으면 중복이다 — 어느 쪽이 근거인지 흐려진다.

    뮤테이션: 목록에 ``order_resv``를 넣거나 ``market_cap``을 빼면 red.
    """
    expected = {name for name in _READ_TRS if not _covered_without_domestic_list(name)}
    actual = set(_READONLY_DOMESTIC_STOCK_API_EXACT) - _SPECIAL_DOMESTIC_ENTRIES

    assert actual - expected == set(), f"판정표 근거 없이 허용된 값: {sorted(actual - expected)}"
    assert expected - actual == set(), f"조회인데 허용 목록에 없는 TR: {sorted(expected - actual)}"
    assert _SPECIAL_DOMESTIC_ENTRIES <= set(_READONLY_DOMESTIC_STOCK_API_EXACT)
    assert not [n for n in _READONLY_DOMESTIC_STOCK_API_EXACT if _covered_without_domestic_list(n)]
    # 결정 코멘트의 전제 — 채팅이 잃을 뻔한 대표 조회가 실제로 이 목록에 있다.
    assert {"volume_rank", "market_cap", "fluctuation", "program_trade_by_stock"} <= actual


@pytest.mark.parametrize("tool_name", _OTHER_ASSET_CLASSES)
def test_domestic_only_entries_are_blocked_for_other_asset_classes(tool_name: str):
    """국내주식 판정표로 연 값은 다른 상품에서 열리지 않는다(판정은 국내주식만 했다).

    뮤테이션: ``_is_readonly_api_type``에서 tool_name 비교를 빼면 red.
    """
    leaked = sorted(
        name for name in _READONLY_DOMESTIC_STOCK_API_EXACT
        if _is_readonly_api_type(name, tool_name=tool_name)
    )
    assert leaked == []


def _forbidden_order_trs(text: str) -> set[str]:
    """에이전트에게 "주문 TR(…)은 호출하지 말라"고 적은 괄호 안의 api_type 이름들."""
    match = re.search(r"주문 TR\(([^)]*)\)", text)
    assert match, "주문 TR 목록 문구를 찾지 못했습니다"
    return set(re.findall(r"[A-Za-z_]+", match.group(1)))


def _assert_order_names_agree_with_the_allowlist(text: str) -> None:
    """에이전트에게 주는 문구가 허용 목록과 반대로 말하지 않는다 (PR #388 리뷰).

    - "금지" 목록은 정확히 주문 5종이다. ``order_``로 시작하는 것을 통째로 금지하면 이 PR이 연
      예약주문조회(``order_resv_ccnl``)까지 금지하게 된다.
    - ``order_``로 시작하는 허용 값은 문구에 이름이 나온다 — 모델이 금지로 오해하지 않게.
    - 문구에 나오는 ``order_…`` 이름은 전부 주문 5종이거나 허용 값이다(접두사 표현 ``order_로`` 등 없음).
    """
    allowed_order_names = {n for n in _READONLY_DOMESTIC_STOCK_API_EXACT if n.startswith("order_")}
    mentioned = set(re.findall(r"order_\w+", text))

    assert _forbidden_order_trs(text) == _EXPECTED_WRITE_TRS
    assert allowed_order_names <= mentioned
    assert mentioned <= _EXPECTED_WRITE_TRS | allowed_order_names, sorted(mentioned)


def test_chat_prompt_names_the_order_trs_instead_of_banning_the_prefix():
    """채팅 프롬프트(react_kis_chat.md)의 주문 금지 문구가 허용 목록과 맞는다 (PR #388 리뷰).

    뮤테이션: 금지 문구를 리뷰 전의 "주문 TR(order_로 시작하는 api_type)"로 되돌리면 red.
    """
    _assert_order_names_agree_with_the_allowlist(_CHAT_PROMPT_PATH.read_text(encoding="utf-8"))


def test_tool_name_and_api_type_are_normalised():
    """래퍼가 넘기는 값은 소문자지만, 판정 함수도 스스로 정규화한다(대소문자·공백)."""
    assert _is_readonly_api_type(" VOLUME_RANK ", tool_name=" Domestic_Stock ") is True


# ---------------------------------------------------------------------------
# 래퍼 배선 — 판정을 실제로 태우는가, 새로 열린 조회도 마스킹되는가
# ---------------------------------------------------------------------------

class _RemoteKis:
    def __init__(self, response: str) -> None:
        self.calls: list[dict] = []
        self.response = response

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class TestReadonlyWrapper:
    # 새로 열린 계좌 성격 조회(주식통합증거금 현황 등)가 돌려줄 법한 금액·수량·계좌번호.
    _ACCOUNT_RESULT = "계좌 12345678-01 · 삼성전자 1,234주 · 증거금 12,345,000원"

    @pytest.fixture
    def remote(self, monkeypatch):
        fake = _RemoteKis(self._ACCOUNT_RESULT)
        monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake)
        monkeypatch.setenv("FINUS_SKIP_MCP_LIST_TOOLS", "1")
        return fake

    @pytest.fixture
    def mapping_box(self):
        box = install_mapping_box()
        token = PII_MAPPING.set(box)
        try:
            yield box
        finally:
            PII_MAPPING.reset(token)

    @pytest.fixture
    def ledger(self):
        led = DataToolLedger()
        token = DATA_TOOL_LEDGER.set(led)
        try:
            yield led
        finally:
            DATA_TOOL_LEDGER.reset(token)

    async def _call(self, api_type: str, *, tool_name: str = "domestic_stock") -> str:
        config = finus_api.FinusAccountBalanceReadonlyConfig(trading_tool_name="domestic_stock")
        async with finus_api.finus_account_balance_readonly(config, None) as info:
            return await info.single_fn(
                finus_api.KisTradingMcpCallInput(tool_name=tool_name, api_type=api_type, params={})
            )

    @pytest.mark.parametrize("api_type", ["volume_rank", "intgr_margin", "find_stock_code"])
    async def test_newly_allowed_tr_reaches_kis_and_its_result_is_masked(
        self, remote, mapping_box, ledger, api_type
    ):
        """새로 연 조회가 KIS까지 가고, 결과는 LLM 컨텍스트로 가기 전에 마스킹된다.

        마스킹은 api_type이 아니라 원장 키(``finus_account_balance``) 단위다 — 새 TR도
        기본값이 "마스킹됨"이라는 전제를 여기서 확인한다.

        뮤테이션: 래퍼가 ``_is_readonly_api_type``에 넘기는 tool_name을 빈 문자열로 바꾸면
        호출이 막혀 red. ``_call_kis_mcp_and_record``가 ``_record_and_mask`` 대신 원문을
        돌려주면 red.
        """
        observation = await self._call(api_type)

        assert [c["arguments"]["api_type"] for c in remote.calls] == [api_type]
        assert "finus_account_balance" in MASKED_TOOLS
        for raw in ("12345678-01", "1,234주", "12,345,000원"):
            assert raw not in observation
        assert set(mapping_box.values()) >= {"12,345,000원"}
        assert [r.tool_name for r in ledger.records] == ["finus_account_balance"]

    @pytest.mark.parametrize("api_type", sorted(_EXPECTED_WRITE_TRS))
    async def test_order_trs_never_reach_kis(self, remote, mapping_box, ledger, api_type):
        """(b) 래퍼 수준 — 주문 TR은 KIS 호출 자체가 일어나지 않는다."""
        observation = await self._call(api_type)

        assert remote.calls == []
        payload = json.loads(observation)
        assert payload["error"] == "kis_api_type_not_allowed_readonly"
        # 에이전트가 재시도하지 않고 사용자에게 주문 명령을 안내하게 한다.
        assert "/buy" in payload["hint"]
        # 거부 hint도 허용 목록과 반대로 말하지 않는다 — "주문(order_*)"처럼 접두사로 금지하면
        # 예약주문조회까지 금지로 읽힌다(PR #388 리뷰).
        _assert_order_names_agree_with_the_allowlist(payload["hint"])

    async def test_domestic_only_tr_is_blocked_for_another_asset_class(
        self, remote, mapping_box, ledger
    ):
        """래퍼가 판정에 **실제 호출의** tool_name을 넘긴다.

        뮤테이션: 래퍼가 tool_name 대신 ``"domestic_stock"`` 상수를 넘기면 red.
        """
        observation = await self._call("volume_rank", tool_name="overseas_stock")

        assert remote.calls == []
        assert json.loads(observation)["error"] == "kis_api_type_not_allowed_readonly"
