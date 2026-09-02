"""backend/pii_mask.py 단위 테스트 (#230, F-17/NFR-05).

recognizer 3종(계좌번호/원화 금액/보유 수량) 각각과 복합 케이스, 왕복 무손실,
그리고 자리표시자 역치환의 fail-open 동작을 고정한다.
"""
import json
import pathlib
import re
import time

from backend.pii_mask import _AMOUNT_UNIT_RE, mask_pii, unmask_pii

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


# 부정 lookbehind의 문자 클래스 내용물을 뽑는다. 컴파일된 .pattern에서 읽으므로
# _HANGUL_DIGIT 같은 상수 보간은 이미 전개된 뒤이고, lookbehind를 여러 개로 쪼개
# 쓰더라도(예: (?<![\d,])(?<![천백십…])) 합집합으로 같은 결과가 나온다.
_NEGATIVE_LOOKBEHIND_CLASS_RE = re.compile(r"\(\?<!\[([^\]]*)\]\)")


def _negative_lookbehind_chars(pattern):
    """패턴에 있는 모든 부정 lookbehind 문자 클래스의 문자 합집합."""
    chars = set()
    for body in _NEGATIVE_LOOKBEHIND_CLASS_RE.findall(pattern):
        chars.update(body)
    return chars


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
            scoped = _SCOPED_PLACEHOLDER_RE.fullmatch(key)
            assert scoped is not None, f"자리표시자가 scope 형식이 아니다: {key}"
            scopes.add(scoped.group(0))
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
        assert "(이전 금액 1)" in restored


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

    def test_masks_korean_numeral_unit_amount(self):
        """숫자와 만/억 사이에 한글 수사(천/백/십)가 끼어도 금액 전체가 마스킹돼야 한다.

        "3천만원"은 한국어 구어와 증권 기사에서 "3,000만원"보다 흔한 표기다. 수사 체인을
        허용하지 않으면 세 금액 정규식 중 어느 것도 매치하지 못한다 — _AMOUNT_WON_RE는
        숫자 바로 뒤에 "원"을 요구해 "3천…"에서 멈추고, _AMOUNT_UNIT_RE는 숫자 바로 뒤에
        만/억을 요구해 "천"에서 멈춘다. 그 결과 값 **전체**가 평문으로 외부 LLM에 나간다.
        위 test_masks_comma_grouped_unit_amount_entirely가 고정한 "3,000만원" 앞자리
        유출과 같은 자리·같은 근거이고, 부분 유출이 아니라 전량 유출이라 방향이 더 나쁘다.

        직격하는 경로는 자유 입력 두 곳이다: telegram_commands._handle_chat_fallback이
        넘기는 사용자 원문("3천만원 있는데 어디 투자할까요?")과
        services.check_signal_significance가 프롬프트에 싣는 뉴스 원문 1000자
        ("영업이익 1천억원", "시가총액 3천억원").

        이 테스트가 잡는 mutation: _AMOUNT_UNIT_RE에서 (?:[천백십]\\d*)+ 갈래 제거.
        """
        for text, expected in [
            ("3천만원", "<AMOUNT_1>"),
            ("1천억원", "<AMOUNT_1>"),
            ("1백만원", "<AMOUNT_1>"),
            ("1천원", "<AMOUNT_1>"),
            ("예수금 1천5백만원", "예수금 <AMOUNT_1>"),
            ("영업이익 1천억원 기록", "영업이익 <AMOUNT_1> 기록"),
            ("시가총액 3천억원", "시가총액 <AMOUNT_1>"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, (
                f"한글 수사 금액이 유출됐다: {text!r} -> {masked!r}"
            )
            assert unmask_pii(masked, mapping) == text

    def test_masks_amount_with_korean_numerals_only(self):
        """선행 아라비아 숫자 없이 수사만으로 표기된 금액도 전체가 마스킹돼야 한다.

        "천만원"·"오천만원"은 telegram_commands._handle_chat_fallback이 넘기는 자유
        입력에서 "3천만원"만큼(오히려 그보다 더) 흔하다. _AMOUNT_UNIT_RE가 선두 \\d+로
        숫자를 강제하면 세 금액 정규식 중 어느 것에도 걸리지 않아 값 **전체**가 평문으로
        외부 LLM에 나간다 — 위 test_masks_korean_numeral_unit_amount가 고정한 "3천만원"과
        같은 자리·같은 전량 유출이다.

        이 테스트가 잡는 mutation: _AMOUNT_UNIT_RE에서 수사 체인 단독 갈래
        ([일이삼사오육칠팔구]?(?:[천백십]...)+) 제거.
        """
        for text, expected in [
            ("천만원", "<AMOUNT_1>"),
            ("백만원", "<AMOUNT_1>"),
            ("십만원", "<AMOUNT_1>"),
            ("천억원", "<AMOUNT_1>"),
            ("삼천만원", "<AMOUNT_1>"),
            ("오천만원", "<AMOUNT_1>"),
            ("이천만원", "<AMOUNT_1>"),
            ("오천원", "<AMOUNT_1>"),
            ("구십원", "<AMOUNT_1>"),
            ("천만원 정도 있는데 어디 투자할까요?", "<AMOUNT_1> 정도 있는데 어디 투자할까요?"),
            ("자산이 천만원입니다", "자산이 <AMOUNT_1>입니다"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, (
                f"한글 수사 금액이 유출됐다: {text!r} -> {masked!r}"
            )
            assert unmask_pii(masked, mapping) == text

    def test_korean_numeral_run_does_not_blow_up(self):
        r"""수사만 길게 이어진 입력에서 마스킹이 2차 함수적으로 느려지면 안 된다.

        수사 체인 단독 갈래는 선행 숫자를 요구하지 않으므로, lookbehind가 콤마만 막으면
        수사 런의 모든 위치에서 매치 시작이 허용된다. 각 시작이 런 전체를 소비했다가
        \s?원에서 실패하고 +를 되감으므로 O(n) 시작 x O(n) 되감기 = O(n²)가 된다.
        mask_pii는 async llm_chat 안의 동기 CPU 작업이라 그동안 이벤트 루프 전체가
        멈추고, _handle_chat_fallback은 텔레그램 원문(최대 4096자)을 가공 없이 넘기므로
        봇에 접근 가능한 누구나 발동시킬 수 있다.

        이 테스트가 잡는 mutation: _AMOUNT_UNIT_RE의 lookbehind에서 수사·한글 수사를
        빼는 모든 변경 — 전부 제거((?<![\d,]))든, 한글 수사만 빠뜨리는 부분 제거
        ((?<![\d,천백십]))든, 12자 중 한 글자만 빠뜨리는 것이든.

        단언을 둘 두는 이유는 시간 단언만으로는 **부분** 제거를 잡지 못하기 때문이다.
        이 머신에서 8192자 mask_pii 실측(시간 단언 단독 판정):

            원본                              1.55 ms   PASS (상한의 1/322)
            M1  (?<![\d,])                 2184.66 ms   RED  (상한의 4.4배)
            M2  (?<![\d,천백십])              541.18 ms   RED  (상한의 1.08배 — 아슬아슬)
            M3  (?<![\d,일이삼사오육칠팔구])    1654.80 ms   RED
            M4  12자 중 "일"만 제거              2.00 ms   **GREEN — 놓친다**
            M5  12자 중 "십"만 제거            551.07 ms   RED  (상한의 1.10배 — 아슬아슬)

        M4가 결정적이다. 한 글자만 빠뜨린 mutation은 이 입력("오천백십" 반복)에 "일"이
        없어 부하가 전혀 늘지 않으므로 시간 단언이 통과시킨다. M2·M5도 상한을 8~10 %만
        넘겨 이 머신보다 조금 빠른 러너에서는 통과한다 — 입력이 [천백십]만으로도 시작
        위치 대부분을 차단당해 부하가 크게 줄기 때문이다. 주석이 경고한 "단위를 추가할 때
        이 문자 집합에도 함께 넣어야 한다"는 실수가 정확히 이 구멍에 해당한다. 반대로
        패턴 단언만으로는 성능 특성 자체를 보증하지 못한다(문자 집합은 맞는데 다른
        수정으로 느려지는 회귀를 놓친다). 그래서 둘을 함께 둔다. 위 6종 전부에 대해
        두 단언을 합친 이 테스트가 red인 것을 실측으로 확인했다.

        패턴 단언은 문자열 하드코딩이 아니라 _AMOUNT_UNIT_RE.pattern에서 부정 lookbehind
        문자 클래스를 뽑아 **집합**으로 비교한다. 하드코딩하면 클래스 안 문자 순서를
        바꾸거나 lookbehind를 여러 개로 쪼개는 등 표현 방식만 달라져도 거짓 실패가 난다.
        집합 비교는 그런 재배열에는 반응하지 않고 12자 중 한 글자라도 실제로 빠질 때만
        red가 된다.

        시간 상한은 넉넉하게 잡는다. 정상 경로 1.55 ms 대 상한 0.5 초 = 322배 여유이고,
        수정 전(M1)은 상한의 4.4배라 느린 CI에서도 오탐 없이 회귀만 잡는다. 절대 시간을
        단언하는 테스트라 머신 편차에 취약한 것이 사실이지만, 이 결함은 결과가 아니라
        소요 시간에만 나타나므로 값 단언으로는 잡을 수 없다 — 편차에 취약한 구간
        (M2·M4·M5 같은 부분 제거)은 위 패턴 단언이 대신 막는다.

        패턴 단언을 먼저 두어, 명백한 mutation은 2 초짜리 측정을 기다리지 않고 즉시
        원인을 지목하며 실패한다.
        """
        required = set("천백십일이삼사오육칠팔구")
        actual = _negative_lookbehind_chars(_AMOUNT_UNIT_RE.pattern)
        assert required <= actual, (
            f"_AMOUNT_UNIT_RE의 부정 lookbehind에서 수사 {sorted(required - actual)}가 "
            "빠졌다 — 수사 런 중간에서 매치 시작이 허용되어 O(n²) 백트래킹이 되살아난다. "
            "단위를 추가할 때는 이 문자 집합에도 함께 넣어야 한다."
        )

        hostile = "오천백십" * 2048  # 8192자
        started = time.perf_counter()
        masked, mapping = mask_pii(hostile)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5, (
            f"수사 런 {len(hostile)}자 마스킹에 {elapsed * 1000:.0f} ms가 걸렸다 — "
            "_AMOUNT_UNIT_RE의 lookbehind가 수사 런 중간 시작을 막지 못해 "
            "O(n²)로 되돌아간 것으로 보인다."
        )
        # "원"이 없으므로 마스킹 대상이 아니다 — 느려지지만 않으면 되는 것이 아니라
        # 동작도 종전과 같아야 한다.
        assert masked == hostile
        assert mapping == {}

    def test_masks_composite_big_unit_amount(self):
        """조·억·만이 공백 없이 이어지는 복합 표기는 값 전체가 하나의 자리표시자여야 한다.

        종전 패턴은 한 매치가 단위 하나만 처리해 "9조1800억원"을 "9조<AMOUNT_1>"(매핑
        "1800억원")로 **부분** 마스킹했고, "조"를 단위로 알지 못해 "10조원"은 전량
        평문이었다(#330). 부분 마스킹은 전량 마스킹보다도 전량 미변경보다도 나쁘다 —
        앞자리 "9조"가 평문으로 남고 바로 뒤에 크기를 알 수 없는 불투명 토큰이 붙어,
        services.check_signal_significance가 뉴스 원문을 실어 보내는 채점 모델이 값을
        "9조"로 읽을 여지가 생긴다(실제 값은 9조1800억원이라 자릿수 차이가 크다).

        이 테스트가 잡는 mutation:
        - 단위 갈래에서 "조" 제거((?:조|억|만) -> (?:억|만)): "9조1800억원"이 다시
          "9조<AMOUNT_1>"이 되고 "10조원"은 전량 평문이 된다.
        - 마디 연쇄 {1,3} 제거(마디 하나만 허용): "조" 갈래는 살아 있어 "10조원"은
          통과하지만 "9조1800억원"이 "9조<AMOUNT_1>"로 되돌아간다.
        - lookbehind 문자 집합에 "조" 추가: 마디 연쇄가 상한 3이라 성능상 필요 없는
          변경인데, "수조5000억원"처럼 "조" 앞에서 매치를 시작할 수 없는 표기는 뒤
          구간마저 시작이 막혀 값 전체가 평문이 된다(마지막 대조군 루프가 FAILED).

        대조군은 마지막 루프에 둔다 — 연쇄를 넣으면서 기존 단일 단위 표기의 매치가
        달라지지 않았는지, 금액이 아닌 표기를 새로 삼키지 않는지 함께 고정한다.
        """
        # 매핑 원값까지 함께 단언한다. 부분 마스킹이어도 자리표시자는 1개라
        # ("9조<AMOUNT_1>") 마스킹된 문자열만 세면 통과하기 때문이다.
        for text, expected, original in [
            ("9조1800억원", "<AMOUNT_1>", "9조1800억원"),
            ("79조987억원", "<AMOUNT_1>", "79조987억원"),
            ("1조5000억원", "<AMOUNT_1>", "1조5000억원"),
            ("1조5천억원", "<AMOUNT_1>", "1조5천억원"),
            ("10조원", "<AMOUNT_1>", "10조원"),
            ("500조원", "<AMOUNT_1>", "500조원"),
            ("1,000조원", "<AMOUNT_1>", "1,000조원"),
            ("0.5조원", "<AMOUNT_1>", "0.5조원"),
            # 마디 3개(조·억·만) = 연쇄 상한
            ("12조3456억7890만원", "<AMOUNT_1>", "12조3456억7890만원"),
            # "조"가 없어도 연쇄가 필요한 같은 결함 클래스
            ("3억2천만원", "<AMOUNT_1>", "3억2천만원"),
            # 수사 단독 갈래에도 "조"를 함께 넣었다(만/억과 같은 자리)
            ("천조원", "<AMOUNT_1>", "천조원"),
            ("삼천조원", "<AMOUNT_1>", "삼천조원"),
            ("영업이익 9조1800억원 기록", "영업이익 <AMOUNT_1> 기록", "9조1800억원"),
            ("시총이 500조원을 넘었다", "시총이 <AMOUNT_1>을 넘었다", "500조원"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, (
                f"조 단위 복합 표기가 부분 마스킹됐다: {text!r} -> {masked!r}"
            )
            assert _norm_mapping(mapping) == {"<AMOUNT_1>": original}, (
                f"매핑 원값이 값 전체가 아니다: {text!r} -> {mapping!r}"
            )
            assert unmask_pii(masked, mapping) == text

        # 대조군: 연쇄 도입 전후로 동작이 같아야 하는 표기들.
        for text, expected in [
            ("3천만원", "<AMOUNT_1>"),
            ("1,234,567원", "<AMOUNT_1>"),
            ("123만원", "<AMOUNT_1>"),
            ("1천5백만원", "<AMOUNT_1>"),
            # "조"/"억" 뒤에 공백을 두고 이어지는 표기는 여전히 뒤 구간만 잡힌다
            # (docs/nfr-05-pii-masking.md 미적용 경로 5번의 알려진 간극).
            ("3억 2천만원", "3억 <AMOUNT_1>"),
            ("9조 1800억원", "9조 <AMOUNT_1>"),
            # "조" 앞이 숫자가 아니면 첫 갈래가 선두 \d+를 요구해 "조" 앞에서 시작하지
            # 못한다. 그래도 뒤 구간은 마스킹돼야 한다 — lookbehind에 "조"를 넣으면
            # 이것마저 막혀 값 전체가 평문이 되므로 넣지 않았다(_AMOUNT_UNIT_RE 주석).
            ("수조5000억원", "수조<AMOUNT_1>"),
            # 금액이 아닌 표기를 새로 삼키지 않는다. 뒤 셋은 "조" 추가로 새로 생길 수
            # 있는 과탐 자리다 — 수사 없이 "조원"으로 시작하거나 "조원"이 다른 단어의
            # 일부인 표기.
            ("제1 원칙", "제1 원칙"),
            ("지금만 원하는 것은", "지금만 원하는 것은"),
            ("조원 단위 적자", "조원 단위 적자"),
            ("조원장님께 보고", "조원장님께 보고"),
            ("천조국이라 불린다", "천조국이라 불린다"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, (
                f"대조군 동작이 바뀌었다: {text!r} -> {masked!r}"
            )
            assert unmask_pii(masked, mapping) == text

    def test_chained_unit_run_does_not_blow_up(self):
        r"""마디를 이어 붙인 뒤에도 수사·숫자가 번갈아 나오는 런에서 폭발하면 안 된다.

        위 test_korean_numeral_run_does_not_blow_up이 막는 O(n²)와 원인이 다르다. 그쪽은
        **시작 위치**가 O(n)개라 생기는 문제이고 lookbehind로 막는다. 이쪽은 마디 연쇄
        {1,3}을 넣으면서 생긴 것으로, "1천1천1천…" 같은 런을 "한 마디의 긴 수사 런"으로도
        "여러 마디"로도 쪼갤 수 있어 **한 번의 시작 위치**에서 분할 경우의 수가 런 길이에
        대해 지수로 늘어난다. 입력 길이에 대한 O(n²)가 아니라 짧은 입력에서도 터지므로
        위 테스트의 8192자 입력으로는 잡히지 않는다.

        마디 안의 수사 런을 소유 수량자로 확정해((?:[천백십]\d*)++) 분할 자체를 없앤다.
        매치 결과는 바뀌지 않는다 — 런 뒤에 오는 것은 (?:조|억|만)이나 다음 마디의 \d+
        뿐이고 둘 다 [천백십]이나 그에 딸린 숫자를 되돌려 받을 이유가 없다.

        이 테스트가 잡는 mutation: 그 소유 수량자를 되돌리는 변경(++ -> +). 이 머신
        실측(mask_pii, '1천' x512 = 1024자):

            현재 (?:[천백십]\d*)++      0.37 ms   PASS (상한의 1/1350)
            M   (?:[천백십]\d*)+     5839.49 ms   RED  (상한의 11.7배)

        위 test_korean_numeral_run_does_not_blow_up과 달리 패턴 단언을 함께 두지 않는다.
        그쪽은 12자 문자 집합에서 한 글자만 빠진 mutation을 시간 단언이 놓쳐서 패턴
        단언이 필요했지만, 여기서 되돌릴 수 있는 것은 이 + 하나뿐이고 그 하나가 상한의
        11.7배로 red가 된다. (안쪽 \d*까지 소유로 바꾸는 변형((?:[천백십]\d*+)++)은
        mutation이 아니라 같은 효과의 다른 표기라 green이 맞다 — 실측 0.34 ms.)
        """
        hostile = "1천" * 512  # 1024자
        started = time.perf_counter()
        masked, mapping = mask_pii(hostile)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5, (
            f"수사·숫자 교대 런 {len(hostile)}자 마스킹에 {elapsed * 1000:.0f} ms가 "
            "걸렸다 — 마디 연쇄 {1,3} 안의 수사 런이 소유 수량자로 확정되지 않아 "
            "분할 경우의 수가 폭발한 것으로 보인다."
        )
        # "원"이 없으므로 마스킹 대상이 아니다 — 동작도 종전과 같아야 한다.
        assert masked == hostile
        assert mapping == {}

    def test_does_not_mask_won_syllable_without_numeral_chain(self):
        """수사 체인 갈래를 넣더라도 "원"으로 시작하는 일반 어휘를 삼키면 안 된다.

        선두 \\d+를 통째로 (?:\\d+)?로 옵션화하는 단순화를 택하면 두 번째 갈래의
        (?:만|억)이 숫자 없이도 발동해 "지금만 원하는 것은"이 "지금<AMOUNT_1>하는 것은"이
        된다 — 마스킹 대상이 아닌 문장을 깨뜨리면서 자리표시자를 만든다. 그래서 수사
        ([천백십])가 실재할 때만 숫자를 생략하도록 갈래를 나눴다.

        이 테스트가 잡는 mutation: _AMOUNT_UNIT_RE의 선두 \\d+를 (?:\\d+)?로 옵션화.
        """
        for text in [
            "지금만 원하는 것은",
            "그것만 원칙대로",
            "천천히 원상복구",
            "10년 뒤 원금",
        ]:
            masked, mapping = mask_pii(text)
            assert masked == text, f"금액이 아닌 표기가 마스킹됐다: {text!r} -> {masked!r}"
            assert mapping == {}

    def test_does_not_mask_ordinal_before_won_syllable(self):
        """수사도 만/억도 없는 형태를 \\s?원으로 삼키면 안 된다.

        _AMOUNT_UNIT_RE의 두 갈래(수사 체인 | 만·억)를 (?:만|억)? 하나로 느슨하게
        합치면 "제1 원칙"이 "제<AMOUNT_1>칙"이 된다 — 마스킹 대상이 아닌 문자열을
        깨뜨리면서 자리표시자를 만들어 LLM이 읽는 문장 자체가 망가진다.

        이 테스트가 잡는 mutation: 위 두 갈래를 (?:만|억)? 하나로 합치는 단순화.
        """
        for text in ["제1 원칙에 따라", "제1 원칙", "제2 원칙과 제3 원칙"]:
            masked, mapping = mask_pii(text)
            assert masked == text, f"금액이 아닌 표기가 마스킹됐다: {text!r} -> {masked!r}"
            assert mapping == {}

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

    def test_labeled_quantity_is_not_misclassified_as_amount(self):
        """금액 라벨 뒤라도 "주"가 붙은 숫자는 AMOUNT가 아니라 QTY여야 한다.

        _LABELED_AMOUNT_RE가 _QTY_RE보다 먼저 적용되므로, 배제가 없으면 콤마가 들어가거나
        4자리 이상인 수량이 금액으로 먹혀 "잔고 <AMOUNT_1>주"가 된다. 왕복은 무손실이지만
        이 설계가 유일하게 보존한다고 선언한 능력(자리표시자끼리의 상대 비교)이 깨진다 —
        주식 수가 금액 이름공간에 끼어들어 AMOUNT_2 > AMOUNT_1 비교가 서로 다른 종류의
        값을 비교하게 된다.

        아래 세 갈래를 함께 고정한다. 배제는 _QTY_RE가 받아 줄 때만 발동해야지, 배제만
        되고 아무도 안 받으면 그 숫자는 평문으로 나간다(=유출).

        이 테스트가 잡는 mutation:
        - amount 그룹의 `(?![\\d,]*\\d(?:\\.\\d+)?<qty>)` 배제 제거
          -> "잔고 1,234주"가 <AMOUNT_1>로 오분류된다.
        - 배제를 amount 그룹 **끝**의 `(?!\\s*주)`로 바꾸기 (리뷰 제안 형태)
          -> \\s*가 개행을 넘어 "예수금 1,234,567\\n주식 평가액"에서 잘못 발동하고,
             (?:,\\d{3})+가 되감기해 "예수금 <AMOUNT_1>,567"이 되어 ",567"이 유출된다.
        - 배제 조건을 _QTY_UNIT_SUFFIX 재사용이 아닌 별도 문자열로 복제
          -> 두 곳이 갈라지는 순간 "잔고 1234주일"처럼 _QTY_RE가 거부하는 형태에서
             배제만 발동해 숫자가 어느 자리표시자도 받지 못한다.
        """
        # (1) 라벨 + 수량 -> QTY
        for text, expected in [
            ("잔고 1,234주", "잔고 <QTY_1>주"),
            ("잔고 12345주", "잔고 <QTY_1>주"),
            ("보유 잔고 500주", "보유 잔고 <QTY_1>주"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, f"수량이 금액으로 오분류됐다: {text} -> {masked}"
            assert unmask_pii(masked, mapping) == text

        # (2) 배제가 줄을 넘어가면 안 된다 — 다음 줄이 "주"로 시작해도 금액은 금액이다.
        text = "예수금 1,234,567\n주식 평가액"
        masked, mapping = mask_pii(text)
        assert _norm(masked) == "예수금 <AMOUNT_1>\n주식 평가액", (
            f"라벨 금액이 부분 마스킹되거나 유출됐다: {masked}"
        )
        assert _norm_mapping(mapping) == {"<AMOUNT_1>": "1,234,567"}
        assert unmask_pii(masked, mapping) == text

        # (3) _QTY_RE가 거부하는 "주" 접미(기간 표현)는 배제도 발동하지 않아야 한다.
        #     발동하면 숫자가 AMOUNT도 QTY도 아니게 되어 평문으로 나간다.
        for text, expected in [
            ("잔고 1234주일", "잔고 <AMOUNT_1>주일"),
            ("손익 12345주간", "손익 <AMOUNT_1>주간"),
            ("잔고 1234주 신고가", "잔고 <AMOUNT_1>주 신고가"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, f"숫자가 어느 자리표시자도 받지 못했다: {text} -> {masked}"
            assert unmask_pii(masked, mapping) == text

    def test_labeled_number_with_trailing_comma_is_not_leaked(self):
        """후행 콤마로 끝나는 숫자 런도 어느 한쪽 자리표시자는 반드시 받아야 한다.

        위 test_labeled_quantity_is_not_misclassified_as_amount가 고정한 "배제는
        _QTY_RE가 받아 줄 때만 발동한다"는 불변식의 구멍이었다. 배제 lookahead가
        `[\\d,]*`로 끝나면 콤마로 끝나는 런까지 받는데 _QTY_RE의 `\\d(?:[\\d,]*\\d)?`는
        숫자로 끝나야 해서, "잔고 1,234,주"에서 라벨 정규식은 배제로 소비를 포기하고
        _QTY_RE는 `(?=주)`에서 실패한다 — "1,234"가 어느 자리표시자도 받지 못한 채
        평문으로 외부 LLM에 나간다.

        후행 콤마는 정상 표기가 아니라 재현성이 낮지만, 배제 조건을 _QTY_RE와 정확히
        일치시켜(끝을 \\d로 강제) 불변식 자체를 참으로 만든다.

        이 테스트가 잡는 mutation: 배제를 `(?![\\d,]*\\d(?:\\.\\d+)?<qty>)`에서
        `(?![\\d,]*(?:\\.\\d+)?<qty>)`로 되돌리기.
        """
        for text, expected in [
            ("잔고 1,234,주", "잔고 <AMOUNT_1>,주"),
            ("잔고 12,345,주식", "잔고 <AMOUNT_1>,주식"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, (
                f"숫자가 어느 자리표시자도 받지 못했다: {text!r} -> {masked!r}"
            )
            assert unmask_pii(masked, mapping) == text

        # 대조군: 후행 콤마가 없는 정상 표기는 종전대로 QTY가 가져간다.
        masked, mapping = mask_pii("잔고 1,234주")
        assert _norm(masked) == "잔고 <QTY_1>주"
        assert _norm_mapping(mapping) == {"<QTY_1>": "1,234"}

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

        mcp-trading/balance.js의 formatQuantity()가 toLocaleString("ko-KR")을
        쓰므로 1,000주 이상은 "1,234주"처럼 콤마가 들어간다. 콤마를 처리하지 못하면
        "1,"이 마스킹되지 않고 남아 **보유 수량의 앞자리가 그대로 외부 LLM으로 나간다**
        — F-17이 막으려던 바로 그 유출이다. 같은 사실을 이미 반영한 선례가
        backend/scheduler.py의 _QTY_RE(r"\\)\\s*·\\s*([\\d,]+)주")다.

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

    def test_masks_fractional_quantity_entirely(self):
        """소수 표기 수량("0.5주")은 정수부까지 통째로 마스킹돼야 한다.

        _QTY_RE의 (?<![\\d,])는 소수점을 배제하지 않으므로, 소수 갈래가 없으면 매치가
        소수점 **뒤**에서 시작해 "0.<QTY_1>주"(매핑 "5")가 되고 "0."이 평문으로 남는다.
        _AMOUNT_WON_RE·_AMOUNT_UNIT_RE는 이미 같은 (?:\\.\\d+)?를 갖고 있어 수량만
        빠져 있던 자리다.

        이 테스트가 잡는 mutation:
        - _QTY_RE에서 (?:\\.\\d+)? 제거 -> "0.5주"가 "0.<QTY_1>주"로 부분 마스킹된다.
        - 대신 lookbehind를 (?<![\\d,.])로 막는 방향 -> "0.5주"가 통째로 미매치가 되어
          부분 유출이 전량 유출로 나빠진다("0.5"가 그대로 남는다).
        """
        for text, expected, value in [
            ("0.5주", "<QTY_1>주", "0.5"),
            ("12.5주 보유", "<QTY_1>주 보유", "12.5"),
            ("1,234.5주", "<QTY_1>주", "1,234.5"),
        ]:
            masked, mapping = mask_pii(text)
            assert _norm(masked) == expected, f"소수 수량이 부분/미마스킹됐다: {text} -> {masked}"
            assert _norm_mapping(mapping) == {"<QTY_1>": value}
            assert unmask_pii(masked, mapping) == text

        # 대조군: 소수점이 아니라 번호 매기기 뒤의 수량은 종전대로 숫자만 잡는다.
        masked, mapping = mask_pii("3. 5주")
        assert _norm(masked) == "3. <QTY_1>주"
        assert _norm_mapping(mapping) == {"<QTY_1>": "5"}

    def test_does_not_mask_period_idioms_with_share_unit(self):
        """'주'가 기간 단위로 쓰인 표현은 보유 수량이 아니므로 마스킹하지 않는다.

        '52주 신고가'는 주식 뉴스에서 가장 흔한 관용구인데,
        backend/services.py의 check_signal_significance()가 외부 뉴스 원문
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

        backend/telegram_commands.py의 _handle_chat_fallback은 사용자 원문을
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
        근거였다 — backend/telegram_commands.py의 _handle_chat_fallback이
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
        (mcp-trading\\balance.js의 formatBalanceReport()가 만드는
        "- {종목} ({코드}) · {수량}주\\n  평단가 ..."),
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
        assert restored == "평가금액은 12,345,000원이고 총자산은 (이전 금액 1)이다."
        assert "<AMOUNT_deadbe_9>" not in restored, "내부 토큰이 사용자 화면에 노출됐다"
        assert "<AMOUNT_deadbe_9>" in caplog.text, (
            "매핑에 없는 자리표시자를 만났는데 경고 로그가 남지 않았다"
        )

    def test_distinct_unknown_placeholders_restore_to_distinct_phrases(self):
        """서로 다른 자리표시자는 서로 다른 중립 문구로 복원돼야 한다.

        종류별 서수가 없으면 한 응답에 인용된 이전 턴 자리표시자가 전부 같은 문자열로
        접힌다. "지난달 <AMOUNT_..._1>에서 이번 달 <AMOUNT_..._2>로 늘었고"가
        "지난달 이전에 언급된 금액에서 이번 달 이전에 언급된 금액로 늘었고"가 되어
        **서로 다른 두 값이 같아 보인다** — 토큰 노출을 막으려던 치환이 없던 오정보를
        만든다. 값을 지어내지 않는다는 성질은 서수를 붙여도 그대로다.

        같은 자리표시자가 여러 번 나오면 같은 서수를 유지해야 하고(같은 값을 가리키므로),
        종류가 다르면 각자 1부터 세어야 한다(한 카운터를 공유하면 첫 수량 인용이
        "(이전 수량 3)"이 되어 있지도 않은 앞선 수량 둘을 암시한다).

        이 테스트가 잡는 mutation: _restore에서 서수 부여를 제거해
        _FALLBACK_LABEL 값을 그대로 반환하도록 되돌리는 변경.
        """
        text = (
            "지난달 <AMOUNT_deadbe_1>에서 이번 달 <AMOUNT_deadbe_2>로 늘었고, "
            "보유는 <QTY_deadbe_1>주입니다. 재차 <AMOUNT_deadbe_1> 기준입니다."
        )
        restored = unmask_pii(text, {})

        assert "(이전 금액 1)" in restored
        assert "(이전 금액 2)" in restored, (
            "서로 다른 두 금액 자리표시자가 같은 문구로 접혔다 — "
            f"다른 값이 같아 보인다: {restored!r}"
        )
        # 종류가 다르면 각자 1부터 센다.
        assert "(이전 수량 1)" in restored, f"수량 서수가 금액과 카운터를 공유했다: {restored!r}"
        # 같은 자리표시자의 재등장은 같은 서수를 유지한다.
        assert restored.count("(이전 금액 1)") == 2, (
            f"같은 자리표시자가 서로 다른 서수로 복원됐다: {restored!r}"
        )
        assert "<AMOUNT_deadbe_1>" not in restored
        assert "<QTY_deadbe_1>" not in restored

    def test_known_placeholder_restores_to_value_even_when_others_are_unknown(self):
        """매핑에 있는 자리표시자는 서수 도입과 무관하게 원값 그대로 복원된다."""
        mapping = {"<AMOUNT_deadbe_1>": "12,345,000원"}
        restored = unmask_pii(
            "평가금액 <AMOUNT_deadbe_1>, 총자산 <AMOUNT_deadbe_9>", mapping
        )
        assert restored == "평가금액 12,345,000원, 총자산 (이전 금액 1)"

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

    def test_empty_mapping_with_placeholder_returns_neutral_phrase_with_warning(self, caplog):
        """mapping이 비어도 이전 턴 자리표시자가 중립 문구로 치환되고 경고가 남아야 한다.

        후속 질의("그럼 팔까?", "왜?")는 PII가 없어 mapping = {}가 된다. NAT 대화
        히스토리에서 이전 턴의 자리표시자가 응답에 인용되면, 조기 반환하지 않고
        _PLACEHOLDER_RE.sub를 통과시켜 중립 문구로 치환해야 한다. 조기 반환하면
        내부 토큰이 경고 없이 사용자 화면에 노출된다.

        이 테스트가 잡는 mutation: unmask_pii의 조기 반환에 `not mapping`을 다시 추가.
        """
        with caplog.at_level("WARNING", logger="backend.pii_mask"):
            result = unmask_pii("앞서 말씀하신 <AMOUNT_deadbe_1> 기준으로는", {})
        assert "(이전 금액 1)" in result, "중립 문구로 치환되지 않았다"
        assert "<AMOUNT_deadbe_1>" not in result, "내부 토큰이 사용자 화면에 노출됐다"
        assert "<AMOUNT_deadbe_1>" in caplog.text, (
            "매핑에 없는 자리표시자를 만났는데 경고 로그가 남지 않았다"
        )

    def test_empty_text_returns_as_is(self):
        assert unmask_pii("", {"<AMOUNT_deadbe_1>": "1원"}) == ""
        assert unmask_pii(None, {"<AMOUNT_deadbe_1>": "1원"}) is None


class _TaggedStr(str):
    """str을 상속해 메타데이터를 얹는 호출자를 흉내낸다(#260 services.NatAnswer).

    services를 import하지 않고 로컬에 정의한다 — 순환 의존과, 이 테스트가 요구하지도
    않는 httpx 의존을 끌어오지 않기 위함이다.
    """

    # 클래스 수준 선언이 필요하다. __new__ 안의 `obj.tag = ...`는 self가 아니라
    # 지역 변수에 대한 대입이라 타입 체커가 멤버로 인정하지 않는다.
    tag: str | None

    def __new__(cls, value: str, tag: str | None = None) -> "_TaggedStr":
        obj = super().__new__(cls, value)
        obj.tag = tag
        return obj


class _StrictStr(str):
    """생성자가 추가 인자를 강제하는 str 서브클래스."""

    required: str

    def __new__(cls, value: str, required: str) -> "_StrictStr":
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
