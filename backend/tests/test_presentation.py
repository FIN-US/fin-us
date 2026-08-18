"""출력 계층 테스트 (#297).

고정하려는 것은 셋이다.
  1. 평문 채널로 나가는 문장에 마크다운 표기가 남지 않는다 (parse_mode를 쓰지 않으므로
     남으면 그대로 화면에 뜬다).
  2. 용어 각주는 사전에 있는 말만, 첫 등장만, 최대 2개만 설명한다.
  3. #260의 추론 각주는 모양도 길이 예산 규칙도 그대로다.
"""

import json

import pytest

from backend import presentation
from backend.presentation import (
    DEFAULT_TELEGRAM_USER_LEVEL,
    KIND_ALERT,
    KIND_ALERT_URGENT,
    KIND_ANALYSIS,
    KIND_DIARY,
    KIND_QUOTE,
    LEVEL_BEGINNER,
    LEVEL_INTERMEDIATE,
    REASONING_FOOTNOTE_SEPARATOR,
    TELEGRAM_MESSAGE_LIMIT,
    TERM_FOOTNOTE_PREFIX,
    URGENT_ALERT_URGENCIES,
    TermEntry,
    alert_kind,
    decision_label,
    find_terms,
    kind_for_agent,
    normalize_level,
    reasoning_footnote,
    render,
    sanitize_markdown,
    term_footnote,
    urgency_label,
)


class FakeTool:
    def __init__(self, name, *, ok=True, empty=False):
        self.name = name
        self.ok = ok
        self.empty = empty


@pytest.fixture
def fake_terms(monkeypatch):
    """사전을 테스트가 통제하는 소수의 항목으로 갈아끼운다.

    실제 terms.json으로 매칭 규칙을 검사하면, 사람이 사전을 손볼 때마다 규칙 테스트가
    깨진다 — 검사 대상은 사전 내용이 아니라 매칭 규칙이다.
    """

    def _install(entries):
        monkeypatch.setattr(presentation, "_terms_cache", tuple(entries))
        monkeypatch.setattr(presentation, "_surface_index_cache", None)

    return _install


# ---- 마크다운 정리 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("**중요** 공지", "중요 공지"),
        ("__강조__ 문구", "강조 문구"),
        ("### 오늘의 시장", "오늘의 시장"),
        ("~~취소선~~ 유지", "취소선 유지"),
        ("`코드` 조각", "코드 조각"),
        ("*기울임* 문장", "기울임 문장"),
        ("_기울임_ 문장", "기울임 문장"),
        ("> 인용문", "인용문"),
        ("- 첫째\n- 둘째", "• 첫째\n• 둘째"),
        ("가격은 \\*별표\\* 입니다", "가격은 *별표* 입니다"),
    ],
)
def test_markdown_markup_is_converted_to_plain_text(raw, expected):
    assert sanitize_markdown(raw) == expected


def test_link_keeps_both_label_and_url():
    """주소를 버리면 사용자가 근거를 열어 볼 수단이 사라진다."""
    assert sanitize_markdown("[공시 원문](https://dart.fss.or.kr/x)") == (
        "공시 원문 (https://dart.fss.or.kr/x)"
    )


def test_unpaired_emphasis_residue_is_removed():
    """길이 제한에 잘린 LLM 출력은 여는 표기만 남기는 일이 흔하다."""
    assert sanitize_markdown("**중요한 소식인데 여기서 잘렸") == "중요한 소식인데 여기서 잘렸"


def test_underscored_identifiers_survive():
    """내부 도구 이름이 본문에 실릴 수 있다. 밑줄쌍을 기울임으로 읽으면 이름이 훼손된다."""
    assert sanitize_markdown("finus_save_diary 를 호출했습니다") == (
        "finus_save_diary 를 호출했습니다"
    )


def test_code_fence_lines_go_away_but_content_stays():
    assert sanitize_markdown("```json\n{\"a\": 1}\n```") == '{"a": 1}'


def test_sanitizer_is_tolerant_of_non_string_and_empty_input():
    assert sanitize_markdown("") == ""
    assert sanitize_markdown(None) == ""


# ---- 용어 매칭 ----


DEPOSIT = TermEntry(term="예수금", description="아직 쓰지 않은 현금", aliases=("주문가능금액",))
FILL = TermEntry(term="체결", description="주문이 거래로 이뤄진 것")
FLOW = TermEntry(term="수급", description="누가 사고 누가 팔았는지")
CAP = TermEntry(term="시가총액", description="회사 전체의 값", aliases=("시총",))
OPEN_PRICE = TermEntry(term="시가", description="장 시작 가격")


