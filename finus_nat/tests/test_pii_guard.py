"""NAT 내부 도구 결과 마스킹 회귀 테스트 (#231, F-17 / NFR-05).

이슈의 완료 조건 "도구 결과가 마스킹 없이 LLM 컨텍스트에 들어가면 실패"를 고정한다.
세 축이다:

1. **경계 왕복** — 최상위 에이전트가 심은 박스 안에서, 도구가 낸 잔고 원문은
   에이전트에게 마스킹된 채 전달되고 최종 응답에서 원값으로 돌아온다.
2. **우회 차단** — ``finus_api.py``의 도구가 결과를 돌려주면서 ``_record_and_mask``를
   거치지 않는 경로가 생기면 AST 가드가 실패한다.
3. **계층 간섭 없음** — backend(``llm_chat``)가 만든 자리표시자를 NAT 경계가
   건드리지 않는다. 건드리면 backend의 왕복이 깨진다.

실 KIS·OpenAI 연결 없이 동작한다.
"""

import ast
import json
import logging
import pathlib
import re
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from nat.data_models.api_server import (
    ChatRequestOrMessage,
    ChatResponse,
    Usage,
)

from nat_finus_nat.agents import (
    FinusReasoningTraceAgentConfig,
    chat_response_plain_text,
    chat_response_with_text,
    finus_reasoning_trace_agent,
)
from nat_finus_nat import finus_api
from nat_finus_nat.finus_api import DATA_TOOL_LEDGER, DataToolLedger, _record_and_mask
from nat_finus_nat.pii_guard import (
    MASKED_TOOLS,
    PII_MAPPING,
    _SCOPED_PLACEHOLDER_RE,
    install_mapping_box,
    mask_tool_result,
    restore_for_internal,
    unmask_response,
)
from nat_finus_nat.pii_mask import _PLACEHOLDER_RE, mask_pii

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FINUS_API_PATH = _REPO_ROOT / "finus_nat" / "src" / "nat_finus_nat" / "finus_api.py"
_BACKEND_PII_MASK = _REPO_ROOT / "backend" / "pii_mask.py"
_NAT_PII_MASK = _REPO_ROOT / "finus_nat" / "src" / "nat_finus_nat" / "pii_mask.py"
_BALANCE_FIXTURE = _REPO_ROOT / "mcp-trading" / "tests" / "fixtures" / "balance_report.json"


