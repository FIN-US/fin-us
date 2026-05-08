
import unittest
from backend.scheduler import extract_stocks_from_balance

class TestBalanceExtraction(unittest.TestCase):
    def test_extract_stocks_success(self):
        balance_text = """
[계좌 잔고 현황]
- 총 평가금액: 1000000원
- 순자산금액: 900000원

[보유 종목 리스트]
- 삼성전자 (005930): 10주 (평가금액: 700000원)
- SK하이닉스 (000660): 2주 (평가금액: 300000원)
        """
        expected = ["삼성전자", "SK하이닉스"]
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, expected)

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