def test_first_occurrence_only_and_at_most_two(fake_terms):
    fake_terms([DEPOSIT, FILL, FLOW])
    text = "예수금 확인 뒤 체결까지 봤고 예수금이 남았으며 수급도 좋습니다"

    found = [entry.term for entry in find_terms(text)]

    assert found == ["예수금", "체결"]


def test_aliases_match_but_the_footnote_names_the_canonical_term(fake_terms):
    fake_terms([DEPOSIT])

    footnote = term_footnote("주문가능금액이 부족합니다. 확인이 필요합니다.", level=LEVEL_BEGINNER)

    assert footnote == f"{TERM_FOOTNOTE_PREFIX} 예수금 — 아직 쓰지 않은 현금"


def test_longer_surface_wins_at_the_same_position(fake_terms):
    """시가총액을 '시가'로 설명하면 설명이 아니라 오답이다."""
    fake_terms([CAP, OPEN_PRICE])

    found = [entry.term for entry in find_terms("시가총액이 늘었습니다. 확인해 주세요.")]

    assert found == ["시가총액"]


def test_intermediate_level_gets_no_term_footnote(fake_terms):
    fake_terms([DEPOSIT, FILL])
    text = "예수금과 체결 내역을 확인했습니다. 자세한 내용은 아래와 같습니다."

    assert term_footnote(text, level=LEVEL_BEGINNER) != ""
    assert term_footnote(text, level=LEVEL_INTERMEDIATE) == ""


def test_a_short_body_still_gets_its_footnote(fake_terms):
    """초보가 짧은 기본 질문을 던졌을 때가 각주가 가장 필요한 순간이다 (#297 검수 3).

    처음에는 "각주가 본문보다 길면 생략"으로 막았는데, 그 규칙이 정확히 이 경우를 막았다.
    """
    fake_terms([FLOW])

    assert term_footnote("수급 응답", level=LEVEL_BEGINNER) == (
        f"{TERM_FOOTNOTE_PREFIX} 수급 — 누가 사고 누가 팔았는지"
    )


def test_terms_the_user_typed_are_not_explained(fake_terms):
    """직접 타이핑한 단어는 아는 단어라는 신호다. 설명하면 참견이 된다."""
    fake_terms([DEPOSIT, FILL])

    footnote = term_footnote(
        "예수금은 120만원이고 어제 주문은 전부 체결됐습니다.",
        level=LEVEL_BEGINNER,
        question="예수금 얼마야?",
    )

    assert footnote == f"{TERM_FOOTNOTE_PREFIX} 체결 — 주문이 거래로 이뤄진 것"


def test_a_known_term_does_not_waste_a_footnote_slot(fake_terms):
    """제외를 상한 뒤에 적용하면 설명이 필요한 말이 두 자리 밖으로 밀린다.

    질문에 "예수금"이 있으면 그 자리는 다음 용어에게 넘어가야 한다 — 그러지 않으면
    슬롯 하나가 조용히 버려져, 각주가 하나만 붙는다.
    """
    fake_terms([DEPOSIT, FILL, FLOW])
    text = "예수금은 320만원이고 어제 주문은 전부 체결됐으며 수급도 나쁘지 않습니다."

    found = [entry.term for entry in find_terms(text, exclude=frozenset({"예수금"}))]

    assert found == ["체결", "수급"]


def test_a_term_typed_as_an_alias_also_counts_as_known(fake_terms):
    """사용자는 '주문가능금액'이라 쳤는데 '예수금'을 설명하면 같은 말을 두 번 하는 것이다."""
    fake_terms([DEPOSIT])

    assert term_footnote(
        "예수금은 120만원입니다.",
        level=LEVEL_BEGINNER,
        question="주문가능금액 알려줘",
    ) == ""


def test_without_a_question_nothing_is_filtered_out(fake_terms):
    """스케줄러 알림은 사용자가 부른 메시지가 아니라 '이미 안다'고 볼 근거가 없다."""
    fake_terms([DEPOSIT])

    assert term_footnote("예수금이 부족합니다.", level=LEVEL_BEGINNER) != ""


def test_unknown_words_get_no_footnote(fake_terms):
    """사전에 없는 말은 설명하지 않는다 — LLM에게 즉석 생성을 시키지 않는 것이 이 층의 전부다."""
    fake_terms([DEPOSIT])

    assert term_footnote("듣도 보도 못한 파생상품 얘기를 길게 늘어놓은 문장입니다.") == ""


def test_terms_json_ships_a_usable_draft():
    """초안 파일이 실제로 읽히는지, 검수 안내가 남아 있는지 확인한다."""
    entries = presentation.load_terms(presentation.TERMS_PATH)

    assert 30 <= len(entries) <= 60
    assert all(entry.term and entry.description for entry in entries)
    assert len({entry.term for entry in entries}) == len(entries)

    raw = json.loads(presentation.TERMS_PATH.read_text(encoding="utf-8"))
    assert "사람 검수" in "".join(raw["_readme"])


