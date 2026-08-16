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

    def test_placeholder_from_other_call_returns_neutral_phrase(self):
        """다른 호출의 자리표시자는 현재 매핑에 없으므로 중립 문구로 치환되어야 한다.

        이전 턴 값도, 내부 토큰도 사용자에게 노출되어서는 안 된다.
        텔레그램 스레드(conversation_id="telegram:{chat_id}")는 max_history_messages=30
        창에서 이전 자리표시자가 재등장하는 상시 경로이므로 이 동작이 중요하다.
        """
        _, mapping_turn1 = mask_pii("잔고 12,345,000원")
        (placeholder_turn1,) = mapping_turn1
        _, mapping_turn2 = mask_pii("5,000,000원 더")

        restored = unmask_pii(f"앞서 말씀하신 {placeholder_turn1} 기준으로는", mapping_turn2)
        assert "5,000,000원" not in restored, "이전 턴 자리표시자가 현재 턴 값으로 잘못 복원됐다"
        assert "12,345,000원" not in restored, "이전 턴의 원값이 노출됐다"
        assert placeholder_turn1 not in restored, "내부 토큰이 사용자 화면에 노출됐다"
        assert "이전에 언급된 금액" in restored


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

    def test_masks_comma_grouped_unit_amount_entirely(self):
        """콤마가 들어간 만/억 단위 금액은 앞자리까지 통째로 마스킹돼야 한다.

        기존 `(?<!\\d)` lookbehind만으로는 "3,000만원"에서 매치가 콤마 **뒤**
        ("000만원")부터 시작돼 앞자리 "3,"가 마스킹되지 않고 남아 외부 LLM으로
        나간다. _QTY_RE가 이미 고친 것과 같은 결함이다
        (TestQuantityRecognizer.test_masks_comma_grouped_quantity_entirely가 수량판으로
        같은 결함을 고정하고 있다).

        이 테스트가 잡는 mutation:
        - `(?:,\\d{3})*` 제거: 정상 콤마 표기("3,000만원")에서 "000만원"만
          자리표시자가 되고 "3,"가 남는다 (아래 루프 1이 FAILED).
        - `(?<![\\d,])` → `(?<!\\d)` (콤마 배제 제거): 비정상 콤마 표기에서
          앞자리가 유출된 채 뒷부분만 자리표시자가 되는 부분 마스킹이 발생한다
          — "1,23만원" → "1,<AMOUNT_1>" 형태 (아래 루프 2가 FAILED).

        대조군(`test_masks_man_won_unit` 등)이 깨지지 않는지 함께 확인한다.
        """
        # 루프 1: 정상 콤마 표기가 통째로 마스킹된다.
        # 잡는 mutation: `(?:,\\d{3})*` 제거.
        for text, expected_masked, expected_original in [
            ("3,000만원", "<AMOUNT_1>", "3,000만원"),
            ("1,234만원", "<AMOUNT_1>", "1,234만원"),
            ("평가금액 12,345만원", "평가금액 <AMOUNT_1>", "12,345만원"),
            ("1,000억원 규모", "<AMOUNT_1> 규모", "1,000억원"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected_masked, (
                f"콤마 포함 만/억 단위 금액이 통째로 마스킹되지 않았다: {text!r} -> {masked!r}"
            )
            (ph,) = mapping
            assert mapping[ph] == expected_original, (
                f"매핑 원값이 틀렸다: {mapping[ph]!r} != {expected_original!r}"
            )
            assert unmask_pii(masked, mapping) == text

        # 루프 2: 비정상 콤마 표기(3자리로 끊기지 않은 표기)에서 부분 마스킹이 없어야 한다.
        # 잡는 mutation: `(?<![\d,])` → `(?<!\d)` (콤마 배제 제거).
        # mutant에서는 "1,23만원" → "1,<AMOUNT_1>"처럼 콤마 앞자리가 유출된 채
        # 뒷부분만 자리표시자가 된다 — "숫자,<AMOUNT" 패턴으로 관측된다.
        # 현재 정규식은 비정상 콤마 표기를 아예 매치하지 않아 원문이 그대로 남는다
        # (과소탐이지만 유출이 없는 안전한 쪽이다).
        for text in [
            "1,23만원",      # 2자리 그룹 — 앞자리 "1," 유출 위험
            "12,34만원",     # 2자리 그룹 — 앞자리 "12," 유출 위험
            "1,0000만원",    # 4자리 그룹 — 앞자리 "1," 유출 위험
            "1,23,456만원",  # 혼합 비정상 — 앞자리 "1," 유출 위험
        ]:
            masked, mapping = mask_pii(text)
            normed = _norm(masked)
            assert not re.search(r"\d,<AMOUNT", normed), (
                f"비정상 콤마 표기에서 앞자리가 유출된 채 뒷부분만 마스킹됐다: "
                f"{text!r} -> {normed!r} (앞자리 숫자와 콤마가 자리표시자 앞에 남음)"
            )

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

        이 테스트가 잡는 mutation: _QTY_RE의 `(?!일|째|차|간|년|기|당)` 또는
        `(?![ \\t]*(?:신고가|...))` 배제 lookahead 제거.
        """
        for text in [
            "52주 신고가 갱신",
            "52주 신저가 경신",
            "52주 최고가 대비",
            "52주 최저가 대비",
            "3주째 상승세",
            "2주차 실적",
            "3주간 조정",
            "3주년 기념",
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

    def test_masks_quantity_with_attached_korean_particle(self):
        """수량 뒤에 조사가 곧바로 붙은 형태도 마스킹돼야 한다.

        위 test_masks_quantity_in_free_form_user_message의 예시가 하필 "3주 보유중"처럼
        띄어쓴 형태라서, "주" 뒤의 한글을 전부 배제하는 `(?![가-힣])`으로도 green이었다.
        하지만 한국어는 수량 명사 뒤에 조사가 곧바로 붙는 쪽이 더 일반적이다
        ("3주를", "3주도", "3주만", "3주보유중"). 한글 전체를 배제하면 이 형태를 전부
        놓쳐 **실제 보유 수량이 그대로 외부 LLM으로 나간다.**

        그리고 그 경로가 하필 잔고 리포트 앵커링("· {qty}주"로만 매치)을 거절한 유일한
        근거였다 — backend/telegram_commands.py:1349-1356의 _handle_chat_fallback이
        텔레그램 사용자 원문을 가공 없이 llm_chat으로 넘기는 경로. 앵커링을
        포기하면서까지 지키려던 경로를 조사 하나로 다시 놓치면 안 되므로 여기서 고정한다.

        이 테스트가 잡는 mutation: _QTY_RE의 `(?!일|째|차|간|년|기|당)` ->
        `(?![가-힣])`(한글 전체 배제로 되돌리기).
        """
        for text, expected in [
            ("삼성전자 3주를 팔까요?", "삼성전자 <QTY_1>주를 팔까요?"),
            ("삼성전자 3주도 있어요", "삼성전자 <QTY_1>주도 있어요"),
            ("삼성전자 3주만 매도", "삼성전자 <QTY_1>주만 매도"),
            ("삼성전자 3주보유중인데", "삼성전자 <QTY_1>주보유중인데"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, f"조사가 붙은 보유 수량이 유출됐다: {text}"
            assert _norm_mapping(mapping) == {"<QTY_1>": "3"}
            assert unmask_pii(masked, mapping) == text

    def test_masks_quantity_before_average_and_streak_words(self):
        """배제 목록에 '평균'·'연속'이 있으면 안 된다 — 배제 목록 자체가 과소탐 장치다.

        _QTY_RE의 관용구 배제는 "52주 신고가"처럼 **기간 표현하고만 결합하는** 어휘로
        제한해야 한다. "평균"·"연속"은 실제 보유 수량 뒤에도 자연스럽게 오므로
        ("1,234주 평균 매입단가는", "5주 연속 매수했는데") 목록에 넣는 순간 그 문장의
        보유 수량이 통째로 마스킹되지 않고 나간다.

        반대 방향(과탐)은 대가가 없다 — "5주 연속 상승" 같은 뉴스 문구를 수량으로
        오분류해도 마스킹 후 역치환되어 사용자에게 보이는 값은 그대로다. 모듈 주석이
        선언한 우선순위(과탐 < 과소탐)를 배제 목록에도 그대로 적용한 것이다.

        이 테스트가 잡는 mutation: _QTY_RE 배제 목록에 `평균`·`연속`을 다시 추가.
        """
        for text, expected in [
            ("삼성전자 1,234주 평균 매입단가는", "삼성전자 <QTY_1>주 평균 매입단가는"),
            ("3주 평균 단가", "<QTY_1>주 평균 단가"),
            ("5주 연속 상승", "<QTY_1>주 연속 상승"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, f"보유 수량이 마스킹되지 않고 남았다: {text}"
            assert unmask_pii(masked, mapping) == text

    def test_qty_at_line_end_is_masked_when_next_line_starts_with_excluded_word(self):
        """관용구 배제는 줄을 넘어가면 안 된다 — 넘어가면 수량이 통째로 유출된다.

        _QTY_RE의 관용구 배제 lookahead를 `(?!\\s*(?:신고가|...))`로 쓰면 \\s*가
        개행을 넘는다. 잔고 리포트는 수량이 줄 끝에 오는 여러 줄 텍스트이므로
        (mcp-trading\\balance.js:278의 "- {종목} ({코드}) · {수량}주\\n  평단가 ..."),
        다음 줄이 배제 목록 단어로 시작하면 배제가 잘못 발동해 **보유 수량이 전혀
        마스킹되지 않은 채 외부 LLM으로 나간다.** 배제는 같은 줄의 후속 어절만
        보려는 것이므로 [ \\t]*로 한정해야 한다.

        이 케이스를 처음 고정할 때는 다음 줄을 "평균단가"로 썼다. 그런데 '평균'은
        보유 수량 뒤에도 자연스럽게 와서 배제 목록에서 빠졌으므로
        (test_masks_quantity_before_average_and_streak_words) 그 문장으로는 \\s* 뮤테이션이
        더 이상 발동하지 않는다 — 회귀 가드로서 죽는다. 그래서 남은 배제어(신고가)로
        바꿔 가드를 살려 둔다. 잔고 리포트 다음 줄이 배제어로 시작하는 형태 자체가
        문제이지, 특정 단어가 문제인 게 아니다.

        이 테스트가 잡는 mutation: _QTY_RE의 `(?![ \\t]*(?:...))` -> `(?!\\s*(?:...))`.
        """
        text = "- 삼성전자 (005930) · 1,234주\n  신고가 갱신"
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
    (1) 형식은 맞지만(=_PLACEHOLDER_RE에 매치) 매핑에 없는 자리표시자 -> 중립 문구로 치환 + 경고 로그
    (2) 형식 자체가 어긋난 변형(scope 없는 구형식 포함) -> 정규식에 안 걸려 자연히 원문 유지
    """

    def test_unknown_scoped_placeholder_returns_neutral_phrase_with_warning(self, caplog):
        """LLM이 존재하지 않는 자리표시자를 지어내면 중립 문구로 치환하고 경고를 남긴다.

        scope 도입 후 "매핑에 없는 자리표시자"의 대표 사례는 다른 호출(=이전 대화 턴)의
        scope를 가진 자리표시자다. 형식은 유효하므로 _PLACEHOLDER_RE에 매치되고,
        매핑에 없으니 중립 문구 치환 + 경고 로그 경로를 탄다. 내부 토큰이 사용자
        화면에 노출되어서는 안 된다.
        """
        mapping = {"<AMOUNT_deadbe_1>": "12,345,000원"}
        text = "평가금액은 <AMOUNT_deadbe_1>이고 총자산은 <AMOUNT_deadbe_9>이다."
        with caplog.at_level("WARNING", logger="backend.pii_mask"):
            restored = unmask_pii(text, mapping)
        assert restored == "평가금액은 12,345,000원이고 총자산은 이전에 언급된 금액이다."
        assert "<AMOUNT_deadbe_9>" not in restored, "내부 토큰이 사용자 화면에 노출됐다"
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


class _TaggedStr(str):
    """str을 상속해 메타데이터를 얹는 호출자를 흉내낸다(#260 services.NatAnswer).

    services를 import하지 않고 로컬에 정의한다 — 순환 의존과, 이 테스트가 요구하지도
    않는 httpx 의존을 끌어오지 않기 위함이다.
    """

    def __new__(cls, value, tag=None):
        obj = super().__new__(cls, value)
        obj.tag = tag
        return obj


class _StrictStr(str):
    """생성자가 추가 인자를 강제하는 str 서브클래스."""

    def __new__(cls, value, required):
        obj = super().__new__(cls, value)
        obj.required = required
        return obj


class _HostileStr(str):
    """__dict__ 접근 자체가 실패하는 str 서브클래스(fail-open 경로 확인용).

    unmask_pii는 어떤 서브클래스가 올지 알 수 없다. 복원이 불가능한 타입을 만나도
    예외를 밖으로 던지지 않는다는 계약을 이 클래스로 고정한다.
    """

    @property
    def __dict__(self):
        raise RuntimeError("이 타입은 복제할 수 없다")


class TestUnmaskPreservesStrSubclass:
    """입력이 str 서브클래스면 반환값도 같은 타입이어야 한다.

    re.sub은 str 서브클래스를 벗겨 항상 plain str을 반환한다. 그래서 호출자가 str을
    상속해 메타데이터를 얹은 값을 넘기면(#260이 도입하는 services.NatAnswer — 추론
    과정 각주를 속성으로 들고 다닌다) 역치환을 통과하는 순간 그 메타데이터가 조용히
    사라진다.

    특히 고약한 것은 **두 경로가 갈린다**는 점이다. unmask_pii는 mapping이 비면 곧바로
    원본을 돌려주므로 PII가 없는 질의에서는 타입이 살아남고, PII가 있는 질의에서만
    타입이 소실된다. 즉 "가끔만, 사용자 입력에 따라서만" 각주가 사라지는 재현하기
    어려운 형태가 된다. 그래서 양쪽 경로를 모두 단언한다.

    이 테스트들이 잡는 mutation: unmask_pii 반환부의 타입 보존 제거, 그리고 복원을
    `str.__new__` + `__dict__` 복사가 아니라 `type(text)(restored)`로 되돌리는 변경.
    """

    def test_subclass_and_its_attributes_survive_restoration(self):
        """타입뿐 아니라 인스턴스 속성까지 살아남아야 한다.

        타입만 유지하고 서브클래스 생성자를 다시 부르면(`type(text)(restored)`)
        NatAnswer처럼 속성이 기본값 있는 키워드 인자인 클래스는 **예외 없이 성공하면서
        속성만 기본값으로 리셋된다** — 각주 데이터가 비워진 채 타입만 맞는 값이 나가서
        결함이 형태만 바꿔 그대로 남는다. 그래서 값까지 단언한다.
        """
        mapping = {"<AMOUNT_deadbe_1>": "12,345,000원"}
        text = _TaggedStr("평가금액은 <AMOUNT_deadbe_1>이다.", tag="trading_agent")

        restored = unmask_pii(text, mapping)

        assert restored == "평가금액은 12,345,000원이다."
        assert type(restored) is _TaggedStr, (
            "역치환을 거치며 str 서브클래스가 plain str로 벗겨졌다 — 호출자가 얹은 "
            "메타데이터가 조용히 사라진다"
        )
        assert restored.tag == "trading_agent", (
            "타입은 유지됐지만 인스턴스 속성이 기본값으로 리셋됐다 — 각주 데이터가 "
            "조용히 비워진다"
        )

    def test_subclass_is_preserved_when_there_is_nothing_to_restore(self):
        """마스킹 대상이 없는 경로도 같은 타입을 유지해야 한다(두 경로가 갈리면 안 된다)."""
        text = _TaggedStr("자리표시자가 없는 응답", tag="trading_agent")

        assert type(unmask_pii(text, {})) is _TaggedStr
        assert type(unmask_pii(text, {"<AMOUNT_deadbe_1>": "1원"})) is _TaggedStr

    def test_subclass_with_required_constructor_arg_is_still_preserved(self):
        """생성자가 추가 인자를 요구해도 복원된다 — 생성자를 다시 부르지 않기 때문이다.

        `type(text)(restored)` 방식이었다면 TypeError로 실패해 plain str로 떨어진다.
        `str.__new__`는 서브클래스 생성자를 우회하므로 이런 클래스도 그대로 살린다.
        """
        mapping = {"<AMOUNT_deadbe_1>": "12,345,000원"}
        text = _StrictStr("평가금액은 <AMOUNT_deadbe_1>이다.", required="keep")

        restored = unmask_pii(text, mapping)

        assert restored == "평가금액은 12,345,000원이다."
        assert type(restored) is _StrictStr
        assert restored.required == "keep"

    def test_unrestorable_subclass_falls_back_to_str_without_raising(self):
        """복원이 불가능한 서브클래스여도 예외를 던지지 않는다(fail-open 계약).

        타입 보존은 부가 기능이고, 이 함수가 예외를 밖으로 던지지 않는다는 것이
        이 모듈의 핵심 계약이다. 값의 정확성을 지키고 타입만 포기한다.
        """
        mapping = {"<AMOUNT_deadbe_1>": "12,345,000원"}
        text = _HostileStr("평가금액은 <AMOUNT_deadbe_1>이다.")

        restored = unmask_pii(text, mapping)

        assert restored == "평가금액은 12,345,000원이다."
        assert type(restored) is str

        # 반대로 바꿀 것이 없으면 복원 자체를 시도하지 않고 원본을 그대로 돌려준다 —
        # 복원 불가능한 타입이 아무 이유 없이 벗겨지거나 경고가 남지 않아야 한다.
        # (이 단언이 잡는 mutation: 반환부의 `if restored == text: return text` 제거)
        untouched = _HostileStr("자리표시자가 없는 응답")
        assert type(unmask_pii(untouched, mapping)) is _HostileStr


class TestMaskEmptyInput:
    def test_empty_string_returns_empty_mapping(self):
        masked, mapping = mask_pii("")
        assert masked == ""
        assert mapping == {}

    def test_none_returns_empty_mapping(self):
        masked, mapping = mask_pii(None)
        assert masked is None
        assert mapping == {}