def _is_call_to(node: ast.AST, name: str) -> bool:
    """*node*가 ``name(...)`` 형태의 호출인가."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _module_str_constants(tree: ast.Module) -> dict[str, str]:
    """모듈 최상위의 ``NAME = "문자열"`` 상수 — ``_KIS_BALANCE_LEDGER_NAME`` 같은 것."""
    consts: dict[str, str] = {}
    for node in tree.body:
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign) else
            list(node.targets) if isinstance(node, ast.Assign) else []
        )
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                consts[target.id] = value.value
    return consts


# 래퍼가 "자기 파라미터를 그대로 전달"하는 자리의 이름. 실제 값은 그 래퍼의 호출부
# (``ledger_tool_name=`` 리터럴)에서 수집되므로 미해석으로 세지 않는다. 형제 가드
# ``backend/tests/test_label_drift.py``의 ``_FORWARDED_PARAM_NAMES``와 같은 규약이다 —
# ``_record_to_ledger``의 docstring이 "리터럴이나 모듈 상수로 유지하라"를 계약으로
# 못박아 뒀고, 전달 자리는 그 계약의 유일한 예외다.
_LEDGER_KWARG = "ledger_tool_name"
_FORWARDED_PARAM_NAMES = frozenset({_LEDGER_KWARG})


def _tool_name_exprs(tree: ast.Module) -> list[ast.expr]:
    """도구명이 담기는 표현식을 전부 모은다 — ``_record_and_mask`` 첫 인자 + ``ledger_tool_name=``.

    호출부의 ``ledger_tool_name=``까지 같이 걷어야 파라미터로 전달되는 래퍼
    (``_mcp_news_stock_function_info``)의 **실제 이름**이 집합에 들어온다.
    """
    exprs: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_call_to(node, "_record_and_mask"):
            # 첫 인자를 키워드로 넘긴 형태는 아래 keywords 루프에도 걸리지 않아 조용히
            # 사라진다. 호출 노드 자체를 넣어 미해석으로 드러나게 한다.
            by_kw = [kw.value for kw in node.keywords if kw.arg == "tool_name"]
            exprs.append(node.args[0] if node.args else (by_kw[0] if by_kw else node))
        exprs.extend(kw.value for kw in node.keywords if kw.arg == _LEDGER_KWARG)
    return exprs


def _recorded_tool_names() -> tuple[set[str], list[str]]:
    """``finus_api.py``가 원장·마스킹에 넘기는 (도구 이름 집합, 정적으로 해석 못 한 표현식).

    첫 인자가 문자열 리터럴이면 그대로, 모듈 상수 이름이면 그 값으로 해석한다
    (``_record_and_mask(_KIS_BALANCE_LEDGER_NAME, ...)`` 경로).

    **미해석을 조용히 버리지 않고 돌려준다** (PR #335 리뷰). 버리면 새 계좌 도구가
    도구명을 파라미터로 넘기는 순간 아래 역방향 가드를 그대로 빠져나간다 — 방금 고친
    ``async def`` 사각지대와 같은 종류다.
    """
    tree = ast.parse(_FINUS_API_PATH.read_text(encoding="utf-8"))
    consts = _module_str_constants(tree)
    names: set[str] = set()
    unresolved: list[str] = []
    for expr in _tool_name_exprs(tree):
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            names.add(expr.value)
        elif isinstance(expr, ast.Name) and expr.id in consts:
            names.add(consts[expr.id])
        elif isinstance(expr, ast.Name) and expr.id in _FORWARDED_PARAM_NAMES:
            continue
        else:
            unresolved.append(f"{_FINUS_API_PATH.name}:{expr.lineno}: {ast.unparse(expr)}")
    return names, unresolved


def _request(text: str) -> ChatRequestOrMessage:
    return ChatRequestOrMessage(messages=[{"role": "user", "content": text}])


def _balance_report_text() -> str:
    """#137 공유 픽스처 — ``formatBalanceReport()``의 실제 출력 계약.

    ``finus_mcp_trading_get_balance``가 ReAct 컨텍스트에 그대로 넣는 텍스트다.
    """
    return json.loads(_BALANCE_FIXTURE.read_text(encoding="utf-8"))["normal"]["expected_text"]


@pytest.fixture
def mapping_box():
    """최상위 에이전트가 하는 일(박스 심기)을 테스트 안에서 재현한다."""
    box = install_mapping_box()
    token = PII_MAPPING.set(box)
    try:
        yield box
    finally:
        PII_MAPPING.reset(token)


@pytest.fixture
def ledger():
    """``_record_and_mask``가 원장에 기록할 수 있게 박스를 심는다(#152 경로)."""
    led = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(led)
    try:
        yield led
    finally:
        DATA_TOOL_LEDGER.reset(token)


# ---------------------------------------------------------------------------
# 1. 마스킹 대상 판정
# ---------------------------------------------------------------------------

class TestMaskToolResult:
    def test_balance_report_leaves_no_plaintext_amount_or_quantity(self, mapping_box):
        """잔고 리포트의 금액·수량이 LLM 컨텍스트로 평문 전달되지 않는다 — 이슈의 본문."""
        text = _balance_report_text()
        masked = mask_tool_result("finus_mcp_trading_get_balance", text)

        assert "1,210,000원" not in masked
        assert "67,000원" not in masked
        assert "· 3주" not in masked
        # 마스킹 자체는 pii_mask가 하므로 여기서는 "무엇 하나 남지 않았는가"만 본다.
        assert not re.search(r"[\d,]+원", masked)

        # 상대 비교에 쓰이는 수익률(%)과 종목코드는 그대로 남는다(#230 범위 밖).
        assert "+4.48%" in masked
        assert "005930" in masked

        # 매핑이 박스에 누적되어 역치환할 수 있어야 한다.
        assert mapping_box
        assert unmask_response(masked) == text

    def test_public_information_tools_are_not_masked(self, mapping_box):
        """뉴스·공시·실적은 공개 정보다 — 마스킹하면 얻는 것 없이 분석만 망가진다."""
        text = "삼성전자 3분기 영업이익 9,000,000원 기록, 목표주가 95,000원 상향"
        assert mask_tool_result("finus_market_news", text) == text
        assert mask_tool_result("finus_disclosure_signal", text) == text
        assert mask_tool_result("finus_earnings_report", text) == text
        assert mapping_box == {}

    def test_masked_tool_names_still_exist_in_source(self):
        """``MASKED_TOOLS``의 이름이 ``finus_api.py``에 없으면 실패한다.

        도구 이름이 바뀌면 목록의 문자열이 조용히 죽어 마스킹이 통째로 빠진다. 이름은
        원장 키에서 그대로 읽으므로 실제 소스와 대조해 고정한다.
        """
        recorded, _ = _recorded_tool_names()
        assert MASKED_TOOLS <= recorded, (
            f"MASKED_TOOLS의 {sorted(MASKED_TOOLS - recorded)}가 finus_api.py의 "
            "_record_and_mask 호출에 없습니다. 도구 이름이 바뀌었다면 "
            "pii_guard.MASKED_TOOLS도 함께 고쳐야 마스킹이 계속 걸립니다."
        )

    def test_every_tool_name_is_statically_resolvable(self):
        """도구명을 하나라도 정적으로 못 읽으면 아래 역방향 가드가 공허해지므로 먼저 막는다.

        미해석을 조용히 건너뛰면, 새 계좌 도구가 도구명을 파라미터로 넘기는 순간
        ``test_new_account_tools_are_masked_by_default``를 그대로 빠져나간다 — 방금 고친
        ``async def`` 사각지대와 같은 종류다 (PR #335 리뷰).

        형제 가드 ``backend/tests/test_label_drift.py::``
        ``test_every_ledger_tool_name_is_statically_resolvable``과 같은 규약이다.
        """
        _, unresolved = _recorded_tool_names()
        assert unresolved == [], (
            "finus_api.py의 도구 결과 반환에서 도구명을 정적으로 해석하지 못했습니다. "
            "리터럴이나 모듈 상수로 되돌리거나, 파라미터로 전달한다면 호출부가 "
            f"{_LEDGER_KWARG}= 리터럴로 넘기고 _FORWARDED_PARAM_NAMES에 추가하세요 "
            f"(그대로 두면 마스킹 커버리지 검사가 조용히 비어 갑니다): {unresolved}"
        )

    def test_new_account_tools_are_masked_by_default(self):
        """새 계좌 도구가 ``MASKED_TOOLS``에서 빠지면 실패한다 — fail-closed의 유일한 가드.

        위 테스트는 ``MASKED_TOOLS ⊆ 소스`` 방향이라 **목록에서 빠진 도구**를 잡지 못한다
        (PR #335 리뷰). 이쪽이 반대 방향이다: ``finus_api.py``가 결과를 돌려주는 도구
        가운데 계좌 계열 접두사를 가진 것은 전부 목록에 있어야 한다. 새 계좌 도구를
        추가하면서 목록을 잊으면 여기서 걸린다.

        접두사 판정 근거: ``finus_mcp_trading_*``는 KIS 잔고·손익·주문 조회 계열이고,
        ``finus_account_*``는 Kis Trading MCP pass-through 래퍼다. 둘 다 계좌 자격증명
        채널이므로 예외 없이 마스킹 대상이다. 정말 마스킹하지 않아야 할 계좌 계열 도구가
        생긴다면, 목록이 아니라 이 테스트에 근거를 적고 면제해야 한다.
        """
        account_prefixes = ("finus_mcp_trading_", "finus_account_")
        recorded, _ = _recorded_tool_names()
        account_tools = {name for name in recorded if name.startswith(account_prefixes)}
        assert account_tools, "계좌 계열 도구를 하나도 찾지 못했습니다 — 접두사 규칙을 확인하세요."
        assert account_tools <= MASKED_TOOLS, (
            f"계좌 계열 도구 {sorted(account_tools - MASKED_TOOLS)}가 "
            "pii_guard.MASKED_TOOLS에 없습니다. 계좌 자격증명으로 조회한 결과가 마스킹 없이 "
            "외부 LLM 컨텍스트로 나갑니다(#231)."
        )

    def test_masks_even_without_a_box_and_logs(self, caplog):
        """박스가 없어도 마스킹은 한다(fail-closed) — 유출보다 품질 저하를 택한다."""
        assert PII_MAPPING.get() is None
        with caplog.at_level(logging.ERROR, logger="nat_finus_nat.pii_guard"):
            masked = mask_tool_result("finus_mcp_trading_get_balance", "예수금 1,000,000원")
        assert "1,000,000원" not in masked
        assert "PII_MAPPING box not set" in caplog.text

    def test_ledger_verdict_survives_masking(self, mapping_box, ledger):
        """마스킹을 끼운 뒤에도 빈 결과·데이터 있음 판정(#209)이 그대로여야 한다."""
        empty_report = "보유 종목이 없습니다."
        returned = _record_and_mask("finus_mcp_trading_balance_rlz_pl", empty_report)
        assert returned == empty_report  # 마스킹할 값이 없는 텍스트
        assert ledger.records[-1].empty is True

        returned = _record_and_mask("finus_mcp_trading_get_balance", _balance_report_text())
        assert "1,210,000원" not in returned
        assert ledger.records[-1].produced_rows is True

    def test_empty_result_literals_survive_masking(self):
        """빈 결과 리터럴(#209)이 마스킹 정규식에 걸리기 시작하면 실패한다.

        ``_record_and_mask``는 원장에 원문을 넣고 에이전트에게 마스킹본을 준다. 오늘
        두 텍스트의 판정이 같은 것은 아래 리터럴 중 마스킹 대상 패턴(금액·수량·10자리
        숫자)을 가진 것이 하나도 없기 때문이다. 리터럴에 "…원"이나 "N주"가 들어오면
        그 전제가 깨진다 — 그때는 리터럴을 고치거나 판정 순서를 다시 봐야 한다.
        """
        literals = [*finus_api._EMPTY_RESULT_LITERALS.values(), "보유 종목이 없습니다.", "[계좌 집계]"]
        for literal in literals:
            masked, mapping = mask_pii(literal)
            assert mapping == {} and masked == literal, (
                f"빈 결과 리터럴 {literal!r}이 마스킹 대상 패턴을 갖게 되었습니다 "
                f"(마스킹 결과: {masked!r}). 원장(_has_empty_result)은 원문을 보고 "
                "에이전트는 마스킹본을 보므로, 이대로면 두 판정이 갈립니다."
            )


# ---------------------------------------------------------------------------
# 2. 경계 역치환
# ---------------------------------------------------------------------------

class TestUnmaskResponse:
    def test_restores_values_this_request_masked(self, mapping_box):
        masked = mask_tool_result("finus_mcp_trading_get_balance", "예수금 1,234,000원 보유 5주")
        answer = f"현재 {masked} 입니다."
        assert unmask_response(answer) == "현재 예수금 1,234,000원 보유 5주 입니다."

    def test_leaves_foreign_scope_placeholders_untouched(self, mapping_box):
        """backend가 만든 자리표시자를 건드리면 backend의 왕복이 깨진다.

        backend ``llm_chat``은 NAT을 부르기 전에 ``user_msg``를 자기 매핑으로 마스킹하고
        응답을 받은 뒤 자기 매핑으로 역치환한다. NAT이 이것을 중립 문구로 바꿔 버리면
        사용자는 자기가 방금 쓴 금액 대신 "(이전 금액 1)"을 보게 된다.
        """
        _, backend_mapping = mask_pii("제 예수금 9,876,000원이면 어떨까요?")
        backend_placeholder = next(iter(backend_mapping))

        answer = f"말씀하신 {backend_placeholder} 기준으로는 충분합니다."
        assert unmask_response(answer) == answer

    def test_neutralizes_hallucinated_placeholders_of_this_request(self, mapping_box, caplog):
        """LLM이 이 요청의 scope로 없는 번호를 지어내면 내부 토큰을 노출하지 않는다."""
        mask_tool_result("finus_mcp_trading_get_balance", "예수금 1,234,000원")
        scope = next(iter(mapping_box)).rsplit("_", 2)[-2]
        invented = f"<AMOUNT_{scope}_99>"

        with caplog.at_level(logging.WARNING, logger="nat_finus_nat.pii_guard"):
            restored = unmask_response(f"총 {invented} 입니다.")
        assert invented not in restored
        assert restored == "총 (이전 금액 1) 입니다."
        assert "중립 문구로 치환" in caplog.text

    def test_no_box_means_no_rewrite(self):
        """박스가 없으면(요청 밖) 응답을 손대지 않는다."""
        assert PII_MAPPING.get() is None
        text = "<AMOUNT_abc123_1> 입니다."
        assert unmask_response(text) == text

    def test_scoped_regex_matches_the_same_placeholders_as_pii_mask(self):
        """pii_guard의 정규식과 pii_mask의 정규식이 같은 것을 매치해야 한다.

        한쪽만 종류가 늘면 마스킹은 되는데 역치환이 안 되는(또는 그 반대인) 상태가 된다.
        """
        sample = "<ACCOUNT_9f2a1c_1> <AMOUNT_9f2a1c_2> <QTY_9f2a1c_3> <AMOUNT_1> <AMOUNTX_9f2a1c_1>"
        assert [m.group(0) for m in _SCOPED_PLACEHOLDER_RE.finditer(sample)] == [
            m.group(0) for m in _PLACEHOLDER_RE.finditer(sample)
        ]


# ---------------------------------------------------------------------------
# 3. 우리 저장소로 나가는 방향 — 매매일지
# ---------------------------------------------------------------------------

class TestRestoreForInternal:
    def test_diary_content_is_stored_with_real_values(self, mapping_box):
        """일지 본문에 자리표시자가 그대로 저장되면 요청 종료 후 복구할 수 없다."""
        masked = mask_tool_result("finus_mcp_trading_get_balance", "삼성전자 3주 평가금액 210,000원")
        drafted_by_llm = f"오늘 {masked} 상태로 마감. 다음 주 재평가."

        stored = restore_for_internal(drafted_by_llm)
        assert "<AMOUNT" not in stored and "<QTY" not in stored
        assert stored == "오늘 삼성전자 3주 평가금액 210,000원 상태로 마감. 다음 주 재평가."

    def test_foreign_placeholders_are_left_for_backend(self, mapping_box):
        _, backend_mapping = mask_pii("100,000원 넣었어요")
        backend_placeholder = next(iter(backend_mapping))
        text = f"사용자 입금 {backend_placeholder}"
        assert restore_for_internal(text) == text

    async def test_save_diary_posts_unmasked_content_to_backend(self, monkeypatch, mapping_box, ledger):
        """``finus_save_diary``가 실제로 역치환한 값을 backend에 POST하는지 배선을 확인한다.

        단위 함수만 테스트하면 도구가 그 함수를 부르지 않게 되는 회귀를 놓친다.
        """
        captured: dict[str, object] = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"status": "success", "data": {"id": 1}}

        class FakeClient:
            def __init__(self, timeout):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json):
                captured["json"] = json
                return FakeResponse()

        monkeypatch.setattr(finus_api.httpx, "AsyncClient", FakeClient)

        # 에이전트는 마스킹된 잔고를 보고 일지를 쓴다.
        masked = mask_tool_result("finus_mcp_trading_get_balance", "삼성전자 3주 210,000원")
        config = finus_api.FinusSaveDiaryConfig(backend_url="http://test-backend:8000")
        async with finus_api.finus_save_diary(config, None) as info:
            await info.single_fn(
                finus_api.FinusSaveDiaryInput(title="매매일지", content=f"오늘 {masked}")
            )

        assert captured["json"] == {"title": "매매일지", "content": "오늘 삼성전자 3주 210,000원"}

    async def test_save_diary_response_does_not_echo_plaintext_back_to_llm(
        self, monkeypatch, mapping_box, ledger
    ):
        """저장 응답이 방금 역치환한 본문을 Observation으로 되비추면 실패한다 (PR #335 리뷰).

        backend ``POST /api/v1/db/diary``는 ``{"status": "success", "data": <Diary 전체>}``를
        돌려주고 그 ``data``에는 평문 ``title``·``content``가 들어 있다. 그대로 반환하면
        이 이슈가 막은 평문이 **같은 요청 안에서** 컨텍스트에 재유입돼 다음 턴에 외부
        LLM으로 나간다 — 도구 결과 마스킹 전체가 무의미해지는 경로다.
        """
        plaintext_title = "매매일지 2026-05-24"
        plaintext_content = "삼성전자 3주 210,000원 매수"

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                # backend/main.py::create_db_diary의 실제 응답 모양.
                return {
                    "status": "success",
                    "data": {
                        "id": 7,
                        "title": plaintext_title,
                        "content": plaintext_content,
                        "created_at": "2026-05-24T09:00:00+00:00",
                    },
                }

        class FakeClient:
            def __init__(self, timeout):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json):
                return FakeResponse()

        monkeypatch.setattr(finus_api.httpx, "AsyncClient", FakeClient)

        masked = mask_tool_result("finus_mcp_trading_get_balance", plaintext_content)
        config = finus_api.FinusSaveDiaryConfig(backend_url="http://test-backend:8000")
        async with finus_api.finus_save_diary(config, None) as info:
            observation = await info.single_fn(
                finus_api.FinusSaveDiaryInput(title=plaintext_title, content=masked)
            )

        # (1) 금액·수량이 평문으로 LLM에게 돌아가지 않는다.
        assert "210,000원" not in observation
        assert "3주" not in observation
        # (2) 저장 확인에 필요한 메타데이터는 남는다.
        assert json.loads(observation) == {"id": 7, "created_at": "2026-05-24T09:00:00+00:00"}
        # (3) 원장에는 남는다 — 마스킹은 LLM 컨텍스트로 나가는 값에만 필요하다(#209).
        assert ledger.records[-1].tool_name == "finus_save_diary"

    async def test_save_diary_error_detail_is_masked(self, monkeypatch, mapping_box, ledger):
        """저장 실패 응답이 요청 본문을 되비춰도 평문이 나가지 않는다 (PR #335 리뷰).

        성공 경로는 반환값을 메타데이터로 좁혀 되비춤을 없앴지만, 오류 경로는 그럴 수
        없다 — backend의 422 응답 ``detail``에는 방금 역치환한 본문이 그대로 실린다.
        ``finus_save_diary``가 ``MASKED_TOOLS``에 있어야 이 경로가 덮인다.
        """

        class FakeResponse:
            status_code = 422
            # FastAPI 검증 오류는 입력을 그대로 되비춘다.
            text = '{"detail":[{"loc":["body","content"],"input":"삼성전자 3주 210,000원 매수"}]}'

            def raise_for_status(self):
                raise finus_api.httpx.HTTPStatusError("422", request=None, response=self)

        class FakeClient:
            def __init__(self, timeout):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json):
                return FakeResponse()

        monkeypatch.setattr(finus_api.httpx, "AsyncClient", FakeClient)

        config = finus_api.FinusSaveDiaryConfig(backend_url="http://test-backend:8000")
        async with finus_api.finus_save_diary(config, None) as info:
            observation = await info.single_fn(
                finus_api.FinusSaveDiaryInput(title="매매일지", content="삼성전자 3주 210,000원 매수")
            )

        assert "diary_api_http_error" in observation
        assert "210,000원" not in observation
        assert "3주" not in observation


