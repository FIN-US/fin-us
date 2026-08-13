"""backend/pii_mask.py 단위 테스트 (#230, F-17/NFR-05).

recognizer 3종(계좌번호/원화 금액/보유 수량) 각각과 복합 케이스, 왕복 무손실,
그리고 자리표시자 역치환의 fail-open 동작을 고정한다.
"""
import json
import pathlib

from backend.pii_mask import mask_pii, unmask_pii

_FIXTURE_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "mcp-trading" / "tests" / "fixtures" / "balance_report.json"
)


class TestAccountRecognizer:
    def test_masks_hyphenated_account_number(self):
        text = "계좌 12345678-01 잔고 조회"
        masked, mapping = mask_pii(text)
        assert masked == "계좌 <ACCOUNT_1> 잔고 조회"
        assert mapping == {"<ACCOUNT_1>": "12345678-01"}

    def test_masks_unhyphenated_account_number(self):
        text = "계좌 1234567801 잔고 조회"
        masked, mapping = mask_pii(text)
        assert masked == "계좌 <ACCOUNT_1> 잔고 조회"
        assert mapping == {"<ACCOUNT_1>": "1234567801"}

    def test_does_not_mask_longer_digit_runs(self):
        """11자리 이상 연속 숫자는 KIS_ACCOUNT_NO(10자리) 형식이 아니므로 매치하지 않는다."""
        text = "주문번호 123456780123"
        masked, mapping = mask_pii(text)
        assert masked == text
        assert mapping == {}


class TestAmountRecognizer:
    def test_masks_comma_grouped_won(self):
        masked, mapping = mask_pii("평가금액 1,234,567원 입니다")
        assert masked == "평가금액 <AMOUNT_1> 입니다"
        assert mapping == {"<AMOUNT_1>": "1,234,567원"}

    def test_masks_bare_digits_with_won(self):
        masked, mapping = mask_pii("평가금액 1234567원 입니다")
        assert masked == "평가금액 <AMOUNT_1> 입니다"
        assert mapping == {"<AMOUNT_1>": "1234567원"}

    def test_masks_man_won_unit(self):
        masked, mapping = mask_pii("평단가 123만원 수준")
        assert masked == "평단가 <AMOUNT_1> 수준"
        assert mapping == {"<AMOUNT_1>": "123만원"}

    def test_masks_labeled_bare_digits_without_won_suffix(self):
        """'원' 접미사가 없는 표기 편차(예: 총자산 1234567)는 금액 라벨 컨텍스트에서만 마스킹한다."""
        masked, mapping = mask_pii("총자산 1234567")
        assert masked == "총자산 <AMOUNT_1>"
        assert mapping == {"<AMOUNT_1>": "1234567"}

    def test_does_not_mask_unlabeled_bare_digits(self):
        """라벨 없는 bare 숫자는 종목코드·날짜 등과 구분할 수 없어 매치하지 않는다(의도된 한계)."""
        masked, mapping = mask_pii("주문수량 1234567")
        assert masked == "주문수량 1234567"
        assert mapping == {}

    def test_does_not_mask_percentage(self):
        """상대 비교(수익률 %)는 원본 그대로 유지되어야 한다 — (a) 방식의 전제."""
        masked, mapping = mask_pii("수익률 +4.48%")
        assert masked == "수익률 +4.48%"
        assert mapping == {}


class TestQuantityRecognizer:
    def test_masks_share_count_but_keeps_unit_suffix(self):
        masked, mapping = mask_pii("삼성전자 3주 보유")
        assert masked == "삼성전자 <QTY_1>주 보유"
        assert mapping == {"<QTY_1>": "3"}

    def test_does_not_mask_week_count(self):
        """'3주일'(3 weeks)은 보유 수량이 아니므로 매치하지 않는다."""
        masked, mapping = mask_pii("3주일 뒤 만기")
        assert masked == "3주일 뒤 만기"
        assert mapping == {}


