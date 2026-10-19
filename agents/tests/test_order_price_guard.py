"""pass-through KIS 주문의 지정가 괴리 가드 테스트 (#365).

세 층을 고정한다.

1. **판정 의미가 backend와 같다** — 판정표(``backend/tests/fixtures/price_gap_policy.json``)를
   backend ``evaluate_hard_limits`` 스위트와 함께 읽는다(#138 공유 판정표 방식). 임계값의
   env 이름·기본값은 ``backend/core/config.py``를 **정적으로** 읽어 대조한다 — import하면
   ``load_dotenv()``가 테스트 세션의 env를 오염시킨다(``test_env_whitelist.py`` 참고).
2. **순수 함수** — 현재가 파싱, 단가 필드 판별, 임계값 해석.
3. **배선** — 실제 ``finus_account_balance`` 도구를 통과시켜, 평가금액 자리표시자가
   ``ORD_UNPR``에 실리면 KIS 주문 호출 **자체가** 일어나지 않는지 본다.
"""
import ast
import json
from pathlib import Path

import pytest

from finus_agents import finus_api
from finus_agents.finus_api import DATA_TOOL_LEDGER, DataToolLedger
from finus_agents.order_price_guard import (
    DEFAULT_MAX_PRICE_GAP_RATIO,
    ERROR_GAP_EXCEEDED,
    ERROR_UNVERIFIABLE,
    PRICE_GAP_RATIO_ENV,
    check_order_price_gap,
    max_price_gap_ratio,
    order_unit_prices,
    parse_current_price,
    price_gap_exceeds,
)
from finus_agents.pii_guard import PII_MAPPING, install_mapping_box, mask_tool_result

_REPO_ROOT = Path(__file__).resolve().parents[2]
_POLICY_PATH = _REPO_ROOT / "backend" / "tests" / "fixtures" / "price_gap_policy.json"
_POLICY_CASES = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["cases"]
_BACKEND_CONFIG = _REPO_ROOT / "backend" / "core" / "config.py"
_QUOTE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kis_trading_mcp_inquire_price"


# ---------------------------------------------------------------------------
# 1. backend와 같은 판정
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _POLICY_CASES, ids=[c["name"] for c in _POLICY_CASES])
def test_gap_judgement_follows_the_table_shared_with_backend(case):
    """backend ``test_price_gap_policy.py``가 같은 표로 ``evaluate_hard_limits``를 돌린다.

    뮤테이션: ``price_gap_exceeds``의 ``>``를 ``>=``로 바꾸면 경계 행
    (``upper_boundary_is_allowed``·``lower_boundary_is_allowed``)이 red가 되고, ``abs``를
    빼면 ``just_under_lower_boundary``가 red가 된다.
    """
    exceeds = price_gap_exceeds(case["price"], case["current_price"], case["max_price_gap_ratio"])

    assert exceeds is case["exceeds"]


def test_threshold_env_name_and_default_match_backend_config():
    """같은 .env 한 줄로 두 계층의 괴리 한도가 함께 움직여야 한다.

    뮤테이션: ``DEFAULT_MAX_PRICE_GAP_RATIO``를 0.05로 바꾸거나 ``PRICE_GAP_RATIO_ENV``를
    다른 이름으로 바꾸면 red.
    """
    tree = ast.parse(_BACKEND_CONFIG.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "ORDER_MAX_PRICE_GAP_RATIO" for t in node.targets)
        ):
            call = node.value
            break
    else:
        raise AssertionError("backend/core/config.py에서 ORDER_MAX_PRICE_GAP_RATIO를 찾지 못했다")

    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    # backend가 해석 규칙을 다른 로더로 바꾸면 아래 test_threshold_falls_back_like_backend가
    # 흉내 내는 규칙도 다시 봐야 한다.
    assert call.func.id == "_float_env"
    env_name, default = (ast.literal_eval(arg) for arg in call.args)
    assert env_name == PRICE_GAP_RATIO_ENV
    assert default == DEFAULT_MAX_PRICE_GAP_RATIO


