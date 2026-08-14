"""backend/pii_mask.py 단위 테스트 (#230, F-17/NFR-05).

recognizer 3종(계좌번호/원화 금액/보유 수량) 각각과 복합 케이스, 왕복 무손실,
그리고 자리표시자 역치환의 fail-open 동작을 고정한다.
"""
import json
import pathlib
import re

from backend.pii_mask import mask_pii, unmask_pii

_FIXTURE_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "mcp-trading" / "tests" / "fixtures" / "balance_report.json"
)

# 자리표시자는 <KIND_{scope}_{n}> 형태이고 scope는 mask_pii 호출마다 새로 뽑는 6자리
# hex nonce다(_Counter docstring 참고). nonce가 무작위라 정확한 문자열 단언을 그대로
# 쓸 수 없으므로, 아래 헬퍼로 nonce만 지워 <AMOUNT_1> 형태로 정규화한 뒤 단언한다 —
# 단언의 의도(어떤 값이 몇 번째 어떤 타입으로 분류됐는가)를 그대로 유지하기 위함이다.
#
# 이 정규화는 nonce 자체를 검증하지 못한다. nonce가 실제로 붙는다는 것과 호출마다
# 달라진다는 것은 TestPlaceholderScope에서 따로 고정한다.
_SCOPED_PLACEHOLDER_RE = re.compile(r"<(ACCOUNT|AMOUNT|QTY)_[0-9a-f]{6}_(\d+)>")


def _norm(text):
    """자리표시자의 호출별 nonce를 지워 <AMOUNT_1> 형태로 정규화한다(단언 가독성 유지용)."""
    return _SCOPED_PLACEHOLDER_RE.sub(r"<\1_\2>", text)


def _norm_mapping(mapping):
    return {_norm(key): value for key, value in mapping.items()}


class TestPlaceholderScope:
    """자리표시자에 호출별 nonce(scope)가 실제로 붙는지 고정한다.

    _norm 헬퍼가 nonce를 지우고 단언하므로 다른 테스트들은 nonce를 검증하지 못한다.
    이 클래스가 그 사각지대를 메운다.

    이 테스트들이 잡는 mutation: _Counter에서 scope를 제거해 자리표시자를 다시
    `<{kind}_{n}>`로 되돌리는 변경. nonce가 없으면 NAT 대화 히스토리를 넘나드는
    자리표시자 이름 충돌이 재발한다(_Counter docstring 참고).
    """

    def test_placeholder_carries_six_hex_scope(self):
        masked, mapping = mask_pii("계좌 12345678-01, 평가금액 1,234원, 삼성전자 3주")
        assert re.fullmatch(
            r"계좌 <ACCOUNT_[0-9a-f]{6}_1>, 평가금액 <AMOUNT_[0-9a-f]{6}_1>, "
            r"삼성전자 <QTY_[0-9a-f]{6}_1>주",
            masked,
        ), f"자리표시자에 6자리 hex scope가 붙어 있지 않다: {masked}"
        assert all(_SCOPED_PLACEHOLDER_RE.fullmatch(key) for key in mapping)

    def test_scope_differs_between_calls(self):
        """서로 다른 mask_pii 호출은 서로 다른 이름공간을 받아야 한다.

        이게 깨지면 같은 conversation_id의 이전 턴 자리표시자가 현재 턴 매핑에
        존재하게 되어, 값이 틀린 채로 조용히 역치환된다.
        """
        scopes = set()
        for _ in range(20):
            _, mapping = mask_pii("평가금액 1,234원")
            (key,) = mapping
            scopes.add(_SCOPED_PLACEHOLDER_RE.fullmatch(key).group(0))
        assert len(scopes) > 1, "호출마다 같은 scope가 나왔다 — nonce가 고정값이다"

    def test_placeholder_from_other_call_is_not_restored(self):
        """다른 호출의 자리표시자는 현재 매핑에 없으므로 역치환되지 않아야 한다."""
        _, mapping_turn1 = mask_pii("잔고 12,345,000원")
        (placeholder_turn1,) = mapping_turn1
        _, mapping_turn2 = mask_pii("5,000,000원 더")

        restored = unmask_pii(f"앞서 말씀하신 {placeholder_turn1} 기준으로는", mapping_turn2)
        assert "5,000,000원" not in restored, (
            "이전 턴 자리표시자가 현재 턴 값으로 잘못 복원됐다"
        )
        assert placeholder_turn1 in restored


