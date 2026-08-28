"""docs/telegram-output-preview.md 생성기 (#297).

문서를 손으로 쓰지 않고 실제 backend.presentation.render를 태워 뽑는다. 손으로 쓰면
코드와 어긋나고, 어긋난 문서를 보고 검수하면 없는 문제를 고치거나 있는 문제를 놓친다 —
#297 검수에서 두 번 겪었다(가짜 시세 샘플, 낡은 표제어 서술).

    uv run --project backend python backend/scripts/generate_output_preview.py

출력 계층이나 terms.json을 고쳤으면 이 스크립트를 다시 돌려 문서를 갱신한다.
"""

import argparse
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "telegram-output-preview.md"

from backend.presentation import (  # noqa: E402
    KIND_ALERT,
    alert_kind,
    KIND_ANALYSIS,
    KIND_BRIEFING,
    KIND_DIARY,
    KIND_QUOTE,
    LEVEL_BEGINNER,
    LEVEL_INTERMEDIATE,
    reasoning_footnote,
    render,
)
from backend.telegram_notifier import (  # noqa: E402
    TelegramNotifier,
    should_send_telegram_alert,
)
from backend.telegram_commands import (  # noqa: E402
    _format_trend_detail,
    _format_trend_summary,
    _parse_investor_flows,
)
from backend.tests.test_telegram_trend import MCP_TREND_RESPONSE  # noqa: E402

TREND_FLOWS = _parse_investor_flows(MCP_TREND_RESPONSE)

# 1번은 실제 포맷터를 태운다 — preview가 코드와 어긋나면 검수의 의미가 없다.
URGENT_ALERT_DATA = {
    "summary": "국민연금 지분 5.1% → 6.3%",
    "details": {
        "decision": "HOLD",
        "confidence_score": 0.82,
        "reason": "**대량보유** 변동으로 단기 변동성 확대 가능성",
    },
    "urgency": "critical",
    "urgency_reason": "5%룰 공시 접수",
    "telegram_alert": True,
}
URGENT_ALERT_BODY = TelegramNotifier("token", "1").format_analysis_alert(
    stock="삼성전자", source="disclosure", analysis_data=URGENT_ALERT_DATA
)
# 배너 판정은 전송 게이트와 같은 것을 쓴다 (presentation.alert_kind 참조).
URGENT_ALERT_KIND = alert_kind(
    should_send_telegram_alert(URGENT_ALERT_DATA, alert_mode="urgent")
)


class Tool:
    def __init__(self, name, ok=True, empty=False):
        self.name = name
        self.ok = ok
        self.empty = empty


LONG_BODY = (
    "삼성전자 실적 코멘트입니다. 이번 분기 영업이익은 시장 기대치를 웃돌았고, "
    "반도체 부문의 수급이 개선되는 흐름이 확인됩니다. "
) * 60