# ---------------------------------------------------------------------------
# 4. 최상위 에이전트 배선 — 마스킹과 역치환이 한 쌍으로 맞물리는가
# ---------------------------------------------------------------------------

class _FakeInnerAgent:
    """도구를 한 번 부르고 그 결과를 그대로 답변에 싣는 안쪽 에이전트."""

    def __init__(self, tool_name: str, tool_result: str) -> None:
        self._tool_name = tool_name
        self._tool_result = tool_result
        self.seen_by_llm: str | None = None

    async def ainvoke(self, _request):
        # 실제 도구가 하는 일 그대로 — 원장 기록 + 마스킹.
        observation = _record_and_mask(self._tool_name, self._tool_result)
        # ReAct 컨텍스트에 들어가는 Observation이 곧 LLM이 보는 것이다.
        self.seen_by_llm = observation
        return ChatResponse.from_string(f"확인했습니다.\n{observation}", usage=Usage())


@asynccontextmanager
async def _trace_agent_response_fn(inner):
    """등록 함수에서 ``_response_fn``을 꺼낸다 — test_reasoning_trace.py와 같은 방식."""
    builder = MagicMock()
    builder.get_function = AsyncMock(return_value=inner)
    config = FinusReasoningTraceAgentConfig(inner_agent_name="inner")
    async with finus_reasoning_trace_agent(config, builder) as info:
        yield info.single_fn