class TestAccountRecognizer:
    def test_masks_hyphenated_account_number(self):
        text = "계좌 12345678-01 잔고 조회"
        masked, mapping = mask_pii(text)
        assert _norm(masked) == "계좌 <ACCOUNT_1> 잔고 조회"
        assert _norm_mapping(mapping) == {"<ACCOUNT_1>": "12345678-01"}

    def test_masks_unhyphenated_account_number(self):
        text = "계좌 1234567801 잔고 조회"
        masked, mapping = mask_pii(text)
        assert _norm(masked) == "계좌 <ACCOUNT_1> 잔고 조회"
        assert _norm_mapping(mapping) == {"<ACCOUNT_1>": "1234567801"}

    def test_does_not_mask_longer_digit_runs(self):
        """11자리 이상 연속 숫자는 KIS_ACCOUNT_NO(10자리) 형식이 아니므로 매치하지 않는다."""
        text = "주문번호 123456780123"
        masked, mapping = mask_pii(text)
        assert masked == text
        assert mapping == {}


class TestAmountRecognizer:
    def test_masks_comma_grouped_won(self):
        masked, mapping = mask_pii("평가금액 1,234,567원 입니다")
        assert _norm(masked) == "평가금액 <AMOUNT_1> 입니다"
        assert _norm_mapping(mapping) == {"<AMOUNT_1>": "1,234,567원"}

    def test_masks_bare_digits_with_won(self):
        masked, mapping = mask_pii("평가금액 1234567원 입니다")
        assert _norm(masked) == "평가금액 <AMOUNT_1> 입니다"
        assert _norm_mapping(mapping) == {"<AMOUNT_1>": "1234567원"}

    def test_masks_man_won_unit(self):
        masked, mapping = mask_pii("평단가 123만원 수준")
        assert _norm(masked) == "평단가 <AMOUNT_1> 수준"
        assert _norm_mapping(mapping) == {"<AMOUNT_1>": "123만원"}

    def test_masks_labeled_bare_digits_without_won_suffix(self):
        """'원' 접미사가 없는 표기 편차(예: 총자산 1234567)는 금액 라벨 컨텍스트에서만 마스킹한다."""
        masked, mapping = mask_pii("총자산 1234567")
        assert _norm(masked) == "총자산 <AMOUNT_1>"
        assert _norm_mapping(mapping) == {"<AMOUNT_1>": "1234567"}

    def test_does_not_mask_unlabeled_bare_digits(self):
        """라벨 없는 bare 숫자는 종목코드·날짜 등과 구분할 수 없어 매치하지 않는다(의도된 한계)."""
        masked, mapping = mask_pii("주문수량 1234567")
        assert masked == "주문수량 1234567"
        assert mapping == {}

    def test_ten_digit_amount_is_not_misclassified_as_account(self):
        """콤마 없는 10자리 금액(예: 1234567890원)은 KIS_ACCOUNT_NO(8+2=10자리)와 자릿수가
        겹치지만, '원' 접미사가 있으면 계좌번호일 수 없다 — 금액 정규식이 계좌번호 정규식보다
        먼저 적용되어야 AMOUNT로 분류된다. 순서가 반대로 바뀌면(계좌번호 먼저) 이 금액이
        <ACCOUNT_1>원처럼 잘못 분류된 채 자리표시자 타입이 LLM에게 틀린 문맥을 전달한다.
        """
        masked, mapping = mask_pii("평가금액 1234567890원 입니다")
        assert _norm(masked) == "평가금액 <AMOUNT_1> 입니다"
        assert _norm_mapping(mapping) == {"<AMOUNT_1>": "1234567890원"}

    def test_labeled_ten_digit_amount_is_not_misclassified_as_account(self):
        """'원' 없이 금액 라벨 뒤에만 오는 10자리 숫자도 계좌번호가 아니라 금액이다.

        위 test_ten_digit_amount_is_not_misclassified_as_account의 짝이다. "원" 접미사가
        계좌번호 후보를 배제하는 판별자인 것과 같은 논리로, "예수금"/"잔고" 같은 금액
        라벨도 판별자다 — 라벨 뒤 숫자는 정의상 계좌번호가 아니다.

        이 테스트가 잡는 mutation: _LABELED_AMOUNT_RE.sub을 다시 _ACCOUNT_RE.sub 뒤로
        옮기는 변경. 그러면 "예수금 1234567890"이 <ACCOUNT_...>로 오분류된다.
        """
        masked, mapping = mask_pii("예수금 1234567890")
        assert _norm(masked) == "예수금 <AMOUNT_1>"
        assert _norm_mapping(mapping) == {"<AMOUNT_1>": "1234567890"}

        masked, mapping = mask_pii("잔고 9876543210")
        assert _norm(masked) == "잔고 <AMOUNT_1>"
        assert _norm_mapping(mapping) == {"<AMOUNT_1>": "9876543210"}

    def test_hyphenated_account_after_amount_label_stays_account(self):
        """금액 라벨 뒤라도 하이픈 계좌 표기는 ACCOUNT로 남아야 한다.

        _LABELED_AMOUNT_RE를 _ACCOUNT_RE 앞으로 옮기면서 amount 그룹에 단
        배제 lookahead `(?!-\\d{2}(?!\\d))`가 이걸 보장한다. 이 테스트가 잡는 mutation:
        그 lookahead 제거. 제거하면 "12345678"만 AMOUNT로 잘리고 "-01"이 원문에 남는다.
        """
        text = "잔고 12345678-01"
        masked, mapping = mask_pii(text)
        assert _norm(masked) == "잔고 <ACCOUNT_1>"
        assert _norm_mapping(mapping) == {"<ACCOUNT_1>": "12345678-01"}
        assert unmask_pii(masked, mapping) == text

    def test_account_and_amount_coexist_correctly(self):
        """계좌번호와 금액이 한 문장에 함께 있어도 각자의 타입으로 정확히 분류되어야 한다."""
        text = "계좌번호 1234567801 예수금 3,000,000원"
        masked, mapping = mask_pii(text)
        assert _norm(masked) == "계좌번호 <ACCOUNT_1> 예수금 <AMOUNT_1>"
        assert _norm_mapping(mapping) == {
            "<ACCOUNT_1>": "1234567801",
            "<AMOUNT_1>": "3,000,000원",
        }
        assert unmask_pii(masked, mapping) == text

    def test_does_not_mask_percentage(self):
        """상대 비교(수익률 %)는 원본 그대로 유지되어야 한다 — (a) 방식의 전제."""
        masked, mapping = mask_pii("수익률 +4.48%")
        assert masked == "수익률 +4.48%"
        assert mapping == {}


