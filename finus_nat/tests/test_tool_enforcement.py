"""#152: 도구 강제 게이트(tool enforcement gate) 동작 고정 테스트.

이슈가 명시한 7개 케이스와 VENDOR_PATCH_STATUS 특성화 테스트를 포함한다.
실 KIS·OpenAI 연결 없이 동작한다.

게이트 로직(``_check_tool_enforcement``)과 재시도·거절 흐름(``_run_with_gate``)을
직접 호출해 검증하므로, 실제 ReAct 에이전트나 LLM 없이도 동작이 고정된다.
"""

import json
import logging
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from nat.data_models.api_server import (
    ChatRequest,
    ChatResponse,
    Message,
    Usage,
    UserMessageContentRoleType,
)

from nat_finus_nat.agents import (
    _EMPTY_RESULT_REJECTION,
    _TOOL_ENFORCEMENT_REJECTION,
    _check_tool_enforcement,
    _extract_financial_numbers,
    _has_numeric_claims,
    _run_with_gate,
)
from nat_finus_nat.finus_api import (
    DATA_TOOL_LEDGER,
    DataToolLedger,
    _EMPTY_RESULT_LITERALS,
    _err_json,
    _has_empty_result,
    _record_to_ledger,
)

# 계약 테스트용: MCP 소스 파일 경로 (리포지토리 루트 기준)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_SOURCES: dict[str, str] = {
    "finus_mcp_trading_today_orders": "mcp-trading/index.js",
    "finus_market_news": "mcp-news/index.js",
    "finus_earnings_report": "mcp-dart/index.js",
}


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
    # Values are scaled by ×1000 to preserve decimal precision (see _NUM_SCALE).
    assert _extract_financial_numbers("75,300원") == frozenset({75_300_000})


def test_extract_financial_numbers_normalizes_korean_unit():
    # Values are scaled by ×1000 to preserve decimal precision (see _NUM_SCALE).
    assert _extract_financial_numbers("7만 5300원") == frozenset({75_300_000})


def test_extract_financial_numbers_matches_comma_and_korean_unit():
    """쉼표 표기와 만 단위 표기가 같은 정규화 값을 갖는다."""
    assert _extract_financial_numbers("75,300원") == _extract_financial_numbers("7만 5300원")


def test_has_numeric_claims_positive():
    assert _has_numeric_claims("잔고는 5,000,000원입니다.") is True


def test_has_numeric_claims_negative():
    assert _has_numeric_claims("반도체 수요가 회복되고 있습니다.") is False


# ---------------------------------------------------------------------------
# Critical 1 — 한국어 만/억/조 단위로 끝나는 금액 탐지
# ---------------------------------------------------------------------------

def test_has_numeric_claims_detects_man_unit_only():
    """'500만원'처럼 만 단위로 끝나는 표기를 탐지한다."""
    assert _has_numeric_claims("잔고는 500만원입니다.") is True


def test_has_numeric_claims_detects_eok_unit_only():
    """'1억원'처럼 억 단위로 끝나는 표기를 탐지한다."""
    assert _has_numeric_claims("평가금액은 1억원입니다.") is True


def test_has_numeric_claims_detects_man_with_comma():
    """'1,200만원'처럼 쉼표+만 단위 표기를 탐지한다."""
    assert _has_numeric_claims("예수금 1,200만원이 있습니다.") is True


def test_has_numeric_claims_detects_jo_unit_only():
    """'350조원'처럼 조 단위로 끝나는 표기를 탐지한다."""
    assert _has_numeric_claims("시가총액은 350조원입니다.") is True


def test_korean_unit_only_same_value_as_full_notation():
    """'500만원'과 '5,000,000원'이 같은 정규화 값을 가진다(탐지+비교 모두 일치)."""
    assert _extract_financial_numbers("500만원") == _extract_financial_numbers("5,000,000원")


def test_korean_unit_only_cross_check():
    """'1억원'과 '100,000,000원'이 같은 정규화 값을 가진다."""
    assert _extract_financial_numbers("1억원") == _extract_financial_numbers("100,000,000원")


def test_gate_trips_on_man_unit_amount_no_tool():
    """'500만원'처럼 만 단위 표기가 있고 도구 없으면 게이트가 트립한다."""
    ledger = DataToolLedger()
    answer = "Final Answer: 잔고는 500만원입니다."
    assert _check_tool_enforcement(answer, ledger, _simple_req()) is True


# ---------------------------------------------------------------------------
# Critical 3 — 소수 구분: 5.7%는 5.3%의 부분집합이 아님
# ---------------------------------------------------------------------------

