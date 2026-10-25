"""시연 리허설 도구(scripts/demo_rehearsal)의 판정·주문 가드를 가짜 실행 기록으로 고정한다.

리허설 러너는 실 LLM·KIS 모의 MCP가 있어야 돌지만, 판정 로직(grading.py)과 주문 가드(guard.py)는
기록 dict만 보는 순수 함수다. 판정이 틀리면 리허설 통과율이 거짓말을 하므로, 장면마다 "통과여야
하는 기록"과 "실패여야 하는 기록"을 한 쌍 이상 둔다. agents 스위트에 두는 이유는 리허설이 agents
venv로 돌고 CI의 agents pytest가 이 파일을 함께 집기 때문이다(실 LLM 없이 돈다).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "demo_rehearsal"))

import grading  # noqa: E402
import guard  # noqa: E402

SAMSUNG = {"name": "삼성전자", "code": "005930", "aliases": []}
HYNIX = {"name": "SK하이닉스", "code": "000660", "aliases": ["하이닉스"]}
FOOTNOTE = "\n─────\n🤖 뉴스 에이전트 · 📚 확인한 자료: KIS 시세·계좌 조회, 뉴스 검색"


def _price_resp(price: int) -> str:
    inner = json.dumps([{"stck_prpr": str(price), "prdy_vrss": "-1000"}])
    return json.dumps({"ok": True, "data": {"data": inner}})


def _kis_price(seq: int, code: str, price: int) -> dict:
    return {"seq": seq, "server": "kis", "tool": "domestic_stock", "api_type": "inquire_price",
            "params": {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, "resp": _price_resp(price),
            "error": None}


def _news(seq: int, stock: str) -> dict:
    return {"seq": seq, "server": "news", "tool": "get_market_news", "api_type": None,
            "params": {"stock_name": stock}, "resp": "[]", "error": None}


def _record(judge: str, answer: str, calls=None, truth=None, **extra) -> dict:
    rec = {"scene": "S?", "judge": judge, "params": {}, "answer": answer, "error": None, "calls": calls or [],
           "llm": {"requests": 2, "http_errors": [], "text": ""}, "truth": truth or {}, "infra": []}
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# 공용
# ---------------------------------------------------------------------------


def test_answer_prices_reads_korean_price_forms():
    text = "현재가는 213,500원, 목표 21만5000원, 또는 21만 5,000원. 전일 대비 1,000원"
    assert {213500, 215000, 1000} <= grading.answer_prices(text)
    assert 5000 not in grading.answer_prices("21만5000원")  # 만 뒤 꼬리를 따로 줍지 않는다


def test_find_mentions_takes_longest_name_first():
    assert grading.find_mentions("삼성전자우 추천", ["삼성전자", "삼성전자우"]) == ["삼성전자우"]


@pytest.mark.parametrize("code", ["EGW00201", "EGW00215"])
def test_kis_rate_limit_is_infra_failure_not_agent_failure(code):
    calls = [{**_kis_price(1, "005930", 213500), "resp": f'{{"error": "{code} 초당 거래건수를 초과"}}'}]
    rec = _record("news_and_price", "삼성전자 현재가 213,500원" + FOOTNOTE, calls,
                  {"stocks": [{**SAMSUNG, "pre": 213500, "post": 213500}]})
    verdict = grading.grade(rec)
    assert verdict.status == grading.INFRA
    assert code in verdict.reasons[0]


def test_llm_rate_limit_is_infra_failure():
    rec = _record("diary_draft", "x" * 200, llm={"requests": 1, "http_errors": [429], "text": ""})
    assert grading.grade(rec).status == grading.INFRA


def test_handler_exception_fails_before_scene_judge():
    rec = _record("diary_draft", "", error="RuntimeError: boom")
    verdict = grading.grade(rec)
    assert verdict.status == grading.FAIL and verdict.reasons[0].startswith("예외")


# ---------------------------------------------------------------------------
# S3 뉴스+현재가
# ---------------------------------------------------------------------------


def _s3(answer: str, calls=None, pre=213500, post=213500) -> dict:
    calls = calls if calls is not None else [_kis_price(1, "005930", 213500), _news(2, "삼성전자")]
    return _record("news_and_price", answer, calls, {"stocks": [{**SAMSUNG, "pre": pre, "post": post}]})


def test_s3_passes_with_news_price_and_footnote():
    verdict = grading.grade(_s3("삼성전자 현재가는 213,500원입니다. 최근 뉴스는 …" + FOOTNOTE))
    assert verdict.status == grading.PASS, verdict


def test_s3_accepts_price_inside_truth_interval_when_market_moves():
    verdict = grading.grade(_s3("삼성전자 현재가 214,000원" + FOOTNOTE, pre=213500, post=214500))
    assert verdict.status == grading.PASS


def test_s3_fails_without_news_call():
    verdict = grading.grade(_s3("삼성전자 213,500원" + FOOTNOTE, calls=[_kis_price(1, "005930", 213500)]))
    assert verdict.status == grading.FAIL
    assert any(r.startswith("뉴스 미호출") for r in verdict.reasons)


def test_s3_fails_on_article_price_outside_truth():
    verdict = grading.grade(_s3("삼성전자 현재가는 198,000원입니다." + FOOTNOTE))
    assert verdict.status == grading.FAIL
    assert any(r.startswith("현재가 불일치") for r in verdict.reasons)


def test_s3_fails_without_footnote():
    verdict = grading.grade(_s3("삼성전자 현재가는 213,500원입니다."))
    assert any(r.startswith("각주 없음") for r in verdict.reasons)


# ---------------------------------------------------------------------------
# S4 후속 비교
# ---------------------------------------------------------------------------


def _s4(answer: str, calls) -> dict:
    stocks = [{**HYNIX, "pre": 610000, "post": 610000, "refetch": True},
              {**SAMSUNG, "pre": 213500, "post": 213500, "refetch": False, "earlier_observed": [213500]}]
    return _record("compare_followup", answer, calls, {"stocks": stocks})


def test_s4_passes_when_new_stock_is_fetched_and_both_prices_match():
    answer = "SK하이닉스 610,000원, 삼성전자 213,500원으로 하이닉스가 비쌉니다."
    assert grading.grade(_s4(answer, [_kis_price(1, "000660", 610000)])).status == grading.PASS


def test_s4_fails_when_new_stock_price_is_not_fetched():
    answer = "SK하이닉스 610,000원, 삼성전자 213,500원"
    verdict = grading.grade(_s4(answer, []))
    assert verdict.status == grading.FAIL
    assert any("SK하이닉스 현재가 조회 호출 없음" in r for r in verdict.reasons)


def test_s4_fails_when_only_one_stock_is_mentioned():
    verdict = grading.grade(_s4("SK하이닉스 610,000원입니다.", [_kis_price(1, "000660", 610000)]))
    assert any(r.startswith("종목 누락") and "삼성전자" in r for r in verdict.reasons)


# ---------------------------------------------------------------------------
# S5 가격 조건 추천
# ---------------------------------------------------------------------------


def _rank_call(p1: str, p2: str, rows: dict[str, int]) -> dict:
    inner = json.dumps([{"hts_kor_isnm": n, "stck_prpr": str(p)} for n, p in rows.items()], ensure_ascii=False)
    return {"seq": 1, "server": "kis", "tool": "domestic_stock", "api_type": "market_cap",
            "params": {"fid_input_price_1": p1, "fid_input_price_2": p2},
            "resp": json.dumps({"data": {"data": inner}}, ensure_ascii=False), "error": None}


_TRUTH_RANK = {"ranking": {"삼성전자": 213500, "SK하이닉스": 610000, "삼성전자우": 170000}}


def _s5(answer: str, calls) -> dict:
    rec = _record("price_cap_recommend", answer, calls, _TRUTH_RANK)
    rec["params"] = {"price_max": 300000}
    return rec


def test_s5_passes_with_upper_bound_in_price_2_and_in_range_pick():
    rec = _s5("30만원 이하 반도체 대형주로 삼성전자(213,500원)를 추천합니다.",
              [_rank_call("", "300000", {"삼성전자": 213500})])
    assert grading.grade(rec).status == grading.PASS


def test_s5_fails_when_upper_bound_goes_to_price_1():
    rec = _s5("삼성전자를 추천합니다.", [_rank_call("300000", "", {"삼성전자": 213500})])
    verdict = grading.grade(rec)
    assert any(r.startswith("가격 인자 방향") for r in verdict.reasons)


def test_s5_fails_when_out_of_range_stock_is_asserted():
    rec = _s5("삼성전자와 SK하이닉스를 추천합니다.", [_rank_call("", "300000", {"삼성전자": 213500})])
    verdict = grading.grade(rec)
    assert verdict.status == grading.FAIL
    assert any(r.startswith("조건 밖 단정") and "SK하이닉스" in r for r in verdict.reasons)


def test_s5_allows_out_of_range_stock_when_answer_says_it_is_excluded():
    rec = _s5("삼성전자를 추천합니다. SK하이닉스는 30만원을 넘어 조건에서 제외했습니다.",
              [_rank_call("", "300000", {"삼성전자": 213500})])
    assert grading.grade(rec).status == grading.PASS


# ---------------------------------------------------------------------------
# S6 /earnings
# ---------------------------------------------------------------------------


def _s6(answer: str, dart_resp: str = '{"revenue": 79140000000000, "operating_income": 10100000000000}',
        error=None) -> dict:
    calls = [{"seq": 1, "server": "backend", "tool": "get_earnings_report", "api_type": None,
              "params": {"stock_name": "삼성전자"}, "resp": dart_resp, "error": error}]
    return _record("earnings", answer, calls)


def test_s6_passes_with_label_and_dart_numbers():
    answer = "🟢 호재: 매출과 영업이익 증가\n매출 79조원, 영업이익 10조원으로 늘었습니다."
    assert grading.grade(_s6(answer)).status == grading.PASS


def test_s6_fails_on_no_verdict_label():
    verdict = grading.grade(_s6("❔ 판정 없음\n매출 79조원"))
    assert verdict.status == grading.FAIL and verdict.reasons[0].startswith("판정 없음")


def test_s6_is_manual_when_numbers_cannot_be_matched():
    verdict = grading.grade(_s6("⚪ 중립: 혼조\n매출 12.3% 증가"))
    assert verdict.status == grading.MANUAL


# ---------------------------------------------------------------------------
# S9 보유종목 뉴스
# ---------------------------------------------------------------------------


def _balance_call(seq: int) -> dict:
    return {"seq": seq, "server": "kis", "tool": "domestic_stock", "api_type": "inquire_balance",
            "params": {"inqr_dvsn": "01"}, "resp": "{}", "error": None}


def test_s9_no_holdings_passes_on_no_holdings_answer():
    rec = _record("holdings_news", "현재 보유 종목이 없습니다.", [_balance_call(1)], {"holdings": []})
    assert grading.grade(rec).status == grading.PASS


def test_s9_no_holdings_fails_when_news_is_fetched_anyway():
    rec = _record("holdings_news", "보유 종목이 없습니다. 대신 삼성전자 뉴스…",
                  [_balance_call(1), _news(2, "삼성전자")], {"holdings": []})
    assert grading.grade(rec).status == grading.FAIL


def test_s9_with_holdings_requires_balance_first_and_only_held_news():
    truth = {"holdings": [{"name": "삼성전자", "code": "005930"}]}
    ok = _record("holdings_news", "삼성전자 뉴스 요약…", [_balance_call(1), _news(2, "삼성전자")], truth)
    assert grading.grade(ok).status == grading.PASS
    early = _record("holdings_news", "삼성전자 뉴스", [_news(1, "삼성전자"), _balance_call(2)], truth)
    assert any(r.startswith("순서") for r in grading.grade(early).reasons)
    outside = _record("holdings_news", "뉴스", [_balance_call(1), _news(2, "NAVER")], truth)
    assert any(r.startswith("보유 밖 뉴스") for r in grading.grade(outside).reasons)


# ---------------------------------------------------------------------------
# S10 종가 예측 거절
# ---------------------------------------------------------------------------


def _s10(answer: str) -> dict:
    return _record("no_price_forecast", answer, [_kis_price(1, "005930", 213500)],
                   {"stocks": [{**SAMSUNG, "pre": 213500, "post": 213500}]})


def test_s10_passes_on_disclaimer_with_only_observed_price():
    verdict = grading.grade(_s10("내일 종가는 정확히 예측할 수 없습니다. 오늘 현재가는 213,500원입니다."))
    assert verdict.status == grading.PASS


def test_s10_fails_on_invented_forecast_price():
    verdict = grading.grade(_s10("내일 종가는 정확히 예측할 수 없지만 215,000원 정도로 예상됩니다."))
    assert verdict.status == grading.FAIL
    assert any(r.startswith("지어낸 수치") for r in verdict.reasons)


def test_s10_fails_on_price_without_disclaimer():
    verdict = grading.grade(_s10("내일 종가는 213,500원입니다."))
    assert any(r.startswith("예측 불가 안내 없음") for r in verdict.reasons)


def test_s10_gate_rejection_counts_as_pass():
    assert grading.grade(_s10("[데이터 미확인] 데이터 조회 도구 없이 …")).status == grading.PASS


# ---------------------------------------------------------------------------
# S11 잔고 마스킹
# ---------------------------------------------------------------------------

_ACCOUNT = "1234567801"


def _s11(llm_text: str, answer: str, amounts=(10000000, 9876543)) -> dict:
    truth = {"amounts": list(amounts), "account_variants": [_ACCOUNT, "12345678-01", "12345678"]}
    return _record("balance_masking", answer, [_balance_call(1)], truth,
                   llm={"requests": 2, "http_errors": [], "text": llm_text})


def test_s11_passes_when_llm_sees_only_placeholders_and_user_sees_amounts():
    rec = _s11('{"messages":[{"content":"예수금 <AMOUNT_9f2a1c_1>, 총평가 <AMOUNT_9f2a1c_2>"}]}',
               "예수금은 10,000,000원, 총평가금액은 9,876,543원입니다.")
    assert grading.grade(rec).status == grading.PASS


def test_s11_fails_when_raw_amount_reaches_llm():
    rec = _s11('{"messages":[{"content":"dnca_tot_amt 10000000 <AMOUNT_9f2a1c_1>"}]}', "예수금 10,000,000원")
    verdict = grading.grade(rec)
    assert any(r.startswith("금액 유출") for r in verdict.reasons)


def test_s11_fails_when_account_number_reaches_llm():
    rec = _s11(f'계좌 {_ACCOUNT} <AMOUNT_9f2a1c_1>', "예수금 10,000,000원")
    assert any(r.startswith("계좌번호 유출") for r in grading.grade(rec).reasons)


def test_s11_fails_when_placeholder_leaks_to_user():
    rec = _s11("<AMOUNT_9f2a1c_1>", "예수금 <AMOUNT_9f2a1c_1>입니다.")
    assert any(r.startswith("복원 실패") for r in grading.grade(rec).reasons)


def test_s11_amount_match_respects_digit_boundaries():
    # 10000000이 100000000(1억) 안에 들어 있다고 유출로 보지 않는다.
    rec = _s11("시가총액 100000000 <AMOUNT_9f2a1c_1>", "예수금 10,000,000원")
    assert grading.grade(rec).status == grading.PASS


# ---------------------------------------------------------------------------
# S12 매매일지
# ---------------------------------------------------------------------------


def test_s12_passes_on_diary_draft():
    answer = "오늘 매매일지 초안입니다.\n" + "체결된 주문은 없고 예수금만 보유 중입니다. " * 4
    assert grading.grade(_record("diary_draft", answer, routed_agent="diary_agent")).status == grading.PASS


def test_s12_fails_on_gate_rejection():
    rec = _record("diary_draft", "[조회 결과 없음] 요청하신 조건으로 조회했으나 데이터가 없습니다. " * 3,
                  routed_agent="diary_agent")
    assert any(r.startswith("게이트 거부") for r in grading.grade(rec).reasons)


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------


def test_summary_excludes_infra_from_pass_rate_and_lists_failed_runs():
    records = [
        {"scene": "S3", "title": "뉴스+현재가", "kind": "scene", "rep": 0, "verdict": {"status": "pass", "reasons": []}},
        {"scene": "S3", "title": "뉴스+현재가", "kind": "scene", "rep": 1,
         "verdict": {"status": "fail", "reasons": ["뉴스 미호출: x"]}},
        {"scene": "S3", "title": "뉴스+현재가", "kind": "scene", "rep": 2,
         "verdict": {"status": "infra", "reasons": ["인프라: KIS EGW00201(초당 거래건수 초과)"]}},
        {"scene": "S3", "title": "뉴스+현재가", "kind": "setup", "rep": 0},
    ]
    text = grading.summarize(records)
    assert "| S3 | 뉴스+현재가 | 3 | 1 | 1 | 1 | 0 | 1/2 (50%) |" in text
    assert "S3 2회차 · 실패" in text and "뉴스 미호출 1" in text


# ---------------------------------------------------------------------------
# 주문 가드
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("api_type", ["order_cash", "order_credit", "order_resv", "order_resv_rvsecncl",
                                      "order_rvsecncl", "ORDER_CASH", "order_something_new"])
def test_guard_blocks_order_api_types(api_type):
    assert guard.is_order_call("domestic_stock", api_type)


@pytest.mark.parametrize("api_type", ["inquire_price", "inquire_balance", "inquire_psbl_order", "market_cap",
                                      "find_api_detail", "find_stock_code", "search_stock_info", None])
def test_guard_lets_read_api_types_through(api_type):
    assert not guard.is_order_call("domestic_stock", api_type)


def test_guard_blocks_stdio_place_order_but_not_order_history_reads():
    assert guard.is_order_call("place_order")
    assert not guard.is_order_call("get_today_daily_orders")
    assert not guard.is_order_call("get_market_news")


def test_guard_exception_is_not_swallowed_by_except_exception():
    with pytest.raises(guard.OrderGuardTripped):
        try:
            raise guard.OrderGuardTripped("x")
        except Exception:  # noqa: BLE001 — 에이전트 코드의 넓은 except를 흉내 낸다
            pytest.fail("OrderGuardTripped가 except Exception에 잡혔다")