class TestQuantityRecognizer:
    def test_masks_share_count_but_keeps_unit_suffix(self):
        masked, mapping = mask_pii("삼성전자 3주 보유")
        assert _norm(masked) == "삼성전자 <QTY_1>주 보유"
        assert _norm_mapping(mapping) == {"<QTY_1>": "3"}

    def test_does_not_mask_week_count(self):
        """'3주일'(3 weeks)은 보유 수량이 아니므로 매치하지 않는다."""
        masked, mapping = mask_pii("3주일 뒤 만기")
        assert masked == "3주일 뒤 만기"
        assert mapping == {}

    def test_masks_comma_grouped_quantity_entirely(self):
        """콤마가 들어간 보유 수량은 앞자리까지 통째로 마스킹돼야 한다.

        mcp-trading/balance.js:208-215의 formatQuantity()가 toLocaleString("ko-KR")을
        쓰므로 1,000주 이상은 "1,234주"처럼 콤마가 들어간다. 콤마를 처리하지 못하면
        "1,"이 마스킹되지 않고 남아 **보유 수량의 앞자리가 그대로 외부 LLM으로 나간다**
        — F-17이 막으려던 바로 그 유출이다. 같은 사실을 이미 반영한 선례가
        backend/scheduler.py:171-175의 r"\\)\\s*·\\s*([\\d,]+)주"다.

        이 테스트가 잡는 mutation: _QTY_RE에서 콤마 지원 제거
        (r"(?<![\\d,])\\d(?:[\\d,]*\\d)?" -> r"(?<!\\d)\\d+").

        공유 픽스처(mcp-trading/tests/fixtures/balance_report.json)의 hldg_qty는
        "3"/"1"뿐이라 이 케이스를 덮지 못한다 — 그래서 별도로 고정한다.
        """
        text = "- 삼성전자 (005930) · 1,234주"
        masked, mapping = mask_pii(text)
        assert _norm(masked) == "- 삼성전자 (005930) · <QTY_1>주"
        assert "1," not in masked, "수량의 앞자리(1,)가 마스킹되지 않고 남았다"
        assert _norm_mapping(mapping) == {"<QTY_1>": "1,234"}
        assert unmask_pii(masked, mapping) == text

        text = "- 카카오 (035720) · 12,345주"
        masked, mapping = mask_pii(text)
        assert _norm(masked) == "- 카카오 (035720) · <QTY_1>주"
        assert "12," not in masked
        assert unmask_pii(masked, mapping) == text

    def test_does_not_mask_period_idioms_with_share_unit(self):
        """'주'가 기간 단위로 쓰인 표현은 보유 수량이 아니므로 마스킹하지 않는다.

        '52주 신고가'는 주식 뉴스에서 가장 흔한 관용구인데,
        backend/services.py:497-512의 check_signal_significance()가 외부 뉴스 원문
        1000자를 그대로 프롬프트에 넣으므로(mcp-news/index.js의 네이버 뉴스 검색
        title/description, 필터링 없음) 과탐이 곧바로 판정 품질을 떨어뜨린다.

        이 테스트가 잡는 mutation: _QTY_RE의 `(?![가-힣])` 또는
        `(?!\\s*(?:신고가|...))` 배제 lookahead 제거.
        """
        for text in [
            "52주 신고가 갱신",
            "52주 신저가 경신",
            "52주 최고가 대비",
            "3주째 상승세",
            "2주차 실적",
            "1주당 배당",
        ]:
            masked, mapping = mask_pii(text)
            assert masked == text, f"기간 표현이 수량으로 오분류됐다: {text} -> {masked}"
            assert mapping == {}

    def test_masks_quantity_in_free_form_user_message(self):
        """텔레그램 자유 입력 경로의 보유 수량도 마스킹돼야 한다.

        backend/telegram_commands.py:1349-1356의 _handle_chat_fallback은 사용자 원문을
        가공 없이 llm_chat으로 보낸다. 잔고 리포트의 "· {qty}주" 형태로만 앵커링하면
        이 경로의 실제 보유 수량을 통째로 놓치므로, 앵커 없는 "3주"도 잡아야 한다.
        """
        masked, mapping = mask_pii("삼성전자 3주 보유중인데 팔아야 할까요?")
        assert _norm(masked) == "삼성전자 <QTY_1>주 보유중인데 팔아야 할까요?"
        assert _norm_mapping(mapping) == {"<QTY_1>": "3"}

    def test_qty_at_line_end_is_masked_when_next_line_starts_with_excluded_word(self):
        """관용구 배제는 줄을 넘어가면 안 된다 — 넘어가면 수량이 통째로 유출된다.

        _QTY_RE의 관용구 배제 lookahead를 `(?!\\s*(?:...|평균|...))`로 쓰면 \\s*가
        개행을 넘는다. 잔고 리포트는 수량이 줄 끝에 오는 여러 줄 텍스트이므로
        (mcp-trading\\balance.js:278의 "- {종목} ({코드}) · {수량}주\\n  평단가 ..."),
        다음 줄이 배제 목록 단어로 시작하면 배제가 잘못 발동해 **보유 수량이 전혀
        마스킹되지 않은 채 외부 LLM으로 나간다.** 배제는 같은 줄의 후속 어절만
        보려는 것이므로 [ \\t]*로 한정해야 한다.

        오늘 balance.js의 실제 문구는 "평단가"라 이 결합이 발동하지 않는다. 그래서
        공유 픽스처 왕복 테스트는 green이고, 이 회귀는 픽스처로 잡히지 않는다 —
        문구가 "평균단가"로 바뀌는 순간 조용히 유출이 생긴다. 그 결합 자체를 여기서
        끊어 고정한다.

        이 테스트가 잡는 mutation: _QTY_RE의 `(?![ \\t]*(?:...))` -> `(?!\\s*(?:...))`.
        """
        text = "- 삼성전자 (005930) · 1,234주\n  평균단가 67,000원"
        masked, mapping = mask_pii(text)

        assert "1,234" not in masked, "수량이 마스킹되지 않고 그대로 남았다"
        assert _norm_mapping(mapping)["<QTY_1>"] == "1,234"
        assert unmask_pii(masked, mapping) == text


