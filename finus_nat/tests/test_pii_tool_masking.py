"""#231: NAT 도구 결과 PII 마스킹 경계 고정 테스트.

이 스위트가 지키는 계약은 셋이다.

1. **마스킹 경계** — 계좌 범위 도구의 결과는 마스킹된 채로 에이전트(=LLM 컨텍스트)에
   들어간다. 여기가 뚫리면 잔고가 평문으로 api.openai.com에 도달한다.
2. **복원 경계** — 자리표시자는 최상위 워크플로 응답과 도구 인자에서만 원값으로
   돌아온다. 여기가 뚫리면 사용자가 ``<AMOUNT_9f2a1c_1>``을 읽게 된다.
3. **fail-closed** — 마스킹 엔진을 쓸 수 없으면 원본 대신 오류를 반환한다.

실 KIS·OpenAI·backend 연결 없이 동작한다. MCP 왕복만 가짜로 갈아끼운다.
"""

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from nat.data_models.api_server import ChatRequestOrMessage, ChatResponse, Usage

from nat_finus_nat import pii
from nat_finus_nat.agents import (
    FinusReasoningTraceAgentConfig,
    _trace_branch_answer,
    _trace_ledger_tools,
    _trace_route,
    finus_reasoning_trace_agent,
    without_pii_placeholders,
)
from nat_finus_nat.finus_api import (
    DATA_TOOL_LEDGER,
    DataToolLedger,
    FinusAccountBalanceConfig,
    FinusListDiariesConfig,
    FinusMcpTradingBalanceRlzPlConfig,
    FinusMcpTradingGetBalanceConfig,
    FinusMcpTradingGetBalanceInput,
    FinusMcpTradingStockNameInput,
    FinusListDiariesInput,
    FinusMarketNewsConfig,
    FinusSaveDiaryConfig,
    FinusSaveDiaryInput,
    _ERROR_JSON_PREFIX_RE,
    _call_kis_mcp_and_record,
    _record_to_ledger,
    finus_list_diaries,
    finus_market_news,
    finus_mcp_trading_balance_rlz_pl,
    finus_mcp_trading_get_balance,
    finus_save_diary,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FINUS_API_SOURCE = Path(__file__).resolve().parents[1] / "src" / "nat_finus_nat" / "finus_api.py"

#: mcp-trading/backend가 공유하는 잔고 리포트 픽스처(#137). 오늘 실제로 LLM 컨텍스트에
#: 실리는 문자열이 이것이므로, 합성 문자열 대신 이 값으로 경계를 고정한다.
_BALANCE_FIXTURE = json.loads(
    (_REPO_ROOT / "mcp-trading" / "tests" / "fixtures" / "balance_report.json").read_text(
        encoding="utf-8"
    )
)
BALANCE_REPORT: str = _BALANCE_FIXTURE["normal"]["expected_text"]

#: 잔고 리포트에서 반드시 가려져야 하는 값들 — 이슈 #231이 열거한 유출 대상
#: (예수금·총평가금액·평가금액·평단가·보유수량).
_MUST_NOT_LEAK = (
    "1,210,000원",  # 총 평가금액 / 순자산금액
    "1,000,000원",  # 예수금
    "210,000원",    # 평가금액 / 금일 매수
    "67,000원",     # 평단가
    "201,000원",    # 평단가
    "200,500원",    # 평가금액
    "3주",          # 보유 수량
    "1주",          # 보유 수량
)

#: 실현손익 리포트의 "[계좌 집계]" 블록에는 실제 계좌번호가 실린다
#: (finus_api._has_empty_result 주석 참고).
RLZ_PL_REPORT = (
    "[실현손익]\n- 삼성전자 (005930) · 3주\n  실현손익 9,000원\n\n"
    "[계좌 집계]\n- 계좌번호: 12345678-01\n- 예수금: 1,000,000원"
)

_PLACEHOLDER_RE = re.compile(r"<(ACCOUNT|AMOUNT|QTY)_[0-9a-f]{6}_\d+>")


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pii_session():
    """모든 테스트에 최상위 워크플로가 심는 것과 같은 세션 상자를 준다."""
    session = pii.PiiSession()
    token = pii.PII_SESSION.set(session)
    yield session
    pii.PII_SESSION.reset(token)


@pytest.fixture(autouse=True)
def _ledger():
    """원장 부재 ERROR 로그가 테스트 출력을 덮지 않도록 상자를 심는다."""
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    yield ledger
    DATA_TOOL_LEDGER.reset(token)


def _stub_mcp_call(monkeypatch, result: str) -> list[dict]:
    """stdio MCP 왕복을 가짜로 갈아끼우고, 실제로 전달된 인자를 수집해 돌려준다."""
    seen: list[dict] = []

    async def _fake(*, vendor_root, subdir, tool_name, arguments, timeout_sec):  # noqa: ARG001
        seen.append(dict(arguments))
        return result

    monkeypatch.setattr("nat_finus_nat.finus_api._mcp_call_tool", _fake)
    return seen


def _stub_remote_mcp_call(monkeypatch, result: str) -> list[dict]:
    seen: list[dict] = []

    async def _fake(*, transport, url, tool_name, arguments, timeout_sec):  # noqa: ARG001
        seen.append(dict(arguments))
        return result

    monkeypatch.setattr("nat_finus_nat.finus_api._mcp_call_tool_remote", _fake)
    return seen


async def _single_fn(register_gen, config, builder=None):
    """등록 제너레이터에서 실제 도구 함수 하나를 꺼낸다."""
    async with register_gen(config, builder or MagicMock()) as info:
        return info.single_fn


def _assert_masked(text: str) -> None:
    for raw in _MUST_NOT_LEAK:
        assert raw not in text, f"마스킹되지 않은 값이 LLM 경계를 넘었습니다: {raw!r}"
    assert _PLACEHOLDER_RE.search(text), "자리표시자가 하나도 없습니다 — 마스킹이 적용되지 않았습니다."


# ---------------------------------------------------------------------------
# 1. 마스킹 경계 — 계좌 데이터가 마스킹된 채로 LLM 컨텍스트에 들어간다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_balance_report_crosses_the_llm_boundary_masked(monkeypatch):
    """이슈 #231의 본체: get_balance 결과가 평문으로 ReAct 컨텍스트에 들어가면 red."""
    _stub_mcp_call(monkeypatch, BALANCE_REPORT)
    fn = await _single_fn(finus_mcp_trading_get_balance, FinusMcpTradingGetBalanceConfig())

    observation = await fn(FinusMcpTradingGetBalanceInput())

    _assert_masked(observation)
    # 종목명·수익률은 남는다 — F-17 대상이 아니고, 가리면 분석이 불가능해진다.
    assert "삼성전자" in observation
    assert "+4.48%" in observation


@pytest.mark.asyncio
async def test_account_number_in_rlz_pl_report_is_masked(monkeypatch):
    _stub_mcp_call(monkeypatch, RLZ_PL_REPORT)
    fn = await _single_fn(finus_mcp_trading_balance_rlz_pl, FinusMcpTradingBalanceRlzPlConfig())

    observation = await fn(FinusMcpTradingStockNameInput())

    assert "12345678-01" not in observation
    assert "<ACCOUNT_" in observation


@pytest.mark.asyncio
async def test_diary_list_is_masked(monkeypatch):
    """매매일지는 사용자가 적은 자기 거래 기록이라 금액이 그대로 들어 있다."""

    class _Resp:
        @staticmethod
        def raise_for_status() -> None: ...

        @staticmethod
        def json() -> dict:
            return {"status": "success", "data": [{"content": "삼성전자 3주 210,000원에 매수"}]}

    monkeypatch.setattr(
        "nat_finus_nat.finus_api.httpx.AsyncClient",
        lambda **_: _async_client_stub(get=_Resp()),
    )
    fn = await _single_fn(finus_list_diaries, FinusListDiariesConfig())

    observation = await fn(FinusListDiariesInput())

    assert "210,000원" not in observation
    assert "3주" not in observation
    assert "<AMOUNT_" in observation


@pytest.mark.asyncio
async def test_public_news_result_is_not_masked(monkeypatch):
    """뉴스·공시는 공개 정보다. 가리면 분석만 망가지고 막히는 유출은 없다."""
    news = "삼성전자 목표주가 95,000원으로 상향"
    _stub_mcp_call(monkeypatch, news)
    fn = await _single_fn(finus_market_news, FinusMarketNewsConfig())

    observation = await fn("삼성전자")

    assert observation == news


# ---------------------------------------------------------------------------
# 2. KIS pass-through — 한 도구 안에서 api_type으로 계좌/시세를 가른다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "api_type,account_scoped",
    [
        ("inquire_balance", True),
        ("inquire_account_balance", True),
        ("inquire_present_balance", True),
        ("inquire_psbl_order", True),
        ("inquire_daily_ccld", True),
        ("order_cash", True),
        ("", True),
        ("brand_new_kis_tr", True),  # 모르는 것은 마스킹한다(fail-closed)
        ("inquire_price", False),
        ("inquire_asking_price_exp_ccn", False),
        ("inquire_daily_itemchartprice", False),
        ("inquire_investor", False),
        ("volume_rank", False),
        ("search_stock_info", False),
        ("find_api_detail", False),
    ],
)
def test_kis_api_type_policy(api_type, account_scoped):
    assert pii.kis_api_type_is_account_scoped(api_type) is account_scoped