class TestReasoningTraceAgentWiring:
    """``finus_reasoning_trace_agent``의 ``_response_fn``을 실제로 통과시킨다."""

    async def test_tool_output_is_masked_inward_and_restored_outward(self, ledger):
        text = _balance_report_text()
        inner = _FakeInnerAgent("finus_mcp_trading_get_balance", text)

        async with _trace_agent_response_fn(inner) as fn:
            result = await fn(_request("내 잔고 어때?"))

        # (1) LLM이 본 것에는 평문 금액·수량이 없다 — 이 이슈가 막으려던 것.
        assert inner.seen_by_llm is not None
        assert "1,210,000원" not in inner.seen_by_llm
        assert "· 3주" not in inner.seen_by_llm

        # (2) 사용자에게 돌아가는 응답에는 원값이 그대로 있다 — 왕복 무손실.
        body = chat_response_plain_text(result)
        assert "1,210,000원" in body
        assert "· 3주" in body
        assert "<AMOUNT" not in body and "<QTY" not in body

    async def test_box_does_not_leak_between_requests(self, ledger):
        """요청이 끝나면 박스가 원상복구된다 — 다음 요청이 남의 매핑을 보면 안 된다."""
        inner = _FakeInnerAgent("finus_mcp_trading_get_balance", "예수금 1,111,000원")
        async with _trace_agent_response_fn(inner) as fn:
            await fn(_request("잔고"))
        assert PII_MAPPING.get() is None