class TestCompositeAndRoundTrip:
    def test_composite_prompt_masks_all_three_categories(self):
        text = "12345678-01 계좌, 삼성전자 3주, 평가금액 12,345,000원, 총자산 45,678,000원"
        masked, mapping = mask_pii(text)
        assert _norm(masked) == (
            "<ACCOUNT_1> 계좌, 삼성전자 <QTY_1>주, 평가금액 <AMOUNT_1>, 총자산 <AMOUNT_2>"
        )
        # 상대 비교(총자산 > 평가금액)를 LLM이 자리표시자만으로 수행할 수 있어야 하므로
        # 서로 다른 자리표시자로 구분되어야 한다.
        normalized = _norm_mapping(mapping)
        assert normalized["<AMOUNT_1>"] != normalized["<AMOUNT_2>"]
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
    """역치환 실패가 분석 결과를 죽이지 않아야 한다(#230 필수 요구사항).

    두 갈래를 모두 고정한다:
    (1) 형식은 맞지만(=_PLACEHOLDER_RE에 매치) 매핑에 없는 자리표시자 -> 원문 유지 + 경고 로그
    (2) 형식 자체가 어긋난 변형(scope 없는 구형식 포함) -> 정규식에 안 걸려 자연히 원문 유지
    """

    def test_unknown_scoped_placeholder_is_left_as_is_with_warning(self, caplog):
        """LLM이 존재하지 않는 자리표시자를 지어내도 예외 없이 원문을 보존하고 경고를 남긴다.

        scope 도입 후 "매핑에 없는 자리표시자"의 대표 사례는 다른 호출(=이전 대화 턴)의
        scope를 가진 자리표시자다. 형식은 유효하므로 _PLACEHOLDER_RE에 매치되고,
        매핑에 없으니 경고 로그 경로를 탄다.
        """
        mapping = {"<AMOUNT_deadbe_1>": "12,345,000원"}
        text = "평가금액은 <AMOUNT_deadbe_1>이고 총자산은 <AMOUNT_deadbe_9>이다."
        with caplog.at_level("WARNING", logger="backend.pii_mask"):
            restored = unmask_pii(text, mapping)
        assert restored == "평가금액은 12,345,000원이고 총자산은 <AMOUNT_deadbe_9>이다."
        assert "<AMOUNT_deadbe_9>" in caplog.text, (
            "매핑에 없는 자리표시자를 만났는데 경고 로그가 남지 않았다"
        )

    def test_legacy_unscoped_placeholder_is_left_as_is_without_raising(self):
        """scope 없는 구형식(<AMOUNT_9>)은 _PLACEHOLDER_RE에 매치되지 않아 그대로 남는다.

        이것이 nonce 도입의 핵심 효과다 — 다른 이름공간의 자리표시자가 현재 매핑의
        값으로 조용히 복원되는 대신 원문 그대로 남는다.
        """
        mapping = {"<AMOUNT_deadbe_1>": "12,345,000원"}
        text = "총자산은 <AMOUNT_9>이다."
        restored = unmask_pii(text, mapping)
        assert restored == "총자산은 <AMOUNT_9>이다."

    def test_malformed_placeholder_is_left_as_is_without_raising(self):
        """LLM이 자리표시자 형식을 변형해도(-> <AMOUNT1>) 예외 없이 원문을 보존한다."""
        mapping = {"<AMOUNT_deadbe_1>": "12,345,000원"}
        text = "평가금액은 <AMOUNT1>이다."
        restored = unmask_pii(text, mapping)
        assert restored == "평가금액은 <AMOUNT1>이다."

    def test_empty_mapping_returns_text_unchanged(self):
        assert unmask_pii("자리표시자가 없는 응답", {}) == "자리표시자가 없는 응답"

    def test_empty_text_returns_as_is(self):
        assert unmask_pii("", {"<AMOUNT_deadbe_1>": "1원"}) == ""
        assert unmask_pii(None, {"<AMOUNT_deadbe_1>": "1원"}) is None


class TestMaskEmptyInput:
    def test_empty_string_returns_empty_mapping(self):
        masked, mapping = mask_pii("")
        assert masked == ""
        assert mapping == {}

    def test_none_returns_empty_mapping(self):
        masked, mapping = mask_pii(None)
        assert masked is None
        assert mapping == {}