@pytest.mark.asyncio
async def test_kis_balance_lookup_is_masked(monkeypatch):
    _stub_remote_mcp_call(monkeypatch, BALANCE_REPORT)

    result = await _call_kis_mcp_and_record(
        "domestic_stock",
        {"api_type": "inquire_balance", "params": {}},
        FinusAccountBalanceConfig(),
    )

    _assert_masked(result)


@pytest.mark.asyncio
async def test_kis_price_lookup_keeps_public_market_data(monkeypatch):
    """현재가는 PII가 아니다 — 가리면 가장 흔한 질문에 답할 수 없게 된다."""
    quote = "삼성전자 현재가 75,300원 (+1.2%)"
    _stub_remote_mcp_call(monkeypatch, quote)

    result = await _call_kis_mcp_and_record(
        "domestic_stock",
        {"api_type": "inquire_price", "params": {}},
        FinusAccountBalanceConfig(),
    )

    assert result == quote


@pytest.mark.asyncio
async def test_unknown_kis_api_type_is_masked(monkeypatch):
    """허용 목록에 없는 api_type은 계좌 TR로 간주한다(fail-closed)."""
    _stub_remote_mcp_call(monkeypatch, BALANCE_REPORT)

    result = await _call_kis_mcp_and_record(
        "domestic_stock",
        {"api_type": "inquire_some_future_account_tr", "params": {}},
        FinusAccountBalanceConfig(),
    )

    _assert_masked(result)


