"""텔레그램으로 나가는 모든 문장이 마지막으로 통과하는 출력 계층 (#297).

이 레포의 원칙은 일관되게 "형식은 코드로 강제하고 LLM은 내용만 만든다"였다 (#129의
종목코드 검증, #260의 추론 각주). 이 모듈은 그 원칙을 문장 자체에 적용한다.

책임이 다섯이다.

1. **마크다운 잔재 정리.** 이 봇은 ``sendMessage``에 ``parse_mode``를 넣지 않는다 —
   레포 전체에서 그 키를 쓰는 곳이 없다. 의도된 정책이다: MarkdownV2를 켜면 ``.``·``-``·
   ``(`` 이스케이프를 하나만 빠뜨려도 텔레그램이 400으로 전송 자체를 거부하고, 그 실패는
   사용자에게 "알림이 오지 않았다"로만 보인다. 평문 정책을 유지하는 대신, LLM이 습관적으로
   뱉는 굵게·밑줄·머리글 표기가 화면에 날것으로 뜨는 문제를 여기서 없앤다.

2. **메시지 종류별 틀.** 틀은 이 모듈의 상수고 LLM 출력은 본문 슬롯에만 들어간다.

3. **용어 각주.** 사전(``terms.json``)에 있는 말만 설명한다. LLM에게 즉석 설명을 시키지
   않는다 — 틀린 시세는 다음 조회에서 정정되지만 틀린 정의는 사용자의 머릿속에 남는다.
   환각을 가장 늦게 발견하게 되는 층위가 교육이다.

4. **정해진 값의 한국어화.** decision(BUY/SELL/HOLD)·urgency(critical/…)처럼 값이 정해진
   필드와, 본문에 새어 나온 내부 도구명(get_investor_trading)을 표로 옮긴다. 정해진 값의
   번역을 LLM에게 시키면 매번 다른 말이 나오고 그중 일부는 뜻이 어긋난다 — 표로 하면
   한 번 정한 말이 계속 같은 말로 나간다. 표에 없는 값은 감추지 않고 원문 그대로 통과시킨다.

5. **길이 예산.** 각주 자리를 먼저 확보하고 본문을 자른다 (#260에서 온 규칙).

**폭을 재려 하지 않는다 — 기준은 "접혔을 때 읽히는가"다.**

텔레그램 말풍선 한 줄에 몇 글자가 들어가는지는 우리가 알 수 없다. 화면 크기, 텔레그램의
글자 크기 설정, OS의 큰 글씨 설정이 곱해진 결과이고 사람마다 다르다. 이 이슈에서 폭을
가정한 설계를 두 번 시도했고 두 번 다 틀렸다 — 각주에 56칸 상한을 두었다가 설명이 전보처럼
변했고, 넘치는 줄을 공백으로 들여썼다가 글꼴 설정에 따라 어긋나는 문제를 만들었다.

그래서 정책은 이렇다.

- 긴 줄은 텔레그램이 접게 둔다. 우리가 접는 지점을 고르지 않는다.
- 대신 **접힌 뒤에도 구조가 남게** 만든다. 나열 항목은 전부 LIST_MARKER로 시작하므로,
  표시 없는 줄은 앞 줄의 계속이라는 뜻이 된다. 접힌 조각이 새 항목으로 오독되지 않는다.
- 정렬(들여쓰기·열 맞춤)에 기대는 배치는 쓰지 않는다. 평문 비례폭 글꼴에서는 애초에
  맞지 않고, 맞더라도 읽는 쪽 설정 하나에 무너진다. 가로 표가 특히 그렇다 — 한 행이
  접히는 순간 뒷조각이 아무 열에나 떨어져 표 전체가 무의미해진다. 표가 필요한 데이터는
  세로 나열로 펴고, 그래서 길어지면 요약과 버튼으로 나눈다
  (telegram_commands의 수급 요약/상세가 그 예다).
- 진짜 열 정렬이 필요해지면 parse_mode=HTML의 ``<pre>``가 유일한 수단이다(단조폭 글꼴,
  접히는 대신 가로 스크롤). 지금 켜지 않는 이유는 위험이 아니라 상호작용이다 — HTML을
  켜면 이 모듈의 마크다운 정리와 이스케이프 책임이 얽힌다. 도구함에 남겨 둔다.

임포트 방향: 이 모듈은 backend의 어떤 모듈도 임포트하지 않는다. 반대로 telegram_notifier·
telegram_commands·redis_state가 이쪽을 임포트한다. 텔레그램 전송 계층이 출력 계층을 쓰는데
출력 계층이 전송 계층의 상수를 되받아 오면 순환이 되므로, 길이 상한과 말줄임 규칙의 정의를
이쪽으로 옮기고 telegram_notifier는 재수출만 한다.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 텔레그램 sendMessage 본문 상한(4096자)보다 여유를 둔 실사용 상한.
# telegram_notifier가 같은 이름으로 재수출한다 (이 모듈이 임포트 그래프의 잎이다).
TELEGRAM_MESSAGE_LIMIT = 4000
TELEGRAM_TRUNCATION_SUFFIX = "...(이하 생략)"


# ---- 사용자 수준 ----

LEVEL_BEGINNER = "beginner"
LEVEL_INTERMEDIATE = "intermediate"
TELEGRAM_USER_LEVELS = (LEVEL_BEGINNER, LEVEL_INTERMEDIATE)
# 기본값이 초보인 이유: 모르는 사람에게 설명이 한 줄 더 붙는 비용보다, 아는 사람에게
# 설명이 없어서 뜻을 잘못 짚는 비용이 크다. 아는 사용자는 /level 중급 한 번으로 끈다.
DEFAULT_TELEGRAM_USER_LEVEL = LEVEL_BEGINNER

LEVEL_LABELS: dict[str, str] = {
    LEVEL_BEGINNER: "초보",
    LEVEL_INTERMEDIATE: "중급",
}
# 사용자가 실제로 칠 법한 말 → 정규값. 한국어 명령(/level 초보)과 콜백 데이터(level:beginner)가
# 같은 정규화를 통과해야 두 경로가 갈라지지 않는다.
_LEVEL_ALIASES: dict[str, str] = {
    "beginner": LEVEL_BEGINNER,
    "basic": LEVEL_BEGINNER,
    "초보": LEVEL_BEGINNER,
    "초급": LEVEL_BEGINNER,
    "입문": LEVEL_BEGINNER,
    "intermediate": LEVEL_INTERMEDIATE,
    "중급": LEVEL_INTERMEDIATE,
    "중수": LEVEL_INTERMEDIATE,
    "숙련": LEVEL_INTERMEDIATE,
}


def normalize_level(value: Any) -> str | None:
    """사용자 입력·저장값을 정규 수준값으로. 해석할 수 없으면 ``None``.

    ``None``과 기본값을 구분한다 — 호출부가 "못 알아들었다"(사용법 안내)와 "설정이
    없다"(기본값 적용)를 다르게 처리해야 하기 때문이다.
    """
    if not isinstance(value, str):
        return None
    return _LEVEL_ALIASES.get(value.strip().casefold())


def level_label(level: str) -> str:
    return LEVEL_LABELS.get(level, level)


# ---- 메시지 종류별 틀 ----

KIND_ALERT = "alert"
KIND_ALERT_URGENT = "alert_urgent"
KIND_QUOTE = "quote"
KIND_ANALYSIS = "analysis"
KIND_DIARY = "diary"
KIND_BRIEFING = "briefing"



@dataclass(frozen=True)
class MessageTemplate:
    """한 종류의 메시지 틀. 슬롯은 본문 하나뿐이다.

    ``header``가 빈 문자열인 종류가 있는 것은 누락이 아니라 결정이다 — TEMPLATES 주석 참조.
    """

    header: str = ""

    def apply(self, body: str) -> str:
        body = body.strip()
        if not self.header:
            return body
        if not body:
            return self.header
        return f"{self.header}\n{body}"


# 배너(머리글)를 다는 기준은 "사용자가 이 메시지를 예상했는가"다.
#
# 알림·브리핑·일지는 봇이 먼저 말을 거는 메시지다. 사용자는 다른 일을 하다 받으므로 첫
# 줄에서 "무엇이 왔는가"를 바로 읽어야 한다.
#
# 시세(/quote·/trend)와 분석답변(자연어 질문)은 사용자가 방금 요청한 것의 답이다. 무엇에 대한
# 답인지 이미 알고 있으므로 배너는 줄만 차지한다. #297 검수에서 이 판단을 유지하기로 했다.
# 붙이기로 바뀌면 아래 두 줄의 header를 채우면 되고 그 외에 고칠 곳은 없다.
#
# 브리핑은 세 번째 경우다. 봇이 먼저 거는 메시지지만 본문 첫 줄이 이미 "📰 오늘의 시장 요약"
# 이라 배너를 얹으면 머리글이 두 줄 연속으로 붙는다.
#
# 알림 배너가 둘인 이유: 긴급 공시 알림과 일반 알림이 같은 얼굴이면 긴급의 의미가 죽는다.
# 사용자는 알림을 흘려보며 읽으므로 구분은 첫 줄에 있어야 한다. 이 표는 telegram_commands의
# ALERT_MODE_EMOJIS(알림 모드 표시)와 같은 역할을 메시지 머리글에서 한다 — 그 옆에 두지 않은
# 것은 임포트 방향 때문이다. 알림을 보내는 telegram_notifier가 telegram_commands를 임포트할
# 수 없으므로, 두 계층이 함께 쓰는 상수는 잎인 이쪽에 있어야 한다.
TEMPLATES: dict[str, MessageTemplate] = {
    KIND_ALERT: MessageTemplate(header="🔔 알림"),
    KIND_ALERT_URGENT: MessageTemplate(header="🚨 긴급 알림"),
    KIND_DIARY: MessageTemplate(header="📓 매매일지"),
    KIND_BRIEFING: MessageTemplate(header=""),
    KIND_QUOTE: MessageTemplate(header=""),
    KIND_ANALYSIS: MessageTemplate(header=""),
}
# 매핑에 없는 kind는 배너 없는 틀로 떨어진다. 예외를 던지지 않는 이유는, 출력 계층이
# 던지는 예외가 곧 "메시지가 아예 안 나감"이기 때문이다 — 틀 하나 고르는 실수의 대가로는 과하다.
_FALLBACK_TEMPLATE = MessageTemplate()


def template_for(kind: str) -> MessageTemplate:
    return TEMPLATES.get(kind, _FALLBACK_TEMPLATE)


# 나열 항목 앞에 붙는 표시. 제목 줄에는 붙이지 않는다.
#
# 값이 늘어선 메시지(잔고·시세·알림)에서 한 줄이 말풍선 폭을 넘으면 뒷부분이 다음 줄 왼쪽
# 끝으로 내려간다. 표시가 없으면 그 조각이 새 항목처럼 보인다 — "상한가: 92,600원 / 하한가:"
# 다음 줄에 "49,900원"만 남으면 그게 값인지 항목인지 알 수 없다. 모든 항목이 표시로 시작하면
# 표시 없는 줄은 앞 줄의 계속이라는 뜻이 되므로, 접혀도 경계가 유지된다.
#
# 텔레그램은 평문이라 정렬(들여쓰기)로는 이걸 못 한다. 읽는 쪽 글꼴·글자 크기 설정에 따라
# 공백 폭이 달라져 오히려 어긋나 보인다 (#297 검수 3차).
LIST_MARKER = "-"


def as_list_items(lines: list[str]) -> list[str]:
    """나열 줄에 LIST_MARKER를 붙인다. 이미 붙어 있으면 그대로 둔다.

    제목 줄은 호출부가 목록에서 빼고 넘긴다 — 어느 줄이 제목인지는 데이터의 모양을 아는
    쪽만 알 수 있고, 여기서 "첫 줄은 제목"으로 못박으면 제목 없는 목록이 머리를 잃는다.
    """
    return [
        line if line.startswith(f"{LIST_MARKER} ") else f"{LIST_MARKER} {line}"
        for line in lines
    ]


def alert_kind(urgent: bool) -> str:
    """긴급 알림 틀 또는 일반 알림 틀 (#297).

    urgency 값이 아니라 **판정 결과**를 받는다. 처음에는 여기서 urgency 문자열을 직접
    보고 긴급 여부를 정했는데, 그러면 판정이 두 곳에 생긴다 — 전송 게이트
    (telegram_notifier.should_send_telegram_alert)와 여기. 두 판정은 실제로 어긋났다:
    게이트는 telegram_alert 플래그도 함께 보고 urgency를 정확히 비교하는데 이쪽은
    소문자로 접어 urgency만 봤다. alert_mode="all"에서 {"urgency": "High",
    "telegram_alert": False}면 배너는 🚨인데 본문은 비긴급 사유를 달고 나갔다
    (#297 자가리뷰).

    판정을 호출부에서 한 번만 하고 그 결과를 넘기면 어긋날 자리가 없어진다. 판정 규칙을
    바꿔도 배너가 따라오지 않는 일이 생기지 않는다.
    """
    return KIND_ALERT_URGENT if urgent else KIND_ALERT


# ---- 구조화된 필드의 한국어화 ----

# AgentReport의 decision/urgency는 LLM 자유 텍스트가 아니라 정해진 값이다. 값이 정해져
# 있으니 번역도 결정론적으로 할 수 있고, 그렇다면 사용자에게 "HOLD"를 보여줄 이유가 없다.
# LLM에게 한국어로 달라고 시키는 대신 여기서 매핑하는 것이 이 계층의 존재 이유다 —
# 시키면 매번 다른 말("보류", "관망", "홀드")이 나오고 그중 일부는 뜻이 어긋난다.
DECISION_LABELS: dict[str, str] = {
    "BUY": "매수",
    "SELL": "매도",
    "HOLD": "보유 유지",
}
URGENCY_LABELS: dict[str, str] = {
    "critical": "매우 높음",
    "high": "높음",
    "normal": "보통",
    "low": "낮음",
}
# 신호 출처. 키는 scheduler.SIGNAL_SOURCES의 name과 models.AgentReport.source 기본값이다.
# 사용자에게는 "이 알림이 어디서 왔는가"이지 내부 식별자가 아니다 (#297 검수 2차).
SOURCE_LABELS: dict[str, str] = {
    "news": "뉴스",
    "disclosure": "공시",
    "manual": "직접 요청",
}


def decision_label(decision: Any) -> str:
    """BUY/SELL/HOLD → 한국어. 표에 없으면 원문 그대로 (#297).

    조용히 감추지 않는다. 모르는 판단값을 "보유 유지" 같은 기본값으로 접으면, 이 봇이
    지어낸 판단을 사용자가 실제 판단으로 읽는다 — #162 리뷰에서 details=None을 HOLD로
    채우지 않기로 한 것과 같은 이유다.
    """
    text = str(decision or "").strip()
    return DECISION_LABELS.get(text.upper(), text)


def urgency_label(urgency: Any) -> str:
    """critical/high/normal/low → 한국어. 표에 없으면 원문 그대로."""
    text = str(urgency or "").strip()
    return URGENCY_LABELS.get(text.lower(), text)


def source_label(source: Any) -> str:
    """news/disclosure → 한국어. 표에 없으면 원문 그대로 (#297 검수 2차).

    감추지 않는 규칙은 decision·urgency와 같다. 새 신호원이 생겼는데 표에 없으면 내부
    이름이 그대로 보이고, 그게 "이 알림이 어디서 왔는지 아예 안 보인다"보다 낫다.
    """
    text = str(source or "").strip()
    return SOURCE_LABELS.get(text.lower(), text)


# ---- 본문에 새어 나온 내부 도구명 ----

# NAT·MCP 서버가 쓰는 내부 도구명. LLM이 "get_investor_trading 결과를 참고했습니다"처럼
# 본문에 그대로 적는 일이 잦다. #260이 각주용으로 만든 TOOL_LABELS와 같은 매핑을 본문에도
# 적용한다 — 각주에서는 한국어로 보여주면서 본문에서는 내부 이름을 흘리면 같은 도구가 한
# 메시지 안에서 두 이름으로 등장한다.
MCP_TOOL_LABELS: dict[str, str] = {
    "get_stock_quote": "현재가 조회",
    "get_investor_trading": "수급 조회",
    "get_balance": "계좌 잔고 조회",
    "get_balance_rlz_pl": "실현손익 조회",
    "get_today_daily_orders": "당일 주문·체결 조회",
    "get_disclosure_signal": "지분공시 조회",
    "get_earnings_report": "DART 실적 조회",
    "get_market_news": "뉴스 검색",
    "resolve_stock_code": "종목코드 조회",
    "place_order": "주문 제출",
}


# ---- 마크다운 잔재 정리 ----

# 순서가 의미를 갖는다. 굵게 표기를 기울임보다 먼저 지워야 두 별표가 한 별표를 거쳐
# 이상하게 남지 않는다.
_FENCE_LINE_RE = re.compile(r"^[ \t]*(?:```|~~~)[^\n]*$", re.M)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M)
_HRULE_RE = re.compile(
    r"^[ \t]{0,3}(?:\*[ \t]*){3,}$|^[ \t]{0,3}(?:-[ \t]*){3,}$|^[ \t]{0,3}(?:_[ \t]*){3,}$",
    re.M,
)
_BLOCKQUOTE_RE = re.compile(r"^[ \t]{0,3}>[ \t]?", re.M)
# 마크다운 글머리표(-, *, +)를 하나로 맞춘다. 목적지는 LIST_MARKER("-")다 — 이 봇의 나열
# 표시가 그것이고, 소스마다 다른 기호를 쓰면 한 화면에 세 종류가 섞인다.
_BULLET_RE = re.compile(r"^([ \t]*)[*+\-][ \t]+", re.M)
# 마크다운 링크. 괄호 안이 **실제 링크 목적지일 때만** 걸린다.
#
# 처음에는 뒤쪽을 [^)]*로 흘려보냈는데, 그러면 링크가 아닌 괄호까지 먹고 그 내용을 지웠다:
# "[대량보유](5.1% → 6.3% 증가)"가 "대량보유 (5.1%)"가 되어 수치가 사라졌다. 이 모듈의
# 계약은 "옮기되 지우지 않는다"이므로 정반대다 (#297 자가리뷰).
#
# 마크다운에서 목적지에 공백을 넣으려면 <>로 감싸야 한다. 그래서 목적지는 <...>이거나
# 공백·괄호 없는 한 덩어리이고, 그 뒤에는 제목("...", '...', (...))만 올 수 있다.
# 이 모양이 아니면 링크가 아니므로 원문을 그대로 둔다 — 대괄호가 남더라도 내용은 산다.
_LINK_RE = re.compile(
    r"!?\[([^\]\n]*)\]\("
    r"\s*(?:<([^>\n]*)>|([^()\s]*))"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)"
)
_BOLD_STAR_RE = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*", re.S)
_BOLD_UNDERSCORE_RE = re.compile(r"(?<![\w\\])__(?!\s)(.+?)(?<!\s)__(?!\w)", re.S)
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?![\s*])([^*\n]+?)(?<!\s)\*(?!\*)")
# 밑줄 기울임만 유독 조건이 많다. finus_save_diary 같은 내부 도구 이름이 본문에 실릴 수
# 있고, 양옆 경계를 보지 않으면 가운데 밑줄쌍을 기울임으로 읽어 이름을 훼손한다.
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<![\w\\_])_(?![\s_])([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~", re.S)
_INLINE_CODE_RE = re.compile(r"`+([^`\n]+?)`+")
_LEFTOVER_EMPHASIS_RE = re.compile(r"(?<!\\)(?:\*\*+|__+)")
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|])")
# 이스케이프 해제는 정리가 끝난 뒤에 한다. 자리표시자에 쓰는 문자는 LLM 출력에 나올 수 없는
# 제어문자여야 한다 — 본문에 우연히 등장하면 그 자리가 엉뚱하게 기호로 복원된다.
_ESCAPE_SENTINEL = "\x00"
_ESCAPE_RESTORE_RE = re.compile(r"\x00(\d+);")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+$", re.M)
_EXTRA_BLANKS_RE = re.compile(r"\n{3,}")


def _replace_link(match: re.Match[str]) -> str:
    label = match.group(1).strip()
    # 2번은 <...>로 감싼 목적지, 3번은 맨 목적지. 둘 중 하나만 잡힌다.
    url = (match.group(2) or match.group(3) or "").strip()
    if not url:
        return label
    if not label or label == url:
        return url
    return f"{label} ({url})"


def sanitize_markdown(text: str) -> str:
    """평문 채널에 그대로 실을 수 있게 마크다운 표기를 벗긴다 (#297).

    지우는 것이 아니라 **옮기는** 것이 원칙이다. 굵게 표기는 알맹이만 남고, 링크는
    "라벨 (주소)"가 된다 — 강조 표기를 없앤다고 강조된 내용까지 없애면 정리가 아니라
    손실이다. 링크 주소를 살리는 것도 같은 이유다. 사용자가 근거를 열어 볼 수단이
    사라지면 #260이 각주로 세운 "근거를 보여준다"는 계약이 본문에서 무너진다.

    쌍이 맞지 않아 변환되지 못한 잔재는 마지막에 제거한다. LLM이 길이 제한에 걸려 문장을
    자르면 여는 표기만 남는 경우가 흔한데, 그게 화면에서는 그냥 오타로 보인다.
    """
    if not isinstance(text, str) or not text:
        return ""

    # 이스케이프된 기호(\*)를 먼저 자리표시자로 치워 둔다. 그대로 두면 강조 정규식이 그
    # 별표를 여는 표기로 읽어, 강조가 아니라고 표시해 둔 자리를 강조로 처리한다.
    result = _MD_ESCAPE_RE.sub(lambda m: f"{_ESCAPE_SENTINEL}{ord(m.group(1))};", text)
    result = _FENCE_LINE_RE.sub("", result)
    result = _HRULE_RE.sub("", result)
    result = _HEADING_RE.sub("", result)
    result = _BLOCKQUOTE_RE.sub("", result)
    result = _LINK_RE.sub(_replace_link, result)
    result = _BOLD_STAR_RE.sub(r"\1", result)
    result = _BOLD_UNDERSCORE_RE.sub(r"\1", result)
    result = _STRIKE_RE.sub(r"\1", result)
    result = _ITALIC_STAR_RE.sub(r"\1", result)
    result = _ITALIC_UNDERSCORE_RE.sub(r"\1", result)
    result = _INLINE_CODE_RE.sub(r"\1", result)
    result = _LEFTOVER_EMPHASIS_RE.sub("", result)
    result = _BULLET_RE.sub(rf"\1{LIST_MARKER} ", result)
    result = _ESCAPE_RESTORE_RE.sub(lambda match: chr(int(match.group(1))), result)
    result = _TRAILING_SPACE_RE.sub("", result)
    result = _EXTRA_BLANKS_RE.sub("\n\n", result)
    return result.strip()


# ---- 용어 사전 ----

TERMS_PATH = Path(__file__).with_name("terms.json")
# 한 메시지에 붙는 용어 각주 개수 상한. 둘을 넘어가면 각주가 본문만큼 길어지고, 그 시점부터
# 사용자는 각주를 읽지 않는다 — 설명이 없는 것과 같아진다.
TERM_FOOTNOTE_MAX_ENTRIES = 2
# 각주 한 줄의 모양: "ℹ️ 예수금: 설명".
# "용어:"라는 말을 붙이지 않는다 — 이모지가 이미 "이건 설명이다"를 뜻하고, 두 개가 연속으로
# 붙으면 같은 말이 두 줄 반복된다. 표제어와 설명 사이도 줄표(—)가 아니라 콜론이다.
# 줄표는 한국어 문장에서 자연스럽지 않고, 사전에서 "말: 뜻" 관계를 나타내는 것은 콜론이다.
TERM_FOOTNOTE_MARK = "ℹ️"
# 각주 한 개는 한 줄이다. 말풍선 폭을 넘으면 텔레그램이 알아서 접는다.
#
# 폭에 맞춰 미리 자르지 않는다. 읽는 쪽 화면 폭도 글자 크기 설정도 알 수 없으므로 우리가
# 고른 지점이 그 사람에게 맞는 지점일 이유가 없고, 어긋나면 접힌 자리가 두 군데가 된다.
# 설명은 이어지는 한 문장이라 접혀도 이어 읽힌다 — 나열 항목과 달리 경계를 표시할 것이 없다.


@dataclass(frozen=True)
class TermEntry:
    term: str
    description: str
    aliases: tuple[str, ...] = ()


_terms_cache: tuple[TermEntry, ...] | None = None
_surface_index_cache: tuple[tuple[str, TermEntry], ...] | None = None


def load_terms(path: Path | None = None) -> tuple[TermEntry, ...]:
    """``terms.json``을 읽어 용어 목록을 돌려준다. 실패하면 빈 목록 (#297).

    파일이 깨졌다고 메시지를 못 보내면 안 된다 — 용어 각주는 부가 정보이고, 그것 때문에
    체결 통지나 긴급 알림이 막히는 것은 명백히 손해다. 대신 경고를 남겨 조용히 사라지지 않게 한다.

    ``path``를 명시하면 캐시를 쓰지도 채우지도 않는다. 테스트가 임시 사전을 끼워 넣어도
    프로세스 전역 캐시가 오염되지 않게 하기 위해서다.
    """
    global _terms_cache, _surface_index_cache
    if path is None and _terms_cache is not None:
        return _terms_cache

    target = path or TERMS_PATH
    entries: list[TermEntry] = []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        for item in raw.get("terms", []):
            term = str(item.get("term", "")).strip()
            description = str(item.get("description", "")).strip()
            if not term or not description:
                continue
            aliases = tuple(
                alias.strip()
                for alias in item.get("aliases", [])
                if isinstance(alias, str) and alias.strip()
            )
            entries.append(TermEntry(term=term, description=description, aliases=aliases))
    except Exception as exc:
        logger.warning("용어 사전을 읽지 못했습니다 (%s): %s", target, exc)
        entries = []

    result = tuple(entries)
    if path is None:
        _terms_cache = result
        _surface_index_cache = None
    return result


def _surface_index() -> tuple[tuple[str, TermEntry], ...]:
    """(찾을 문자열, 용어) 목록. 긴 표기가 먼저 오도록 정렬한다.

    "시가총액"과 "시가"가 둘 다 사전에 있으면 같은 자리에서 긴 쪽이 이겨야 한다. 짧은 쪽이
    이기면 시가총액을 시가로 설명하게 되는데, 이건 설명이 아니라 오답이다.
    """
    global _surface_index_cache
    if _surface_index_cache is not None:
        return _surface_index_cache

    pairs: list[tuple[str, TermEntry]] = []
    for entry in load_terms():
        for surface in (entry.term, *entry.aliases):
            pairs.append((surface, entry))
    pairs.sort(key=lambda pair: -len(pair[0]))
    _surface_index_cache = tuple(pairs)
    return _surface_index_cache


def find_terms(
    text: str,
    *,
    limit: int = TERM_FOOTNOTE_MAX_ENTRIES,
    exclude: frozenset[str] | set[str] = frozenset(),
) -> list[TermEntry]:
    """본문에서 설명할 용어를 첫 등장 순서로 최대 ``limit``개 고른다 (#297).

    한국어에는 낱말 경계가 없어 부분 문자열 매칭이 불가피하다. 그래서 겹침을 막는 규칙이
    둘이다. 같은 자리에서는 긴 표기가 이기고(_surface_index), 이미 고른 구간과 겹치는
    매칭은 버린다. 별칭으로 걸려도 각주에는 대표 용어와 그 설명이 나가고, 같은 용어는
    본문에 몇 번 나오든 한 번만 설명한다.

    등장 위치는 첫 번째만이 아니라 **전부** 모은다. 첫 등장만 보면 그 자리가 더 긴 용어에
    먹혔을 때 뒤에 있는 단독 등장까지 함께 사라진다 — "미체결 잔량이 있고 체결은 아직입니다"
    에서 체결의 첫 등장은 미체결 안이라 버려지고, 11자 뒤의 진짜 체결은 보이지도 않았다
    (#297 자가리뷰).

    ``exclude``는 상한을 적용하기 **전에** 걸러진다. 나중에 걸러내면 제외될 용어가 두
    자리 중 하나를 먹고 사라져, 설명이 필요한 말이 상한에 밀린다.
    """
    if not text or limit <= 0:
        return []

    hits: list[tuple[int, int, TermEntry]] = []
    for surface, entry in _surface_index():
        start = text.find(surface)
        while start >= 0:
            hits.append((start, start + len(surface), entry))
            start = text.find(surface, start + 1)

    # 위치 오름차순, 같은 위치면 긴 것 먼저.
    hits.sort(key=lambda hit: (hit[0], -(hit[1] - hit[0])))

    chosen: list[TermEntry] = []
    taken: list[tuple[int, int]] = []
    seen: set[str] = set()
    for start, end, entry in hits:
        if entry.term in seen or entry.term in exclude:
            continue
        if any(start < taken_end and taken_start < end for taken_start, taken_end in taken):
            continue
        seen.add(entry.term)
        taken.append((start, end))
        chosen.append(entry)
        if len(chosen) >= limit:
            break
    return chosen


def term_footnote(
    text: str,
    *,
    level: str = DEFAULT_TELEGRAM_USER_LEVEL,
    question: str = "",
) -> str:
    """용어 각주 블록. 중급이거나 설명할 용어가 없으면 빈 문자열 (#297).

    ``question``은 이 메시지를 부른 사용자 입력이다. 거기 이미 등장한 용어는 설명하지
    않는다 — 사용자가 직접 타이핑한 단어는 아는 단어라는 신호이고, "예수금 얼마야?"라는
    질문에 예수금의 정의가 따라붙는 건 설명이 아니라 참견이다.

    길이로 거르지 않는다. 처음 도입할 때는 "각주가 본문보다 길면 생략"으로 막았는데,
    그 규칙은 초보가 짧은 기본 질문을 던졌을 때 — 각주가 가장 필요한 순간 — 정확히
    각주를 막았다. 질문에 없던 낯선 말이라면 답변이 한 줄이어도 설명할 값어치가 있다.

    질문 텍스트가 없는 경로(스케줄러 알림·브리핑)는 아무것도 걸러내지 않는다. 사용자가
    부르지 않은 메시지라 "이미 아는 말"이라고 볼 근거가 없다.
    """
    if level != LEVEL_BEGINNER:
        return ""
    entries = find_terms(text, exclude=_terms_in(question))
    return "\n".join(
        f"{TERM_FOOTNOTE_MARK} {entry.term}: {entry.description}" for entry in entries
    )


def _terms_in(question: str) -> frozenset[str]:
    """질문 텍스트에 등장한 용어 이름들. 개수 상한 없이 전부 본다.

    find_terms의 상한(2개)을 쓰면 질문에 셋을 적은 사용자가 셋째 용어의 설명을 받게 된다.
    여기서 세는 것은 "보여줄 것"이 아니라 "빼야 할 것"이라 상한이 없어야 맞다.
    """
    if not question:
        return frozenset()
    return frozenset(
        entry.term for surface, entry in _surface_index() if surface in question
    )


# ---- 추론 각주 (#260에서 이관) ----

REASONING_FOOTNOTE_SEPARATOR = "─────"
# 각주 전체 길이 상한. 각주 자리를 먼저 확보하고 본문을 자르는 구조라, 각주가 길어지면
# 본문 몫이 그만큼 줄어든다. 상한이 없으면 본문 예산이 음수가 되어 답변이 통째로
# 사라진 채 각주만 남을 수 있다.
REASONING_FOOTNOTE_MAX_CHARS = 300

# NAT supervisor 브랜치명 → 사용자에게 보여줄 한국어 라벨.
# 키는 finus_nat/configs/router*.yml의 branches[].name과 같아야 한다.
AGENT_LABELS: dict[str, str] = {
    "trading_agent": "트레이딩 에이전트",
    "monitoring_agent": "모니터링 에이전트",
    "news_agent": "뉴스 에이전트",
    "recommend_agent": "추천 에이전트",
    "strategy_agent": "전략 에이전트",
    "diary_agent": "매매일지 에이전트",
}

# 도구 강제 원장(finus_nat/src/nat_finus_nat/finus_api.py의 _record_to_ledger 호출부)에
# 기록되는 내부 도구명 → 사용자에게 보여줄 한국어 라벨.
# 매핑에 없는 도구는 내부 이름을 그대로 노출한다 — 조용히 감추면 각주가 "확인한 자료"를
# 실제보다 적게 보여주게 되어, 근거를 보여준다는 목적 자체가 무너진다.
TOOL_LABELS: dict[str, str] = {
    "finus_account_balance": "KIS 시세·계좌 조회",
    "finus_market_news": "뉴스 검색",
    "finus_disclosure_signal": "지분공시 조회",
    "finus_earnings_report": "DART 실적 조회",
    "finus_mcp_trading_today_orders": "당일 주문·체결 조회",
    "finus_mcp_trading_get_balance": "계좌 잔고 조회",
    "finus_mcp_trading_balance_rlz_pl": "실현손익 조회",
    "finus_save_diary": "매매일지 저장",
    "finus_list_diaries": "매매일지 조회",
}

# routed_agent → 메시지 틀. 라우팅 결과가 곧 메시지 종류다. 본문을 파싱해 "일지 같다"고
# 추측하지 않는다 — #260이 각주에서 세운 원칙(모델의 주장이 아니라 실제 라우팅만 믿는다)이
# 틀 선택에도 그대로 적용된다.
AGENT_KINDS: dict[str, str] = {
    "diary_agent": KIND_DIARY,
}

# 본문 치환에 쓰는 도구명 표. 각주용 TOOL_LABELS와 MCP 서버 쪽 이름을 합친다.
# 긴 이름을 먼저 시도해야 finus_mcp_trading_get_balance가 get_balance로 반쯤 치환되지 않는다.
_TOOL_NAME_LABELS: dict[str, str] = {**MCP_TOOL_LABELS, **TOOL_LABELS}
_TOOL_NAME_RE = re.compile(
    r"(?<![\w])(?:%s)(?![\w])"
    % "|".join(re.escape(name) for name in sorted(_TOOL_NAME_LABELS, key=len, reverse=True))
)


def humanize_tool_names(text: str) -> str:
    """본문에 노출된 내부 도구명을 한국어 라벨로 바꾼다 (#297).

    표에 없는 이름은 그대로 둔다. 각주에서와 같은 규칙이다 — 모르는 도구를 감추면 사용자가
    보는 근거가 실제보다 줄어든다.

    낱말 경계를 보므로 finus_mcp_trading_get_balance 안의 get_balance는 따로 걸리지 않는다.
    """
    if not text:
        return text
    return _TOOL_NAME_RE.sub(lambda match: _TOOL_NAME_LABELS[match.group(0)], text)


def clamp(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> str:
    """앞뒤 공백을 털고 상한에 맞춰 자른다. 자르면 말줄임 접미사를 붙인다."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    # max(0, ...): limit이 말줄임 접미사보다 짧아도 음수 인덱스로 뒤집히지 않게 한다.
    keep = max(0, limit - len(TELEGRAM_TRUNCATION_SUFFIX))
    return f"{stripped[:keep]}{TELEGRAM_TRUNCATION_SUFFIX}"


def reasoning_footnote(routed_agent: Any, tools_used: Any) -> str:
    """담당 에이전트·확인한 자료 각주를 만든다. 근거가 없으면 빈 문자열 (#260).

    #297에서 telegram_commands._reasoning_footnote를 그대로 옮겨 왔다. 동작은 불변이고,
    옮긴 이유는 나가는 문장을 조립하는 지점이 한 군데여야 용어 각주와의 순서·길이 예산을
    한 곳에서 결정할 수 있기 때문이다.

    입력은 NAT 응답의 ``routed_agent``/``tools_used`` 필드에서만 온다 — 답변 텍스트를
    파싱해 에이전트명이나 도구명을 추측하지 않는다 (#129와 같은 원칙). 파싱으로 만들면
    "실제로 호출한 도구"가 아니라 "모델이 호출했다고 주장하는 도구"가 되어, 근거로
    보여주는 각주가 오히려 환각을 사실처럼 전달하는 표면이 된다.

    두 값이 모두 없으면(구버전 finus_nat 등) 각주를 조용히 생략한다. 라우팅은 됐는데
    도구가 하나도 실행되지 않은 경우는 "없음"으로 드러낸다 — 도구 없이 나온 답변이라는
    사실 자체가 사용자가 알아야 할 근거다.

    호출했지만 실패한 도구는 ``(실패)``, 성공했지만 결과가 비었던 도구는 ``(결과 없음)``을
    붙여 데이터를 얻은 호출과 구분한다. 둘 다 그냥 "확인한 자료"로 적으면 사용자는 답변이
    그 데이터에 근거했다고 읽는다 — 실제로는 아니므로, 근거를 보여준다는 이 기능의 목적과
    정반대의 오독이 된다. 빈 결과는 특히 NAT가 "[조회 결과 없음] ..."을 본문으로 돌려주는
    경로(#209)와 겹쳐, 본문은 데이터가 없다고 말하는데 각주만 자료를 확인했다고 말하게
    된다. 그렇다고 목록에서 빼면 시도조차 안 한 것처럼 보이므로, 빼지 않고 결과를 함께 적는다.
    """
    agent = routed_agent.strip() if isinstance(routed_agent, str) else ""
    tools = list(tools_used) if isinstance(tools_used, (list, tuple)) else []
    if not agent and not tools:
        return ""

    entries: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name.strip():
            continue
        entry = TOOL_LABELS.get(name.strip(), name.strip())
        if getattr(tool, "ok", False) is not True:
            entry = f"{entry}(실패)"
        elif getattr(tool, "empty", False) is True:
            entry = f"{entry}(결과 없음)"
        if entry not in entries:  # 서로 다른 내부 도구가 같은 라벨로 접힐 수 있다
            entries.append(entry)

    if not agent and not entries:
        return ""

    parts: list[str] = []
    if agent:
        parts.append(f"🤖 {AGENT_LABELS.get(agent, agent)}")
    parts.append(f"📚 확인한 자료: {', '.join(entries) if entries else '없음'}")
    footnote = f"{REASONING_FOOTNOTE_SEPARATOR}\n{' · '.join(parts)}"
    return clamp(footnote, REASONING_FOOTNOTE_MAX_CHARS)


def kind_for_agent(routed_agent: Any) -> str:
    """라우팅된 에이전트에 맞는 메시지 종류. 모르면 분석답변."""
    if not isinstance(routed_agent, str):
        return KIND_ANALYSIS
    return AGENT_KINDS.get(routed_agent.strip(), KIND_ANALYSIS)


# ---- 조립 ----


def render(
    text: Any,
    kind: str = KIND_ANALYSIS,
    level: str = DEFAULT_TELEGRAM_USER_LEVEL,
    *,
    reasoning: str = "",
    question: str = "",
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> str:
    """텔레그램으로 나가기 직전의 단일 통과 지점 (#297).

    순서: 마크다운 정리 → 도구명 한국어화 → 틀 적용 → 용어 각주 → 추론 각주 → 길이 예산.

    ``question``은 이 메시지를 부른 사용자 입력이다. 거기 이미 나온 용어는 설명하지
    않는다(term_footnote 참조). 봇이 먼저 거는 메시지는 그냥 비워 둔다.

    길이 예산은 뒤에서부터 확보한다. 추론 각주(근거) 자리를 먼저 잡고, 그다음 용어 각주,
    남은 자리에 본문을 맞춘다 — 합친 뒤에 자르면 긴 답변에서만 각주가 통째로 사라져,
    정작 근거와 설명이 필요한 메시지에서만 근거와 설명이 없어진다 (#260에서 온 규칙).

    용어 스캔 대상은 본문뿐이다. 추론 각주에는 도구 라벨("KIS 시세·계좌 조회")이 들어
    있어 함께 스캔하면 사용자가 쓰지도 않은 말에 설명이 붙는다.

    ``reasoning``이 비어 있고 걸린 용어가 없으면 결과는 ``clamp(본문)``과 정확히 같다 —
    #260 이전 경로의 동작이 그대로 유지된다.
    """
    body = template_for(kind).apply(
        humanize_tool_names(sanitize_markdown(str(text)))
    )

    reasoning_block = f"\n\n{reasoning}" if reasoning else ""
    budget = limit - len(reasoning_block)

    terms = term_footnote(body, level=level, question=question)
    term_block = f"\n\n{terms}" if terms else ""
    # 용어 각주는 근거가 아니라 편의다. 자리가 모자라면 본문보다 먼저 포기한다 —
    # 본문 예산을 절반 아래로 밀어내면서까지 설명을 남길 이유가 없다.
    if len(term_block) > budget // 2:
        term_block = ""
    budget -= len(term_block)

    return f"{clamp(body, budget)}{term_block}{reasoning_block}"
