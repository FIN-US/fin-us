"""backend/stock_code.py 공용 모듈 테스트 (#140).

_STOCK_CODE_EXTRACT_RE, _STOCK_CODE_RE / _looks_like_stock_code, _ORDERABLE_STOCK_CODE_RE에 대한
순수 단위 테스트. 각 호출부 고유 동작(services.py의 빈 문자열 폴백,
telegram_commands.py의 조기 거절)은 각자의 테스트 파일에 남아 있다.
"""
import pytest

from backend import stock_code
from backend.stock_code import (
    _ORDERABLE_STOCK_CODE_RE,
    _STOCK_CODE_EXTRACT_RE,
    _has_code_digit,
    _looks_like_stock_code,
)


# ──────────────────────────────────────────────────────────────────────────
# _STOCK_CODE_EXTRACT_RE — MCP 응답에서 코드 추출
# ──────────────────────────────────────────────────────────────────────────

class TestStockCodeExtractRe:
    def test_numeric_six_char(self):
        """6자리 숫자 코드(KIS 표준)를 추출한다."""
        m = _STOCK_CODE_EXTRACT_RE.search("삼성전자 (005930, KOSPI)")
        assert m is not None
        assert m.group(1) == "005930"

    def test_alphanumeric_six_char(self):
        """영숫자 6자 ETN 코드를 추출한다."""
        m = _STOCK_CODE_EXTRACT_RE.search("덕양에너젠 (0001A0, KOSDAQ)")
        assert m is not None
        assert m.group(1) == "0001A0"

    def test_seven_char_etn(self):
        """7자 ETN 코드를 추출한다(389종목)."""
        m = _STOCK_CODE_EXTRACT_RE.search(
            "신한 레버리지 다우존스지수 선물 ETN(H) (Q500020, KOSPI)"
        )
        assert m is not None
        assert m.group(1) == "Q500020"

    def test_nine_char_fund_code(self):
        """9자 펀드 코드(75종목)를 추출한다 — {6,7}로 좁히면 조용히 실패하는 경로."""
        m = _STOCK_CODE_EXTRACT_RE.search(
            "한투글로벌넥스트웨이브1(A) (F70100026, KOSPI)"
        )
        assert m is not None
        assert m.group(1) == "F70100026"

    def test_parentheses_in_stock_name_anchors_on_code_comma(self):
        """종목명 안에 닫히지 않는 괄호가 있어도 코드+쉼표 앵커가 올바른 코드를 뽑는다.

        종목마스터 실제 항목: "KIWOOM 엔비디아미국30년국채혼합액티브(H (0015E0, KOSPI)"
        """
        m = _STOCK_CODE_EXTRACT_RE.search(
            "KIWOOM 엔비디아미국30년국채혼합액티브(H (0015E0, KOSPI)"
        )
        assert m is not None
        assert m.group(1) == "0015E0"

    def test_no_match_returns_none(self):
        """코드+쉼표 패턴이 없으면 None을 반환한다."""
        assert _STOCK_CODE_EXTRACT_RE.search("종목을 찾지 못했습니다") is None


# ──────────────────────────────────────────────────────────────────────────
# _has_code_digit
# ──────────────────────────────────────────────────────────────────────────

class TestHasCodeDigit:
    def test_all_digits(self):
        assert _has_code_digit("005930")

    def test_mixed_alphanumeric(self):
        assert _has_code_digit("0001A0")

    def test_no_digits_returns_false(self):
        assert not _has_code_digit("SAMSUNG")
        assert not _has_code_digit("SIMPAC")
        assert not _has_code_digit("")


# ──────────────────────────────────────────────────────────────────────────
# _looks_like_stock_code — 입력 판정 (MCP 조회 생략 여부)
# ──────────────────────────────────────────────────────────────────────────