def test_broken_dictionary_does_not_block_messages(tmp_path, caplog):
    """사전이 깨졌다고 시세나 알림이 막히면 부가 기능이 본 기능을 잡아먹는 것이다."""
    broken = tmp_path / "terms.json"
    broken.write_text("{ not json", encoding="utf-8")

    assert presentation.load_terms(broken) == ()


# ---- 수준 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("초보", LEVEL_BEGINNER),
        ("beginner", LEVEL_BEGINNER),
        (" 중급 ", LEVEL_INTERMEDIATE),
        ("INTERMEDIATE", LEVEL_INTERMEDIATE),
        ("고수", None),
        (None, None),
        (3, None),
    ],
)
def test_level_normalization(raw, expected):
    assert normalize_level(raw) == expected


def test_default_level_is_beginner():
    assert DEFAULT_TELEGRAM_USER_LEVEL == LEVEL_BEGINNER


# ---- 틀 ----


def test_four_message_kinds_render_through_their_own_template():
    body = "본문입니다"

    assert render(body, KIND_ALERT, LEVEL_INTERMEDIATE) == "🔔 알림\n본문입니다"
    assert render(body, KIND_DIARY, LEVEL_INTERMEDIATE) == "📓 매매일지\n본문입니다"
    # 시세·분석답변은 사용자가 방금 요청한 것의 답이라 배너를 얹지 않는다 (TEMPLATES 주석).
    assert render(body, KIND_QUOTE, LEVEL_INTERMEDIATE) == "본문입니다"
    assert render(body, KIND_ANALYSIS, LEVEL_INTERMEDIATE) == "본문입니다"


def test_urgent_alerts_get_their_own_banner():
    """긴급 공시 알림과 일반 알림이 같은 얼굴이면 긴급의 의미가 죽는다 (#297 검수 1)."""
    assert render("본문", alert_kind("critical"), LEVEL_INTERMEDIATE) == "🚨 긴급 알림\n본문"
    assert render("본문", alert_kind("high"), LEVEL_INTERMEDIATE) == "🚨 긴급 알림\n본문"
    assert render("본문", alert_kind("normal"), LEVEL_INTERMEDIATE) == "🔔 알림\n본문"


def test_unknown_urgency_does_not_escalate_to_the_urgent_banner():
    """오탈자 하나로 모든 알림이 🚨가 되면 긴급 표시가 다시 무의미해진다."""
    assert alert_kind("crtical") == KIND_ALERT
    assert alert_kind(None) == KIND_ALERT
    assert alert_kind("") == KIND_ALERT


def test_alert_banner_and_send_gate_share_one_urgency_set():
    """전송 게이트와 배너가 다른 기준을 보면 '긴급이라 보냈는데 배너는 평범한 알림'이 된다."""
    from backend.telegram_notifier import URGENT_TELEGRAM_LEVELS

    assert URGENT_TELEGRAM_LEVELS == URGENT_ALERT_URGENCIES


# ---- 구조화된 필드의 한국어화 ----


@pytest.mark.parametrize(
    ("decision", "expected"),
    [("BUY", "매수"), ("SELL", "매도"), ("HOLD", "보유 유지"), ("hold", "보유 유지")],
)
def test_decision_is_translated_deterministically(decision, expected):
    assert decision_label(decision) == expected


def test_unknown_decision_passes_through_instead_of_defaulting():
    """모르는 판단값을 기본값으로 접으면 지어낸 판단을 사용자가 실제 판단으로 읽는다 (#162)."""
    assert decision_label("판단 없음 (도구 미사용 provider)") == (
        "판단 없음 (도구 미사용 provider)"
    )


@pytest.mark.parametrize(
    ("urgency", "expected"),
    [("critical", "매우 높음"), ("high", "높음"), ("normal", "보통"), ("확인불가", "확인불가")],
)
def test_urgency_is_translated_deterministically(urgency, expected):
    assert urgency_label(urgency) == expected


# ---- 본문에 새어 나온 내부 도구명 ----


def test_internal_tool_names_in_the_body_are_replaced(fake_terms):
    """각주에서는 한국어로 보여주면서 본문에서는 내부 이름을 흘리면 안 된다 (#297 검수 5)."""
    fake_terms([])

    message = render(
        "`get_investor_trading` 결과를 참고했습니다.", KIND_ANALYSIS, LEVEL_INTERMEDIATE
    )

    assert message == "수급 조회 결과를 참고했습니다."


