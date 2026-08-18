"""출력 계층 테스트 (#297).

고정하려는 것은 둘이다.
  1. 평문 채널로 나가는 문장에 마크다운 표기가 남지 않는다 (parse_mode를 쓰지 않으므로
     남으면 그대로 화면에 뜬다).
  2. #260의 추론 각주는 모양도 길이 예산 규칙도 그대로다.
"""

import pytest

from backend.presentation import (
    KIND_ALERT,
    KIND_ANALYSIS,
    KIND_DIARY,
    KIND_QUOTE,
    REASONING_FOOTNOTE_SEPARATOR,
    TELEGRAM_MESSAGE_LIMIT,
    kind_for_agent,
    reasoning_footnote,
    render,
    sanitize_markdown,
)


class FakeTool:
    def __init__(self, name, *, ok=True, empty=False):
        self.name = name
        self.ok = ok
        self.empty = empty


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


# ---- 틀 ----


def test_four_message_kinds_render_through_their_own_template():
    body = "본문입니다"

    assert render(body, KIND_ALERT) == "🔔 알림\n본문입니다"
    assert render(body, KIND_DIARY) == "📓 매매일지\n본문입니다"
    # 시세·분석답변은 사용자가 방금 요청한 것의 답이라 배너를 얹지 않는다 (TEMPLATES 주석).
    assert render(body, KIND_QUOTE) == "본문입니다"
    assert render(body, KIND_ANALYSIS) == "본문입니다"


def test_unknown_kind_falls_back_instead_of_raising():
    """출력 계층이 던지는 예외는 곧 '메시지가 아예 안 나감'이다."""
    assert render("본문", "존재하지 않는 종류") == "본문"


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


def test_render_without_a_footnote_is_just_the_trimmed_body():
    """#260 이전 경로의 동작이 그대로 유지된다는 것을 고정한다."""
    assert render("  답변입니다  ", KIND_ANALYSIS) == "답변입니다"


def test_long_body_is_truncated_but_the_footnote_survives():
    reasoning = reasoning_footnote("trading_agent", (FakeTool("finus_account_balance"),))

    message = render("가" * 8000, KIND_ANALYSIS, reasoning=reasoning)

    assert len(message) <= TELEGRAM_MESSAGE_LIMIT
    assert message.endswith(reasoning)


def test_markdown_is_cleaned_before_the_footnote_is_attached():
    reasoning = reasoning_footnote("news_agent", (FakeTool("finus_market_news"),))

    message = render("**중요** 소식입니다.", KIND_ANALYSIS, reasoning=reasoning)

    assert message == f"중요 소식입니다.\n\n{reasoning}"
