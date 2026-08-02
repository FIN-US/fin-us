
import unittest
from backend.scheduler import extract_stocks_from_balance

# 아래 픽스처는 mcp-trading/balance.js의 formatBalanceReport()가 실제로 생성하는
# 문자열을 그대로 재현한 것입니다. 근거:
#   - 생성부: mcp-trading/balance.js:90-119 (특히 종목 줄 템플릿 :97-101, join("\n\n") :103)
#   - 검증부: mcp-trading/tests/order.test.js:92-146
#     ("formatBalanceReport displays unsettled cash fields and account return rate" 테스트의
#      입력/기대값을 그대로 옮겼습니다.)
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
        종목으로 오인되지 않아야 합니다. 결과 개수가 정확히 2개(들여쓴 줄이 섞이지 않음)인지도 함께 검증합니다.
        """
        # 픽스처 무결성 전제: 종목 사이에 빈 줄이 실제로 있는지 확인합니다. 이 assertIn이
        # 실패한다면 REAL_BALANCE_TEXT가 축약되면서 종목 사이 빈 줄이 사라졌다는 뜻이며,
        # 아래 traversal은 그 빈 줄이 있어도 깨지지 않는지를 확인합니다.
        self.assertIn("\n\n", REAL_BALANCE_TEXT.split("[보유 종목 리스트]")[1])
        expected = ["삼성전자", "NAVER"]
        result = extract_stocks_from_balance(REAL_BALANCE_TEXT)
        self.assertEqual(result, expected)
        self.assertEqual(len(result), 2)

    def test_extract_stocks_requires_dash_space_prefix(self):
        """파서가 의존하는 계약을 명시적으로 고정합니다: 종목 줄이 "- " 로 시작하지 않으면
        추출되지 않습니다 (extract_stocks_from_balance 의 line.startswith("- ") 조건).

        주의: 이 테스트는 파서 쪽 계약만 고정하며, mcp-trading 이 실제로 "- " 를 계속
        내보내는지는 검증하지 못합니다. 그 크로스-레포 가드는 mcp-trading 쪽 테스트
        ("...truncation note stays outside the holdings section...")가 담당합니다.
        """
        mutated_text = REAL_BALANCE_TEXT.replace("- 삼성전자", "• 삼성전자").replace(
            "- NAVER", "• NAVER"
        )
        result = extract_stocks_from_balance(mutated_text)
        self.assertEqual(result, [])

    def test_extract_stocks_skips_empty_name(self):
        """mcp-trading/balance.js의 종목 줄 템플릿
        (`- ${h.prdt_name} (${h.pdno}) · ${formatQuantity(h.hldg_qty)}주\\n`)은
        prdt_name이 빈 문자열이면 "-  (005930) · 3주"처럼 대시-공백 접두사 뒤에
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

if __name__ == "__main__":
    unittest.main()