# ---------------------------------------------------------------------------
# 3. fail-closed — 마스킹이 불가능하면 원본을 내보내지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_masking_failure_blocks_the_result_instead_of_leaking(monkeypatch):
    """엔진을 못 찾으면 잔고 대신 오류가 나간다. 유출보다 조회 실패가 낫다."""
    monkeypatch.setattr(pii, "_MASKER", None)
    monkeypatch.setenv(pii._PATH_ENV, str(_REPO_ROOT / "does" / "not" / "exist.py"))
    _stub_mcp_call(monkeypatch, BALANCE_REPORT)
    fn = await _single_fn(finus_mcp_trading_get_balance, FinusMcpTradingGetBalanceConfig())

    observation = await fn(FinusMcpTradingGetBalanceInput())

    for raw in _MUST_NOT_LEAK:
        assert raw not in observation
    assert json.loads(observation)["error"] == "pii_masking_unavailable"


def test_fail_closed_payload_is_shaped_like_other_tool_errors():
    """원장(_record_to_ledger)과 에이전트가 오류로 인식할 수 있어야 한다."""
    payload = pii.masking_unavailable_error_json("finus_mcp_trading_get_balance", "boom")
    assert _ERROR_JSON_PREFIX_RE.match(payload)


def test_missing_session_still_masks(caplog):
    """세션 상자가 없어도 원값을 흘리지 않는다 — 복원 불가를 감수하고 마스킹한다."""
    token = pii.PII_SESSION.set(None)
    try:
        with caplog.at_level("ERROR"):
            masked = pii.mask_account_text("예수금 1,000,000원")
    finally:
        pii.PII_SESSION.reset(token)

    assert "1,000,000원" not in masked
    assert any("PII_SESSION" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# 4. 복원 경계 — 자리표시자는 최상위 응답과 도구 인자에서만 돌아온다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_restores_placeholders_in_the_user_facing_answer(monkeypatch, tmp_path):
    """도구 -> 마스킹 -> LLM -> 최상위 응답 왕복. 사용자에게는 원값이 나간다."""
    _stub_mcp_call(monkeypatch, BALANCE_REPORT)
    balance_fn = await _single_fn(finus_mcp_trading_get_balance, FinusMcpTradingGetBalanceConfig())
    seen_by_llm: list[str] = []

    async def inner(_request):
        # 브랜치가 도구를 부르고, 그 관측(Observation)을 인용해 답변을 만든다.
        observation = await balance_fn(FinusMcpTradingGetBalanceInput())
        seen_by_llm.append(observation)
        _record_to_ledger("finus_mcp_trading_get_balance", observation)
        answer = "예수금은 " + observation.split("- 예수금: ")[1].split("\n")[0] + " 입니다."
        _trace_branch_answer(answer)
        return ChatResponse.from_string(answer, usage=Usage())

    inner_fn = MagicMock()
    inner_fn.ainvoke = AsyncMock(side_effect=inner)
    builder = MagicMock()
    builder.get_function = AsyncMock(return_value=inner_fn)

    async with finus_reasoning_trace_agent(
        FinusReasoningTraceAgentConfig(inner_agent_name="inner"), builder
    ) as info:
        result = await info.single_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "내 잔고 어때?"}])
        )

    # LLM이 본 것은 마스킹된 텍스트다.
    _assert_masked(seen_by_llm[0])
    # 사용자가 받는 것은 원값이다.
    assert result.choices[0].message.content == "예수금은 1,000,000원 입니다."