def test_threshold_is_read_from_the_backend_env_name(monkeypatch):
    """뮤테이션: ``max_price_gap_ratio``가 env를 읽지 않고 기본값을 돌려주면 red."""
    monkeypatch.setenv("ORDER_MAX_PRICE_GAP_RATIO", "0.1")

    assert max_price_gap_ratio() == 0.1


@pytest.mark.parametrize("raw", ["", "  ", "abc", "inf", "nan", "-0.01"])
def test_threshold_falls_back_like_backend(monkeypatch, raw):
    """``backend/core/config.py``의 ``_float_env``와 같은 규칙 — 조용히 한도가 사라지지 않는다.

    뮤테이션: ``isfinite``·음수 가드를 빼면 ``inf``/``nan``/``-0.01`` 행이 red.
    """
    monkeypatch.setenv("ORDER_MAX_PRICE_GAP_RATIO", raw)

    assert max_price_gap_ratio() == DEFAULT_MAX_PRICE_GAP_RATIO


# ---------------------------------------------------------------------------
# 2. 순수 함수
# ---------------------------------------------------------------------------


def _quote_fixture(name: str) -> str:
    """Kis Trading MCP ``inquire_price`` 실응답 원문(#381).

    모의투자 계정으로 가드와 같은 인자(``fid_cond_mrkt_div_code=J``, ``fid_input_iscd``,
    ``env_dv``)를 ``_mcp_call_tool_remote``로 보내 받은 문자열을 손대지 않고 저장했다. 시세
    응답뿐이라 계좌번호·키·토큰이 없다.

    - ``005930_listed`` — 정상 종목(삼성전자). ``env_dv=real``·``env_dv`` 생략도 같은 모양이었다.
    - ``000300_trading_halted`` — 거래정지 종목(``iscd_stat_cls_code=58``). 마지막 체결가가 실린다.
    - ``999999_unknown_code`` — 존재하지 않는 종목코드. 오류가 아니라 0으로 채운 행이 오고
      ``stck_shrn_iscd``가 없다. 상장폐지 종목(071460)도 같은 모양이었다.
    - ``error_missing_argument`` — 인자 누락. ``success: false`` + ``error``.
    - ``error_kis_rejected`` — KIS가 거절(``OPSQ2001``). ``success: true``인데 ``data``가 텍스트다.
    """
    return (_QUOTE_FIXTURES / f"{name}.json").read_text(encoding="utf-8")


def _quote_with_rows(rows: list[dict]) -> str:
    """정상 종목 실응답의 봉투는 그대로 두고 안쪽 행만 바꾼다."""
    envelope = json.loads(_quote_fixture("005930_listed"))
    envelope["data"]["data"] = json.dumps(rows, ensure_ascii=False) + "\n"
    return json.dumps(envelope, ensure_ascii=False)


def _listed_row() -> dict:
    envelope = json.loads(_quote_fixture("005930_listed"))
    (row,) = json.loads(envelope["data"]["data"])
    return row


def quote_text(stock_code: str = "005930", price: str = "68900") -> str:
    """배선 테스트용 — 실응답 모양에 종목코드·현재가만 바꿔 넣는다."""
    return _quote_with_rows([{**_listed_row(), "stck_shrn_iscd": stock_code, "stck_prpr": price}])


@pytest.mark.parametrize(
    ("fixture", "stock_code", "expected"),
    [("005930_listed", "005930", 223500), ("000300_trading_halted", "000300", 4200)],
)
def test_current_price_is_read_from_the_real_quote(fixture, stock_code, expected):
    """실응답의 현재가는 안쪽 ``data``(JSON 문자열) 속에 있다.

    뮤테이션: 바깥 JSON에서 키를 찾던 #381 이전 파서로 되돌리거나, ``_quote_rows``가 안쪽
    ``data`` 문자열을 파싱하지 않게 하면 두 행 모두 ``None``이라 red다. 이전 파서는 실응답을
    못 읽어 단가 있는 주문을 전부 거부하고 있었다.
    """
    assert parse_current_price(_quote_fixture(fixture), stock_code=stock_code) == expected