def test_gate_trips_on_mismatched_decimal_numbers():
    """5.7%와 5.3%는 다른 수치이므로 게이트가 트립해야 한다.

    int(float(s)) 버그가 있으면 두 값 모두 5로 정규화돼 오통과한다.
    _NUM_SCALE=1000 이후에는 5700≠5300이라 정확히 구분된다.
    """
    ledger = DataToolLedger()
    req = _req(
        ("user", "수익률 알려줘"),
        ("assistant", "수익률은 5.3%입니다."),
        ("user", "정말?"),
    )
    answer = "Final Answer: 수익률은 5.7%입니다."
    assert _check_tool_enforcement(answer, ledger, req) is True


def test_gate_passes_on_exact_decimal_restatement():
    """대화록에 있는 소수 수치를 그대로 재진술하면 통과한다."""
    ledger = DataToolLedger()
    req = _req(
        ("user", "수익률 알려줘"),
        ("assistant", "수익률은 5.7%입니다."),
        ("user", "확인해줘"),
    )
    answer = "Final Answer: 말씀드린 대로 수익률은 5.7%입니다."
    assert _check_tool_enforcement(answer, ledger, req) is False


# ---------------------------------------------------------------------------
# Improvement 1 — 주(株) vs 기간 표현 오탐 방지
# ---------------------------------------------------------------------------

def test_has_numeric_claims_does_not_match_juil():
    """'1주일'은 기간 표현이므로 탐지하지 않는다."""
    assert _has_numeric_claims("1주일 전 뉴스입니다.") is False


def test_has_numeric_claims_does_not_match_ju_dongan():
    """'2주 동안'은 기간 표현이므로 탐지하지 않는다."""
    assert _has_numeric_claims("최근 2주 동안 반도체 업황이 개선됐습니다.") is False


def test_has_numeric_claims_does_not_match_jugan():
    """'2주간'은 기간 표현이므로 탐지하지 않는다."""
    assert _has_numeric_claims("2주간 상승세가 이어졌습니다.") is False


def test_has_numeric_claims_does_not_match_ju_after():
    """'3주 후'는 기간 표현이므로 탐지하지 않는다."""
    assert _has_numeric_claims("3주 후 실적 발표가 있습니다.") is False


def test_has_numeric_claims_matches_stock_quantity():
    """'100주 보유'는 주식 수량이므로 탐지한다."""
    assert _has_numeric_claims("삼성전자 100주 보유 중입니다.") is True


def test_has_numeric_claims_matches_stock_sell():
    """'100주를 매도'는 주식 수량이므로 탐지한다."""
    assert _has_numeric_claims("삼성전자 100주를 매도했습니다.") is True


# ---------------------------------------------------------------------------
# Improvement 4 — _record_to_ledger의 오류 판정 정합성 고정
# ---------------------------------------------------------------------------