CASES = [
    (
        "1. 긴급 분석 알림 (긴급 알림 틀)",
        "scheduler → telegram_notifier.send_analysis_alert",
        URGENT_ALERT_KIND,
        URGENT_ALERT_BODY,
        "",
        "",
    ),
    (
        "2. 촉매 이벤트 알림 (알림 틀)",
        "scheduler._send_due_catalyst_alerts",
        KIND_ALERT,
        "📅 D-2 촉매 이벤트\n"
        "종목: NAVER\n"
        "유형: 실적 발표\n"
        "예정일: 2026-08-20\n"
        "출처: [DART 공시](https://dart.fss.or.kr/x/12345)",
        "",
        "",
    ),
    (
        "3. 현재가 조회 (시세 틀)",
        "/quote 삼성전자 — mcp-trading/index.js:230 실제 출력",
        KIND_QUOTE,
        "[삼성전자] 현재가 시세\n"
        "- 종목코드: 005930\n"
        "- 현재가: 71,300원\n"
        "- 전일 대비: 1200 (1.71%)\n"
        "- 거래량: 12483201\n"
        "- 시가/고가/저가: 70,100원 / 71,900원 / 69,800원",
        "",
        "삼성전자",
    ),
    (
        "4. 수급 조회 (시세 틀) — 요약 + 상세 버튼",
        "/trend 삼성전자",
        KIND_QUOTE,
        _format_trend_summary("삼성전자", TREND_FLOWS),
        "",
        "삼성전자",
    ),
    (
        "4-1. 수급 상세 (버튼을 누른 경우, 별도 메시지)",
        "market:trend_detail 콜백",
        KIND_QUOTE,
        _format_trend_detail("삼성전자", TREND_FLOWS),
        "",
        "삼성전자",
    ),
    (
        "5. 짧은 분석 답변 (분석답변 틀)",
        "자연어 질문 → NAT",
        KIND_ANALYSIS,
        "지금 예수금은 1,204,300원입니다.",
        reasoning_footnote("trading_agent", (Tool("finus_mcp_trading_get_balance"),)),
        "지금 계좌에 돈 얼마 남았어?",
    ),
    (
        "6. 마크다운 잔재가 많은 답변 (분석답변 틀)",
        "LLM이 습관적으로 마크다운을 뱉은 경우",
        KIND_ANALYSIS,
        "### 오늘의 판단\n"
        "**결론**: 관망을 권합니다.\n\n"
        "- 외국인 __순매수__ 전환은 확인됐지만 거래량이 받쳐주지 않습니다.\n"
        "- 다음 주 *FOMC* 전까지는 변동성이 큽니다.\n"
        "- 근거: [지분공시 원문](https://dart.fss.or.kr/x/9911)\n\n"
        "---\n"
        "> 투자 판단의 책임은 본인에게 있습니다.\n"
        "`get_investor_trading` 결과를 참고했습니다.",
        reasoning_footnote(
            "strategy_agent",
            (Tool("finus_market_news"), Tool("finus_disclosure_signal", empty=True)),
        ),
        "오늘 삼성전자 어때?",
    ),
    (
        "7. 매매일지 (일지 틀)",
        "자연어 → diary_agent",
        KIND_DIARY,
        "2026-08-18 기록을 저장했습니다.\n\n"
        "매수: 삼성전자 10주 @ 70,100 (지정가)\n"
        "메모: 실적 발표 전 분할 매수 1회차\n"
        "현재 평가손익: +12,000원",
        reasoning_footnote("diary_agent", (Tool("finus_save_diary"),)),
        "오늘 일지 써줘",
    ),
    (
        "8. 아주 긴 답변 (본문은 잘려도 각주는 남는다)",
        "자연어 질문 → NAT, 4000자 초과",
        KIND_ANALYSIS,
        LONG_BODY,
        reasoning_footnote(
            "news_agent", (Tool("finus_market_news"), Tool("finus_earnings_report"))
        ),
        "삼성전자 실적 어때?",
    ),
    (
        "9. 용어가 셋 이상 등장하는 답변 (첫 등장 2개만)",
        "자연어 질문 → NAT",
        KIND_ANALYSIS,
        "예수금은 320만원 남아 있습니다. 어제 낸 지정가 주문은 전부 체결됐고, "
        "현재 평가손익은 +48,000원입니다. 증거금 비율이 높은 종목이라 추가 매수 시 "
        "주문가능금액을 먼저 확인하세요.",
        reasoning_footnote("trading_agent", (Tool("finus_mcp_trading_get_balance"),)),
        "예수금이랑 평가손익 알려줘",
    ),
    (
        "10. 모닝 브리핑 (브리핑 틀)",
        "scheduler.morning_briefing_task",
        KIND_BRIEFING,
        "📰 오늘의 시장 요약\n"
        "미국 증시 상승 마감, 달러 약세. 코스피는 **강보합** 출발이 예상됩니다.\n\n"
        "📊 관심종목 동향\n"
        "- 삼성전자: 외국인 순매수 3일째\n"
        "- NAVER: 목표주가 상향\n\n"
        "🎯 오늘의 트레이딩 아이디어\n"
        "- 반도체 수급 개선 흐름 관찰\n\n"
        "⚡ 주요 촉매 이벤트\n"
        "- NAVER 실적 발표 (D-2)",
        reasoning_footnote(None, ()),
        "",
    ),
]