class TestLooksLikeStockCode:
    # 수용 케이스
    def test_six_char_numeric(self):
        assert _looks_like_stock_code("005930")

    def test_six_char_alphanumeric(self):
        assert _looks_like_stock_code("0001A0")

    def test_seven_char_etn(self):
        assert _looks_like_stock_code("Q500020")

    def test_nine_char_fund_code(self):
        """{6,7} → {6,9} 확대의 핵심 회귀 가드.

        뮤테이션 ①: _STOCK_CODE_RE를 {6,7}로 되돌리면 이 케이스가 red가 된다.
        F70100026은 종목마스터 실제 항목(코드 길이 9, 펀드 75종 중 하나).
        #140 이전에는 False → MCP 서브프로세스를 불필요하게 호출했다.
        """
        assert _looks_like_stock_code("F70100026")

    def test_lowercase_input_normalised(self):
        """소문자 입력도 upper() 정규화 후 판정한다."""
        assert _looks_like_stock_code("005930")
        assert _looks_like_stock_code("f70100026")  # 9자 펀드, 소문자

    def test_leading_trailing_whitespace_stripped(self):
        assert _looks_like_stock_code("  005930  ")

    # 거절 케이스
    def test_rejects_all_alpha_no_digit(self):
        """숫자가 없는 6~7자 영문은 종목명으로 보고 MCP로 넘긴다."""
        assert not _looks_like_stock_code("SAMSUNG")
        assert not _looks_like_stock_code("SIMPAC")  # 코드 형태 이름, 숫자 없음

    def test_rejects_korean_name(self):
        assert not _looks_like_stock_code("삼성전자")

    def test_rejects_empty_string(self):
        assert not _looks_like_stock_code("")

    def test_rejects_too_short(self):
        assert not _looks_like_stock_code("00593")  # 5자

    def test_rejects_too_long(self):
        """10자 이상은 종목코드가 아니다(현재 마스터 최장 9자)."""
        assert not _looks_like_stock_code("F701000260")  # 10자

    def test_rejects_string_with_space_inside(self):
        """내부 공백이 있으면 strip() 후에도 코드 형태가 아니다."""
        assert not _looks_like_stock_code("005 930")


# ──────────────────────────────────────────────────────────────────────────
# _ORDERABLE_STOCK_CODE_RE — 주문 가능 코드 판정
# mcp-trading/order.js:77-84 정책의 백엔드 복제본
# ──────────────────────────────────────────────────────────────────────────

class TestOrderableStockCodeRe:
    """뮤테이션 ②: _ORDERABLE_STOCK_CODE_RE 판정을 무력화(항상 True 반환 등)하면
    test_rejects_alphanumeric / test_rejects_nine_char_fund 가 red가 된다.
    """

    def test_accepts_six_char_numeric(self):
        assert _ORDERABLE_STOCK_CODE_RE.fullmatch("005930")

    def test_accepts_seven_char_numeric(self):
        assert _ORDERABLE_STOCK_CODE_RE.fullmatch("1234567")

    def test_rejects_alphanumeric(self):
        """영숫자 코드는 order.js가 전용 메시지로 거절한다 — 백엔드도 동일하게 거절."""
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("0001A0")

    def test_rejects_nine_char_fund(self):
        r"""9자 펀드 코드는 order.js 두 번째 가드(/^\d{6,7}$/)에서 거절된다.

        _looks_like_stock_code가 True를 반환하도록 확대됐어도(_STOCK_CODE_RE {6,9}),
        주문 가능 여부는 여전히 숫자 6~7자로 제한된다 — 정책 불변.
        """
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("F70100026")

    def test_rejects_five_char(self):
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("00593")

    def test_rejects_eight_char(self):
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("12345678")

    def test_rejects_empty(self):
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("")

    def test_rejects_fullwidth_digits(self):
        r"""전각 숫자(０ 등)는 [0-9] 패턴에 매치되지 않아야 한다.

        Python의 `\d`는 전각 숫자를 포함하므로 `[0-9]`로 명시한 이유를 고정한다.
        """
        assert not _ORDERABLE_STOCK_CODE_RE.fullmatch("００５９３０")
