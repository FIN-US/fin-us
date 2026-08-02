
import unittest
from backend.scheduler import extract_stocks_from_balance

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
        """이슈 #153: 종목명 자체에 괄호가 있으면 종목코드는 항상 마지막 "(코드)"
        그룹에 있다. rsplit("(", 1)을 쓰면 마지막 "(" 에서 잘려 종목명의 괄호가
        보존되지만, split("(")[0]을 쓰면 첫 "(" 에서 잘려 종목명이 손상된다.

        "CJ4우(전환)" / 코드 00104K는 실제로 mcp-trading/data/stocks.json에 있는
        행이다(git grep '"code": "00104K"' mcp-trading/data/stocks.json으로 확인
        가능). 이 테스트는 split("(")[0]으로 되돌리면(mutation) "CJ4우"만 남아
        실패하므로, 첫 괄호가 아니라 마지막 괄호를 기준으로 자르는지를 검증한다.
        """
        balance_text = """[보유 종목 리스트]
- CJ4우(전환) (00104K) · 1주
  평단가 10,000원 → 평가금액 10,000원
  손익 +0원 · 수익률 🔴 ▲ +0.00%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["CJ4우(전환)"])

    def test_extract_stocks_two_paren_groups_in_name(self):
        """이슈 #153 수용 기준의 핵심 케이스: 종목명 자체가 괄호 그룹을 두 개
        가지고 있어도(코드 괄호까지 합치면 줄 안에 괄호가 세 개) rsplit("(", 1)은
        항상 마지막(=코드) 괄호에서만 잘라야 한다. split("(")[0]은 첫 괄호에서
        잘려 "룩셈부르크코어오피스"만 남기므로 이 테스트를 죽인다.

        "룩셈부르크코어오피스(파생형)(A)" / 코드 F70102B96은 실제로
        mcp-trading/data/stocks.json에 있는 행이다(git grep
        '"code": "F70102B96"' mcp-trading/data/stocks.json으로 확인 가능).
        """
        balance_text = """[보유 종목 리스트]
- 룩셈부르크코어오피스(파생형)(A) (F70102B96) · 1주
  평단가 10,000원 → 평가금액 10,000원
  손익 +0원 · 수익률 🔴 ▲ +0.00%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["룩셈부르크코어오피스(파생형)(A)"])

    def test_extract_stocks_paren_free_name_unchanged(self):
        """이슈 #153 수용 기준: 괄호가 없는 기존 종목명("삼성전자")의 동작은
        rsplit("(", 1) 도입 후에도 split("(")[0]과 결과가 같아야 한다(줄 안에
        괄호가 하나뿐이므로 첫 괄호와 마지막 괄호가 같은 지점). 이 테스트는
        rsplit 관련 회귀뿐 아니라 replace("- ", "", 1) count=1 도입으로 접두사
        "- " 제거가 깨지는 회귀도 함께 잡는다(count=1을 빼먹고 "- "를 통째로
        남기면 "- 삼성전자"가 되어 실패한다).
        """
        balance_text = """[보유 종목 리스트]
- 삼성전자 (005930) · 3주
  평단가 67,000원 → 평가금액 210,000원
  손익 +9,000원 · 수익률 🔴 ▲ +4.48%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["삼성전자"])

    def test_extract_stocks_interior_dash_space_preserved(self):
        """이슈 #153 "함께 볼 것": replace("- ", "")에 count가 없으면 전역
        치환되어 종목명 중간의 "- "까지 지워진다("한국 - 전력" → "한국 전력").
        count=1로 좁히면 맨 앞 접두사 "- "만 지워지고 중간의 "- "는 보존되어야
        한다. mcp-trading/data/stocks.json 마스터에는 "- "를 포함하는 종목명이
        0건이지만(git grep로 확인), prdt_name은 마스터가 아니라 KIS output1
        응답에서 오므로 이 입력 형태를 배제할 수 없다.

        이 테스트는 replace("- ", "")로 되돌리면(count 제거, mutation) "한국
        전력"이 되어 실패하므로 count=1이 실제로 적용되고 있는지를 검증한다.

        stocks.json 마스터에 이 이름을 가진 실제 종목이 없으므로(0건, git grep로
        확인) 코드는 임의값(999999)을 쓴다 — prdt_name이 마스터가 아니라 KIS
        output1에서 온다는 이슈 본문의 지적대로, 이 테스트는 마스터에 없는
        가상의 이론적 입력을 재현한다.
        """
        balance_text = """[보유 종목 리스트]
- 한국 - 전력 (999999) · 1주
  평단가 10,000원 → 평가금액 10,000원
  손익 +0원 · 수익률 🔴 ▲ +0.00%"""
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["한국 - 전력"])

    def test_extract_stocks_crlf_after_section_marker(self):
        """.strip() 프로브(이슈 #153 "함께 볼 것"): [보유 종목 리스트] 섹션
        마커 뒤에 CRLF가 온 입력(예: mcp-trading/balance.js가 core.autocrlf=true
        환경에서 CRLF로 체크아웃되어 raw 템플릿 리터럴 개행이 "\\r\\n"이 되는
        경우 — git config core.autocrlf 및 mcp-trading/balance.js가 실제로
        CRLF로 체크아웃되어 있음을 확인함)에서도 정상 추출되는지 확인한다.

        .strip()을 제거해도(mutation) 이 테스트는 여전히 통과한다: "\\r\\n"을
        "\\n"으로만 split하면 첫 줄이 "\\r" 하나만 남는 줄이 되는데, 이 줄은
        "- "로 시작하지 않으므로 line.startswith("- ") 가드에서 걸러진다.
        즉 이 테스트는 .strip()이 죽지 않는다는 것, 다시 말해 .strip()이
        이 입력 형태에 대해서도 vestigial(죽은 코드)임을 보여준다.
        """
        balance_text = "[보유 종목 리스트]\r\n- 삼성전자 (005930) · 3주\r\n  평단가 67,000원 → 평가금액 210,000원\r\n  손익 +9,000원 · 수익률 🔴 ▲ +4.48%"
        result = extract_stocks_from_balance(balance_text)
        self.assertEqual(result, ["삼성전자"])

if __name__ == "__main__":
    unittest.main()