class TestCompositeAndRoundTrip:
    def test_composite_prompt_masks_all_three_categories(self):
        text = "12345678-01 계좌, 삼성전자 3주, 평가금액 12,345,000원, 총자산 45,678,000원"
        masked, mapping = mask_pii(text)
        assert masked == (
            "<ACCOUNT_1> 계좌, 삼성전자 <QTY_1>주, 평가금액 <AMOUNT_1>, 총자산 <AMOUNT_2>"
        )
        # 상대 비교(총자산 > 평가금액)를 LLM이 자리표시자만으로 수행할 수 있어야 하므로
        # 서로 다른 자리표시자로 구분되어야 한다.
        assert mapping["<AMOUNT_1>"] != mapping["<AMOUNT_2>"]
        assert unmask_pii(masked, mapping) == text

    def test_round_trip_is_lossless_for_composite_prompt(self):
        text = "12345678-01 계좌, 삼성전자 3주, 평가금액 12,345,000원, 총자산 45,678,000원"
        masked, mapping = mask_pii(text)
        assert unmask_pii(masked, mapping) == text

    def test_round_trip_is_lossless_for_real_kis_balance_report(self):
        """mcp-trading/balance.js formatBalanceReport()의 실제 출력 형식(#137 공유 픽스처)에
        대해 왕복 무손실을 확인한다. 이 텍스트는 generate_morning_briefing()이 조립하는
        프롬프트에 그대로 들어가 llm_chat()을 통과한다.
        """
        fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
        text = fixture["normal"]["expected_text"]
        masked, mapping = mask_pii(text)

        # 실측: 모든 원화 금액과 보유 수량이 자리표시자로 치환되어야 한다.
        assert "1,210,000원" not in masked
        assert "67,000원" not in masked
        assert "· 3주" not in masked
        # 상대 비교에 쓰이는 수익률(%)은 원본 그대로 남아야 한다.
        assert "+4.48%" in masked
        assert "-0.25%" in masked
        # 종목코드는 PII가 아니므로(#230 범위 밖) 그대로 남아야 한다.
        assert "005930" in masked
        assert "035420" in masked

        assert unmask_pii(masked, mapping) == text

    def test_masking_is_idempotent_on_already_placeholder_text(self):
        """자리표시자 자체는 다시 마스킹 대상이 되지 않아야 한다(재귀적 오염 방지)."""
        text = "12345678-01 계좌, 삼성전자 3주, 평가금액 12,345,000원"
        masked_once, _ = mask_pii(text)
        masked_twice, mapping_twice = mask_pii(masked_once)
        assert masked_twice == masked_once
        assert mapping_twice == {}


class TestUnmaskFailOpen:
    """역치환 실패가 분석 결과를 죽이지 않아야 한다(#230 필수 요구사항)."""

    def test_unknown_placeholder_is_left_as_is_without_raising(self):
        """LLM이 존재하지 않는 자리표시자(<AMOUNT_9>)를 지어내도 예외 없이 원문을 보존한다."""
        mapping = {"<AMOUNT_1>": "12,345,000원"}
        text = "평가금액은 <AMOUNT_1>이고 총자산은 <AMOUNT_9>이다."
        restored = unmask_pii(text, mapping)
        assert restored == "평가금액은 12,345,000원이고 총자산은 <AMOUNT_9>이다."

    def test_malformed_placeholder_is_left_as_is_without_raising(self):
        """LLM이 자리표시자 형식을 변형해도(<AMOUNT_1> -> <AMOUNT1>) 예외 없이 원문을 보존한다."""
        mapping = {"<AMOUNT_1>": "12,345,000원"}
        text = "평가금액은 <AMOUNT1>이다."
        restored = unmask_pii(text, mapping)
        assert restored == "평가금액은 <AMOUNT1>이다."

    def test_empty_mapping_returns_text_unchanged(self):
        assert unmask_pii("자리표시자가 없는 응답", {}) == "자리표시자가 없는 응답"

    def test_empty_text_returns_as_is(self):
        assert unmask_pii("", {"<AMOUNT_1>": "1원"}) == ""
        assert unmask_pii(None, {"<AMOUNT_1>": "1원"}) is None


class TestMaskEmptyInput:
    def test_empty_string_returns_empty_mapping(self):
        masked, mapping = mask_pii("")
        assert masked == ""
        assert mapping == {}

    def test_none_returns_empty_mapping(self):
        masked, mapping = mask_pii(None)
        assert masked is None
        assert mapping == {}