@pytest.mark.parametrize(
    "fixture", ["999999_unknown_code", "error_missing_argument", "error_kis_rejected"]
)
def test_real_unreadable_quotes_are_none(fixture):
    """존재하지 않는 종목코드는 0으로 채운 행, 인자 오류는 ``success: false``, KIS 거절은 텍스트다.

    실응답 모양을 고정하는 회귀 테스트다. ``999999_unknown_code``는 ``stck_shrn_iscd`` 부재와
    현재가 0 두 겹으로 걸러져 한쪽만 빼서는 red가 되지 않는다 — 각 겹은
    ``test_quote_row_of_another_stock_is_not_the_reference``와
    ``test_zero_or_blank_current_price_is_none``이 따로 잡는다.
    """
    assert parse_current_price(_quote_fixture(fixture), stock_code="999999") is None


def test_quote_row_of_another_stock_is_not_the_reference():
    """조회한 종목이 아닌 행의 현재가로 괴리를 재지 않는다.

    뮤테이션: ``stck_shrn_iscd`` 대조를 빼면 005930 응답이 000660의 기준가로 읽혀 red.
    """
    assert parse_current_price(_quote_fixture("005930_listed"), stock_code="000660") is None


def test_quote_with_several_rows_is_none():
    """행이 여럿이면 어느 행이 이 종목인지 고를 근거가 없다 — 첫 행을 집지 않는다.

    뮤테이션: 행 개수 검사(``len(rows) != 1``)를 빼고 첫 행을 쓰게 하면 red.
    """
    other = {**_listed_row(), "stck_shrn_iscd": "005930", "stck_prpr": "100"}

    assert parse_current_price(_quote_with_rows([_listed_row(), other]), stock_code="005930") is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        '{"error": "mcp_call_failed", "detail": "stck_prpr"}',
        # 이전 파서가 추정해 읽던 모양 — 실응답이 아니므로 더는 현재가가 아니다.
        '{"output": {"stck_prpr": "68900", "stck_shrn_iscd": "005930"}}',
        "- 현재가 stck_prpr: 68,900",
        "조회 결과가 없습니다.",
    ],
)
def test_unreadable_quote_is_none_not_zero(text):
    """0이나 오류를 현재가로 읽으면 괴리 계산이 무너진다 — 못 읽은 것은 ``None``이다.

    뮤테이션: ``parse_current_price``를 #381 이전의 관대한 탐색(JSON 어느 깊이든 키 탐색 +
    텍스트 정규식)으로 되돌리면 ``output`` 행과 텍스트 행이 68900으로 읽혀 red.
    """
    assert parse_current_price(text, stock_code="005930") is None


@pytest.mark.parametrize("price", ["0", "-", ""])
def test_zero_or_blank_current_price_is_none(price):
    """뮤테이션: ``number <= 0`` 거르기를 빼면 ``0``·빈 값 행이 red."""
    assert parse_current_price(quote_text(price=price), stock_code="005930") is None


def test_unit_price_fields_are_judged_by_suffix_and_zero_is_market():
    prices, unreadable = order_unit_prices(
        {"PDNO": "005930", "ORD_QTY": "3", "ord_unpr": "68,900", "TOT_AMT": "999"}
    )
    assert [(p.field, p.price) for p in prices] == [("ord_unpr", 68900)]
    assert unreadable == []

    assert order_unit_prices({"ORD_UNPR": "0"}) == ([], [])
    assert order_unit_prices({"ORD_UNPR": ""}) == ([], [])
    assert order_unit_prices({"ORD_UNPR": "6만9천"}) == ([], ["ORD_UNPR"])


async def test_quote_fetch_exception_is_a_rejection_not_a_pass():
    """뮤테이션: ``fetch_quote`` 호출을 감싼 ``try/except``를 빼면 예외가 탈출해 red."""

    async def _boom(stock_code, env_dv):
        raise RuntimeError("connection reset")

    rejection = await check_order_price_gap(
        tool_name="domestic_stock",
        api_type="order_cash",
        params={"PDNO": "005930", "ORD_UNPR": "68900"},
        fetch_quote=_boom,
    )

    assert rejection is not None
    assert (rejection.error, rejection.reason) == (ERROR_UNVERIFIABLE, "current_price_unavailable")


# ---------------------------------------------------------------------------
# 3. 배선 — 실제 도구를 통과시킨다
# ---------------------------------------------------------------------------


