"""#152: 도구 강제 게이트(tool enforcement gate) 동작 고정 테스트.

이슈가 명시한 7개 케이스와 VENDOR_PATCH_STATUS 특성화 테스트를 포함한다.
실 KIS·OpenAI 연결 없이 동작한다.

게이트 로직(``_check_tool_enforcement``)과 재시도·거절 흐름(``_run_with_gate``)을
직접 호출해 검증하므로, 실제 ReAct 에이전트나 LLM 없이도 동작이 고정된다.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

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
from nat_finus_nat.finus_api import DATA_TOOL_LEDGER, DataToolLedger, _err_json, _record_to_ledger


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