class TestChatResponseWithText:
    def test_replaces_body_and_keeps_extra_fields(self):
        """``routed_agent``/``tools_used``(#260)를 잃지 않아야 backend가 각주를 그린다."""
        response = ChatResponse.from_string("원문", usage=Usage()).model_copy(
            update={"routed_agent": "trading_agent", "tools_used": [{"name": "t", "ok": True}]}
        )
        replaced = chat_response_with_text(response, "바뀐 본문")

        assert chat_response_plain_text(replaced) == "바뀐 본문"
        dumped = replaced.model_dump()
        assert dumped["routed_agent"] == "trading_agent"
        assert dumped["tools_used"] == [{"name": "t", "ok": True}]
        # 원본은 건드리지 않는다(model_copy).
        assert chat_response_plain_text(response) == "원문"


# ---------------------------------------------------------------------------
# 5. 우회 차단 · 사본 드리프트
# ---------------------------------------------------------------------------

class TestNoBypass:
    def test_ledger_is_only_recorded_through_record_and_mask(self):
        """``_record_to_ledger``를 직접 부르고 결과를 돌려주는 도구가 다시 생기면 실패한다.

        도구가 결과를 반환하는 자리는 예외 없이 ``_record_and_mask``를 지나야 마스킹이
        걸린다. ``_record_to_ledger``를 직접 부르는 새 경로는 그 계약을 우회하는
        경로이므로 — 정의 그 자체와 ``_record_and_mask`` 안의 호출만 허용한다.
        """
        tree = ast.parse(_FINUS_API_PATH.read_text(encoding="utf-8"))

        # 도구는 전부 ``async def``다. 함수를 훑어 들어가는 대신 **호출 전부를 모으고
        # 허용된 자리(_record_and_mask 정의 안)만 뺀다** — 이렇게 하면 def 종류
        # (FunctionDef/AsyncFunctionDef)나 중첩 깊이, 모듈 최상위 호출까지 한 번에
        # 덮인다. 이전 판은 ``ast.FunctionDef``만 봐서 async 도구 전부가 사각지대였다
        # (PR #335 리뷰, 실측: 사정권에 든 것은 sync 함수 하나뿐이었다).
        allowed = {
            inner.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_record_and_mask"
            for inner in ast.walk(node)
            if _is_call_to(inner, "_record_to_ledger")
        }
        offenders = sorted(
            {node.lineno for node in ast.walk(tree) if _is_call_to(node, "_record_to_ledger")}
            - allowed
        )

        assert offenders == [], (
            f"finus_api.py:{offenders} 가 _record_to_ledger를 직접 호출합니다. "
            "도구 결과를 돌려주는 자리는 _record_and_mask를 거쳐야 마스킹(#231)이 걸립니다."
        )


