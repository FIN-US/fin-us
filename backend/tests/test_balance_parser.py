"""이슈 #153 관련 테스트들의 픽스처는 mcp-trading/data/stocks.json 마스터의 실제
종목 행을 사용한다(예: 종목코드 00104K, F70102B96). 특정 종목코드나 종목명이
마스터에 있는지는 이 저장소에서 `git grep '"code": "<코드>"'
mcp-trading/data/stocks.json`으로 직접 확인할 수 있다.
"""

import unittest
from backend.scheduler import extract_stocks_from_balance, is_balance_truncated

# 아래 픽스처는 mcp-trading/balance.js의 formatBalanceReport()가 실제로 생성하는
# 문자열을 그대로 재현한 것입니다. 근거(줄 번호 대신 심볼/테스트명으로 고정 — 이 저장소
# 안의 mcp-trading/ 디렉터리를 대상으로 git grep 으로 바로 확인 가능합니다):
#   - 생성부: mcp-trading/balance.js 의 formatBalanceReport() — 종목 줄 템플릿과
#     종목 사이 구분자 .join("\n\n")
#   - 검증부: mcp-trading/tests/order.test.js 의
#     "formatBalanceReport displays unsettled cash fields and account return rate" 테스트
# REAL_BALANCE_TEXT는 그 테스트의 기대값 리터럴을 그대로 복사한 것이므로, balance.js의
# 종목 줄 템플릿을 바꾸면 이 픽스처와 그 테스트의 기대값 양쪽을 함께 수정해야 합니다.
# 종목 한 건은 3줄 블록(헤더 줄 + 공백 2칸 들여쓰기 2줄)이며, 종목 사이에는 빈 줄이 하나 있습니다.
REAL_BALANCE_TEXT = """[계좌 잔고 현황]
- 총 평가금액: 1,210,000원
- 순자산금액: 1,210,000원
- 총 손익: 9,000원 (수익률: 🔴 ▲ +2.23%)
- 거래가능금액: 1,000,000원
- 정산중 금액(가수도): 1,009,000원
- 익일 정산예정금액: 790,000원
- 금일 매수/매도: 210,000원 / 0원

[보유 종목 리스트]
- 삼성전자 (005930) · 3주
  평단가 67,000원 → 평가금액 210,000원
  손익 +9,000원 · 수익률 🔴 ▲ +4.48%

- NAVER (035420) · 1주
  평단가 201,000원 → 평가금액 200,500원
  손익 -500원 · 수익률 🔵 ▼ -0.25%"""


class TestBalanceExtraction(unittest.TestCase):
    def test_extract_stocks_success(self):
        """실제 mcp-trading 출력 형식(3줄 블록, 종목 사이 빈 줄)에서 종목명만 정확히 추출되는지 확인합니다.

        들여쓴 2·3번째 줄("  평단가 ...", "  손익 ...")은 "- " 접두사가 없으므로
        종목으로 오인되지 않아야 합니다.
        """
        # 픽스처 무결성 전제: 종목 사이에 빈 줄이 실제로 있는지 확인합니다. 이 assertIn이
        # 실패한다면 REAL_BALANCE_TEXT가 축약되면서 종목 사이 빈 줄이 사라졌다는 뜻이며,
        # 아래 traversal은 그 빈 줄이 있어도 깨지지 않는지를 확인합니다.
        self.assertIn("\n\n", REAL_BALANCE_TEXT.split("[보유 종목 리스트]")[1])
        expected = ["삼성전자", "NAVER"]
        result = extract_stocks_from_balance(REAL_BALANCE_TEXT)
        self.assertEqual(result, expected)

    def test_extract_stocks_requires_dash_space_prefix(self):
        """파서가 의존하는 계약을 명시적으로 고정합니다: 종목 줄이 "- " 로 시작하지 않으면
        추출되지 않습니다 (extract_stocks_from_balance 의 line.startswith("- ") 조건).

        주의: 이 테스트는 파서 쪽 계약만 고정합니다. 포매터 쪽에는 "[보유 종목 리스트]"
        구간 안에서 종목 줄이 아닌 어떤 줄도 "- " 로 시작해서는 안 된다는 불변식이
        있어야 하며, 그 가드는 mcp-trading 쪽 테스트 스위트에 있어야 합니다.
        mcp-trading/tests/order.test.js 에서, "- " 로 시작하는 종목 줄만 걸러냈을 때
        페이지 상한 안내 문구("[안내]", "상한")가 섞여 들어오지 않는지 확인하는 테스트를
        찾아 확인하세요 — 정확한 테스트 이름 대신 이 동작을 기준으로 찾아야 테스트명이
        브랜치마다 바뀌어도 근거가 깨지지 않습니다.
        """
        mutated_text = REAL_BALANCE_TEXT.replace("- 삼성전자", "• 삼성전자").replace(
            "- NAVER", "• NAVER"
        )
        result = extract_stocks_from_balance(mutated_text)
        self.assertEqual(result, [])

    def test_extract_stocks_skips_empty_name(self):
        """mcp-trading/balance.js의 종목 줄 템플릿
        (`- ${h.prdt_name} (${h.pdno}) · ${formatQuantity(h.hldg_qty)}주\\n`)은
        prdt_name이 빈 문자열이면 "-  (035420) · 1주"처럼 대시-공백 접두사 뒤에
        공백이 하나 더 남는 줄을 만듭니다("(" 앞의 고정 공백 때문). 이 줄 옆에 정상
        종목이 있을 때, extract_stocks_from_balance의 `if name:` 가드가 빈 이름
        줄만 건너뛰고 정상 종목은 그대로 추출하는지 확인합니다.
        """
        balance_text = """[보유 종목 리스트]
- 삼성전자 (005930) · 3주
  평단가 67,000원 → 평가금액 210,000원
  손익 +9,000원 · 수익률 🔴 ▲ +4.48%

-  (035420) · 1주
  평단가 201,000원 → 평가금액 200,500원
  손익 -500원 · 수익률 🔵 ▼ -0.25%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["삼성전자"])

    def test_extract_stocks_empty(self):
        balance_text = """
