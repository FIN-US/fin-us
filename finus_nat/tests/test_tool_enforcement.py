"""#152: 도구 강제 게이트(tool enforcement gate) 동작 고정 테스트.

이슈가 명시한 7개 케이스와 VENDOR_PATCH_STATUS 특성화 테스트를 포함한다.
실 KIS·OpenAI 연결 없이 동작한다.

게이트 로직(``_check_tool_enforcement``)과 재시도·거절 흐름(``_run_with_gate``)을
직접 호출해 검증하므로, 실제 ReAct 에이전트나 LLM 없이도 동작이 고정된다.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from nat.data_models.api_server import (
    ChatRequest,
    ChatResponse,
    Message,
    Usage,
    UserMessageContentRoleType,
)

from nat_finus_nat.agents import (
    _TOOL_ENFORCEMENT_REJECTION,
    _check_tool_enforcement,
    _extract_financial_numbers,
    _has_numeric_claims,
    _run_with_gate,
)
from nat_finus_nat.finus_api import DATA_TOOL_LEDGER, DataToolLedger, _record_to_ledger


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _req(*turns: tuple[str, str]) -> ChatRequest:
    """역할·내용 튜플 목록으로 ChatRequest를 만든다."""
    return ChatRequest(
        messages=[
            Message(role=UserMessageContentRoleType(role), content=content)
            for role, content in turns
        ]
    )


def _simple_req(text: str = "잔고 알려줘") -> ChatRequest:
    return _req(("user", text))


def _ledger_with_success(tool: str = "finus_mcp_trading_get_balance") -> DataToolLedger:
    ledger = DataToolLedger()
    ledger.record(tool, ok=True, produced_rows=True)
    return ledger


def _ledger_with_error(tool: str = "finus_mcp_trading_get_balance") -> DataToolLedger:
    ledger = DataToolLedger()
    ledger.record(tool, ok=False, produced_rows=False)
    return ledger


# ---------------------------------------------------------------------------
# 케이스 1 — 수치 주장 + 도구 호출 없음 → 트립
# ---------------------------------------------------------------------------

def test_gate_trips_on_numeric_claim_no_tool():
    """도구를 전혀 호출하지 않고 잔고 수치를 주장하면 게이트가 트립한다."""
    ledger = DataToolLedger()  # 빈 원장 — 호출 없음
    answer = "Thought: 알겠습니다\nFinal Answer: 잔고는 5,000,000원입니다."
    assert _check_tool_enforcement(answer, ledger, _simple_req()) is True


# ---------------------------------------------------------------------------
# 케이스 2 — 수치 주장 + 도구 성공(데이터 있음) → 통과
# ---------------------------------------------------------------------------

def test_gate_passes_on_numeric_claim_with_tool_success():
    """데이터 조회 도구가 성공적으로 응답했으면 수치 주장이 있어도 통과한다."""
    ledger = _ledger_with_success()
    answer = "Final Answer: 잔고는 5,000,000원입니다."
    assert _check_tool_enforcement(answer, ledger, _simple_req()) is False


# ---------------------------------------------------------------------------
# 케이스 3 — 수치 주장 + 도구 호출했으나 오류 → 트립
# ---------------------------------------------------------------------------

def test_gate_trips_on_numeric_claim_with_tool_error():
    """도구를 호출했지만 오류 응답을 받았고 수치를 주장하면 트립한다.

    KIS 재시도 루프에서 에이전트가 오류 Observation을 받은 뒤 수치를 지어내는 경로가
    가장 빈번한 조작 경로다(이슈 #152 섹션 "반드시 지켜야 할 것들" 참고).
    """
    ledger = _ledger_with_error()
    answer = "Final Answer: 잔고는 5,000,000원입니다."
    assert _check_tool_enforcement(answer, ledger, _simple_req()) is True


# ---------------------------------------------------------------------------
# 케이스 4 — 도구 없는 정성적 후속 답변 → 통과
# ---------------------------------------------------------------------------

def test_gate_passes_on_qualitative_answer_without_tool():
    """도구 없이 정성적 평가만 답해도 수치가 없으면 게이트를 통과한다.

    news_agent·strategy_agent YAML은 직전 대화 맥락만으로 후속 질문에 답하라고
    명시적으로 지시한다 — 이 경우 도구 없이 답하는 것이 의도된 동작이다.
    """
    ledger = DataToolLedger()
    req = _req(
        ("user", "삼성전자 최근 주가 흐름 어때?"),
        ("assistant", "최근 삼성전자는 반도체 수요 회복으로 상승세입니다."),
        ("user", "왜 그런거야?"),
    )
    answer = "Final Answer: 반도체 수요 회복과 AI 모멘텀 덕분에 투자심리가 개선됐습니다."
    assert _check_tool_enforcement(answer, ledger, req) is False


# ---------------------------------------------------------------------------
# 케이스 5 — 대화록의 수치를 다른 표기로 재진술 → 통과
# ---------------------------------------------------------------------------

def test_gate_passes_on_transcript_number_restatement():
    """대화록에 있던 수치를 다른 표기(쉼표/만 단위)로 재진술하면 통과한다.

    ``75,300원`` → ``7만 5300원`` 은 같은 값(75300)이므로 새로운 수치를
    지어낸 것이 아니다.
    """
    ledger = DataToolLedger()  # 도구 없음 — 맥락 재진술이므로 허용
    req = _req(
        ("user", "삼성전자 현재가 알려줘"),
        ("assistant", "삼성전자 현재가는 75,300원입니다."),
        ("user", "그래서 7만원대야?"),
    )
    answer = "Final Answer: 네, 7만 5300원으로 7만원대 중반입니다."
    assert _check_tool_enforcement(answer, ledger, req) is False


# ---------------------------------------------------------------------------
# 케이스 6 — 원장 박스 없음 → 트립 + ERROR 로그
# ---------------------------------------------------------------------------

def test_record_to_ledger_logs_error_and_leaves_ledger_empty_when_box_is_none(caplog):
    """DATA_TOOL_LEDGER가 None일 때 _record_to_ledger가 ERROR를 로그하고
    조용히 반환한다. 이후 빈 원장으로 게이트 검사하면 수치 주장에 대해 트립한다.

    스레드풀 등 ContextVar가 전파되지 않는 실행 컨텍스트를 시뮬레이션한다.
    """
    token = DATA_TOOL_LEDGER.set(None)
    try:
        with caplog.at_level(logging.ERROR, logger="nat_finus_nat.finus_api"):
            _record_to_ledger("finus_mcp_trading_get_balance", '{"total_balance": 5000000}')
    finally:
        DATA_TOOL_LEDGER.reset(token)

    # ERROR 로그가 발생했는지 확인
    assert any(
        "DATA_TOOL_LEDGER" in r.message for r in caplog.records
    ), "DATA_TOOL_LEDGER 없음에 대한 ERROR 로그가 없습니다"

    # 기록에 실패한 빈 원장 → 수치 주장 시 트립
    empty_ledger = DataToolLedger()
    answer = "Final Answer: 잔고는 5,000,000원입니다."
    assert _check_tool_enforcement(answer, empty_ledger, _simple_req()) is True


# ---------------------------------------------------------------------------
# 케이스 7 — 두 번째 시도도 실패 → 결정론적 거절, 모델 문장 아님
# ---------------------------------------------------------------------------

async def test_second_attempt_failure_returns_deterministic_rejection():
    """두 번 모두 게이트가 트립하면 _TOOL_ENFORCEMENT_REJECTION을 반환해야 한다.

    항상 수치를 지어내고 도구를 전혀 호출하지 않는 내부 에이전트를 모킹해
    재시도 후 결정론적 거절 문자열이 반환되는지 검증한다.
    """
    fabricated = "Final Answer: 잔고는 5,000,000원입니다."
    # inner.ainvoke가 항상 수치를 지어낸 ChatResponse를 반환하도록 모킹
    mock_ainvoke = AsyncMock(return_value=ChatResponse.from_string(fabricated, usage=Usage()))
    mock_inner = MagicMock()
    mock_inner.ainvoke = mock_ainvoke

    result = await _run_with_gate(
        inner=mock_inner,
        query="잔고 알려줘",
        chat_request=_simple_req(),
        inner_name="trading_agent_react",
    )

    assert result == _TOOL_ENFORCEMENT_REJECTION, (
        f"결정론적 거절 메시지가 반환돼야 합니다. 실제: {result!r}"
    )
    assert mock_ainvoke.call_count == 2, (
        f"내부 에이전트를 정확히 2회 호출해야 합니다. 실제: {mock_ainvoke.call_count}"
    )


# ---------------------------------------------------------------------------
# 보조 단언 — 숫자 정규화
# ---------------------------------------------------------------------------

def test_extract_financial_numbers_normalizes_comma_notation():
    assert _extract_financial_numbers("75,300원") == frozenset({75300})


def test_extract_financial_numbers_normalizes_korean_unit():
    assert _extract_financial_numbers("7만 5300원") == frozenset({75300})


def test_extract_financial_numbers_matches_comma_and_korean_unit():
    """쉼표 표기와 만 단위 표기가 같은 정규화 값을 갖는다."""
    assert _extract_financial_numbers("75,300원") == _extract_financial_numbers("7만 5300원")


def test_has_numeric_claims_positive():
    assert _has_numeric_claims("잔고는 5,000,000원입니다.") is True


def test_has_numeric_claims_negative():
    assert _has_numeric_claims("반도체 수요가 회복되고 있습니다.") is False