# preview는 사람이 읽는 파일이다. 8번은 길이 상한을 넘기려고 같은 문장을 반복한 더미
# 본문이라 그대로 실으면 파일의 대부분이 그 반복으로 찬다. 표시할 때만 가운데를 접는다 —
# render()가 하는 일이 아니라는 것을 접힌 자리에 명시한다.
ELIDE_OVER = 900
ELIDE_HEAD = 260
ELIDE_TAIL = 420


# 텔레그램 모바일 말풍선 한 줄은 기본 글꼴에서 반각 44칸 안팎이다. 코드블록은 접히지 않아
# preview만 보면 긴 줄이 멀쩡해 보인다 — 1차 검수에서 각주 48줄이 전부 접히는 걸 못 잡은
# 이유가 그것이다. 눈금자를 깔고 넘치는 줄에 초과 폭을 표시한다.
BUBBLE = 44
RULER = "─" * BUBBLE + "┆ 44칸 (모바일 말풍선 한 줄)"


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def block(text, *, elide=False):
    shown = text
    if elide and len(text) > ELIDE_OVER:
        omitted = len(text) - ELIDE_HEAD - ELIDE_TAIL
        mark = f"…〔preview 표시용 중략 {omitted}자, 같은 문장이 계속 이어집니다〕…"
        shown = text[:ELIDE_HEAD] + "\n\n" + mark + "\n\n" + text[-ELIDE_TAIL:]
    marked = []
    for line in shown.split("\n"):
        over = display_width(line) - BUBBLE
        marked.append(line + (f"   ↵ +{over}칸" if over > 0 else ""))
    return "```\n" + RULER + "\n" + "\n".join(marked) + "\n```"