[계좌 잔고 현황]
- 총 평가금액: 1000000원

[보유 종목 리스트]
보유 종목이 없습니다.
        """
        expected = []
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, expected)

    def test_extract_stocks_no_list(self):
        balance_text = "일반 텍스트"
        expected = []
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, expected)

    def test_extract_stocks_paren_in_name_uses_last_paren_group(self):
        """종목명 자체에 괄호가 있어도(코드 00104K, CJ4우(전환)) 마지막 "(코드)"
        그룹만 잘라내는지 고정한다. split("(")[0]으로 되돌리면(mutation) 첫
        괄호에서 잘려 "CJ4우"만 남아 실패한다.
        """
        balance_text = """[보유 종목 리스트]
- CJ4우(전환) (00104K) · 1주
  평단가 10,000원 → 평가금액 10,000원
  손익 +0원 · 수익률 🔴 ▲ +0.00%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["CJ4우(전환)"])

    def test_extract_stocks_two_paren_groups_in_name(self):
        """종목명에 괄호가 두 개 있어도(코드 F70102B96,
        룩셈부르크코어오피스(파생형)(A)) 줄 안의 세 괄호 중 마지막(=코드) 괄호
        에서만 잘라내는지 고정한다. split("(")[0]은 첫 괄호에서 잘려
        "룩셈부르크코어오피스"만 남기므로 이 테스트를 죽인다.
        """
        balance_text = """[보유 종목 리스트]
- 룩셈부르크코어오피스(파생형)(A) (F70102B96) · 1주
  평단가 10,000원 → 평가금액 10,000원
  손익 +0원 · 수익률 🔴 ▲ +0.00%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["룩셈부르크코어오피스(파생형)(A)"])

    def test_extract_stocks_paren_free_name_unchanged(self):
        """괄호 없는 기존 종목명("삼성전자")은 rsplit 도입 후에도 동작이 그대로
        임을 고정한다. rsplit 회귀뿐 아니라 replace("- ", "", 1)의 count=1 누락
        (접두사 "- "가 통째로 남아 "- 삼성전자"가 됨)도 함께 잡는다.
        """
        balance_text = """[보유 종목 리스트]
