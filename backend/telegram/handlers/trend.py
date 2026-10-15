"""/trend 수급 응답을 읽고 요약하는 순수 함수."""

import re
from dataclasses import dataclass

from backend.telegram.presentation import as_list_items


# ---- 수급 표 (#297 검수 4차) ----

# 텔레그램에서 표는 유지할 수 없다. 한 행이 폭을 넘으면 뒷조각이 아무 열에나 떨어져
# 정렬이 통째로 무너지고, 폭은 읽는 쪽 글자 크기 설정에 달려 있어 우리가 보장할 수 없다.
# 그래서 가로 표를 세로 나열로 바꾼다 — 세로는 접혀도 "- " 표시가 항목 경계를 지킨다.
#
# 5일 × 3주체를 세로로 펴면 20줄이라 그것대로 안 읽힌다. 기본 메시지는 방향 한 줄과
# 최근 1일만 보여주고, 5일치는 버튼을 누른 사람에게만 별도 메시지로 보낸다. 링크로 빼지
# 않는 이유는 목적지 화면이 없기도 하지만, 이 봇의 값이 "텔레그램 안에서 끝난다"는 데
# 있기 때문이다.
#
# mcp/trading/index.js의 getInvestorTrading이 만드는 행을 읽는다. 다른 서비스의 출력
# 형식에 기대는 파싱이라 깨질 수 있으므로, 한 행도 못 읽으면 원문을 그대로 내보낸다
# (_format_investor_flow의 None 반환). 형식이 바뀌면 요약이 사라질 뿐 조회는 계속 된다.
_INVESTOR_ROW_RE = re.compile(
    r"^\s*(\d{8})\s*\|\s*개인:\s*(-?[\d,]+)\s*\|\s*"
    r"외국인:\s*(-?[\d,]+)\s*\|\s*기관:\s*(-?[\d,]+)\s*$",
    re.M,
)
# 요약에 싣는 일수. 상세는 MCP가 준 만큼 전부 싣는다.
TREND_SUMMARY_DAYS = 1


def trend_detail_button_text(days: int) -> str:
    """상세 버튼 라벨. 일수를 고정값으로 박지 않는다 (#297 검수 2차).

    MCP는 최대 5일을 주지만 실제로는 그날 데이터가 있는 만큼만 온다 — 연휴 직후면 3일치다.
    라벨에 5를 박아 두면 "5일 상세 보기"를 눌렀는데 "3일 상세"가 나온다.
    """
    return f"📊 {days}일 상세 보기"


@dataclass(frozen=True)
class InvestorFlow:
    """하루치 투자자별 순매수 수량(주). 음수면 순매도."""

    date: str
    individual: int
    foreign: int
    institution: int


# (표시 이름, 필드명). 순서가 화면 순서다.
TREND_ACTORS: tuple[tuple[str, str], ...] = (
    ("개인", "individual"),
    ("외국인", "foreign"),
    ("기관", "institution"),
)


def _parse_investor_flows(raw: str) -> list[InvestorFlow]:
    """수급 응답에서 날짜별 행을 뽑는다. 못 읽으면 빈 목록.

    값이 없는 칸을 "-"로 채우는 행(formatQuantity의 빈 값 처리)은 정규식에 걸리지 않아
    조용히 빠진다. 그 하루를 요약에서 빼는 편이, 0으로 채워 "순매수도 순매도도 아님"이라고
    말하는 것보다 낫다 — 없는 데이터를 지어내지 않는다는 이 레포의 선(#162)과 같다.
    """
    flows = [
        InvestorFlow(
            date=match.group(1),
            individual=int(match.group(2).replace(",", "")),
            foreign=int(match.group(3).replace(",", "")),
            institution=int(match.group(4).replace(",", "")),
        )
        for match in _INVESTOR_ROW_RE.finditer(raw)
    ]
    # MCP가 최신순으로 준다고 가정하지 않는다. 정렬이 뒤집히면 "3일 연속"이 거꾸로 세어진다.
    return sorted(flows, key=lambda flow: flow.date, reverse=True)


def _format_flow_date(date: str) -> str:
    return f"{date[4:6]}/{date[6:8]}" if len(date) == 8 else date


def _format_flow_rounded(value: int) -> str:
    """요약용 표기. 만 주 이상은 만 단위로 접는다.

    요약에서 중요한 것은 방향과 규모지 정확한 주수가 아니다. 정확한 값이 필요한 사람은
    상세 버튼을 누르고, 거기서는 반올림하지 않는다.
    """
    if abs(value) >= 10_000:
        return f"{value / 10_000:+,.0f}만주"
    return f"{value:+,}주"


def _flow_streak(flows: list[InvestorFlow], field: str) -> int:
    """최근일부터 같은 방향이 이어진 일수. 0이 끼면 거기서 끊는다."""
    latest = getattr(flows[0], field)
    if latest == 0:
        return 0
    positive = latest > 0
    streak = 0
    for flow in flows:
        value = getattr(flow, field)
        if value == 0 or (value > 0) != positive:
            break
        streak += 1
    return streak


def _trend_headline(flows: list[InvestorFlow]) -> str:
    """가장 길게 이어진 방향 한 줄. 계산일 뿐 판단이 아니다.

    "외국인 3일 연속 순매수"는 데이터에서 바로 나오는 사실이다. 여기에 그래서 어떻다는
    말을 붙이지 않는다 — 그건 이 계층이 할 일이 아니고, 붙이는 순간 투자 권유가 된다.
    """
    ranked = sorted(
        (
            (_flow_streak(flows, field), abs(getattr(flows[0], field)), label, field)
            for label, field in TREND_ACTORS
        ),
        reverse=True,
    )
    streak, _, label, field = ranked[0]
    if streak == 0:
        return ""
    direction = "순매수" if getattr(flows[0], field) > 0 else "순매도"
    if streak == 1:
        return f"{label} {direction}"
    return f"{label} {streak}일 연속 {direction}"


def _format_flow_block(flow: InvestorFlow, *, rounded: bool) -> list[str]:
    formatter = _format_flow_rounded if rounded else (lambda value: f"{value:+,}")
    return [
        _format_flow_date(flow.date),
        *as_list_items(
            [f"{label} {formatter(getattr(flow, field))}" for label, field in TREND_ACTORS]
        ),
    ]


def _format_trend_summary(stock: str, flows: list[InvestorFlow]) -> str:
    """방향 한 줄 + 최근 1일 세로.

    파싱 결과를 인자로 받는다. 호출부가 이미 한 번 읽었고(빈 목록이면 원문으로 떨어진다)
    그 길이가 상세 버튼 라벨에도 쓰이므로, 여기서 또 읽으면 두 값이 어긋날 수 있다.
    """
    lines = [f"[{stock}] 투자자 매매동향"]
    headline = _trend_headline(flows)
    if headline:
        lines.append(headline)
    for flow in flows[:TREND_SUMMARY_DAYS]:
        lines += ["", *_format_flow_block(flow, rounded=True)]
    return "\n".join(lines)


def _format_trend_detail(stock: str, flows: list[InvestorFlow]) -> str:
    """전체 일수를 세로로. 반올림하지 않는다."""
    lines = [f"[{stock}] 투자자 매매동향 {len(flows)}일 상세", "단위: 주, +는 순매수"]
    for flow in flows:
        lines += ["", *_format_flow_block(flow, rounded=False)]
    return "\n".join(lines)