lines = [
    "# 텔레그램 출력 미리보기",
    "",
    "> 이 파일은 `backend/scripts/generate_output_preview.py`가 만든다. 손으로 고치지 말고",
    "> 스크립트를 다시 돌린다 — 예시는 전부 `backend/presentation.py`의 `render()`를 실제로",
    "> 태워 뽑은 것이라, 손으로 쓰면 코드와 어긋난다.",
    ">",
    "> ```",
    "> uv run --project backend python backend/scripts/generate_output_preview.py",
    "> ```",
    "",
    "출력 계층(#297)이 텔레그램으로 내보내는 메시지가 실제로 어떻게 보이는지 모아 둔 문서다.",
    "문구를 승인하거나 고치라고 지시할 때, 그리고 출력 계층을 손볼 때 회귀를 눈으로 확인할 때 쓴다.",
    "",
    "- **before** = 출력 계층을 거치지 않은 원본 (LLM·MCP·포맷터가 준 그대로)",
    "- **after (초보)** = 기본값. 용어 각주가 붙는다",
    "- **after (중급)** = `/level 중급`. 용어 각주만 빠지고 근거 각주는 남는다",
    "",
    "각 블록 맨 위 눈금자는 모바일 말풍선 한 줄 폭(반각 44칸)이다. 그 줄을 넘는 줄에는",
    "`↵ +N칸`을 달아 뒀다 — 실제 화면에서는 거기서 접힌다. 눈금자가 있는 이유는 코드블록이",
    "접히지 않아서다. 눈금자가 없던 판본으로 검수했을 때 각주 48줄이 전부 접히고 있는 걸",
    "아무도 못 봤다.",
    "",
    "## 이 문서가 보여주는 정책",
    "",
    "규칙의 근거는 코드 주석에 있다. 여기서는 어느 예시에서 볼 수 있는지만 짚는다.",
    "",
    "1. **폭을 가정하지 않는다.** 말풍선 한 줄 폭은 화면 크기 × 텔레그램 글자 크기 설정 ×",
    "   OS 큰 글씨 설정이라 보장할 수 없다. 긴 줄은 텔레그램이 접게 두고, 대신 **접힌 뒤에도",
    "   구조가 남게** 만든다 — 나열 항목은 전부 `- `로 시작하므로 표시 없는 줄은 앞 줄의",
    "   계속이라는 뜻이 된다 (1·3·4·10번). `presentation` 모듈 독스트링의 정책 항목.",
    "2. **가로 표는 쓰지 않는다.** 한 행이 접히면 뒷조각이 아무 열에나 떨어져 표가 통째로",
    "   무너진다. 세로로 펴고, 길어지면 요약과 버튼으로 나눈다 (4·4-1번, 62칸 → 26칸+35칸).",
    "3. **배너는 봇이 먼저 거는 메시지에만.** 알림·일지에 붙고(1·2·7번) 시세·분석답변에는",
    "   붙지 않는다(3~6·8·9번). 모든 메시지에 배너가 있으면 배너의 정보량이 0이 된다.",
    "   알림은 urgency로 갈린다: critical/high면 `🚨 긴급 알림`, 그 외 `🔔 알림`.",
    "4. **정해진 값은 표로 한국어화한다.** `HOLD`→`보유 유지`, `critical`→`매우 높음`,",
    "   `disclosure`→`공시` (1번). 본문에 새어 나온 내부 도구명도 같은 규칙 (6번).",
    "   표에 없는 값은 감추지 않고 원문 그대로 통과시킨다.",
    "5. **마크다운은 지우지 않고 옮긴다.** 이 봇은 `parse_mode`를 쓰지 않아 표기가 그대로",
    "   화면에 뜬다. 굵게 표기는 알맹이만 남기고 링크는 `라벨 (주소)`가 된다 (6번).",
    "6. **용어 각주는 검수된 사전에서만.** `backend/terms.json`에 있는 말만, 첫 등장 2개까지.",
    "   사용자가 질문에 직접 쓴 말은 아는 말로 보고 건너뛴다 (9번: 질문에 있는 예수금·",
    "   평가손익 대신 지정가·체결을 설명한다).",
    "",
    "## 아직 사람이 정해야 하는 것",
    "",
    "- `terms.json`의 설명 48개는 LLM 초안이다. 제도 수치와 표현을 검수해야 한다",
    "  (파일 맨 위 `_readme`에 기준이 있다).",
    "- 8번처럼 4000자를 넘는 답변은 지금 잘린다. 분할 전송으로 바꾸는 건 후속 과제다.",
    "",
    "---",
    "",
]

# 수급은 backend가 본문을 다시 만드는 유일한 경로라 before가 body와 다르다.
BEFORE_OVERRIDES = {
    "4. 수급 조회 (시세 틀) — 요약 + 상세 버튼": MCP_TREND_RESPONSE,
    "4-1. 수급 상세 (버튼을 누른 경우, 별도 메시지)": MCP_TREND_RESPONSE,
}

for title, source, kind, body, reasoning, question in CASES:
    before = BEFORE_OVERRIDES.get(title, body)
    if reasoning:
        before = f"{before}\n\n{reasoning}"
    elide = body is LONG_BODY
    lines += [
        f"## {title}",
        "",
        f"경로: `{source}`"
        + (f"  ·  사용자 입력: `{question}`" if question else ""),
        "",
        "**before**",
        "",
        block(before, elide=elide),
        "",
        "**after (초보)**",
        "",
        block(
            render(body, kind, LEVEL_BEGINNER, reasoning=reasoning, question=question),
            elide=elide,
        ),
        "",
        "**after (중급)**",
        "",
        block(
            render(body, kind, LEVEL_INTERMEDIATE, reasoning=reasoning, question=question),
            elide=elide,
        ),
        "",
        "---",
        "",
    ]

parser = argparse.ArgumentParser(description="텔레그램 출력 미리보기 문서를 생성한다.")
parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
args = parser.parse_args()

args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"{args.out} 생성 ({len(lines)}줄)")