- 삼성전자 (005930) · 3주
  평단가 67,000원 → 평가금액 210,000원
  손익 +9,000원 · 수익률 🔴 ▲ +4.48%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["삼성전자"])

    def test_extract_stocks_interior_dash_space_preserved(self):
        """count=1로 맨 앞 접두사 "- "만 지우고 종목명 중간의 "- "는 보존되는지
        고정한다. count를 빼면(mutation) 전역 치환되어 "한국 - 전력"이 "한국
        전력"이 되어 실패한다.

        이 이름(코드 999999)은 stocks.json 마스터에 없는 합성 픽스처다 — 마스터에
        "- "를 포함하는 종목명은 0건이지만, prdt_name은 마스터가 아니라 KIS
        output1 응답에서 오므로 이 입력 형태를 배제할 수 없다.
        """
        balance_text = """[보유 종목 리스트]
- 한국 - 전력 (999999) · 1주
  평단가 10,000원 → 평가금액 10,000원
  손익 +0원 · 수익률 🔴 ▲ +0.00%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["한국 - 전력"])

    def test_extract_stocks_crlf_after_section_marker(self):
        """CRLF 입력(core.autocrlf=true 환경에서 balance.js가 CRLF로 체크아웃되는
        경우)에서도 종목이 정상 추출되는지 고정한다.

        참고: 이 입력 형태에서 .strip()은 결과에 영향을 주지 않는다. "\\r"만 남은
        첫 줄은 startswith("- ") 가드에서 어차피 걸러지기 때문이다.
        """
        balance_text = "[보유 종목 리스트]\r\n- 삼성전자 (005930) · 3주\r\n  평단가 67,000원 → 평가금액 210,000원\r\n  손익 +9,000원 · 수익률 🔴 ▲ +4.48%"
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["삼성전자"])

    def test_extract_stocks_missing_code_paren_is_rejected_with_warning(self):
        """코드 괄호가 없는 줄("- 삼성전자 · 3주")은 STOCK_LINE_RE에 매치되지 않아
        종목으로 추출되지 않고, 경고 로그가 기록되어야 합니다(이슈 #157).

        이 줄이 조용히 통과되면 종목명 자리에 "삼성전자 · 3주" 전체가 들어가
        뉴스·DART 조회 질의가 오염됩니다.
        """
        balance_text = """[보유 종목 리스트]
- 삼성전자 · 3주
  평단가 67,000원 → 평가금액 210,000원
  손익 +9,000원 · 수익률 🔴 ▲ +4.48%"""
        with self.assertLogs("backend.scheduler", level="WARNING") as log_ctx:
            result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, [])
        self.assertTrue(
            any("예상치 못한 잔고 종목 줄 형식" in msg for msg in log_ctx.output),
            "경고 로그에 '예상치 못한 잔고 종목 줄 형식'이 포함되어야 합니다.",
        )

    def test_extract_stocks_malformed_line_skipped_but_valid_line_extracted(self):
        """형식이 깨진 줄과 정상 줄이 섞여 있을 때, 깨진 줄만 건너뛰고 정상 줄은
        추출되어야 합니다. 경고 로그도 깨진 줄에 대해서만 기록됩니다.
        """
        balance_text = """[보유 종목 리스트]
- 삼성전자 · 3주
  평단가 67,000원 → 평가금액 210,000원
  손익 +9,000원 · 수익률 🔴 ▲ +4.48%

- NAVER (035420) · 1주
  평단가 201,000원 → 평가금액 200,500원
  손익 -500원 · 수익률 🔵 ▼ -0.25%"""
        with self.assertLogs("backend.scheduler", level="WARNING") as log_ctx:
            result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["NAVER"])
        self.assertTrue(
            any("예상치 못한 잔고 종목 줄 형식" in msg for msg in log_ctx.output),
        )

    def test_extract_stocks_notice_line_not_extracted_as_stock(self):
        """[안내] 잘림 문구("- "로 시작하지 않음)는 startswith("- ") 가드로 걸러지므로
        종목으로 추출되지 않습니다. STOCK_LINE_RE 도입 후에도 이 동작이 유지되는지
        회귀 방지로 확인합니다.
        """
        balance_text = (
            "[보유 종목 리스트]\n"
            "- 삼성전자 (005930) · 3주\n"
            "  평단가 67,000원 → 평가금액 210,000원\n"
            "  손익 +9,000원 · 수익률 🔴 ▲ +4.48%\n"
            "\n"
            "[안내] 페이지 상한(20회)에 도달하여 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다."
        )
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["삼성전자"])

# 아래 픽스처는 mcp-trading/balance.js의 formatTruncationNote()가 실제로 만드는 문구를
# 그대로 재현한 것입니다(이슈 #136). 근거:
#   - 생성부: mcp-trading/balance.js 의 formatTruncationNote() — 사유별 reasons 맵과
#     고정 접두/접미 리터럴("[안내] " / " 조회가 중단되어 일부 보유 종목이 위 목록에서
#     누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.")
#   - 결합 고정부: mcp-trading/tests/order.test.js 의 잘림 노트 테스트가
#     `assert.match(text, /조회가 중단되어/)`로 이 리터럴을 직접 단언한다. 이 픽스처는
#     JS 출력을 손으로 베낀 사본이라 그 자체로는 드리프트를 못 잡지만(JS가 바뀌어도
#     통과), JS 쪽 단언이 리터럴을 고정하므로 결합이 실제로 깨진다.
# formatTruncationNote()의 reasons 맵이나 고정 리터럴을 바꾸면 이 픽스처들과
# is_balance_truncated()의 매칭 대상 문자열(backend/scheduler.py의
# _BALANCE_TRUNCATION_MARKER)을 함께 검토해야 합니다.
TRUNCATION_NOTES_BY_REASON = {
    "max_pages": "\n\n[안내] 페이지 상한(20회)에 도달하여 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.",
    "time_budget": "\n\n[안내] 조회 시간 예산을 초과하여 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.",
    "no_cursor": "\n\n[안내] 연속조회 커서가 오지 않아 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.",
    "repeated_cursor": "\n\n[안내] 동일한 연속조회 커서가 반복되어 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.",
    "error": "\n\n[안내] 연속조회 중 오류가 발생하여 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.",
    # reasons 맵에 없는 사유(향후 추가될 수 있는 값)에 대한 fallback 문구.
    "__unknown__": "\n\n[안내] 연속조회가 완료되지 않아 조회가 중단되어 일부 보유 종목이 위 목록에서 누락되었을 수 있습니다. 실제 잔고는 별도로 확인하세요.",
}


class TestBalanceTruncationDetection(unittest.TestCase):
    """이슈 #136: 스케줄러가 잔고 연속조회 잘림을 감지하는 is_balance_truncated()를 검증합니다."""

    def test_detects_every_known_truncation_reason(self):
        """balance.js의 모든 truncated 사유(및 목록에 없는 사유의 fallback)에서
        True를 반환하는지 확인합니다. 사유별 reason 문구가 아니라 고정 접두/접미
        리터럴만 매칭하므로, 새 사유가 reasons 맵에 추가되어도 이 테스트는
        코드 변경 없이 계속 통과해야 합니다.
        """
        for reason, note in TRUNCATION_NOTES_BY_REASON.items():
            balance_text = (
                "[보유 종목 리스트]\n"
                "- 삼성전자 (005930) · 3주\n"
                "  평단가 67,000원 → 평가금액 210,000원\n"
                "  손익 +9,000원 · 수익률 🔴 ▲ +4.48%" + note
            )
            with self.subTest(reason=reason):
                self.assertTrue(is_balance_truncated(balance_text))

    def test_returns_false_for_untruncated_response(self):
        """잘리지 않은 정상 응답(REAL_BALANCE_TEXT)에서는 False를 반환해, 매 주기마다
        불필요한 경고가 울리지 않는지("늑대가 왔다" 오탐 방지) 확인합니다.
        """
        self.assertFalse(is_balance_truncated(REAL_BALANCE_TEXT))

    def test_returns_false_for_text_without_notice(self):
        balance_text = "일반 텍스트"
        self.assertFalse(is_balance_truncated(balance_text))

    def test_truncation_notice_is_not_extracted_as_a_stock(self):
        """실제 스케줄러 흐름을 재현합니다: 잘림이 발생하면 balance_text 하나에 보유
        종목 목록과 [안내] 잘림 문구가 함께 들어옵니다. is_balance_truncated()가
        True를 반환하는 동시에, extract_stocks_from_balance()는 그 문구를 절대
        종목명으로 추출하면 안 됩니다(둘 다 확인해야 [안내] 문구가 종목 목록에
        섞여 들어가는 회귀를 잡을 수 있습니다).
        """
        balance_text = (
            "[보유 종목 리스트]\n"
            "- 삼성전자 (005930) · 3주\n"
            "  평단가 67,000원 → 평가금액 210,000원\n"
            "  손익 +9,000원 · 수익률 🔴 ▲ +4.48%"
            + TRUNCATION_NOTES_BY_REASON["max_pages"]
        )

        self.assertTrue(is_balance_truncated(balance_text))
        stocks = extract_stocks_from_balance(balance_text)
        self.assertEqual(stocks, ["삼성전자"])


if __name__ == "__main__":
    unittest.main()