@pytest.mark.asyncio
async def test_restore_runs_after_the_footnote_check(monkeypatch):
    """복원이 각주 부착보다 먼저면 본문 비교가 어긋나 각주가 통째로 사라진다(#294)."""
    _stub_mcp_call(monkeypatch, BALANCE_REPORT)
    balance_fn = await _single_fn(finus_mcp_trading_get_balance, FinusMcpTradingGetBalanceConfig())

    async def inner(_request):
        ledger = DataToolLedger()
        token = DATA_TOOL_LEDGER.set(ledger)
        try:
            observation = await balance_fn(FinusMcpTradingGetBalanceInput())
            _record_to_ledger("finus_mcp_trading_get_balance", observation)
        finally:
            DATA_TOOL_LEDGER.reset(token)
        _trace_route("trading_agent")
        _trace_ledger_tools(ledger)
        answer = "예수금은 " + observation.split("- 예수금: ")[1].split("\n")[0] + " 입니다."
        _trace_branch_answer(answer)
        return ChatResponse.from_string(answer, usage=Usage())

    inner_fn = MagicMock()
    inner_fn.ainvoke = AsyncMock(side_effect=inner)
    builder = MagicMock()
    builder.get_function = AsyncMock(return_value=inner_fn)

    async with finus_reasoning_trace_agent(
        FinusReasoningTraceAgentConfig(inner_agent_name="inner"), builder
    ) as info:
        result = await info.single_fn(
            ChatRequestOrMessage(messages=[{"role": "user", "content": "내 잔고 어때?"}])
        )

    dumped = result.model_dump()
    assert dumped["tools_used"] == [
        {"name": "finus_mcp_trading_get_balance", "ok": True, "empty": False}
    ]


def test_restore_preserves_reasoning_trace_fields():
    session = pii.PiiSession()
    masked = session.mask("예수금 1,000,000원")
    response = ChatResponse.from_string(masked, usage=Usage()).model_copy(
        update={"routed_agent": "trading_agent", "tools_used": []}
    )

    restored = without_pii_placeholders(response, session)

    dumped = restored.model_dump()
    assert dumped["choices"][0]["message"]["content"] == "예수금 1,000,000원"
    assert dumped["routed_agent"] == "trading_agent"


def test_sessions_do_not_share_mappings_across_requests():
    """앞 요청의 매핑으로 다음 요청의 자리표시자가 복원되면 조용한 오답이 된다."""
    first = pii.PiiSession()
    masked = first.mask("예수금 1,000,000원")

    second = pii.PiiSession()
    restored = second.unmask(masked)

    assert "1,000,000원" not in restored
    assert "(이전 금액 1)" in restored  # pii_mask의 fail-open 중립 문구


# ---------------------------------------------------------------------------
# 5. 나가는 쪽 — LLM이 만든 도구 인자는 원값으로 되돌린다
# ---------------------------------------------------------------------------


def _async_client_stub(*, get=None, post=None):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if get is not None:
        client.get = AsyncMock(return_value=get)
    if post is not None:
        client.post = AsyncMock(return_value=post)
    return client


@pytest.mark.asyncio
async def test_mcp_arguments_are_restored_before_the_call(monkeypatch, _pii_session):
    """자리표시자가 그대로 MCP로 나가면 주문·조회 파라미터가 내부 토큰이 된다.

    복원 지점은 실제 세션이 열리는 ``_mcp_call_tool_remote`` 안이므로, MCP 세션만
    가짜로 갈아끼워 ``call_tool``이 실제로 받은 인자를 본다.
    """
    masked = _pii_session.mask("1,000주")
    seen: list[dict] = []

    @asynccontextmanager
    async def _fake_session(*, transport, url, operation_timeout):  # noqa: ARG001
        async def _call_tool(tool_name, arguments):  # noqa: ARG001
            seen.append(arguments)
            return SimpleNamespace(content=[SimpleNamespace(text="ok")])

        yield SimpleNamespace(call_tool=_call_tool)

    monkeypatch.setattr("nat_finus_nat.finus_api._remote_mcp_session", _fake_session)

    await _call_kis_mcp_and_record(
        "domestic_stock",
        {"api_type": "inquire_price", "params": {"qty": masked}},
        FinusAccountBalanceConfig(),
    )

    assert seen[0]["params"]["qty"] == "1,000주"


