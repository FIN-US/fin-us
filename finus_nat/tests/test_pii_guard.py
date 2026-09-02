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

    def test_every_account_data_tool_is_covered(self):
        """계좌 자격증명으로 조회하는 도구가 목록에서 빠지면 실패한다.

        도구 이름은 ``finus_api.py``의 원장 키에서 그대로 읽는다 — 도구 이름이 바뀌면
        ``MASKED_TOOLS``의 문자열이 조용히 죽으므로, 실제 소스와 대조해 고정한다.
        """
        source = _FINUS_API_PATH.read_text(encoding="utf-8")
        for name in MASKED_TOOLS:
            assert f'"{name}"' in source, (
                f"MASKED_TOOLS의 {name!r}가 finus_api.py에 없습니다. 도구 이름이 바뀌었다면 "
                "pii_guard.MASKED_TOOLS도 함께 고쳐야 마스킹이 계속 걸립니다."
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

        offenders: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name == "_record_and_mask":
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "_record_to_ledger"
                ):
                    offenders.append(inner.lineno)

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
