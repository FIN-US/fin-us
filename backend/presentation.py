"""텔레그램으로 나가는 모든 문장이 마지막으로 통과하는 출력 계층 (#297).

이 레포의 원칙은 일관되게 "형식은 코드로 강제하고 LLM은 내용만 만든다"였다 (#129의
종목코드 검증, #260의 추론 각주). 이 모듈은 그 원칙을 문장 자체에 적용한다.

책임이 넷이다.

1. **마크다운 잔재 정리.** 이 봇은 ``sendMessage``에 ``parse_mode``를 넣지 않는다 —
   레포 전체에서 그 키를 쓰는 곳이 없다. 의도된 정책이다: MarkdownV2를 켜면 ``.``·``-``·
   ``(`` 이스케이프를 하나만 빠뜨려도 텔레그램이 400으로 전송 자체를 거부하고, 그 실패는
   사용자에게 "알림이 오지 않았다"로만 보인다. 평문 정책을 유지하는 대신, LLM이 습관적으로
   뱉는 굵게·밑줄·머리글 표기가 화면에 날것으로 뜨는 문제를 여기서 없앤다.

2. **메시지 종류별 틀.** 틀은 이 모듈의 상수고 LLM 출력은 본문 슬롯에만 들어간다.

3. **용어 각주.** 사전(``terms.json``)에 있는 말만 설명한다. LLM에게 즉석 설명을 시키지
   않는다 — 틀린 시세는 다음 조회에서 정정되지만 틀린 정의는 사용자의 머릿속에 남는다.
   환각을 가장 늦게 발견하게 되는 층위가 교육이다.

4. **길이 예산.** 각주 자리를 먼저 확보하고 본문을 자른다 (#260에서 온 규칙).

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
# 답인지 이미 알고 있으므로 배너는 줄만 차지한다. 게다가 이 두 경로의 현재 출력 모양은 기존
# 회귀 테스트가 문자열 동등성으로 고정하고 있다 — 배너를 붙이는 것은 눈에 보이는 동작 변경이고,
# 그 판단은 사람이 preview.md를 보고 내릴 몫이다. 붙이기로 하면 아래 두 줄의 header를 채우면
# 되고 그 외에 고칠 곳은 없다.
#
# 브리핑은 세 번째 경우다. 봇이 먼저 거는 메시지지만 본문 첫 줄이 이미 "📰 오늘의 시장 요약"
# 이라 배너를 얹으면 머리글이 두 줄 연속으로 붙는다.
TEMPLATES: dict[str, MessageTemplate] = {
    KIND_ALERT: MessageTemplate(header="🔔 알림"),
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
_BULLET_RE = re.compile(r"^([ \t]*)[*+\-][ \t]+", re.M)
_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\(\s*<?([^)\s]*)>?[^)]*\)")
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
    url = match.group(2).strip()
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
    result = _BULLET_RE.sub(r"\1• ", result)
    result = _ESCAPE_RESTORE_RE.sub(lambda match: chr(int(match.group(1))), result)
    result = _TRAILING_SPACE_RE.sub("", result)
    result = _EXTRA_BLANKS_RE.sub("\n\n", result)
    return result.strip()


# ---- 용어 사전 ----

TERMS_PATH = Path(__file__).with_name("terms.json")
# 한 메시지에 붙는 용어 각주 개수 상한. 셋을 넘어가면 각주가 본문만큼 길어지고, 그 시점부터
# 사용자는 각주를 읽지 않는다 — 설명이 없는 것과 같아진다.
TERM_FOOTNOTE_MAX_ENTRIES = 2
TERM_FOOTNOTE_PREFIX = "ℹ️ 용어:"
# 각주 총량이 본문의 몇 배까지 허용되는가. 2배는 "메시지의 2/3까지는 설명이어도 된다"는 뜻이다.
# 이 상한이 없으면 "수급 응답" 다섯 글자 아래에 마흔 글자짜리 정의가 붙어, 설명이 설명 대상을
# 밀어낸다. 반대로 너무 빡빡하게 잡으면 한두 문장짜리 답변에서 설명이 통째로 사라진다.
TERM_FOOTNOTE_MAX_BODY_RATIO = 2


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


def find_terms(text: str, *, limit: int = TERM_FOOTNOTE_MAX_ENTRIES) -> list[TermEntry]:
    """본문에서 설명할 용어를 첫 등장 순서로 최대 ``limit``개 고른다 (#297).

    한국어에는 낱말 경계가 없어 부분 문자열 매칭이 불가피하다. 그래서 겹침을 막는 규칙이
    둘이다. 같은 자리에서는 긴 표기가 이기고(_surface_index), 이미 고른 구간과 겹치는
    매칭은 버린다. 별칭으로 걸려도 각주에는 대표 용어와 그 설명이 나가고, 같은 용어는
    본문에 몇 번 나오든 한 번만 설명한다.
    """
    if not text or limit <= 0:
        return []

    hits: list[tuple[int, int, TermEntry]] = []
    for surface, entry in _surface_index():
        position = text.find(surface)
        if position >= 0:
            hits.append((position, position + len(surface), entry))

    # 위치 오름차순, 같은 위치면 긴 것 먼저.
    hits.sort(key=lambda hit: (hit[0], -(hit[1] - hit[0])))

    chosen: list[TermEntry] = []
    taken: list[tuple[int, int]] = []
    seen: set[str] = set()
    for start, end, entry in hits:
        if entry.term in seen:
            continue
        if any(start < taken_end and taken_start < end for taken_start, taken_end in taken):
            continue
        seen.add(entry.term)
        taken.append((start, end))
        chosen.append(entry)
        if len(chosen) >= limit:
            break
    return chosen


def term_footnote(text: str, *, level: str = DEFAULT_TELEGRAM_USER_LEVEL) -> str:
    """용어 각주 블록. 중급이거나 걸린 용어가 없으면 빈 문자열 (#297).

    각주가 본문에 비해 지나치게 길면 뒤에서부터 버린다(TERM_FOOTNOTE_MAX_BODY_RATIO).
    설명이 설명 대상을 밀어내면 그건 도움이 아니다. 하나도 못 남기면 각주 없이 나간다.
    """
    if level != LEVEL_BEGINNER:
        return ""
    entries = find_terms(text)
    if not entries:
        return ""
    lines = [
        f"{TERM_FOOTNOTE_PREFIX} {entry.term} — {entry.description}" for entry in entries
    ]
    # 넘치면 뒤에서부터 버린다. 앞쪽이 본문에 먼저 등장한 용어이므로, 하나만 남길 수 있다면
    # 사용자가 먼저 만난 말을 남기는 것이 맞다.
    allowance = len(text) * TERM_FOOTNOTE_MAX_BODY_RATIO
    while lines and len("\n".join(lines)) > allowance:
        lines.pop()
    return "\n".join(lines)


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
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> str:
    """텔레그램으로 나가기 직전의 단일 통과 지점 (#297).

    순서: 마크다운 정리 → 틀 적용 → 용어 각주 → 추론 각주 → 길이 예산.

    길이 예산은 뒤에서부터 확보한다. 추론 각주(근거) 자리를 먼저 잡고, 그다음 용어 각주,
    남은 자리에 본문을 맞춘다 — 합친 뒤에 자르면 긴 답변에서만 각주가 통째로 사라져,
    정작 근거와 설명이 필요한 메시지에서만 근거와 설명이 없어진다 (#260에서 온 규칙).

    용어 스캔 대상은 본문뿐이다. 추론 각주에는 도구 라벨("KIS 시세·계좌 조회")이 들어
    있어 함께 스캔하면 사용자가 쓰지도 않은 말에 설명이 붙는다.

    ``reasoning``이 비어 있고 걸린 용어가 없으면 결과는 ``clamp(본문)``과 정확히 같다 —
    #260 이전 경로의 동작이 그대로 유지된다.
    """
    body = template_for(kind).apply(sanitize_markdown(str(text)))

    reasoning_block = f"\n\n{reasoning}" if reasoning else ""
    budget = limit - len(reasoning_block)

    terms = term_footnote(body, level=level)
    term_block = f"\n\n{terms}" if terms else ""
    # 용어 각주는 근거가 아니라 편의다. 자리가 모자라면 본문보다 먼저 포기한다 —
    # 본문 예산을 절반 아래로 밀어내면서까지 설명을 남길 이유가 없다.
    if len(term_block) > budget // 2:
        term_block = ""
    budget -= len(term_block)

    return f"{clamp(body, budget)}{term_block}{reasoning_block}"