@pytest.mark.asyncio
async def test_diary_content_is_restored_before_it_is_persisted(monkeypatch, _pii_session):
    """마스킹된 본문이 그대로 저장되면 사용자가 나중에 읽는 일지가 토큰이 된다."""
    masked_content = _pii_session.mask("삼성전자 3주를 210,000원에 매수")
    captured: dict = {}

    class _Resp:
        @staticmethod
        def raise_for_status() -> None: ...

        @staticmethod
        def json() -> dict:
            return {"status": "success", "data": {"id": 1}}

    client = _async_client_stub(post=_Resp())

    async def _post(url, json=None):  # noqa: A002
        captured["payload"] = json
        return _Resp()

    client.post = AsyncMock(side_effect=_post)
    monkeypatch.setattr("nat_finus_nat.finus_api.httpx.AsyncClient", lambda **_: client)
    fn = await _single_fn(finus_save_diary, FinusSaveDiaryConfig())

    await fn(FinusSaveDiaryInput(title="매매일지", content=masked_content))

    assert captured["payload"]["content"] == "삼성전자 3주를 210,000원에 매수"


def test_outbound_restore_leaves_unknown_placeholders_alone():
    """기계가 읽는 값에는 '(이전 금액 1)' 같은 한국어 대체를 넣지 않는다."""
    session = pii.PiiSession()
    session.mask("1,000,000원")
    token = pii.PII_SESSION.set(session)
    try:
        restored = pii.restore_outbound({"qty": "<QTY_abcdef_9>"})
    finally:
        pii.PII_SESSION.reset(token)

    assert restored == {"qty": "<QTY_abcdef_9>"}


# ---------------------------------------------------------------------------
# 6. 기존 계약 비회귀 — 원장(#152/#209)은 원문으로 판정한다
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_still_sees_a_successful_read(monkeypatch, _ledger):
    """마스킹이 원장 판정을 바꾸면 도구 강제 게이트가 헛돌며 조회를 두 번 한다."""
    _stub_mcp_call(monkeypatch, BALANCE_REPORT)
    fn = await _single_fn(finus_mcp_trading_get_balance, FinusMcpTradingGetBalanceConfig())

    await fn(FinusMcpTradingGetBalanceInput())

    assert _ledger.any_success() is True
    assert _ledger.records[-1].empty is False


@pytest.mark.asyncio
async def test_ledger_still_detects_an_empty_result(monkeypatch, _ledger):
    _stub_mcp_call(monkeypatch, "보유 종목이 없습니다.")
    fn = await _single_fn(finus_mcp_trading_balance_rlz_pl, FinusMcpTradingBalanceRlzPlConfig())

    await fn(FinusMcpTradingStockNameInput())

    assert _ledger.only_empty_reads() is True


# ---------------------------------------------------------------------------
# 7. 드리프트 가드 — 마스킹 대상 목록과 실제 도구 이름이 어긋나지 않게
# ---------------------------------------------------------------------------


def test_account_scoped_tools_are_real_tool_names():
    """오타 하나로 마스킹이 조용히 꺼지는 것을 막는다.

    :func:`_mask_account_tool_result`는 이름이 목록에 없으면 그냥 통과시킨다 —
    잘못 적은 이름은 예외가 아니라 평문 유출로 나타난다.
    """
    source = _FINUS_API_SOURCE.read_text(encoding="utf-8")
    for tool_name in pii.ACCOUNT_SCOPED_TOOLS:
        assert f'_record_to_ledger("{tool_name}"' in source, (
            f"{tool_name}은 finus_api.py의 원장 기록 이름과 일치하지 않습니다."
        )
        assert f'_mask_account_tool_result("{tool_name}"' in source, (
            f"{tool_name}이 ACCOUNT_SCOPED_TOOLS에 있지만 마스킹 호출부가 없습니다."
        )


def test_public_market_allowlist_has_no_account_tr():
    """시세 허용 목록에 계좌 TR 접두사가 섞이면 그 순간 평문으로 나간다."""
    forbidden = ("inquire_balance", "inquire_account", "inquire_psbl", "inquire_daily_ccld", "order")
    for prefix in pii._PUBLIC_MARKET_API_PREFIXES:
        assert not any(prefix.startswith(bad) for bad in forbidden), prefix