@pytest.fixture
def mapping_box():
    box = install_mapping_box()
    token = PII_MAPPING.set(box)
    try:
        yield box
    finally:
        PII_MAPPING.reset(token)


@pytest.fixture
def ledger():
    led = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(led)
    try:
        yield led
    finally:
        DATA_TOOL_LEDGER.reset(token)


class _RemoteKis:
    """원격 KIS MCP 대역. 도구가 실제로 넘긴 인자를 관측한다.

    ``inquire_price``에는 :attr:`quote`를, 그 밖의 호출에는 주문 접수 응답을 돌려준다.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.quote = quote_text("005930", "68900")

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["arguments"]["api_type"] == "inquire_price":
            return self.quote
        return '{"output": [{"odno": "0000117057"}]}'

    @property
    def quotes(self) -> list[dict]:
        return [c for c in self.calls if c["arguments"]["api_type"] == "inquire_price"]

    @property
    def orders(self) -> list[dict]:
        return [c for c in self.calls if c["arguments"]["api_type"] != "inquire_price"]


class TestKisOrderPriceGapGuard:
    # 이슈 본문의 실측 문자열. 마스킹하면 두 금액이 모두 AMOUNT 자리표시자가 된다.
    _BALANCE = "삼성전자 (005930) · 10주 · 평가금액 12,345,000원 · 현재가 68,900원"

    @pytest.fixture
    def remote(self, monkeypatch):
        fake = _RemoteKis()
        monkeypatch.setattr(finus_api, "_mcp_call_tool_remote", fake)
        monkeypatch.setenv("FINUS_SKIP_MCP_LIST_TOOLS", "1")
        monkeypatch.delenv("ORDER_MAX_PRICE_GAP_RATIO", raising=False)
        return fake

    def _placeholders(self, box) -> tuple[str, str]:
        mask_tool_result("finus_mcp_trading_get_balance", self._BALANCE)
        total = next(ph for ph, raw in box.items() if raw.startswith("12,345,000"))
        price = next(ph for ph, raw in box.items() if raw.startswith("68,900"))
        assert total.startswith("<AMOUNT_") and price.startswith("<AMOUNT_")
        return total, price

    async def _call(self, api_type: str, params: dict, *, tool_name: str = "domestic_stock") -> str:
        config = finus_api.FinusAccountBalanceConfig(trading_tool_name="domestic_stock")
        async with finus_api.finus_account_balance(config, None) as info:
            return await info.single_fn(
                finus_api.KisTradingMcpCallInput(tool_name=tool_name, api_type=api_type, params=params)
            )

    async def test_total_value_placeholder_in_unit_price_never_reaches_kis(
        self, remote, mapping_box, ledger
    ):
        """이슈의 재현 경로 — 종류 검사는 통과하지만 괴리 가드가 주문 호출 자체를 막는다.

        뮤테이션: ``_call_kis_mcp_and_record``에서 ``_order_price_gap_rejection`` 호출을 빼면
        주문이 ``ORD_UNPR=12345000``으로 KIS까지 가서 red.
        """
        total, _ = self._placeholders(mapping_box)

        observation = await self._call(
            "order_cash", {"PDNO": "005930", "ORD_DVSN": "00", "ORD_QTY": "1", "ORD_UNPR": total}
        )

        assert remote.orders == []
        payload = json.loads(observation)
        assert payload["error"] == ERROR_GAP_EXCEEDED
        assert payload["fields"] == ["ORD_UNPR"]
        assert "inquire_price" in payload["hint"]
        # 가격 원값도 내부 토큰도 Observation으로 돌려주지 않는다.
        assert "12345000" not in observation
        assert total not in observation

    async def test_current_price_placeholder_still_orders(self, remote, mapping_box, ledger):
        """정상 단가는 통과하고, 기준가는 주문 종목의 현재가로 조회한다.

        주문 params에 ``env_dv``가 없으면 Kis Trading MCP와 같은 ``demo``로 조회한다 — 주문과
        조회가 같은 환경을 탄다(PR #379 리뷰 후속).

        뮤테이션: 괴리 판정을 건너뛰고 단가가 있으면 무조건 거부하게 바꾸면 red. 조회 인자의
        종목코드를 ``PDNO``가 아닌 값으로 바꿔도 red. 조회 기본 환경을 ``real``로 되돌려도 red.
        """
        _, price = self._placeholders(mapping_box)

        await self._call(
            "order_cash", {"PDNO": "005930", "ORD_DVSN": "00", "ORD_QTY": "1", "ORD_UNPR": price}
        )

        assert len(remote.orders) == 1
        assert remote.orders[0]["arguments"]["params"]["ORD_UNPR"] == "68900"
        assert [q["arguments"]["params"] for q in remote.quotes] == [
            {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930", "env_dv": "demo"}
        ]
        assert remote.quotes[0]["tool_name"] == "domestic_stock"

    @pytest.mark.parametrize(
        "quote",
        [
            '{"error": "mcp_call_failed", "tool": "domestic_stock"}',
            _quote_fixture("error_kis_rejected"),
            quote_text("005930", "0"),
        ],
        ids=["quote_call_failed", "quote_rejected_by_kis", "quote_without_current_price"],
    )
    async def test_unverifiable_current_price_blocks_the_order(
        self, remote, mapping_box, ledger, quote
    ):
        """fail-closed — 현재가를 확인하지 못하면 정상처럼 보이는 단가도 보내지 않는다.

        뮤테이션: ``parse_current_price``가 ``None``일 때 ``None``(통과)을 돌려주게 바꾸면 red.
        """
        remote.quote = quote

        observation = await self._call(
            "order_cash", {"PDNO": "005930", "ORD_DVSN": "00", "ORD_QTY": "1", "ORD_UNPR": "68900"}
        )

        assert remote.orders == []
        payload = json.loads(observation)
        assert (payload["error"], payload["reason"]) == (ERROR_UNVERIFIABLE, "current_price_unavailable")
        assert payload["hint"]

    async def test_market_order_is_not_gap_checked(self, remote, mapping_box, ledger):
        """backend와 같다 — 시장가(단가 0)는 괴리 판정 대상이 아니다.

        뮤테이션: ``order_unit_prices``가 0도 단가로 세게(``number >= 0``) 바꾸면 괴리 100%로
        거부돼 red.
        """
        await self._call(
            "order_cash", {"PDNO": "005930", "ORD_DVSN": "01", "ORD_QTY": "1", "ORD_UNPR": "0"}
        )

        assert remote.quotes == []
        assert len(remote.orders) == 1

    async def test_unlisted_write_api_type_is_still_checked(self, remote, mapping_box, ledger):
        """주문 TR 이름 목록이 아니라 "읽기 전용이 아니면 주문일 수 있다"로 판정한다.

        뮤테이션: 대상 판정을 ``api_type == "order_cash"``(또는 ``order_`` 접두사) 같은 이름
        목록으로 바꾸면 red.
        """
        total, _ = self._placeholders(mapping_box)

        observation = await self._call(
            "new_write_tr_not_in_any_list", {"PDNO": "005930", "ORD_QTY": "1", "ORD_UNPR": total}
        )

        assert remote.orders == []
        assert json.loads(observation)["error"] == ERROR_GAP_EXCEEDED

    async def test_read_only_api_type_with_a_price_field_is_not_checked(
        self, remote, mapping_box, ledger
    ):
        """``inquire_psbl_order``는 ``ORD_UNPR``를 **입력으로** 받는 조회다 — 주문이 아니다.

        뮤테이션: ``_order_price_gap_rejection``의 읽기 전용 제외를 빼면 괴리 초과로 조회가
        막혀 red.
        """
        await self._call(
            "inquire_psbl_order", {"PDNO": "005930", "ORD_UNPR": "12345000", "ORD_DVSN": "00"}
        )

        assert remote.quotes == []
        assert [c["arguments"]["api_type"] for c in remote.calls] == ["inquire_psbl_order"]

    async def test_product_without_a_quote_contract_is_rejected(self, remote, mapping_box, ledger):
        """기준 현재가를 조회할 수 없는 상품의 단가 있는 주문은 보내지 않는다.

        뮤테이션: ``unsupported_product`` 분기를 빼면 국내주식 현재가(68,900)와 같은 단가로
        비교돼 통과하므로 red.
        """
        observation = await self._call(
            "order",
            {"PDNO": "005930", "ORD_QTY": "1", "OVRS_ORD_UNPR": "68900"},
            tool_name="overseas_stock",
        )

        assert remote.calls == []
        payload = json.loads(observation)
        assert (payload["error"], payload["reason"]) == (ERROR_UNVERIFIABLE, "unsupported_product")

    async def test_missing_stock_code_is_rejected(self, remote, mapping_box, ledger):
        """뮤테이션: ``stock_code_missing`` 분기를 빼면 빈 종목코드로 조회한 대역 현재가와
        비교돼 통과하므로 red."""
        observation = await self._call("order_cash", {"ORD_QTY": "1", "ORD_UNPR": "68900"})

        assert remote.calls == []
        assert json.loads(observation)["reason"] == "stock_code_missing"

    async def test_unreadable_unit_price_is_rejected(self, remote, mapping_box, ledger):
        """뮤테이션: 읽지 못한 단가를 시장가(0)처럼 넘기면 주문이 나가서 red."""
        observation = await self._call(
            "order_cash", {"PDNO": "005930", "ORD_QTY": "1", "ORD_UNPR": "6만9천"}
        )

        assert remote.calls == []
        assert json.loads(observation)["reason"] == "unit_price_not_a_number"

    async def test_threshold_from_env_is_applied_to_the_order(self, remote, mapping_box, ledger, monkeypatch):
        """71,000 / 68,900은 3.05% 괴리 — 기본값(3%)이면 막히고 5%로 넓히면 나간다.

        뮤테이션: 판정에 ``max_price_gap_ratio()`` 대신 기본값 상수를 쓰면 red.
        """
        monkeypatch.setenv("ORDER_MAX_PRICE_GAP_RATIO", "0.05")

        await self._call("order_cash", {"PDNO": "005930", "ORD_QTY": "1", "ORD_UNPR": "71000"})

        assert len(remote.orders) == 1

    async def test_order_env_dv_is_used_for_the_reference_quote(self, remote, mapping_box, ledger):
        """실전 주문이면 기준가도 실전 환경으로 조회한다.

        뮤테이션: 조회 ``env_dv``를 주문 params와 무관한 상수로 바꾸면 red.
        """
        await self._call(
            "order_cash", {"PDNO": "005930", "ORD_QTY": "1", "ORD_UNPR": "68900", "env_dv": "real"}
        )

        assert [q["arguments"]["params"]["env_dv"] for q in remote.quotes] == ["real"]
        assert len(remote.orders) == 1

    # --- 정정·취소 (PR #379 리뷰) -------------------------------------------------------------
    # upstream examples_llm/domestic_stock/order_rvsecncl 시그니처 그대로 싣는다 — PDNO가 없다.

    _RVSECNCL_BASE = {
        "KRX_FWDG_ORD_ORGNO": "06010",
        "ORGN_ODNO": "0000117057",
        "ORD_DVSN": "00",
        "ORD_QTY": "0",
        "QTY_ALL_ORD_YN": "Y",
    }

    async def test_cancel_of_an_original_order_passes_regardless_of_unit_price(
        self, remote, mapping_box, ledger
    ):
        """취소는 새 주문을 만들지 않는다 — 원주문가든 엉뚱한 값이든 단가에 실려도 나간다.

        뮤테이션: ``check_order_price_gap`` 첫머리의 취소 면제를 빼면 종목코드가 없어 거부돼 red.
        """
        observation = await self._call(
            "order_rvsecncl",
            {**self._RVSECNCL_BASE, "RVSE_CNCL_DVSN_CD": "02", "ORD_UNPR": "12345000"},
        )

        assert remote.quotes == []
        assert len(remote.orders) == 1, observation

    async def test_price_amend_without_stock_code_is_rejected_with_a_cancel_and_reorder_hint(
        self, remote, mapping_box, ledger
    ):
        """국내주식 정정 TR에는 PDNO가 없다 — "PDNO를 넣으라"는 안내는 스키마 밖으로 유도한다.

        뮤테이션: ``amend_stock_code_unknown`` 분기를 빼면 ``stock_code_missing``으로 떨어져 red.
        """
        observation = await self._call(
            "order_rvsecncl",
            {**self._RVSECNCL_BASE, "RVSE_CNCL_DVSN_CD": "01", "ORD_UNPR": "70000"},
        )

        assert remote.calls == []
        payload = json.loads(observation)
        assert (payload["error"], payload["reason"]) == (ERROR_UNVERIFIABLE, "amend_stock_code_unknown")
        assert "RVSE_CNCL_DVSN_CD=02" in payload["hint"]
        assert "PDNO를 추가해 재시도하지 말고" in payload["hint"]

    @pytest.mark.parametrize(
        "params",
        [
            {"RVSE_CNCL_DVSN_CD": "02", "ORD_UNPR": "12345000"},
            {"ORGN_ODNO": "0000117057", "RVSE_CNCL_DVSN_CD": "00", "ORD_UNPR": "12345000"},
        ],
        ids=["cancel_code_without_original_order", "code_00_is_not_a_cancel"],
    )
    async def test_cancel_is_recognised_only_with_original_order_and_code_02(
        self, remote, mapping_box, ledger, params
    ):
        """취소 면제가 가격 검사를 우회하는 통로가 되지 않는다.

        ``00``은 해외주식 ``order_resv``가 같은 필드를 신규 주문에 쓰는 값이다.

        뮤테이션: 취소 판정에서 ``ORGN_ODNO`` 요구를 빼면 첫 행이, 코드 대조를 ``!= "01"``로
        느슨하게 하면 둘째 행이 통과해 red.
        """
        observation = await self._call("order_rvsecncl", {"ORD_QTY": "1", **params})

        assert remote.orders == []
        assert json.loads(observation)["error"] == ERROR_UNVERIFIABLE

    async def test_amend_carrying_a_stock_code_is_gap_checked(self, remote, mapping_box, ledger):
        """종목코드가 실린 정정은 일반 주문과 같이 괴리로 판정한다.

        뮤테이션: 원주문번호만 보고 PDNO 유무와 무관하게 ``amend_stock_code_unknown``으로
        거부하게 바꾸면 red.
        """
        observation = await self._call(
            "order_rvsecncl",
            {**self._RVSECNCL_BASE, "PDNO": "005930", "RVSE_CNCL_DVSN_CD": "01", "ORD_UNPR": "12345000"},
        )

        assert remote.orders == []
        assert json.loads(observation)["error"] == ERROR_GAP_EXCEEDED

    # --- 조회 TR 오인 (PR #379 리뷰) ----------------------------------------------------------

    async def test_pension_orderable_inquiry_is_not_gap_checked(self, remote, mapping_box, ledger):
        """퇴직연금 주문가능조회(TR TTTC0503R)는 ``inquire_``로 시작하지 않지만 조회다.

        뮤테이션: ``_READONLY_API_ALLOWLIST_EXACT``에서 ``pension_inquire_psbl_order``를 빼면
        괴리 초과로 조회가 막혀 red.
        """
        await self._call(
            "pension_inquire_psbl_order", {"PDNO": "069500", "ORD_UNPR": "12345000", "ORD_DVSN": "00"}
        )

        assert remote.quotes == []
        assert [c["arguments"]["api_type"] for c in remote.calls] == ["pension_inquire_psbl_order"]

    async def test_quote_condition_flag_is_not_a_unit_price(self, remote, mapping_box, ledger):
        """``FID_ORG_ADJ_PRC``(수정주가 반영 여부 0/1)는 마디가 PRC여도 단가가 아니다.

        뮤테이션: ``order_unit_prices``의 ``FID_*`` 제외를 빼면 종목코드 없는 주문으로 보고
        조회를 막아 red.
        """
        assert order_unit_prices({"FID_ORG_ADJ_PRC": "1", "fid_input_iscd": "005930"}) == ([], [])

        await self._call(
            "investor_trade_by_stock_daily",
            {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": "005930", "fid_org_adj_prc": "1"},
        )

        assert [c["arguments"]["api_type"] for c in remote.calls] == ["investor_trade_by_stock_daily"]