class TestVendoredCopy:
    def test_nat_copy_is_byte_identical_to_backend(self):
        """``pii_mask.py``의 두 사본이 어긋나면 실패한다.

        NAT은 backend를 의존성으로 갖지 않고 NAT 이미지도 backend를 COPY하지 않으므로
        (``finus_nat/Dockerfile``) import로 공유할 수 없어 복제했다. 한쪽만 고치면 같은
        잔고 텍스트가 backend 경로와 NAT 경로에서 다르게 마스킹돼, 한 대화 안에서
        자리표시자 규약이 갈린다.
        """
        assert _NAT_PII_MASK.read_bytes() == _BACKEND_PII_MASK.read_bytes(), (
            "backend/pii_mask.py 와 finus_nat/src/nat_finus_nat/pii_mask.py 가 다릅니다. "
            "한쪽을 고쳤다면 `cp backend/pii_mask.py finus_nat/src/nat_finus_nat/pii_mask.py` 로 "
            "사본을 맞추세요."
        )

    def test_vendored_copy_uses_only_the_standard_library(self):
        """복제가 성립하는 전제 — 새 의존성이 들어오면 NAT 이미지에서 import가 깨진다."""
        tree = ast.parse(_NAT_PII_MASK.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])

        assert roots <= {"__future__", "logging", "re", "secrets", "typing"}, (
            f"pii_mask.py가 stdlib 밖의 모듈을 import합니다: {sorted(roots)}. "
            "이 모듈은 finus_nat으로 그대로 복제되므로 stdlib만 써야 합니다."
        )