def test_record_to_ledger_marks_err_json_as_failure():
    """_err_json()이 만든 오류 응답은 ok=False로, 정상 JSON은 ok=True로 기록된다.

    이 단언이 깨지면 새 오류 반환 경로가 {"error": ...} 규약을 벗어난 것이고,
    그 도구에 대해 게이트가 조용히 무력화된다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger("finus_account_balance", _err_json("mcp_timeout", tool="domestic_stock"))
        _record_to_ledger("finus_account_balance", '{"output": [{"prpr": "75300"}]}')
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert [r.ok for r in ledger.records] == [False, True]
    assert ledger.any_success() is True


# ---------------------------------------------------------------------------
# Critical 2 — 부작용 도구 재시도 금지 가드
# ---------------------------------------------------------------------------

def test_ledger_had_side_effect_true_for_save_diary():
    """finus_save_diary 기록이 있으면 had_side_effect()가 True를 반환한다."""
    ledger = DataToolLedger()
    ledger.record("finus_save_diary", ok=True, produced_rows=True)
    assert ledger.had_side_effect() is True


def test_ledger_had_side_effect_false_for_read_tools():
    """읽기 전용 도구만 기록된 원장은 had_side_effect()가 False를 반환한다."""
    ledger = DataToolLedger()
    ledger.record("finus_mcp_trading_get_balance", ok=True, produced_rows=True)
    assert ledger.had_side_effect() is False


async def test_side_effect_guard_skips_retry_on_write_tool():
    """finus_save_diary가 실패 후에도 수치를 주장하면 재시도 없이 즉시 거절한다.

    시나리오: 저장 API 오류(ok=False) → 원장에 실패 기록 → 에이전트가 수치 주장
    → 게이트 트립 → 가드가 had_side_effect()=True를 보고 재시도 건너뜀.
    재시도가 일어나면 저장이 다시 시도되어 DB에 중복/잡음이 생길 수 있다.
    """
    # HTTP 오류 후에도 에이전트가 수치를 지어내는 답변을 돌려준다고 가정
    fabricated = "Final Answer: 실현손익 52,000원을 일지에 저장했습니다."
    call_count = 0

    async def ainvoke_with_failed_side_effect(_input):
        nonlocal call_count
        call_count += 1
        ledger = DATA_TOOL_LEDGER.get()
        if ledger is not None:
            # 저장 시도는 했지만 API 오류 — any_success()=False이므로 게이트가 트립된다
            ledger.record("finus_save_diary", ok=False, produced_rows=False)
        return ChatResponse.from_string(fabricated, usage=Usage())

    mock_inner = MagicMock()
    mock_inner.ainvoke = ainvoke_with_failed_side_effect

    result = await _run_with_gate(
        inner=mock_inner,
        query="오늘 매매일지 저장해줘",
        chat_request=_simple_req("오늘 매매일지 저장해줘"),
        inner_name="diary_agent",
    )

    assert result == _TOOL_ENFORCEMENT_REJECTION, (
        f"결정론적 거절 메시지가 반환돼야 합니다. 실제: {result!r}"
    )
    assert call_count == 1, (
        f"부작용 가드로 재시도 없이 1회만 호출해야 합니다. 실제: {call_count}"
    )


# ---------------------------------------------------------------------------
# Critical (신규) — 일지 저장 성공은 데이터 조회 성공이 아니다
# ---------------------------------------------------------------------------

def test_successful_save_diary_does_not_count_as_data_success():
    """일지 저장 성공은 데이터 조회 성공이 아니다 — 게이트를 풀어서는 안 된다.

    finus_save_diary 성공 응답({id: 12, ...})이 any_success()=True가 되면,
    "일지 저장 + 잔고 수치 지어내기" 패턴이 게이트를 우회한다(이슈 #152 재현 경로).
    produced_rows=ok and tool_name not in _SIDE_EFFECT_TOOLS 수정으로 차단된다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger("finus_save_diary", '{"id": 12, "title": "매매일지"}')
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.had_side_effect() is True
    assert ledger.any_success() is False
    answer = "Final Answer: 잔고는 500만원입니다."
    assert _check_tool_enforcement(answer, ledger, _simple_req()) is True


async def test_side_effect_guard_skips_retry_on_successful_write_tool():
    """finus_save_diary 저장 성공 후 수치를 주장해도 재시도 없이 즉시 거절한다.

    시나리오: 저장 성공(ok=True) → produced_rows=False이므로 any_success()=False
    → 에이전트가 수치 주장 → 게이트 트립 → had_side_effect()=True → 재시도 건너뜀.
    이 경로가 성공 경로(ok=True)에서도 부작용 가드가 동작함을 고정한다.
    """
    fabricated = "Final Answer: 실현손익 52,000원을 일지에 저장했습니다."
    call_count = 0

    async def ainvoke_with_successful_side_effect(_input):
        nonlocal call_count
        call_count += 1
        # _record_to_ledger를 직접 호출해 실제 운영 경로와 동일한 기록 방식으로 검증
        _record_to_ledger("finus_save_diary", '{"id": 12, "title": "매매일지"}')
        return ChatResponse.from_string(fabricated, usage=Usage())

    mock_inner = MagicMock()
    mock_inner.ainvoke = ainvoke_with_successful_side_effect

    result = await _run_with_gate(
        inner=mock_inner,
        query="오늘 매매일지 저장해줘",
        chat_request=_simple_req("오늘 매매일지 저장해줘"),
        inner_name="diary_agent",
    )

    assert result == _TOOL_ENFORCEMENT_REJECTION, (
        f"결정론적 거절 메시지가 반환돼야 합니다. 실제: {result!r}"
    )
    assert call_count == 1, (
        f"부작용 가드로 재시도 없이 1회만 호출해야 합니다. 실제: {call_count}"
    )


# ---------------------------------------------------------------------------
# Improvement (신규) — 주격 조사 '가'가 붙은 수량은 탐지해야 한다
# ---------------------------------------------------------------------------

def test_has_numeric_claims_matches_stock_quantity_with_ga_particle():
    """'100주가 남아 있습니다'처럼 수량 뒤에 주격 조사 가가 붙어도 탐지한다."""
    assert _has_numeric_claims("삼성전자 100주가 남아 있습니다.") is True


def test_has_numeric_claims_matches_stock_quantity_ga_particle_variant():
    """'보유 수량 100주가 전부입니다'에서 수량을 탐지한다."""
    assert _has_numeric_claims("보유 수량 100주가 전부입니다.") is True


def test_has_numeric_claims_matches_executed_quantity_with_ga_particle():
    """'50주가 체결됐습니다'에서 수량을 탐지한다."""
    assert _has_numeric_claims("50주가 체결됐습니다.") is True


def test_has_numeric_claims_does_not_match_juga_as_stock_price_noun():
    """'주가가 상승했습니다'에서 주가(株價) 자체는 탐지하지 않는다(수치 없음)."""
    assert _has_numeric_claims("주가가 상승했습니다.") is False


def test_has_numeric_claims_does_not_match_juga_with_neun_particle():
    """'주가는 N원입니다'에서 주가 명사는 제외하되 원 단위 수치로 탐지된다."""
    # 주가(株價) 자체는 MISS이지만 7만원 때문에 전체 문장은 HIT
    assert _has_numeric_claims("주가는 7만원입니다.") is True


# ---------------------------------------------------------------------------
# 이슈 #209 — 빈-결과 축: 읽기 도구 성공 + 빈 응답은 any_success()=False
# ---------------------------------------------------------------------------

def test_record_to_ledger_empty_today_orders_does_not_count_as_success():
    """당일 주문체결 조회 성공 + 결과 없음 → any_success()=False.

    mcp-trading/index.js formatDailyOrderCcldReport 의 rows.length===0 분기가
    "해당 조건의 주문·체결이 없습니다." 리터럴을 출력한다.
    이 응답으로 _record_to_ledger를 호출하면 produced_rows=False가 돼야 한다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_mcp_trading_today_orders",
            "[당일 주문·체결 내역] 20260807\n- 조회 TR: TTTC0081R\n- 결과: 해당 조건의 주문·체결이 없습니다.",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is False
    answer = "Final Answer: 오늘 체결된 주문은 삼성전자 100주입니다."
    assert _check_tool_enforcement(answer, ledger, _simple_req()) is True


def test_record_to_ledger_nonempty_today_orders_counts_as_success():
    """당일 주문체결 조회 성공 + 데이터 있음 → any_success()=True (오탐 없음)."""
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_mcp_trading_today_orders",
            "[당일 주문·체결 내역] 20260807\n1. 삼성전자 (005930)\n   주문 100주 @ 75,300원",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is True


def test_record_to_ledger_empty_market_news_does_not_count_as_success():
    """시장 뉴스 조회 성공 + 결과 없음 → any_success()=False.

    mcp-news/index.js fetchMarketNews 가 뉴스 항목이 없을 때
    "'${stockName}'에 대한 뉴스를 찾지 못했습니다." 를 반환한다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_market_news",
            "'삼성전자'에 대한 뉴스를 찾지 못했습니다.",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is False


def test_record_to_ledger_nonempty_market_news_counts_as_success():
    """시장 뉴스 조회 성공 + 데이터 있음 → any_success()=True (오탐 없음)."""
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_market_news",
            "삼성전자 4분기 실적 호조 - 반도체 수요 회복 | 2026-08-07 | https://news.naver.com/...",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is True


def test_record_to_ledger_empty_earnings_report_does_not_count_as_success():
    """실적 리포트 조회 성공 + 데이터 없음 → any_success()=False.

    mcp-dart/index.js formatEarningsReport 의 rows.length===0 분기가
    "OpenDART 재무제표 데이터가 없습니다." 리터럴을 출력한다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_earnings_report",
            "[삼성전자] DART 실적 리포트\n- 조회기간: 2026Q1\n- 조회 결과: OpenDART 재무제표 데이터가 없습니다.",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is False


def test_record_to_ledger_empty_list_diaries_does_not_count_as_success():
    """매매일지 목록 조회 성공 + 빈 배열("[]") → any_success()=False.

    finus_api.py finus_list_diaries 는 body.get("data")를 json.dumps()해 반환한다.
    data가 빈 리스트일 때 결과는 "[]" 이므로 구조적으로 탐지한다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger("finus_list_diaries", "[]")
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is False


def test_record_to_ledger_null_list_diaries_does_not_count_as_success():
    """매매일지 목록 조회 성공 + null 응답("null") → any_success()=False.

    body.get("data")가 None이면 json.dumps(None)="null" 이 반환된다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger("finus_list_diaries", "null")
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is False


def test_record_to_ledger_nonempty_list_diaries_counts_as_success():
    """매매일지 목록 조회 성공 + 데이터 있음 → any_success()=True (오탐 없음)."""
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_list_diaries",
            '[{"id": 1, "title": "매매일지 2026-08-07", "content": "삼성전자 매수"}]',
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is True


def test_record_to_ledger_empty_rlz_pl_no_summary_does_not_count_as_success():
    """실현손익 조회 성공 + 보유종목 없음 + 계좌집계 없음 → any_success()=False.

    balance-rlz-pl-report.js formatBalanceRlzPlReport 의 rows.length===0 분기:
    summary가 null이면 "[계좌 집계]" 블록을 출력하지 않는다.
    실제 수치가 전혀 없는 이 응답은 produced_rows=False가 돼야 한다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_mcp_trading_balance_rlz_pl",
            "[주식잔고조회_실현손익]\n- 조회 TR: TTTC8494R (v1_국내주식-041)\n- 보유 종목이 없습니다.",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is False


def test_record_to_ledger_empty_rlz_pl_with_summary_counts_as_success():
    """실현손익 조회 성공 + 보유종목 없음 + 계좌집계 있음 → any_success()=True (오탐 없음).

    balance-rlz-pl-report.js 의 rows.length===0 + summary 존재 분기:
    "[계좌 집계]" 블록에 예수금·평가금액 등 실제 계좌 수치가 포함된다.
    produced_rows=False로 만들면 이 정상 데이터 응답을 차단하는 오탐이 된다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_mcp_trading_balance_rlz_pl",
            "[주식잔고조회_실현손익]\n- 보유 종목이 없습니다.\n\n[계좌 집계]\n- 예수금: 1,000,000원\n- 실현손익: 52,000원",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is True


# ---------------------------------------------------------------------------
# Critical (PR #216 리뷰) — only_empty_reads() 단위 테스트
# ---------------------------------------------------------------------------

def test_only_empty_reads_true_when_all_reads_empty():
    """읽기 도구가 실행됐고 전부 빈 결과면 only_empty_reads()=True."""
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_mcp_trading_today_orders",
            "[당일 주문·체결 내역] 20260807\n- 결과: 해당 조건의 주문·체결이 없습니다.",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.only_empty_reads() is True


def test_only_empty_reads_false_when_no_records():
    """도구를 전혀 호출하지 않으면 only_empty_reads()=False."""
    ledger = DataToolLedger()
    assert ledger.only_empty_reads() is False


def test_only_empty_reads_false_when_only_side_effects():
    """쓰기 도구만 있으면 읽기 목록이 비어 only_empty_reads()=False."""
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger("finus_save_diary", '{"id": 12, "title": "매매일지"}')
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.only_empty_reads() is False


def test_only_empty_reads_false_when_one_read_has_data():
    """빈 읽기와 데이터 있는 읽기가 섞이면 only_empty_reads()=False."""
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_mcp_trading_today_orders",
            "[당일 주문·체결 내역] 20260807\n- 결과: 해당 조건의 주문·체결이 없습니다.",
        )
        _record_to_ledger(
            "finus_market_news",
            "삼성전자 4분기 실적 호조 - 반도체 수요 회복 | 2026-08-07",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.only_empty_reads() is False


# ---------------------------------------------------------------------------
# Critical (PR #216 리뷰) — _run_with_gate: 빈 결과 경로의 재시도 건너뜀 확인
# ---------------------------------------------------------------------------

async def test_run_with_gate_empty_reads_skips_retry_returns_empty_rejection():
    """빈 결과 + 0값 답변: 재시도 없이 _EMPTY_RESULT_REJECTION을 반환해야 한다.

    시나리오: today_orders가 빈 결과를 돌려주고 에이전트가 '0주'라고 정직하게 답한다.
    수정 전: 게이트 트립 → 재시도 → 재시도도 트립 → _TOOL_ENFORCEMENT_REJECTION (거짓 메시지)
    수정 후: 게이트 트립 → only_empty_reads()=True → 재시도 건너뜀 → _EMPTY_RESULT_REJECTION
    LLM 호출이 1회로 끝나는지(재시도 없음)를 확인한다.
    """
    call_count = 0

    async def ainvoke_empty_then_zero(_input):
        nonlocal call_count
        call_count += 1
        _record_to_ledger(
            "finus_mcp_trading_today_orders",
            "[당일 주문·체결 내역] 20260807\n- 결과: 해당 조건의 주문·체결이 없습니다.",
        )
        return ChatResponse.from_string("오늘 체결된 주문은 0주입니다.", usage=Usage())

    mock_inner = MagicMock()
    mock_inner.ainvoke = ainvoke_empty_then_zero

    result = await _run_with_gate(
        inner=mock_inner,
        query="오늘 주문 내역 알려줘",
        chat_request=_simple_req("오늘 주문 내역 알려줘"),
        inner_name="trading_agent_react",
    )

    assert result == _EMPTY_RESULT_REJECTION, (
        f"_EMPTY_RESULT_REJECTION이 반환돼야 합니다. 실제: {result!r}"
    )
    assert call_count == 1, (
        f"빈 결과 가드로 재시도 없이 1회만 호출해야 합니다. 실제: {call_count}"
    )


async def test_run_with_gate_empty_reads_fabricated_nonzero_still_blocked():
    """빈 결과 + 비0 수치 지어내기: 여전히 차단되고 _EMPTY_RESULT_REJECTION을 반환한다.

    수정의 의도는 게이트를 통째로 푸는 것이 아니다 — 빈 결과로 수치를 지어내는 것은
    여전히 차단된다. 다만 거짓 _TOOL_ENFORCEMENT_REJECTION 대신 정직한
    _EMPTY_RESULT_REJECTION을 반환하고, LLM 호출은 1회로 줄인다.
    """
    call_count = 0

    async def ainvoke_empty_then_fabricated(_input):
        nonlocal call_count
        call_count += 1
        _record_to_ledger(
            "finus_mcp_trading_today_orders",
            "[당일 주문·체결 내역] 20260807\n- 결과: 해당 조건의 주문·체결이 없습니다.",
        )
        return ChatResponse.from_string("잔고는 500만원입니다.", usage=Usage())

    mock_inner = MagicMock()
    mock_inner.ainvoke = ainvoke_empty_then_fabricated

    result = await _run_with_gate(
        inner=mock_inner,
        query="잔고 알려줘",
        chat_request=_simple_req("잔고 알려줘"),
        inner_name="trading_agent_react",
    )

    assert result == _EMPTY_RESULT_REJECTION, (
        f"_EMPTY_RESULT_REJECTION이 반환돼야 합니다. 실제: {result!r}"
    )
    assert call_count == 1, (
        f"빈 결과 가드로 재시도 없이 1회만 호출해야 합니다. 실제: {call_count}"
    )


# ---------------------------------------------------------------------------
# Improvement 2 (PR #216 리뷰) — MCP 소스와의 계약 테스트
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name,rel_path", sorted(_MCP_SOURCES.items()))
def test_empty_result_literal_still_exists_in_mcp_source(tool_name: str, rel_path: str):
    """_EMPTY_RESULT_LITERALS는 MCP 소스 문자열에 대한 하드 의존이다.

    JS 쪽 문구가 바뀌면 게이트가 조용히 #209로 회귀하므로, 여기서 깨뜨려 알린다.
    """
    source = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    literal = _EMPTY_RESULT_LITERALS[tool_name]
    assert literal in source, (
        f"{rel_path}에서 '{literal}'가 사라졌습니다. "
        f"_EMPTY_RESULT_LITERALS[{tool_name!r}]를 새 문구로 갱신하세요."
    )


def test_rlz_pl_dual_condition_markers_still_exist():
    """balance-rlz-pl-report.js의 이중 조건 마커가 유지되는지 확인한다.

    '보유 종목이 없습니다.'와 '[계좌 집계]'가 두 분기를 구분하는 근거다.
    둘 중 하나라도 바뀌면 빈-결과 판정 로직이 조용히 무력화된다.
    """
    source = (_REPO_ROOT / "mcp-trading/balance-rlz-pl-report.js").read_text(encoding="utf-8")
    assert "보유 종목이 없습니다." in source
    assert "[계좌 집계]" in source


# ---------------------------------------------------------------------------
# Improvement 3 (PR #216 리뷰) — get_balance 의도적 통과를 고정하는 테스트
# ---------------------------------------------------------------------------

def test_record_to_ledger_get_balance_no_holdings_still_counts_as_success():
    """무보유 잔고 응답은 '데이터 없음'이 아니라 '보유 0건이라는 실제 데이터'다.

    mcp-trading/balance.js 는 보유 목록이 비어도 [계좌 잔고 현황] 블록에
    거래가능금액·정산예정금액 등 실제 KIS 수치를 그대로 포함한다.
    이를 빈 결과로 판정하면 예수금을 인용하는 정상 답변이 차단된다(오탐).
    balance-rlz-pl-report.js 와 문구가 같으므로 도구 이름 게이트가 필수다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_mcp_trading_get_balance",
            "[계좌 잔고 현황]\n- 총 평가금액: 0원\n- 거래가능금액: 1,250,000원\n"
            "- 익일 정산예정금액: 1,250,000원\n\n[보유 종목 리스트]\n보유 종목이 없습니다.",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is True


# ---------------------------------------------------------------------------
# Nitpick 1 (PR #216 리뷰) — finus_earnings_report 대칭 페어 추가
# ---------------------------------------------------------------------------

def test_record_to_ledger_nonempty_earnings_report_counts_as_success():
    """실적 리포트 조회 성공 + 데이터 있음 → any_success()=True (오탐 없음).

    mcp-dart/index.js formatEarningsReport 의 데이터 있는 분기는
    '[주요 실적]' 블록을 포함한다. 이 응답이 빈 결과로 오판되지 않아야 한다.
    """
    ledger = DataToolLedger()
    token = DATA_TOOL_LEDGER.set(ledger)
    try:
        _record_to_ledger(
            "finus_earnings_report",
            "[삼성전자] DART 실적 리포트\n- 조회기간: 2025FY\n[주요 실적]\n"
            "- 매출: 300조원\n- 영업이익: 32조원",
        )
    finally:
        DATA_TOOL_LEDGER.reset(token)

    assert ledger.any_success() is True

# ---------------------------------------------------------------------------
# Improvement 1 (PR #216 리뷰) — JSON fall-through 회귀 가드
#
# _has_empty_result의 JSON 분기는 빈 컨테이너일 때만 True를 확정하고,
# 비어있지 않은 JSON이어도 리터럴 검사로 fall-through한다.
# 조기 `return False`를 복원(뮤턴트)하면 이 테스트들이 red가 된다.
# ---------------------------------------------------------------------------

def test_has_empty_result_json_string_wrapped_literal_detected():
    """JSON 문자열로 감싼 리터럴도 빈 결과로 판정한다.

    잡는 뮤턴트: JSON 분기에 `return False` 조기 종료를 복원하면,
    파싱된 문자열이 비어있지 않아 fall-through 없이 False를 반환하고 이 단언이 깨진다.

    Node.js JSON.stringify는 한국어를 \\uXXXX 없이 그대로 직렬화하므로
    ensure_ascii=False로 시뮬레이션한다.
    """
    lit = _EMPTY_RESULT_LITERALS["finus_market_news"]
    # Node.js style: JSON.stringify("'삼성전자'에 대한 뉴스를 찾지 못했습니다.")
    wrapped = json.dumps("'삼성전자'" + lit, ensure_ascii=False)
    assert _has_empty_result("finus_market_news", wrapped) is True


def test_has_empty_result_json_object_wrapped_literal_detected():
    """JSON 객체로 감싼 리터럴도 빈 결과로 판정한다.

    잡는 뮤턴트: JSON 분기에 `return False` 조기 종료를 복원하면,
    파싱된 객체가 비어있지 않아 fall-through 없이 False를 반환하고 이 단언이 깨진다.

    MCP 래퍼가 {"text": "..."} 형태로 감쌀 때 리터럴 탐지가 유지됨을 고정한다.
    """
    lit = _EMPTY_RESULT_LITERALS["finus_market_news"]
    wrapped = json.dumps({"text": "'삼성전자'" + lit}, ensure_ascii=False)
    assert _has_empty_result("finus_market_news", wrapped) is True


def test_has_empty_result_empty_dict_json_is_true():
    """빈 dict JSON ('{}')을 빈 결과로 판정한다.

    잡는 뮤턴트: isinstance 판정에서 dict를 제거(list만 허용)하면 {} → False가 돼
    이 단언이 깨진다. finus_list_diaries 등에서 backend가 {}를 돌려줄 때의 방어선이다.
    """
    assert _has_empty_result("finus_list_diaries", "{}") is True


def test_has_empty_result_empty_string_json_is_true():
    """빈 문자열 JSON ('\"\"')을 빈 결과로 판정한다.

    잡는 뮤턴트: isinstance 판정에서 str를 제거(list/dict만 허용)하면 \"\" → False가 돼
    이 단언이 깨진다.
    """
    assert _has_empty_result("finus_list_diaries", '""') is True


def test_has_empty_result_data_bearing_json_is_false():
    """데이터가 있는 JSON은 빈 결과로 판정하지 않는다(오탐 방지).

    fall-through 변경이 오탐을 만들지 않음을 고정한다.
    리터럴 검사는 도구 이름으로 게이트되므로 finus_market_news 의
    실제 뉴스 JSON 객체는 통과해야 한다.
    """
    data = json.dumps({"items": ["삼성전자 뉴스1", "뉴스2"]}, ensure_ascii=False)
    assert _has_empty_result("finus_market_news", data) is False


# ---------------------------------------------------------------------------
# 🟡 Improvement (PR #216 3차 리뷰) — 2차 시도 경로의 only_empty_reads() 가드
# ---------------------------------------------------------------------------

async def test_run_with_gate_second_attempt_empty_reads_returns_empty_rejection():
    """1차 도구 없음 → 2차 도구 호출 + 빈 결과 → _EMPTY_RESULT_REJECTION 반환.

    시나리오:
      1차: 에이전트가 도구를 전혀 호출하지 않고 수치를 지어냄 → 게이트 트립
           (ledger 비어 있음 → only_empty_reads()=False → 재시도 진행)
      2차: 교정 프리픽스 덕분에 도구 호출함(today_orders 빈 결과) + 여전히 수치 주장
           → 게이트 트립 → ledger2.only_empty_reads()=True
           → _EMPTY_RESULT_REJECTION 반환 ("도구 없이"가 아니므로 올바른 메시지)
    LLM 호출이 정확히 2회인지 확인한다(2차는 실제로 실행되므로).
    """
    call_count = 0

    async def ainvoke_no_tool_then_empty(_input):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # 2차: 교정 프리픽스로 인해 도구를 호출하지만 빈 결과
            _record_to_ledger(
                "finus_mcp_trading_today_orders",
                "[당일 주문·체결 내역] 20260807\n- 결과: 해당 조건의 주문·체결이 없습니다.",
            )
        # 1차·2차 모두 수치를 지어낸 답변 반환
        return ChatResponse.from_string("오늘 체결된 주문은 삼성전자 100주입니다.", usage=Usage())

    mock_inner = MagicMock()
    mock_inner.ainvoke = ainvoke_no_tool_then_empty

    result = await _run_with_gate(
        inner=mock_inner,
        query="오늘 주문 내역 알려줘",
        chat_request=_simple_req("오늘 주문 내역 알려줘"),
        inner_name="trading_agent_react",
    )

    assert result == _EMPTY_RESULT_REJECTION, (
        f"2차 빈 결과 경로에서 _EMPTY_RESULT_REJECTION이 반환돼야 합니다. 실제: {result!r}"
    )
    assert call_count == 2, (
        f"1차(도구 없음)·2차(빈 결과) 모두 실행되므로 LLM 호출은 2회여야 합니다. 실제: {call_count}"
    )


# ---------------------------------------------------------------------------
# 🔵 Nitpick (PR #216 3차 리뷰) — had_side_effect + only_empty_reads 조합
# ---------------------------------------------------------------------------

async def test_side_effect_guard_with_empty_reads_returns_empty_rejection():
    """save_diary + 빈 조회(market_news) 조합 → _EMPTY_RESULT_REJECTION + 재시도 건너뜀.

    시나리오:
      쓰기 도구(finus_save_diary)와 빈 결과 읽기 도구(finus_market_news)가 함께 실행됨.
      had_side_effect()=True → 재시도 건너뜀(call_count=1 유지).
      only_empty_reads()=True(읽기 목록이 전부 빈 결과) → _EMPTY_RESULT_REJECTION 반환.
      "도구 없이"라고 말하는 _TOOL_ENFORCEMENT_REJECTION은 사실과 다르므로 차단된다.
    """
    call_count = 0

    async def ainvoke_save_and_empty_news(_input):
        nonlocal call_count
        call_count += 1
        # 쓰기 도구: 저장 성공
        _record_to_ledger("finus_save_diary", '{"id": 12, "title": "매매일지"}')
        # 읽기 도구: 빈 결과
        _record_to_ledger(
            "finus_market_news",
            "'삼성전자'에 대한 뉴스를 찾지 못했습니다.",
        )
        return ChatResponse.from_string("잔고는 500만원입니다.", usage=Usage())

    mock_inner = MagicMock()
    mock_inner.ainvoke = ainvoke_save_and_empty_news

    result = await _run_with_gate(
        inner=mock_inner,
        query="삼성전자 뉴스 알려줘",
        chat_request=_simple_req("삼성전자 뉴스 알려줘"),
        inner_name="news_agent",
    )

    assert result == _EMPTY_RESULT_REJECTION, (
        f"쓰기+빈 조회 조합에서 _EMPTY_RESULT_REJECTION이 반환돼야 합니다. 실제: {result!r}"
    )
    assert call_count == 1, (
        f"부작용 가드로 재시도 없이 1회만 호출해야 합니다. 실제: {call_count}"
    )