def test_a_longer_tool_name_is_not_half_replaced(fake_terms):
    """finus_mcp_trading_get_balance가 'finus_mcp_trading_계좌 잔고 조회'가 되면 안 된다."""
    fake_terms([])

    assert render(
        "finus_mcp_trading_get_balance 를 호출했습니다.", KIND_ANALYSIS, LEVEL_INTERMEDIATE
    ) == "계좌 잔고 조회 를 호출했습니다."


def test_unknown_tool_names_are_left_alone(fake_terms):
    """모르는 도구를 감추면 사용자가 보는 근거가 실제보다 줄어든다 — 각주와 같은 규칙이다."""
    fake_terms([])

    assert render("get_brand_new_thing 호출", KIND_ANALYSIS, LEVEL_INTERMEDIATE) == (
        "get_brand_new_thing 호출"
    )


def test_unknown_kind_falls_back_instead_of_raising():
    """출력 계층이 던지는 예외는 곧 '메시지가 아예 안 나감'이다."""
    assert render("본문", "존재하지 않는 종류", LEVEL_INTERMEDIATE) == "본문"


def test_diary_kind_comes_from_routing_not_from_the_text():
    assert kind_for_agent("diary_agent") == KIND_DIARY
    assert kind_for_agent("news_agent") == KIND_ANALYSIS
    assert kind_for_agent(None) == KIND_ANALYSIS


# ---- 조립 ----


def test_reasoning_footnote_is_unchanged_after_the_move():
    footnote = reasoning_footnote(
        "news_agent",
        (FakeTool("finus_market_news"), FakeTool("finus_account_balance", ok=False)),
    )

    assert footnote == (
        f"{REASONING_FOOTNOTE_SEPARATOR}\n"
        "🤖 뉴스 에이전트 · 📚 확인한 자료: 뉴스 검색, KIS 시세·계좌 조회(실패)"
    )


def test_render_without_footnotes_is_just_the_trimmed_body(fake_terms):
    """#260 이전 경로의 동작이 그대로 유지된다는 것을 고정한다."""
    fake_terms([])

    assert render("  답변입니다  ", KIND_ANALYSIS, LEVEL_BEGINNER) == "답변입니다"


def test_term_and_reasoning_footnotes_coexist_in_order(fake_terms):
    fake_terms([DEPOSIT])
    reasoning = reasoning_footnote("trading_agent", (FakeTool("finus_account_balance"),))

    message = render(
        "예수금은 120만원 남아 있습니다. 추가 매수 여력이 있는 상태입니다.",
        KIND_ANALYSIS,
        LEVEL_BEGINNER,
        reasoning=reasoning,
    )

    body, term_block, reasoning_block = message.split("\n\n")
    assert body.startswith("예수금은 120만원")
    assert term_block == f"{TERM_FOOTNOTE_PREFIX} 예수금 — 아직 쓰지 않은 현금"
    assert reasoning_block == reasoning


def test_intermediate_level_keeps_the_reasoning_footnote(fake_terms):
    """수준은 용어 각주만 끈다. 근거는 수준과 무관하게 남아야 한다."""
    fake_terms([DEPOSIT])
    reasoning = reasoning_footnote("trading_agent", (FakeTool("finus_account_balance"),))

    message = render(
        "예수금은 120만원 남아 있습니다. 추가 매수 여력이 있는 상태입니다.",
        KIND_ANALYSIS,
        LEVEL_INTERMEDIATE,
        reasoning=reasoning,
    )

    assert TERM_FOOTNOTE_PREFIX not in message
    assert message.endswith(reasoning)


def test_long_body_is_truncated_but_both_footnotes_survive(fake_terms):
    fake_terms([DEPOSIT])
    reasoning = reasoning_footnote("trading_agent", (FakeTool("finus_account_balance"),))

    message = render(
        "예수금 " + "가" * 8000,
        KIND_ANALYSIS,
        LEVEL_BEGINNER,
        reasoning=reasoning,
    )

    assert len(message) <= TELEGRAM_MESSAGE_LIMIT
    assert message.endswith(reasoning)
    assert TERM_FOOTNOTE_PREFIX in message


def test_markdown_is_cleaned_before_terms_are_matched(fake_terms):
    """표기를 벗기기 전에 스캔하면 '**예수금**'이 사전과 어긋나 설명을 놓친다."""
    fake_terms([DEPOSIT])

    message = render(
        "**예수금**은 충분합니다. 추가 매수 여력이 남아 있는 상태입니다.",
        KIND_ANALYSIS,
        LEVEL_BEGINNER,
    )

    assert message.startswith("예수금은 충분합니다")
    assert TERM_FOOTNOTE_PREFIX in message
