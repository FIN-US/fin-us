import asyncio
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote as _url_quote

from fastapi import HTTPException
from sqlmodel import Session

from .config import (
    DART_MCP_PARAMS,
    KIS_ORDER_ENV,
    KIS_REAL_ORDER_ENABLED,
    NEWS_MCP_PARAMS,
    TRADING_MCP_PARAMS,
    VISUALIZATION_URL,
)
from .catalyst_repo import SqliteCatalystEventRepo
from .database import engine
from .redis_state import (
    InMemoryPendingOrderStore,
    PendingOrderStore,
    RedisSchedulerState,
    RedisPendingOrderStore,
    RedisTelegramPollerStore,
    TelegramPollerFailure,
    TelegramPollerState,
    TelegramPollerStore,
    create_redis_client,
    redis_state,
)
from .presentation import (
    DEFAULT_TELEGRAM_USER_LEVEL,
    KIND_ANALYSIS,
    KIND_QUOTE,
    LEVEL_BEGINNER,
    LEVEL_INTERMEDIATE,
    as_list_items,
    sanitize_markdown,
    reasoning_footnote,
    kind_for_agent,
    level_label,
    normalize_level,
    render,
    split_for_telegram,
)
from .services import llm_chat, run_mcp_tool, short_error as _short_error
from .watchlist_repo import SqliteWatchlistRepo
from .telegram_notifier import (
    SETTLED_SEND_RETRY_BACKOFF_SECONDS,
    SETTLED_SEND_TIMEOUT_SECONDS,
    TELEGRAM_ALERT_MODES,
    TelegramCommandNotifier,
    TelegramPollerNotifier,
    fetch_telegram_api,
    send_text_settled,
    telegram_notifier,
)
from .timeutil import KST
from .trading_orders import (
    ORDER_CANCEL_CALLBACK,
    ORDER_CONFIRM_CALLBACK,
    ORDER_EXPIRES_AFTER,
    McpTradingOrderGateway,
    OrderSide,
    OrderType,
    PendingOrder,
    TradeLedger,
    TradeRecorder,
    is_korean_market_open,
    order_reply_markup,
)
from .stock_code import (
    _STOCK_CODE_EXTRACT_RE,
    _is_unresolved_echo,
    extract_stock_name,
    is_orderable_stock_code,
)
from .order_assist import ProposalTrigger, parse_current_price, run_order_assist

logger = logging.getLogger(__name__)

ALERT_COMMAND_HELP = "사용법: /alerts urgent | all | off | status"
LEVEL_COMMAND_HELP = "사용법: /level 초보 | 중급"
LEVEL_ONBOARDING_QUESTION = "주식 투자 얼마나 익숙하세요?"
# 온보딩은 이 한 문항이 전부다. 지식 퀴즈로 수준을 판정하지 않는다 — 봇이 사용자를 시험하는
# 관계가 되는 순간 설명을 켜 두는 것이 창피한 일이 되고, 그러면 초보 모드가 쓰이지 않는다.
START_MESSAGE = "\n".join(
    [
        "안녕하세요. 이 봇은 시세·공시·매매를 텔레그램에서 다룹니다.",
        "",
        LEVEL_ONBOARDING_QUESTION,
        "초보를 고르면 낯선 용어에 한 줄 설명이 붙습니다. 나중에 /level 로 바꿀 수 있어요.",
    ]
)
BUY_COMMAND_HELP = "사용법: /buy <종목명> <수량> [지정가]"
SELL_COMMAND_HELP = "사용법: /sell <종목명> <수량> [지정가]"
WATCH_COMMAND_HELP = "사용법: /watch add <종목명> | remove <종목명> | list"
CATALYST_COMMAND_HELP = "사용법: /catalysts <종목명>"
ADVISE_COMMAND_HELP = "사용법: /advise <종목명>"
EARNINGS_COMMAND_HELP = "사용법: /earnings <종목명> [기간]  예: /earnings 삼성전자 2025Q1"
NATURAL_ORDER_HELP = "자연어 주문을 해석할 수 없습니다. /buy 또는 /sell 형식으로 입력하세요."
# 미등록 코드는 mcp-trading/stock-master.js가 {code: X, name: X, market: "UNKNOWN"}을
# echo하므로 종목명 == 종목코드가 곧 미해석 신호다.
UNRESOLVED_STOCK_WARNING = (
    "⚠️ 종목명을 확인하지 못했습니다. 입력한 코드가 맞는지 다시 확인하세요."
)
# _STOCK_CODE_EXTRACT_RE, _ORDERABLE_STOCK_CODE_RE → backend/stock_code.py (#140)
# ORDER_CONFIRM_CALLBACK, ORDER_CANCEL_CALLBACK, order_reply_markup → trading_orders.py (#314).
# 스케줄러의 자동 제안도 같은 버튼을 써야 해서 옮겼다. 이름은 여기서 계속 읽을 수 있다.
ORDER_STALE_CALLBACK_TEXT = "이전 주문 버튼입니다. 최신 주문 메시지에서 다시 선택하세요."
ALERT_CALLBACK_PREFIX = "alerts:"
LEVEL_CALLBACK_PREFIX = "level:"
BALANCE_REFRESH_CALLBACK = "balance:refresh"
TRADE_CALLBACK_PREFIX = "trade:"
LOOKUP_CALLBACK_PREFIX = "lookup:"
WATCH_LIST_CALLBACK = "watch:list"
WATCH_LIST_QUOTE_DELAY_SECONDS = 1.1
MARKET_QUOTE_CALLBACK = "market:quote"
MARKET_TREND_CALLBACK = "market:trend"
MARKET_TREND_DETAIL_CALLBACK = "market:trend_detail"
MARKET_STALE_CALLBACK_TEXT = "이전 조회 버튼입니다. 최신 조회 메시지에서 다시 선택하세요."
MARKET_CALLBACK_LIMIT = 100
# 실패한 update는 offset을 유지해 재시도한다(일시 장애 때 명령을 버리지 않기 위함).
# 다만 결정론적으로 실패하는 update는 영원히 재시도되며 뒤의 명령까지 막으므로
# 예산을 넘기면 건너뛴다 (#241).
#
# 예산은 "횟수"가 아니라 "시간"이다. 고정 5초 × 3회는 실질 10초여서 redis 재시작·컨테이너
# 재기동·배포 어느 것도 버티지 못한다 — 일시 장애에 명령을 버리지 않겠다는 원래 목적을
# 달성하지 못한다 (PR #242 리뷰).
UPDATE_RETRY_WINDOW_SECONDS = 60.0
# 전송 실패는 그 update의 문제가 아니라 전역 장애(429 rate limit·5xx)다. 같은 예산을 쓰면
# 429 몇 초 때문에 대기 중인 명령이 전부 폐기되는데, 폐기 통지도 같은 이유로 실패해
# 사용자는 아무것도 못 받는다. 기존 동작(막히더라도 복구되면 전부 실행) 대비 회귀이므로
# 전송 실패에는 훨씬 긴 창을 준다 (PR #242 리뷰).
#
# 이 값은 무한정 키우면 안 된다. handle_update가 던지는 지배적 경로가 전송 실패라서,
# 영구적 전송 실패(특정 메시지의 Markdown 파싱 400)가 하나 들어오면 offset이 이 시간만큼
# 얼어붙는다. #259 1단계 이전에는 그 동결이 "#241의 재발"이었지만 지금은 채택한 설계다 —
# 배치가 그 update에서 끊기는 것이 이 봇이 중복 실행 대신 고른 쪽이다. 그래서 상한이 필요한
# 이유는 오히려 더 강해졌다: 얼어붙는 동안 뒤의 명령도 실행되지 않아 봇이 이 시간만큼 통째로
# 무응답이다. 상한이 있다는 사실 자체를 테스트가 고정한다
# (test_poller_eventually_skips_persistent_send_failures).
SEND_FAILURE_RETRY_WINDOW_SECONDS = 300.0
# 재시도 간격. 실패가 이어지면 폴링을 늦춰 텔레그램 rate limit을 자극하지 않는다.
# 폴러는 단일 인스턴스라 동시 기상이 겹칠 일이 없어 jitter는 두지 않았다.
#
# 위 두 창과 맞물려 실제 예산은 이렇게 정해진다 (PR #242 리뷰):
#   일반 실패   t=0, 5, 20, 65  → 4시도 / 65초 후 폐기
#   전송 실패   t=0, 5, 20, 65, 110 … 335 → 10시도 / 335초 후 폐기 (마지막 간격 45초 반복)
UPDATE_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 45.0)
UPDATE_SKIPPED_NOTICE = "요청 처리에 실패했어요. 다시 시도해주세요."
# 통지에 붙일 실패 명령 요약의 최대 길이. 명령을 연달아 보낸 사용자가 무엇을 다시
# 보내야 하는지 알 수 있어야 한다 (PR #242 리뷰).
UPDATE_LABEL_LIMIT = 40
# 폴러 상태 저장소 호출의 상한. 이게 없으면 fail-open이 fail-hang으로 무너진다: 저장소가
# 예외 대신 hang하면 _persist_state의 await가 돌아오지 않고, 그 await는 run()의 배치 루프
# 안이라 폴러 태스크가 통째로 멈춘다. except Exception은 예외가 나야 도는데 hang은 예외를
# 내지 않으므로 "인메모리로 계속" 로그조차 남지 않는다.
#
# #268이 create_redis_client()에 socket_timeout을 걸어 redis 클라이언트발 hang은 소켓에서
# 먼저 잘리지만, 이 wait_for는 그대로 남긴다. 두 상한이 덮는 범위가 다르기 때문이다:
# state_store는 덕 타이핑된 주입 지점이라 redis가 아닌 구현이 들어올 수 있고(테스트의
# 인메모리 저장소가 그렇다), 소켓 타임아웃은 명령 하나의 상한이지 호출 하나의 상한이 아니다
# — load()나 save()가 명령을 여럿 쓰게 되면 상한이 그만큼 곱해진다.
#
# 두 값이 같아(둘 다 3.0) redis 저장소에서는 어느 쪽이 먼저 발화할지 정해져 있지 않다.
# 결과는 같고(삼킨 뒤 인메모리로 계속) 로그 문구만 갈린다 — 소켓 쪽이 이기면 "Timeout
# reading from host:port"가 붙어 원인이 더 분명해진다.
#
# 이 PR이 노출을 넓혔다는 점이 근거다: 이전에는 redis를 만지는 명령(/alerts, /buy, /confirm,
# /cancel)만 이 위험을 졌고 /help·자연어·/watch는 blackhole 중에도 서비스됐지만, 이제 모든
# update가 루프 안에서 redis를 동기 대기한다 (PR #251 리뷰).
#
# 값은 사용자 체감(폴링 지연)과 일시적 지연 흡수 사이의 절충이다. 정상 redis는 1ms 수준이라
# 이 상한에 닿는 것은 이미 비정상이며, docker-compose에서 가장 흔한 장애(컨테이너 다운)는
# 즉시 ECONNREFUSED를 내므로 이 경로를 타지 않는다.
#
# 값을 조정할 때 알아둘 성질: 타임아웃은 update마다 걸리므로 hang 중 배치 하나가 멈추는
# 시간은 "배치 크기 × 이 값"으로 선형이다(응답 자체는 handle_update가 먼저 끝내 나간 뒤,
# 그 뒤에 죽은 시간이 붙는다). getUpdates limit이 100이면 최악 약 300초다 (PR #251 리뷰).
STATE_STORE_TIMEOUT_SECONDS = 3.0


# 확정 전송의 재시도 상수(SETTLED_SEND_*)와 루프(send_text_settled)는 telegram_notifier로
# 옮겼다 (PR #327 리뷰). 스케줄러의 자동 제안(#314)이 같은 재시도를 써야 하는데, 핸들러
# 메서드로 두면 그쪽에서 부를 수 없어 한쪽만 단발 전송이 되고 같은 429에 한쪽 메시지만
# 조용히 버려진다. 이름은 위 import로 여기서 계속 읽을 수 있다.
#
# 다만 **여기 있는 이름을 monkeypatch해도 상한은 바뀌지 않는다** — 값을 읽는 것은
# telegram_notifier 안이고 이쪽은 임포트 시점에 묶인 별개 바인딩이다. 테스트가 상한을
# 줄이려면 telegram_notifier 쪽을 패치해야 하며, 잘못된 대상을 잡으면
# test_settled_send_gives_up_at_the_wall_clock_bound의 elapsed 단언이 잡는다.

# getUpdates 배치 크기. 명시하지 않으면 Telegram 기본값이 100이라, 배치 전체가 settled
# 전송에 닿으면 한 루프의 점유가 100 × SETTLED_SEND_TIMEOUT_SECONDS까지 늘어나 사실상
# 상한이 없어진다. 배치를 작게 끊으면 최악이 유계가 된다 (PR #253 2차 리뷰).
#
# #259 2단계가 이 계산에서 가장 큰 항을 뺐다 — 체결 통지가 outbox로 나가면서 /confirm
# 성공 경로는 더 이상 settled 전송을 쓰지 않는다. 남은 settled 경로(/buy 프롬프트,
# /cancel, /confirm 403·불명확, /earnings·자연어)는 그대로라 이 상한은 계속 필요하다.
GET_UPDATES_LIMIT = 10


class TelegramSendError(RuntimeError):
    """텔레그램 전송 실패. handle_update가 던지는 예외 중 유일하게 update와 무관하다.

    MCP·redis·저장소 오류는 전부 사용자 메시지로 변환되므로, handle_update가 실제로
    던지는 지배적 경로는 전송 실패다. 이걸 update별 poison으로 세면 429 한 번에
    대기 중인 명령이 전부 폐기되므로 폴러가 별도 예산으로 다뤄야 한다 (PR #242 리뷰).
    """


ALERT_MODE_EMOJIS = {
    "urgent": "🚨",
    "all": "📣",
    "off": "🔕",
}
TELEGRAM_INTERACTIVE_HELP = "\n".join(
    [
        "사용 가능한 명령:",
        "/alerts urgent|all|off|status - Telegram 알림 모드 변경",
        "/level 초보|중급 - 용어 설명 표시 수준 변경",
        "/balance - 예수금·총자산·보유 종목 조회",
        "/watch add <종목명>|remove <종목명>|list - 관심 종목 관리",
        "/catalysts <종목명> - 예정 촉매 이벤트 조회",
        "/trade - 매수·매도 주문 입력 안내",
        "/lookup - 현재가·수급 조회 입력 안내",
        "/visualize - Unity 포트폴리오 시각화 링크",
        "/quote <종목명> - 현재가 조회",
        "/trend <종목명> - 외국인·기관·개인 수급 조회",
        "/earnings <종목명> [기간] - DART 실적·뉴스 분석",
        "/advise <종목명> - 주문 보조: 제안 → 한도 검사 → 검증 → 확정 버튼",
        "/buy <종목명> <수량> [지정가] - 매수 주문 준비",
        "/sell <종목명> <수량> [지정가] - 매도 주문 준비",
        "/confirm - 대기 주문 확정",
        "/cancel - 대기 주문 취소",
        "일반 문장은 NAT에게 바로 질문합니다.",
    ]
)
TELEGRAM_BOT_COMMANDS = [
    {"command": "help", "description": "사용 가능한 명령 확인"},
    {"command": "balance", "description": "예수금·총자산·보유 종목 조회"},
    {"command": "watch", "description": "관심 종목 추가·삭제·조회 (add/remove/list)"},
    {"command": "catalysts", "description": "예정 촉매 이벤트 조회"},
    {"command": "quote", "description": "종목 현재가 조회"},
    {"command": "trend", "description": "종목 외국인·기관·개인 수급 조회"},
    {"command": "earnings", "description": "DART 실적·뉴스 분석"},
    {"command": "alerts", "description": "Telegram 알림 모드 변경"},
    {"command": "level", "description": "용어 설명 표시 수준 변경 (초보/중급)"},
    {"command": "start", "description": "봇 소개와 수준 설정"},
    {"command": "visualize", "description": "Unity 포트폴리오 시각화 링크"},
    {"command": "trade", "description": "매수·매도 주문 입력 안내"},
    {"command": "lookup", "description": "현재가·수급 조회 입력 안내"},
    {"command": "advise", "description": "주문 보조 (예: /advise 삼성전자)"},
    {"command": "buy", "description": "매수 주문 준비 (예: /buy 삼성전자 1)"},
    {"command": "sell", "description": "매도 주문 준비 (예: /sell 삼성전자 1)"},
    {"command": "confirm", "description": "대기 주문 확정"},
    {"command": "cancel", "description": "대기 주문 취소"},
]
QUOTE_COMMAND_HELP = "사용법: /quote <종목명>"
TREND_COMMAND_HELP = "사용법: /trend <종목명>"
TRADE_COMMAND_HELP = "\n".join(
    [
        "매매 주문 입력 안내:",
        f"{BUY_COMMAND_HELP}  예: /buy 삼성전자 1",
        f"{SELL_COMMAND_HELP}  예: /sell NAVER 1 200000",
        "주문은 실제 제출 전 반드시 확정이 필요합니다.",
    ]
)
LOOKUP_COMMAND_HELP = "\n".join(
    [
        "조회 입력 안내:",
        f"{QUOTE_COMMAND_HELP}  예: /quote 삼성전자",
        f"{TREND_COMMAND_HELP}  예: /trend 삼성전자",
    ]
)

# ---- 추론 과정 표시 (#260) ----

NAT_PROGRESS_MESSAGE = "⏳ 분석 중입니다..."
# 진행 메시지 삭제가 거부됐을 때 남길 종료 표시. "분석 중"이 영원히 남지 않게 한다.
PROGRESS_DONE_MESSAGE = "✅ 분석 완료"

# 각주 상수(REASONING_FOOTNOTE_*)와 라벨 표(AGENT_LABELS·TOOL_LABELS), 각주 조립 자체는
# #297에서 presentation으로 옮겼다. 동작은 그대로다 — 옮긴 이유는 나가는 문장을 조립하는
# 지점이 하나여야 용어 각주와의 순서·길이 예산을 한 곳에서 정할 수 있기 때문이다.
# 이 모듈은 그 이름들을 다시 내보내지 않는다. 재수출만 남은 임포트는 정의가 두 곳에 있다는
# 착시를 만들고, 정적 검사에는 미사용 임포트로 보인다 (#297 자가리뷰).

_telegram_command_task: asyncio.Task | None = None

# 설명 수준 캐시 (#297). 값 자체는 거의 바뀌지 않으므로 짧게 들고 있어도 사용자가 체감할
# 지연은 없고, /level로 바꾸면 _apply_level이 그 자리에서 캐시를 갱신하므로 즉시 반영된다.
LEVEL_CACHE_TTL_SECONDS = 60.0
# 읽기에 실패했을 때는 더 길게 기다린다. 실패의 지배적 원인은 redis 부재이고 그건 다음
# 1초 안에 낫지 않는다 — 짧게 잡으면 장애 내내 메시지마다 소켓 타임아웃을 다시 문다.
LEVEL_LOOKUP_FAILURE_COOLDOWN_SECONDS = 300.0
_level_cache: tuple[str, float] | None = None


def _level_cache_get(now: float) -> str | None:
    if _level_cache is None or now >= _level_cache[1]:
        return None
    return _level_cache[0]


def _level_cache_put(level: str, expires_at: float) -> None:
    global _level_cache
    _level_cache = (level, expires_at)


def reset_level_cache() -> None:
    """캐시를 비운다. 테스트가 수준 저장소를 갈아끼울 때 쓴다."""
    global _level_cache
    _level_cache = None



def _nat_answer_message(
    result: Any,
    level: str = DEFAULT_TELEGRAM_USER_LEVEL,
    question: str = "",
) -> str:
    """NAT 응답을 텔레그램 메시지로 만든다 (#260, #297).

    ``routed_agent``/``tools_used``는 ``services.NatAnswer``가 실어 오는 속성이다.
    속성이 없는 값(구버전 경로, 문자열만 주는 대역)이면 각주 없이 본문만 보낸다.

    #297에서 조립을 presentation.render에 위임했다. 마크다운 정리와 용어 각주가 함께
    붙지만 추론 각주의 모양은 그대로다. 틀은 라우팅된 에이전트로 정한다(매매일지 답변은
    일지 틀). 본문을 파싱해 추측하지 않는 것이 #260 각주와 같은 원칙이다.

    길이는 여기서 맞추지 않는다 (#313). 상한을 넘으면 전송 계층이 나눠 보내고, 각주는
    마지막 조각에 남는다 — 자리 다툼이 없어졌으므로 예산 규칙도 없어졌다.
    """
    routed_agent = getattr(result, "routed_agent", None)
    return render(
        result,
        kind_for_agent(routed_agent),
        level,
        reasoning=reasoning_footnote(routed_agent, getattr(result, "tools_used", ())),
        question=question,
    )


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
# mcp-trading/index.js의 getInvestorTrading이 만드는 행을 읽는다. 다른 서비스의 출력
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


def _telegram_command_parts(text: str) -> tuple[str, str, str]:
    command, _, argument = text.partition(" ")
    command_name, separator, bot_username = command.partition("@")
    return command_name.lower(), bot_username.lower() if separator else "", argument.strip()


def _create_order_gateway() -> McpTradingOrderGateway:
    order_env = "real" if KIS_ORDER_ENV == "real" else "demo"
    return McpTradingOrderGateway(
        server_params=TRADING_MCP_PARAMS,
        mcp_runner=run_mcp_tool,
        order_env=order_env,
        real_order_enabled=KIS_REAL_ORDER_ENABLED,
    )


def _create_trade_recorder() -> TradeRecorder:
    return TradeRecorder(lambda: Session(engine))


def _create_pending_order_store() -> PendingOrderStore:
    """프로덕션 Redis pending_order 저장소를 생성한다.

    단일 Redis 클라이언트 인스턴스를 생성해 커넥션 풀을 재사용한다.
    핸들러 인스턴스당 한 번만 호출되므로 클라이언트 누적 없음.

    반환 타입은 구체 클래스가 아니라 Protocol이다. 유일한 호출부인 주입 지점이 계약 밖의
    것을 집어들지 못하게 막아, 구현체 교체가 그 지점으로 번지지 않게 한다 (PR #287 리뷰).
    """
    return RedisPendingOrderStore(create_redis_client())


def _create_poller_state_store() -> TelegramPollerStore:
    """프로덕션 폴러 상태 저장소를 생성한다 (#248).

    _create_pending_order_store와 같은 이유로 클라이언트를 하나만 만들어 풀을 재사용하고,
    같은 이유로 반환 타입을 Protocol로 좁힌다.
    """
    return RedisTelegramPollerStore(create_redis_client())


def _default_watchlist_repo() -> SqliteWatchlistRepo:
    return SqliteWatchlistRepo(lambda: Session(engine))


def _default_catalyst_repo() -> SqliteCatalystEventRepo:
    return SqliteCatalystEventRepo(lambda: Session(engine))


class TelegramCommandHandler:
    def __init__(
        self,
        *,
        notifier: TelegramCommandNotifier,
        state_factory: Callable[[], Any] = redis_state,
        watchlist_repo: Any | None = None,
        catalyst_repo: Any | None = None,
        mcp_runner: Callable[[Any, str, dict[str, Any]], Any] = run_mcp_tool,
        llm_runner: Callable[..., Any] = llm_chat,
        order_gateway: Any | None = None,
        trade_recorder: TradeLedger | None = None,
        now_factory: Callable[[], datetime] | None = None,
        visualization_url: str = VISUALIZATION_URL,
        pending_order_store: PendingOrderStore | None = None,
    ):
        self.notifier = notifier
        self.state_factory = state_factory
        self.watchlist_repo = watchlist_repo if watchlist_repo is not None else _default_watchlist_repo()
        self.catalyst_repo = catalyst_repo if catalyst_repo is not None else _default_catalyst_repo()
        self.mcp_runner = mcp_runner
        self.llm_runner = llm_runner
        self.order_gateway = order_gateway
        # None을 그대로 두지 않는다 (#259 2단계). 체결 통지 outbox가 이 원장 위에 서므로,
        # 원장이 없는 핸들러는 "체결됐는데 통지가 조용히 사라지는" 배포가 된다. 프로덕션은
        # 어차피 항상 _create_trade_recorder()를 주입해 왔으니 실동작 변화는 없고, 바뀐 것은
        # 미주입 시의 뜻이다 — "원장 없음"이 아니라 "기본 원장"이다. watchlist_repo·
        # catalyst_repo가 같은 규칙을 쓴다.
        self.trade_recorder: TradeLedger = (
            trade_recorder if trade_recorder is not None else _create_trade_recorder()
        )
        self.now_factory = now_factory or (lambda: datetime.now(KST))
        self.visualization_url = visualization_url.strip()
        # 프로덕션에서는 TelegramCommandPoller가 RedisPendingOrderStore를 주입한다.
        # 미주입 시의 InMemoryPendingOrderStore는 동기 dict 인터페이스도 함께 제공하지만
        # 그건 PendingOrderStore 계약 밖이라, 여기서는 프로토콜이 보장하는 것만 쓴다.
        if pending_order_store is None:
            logger.warning(
                "pending_order_store 미주입 — InMemoryPendingOrderStore 사용. "
                "멀티워커 환경에서는 주문이 프로세스 간 격리된다(#63)."
            )
            pending_order_store = InMemoryPendingOrderStore()
        self.pending_orders: PendingOrderStore = pending_order_store
        self.market_callbacks: dict[str, tuple[str, str]] = {}

    async def handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query") or {}
        if callback_query:
            await self._handle_callback_query(callback_query)
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")).strip() != self.notifier.chat_id:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        command, bot_username, argument = _telegram_command_parts(text)
        if self._matches_command(command, bot_username, "/alerts"):
            await self._handle_alerts(argument)
            return
        if self._matches_command(command, bot_username, "/level"):
            await self._handle_level(argument)
            return
        if self._matches_command(command, bot_username, "/start"):
            await self._handle_start()
            return
        if self._matches_command(command, bot_username, "/help"):
            await self._send_text_or_raise(
                TELEGRAM_INTERACTIVE_HELP,
                reply_markup=self._help_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/balance"):
            await self._handle_balance()
            return
        if self._matches_command(command, bot_username, "/watch"):
            await self._handle_watch(argument)
            return
        if self._matches_command(command, bot_username, "/catalysts"):
            await self._handle_catalysts(argument)
            return
        if self._matches_command(command, bot_username, "/trade"):
            await self._send_text_or_raise(
                TRADE_COMMAND_HELP,
                reply_markup=self._trade_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/lookup"):
            await self._send_text_or_raise(
                LOOKUP_COMMAND_HELP,
                reply_markup=self._lookup_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/visualize"):
            await self._handle_visualize()
            return
        if self._matches_command(command, bot_username, "/quote"):
            await self._handle_quote(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/trend"):
            await self._handle_trend(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/earnings"):
            await self._handle_earnings(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/advise"):
            await self._handle_advise(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/buy"):
            await self._handle_order_command("BUY", argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/sell"):
            await self._handle_order_command("SELL", argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/confirm"):
            await self._handle_confirm(str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/cancel"):
            await self._handle_cancel(str(chat.get("id", "")).strip())
            return
        if text.startswith("/"):
            await self._send_text_or_raise(
                TELEGRAM_INTERACTIVE_HELP,
                reply_markup=self._help_reply_markup(),
            )
            return

        if self._looks_like_natural_order(text):
            natural_order = self._parse_natural_order_text(text)
            if natural_order is None:
                await self._send_text_or_raise(NATURAL_ORDER_HELP)
                return

            side, order_argument = natural_order
            await self._handle_order_command(
                side,
                order_argument,
                str(chat.get("id", "")).strip(),
            )
            return

        await self._handle_chat_fallback(text, str(chat.get("id", "")).strip())

    async def _send_text_or_raise(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """재시도 가능한 지점의 전송. 실패하면 폴러가 update 전체를 다시 실행한다.

        호출부가 "여기서 실패해도 다시 실행하면 같은 결과가 나온다"를 보장할 때만 쓴다.
        부수효과가 확정된 뒤라면 _send_text_settled를 쓴다 (#247).

        이 호출이 든 try 블록은 TelegramSendError를 반드시 재던져야 한다. 전송 실패를
        사용자 메시지로 변환하면 무의미한 중복 메시지가 한 번 더 나간다.
        test_every_try_containing_a_retryable_send_reraises_it이 이를 강제한다 (#249).

        메시지가 나뉘어(#313) 앞 조각만 나간 뒤 실패하면, update 재실행은 첫 조각부터
        다시 보낸다. 이미 도착한 조각이 한 벌 더 보이는 대신 빠진 뒷부분이 채워진다 —
        이 경로의 계약이 "다시 실행해도 같은 결과"이므로 재실행 지점을 조각 단위로
        기억할 자리가 없고, 있더라도 중복보다 누락이 나쁘다. 부수효과가 확정돼 재실행할
        수 없는 경로는 _send_text_settled가 조각 단위로 이어 보낸다.
        """
        sent = await self.notifier.send_text(text, reply_markup=reply_markup)
        if sent is False:
            raise TelegramSendError("telegram send failed")

    async def _send_text_settled(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """확정된 부수효과의 결과 전송. 재시도 규칙은 telegram_notifier에 하나뿐이다.

        스케줄러의 자동 제안(#314)이 같은 함수를 부른다 — 한쪽만 단발 전송이면 같은 429에
        한쪽 메시지만 조용히 버려진다 (PR #327 리뷰). 조각 나누기와 조각 단위 재시도(#313)도
        그쪽으로 함께 옮겨 갔다. 자동 제안의 승인 프롬프트는 제안 근거와 검증 의견을 함께
        실어 상한을 넘길 수 있으므로, 나누기가 이 경로에만 있으면 안 된다.

        여기서는 테스트가 대체할 수 있는 _sleep을 넘겨 주는 것 말고 하는 일이 없다.
        """
        return await send_text_settled(
            self.notifier, text, reply_markup=reply_markup, sleep=self._sleep
        )

    async def _sleep(self, seconds: float) -> None:
        """테스트가 전역 asyncio.sleep 대신 이 인스턴스만 대체할 수 있게 하는 간접층.

        _handle_watch의 조회 간격은 의도적으로 전역 asyncio.sleep을 그대로 쓴다 —
        기존 테스트가 그 sleep을 전역 패치로 가로채는 데 의존한다.
        """
        await asyncio.sleep(seconds)

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        if chat_id != self.notifier.chat_id:
            return

        callback_query_id = str(callback_query.get("id", "")).strip()
        data = str(callback_query.get("data") or "").strip()
        if data == ORDER_CONFIRM_CALLBACK or data.startswith(f"{ORDER_CONFIRM_CALLBACK}:"):
            await self._handle_order_callback(
                callback_query_id,
                chat_id,
                "confirm",
                data,
            )
            return
        if data == ORDER_CANCEL_CALLBACK or data.startswith(f"{ORDER_CANCEL_CALLBACK}:"):
            await self._handle_order_callback(
                callback_query_id,
                chat_id,
                "cancel",
                data,
            )
            return

        if data.startswith(LEVEL_CALLBACK_PREFIX):
            await self._handle_level_callback(callback_query_id, data)
            return
        if data.startswith(ALERT_CALLBACK_PREFIX):
            await self._handle_alerts_callback(callback_query_id, data)
            return

        if data == BALANCE_REFRESH_CALLBACK:
            await self._answer_callback_query(callback_query_id)
            await self._handle_balance()
            return

        if data == WATCH_LIST_CALLBACK:
            await self._answer_callback_query(callback_query_id)
            await self._handle_watch("list")
            return

        if data.startswith(TRADE_CALLBACK_PREFIX):
            await self._handle_trade_callback(callback_query_id, data)
            return

        if data.startswith(LOOKUP_CALLBACK_PREFIX):
            await self._handle_lookup_callback(callback_query_id, data)
            return

        if data.startswith(f"{MARKET_QUOTE_CALLBACK}:"):
            await self._handle_market_callback(callback_query_id, chat_id, "quote", data)
            return

        # 접두어가 겹치므로 긴 쪽(market:trend_detail)을 먼저 본다. 순서가 바뀌면 상세
        # 버튼이 요약 경로로 떨어져 같은 메시지가 두 번 나간다.
        if data.startswith(f"{MARKET_TREND_DETAIL_CALLBACK}:"):
            await self._handle_market_callback(
                callback_query_id, chat_id, "trend_detail", data
            )
            return

        if data.startswith(f"{MARKET_TREND_CALLBACK}:"):
            await self._handle_market_callback(callback_query_id, chat_id, "trend", data)
            return

        await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")

    async def _handle_order_callback(
        self,
        callback_query_id: str,
        chat_id: str,
        action: str,
        data: str,
    ) -> None:
        try:
            order = await self.pending_orders.get(chat_id)
        except Exception as exc:
            logger.error("pending_order 조회 실패 (callback): %s", exc)
            await self._answer_callback_query(
                callback_query_id,
                text="주문 저장소 오류로 처리할 수 없습니다. 잠시 후 다시 시도하세요.",
            )
            return
        token = self._extract_order_callback_token(data)
        if order is None or not token or token != order.callback_token:
            await self._answer_callback_query(
                callback_query_id,
                text=ORDER_STALE_CALLBACK_TEXT,
            )
            return

        await self._answer_callback_query(callback_query_id)
        if action == "confirm":
            await self._handle_confirm(chat_id)
            return
        await self._handle_cancel(chat_id)

    async def _answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
    ) -> None:
        if not callback_query_id:
            return
        await self.notifier.answer_callback_query(callback_query_id, text=text)

    async def _handle_alerts_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(ALERT_CALLBACK_PREFIX).strip()
        if action != "status" and action not in TELEGRAM_ALERT_MODES:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._handle_alerts(action)

    async def _handle_visualize(self) -> None:
        if not self.visualization_url:
            await self._send_text_or_raise(
                "시각화 URL이 설정되지 않았습니다. VISUALIZATION_URL 환경변수를 설정하세요."
            )
            return

        await self._send_text_or_raise(f"Unity 포트폴리오 시각화:\n{self.visualization_url}")

    async def _handle_trade_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(TRADE_CALLBACK_PREFIX).strip()
        if action == "menu":
            text = TRADE_COMMAND_HELP
            reply_markup = self._trade_reply_markup()
        elif action == "buy":
            text = BUY_COMMAND_HELP
            reply_markup = None
        elif action == "sell":
            text = SELL_COMMAND_HELP
            reply_markup = None
        else:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._send_text_or_raise(text, reply_markup=reply_markup)

    async def _handle_lookup_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(LOOKUP_CALLBACK_PREFIX).strip()
        if action == "menu":
            text = LOOKUP_COMMAND_HELP
            reply_markup = self._lookup_reply_markup()
        elif action == "quote":
            text = QUOTE_COMMAND_HELP
            reply_markup = None
        elif action == "trend":
            text = TREND_COMMAND_HELP
            reply_markup = None
        else:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._send_text_or_raise(text, reply_markup=reply_markup)

    async def _handle_market_callback(
        self,
        callback_query_id: str,
        chat_id: str,
        action: str,
        data: str,
    ) -> None:
        token = self._extract_callback_token(data)
        # pop이 아니라 get이다. pop하면 조회 결과 전송이 실패했을 때 폴러 재시도가
        # MARKET_STALE_CALLBACK_TEXT로 끝나 방금 누른 버튼에 "이전 조회 버튼입니다"가 뜬다.
        # 토큰 소비는 되돌릴 수 있는 부수효과라 확정시킬 이유가 없다 — 전송이 성공한 뒤에
        # 소비해 재시도 경계를 전송 뒤로 옮긴다 (PR #253 2차 리뷰).
        context = self.market_callbacks.get(token) if token else None
        # token이 falsy면 context는 반드시 None이므로 판정 결과는 그대로다.
        # token을 함께 보면 아래 pop(token)에서 str | None이 str로 좁혀진다.
        if token is None or context is None:
            await self._answer_callback_query(
                callback_query_id,
                text=MARKET_STALE_CALLBACK_TEXT,
            )
            return

        callback_chat_id, stock = context
        if callback_chat_id != chat_id:
            await self._answer_callback_query(
                callback_query_id,
                text=MARKET_STALE_CALLBACK_TEXT,
            )
            return

        await self._answer_callback_query(callback_query_id)
        if action == "quote":
            await self._handle_quote(stock, chat_id)
        else:
            await self._handle_trend(stock, chat_id, detail=action == "trend_detail")
        # 전송이 성공한 뒤에만 소비한다. 위에서 TelegramSendError가 나면 여기 도달하지
        # 않으므로 재시도가 같은 토큰으로 같은 조회를 다시 수행한다.
        self.market_callbacks.pop(token, None)

    def _matches_command(self, command: str, bot_username: str, expected: str) -> bool:
        if command != expected:
            return False
        if not bot_username:
            return True
        notifier_username = str(getattr(self.notifier, "bot_username", "") or "").lower()
        return bot_username == notifier_username

    async def _handle_alerts(self, argument: str) -> None:
        parts = argument.split()
        action = parts[0].lower() if parts else "status"
        async with self._state() as state:
            if action == "status":
                mode = await state.get_telegram_alert_mode()
                await self._send_text_or_raise(
                    f"현재 Telegram 알림 모드: {self._format_alert_mode(mode)}",
                    reply_markup=self._alerts_reply_markup(),
                )
                return

            if action not in TELEGRAM_ALERT_MODES:
                await self._send_text_or_raise(
                    ALERT_COMMAND_HELP,
                    reply_markup=self._alerts_reply_markup(),
                )
                return

            await state.set_telegram_alert_mode(action)
            await self._send_text_or_raise(
                f"Telegram 알림 모드가 {self._format_alert_mode(action)}(으)로 변경되었습니다.",
                reply_markup=self._alerts_reply_markup(),
            )

    async def _handle_start(self) -> None:
        """봇 소개 + 수준 1문항. /start는 텔레그램이 첫 대화에서 자동으로 보내는 명령이다 (#297).

        여기서 수준을 저장하지 않는다 — 버튼을 누르지 않고 넘어간 사용자도 기본값(초보)으로
        동작해야 한다. 저장은 버튼 콜백이나 /level에서만 일어난다.
        """
        await self._send_text_or_raise(
            START_MESSAGE,
            reply_markup=self._level_reply_markup(),
        )

    async def _handle_level(self, argument: str) -> None:
        """/level 초보|중급. 인자가 없으면 현재 설정을 버튼과 함께 보여준다 (#297).

        /alerts와 같은 모양을 일부러 지켰다 — 두 명령 다 "이 채팅의 표시 설정"이고,
        인자 없이 치면 현재 상태가 나오는 규칙이 명령마다 다르면 사용자가 외울 것이 늘어난다.
        """
        parts = argument.split()
        if not parts:
            level = await self._current_level()
            await self._send_text_or_raise(
                f"현재 설명 수준: {level_label(level)}\n{LEVEL_COMMAND_HELP}",
                reply_markup=self._level_reply_markup(),
            )
            return

        requested = normalize_level(parts[0])
        if requested is None:
            await self._send_text_or_raise(
                LEVEL_COMMAND_HELP,
                reply_markup=self._level_reply_markup(),
            )
            return

        await self._apply_level(requested)

    async def _apply_level(self, level: str) -> None:
        async with self._state() as state:
            await state.set_telegram_user_level(level)
        # 저장이 성공한 뒤에만 캐시를 갱신한다. 먼저 갱신하면 저장에 실패한 값이 이 프로세스
        # 안에서만 적용된 것처럼 보이고, 재시작 후 조용히 되돌아간다.
        _level_cache_put(level, time.monotonic() + LEVEL_CACHE_TTL_SECONDS)
        await self._send_text_or_raise(
            self._level_changed_message(level),
            reply_markup=self._level_reply_markup(),
        )

    def _level_changed_message(self, level: str) -> str:
        if level == LEVEL_BEGINNER:
            detail = "낯선 용어가 나오면 한 줄 설명을 붙여 드립니다."
        else:
            detail = "용어 설명 없이 내용만 보냅니다."
        return f"설명 수준을 {level_label(level)}(으)로 바꿨습니다. {detail}"

    async def _handle_level_callback(self, callback_query_id: str, data: str) -> None:
        requested = normalize_level(data.removeprefix(LEVEL_CALLBACK_PREFIX).strip())
        if requested is None:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return
        await self._answer_callback_query(callback_query_id)
        await self._apply_level(requested)

    async def _current_level(self) -> str:
        """저장된 설명 수준. 읽지 못하면 기본값(초보) (#297).

        redis를 만지는 다른 명령(/alerts·/buy)과 달리 여기서 예외를 삼킨다. 그 명령들은
        redis 장애가 곧 명령의 실패지만, 수준은 **다른 메시지의 곁다리 정보**다 —
        수준을 못 읽었다고 시세나 알림 자체를 실패시키면, 부가 기능이 본 기능의 가용성을
        떨어뜨리는 꼴이 된다. 못 읽으면 설명을 붙이는 쪽(초보)으로 떨어진다: 아는 사람에게
        설명이 한 줄 붙는 쪽이, 모르는 사람이 설명을 못 받는 쪽보다 덜 나쁘다.

        캐시는 성능이 아니라 그 fail-open을 싸게 만들기 위한 것이다. 캐시가 없으면 redis가
        죽어 있는 동안 나가는 **모든** 메시지가 소켓 타임아웃(3초, #268)을 한 번씩 문다 —
        조회도 답변도 3초씩 늦어지는데 그 대가로 얻는 것은 어차피 기본값이다. 그래서 실패는
        더 길게 기억한다.

        캐시가 인스턴스가 아니라 모듈 수준인 이유: 이 설정은 채팅 하나에 하나뿐이고
        (알림 모드와 같은 단일 키), 폴러가 핸들러를 다시 세워도 같은 값이어야 한다.
        """
        now = time.monotonic()
        cached = _level_cache_get(now)
        if cached is not None:
            return cached
        try:
            async with self._state() as state:
                level = await state.get_telegram_user_level()
            ttl = LEVEL_CACHE_TTL_SECONDS
        except Exception as exc:
            logger.warning("설명 수준을 읽지 못해 기본값을 씁니다: %s", exc)
            level = DEFAULT_TELEGRAM_USER_LEVEL
            ttl = LEVEL_LOOKUP_FAILURE_COOLDOWN_SECONDS
        _level_cache_put(level, now + ttl)
        return level

    async def _handle_watch(self, argument: str) -> None:
        parts = argument.split(None, 1)
        subcommand = parts[0].lower() if parts else "list"
        stock = parts[1].strip() if len(parts) > 1 else ""

        if subcommand == "list":
            watchlist = await self.watchlist_repo.get_watchlist()
            if not watchlist:
                text = "관심 종목이 없습니다.\n/watch add <종목명> 으로 추가하세요."
            else:
                lines = []
                for index, stock in enumerate(watchlist):
                    if index > 0:
                        await asyncio.sleep(WATCH_LIST_QUOTE_DELAY_SECONDS)
                    try:
                        raw = await self.mcp_runner(
                            TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": stock}
                        )
                        summary = self._parse_quote_summary(str(raw))
                        if summary:
                            price, rate = summary
                            try:
                                rate_float = float(rate.rstrip("%"))
                            except ValueError:
                                line = f"• {stock}  (조회 실패)"
                                lines.append(line)
                                continue
                            if rate_float < 0:
                                line = f"🔵 {stock}  {price}  ▼ {rate}"
                            elif rate_float == 0.0:
                                line = f"⬜ {stock}  {price}  {rate}"
                            else:
                                sign = "" if rate.startswith("+") else "+"
                                line = f"🔴 {stock}  {price}  ▲ {sign}{rate}"
                        else:
                            line = f"• {stock}  (조회 실패)"
                    except Exception:
                        line = f"• {stock}  (조회 실패)"
                    lines.append(line)
                text = "관심 종목 목록:\n" + "\n".join(lines)
            await self._send_text_or_raise(text, reply_markup=self._watch_reply_markup())
            return

        if subcommand == "add":
            if not stock:
                await self._send_text_or_raise(WATCH_COMMAND_HELP)
                return
            await self.watchlist_repo.add_to_watchlist(stock)
            await self._send_text_or_raise(
                f"관심 종목에 {stock}을(를) 추가했습니다.",
                reply_markup=self._watch_reply_markup(),
            )
            return

        if subcommand == "remove":
            if not stock:
                await self._send_text_or_raise(WATCH_COMMAND_HELP)
                return
            await self.watchlist_repo.remove_from_watchlist(stock)
            await self._send_text_or_raise(
                f"관심 종목에서 {stock}을(를) 삭제했습니다.",
                reply_markup=self._watch_reply_markup(),
            )
            return

        await self._send_text_or_raise(WATCH_COMMAND_HELP)

    async def _handle_catalysts(self, argument: str) -> None:
        stock = argument.strip()
        if not stock:
            await self._send_text_or_raise(CATALYST_COMMAND_HELP)
            return

        today = self.now_factory().astimezone(KST).date()
        events = await self.catalyst_repo.list_upcoming(stock, today=today, limit=20)
        if not events:
            await self._send_text_or_raise(
                f"{stock} 예정 이벤트가 없습니다.\n"
                "이벤트는 관심 종목 대상 스케줄러가 수집한 뒤 표시됩니다."
            )
            return

        items = [
            f"{event.event_date.isoformat()}  {getattr(event, 'description', '')}"
            f" ({self._format_catalyst_type(getattr(event, 'event_type', ''))})"
            for event in events
        ]
        # 제목은 목록 밖이다. 글머리표는 "•"가 아니라 이 봇의 나열 표시를 쓴다 (#297 자가리뷰).
        lines = [f"📅 {stock} 예정 이벤트", *as_list_items(items)]
        await self._send_text_or_raise(
            render(
                "\n".join(lines),
                KIND_QUOTE,
                await self._current_level(),
                question=argument,
            )
        )

    async def _handle_balance(self) -> None:
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {})
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(
            render(result, KIND_QUOTE, await self._current_level()),
            reply_markup=self._balance_reply_markup(),
        )

    async def _handle_quote(self, argument: str, chat_id: str) -> None:
        if not argument:
            await self._send_text_or_raise(QUOTE_COMMAND_HELP)
            return

        stock = argument.strip()
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "get_stock_quote",
                {"stock_name": stock},
            )
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(
            render(
                result,
                KIND_QUOTE,
                await self._current_level(),
                question=argument,
            ),
            reply_markup=self._market_reply_markup("trend", chat_id, stock),
        )

    async def _handle_advise(self, argument: str, chat_id: str) -> None:
        """``/advise <종목명>`` — 주문 보조 (#299).

        이 메서드가 하는 일은 파싱, 호출, 결과 전달이 전부다. 제안·한도 판정·검증·
        대기 주문 저장은 모두 order_assist.run_order_assist 안에 있다. 규칙이 두 파일에
        나뉘면 "어느 쪽이 최종 판정인가"가 흐려지고, 그 순간 한쪽만 고치는 변경이 생긴다.

        승인 결과의 버튼은 기존 _order_reply_markup을 그대로 쓴다 — 확정/취소 콜백,
        60초 만료, 체결 흐름이 /buy와 완전히 같은 경로를 탄다.
        """
        stock_name = argument.strip()
        if not stock_name:
            await self._send_text_or_raise(ADVISE_COMMAND_HELP)
            return

        await self.notifier.send_chat_action("typing")
        # 제안 에이전트 왕복은 수십 초가 걸린다. /earnings·NAT 폴백과 같은 방식으로
        # 접수 즉시 진행 메시지를 남기고, 결과가 오면 치운다 (#260).
        progress_message_id = await self._send_progress_message(NAT_PROGRESS_MESSAGE)
        trigger = ProposalTrigger(source="telegram", stock=stock_name, chat_id=chat_id)
        try:
            result = await run_order_assist(
                trigger,
                pending_orders=self.pending_orders,
                mcp_runner=self.mcp_runner,
                now_factory=self.now_factory,
            )
        except Exception as exc:
            await self._clear_progress_message(progress_message_id)
            await self._send_text_or_raise(f"주문 보조 실패: {_short_error(exc)}")
            return
        await self._clear_progress_message(progress_message_id)

        if result.order is None:
            # 거부·충돌 둘 다 여기로 온다. 충돌을 조용히 버리면 사용자는 명령이 씹힌
            # 것으로 읽는다 — 대기 주문이 있다는 사실을 반드시 알려야 한다.
            await self._send_text_settled(result.message)
            return

        notified = await self._send_text_settled(
            result.message,
            reply_markup=self._order_reply_markup(result.order),
        )
        if not notified:
            # /buy와 같은 처리다. 프롬프트가 끝내 안 나갔으면 사용자는 대기 주문의
            # 존재를 모르고, 60초 안의 다음 명령이 영문 모를 충돌로 막힌다 (#247).
            try:
                await self.pending_orders.delete(chat_id)
            except Exception as exc:
                logger.error("제안 프롬프트 미전달 후 대기 주문 정리 실패: %s", exc)

    async def _handle_order_command(
        self, side: OrderSide, argument: str, chat_id: str
    ) -> None:
        usage = BUY_COMMAND_HELP if side == "BUY" else SELL_COMMAND_HELP
        parsed = self._parse_order_argument(argument)
        if parsed is None:
            await self._send_text_or_raise(usage)
            return

        stock_name, quantity, price, order_type = parsed
        now = self.now_factory()
        if not is_korean_market_open(now):
            await self._send_text_or_raise(
                "주문 불가: 현재 장 운영 시간이 아닙니다. (평일 09:00~15:30)"
            )
            return

        try:
            await self._drop_expired_pending_order(chat_id, now)
            has_pending = await self.pending_orders.has(chat_id)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
            return
        if has_pending:
            await self._send_text_or_raise(
                "이미 대기 중인 주문이 있습니다. /confirm 또는 /cancel로 먼저 처리하세요."
            )
            return

        await self.notifier.send_chat_action("typing")
        try:
            resolved = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "resolve_stock_code",
                {"stock_name": stock_name},
            )
            stock_code = self._extract_stock_code(str(resolved))
            # 사용자가 코드를 직접 입력해도 해석된 종목명을 쓴다. 원문(stock_name)을 그대로
            # 넣으면 name == code가 되어 정상 종목에도 미해석 경고가 뜬다(#139 리뷰).
            resolved_name = self._extract_stock_name(str(resolved)) or stock_name
            if stock_code is None:
                await self._send_text_or_raise("주문 준비 실패: 종목코드를 확인할 수 없습니다.")
                return
            # 추출 성공 != 존재 확인. stock-master.js Step 3는 마스터에 없는 코드 형태
            # 입력을 market="UNKNOWN"으로 그대로 에코하므로("999999 (999999, UNKNOWN)")
            # 코드 추출은 성공하고 아래 숫자 6~7자 검사도 통과한다. 백엔드가 여기서
            # 끊지 않으면 실재 여부 확인이 KIS 왕복이나 /confirm 시점 브로커 거절로
            # 미뤄진다 — 리포트 저장 경로(services._resolve_stock_code)는 이미 같은
            # 판정으로 막고 있으므로, 위험도가 더 높은 주문 경로도 같은 선에서 막는다.
            if _is_unresolved_echo(str(resolved)):
                await self._send_text_or_raise(
                    f"주문 불가: {stock_name}({stock_code}) — 종목마스터에 없는 종목입니다. "
                    "종목코드를 확인하거나 mcp-trading/data/stocks.json을 갱신하세요."
                )
                return
            # 추출 성공 != 주문 가능. mcp-trading/order.js의 buildCashOrderBody()가
            # 기본값으로는 숫자 코드만 주문을 받으므로(#73에서 확정된 정책, #138에서
            # KIS_ALNUM_STOCK_ORDER_ENABLED 플래그로 완화 가능해짐), 시세·잔고 조회와
            # 60초 대기 슬롯을 쓰기 전에 여기서 끊는다. 이 검사가 없으면 /confirm
            # 이후에야 같은 사유로 실패한다.
            if not is_orderable_stock_code(stock_code):
                # 사용자가 입력한 건 종목명인데 거절 사유는 종목코드 형태다.
                # 코드를 함께 보여주지 않으면 인과가 보이지 않는다.
                # 조사(은/는)는 코드 끝자리 받침에 따라 갈리므로 아예 쓰지 않는다.
                await self._send_text_or_raise(
                    f"주문 불가: {stock_name}({stock_code}) — 현재 주문을 지원하지 않습니다. "
                    "ETN·펀드 등 영숫자 종목코드는 아직 주문 대상이 아닙니다."
                )
                return
            quote_result, balance_result = await asyncio.gather(
                self.mcp_runner(
                    TRADING_MCP_PARAMS,
                    "get_stock_quote",
                    {"stock_name": stock_name},
                ),
                self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {}),
            )
        except TelegramSendError:
            # 위 검증 실패 메시지(종목코드 미확인·미등록 종목·주문 불가 코드)의 전송이
            # 실패한 경우다. 전송 실패는 "이 명령을 처리하다 생긴 오류"가 아니라 "사용자에게
            # 말을 걸 수 없는 상태"라 사용자 메시지로 변환하는 것 자체가 무의미하다.
            #
            # 이 분기가 막는 것은 폴러가 실패를 못 보는 것이 아니다 — 변환한 메시지가
            # 실패하면 그것도 TelegramSendError라 어차피 폴러에 도달한다. 실제로 막는 것은
            # 변환한 메시지가 성공했을 때 "주문 준비 실패: telegram send failed"라는
            # 무의미한 중복 메시지가 사용자에게 한 번 더 가는 것이다 (PR #253 2차 리뷰).
            #
            # 이 지점은 아직 부수효과가 없어 재시도가 안전하다 (#249).
            raise
        except Exception as exc:
            await self._send_text_or_raise(f"주문 준비 실패: {_short_error(exc)}")
            return

        if order_type == "MARKET":
            # 시장가에는 지정가가 없지만, 단가를 모르는 채로 두면 그 0이 체결 기록
            # (TradeHistory.price)까지 그대로 내려가 일 거래대금 한도를 무력화한다(#309).
            # KIS 현금주문 응답에는 체결가가 없으므로(order-cash output은 ODNO·ORD_TMD뿐)
            # 방금 프롬프트용으로 받아 둔 현재가를 기록용 참고단가로 쓴다. /advise는 이미
            # 같은 값을 같은 자리에 넣는다(order_assist.run_order_assist).
            #
            # 읽지 못해도 주문은 막지 않는다 — 사용자가 명시적으로 낸 주문을 기록 사정으로
            # 되돌리는 것이기 때문이다. 대신 0이 그대로 남고, load_daily_usage의 가드가
            # 그날 /advise를 usage_failed로 막는다. 한도가 조용히 넓어지는 것보다 그쪽이
            # 낫다는 것이 #309의 결론이다.
            reference_price = parse_current_price(str(quote_result))
            if reference_price is None:
                logger.warning(
                    "시장가 참고단가를 읽지 못했다 (stock=%s) — 이 주문이 확정되면 단가 "
                    "없이 기록되고, 그때부터 오늘 /advise가 일 거래대금 집계 실패로 "
                    "막힌다 (#309)",
                    stock_code,
                )
            else:
                price = reference_price

        order = PendingOrder(
            chat_id=chat_id,
            stock_name=resolved_name,
            stock_code=stock_code,
            side=side,
            quantity=quantity,
            # MARKET이면 표시·기록용 참고단가(주문 시점 현재가)다. 주문 자체에는 쓰이지
            # 않는다 — McpTradingOrderGateway가 시장가에는 price=0을 보낸다.
            price=price,
            # now(명령 수신 시각)가 아니라 저장 직전 시각으로 스탬프한다. 위 MCP 조회는
            # run_mcp_tool의 wait_for(30초)를 두 구간(resolve, gather) 쓰므로 최대 60초가
            # 걸리고, now를 쓰면 프롬프트가 도착하기도 전에 만료 시각이 지나 있다 —
            # 절대 시각 표기가 "과거 시각에 만료됩니다"가 된다 (PR #253 2차 리뷰).
            # now는 장 운영 판정과 만료 스윕에 그대로 쓴다.
            created_at=self.now_factory(),
            order_type=order_type,
            callback_token=secrets.token_urlsafe(8),
        )
        try:
            stored = await self.pending_orders.set_if_absent(chat_id, order)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장 실패: {_short_error(exc)}")
            return
        if not stored:
            # MCP 호출 사이에 같은 chat에서 /buy가 먼저 체결된 경우
            await self._send_text_or_raise(
                "이미 대기 중인 주문이 있습니다. /confirm 또는 /cancel로 먼저 처리하세요."
            )
            return
        # 대기 주문이 저장된 뒤다 — 재실행은 has_pending에 걸려 "이미 대기 중인 주문이
        # 있습니다"로 끝나고, 사용자는 확인 버튼을 영영 받지 못한다 (#247).
        notified = await self._send_text_settled(
            self._format_order_prompt(order, str(quote_result), str(balance_result)),
            reply_markup=self._order_reply_markup(order),
        )
        if not notified:
            # 프롬프트가 끝내 안 나갔으면 사용자는 대기 주문의 존재를 모른다. 그대로 두면
            # 60초 안의 다음 /buy가 영문 모를 "이미 대기 중인 주문이 있습니다"로 막힌다.
            # 아직 아무것도 체결되지 않았으므로 지우는 쪽이 안전하다 (PR #253 2차 리뷰).
            try:
                await self.pending_orders.delete(chat_id)
            except Exception as exc:
                logger.error("프롬프트 미전달 후 대기 주문 정리 실패: %s", exc)

    async def _handle_cancel(self, chat_id: str) -> None:
        try:
            await self._drop_expired_pending_order(chat_id, self.now_factory())
            has_pending = await self.pending_orders.has(chat_id)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
            return
        if not has_pending:
            await self._send_text_or_raise("취소할 대기 주문이 없습니다.")
            return
        try:
            await self.pending_orders.delete(chat_id)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
            return
        # 대기 주문이 삭제된 뒤다 — 재실행은 "취소할 대기 주문이 없습니다"로 끝난다 (#247).
        await self._send_text_settled("대기 주문을 취소했습니다.")

    async def _handle_confirm(self, chat_id: str) -> None:
        # order_gateway 부재 체크를 claim 전에 수행해 주문이 소비되지 않게 한다.
        if self.order_gateway is None:
            await self._send_text_or_raise("주문 실행 설정이 준비되지 않았습니다.")
            return
        try:
            await self._drop_expired_pending_order(chat_id, self.now_factory())
            # claim(GETDEL): 원자적 읽기+삭제. 재시작 후 재전송된 Telegram update나
            # 멀티워커 경합에서 정확히 하나의 호출만 order를 받고 나머지는 None을 받는다.
            order = await self.pending_orders.claim(chat_id)
        except Exception as exc:
            await self._send_text_or_raise(f"주문 저장소 오류: {_short_error(exc)}")
            return
        if order is None:
            await self._send_text_or_raise("확정할 대기 주문이 없습니다.")
            return

        await self.notifier.send_chat_action("typing")
        try:
            result = await self.order_gateway.place_order(order)
        except Exception as exc:
            if isinstance(exc, HTTPException) and exc.status_code == 403:
                # 403 = 실계좌 가드 미충족: 주문 미실행이 확실하므로 대기 주문 복원.
                # created_at을 유지하므로 앱 레벨 60초 만료는 그대로 적용된다.
                # set_if_absent: 복원 도중 새 /buy가 들어온 경우 새 주문을 보호한다.
                restored = False
                try:
                    restored = await self.pending_orders.set_if_absent(chat_id, order)
                except Exception as put_exc:
                    logger.error("pending_order 복원 실패 (403 후): %s", put_exc)
                else:
                    if not restored:
                        logger.warning(
                            "403 복원 생략 — 그 사이 새 대기 주문이 생성됨: %s", chat_id
                        )
                message = f"주문 실패: {_short_error(exc)}"
                if restored:
                    # 대기 주문이 claim 이전 상태로 돌아갔다. 재실행하면 같은 403으로
                    # 같은 메시지에 도달하므로 재시도가 안전하다 — 단 만료 창이 남아 있는
                    # 동안만이다. 전송 실패 창(300초)이 주문 만료(60초)의 5배라, 60초를
                    # 넘겨 재시도하면 _drop_expired_pending_order가 먼저 지워 "확정할 대기
                    # 주문이 없습니다"가 나가고 원래 거절 사유는 유실된다. 403은 미체결이
                    # 확실해 위험하지는 않다 (PR #253 2차 리뷰).
                    await self._send_text_or_raise(message)
                else:
                    # 복원에 실패했거나 새 주문이 선점했다 — 재실행은 "확정할 대기 주문이
                    # 없습니다"로 끝나 원래 사유를 전하지 못한다 (#247).
                    await self._send_text_settled(message)
                return

            # 주문 실행 결과 불명확: claim으로 이미 삭제됨 — 추가 delete 불필요.
            # 재실행은 claim이 비어 "확정할 대기 주문이 없습니다"로 끝나므로 확인 요청이
            # 사라진다 (#247).
            await self._send_text_settled(
                "주문 실패 또는 상태 확인 필요: "
                f"{_short_error(exc)}\n"
                "중복 주문 방지를 위해 대기 주문을 제거했습니다."
            )
            return

        # 주문 성공: claim으로 이미 삭제됨 — 추가 delete 불필요.
        #
        # 여기부터가 체결 통지 outbox다 (#259 2단계). 재실행은 "확정할 대기 주문이
        # 없습니다"로 끝나므로(#247) 이 통지는 전송 실패로 사라지면 안 되는데, 그 보장을
        # 이 자리의 재시도로 만들지 않는다. _send_text_settled는 최대
        # SETTLED_SEND_TIMEOUT_SECONDS(20초) 동안 폴러 루프를 붙잡고, 그동안 같은 배치에서
        # 재시도를 기다리는 update가 예산(UPDATE_RETRY_WINDOW_SECONDS, 60초)을 통째로 잃는다.
        # 상한을 60초 아래로 내려도 닫히지 않는다 — 그러면 429를 흡수한다는 재시도의 목적이
        # 사라진다.
        #
        # 대신 순서를 기록 → 전송 → 마킹으로 둔다. 체결이 먼저 원장에 남고(notified_at =
        # null) 통지 책임이 outbox로 넘어가므로 전송은 한 번만 시도하면 된다. 실패하면
        # 미통지 행이 남고 scheduler.trade_notification_task가 다음 주기에 다시 알린다.
        # 순서를 뒤집어 전송을 먼저 하면 그 사이의 죽음이 곧 무응답이다.
        try:
            trade_id = self.trade_recorder.record(result)
        except Exception as exc:
            # 원장이 없으면 outbox도 없다 — 재배달할 근거가 남지 않는다. 그래서 이 경우에만
            # 예전처럼 그 자리에서 재시도한다. 폴러를 붙잡는 대가를 다시 무는 대신, 이
            # 메시지가 사용자가 받을 유일한 통지가 되기 때문이다.
            #
            # 기록 실패를 경고 문자열로만 덧붙이던 예전 동작은 outbox가 서는 순간 위험해진다.
            # 그때는 "기록 실패"가 곧 "통지가 조용히 사라지는 경로"인데, 그 사실이 성공
            # 메시지 뒤에 붙는 한 줄로만 드러났다.
            logger.error("체결 이력 기록 실패 — outbox 대상에서 빠진다: %s", exc)
            await self._send_text_settled(
                f"주문 완료: {result.message}"
                f"\n거래 이력 기록 실패: {_short_error(exc)}"
                "\n이 메시지가 유일한 통지입니다 — 받지 못했다면 증권사 앱에서 확인하세요."
            )
            return

        try:
            sent = await self.notifier.send_text(f"주문 완료: {result.message}")
        except Exception as exc:
            # 이 경로가 예외를 올리면 폴러가 update를 재실행하고, claim이 비어 체결된 주문이
            # "확정할 대기 주문이 없습니다"로 오표시된다 (#247). 실제 notifier는 실패를
            # False로 접어 오지만 계약을 여기서 닫는다 — _handle_one_update의 독스트링이
            # "확정 뒤의 전송은 예외를 던지지 않는다"에 기대고 있고, _send_text_settled를
            # 걷어내면서 그 보장을 대신 서 주던 자리도 함께 사라졌다.
            #
            # CancelledError는 BaseException이라 여기 걸리지 않는다. 폴러의 graceful
            # shutdown이 막히지 않고, 그 경우 행은 미통지로 남아 재시작 뒤 배달된다.
            logger.error(
                "체결 통지 전송 중 예외 — outbox 재배달 대기 (trade_id=%s): %s", trade_id, exc
            )
            return
        if sent is False:
            # 여기서 끝내도 통지가 사라지지는 않는다. notified_at이 비어 있는 행이 곧
            # 재배달 대기열이다. 전송 실패의 알람·메트릭은 #259 5단계의 몫이다.
            logger.error("체결 통지 전송 실패 — outbox 재배달 대기 (trade_id=%s)", trade_id)
            return

        try:
            self.trade_recorder.mark_notified(trade_id, notified_at=self.now_factory())
        except Exception as exc:
            # 전송은 이미 나갔다. 마킹만 실패하면 다음 주기가 같은 체결을 한 번 더 알린다 —
            # 중복이지만 무응답보다 낫고, 재배달 문구가 재전송임을 밝힌다. asyncio.timeout
            # 만료처럼 "보냈는지 모르는" 경우도 같은 결말이라 여기만의 문제가 아니다.
            logger.error("체결 통지 마킹 실패 — 중복 배달 가능 (trade_id=%s): %s", trade_id, exc)

    def _parse_order_argument(self, argument: str) -> tuple[str, int, int, OrderType] | None:
        parts = argument.split()
        if len(parts) < 2:
            return None

        last_value = self._parse_positive_int(parts[-1])
        if last_value is None:
            return None

        previous_value = self._parse_positive_int(parts[-2]) if len(parts) >= 3 else None
        if previous_value is None:
            stock_name = " ".join(parts[:-1]).strip()
            quantity = last_value
            price = 0
            order_type: OrderType = "MARKET"
        else:
            stock_name = " ".join(parts[:-2]).strip()
            quantity = previous_value
            price = last_value
            order_type = "LIMIT"

        if not stock_name:
            return None
        return stock_name, quantity, price, order_type

    def _looks_like_natural_order(self, text: str) -> bool:
        return (
            ("매수" in text or "매도" in text)
            and re.search(r"\d[\d,]*\s*주", text) is not None
        )

    def _parse_natural_order_text(self, text: str) -> tuple[OrderSide, str] | None:
        buy_count = text.count("매수")
        sell_count = text.count("매도")
        if buy_count + sell_count != 1:
            return None
        side: OrderSide = "BUY" if buy_count == 1 else "SELL"

        quantity_match = re.search(r"(?P<quantity>\d[\d,]*)\s*주", text)
        if quantity_match is None:
            return None
        quantity = self._parse_positive_int(quantity_match.group("quantity"))
        if quantity is None:
            return None

        has_market = "시장가" in text
        price_matches = list(re.finditer(r"(?P<price>\d[\d,]*)\s*원", text))
        if has_market and price_matches:
            return None
        price = 0
        if price_matches:
            price = self._parse_positive_int(price_matches[-1].group("price")) or 0
            if price <= 0:
                return None

        stock_name = text[: quantity_match.start()].strip()
        if not stock_name:
            side_word = "매수" if side == "BUY" else "매도"
            side_index = text.find(side_word)
            if side_index < quantity_match.start():
                stock_name = text[
                    side_index + len(side_word) : quantity_match.start()
                ].strip()
        if not stock_name:
            return None

        if price > 0:
            return side, f"{stock_name} {quantity} {price}"
        return side, f"{stock_name} {quantity}"

    def _parse_positive_int(self, raw_value: str) -> int | None:
        try:
            value = int(raw_value.replace(",", ""))
        except ValueError:
            return None
        return value if value > 0 else None

    async def _drop_expired_pending_order(self, chat_id: str, now: datetime) -> None:
        order = await self.pending_orders.get(chat_id)
        if order is None:
            return
        created_at = order.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=KST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=KST)
        if now.astimezone(KST) - created_at.astimezone(KST) > ORDER_EXPIRES_AFTER:
            await self.pending_orders.delete(chat_id)

    def _extract_stock_code(self, text: str) -> str | None:
        match = _STOCK_CODE_EXTRACT_RE.search(text)
        return match.group(1) if match else None

    def _extract_stock_name(self, text: str) -> str | None:
        # 규칙은 stock_code.extract_stock_name 하나가 소유한다 — order_assist도 같은
        # 것을 쓴다. 두 곳이 각자 자르면 같은 종목이 화면마다 다른 이름으로 남는다.
        return extract_stock_name(text)

    def _extract_order_callback_token(self, data: str) -> str | None:
        if data in {ORDER_CONFIRM_CALLBACK, ORDER_CANCEL_CALLBACK}:
            return None
        return self._extract_callback_token(data)

    def _extract_callback_token(self, data: str) -> str | None:
        _, separator, token = data.rpartition(":")
        if not separator:
            return None
        return token.strip() or None

    def _help_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "💰 잔고", "callback_data": BALANCE_REFRESH_CALLBACK},
                    {"text": "🔔 알림", "callback_data": f"{ALERT_CALLBACK_PREFIX}status"},
                ],
                [
                    {"text": "🧾 매매", "callback_data": f"{TRADE_CALLBACK_PREFIX}menu"},
                    {"text": "🔎 조회", "callback_data": f"{LOOKUP_CALLBACK_PREFIX}menu"},
                ],
            ]
        }

    def _level_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "🌱 처음이에요",
                        "callback_data": f"{LEVEL_CALLBACK_PREFIX}{LEVEL_BEGINNER}",
                    },
                    {
                        "text": "📈 좀 해봤어요",
                        "callback_data": f"{LEVEL_CALLBACK_PREFIX}{LEVEL_INTERMEDIATE}",
                    },
                ]
            ]
        }

    def _alerts_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🚨 긴급만", "callback_data": f"{ALERT_CALLBACK_PREFIX}urgent"},
                    {"text": "📣 전체", "callback_data": f"{ALERT_CALLBACK_PREFIX}all"},
                    {"text": "🔕 끄기", "callback_data": f"{ALERT_CALLBACK_PREFIX}off"},
                ],
                [
                    {"text": "🔎 현재 상태", "callback_data": f"{ALERT_CALLBACK_PREFIX}status"}
                ],
            ]
        }

    def _format_alert_mode(self, mode: str) -> str:
        emoji = ALERT_MODE_EMOJIS.get(mode)
        if emoji is None:
            return mode
        return f"{emoji} {mode}"

    def _format_catalyst_type(self, event_type: str) -> str:
        return {
            "earnings": "실적",
            "dividend": "배당",
            "disclosure": "공시",
            "agm": "주주총회",
        }.get(event_type, event_type or "기타")

    def _balance_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🔄 새로고침", "callback_data": BALANCE_REFRESH_CALLBACK}]
            ]
        }

    def _watch_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "📋 목록 새로고침", "callback_data": WATCH_LIST_CALLBACK}]
            ]
        }

    def _trade_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🛒 매수 입력법", "callback_data": f"{TRADE_CALLBACK_PREFIX}buy"},
                    {"text": "💸 매도 입력법", "callback_data": f"{TRADE_CALLBACK_PREFIX}sell"},
                ]
            ]
        }

    def _lookup_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "💵 현재가 입력법",
                        "callback_data": f"{LOOKUP_CALLBACK_PREFIX}quote",
                    },
                    {
                        "text": "📊 수급 입력법",
                        "callback_data": f"{LOOKUP_CALLBACK_PREFIX}trend",
                    },
                ]
            ]
        }

    def _market_reply_markup(
        self,
        action: str,
        chat_id: str,
        stock: str,
        *,
        detail_days: int = 0,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(8)
        if len(self.market_callbacks) >= MARKET_CALLBACK_LIMIT:
            self.market_callbacks.pop(next(iter(self.market_callbacks)), None)
        self.market_callbacks[token] = (chat_id, stock)
        quote_button = {
            "text": "💵 현재가 보기",
            "callback_data": f"{MARKET_QUOTE_CALLBACK}:{token}",
        }
        if action == "quote":
            return {"inline_keyboard": [[quote_button]]}
        if action == "trend_detail":
            # 수급 요약 아래에는 상세가 먼저다. 방금 받은 메시지에 이어지는 동작이라
            # 다른 종류의 조회(현재가)보다 앞에 온다 (#297 검수 4차).
            return {
                "inline_keyboard": [
                    [
                        {
                            "text": trend_detail_button_text(detail_days),
                            "callback_data": f"{MARKET_TREND_DETAIL_CALLBACK}:{token}",
                        }
                    ],
                    [quote_button],
                ]
            }
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "📊 수급 보기",
                        "callback_data": f"{MARKET_TREND_CALLBACK}:{token}",
                    }
                ]
            ]
        }

    def _order_reply_markup(self, order: PendingOrder) -> dict[str, Any]:
        # 실제 조립은 trading_orders.order_reply_markup 하나뿐이다 (#314). 스케줄러의
        # 자동 제안이 같은 버튼을 써야 해서 옮겼고, 여기서는 호출부 이름만 유지한다.
        return order_reply_markup(order)

    def _format_order_prompt(
        self,
        order: PendingOrder,
        quote_result: str,
        balance_result: str,
    ) -> str:
        side_text = "매수" if order.side == "BUY" else "매도"
        lines = [
            f"{order.stock_name} {side_text} 주문 확인",
            f"종목코드: {order.stock_code}",
            f"수량: {order.quantity:,}주",
            f"주문유형: {'시장가' if order.order_type == 'MARKET' else '지정가'}",
        ]
        if order.order_type == "LIMIT":
            amount = order.quantity * order.price
            lines.extend(
                [
                    f"지정가: {order.price:,}원",
                    f"주문금액: {amount:,}원",
                ]
            )
        elif order.price > 0:
            # 시장가에도 참고단가가 잡히면서 금액을 셀 수 있게 됐다(#309). 돈이 나가는
            # 확인 단계에서 금액만 빠져 있을 이유가 없다 — 일 거래대금 한도에 가산될
            # 값도 이것이다. 체결가가 아니라 주문 시점 현재가 기준이므로 "예상"이고,
            # 제안 경로의 승인 메시지도 같은 라벨을 쓴다 (PR #323 리뷰).
            lines.append(f"예상 주문금액: {order.quantity * order.price:,}원")

        # 기록용 참고단가와 **같은 파서**로 읽는다. 원문 줄을 그대로 집어 오면 표시와
        # 기록이 서로 다른 라벨 집합을 보게 되고("price:"는 여기서만 매치된다), 라벨이
        # 바뀌는 날 사용자는 확인 화면에서 현재가를 보는데 기록은 0("금액 모름")이 되는
        # 어긋남이 생긴다. 하나로 묶어 두면 둘이 함께 사라진다 (PR #323 리뷰).
        current_price = parse_current_price(str(quote_result))
        if current_price is not None:
            lines.append(f"현재가: {current_price:,}원")

        balance = self._first_line_containing(
            str(balance_result),
            # get_balance가 실제로 내는 라벨은 "예수금"이다(mcp-trading/balance.js).
            # 나머지는 다른 잔고 응답이 섞여 들어올 때를 위한 방어선이다 — 이 프롬프트가
            # 보여주는 것은 주문가능금액이 아니라 예수금이라는 점을 유의한다(#310).
            ("주문가능", "예수금", "총자산", "balance"),
        )
        if balance:
            # mcp-trading은 목록 항목을 "- 라벨: 값"으로 낸다. 그 줄을 그대로 붙이면
            # 직접 조립한 위 줄들(종목코드:, 수량:, 현재가: …)과 표기가 어긋난다.
            # 접두사만 떼어 제안 경로의 승인 메시지와 같은 표기로 맞춘다 (PR #323 리뷰).
            lines.append(balance.removeprefix("- "))

        # 미해석 에코(name == code)는 이제 주문 준비 단계에서 _is_unresolved_echo가
        # 끊으므로 여기까지 오지 않는다(#151). 마스터에 name == code인 항목이 생기는
        # 경우에 대비한 방어선으로만 남긴다 — 현재 마스터 4,353종에는 0건이다.
        if order.stock_name == order.stock_code:
            lines.append(UNRESOLVED_STOCK_WARNING)

        # 만료를 "60초 후"로 쓰면 메시지가 언제 도착하든 60초를 약속하게 된다. 실제로는
        # created_at이 MCP 조회 전(_handle_order_command의 now = self.now_factory())에 찍히고,
        # 전송이 429로 밀리면 _send_text_settled가 SETTLED_SEND_TIMEOUT_SECONDS까지 더 쓴다
        # — 사용자가 확인 버튼을 받는 시점엔 이미 상당히 지나 있다.
        # 절대 시각은 도착이 늦어도 어긋나지 않는다.
        expires_at = order.created_at + ORDER_EXPIRES_AFTER
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=KST)
        lines.extend(
            [
                "",
                "/confirm 입력 시 대기 주문을 확정합니다.",
                "/cancel 입력 시 대기 주문을 취소합니다.",
                f"이 주문은 {expires_at.astimezone(KST):%H:%M:%S}에 만료됩니다.",
            ]
        )
        return "\n".join(lines)

    def _first_line_containing(self, text: str, needles: tuple[str, ...]) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and any(needle in stripped for needle in needles):
                return stripped
        return None

    def _parse_quote_summary(self, raw: str) -> tuple[str, str] | None:
        price_line = self._first_line_containing(raw, ("현재가:",))
        rate_line = self._first_line_containing(raw, ("전일 대비:",))
        if not price_line or not rate_line:
            return None
        price_match = re.search(r"현재가:\s*(.+)", price_line)
        rate_match = re.search(r"\(([^)]+%)\)", rate_line)
        if not price_match or not rate_match:
            return None
        return price_match.group(1).strip(), rate_match.group(1).strip()

    async def _handle_trend(self, argument: str, chat_id: str, *, detail: bool = False) -> None:
        """수급 조회. 기본은 요약, ``detail``이면 전체 일수를 세로로 (#297 검수 4차).

        두 경로가 같은 MCP 호출을 각자 한 번씩 한다. 요약할 때 상세까지 만들어 들고 있지
        않는 이유는, 그러려면 버튼을 누를 때까지 원문을 어딘가 보관해야 하고 그 저장소가
        market_callbacks처럼 또 하나의 만료·용량 관리 대상이 되기 때문이다. 조회 버튼이
        이미 재조회로 동작하고 있어(현재가·수급 버튼) 규칙도 그쪽과 같아진다.
        """
        if not argument:
            await self._send_text_or_raise(TREND_COMMAND_HELP)
            return

        stock = argument.strip()
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "get_investor_trading",
                {"stock_name": stock},
            )
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return

        flows = _parse_investor_flows(str(result))
        if not flows:
            # 형식이 바뀌어 파싱이 깨졌다. 원문을 그대로 보내고 상세 버튼은 달지 않는다 —
            # 눌러 봐야 같은 원문이 한 번 더 갈 뿐이다 (#297 검수 2차).
            body = str(result)
            markup = self._market_reply_markup("quote", chat_id, stock)
        elif detail:
            body = _format_trend_detail(stock, flows)
            markup = self._market_reply_markup("quote", chat_id, stock)
        else:
            body = _format_trend_summary(stock, flows)
            markup = self._market_reply_markup(
                "trend_detail", chat_id, stock, detail_days=len(flows)
            )
        await self._send_text_or_raise(
            render(
                body,
                KIND_QUOTE,
                await self._current_level(),
                question=argument,
            ),
            reply_markup=markup,
        )

    async def _handle_earnings(self, argument: str, chat_id: str) -> None:
        parsed = self._parse_earnings_argument(argument)
        if parsed is None:
            await self._send_text_or_raise(EARNINGS_COMMAND_HELP)
            return

        stock, period = parsed
        # LLM 호출 전에 읽는다 — 뒤로 미루면 수십 초짜리 왕복이 끝난 다음 redis 왕복이
        # 한 번 더 붙는다 (_handle_chat_fallback과 같은 이유).
        level = await self._current_level()
        dart_arguments: dict[str, Any] = {"stock_name": stock}
        if period:
            dart_arguments["period"] = period

        await self.notifier.send_chat_action("typing")
        try:
            dart_result, news_result = await asyncio.gather(
                self.mcp_runner(DART_MCP_PARAMS, "get_earnings_report", dart_arguments),
                self.mcp_runner(NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock}),
            )
            result = await self.llm_runner(
                "nat",
                self._earnings_analysis_prompt(stock, period, str(dart_result), str(news_result)),
                conversation_id=f"telegram:{chat_id}:earnings:{_url_quote(stock, safe='')}",
            )
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return

        # LLM 호출이 끝난 뒤다 — 재실행은 DART·뉴스 조회와 LLM 호출을 그대로 반복해
        # 예산만큼 재과금된다 (#247).
        await self._send_text_settled(
            render(
                self._format_earnings_response(str(result)),
                KIND_ANALYSIS,
                level,
                question=argument,
            )
        )

    def _parse_earnings_argument(self, argument: str) -> tuple[str, str | None] | None:
        parts = argument.split()
        if not parts:
            return None

        period = None
        if re.match(r"^\d{4}(?:[-_\s]?Q[1-4]|[-_\s]?FY)$", parts[-1], flags=re.IGNORECASE):
            period = parts[-1].upper().replace("-", "").replace("_", "")
            parts = parts[:-1]

        stock = " ".join(parts).strip()
        if not stock:
            return None
        return stock, period

    def _earnings_analysis_prompt(
        self,
        stock: str,
        period: str | None,
        dart_result: str,
        news_result: str,
    ) -> str:
        period_line = f"조회 기간: {period}" if period else "조회 기간: DART MCP 기본값"
        return (
            "News Analyst 실적 분석 모드로 다음 종목의 구조화된 실적 리포트를 작성하라.\n"
            f"종목: {stock}\n"
            f"{period_line}\n\n"
            "[DART 실적 데이터]\n"
            f"{dart_result}\n\n"
            "[최신 뉴스]\n"
            f"{news_result}\n\n"
            "반드시 다음 항목을 포함하라:\n"
            "- 매출/영업이익/순이익 전년 동기 대비 증감\n"
            "- 컨센서스 대비 서프라이즈/미스 판단. 컨센서스 데이터가 없으면 추정하지 말고 데이터 없음으로 표시\n"
            "- 주요 뉴스 기반 정성적 코멘트\n"
            "- 다음 분기 전망\n"
            "첫 줄은 반드시 `호재`, `악재`, `중립` 중 하나의 판정과 한 줄 근거로 시작하라.\n"
            "Markdown 문법(`#`, `**`, 표, 코드블록)을 쓰지 말고 Telegram에서 읽기 쉬운 일반 텍스트로 답하라.\n"
            "투자 조언은 단정하지 말고, 확인된 DART·뉴스 근거와 한계를 구분해 한국어로 답하라."
        )

    def _format_earnings_response(self, text: str) -> str:
        # 출력 계층의 정리기를 쓴다. 예전에는 이 클래스가 자체 정리기(_telegram_plain_text)를
        # 갖고 있었는데, 그쪽은 머리글·굵게·글머리표만 알아서 링크·기울임·취소선·이스케이프를
        # 그대로 흘렸고 글머리표도 "•"로 바꿔 나머지 메시지와 어긋났다 (#297 자가리뷰).
        # 판정 접두어를 붙이려면 정리된 첫 줄을 봐야 해서 render보다 먼저 한 번 부른다 —
        # sanitize_markdown은 이미 정리된 문장에 다시 걸어도 같은 결과다.
        plain = sanitize_markdown(text)
        verdict = self._earnings_verdict(plain)
        if plain.startswith(("🟢 호재", "🔴 악재", "⚪ 중립")):
            return plain
        lines = plain.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines:
            first = lines[0].strip()
            for label in ("호재", "악재", "중립"):
                if first == label:
                    lines = lines[1:]
                    while lines and not lines[0].strip():
                        lines.pop(0)
                    body = "\n".join(lines).strip()
                    return f"{verdict}\n{body}".strip()
                if first.startswith(f"{label}:") or first.startswith(f"{label} -"):
                    lines[0] = first.replace(label, verdict, 1)
                    return "\n".join(lines).strip()
        return f"{verdict}\n{plain}".strip()

    def _earnings_verdict(self, text: str) -> str:
        if "악재" in text:
            return "🔴 악재"
        if "호재" in text:
            return "🟢 호재"
        return "⚪ 중립"

    async def _send_progress_message(self, text: str) -> int | None:
        """진행 메시지를 보내고 나중에 치울 message_id를 반환한다 (#260).

        message_id를 돌려주지 못하는 notifier에서는 일반 전송만 하고 ``None``을
        반환한다 — 이 경우 진행 메시지는 대화에 그대로 남는다.

        전송 실패는 여기서 삼킨다. ``TelegramNotifier``의 ``send_text_returning_id``/
        ``send_text``는 이미 내부에서 예외를 잡아 각각 ``None``·무시로 떨어뜨리지만,
        notifier는 생성자로 주입 가능하므로 대역이 예외를 던질 수 있다. 진행 표시는
        답변에 덧붙는 편의 기능이라 이걸로 질의 처리 자체를 실패시키면 사용자는 답도
        못 받는다 — 그래서 주입된 구현이 무엇이든 여기서 한 겹 감싼다.
        """
        try:
            # notifier는 주입 가능하고 이 능력은 선택 사항이라, 선언 타입이 아니라
            # 런타임 존재 여부로 판정한다. 선언 타입에 없는 속성이라 getattr의 결과는
            # object로 추론되는데, 그러면 callable() 통과 뒤에도 await가 막힌다.
            send_returning_id: Callable[..., Any] | None = getattr(
                self.notifier, "send_text_returning_id", None
            )
            if callable(send_returning_id):
                return await send_returning_id(text)
            await self.notifier.send_text(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("진행 메시지를 보내지 못했습니다: %s", exc)
        return None

    async def _clear_progress_message(self, message_id: int | None) -> None:
        """진행 메시지를 치운다 — 삭제하고, 삭제가 거부되면 종료 표시로 편집한다 (#260).

        최종 답변을 이 메시지의 **편집으로 내보내지 않는 이유**: 텔레그램은 메시지
        편집에 대해 푸시 알림을 보내지 않고 읽지 않음 표시도 갱신하지 않는다. 편집으로
        답을 내보내면 수십 초를 기다리다 앱을 닫은 사용자가 정작 답변 도착 알림을 받지
        못한다 — 체감 응답성을 높이려는 기능이 알림 가치를 뒤집는 셈이다.
        답변은 항상 새 메시지로 보낸다.

        실패는 삼킨다. 진행 메시지 정리는 답변 전달보다 덜 중요하다.
        """
        if message_id is None:
            return

        # notifier는 주입 가능하고 이 능력은 선택 사항이라, 선언 타입이 아니라
        # 런타임 존재 여부로 판정한다. 선언 타입에 없는 속성이라 getattr의 결과는
        # object로 추론되는데, 그러면 callable() 통과 뒤에도 await가 막힌다.
        delete_message: Callable[..., Any] | None = getattr(
            self.notifier, "delete_message", None
        )
        if callable(delete_message) and await delete_message(message_id):
            return

        edit_message_text: Callable[..., Any] | None = getattr(
            self.notifier, "edit_message_text", None
        )
        if callable(edit_message_text) and await edit_message_text(message_id, PROGRESS_DONE_MESSAGE):
            return

        logger.info("진행 메시지를 정리하지 못했습니다 (message_id=%s)", message_id)

    async def _handle_chat_fallback(self, text: str, chat_id: str) -> None:
        await self.notifier.send_chat_action("typing")
        # #260: NAT 응답까지 수십 초가 걸린다. typing 액션은 5초 뒤 사라지므로
        # 접수 즉시 진행 메시지를 남기고, 응답이 오면 그 메시지를 치운 뒤 답변을
        # 새 메시지로 보낸다(편집은 푸시 알림을 발생시키지 않는다).
        progress_message_id = await self._send_progress_message(NAT_PROGRESS_MESSAGE)
        # NAT 호출 전에 읽는다. 호출 뒤로 미루면 수십 초짜리 LLM 왕복이 끝난 다음에 redis
        # 왕복이 한 번 더 붙어 진행 메시지가 그만큼 더 오래 남는다.
        level = await self._current_level()
        try:
            result = await self.llm_runner(
                "nat",
                text,
                conversation_id=f"telegram:{chat_id}",
            )
        except Exception as exc:
            # 통지보다 먼저 치운다 — 아래 전송이 실패해 재시도로 넘어가도
            # "분석 중"이 채팅에 남지 않는다 (#260).
            await self._clear_progress_message(progress_message_id)
            await self._send_text_or_raise(f"응답 생성 실패: {_short_error(exc)}")
            return
        # LLM 호출이 끝난 뒤다. 재실행은 같은 conversation_id로 NAT를 다시 호출해 대화
        # 이력을 오염시키고 예산만큼 재과금된다 — 전송 실패 예산으로는 최대 10회다 (#247).
        await self._clear_progress_message(progress_message_id)
        # _nat_answer_message(→ presentation.render)는 길이를 보지 않고 전체 문장을
        # 조립하고, _send_text_settled가 상한에 맞춰 나눠 보낸다 (#260, #297, #313).
        # 각주는 마지막 조각에 통째로 실린다 — render 독스트링 참조.
        await self._send_text_settled(_nat_answer_message(result, level, text))

    @asynccontextmanager
    async def _state(self):
        state = self.state_factory()
        if hasattr(state, "__aenter__"):
            async with state as opened:
                yield opened
        else:
            yield state


def _update_sort_key(update: dict[str, Any]) -> tuple[int, int]:
    """update_id 오름차순 정렬 키. update_id가 없는 update는 맨 앞으로 보낸다 (#241).

    #259 1단계로 "poison 뒤 update 선행 실행"은 되돌렸지만 이 정렬은 남는다. 배치를 poison에서
    끊는 것은 미해결 update를 그보다 뒤의 update보다 먼저 만난다는 전제 위에서만 offset을
    지켜주기 때문이다 — 역순 배치라면 뒤쪽 update가 먼저 성공해 offset이 poison을 지나가고,
    그러면 그 poison은 재배달 대상에서 조용히 사라진다.

    update_id가 없는 update는 재시도 예산도 offset도 추적할 수 없어 실패해도 그 자리에서
    끝난다(= 배치를 끊지 않는다). 그래서 앞에 두어도 뒤에 오는 update의 재시도 판정에
    영향을 주지 않는다. 다만 offset이 그걸 지나갈 수 없어 재배달될 때마다 다시 실행되고,
    배치가 끊기지 않아 sleep도 없으므로 getUpdates busy loop가 된다. 실제 Telegram은 항상
    update_id를 주므로 이론적 경로다 (PR #242 리뷰).
    """
    update_id = update.get("update_id")
    if isinstance(update_id, int):
        return (1, update_id)
    return (0, 0)


class _UpdateOutcome(Enum):
    """_handle_one_update의 결과 (#241).

    offset을 전진시켜도 되는지(DONE/SKIPPED)와 스킵 통지가 필요한지(SKIPPED)를 한 값으로
    구분한다. 평문 str이면 오타가 조용히 통과하므로 Enum으로 강제한다 (PR #242 리뷰).
    """

    DONE = "done"
    RETRY = "retry"
    SKIPPED = "skipped"


def _update_chat_id(update: dict[str, Any]) -> str:
    callback_query = update.get("callback_query") or {}
    message = update.get("message") or callback_query.get("message") or {}
    return str((message.get("chat") or {}).get("id", "")).strip()


def _update_label(update: dict[str, Any]) -> str:
    """실패 통지에 붙일 요약. 사용자가 무엇을 다시 보내야 하는지 알 수 있게 한다 (#241)."""
    callback_query = update.get("callback_query") or {}
    message = update.get("message") or callback_query.get("message") or {}
    text = (message.get("text") or "").strip()
    if not text:
        text = str(callback_query.get("data") or "").strip()
    if not text:
        return f"update {update.get('update_id')}"
    if len(text) > UPDATE_LABEL_LIMIT:
        return text[:UPDATE_LABEL_LIMIT] + "…"
    return text


@dataclass
class _UpdateFailure:
    """update별 재시도 예산. 기준은 횟수가 아니라 첫 실패 이후 흐른 시간이다 (#241).

    시계가 둘인 이유는 예산이 재시작을 넘겨야 하기 때문이다 (#350). ``first_at``은
    monotonic이라 경과 계산이 벽시계 조정에 흔들리지 않지만 프로세스가 죽으면 원점이
    사라지고, ``first_wall_at``은 그 반대다. 그래서 진행 중인 판정은 monotonic으로 하고
    영속화·복원 경계에서만 벽시계를 쓴다 — 복원은 벽시계 경과만큼 뒤로 민 monotonic 값을
    ``first_at``에 넣어, 그 뒤의 코드가 계속 한 시계만 보게 한다.

    ``first_wall_at``은 복원해도 원래 값을 그대로 유지한다. 복원 시점으로 새로 찍으면
    재시작마다 앵커가 앞당겨져, 잦은 재시작이 다시 예산을 무한히 늘리는 #350의 경로가
    벽시계 쪽으로 되살아난다.
    """

    first_at: float
    attempts: int
    send_failure: bool
    first_wall_at: float


class TelegramCommandPoller:
    def __init__(
        self,
        *,
        notifier: TelegramPollerNotifier = telegram_notifier,
        handler: TelegramCommandHandler | None = None,
        state_store: TelegramPollerStore | None = None,
    ):
        self.notifier = notifier
        if handler is None:
            handler = TelegramCommandHandler(
                notifier=notifier,
                order_gateway=_create_order_gateway(),
                trade_recorder=_create_trade_recorder(),
                pending_order_store=_create_pending_order_store(),
            )
        self.handler = handler
        # offset은 redis에 영속화된다 (#248). 인메모리였을 때는 재시작이 offset을 처음으로
        # 되감아, 배치 중간까지 실행한 update가 통째로 재배달·재실행됐다.
        #
        # #248이 offset과 함께 저장하던 _handled_ahead는 #259 1단계에서 사라졌다. 그 집합은
        # "poison 뒤 update를 먼저 실행한다"는 #241의 최적화가 만들어낸 중복 실행 위험을 막는
        # 장치였는데, 최적화 자체를 되돌려 poison에서 배치를 끊게 하면서 기억해야 할 선행
        # 실행이 없어졌다. 남는 창은 handle_update 한 번의 실행 시간뿐이라 offset만 저장한다
        # (#270 → #259).
        self.state_store: TelegramPollerStore = (
            state_store if state_store is not None else _create_poller_state_store()
        )
        self.offset: int | None = None
        # update_id -> 재시도 예산. offset과 함께 영속화된다 (#350).
        #
        # #248은 이 값을 일부러 뺐다. 근거는 "유실돼도 poison 폐기가 미뤄질 뿐 중복 실행으로
        # 이어지지 않는다"였는데, #259 1단계가 배치를 poison에서 끊게 만들면서 그 대가가
        # **그동안 그 채팅의 모든 명령이 정지**로 커졌다. #241 하에서는 뒤의 update가 선행
        # 실행돼 "폐기가 미뤄짐"에 그쳤다. 리셋 계기는 배포가 아니라 Dockerfile의
        # uvicorn --reload + bind mount라 backend 파일 저장 하나면 되고, 예산(일반 65초·
        # 전송 실패 335초)보다 잦은 재시작이 이어지면 poison은 영원히 폐기되지 않았다.
        #
        # 방향이 offset과 반대라는 점은 그대로다: offset은 "자신을 poison에 붙들어 매는
        # 상태"이고 이건 "poison을 포기하게 해주는 유일한 상태"다 (PR #251 리뷰). 그래서
        # 유실 시의 degrade 방향도 반대다 — offset이 없으면 재실행, 이게 없으면 정지.
        # 복원 실패를 저장소가 offset과 다르게 다루는 근거가 그것이다
        # (RedisTelegramPollerStore._deserialize_failures).
        self._failures: dict[int, _UpdateFailure] = {}

    async def run(self) -> None:
        if not self.notifier.enabled:
            return
        await self._setup_bot_profile()
        await self._restore_state()

        while True:
            try:
                updates = await self._get_updates()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # getUpdates 자체의 네트워크 오류는 기존대로 5초 후 재시도한다.
                logger.error("Telegram command polling failed: %s", exc)
                await self._sleep(UPDATE_RETRY_BACKOFF_SECONDS[0])
                continue

            # 재시도 대기 중인 update를 만나면 배치를 그 자리에서 끊는다. #241은 반대로
            # 뒤의 update를 계속 실행해 head-of-line blocking을 없앴는데, 그 선행 실행분은
            # offset이 확정하지 못한 채 재배달되므로 "무엇을 이미 실행했는지"를 기억하는
            # 상태(_handled_ahead)가 딸려왔다. 그 상태의 유실은 곧 주문 부수효과의 중복
            # 실행이고, 그것을 막을 장치는 상태 자신뿐이었다. 단일 채팅·단일 프로세스인 이
            # 봇에서 선행 실행이 사는 값은 "내 다음 명령이 최대 335초(전송 실패 예산) 늦게
            # 실행되는 것"을 피하는 정도라, 중복 실행 위험과 맞바꾸지 않는다 (#270 → #259).
            #
            # 끊는 것은 미해결 update를 그보다 뒤의 update보다 먼저 만난다는 전제 위에서만
            # offset을 지켜준다. getUpdates가 오름차순을 보장한다는 외부 전제에 정확성을
            # 걸지 않도록 여기서 직접 정렬해 전제를 없앤다 (#241).
            updates = sorted(updates, key=_update_sort_key)
            retry_pending = False
            skipped: list[dict[str, Any]] = []
            for update in updates:
                update_id = update.get("update_id")
                outcome = await self._handle_one_update(update, update_id)
                if outcome is _UpdateOutcome.RETRY:
                    retry_pending = True
                    break
                if outcome is _UpdateOutcome.SKIPPED:
                    skipped.append(update)
                if not isinstance(update_id, int):
                    continue

                self._failures.pop(update_id, None)
                self.offset = update_id + 1
                self._forget_passed_updates(self.offset)
                # 배치 끝이 아니라 update마다 쓴다. 배치 단위로 미루면 중간에 죽었을 때
                # 이미 실행한 update의 기록이 통째로 사라져 영속화의 의미가 없다 (#248).
                #
                # 남는 창은 handle_update의 실행 시간이다. #253이 _send_text_settled로
                # "부수효과 확정 뒤의 전송"을 그 자리에서 재시도하게 만들면서 이 시간이
                # 밀리초에서 십수 초로 늘어났었다. 그 구간에 SIGTERM이 오면 체결은 됐는데
                # persist 전이라, 재시작 후 재배달·재실행에서 claim이 None을 돌려주고
                # 사용자는 "확정할 대기 주문이 없습니다"를 받는다 — #253이 없애려던 그
                # 오표시다.
                #
                # #259 2단계가 이 창을 **줄였다**. 체결 통지가 outbox로 나가면서 /confirm
                # 성공 경로의 전송은 재시도 없는 한 번이 됐고, 창은 최대 20초에서 send_text
                # 한 번(httpx 타임아웃 10초)으로 내려왔다. 그리고 성격이 바뀌었다: 그 창에서
                # 죽어도 체결은 이미 원장에 미통지로 남아 있어 다음 주기가 통지를 배달한다.
                # 남는 피해는 재실행이 내보내는 "확정할 대기 주문이 없습니다" 한 줄이다.
                #
                # 창 자체는 아직 열려 있다. #269가 그 사실을 회귀 테스트로 고정했고
                # (test_confirmed_order_is_reexecuted_when_restart_lands_in_the_settled_send),
                # 닫는 작업은 #293(= #259 3단계)이다.
                await self._persist_state()

            if skipped:
                # 배치 통지는 한 건으로 합친다. poison N건에 N번 발송하면 채팅당 초당 ~1건
                # 제한에 걸려 429를 만들고, 429는 다시 전송 실패 = 새 poison이 된다 (PR #242 리뷰).
                #
                # #259 1단계 이후 이 목록의 원소는 실제로는 최대 1개다 — 예산은 시도해야 쌓이고
                # 시도는 첫 RETRY에서 끊기므로, 한 배치에서 예산을 소진할 수 있는 update는
                # 맨 앞의 것 하나뿐이다. 합치는 코드를 남겨두는 이유는 그 상한이 배치 루프의
                # 모양에 딸린 성질이라서다: 루프가 다시 바뀌면 N건이 되살아나는데, 그때
                # 통지가 429를 만드는 경로는 여기서만 막힌다. 상한 자체는
                # test_skip_notice_merges_updates_into_one_message가 helper 단위로 고정한다.
                await self._notify_updates_skipped(skipped)
            if retry_pending:
                # 예산을 여기서 쓴다 (#350). 위 루프의 쓰기 지점은 update를 통과시킨 뒤에만
                # 도는데, RETRY는 배치를 그 자리에서 끊어 거기 닿지 못한다 — 이 한 줄이
                # 없으면 예산은 영속화 대상이 되고도 실제로는 한 번도 저장되지 않는다.
                #
                # 이 쓰기는 재시도 사이클당 한 번(간격 5~45초)이라 정상 경로의 쓰기 빈도를
                # 바꾸지 않는다. offset은 그대로고 바뀌는 것은 attempts뿐이지만, 상태를 통째로
                # 덮어쓰는 계약이라 부분 갱신을 따로 만들 이유가 없다.
                await self._persist_state()
                # 실패한 update를 곧바로 다시 받아 예산을 순식간에 소진하지 않도록 쉬어 간다.
                # 실패가 이어지면 간격을 늘려 복구 창을 벌고 rate limit도 덜 자극한다 (#241).
                await self._sleep(self._retry_delay())

    async def _handle_one_update(
        self,
        update: dict[str, Any],
        update_id: Any,
    ) -> _UpdateOutcome:
        """update 처리 결과를 반환한다: 완료 / 재시도 대기 / 예산 소진 후 스킵 (#241).

        재시도는 handle_update가 멱등하다는 전제 위에 있고, 그 전제는 핸들러가 지킨다:
        부수효과가 확정된 뒤의 전송은 예외를 던지지 않으므로 여기까지 오지 않는다. 즉
        재실행되는 것은 부수효과 이전 구간뿐이다 (#247). 그 전송이 실패했을 때 무엇으로
        되살리는지는 경로마다 다르다 — /confirm 체결 성공은 원장에 남은 미통지 행과
        scheduler.trade_notification_task가 받고(#259 2단계), 나머지 settled 경로는
        _send_text_settled의 인플레이스 재시도가 전부다.

        반대로 부수효과 이전의 전송 실패는 변환 경로(except Exception)에 삼켜지지 않고
        반드시 여기 도달한다 — 전송을 본문에 둔 try는 TelegramSendError를 재던져야 하고,
        test_every_try_containing_a_retryable_send_reraises_it이 그것을 정적으로 강제한다
        (#249). 그 가드는 직접 호출만 보므로, 전송을 감싼 헬퍼를 try 안에서 부르는 코드가
        생기면 이 전제가 조용히 깨진다.

        자연어 경로(_handle_chat_fallback)에는 사용자에게 보이는 부수효과가 하나 더
        있다 — 진행 메시지다 (#260). 답변 전송은 _send_text_settled라 여기 도달하지
        않지만, LLM 실패 통지는 _send_text_or_raise이므로 그 전송이 실패하면 재시도가
        걸리고 진행 메시지가 매번 새로 나간다. 진행 메시지 전송 실패는 notifier가 삼켜
        예산에도 잡히지 않으므로 같은 채팅의 rate limit을 추가로 소모한다. 예산을 손볼
        때 함께 보라 (PR #263 리뷰, #275).
        """
        try:
            await self.handler.handle_update(update)
            return _UpdateOutcome.DONE
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not isinstance(update_id, int):
                # update_id가 없으면 예산도 offset도 추적할 수 없다. 붙잡아 둘 방법이
                # 없으므로 로그만 남기고 넘어간다.
                logger.error("Telegram update handling failed without update_id: %s", exc)
                return _UpdateOutcome.DONE

            send_failure = isinstance(exc, TelegramSendError)
            now = self._now()
            failure = self._failures.get(update_id)
            if failure is None:
                failure = _UpdateFailure(
                    first_at=now,
                    attempts=1,
                    send_failure=send_failure,
                    first_wall_at=self._wall_now(),
                )
                self._failures[update_id] = failure
            else:
                failure.attempts += 1
                # 한 번이라도 전송 실패가 있었으면 긴 창을 유지한다. first_at은 첫 실패에
                # 고정인데 창을 마지막 예외 종류로 매번 다시 고르면 두 값의 기준이 어긋난다:
                # 429가 290초 이어지다 마지막에 redis 오류가 한 번 나면 창이 60초로 줄어
                # 그 오류에 재시도가 0회 주어진 채 즉시 폐기된다. 종류가 바뀔 때마다
                # first_at을 리셋하면 종류를 번갈아 던지는 update가 영원히 살아남으므로,
                # 상한이 유계인 이 방식을 택했다 (PR #242 리뷰).
                failure.send_failure = failure.send_failure or send_failure

            elapsed = now - failure.first_at
            window = (
                SEND_FAILURE_RETRY_WINDOW_SECONDS
                if failure.send_failure
                else UPDATE_RETRY_WINDOW_SECONDS
            )
            if elapsed <= window:
                logger.error(
                    "Telegram update %s handling failed (attempt %s, %.0fs/%.0fs): %s",
                    update_id,
                    failure.attempts,
                    elapsed,
                    window,
                    exc,
                )
                return _UpdateOutcome.RETRY

            logger.error(
                "Telegram update %s skipped after %s attempts over %.0fs: %s",
                update_id,
                failure.attempts,
                elapsed,
                exc,
            )
            return _UpdateOutcome.SKIPPED

    def _retry_delay(self) -> float:
        """미해결 update 중 가장 적게 시도한 것을 기준으로 백오프 간격을 고른다 (#241).

        간격은 배치 전체에 하나뿐이라 가장 급한 쪽에 맞춰야 한다. 최대 시도 수를 쓰면
        갓 실패한 update가 오래된 poison의 45초 간격을 물려받아 자기 창(60초) 안에
        시도 횟수를 손해 본다 (PR #242 리뷰).
        """
        attempts = min((f.attempts for f in self._failures.values()), default=1)
        index = min(attempts, len(UPDATE_RETRY_BACKOFF_SECONDS)) - 1
        return UPDATE_RETRY_BACKOFF_SECONDS[index]

    async def _notify_updates_skipped(self, updates: list[dict[str, Any]]) -> None:
        # run()의 배치 루프는 try 밖이라 여기서 새는 예외는 폴러 태스크를 죽인다.
        # 통지문 조립(임의의 update dict를 파싱한다)까지 try 안에 둔다 (PR #242 리뷰).
        try:
            # notifier는 자기 chat에만 보낼 수 있으므로 다른 chat이면 통지할 방법이 없다.
            mine = [u for u in updates if _update_chat_id(u) == self.notifier.chat_id]
            if not mine:
                return
            labels = ", ".join(_update_label(u) for u in mine)
            sent = await self.notifier.send_text(
                f"{UPDATE_SKIPPED_NOTICE}\n실패한 요청: {labels}"
            )
        except Exception as exc:
            # 통지 실패가 폴러를 다시 막으면 안 된다. send_text는 예외를 삼키므로 여기
            # except는 duck-typed notifier 방어용이다 (#241).
            logger.error("Telegram skip notice send failed: %s", exc)
            return
        if sent is False:
            # send_text는 실패해도 예외 대신 False를 돌려준다. 유일한 실패 신호라
            # 반드시 확인해야 통지 실패가 어디에도 안 남는 일이 없다 (PR #242 리뷰).
            logger.error("Telegram skip notice not delivered for %s updates", len(mine))

    async def _restore_state(self) -> None:
        """재시작 전 offset과 재시도 예산을 복원한다 (#248, #350).

        실패하면 빈 상태로 시작한다. Telegram이 미확정 update를 전부 재배달하므로 명령이
        유실되지는 않지만, 실행됐던 것이 다시 실행된다 — 영속화 이전과 같은 상태다.
        여기서 예외를 올리면 redis 장애가 폴러 태스크의 죽음이 되므로 삼킨다.

        wait_for가 필요한 이유는 STATE_STORE_TIMEOUT_SECONDS 주석에 있다. 여기서는 hang이
        폴링 시작 자체를 막아, 봇이 아무 응답도 못 하는 상태로 무한정 머문다.
        """
        try:
            state = await asyncio.wait_for(
                self.state_store.load(), timeout=STATE_STORE_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # asyncio.TimeoutError는 3.11+에서 내장 TimeoutError(OSError 하위)라 여기 걸린다.
            # CancelledError는 BaseException이라 걸리지 않고 정상 전파된다.
            logger.error("Telegram poller 상태 복원 실패, 빈 상태로 시작: %s", exc)
            return
        self.offset = state.offset
        self._failures = self._restore_failures(state.failures)
        if state.offset is not None:
            logger.info("Telegram poller 상태 복원: offset=%s", state.offset)
        for update_id, failure in self._failures.items():
            # 재시작을 넘긴 예산은 그 자체로 조사 대상이다 — 이 로그가 없으면 "재시작 직후
            # 첫 실패에서 바로 폐기됐다"가 원인 없는 사건으로 보인다.
            logger.info(
                "Telegram poller 재시도 예산 복원: update=%s attempts=%s elapsed=%.0fs",
                update_id,
                failure.attempts,
                self._now() - failure.first_at,
            )

    def _restore_failures(
        self, failures: tuple[TelegramPollerFailure, ...]
    ) -> dict[int, _UpdateFailure]:
        """저장된 벽시계 앵커를 이 프로세스의 monotonic 기준으로 환산한다 (#350).

        경과가 음수면 0으로 접는다. 벽시계가 뒤로 뛴 상태라 그 update가 실제로 얼마나
        기다렸는지 알 방법이 없고, 이 방향의 오차는 "예산을 한 번 더 준다"(= #350 이전
        동작)로 떨어져 정지를 새로 만들지 않는다.

        경과가 창을 이미 넘겼어도 여기서 폐기하지는 않는다. 폐기 판정은 _handle_one_update의
        한 곳에 남겨 둔다 — 그 update가 재배달돼 다시 실패해야 폐기가 의미를 갖고, 재시작
        사이에 원인이 사라졌다면 그냥 성공하고 예산은 pop된다.
        """
        wall_now = self._wall_now()
        now = self._now()
        restored: dict[int, _UpdateFailure] = {}
        for failure in failures:
            elapsed = max(0.0, wall_now - failure.first_at)
            restored[failure.update_id] = _UpdateFailure(
                first_at=now - elapsed,
                attempts=failure.attempts,
                send_failure=failure.send_failure,
                first_wall_at=failure.first_at,
            )
        return restored

    async def _persist_state(self) -> None:
        """offset과 재시도 예산을 저장한다 (#248, #350).

        쓰기 실패는 삼킨다 — fail-open 근거는 RedisTelegramPollerStore 독스트링에 있다.
        폴링은 인메모리 상태로 계속되고, 다음 update의 쓰기가 성공하면 그 시점 상태가
        통째로 반영되므로 실패가 누적되지 않는다(전체 상태를 매번 덮어쓰는 덕분이다).

        wait_for가 없으면 이 삼킴이 무의미해진다 — 근거는 STATE_STORE_TIMEOUT_SECONDS 주석.
        여기 남는 로그가 "영속화가 조용히 죽었다"를 알리는 유일한 신호다. 이게 반복되면
        중복 창이 #248 이전 수준으로 돌아간 것이므로 경보 대상으로 삼을 만하다.
        """
        try:
            await asyncio.wait_for(
                self.state_store.save(
                    TelegramPollerState(
                        offset=self.offset, failures=self._failures_snapshot()
                    )
                ),
                timeout=STATE_STORE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # TimeoutError·CancelledError 취급은 _restore_state 주석 참조.
            logger.error("Telegram poller 상태 저장 실패, 인메모리로 계속: %s", exc)

    def _failures_snapshot(self) -> tuple[TelegramPollerFailure, ...]:
        """인메모리 예산을 저장 형식으로 옮긴다 — 나가는 시각은 벽시계다 (#350).

        update_id 순으로 정렬하는 이유는 payload를 재현 가능하게 만들기 위해서다. dict의
        삽입 순서가 그대로 나가면 같은 상태가 서로 다른 문자열이 되어, 저장된 값을 눈으로
        비교하거나 테스트가 payload를 고정하기 어려워진다.
        """
        return tuple(
            TelegramPollerFailure(
                update_id=update_id,
                first_at=failure.first_wall_at,
                attempts=failure.attempts,
                send_failure=failure.send_failure,
            )
            for update_id, failure in sorted(self._failures.items())
        )

    def _forget_passed_updates(self, offset: int) -> None:
        """offset이 지나간 update_id 기록을 정리해 추적 상태가 무한히 커지지 않게 한다 (#241).

        #259 1단계 이후 이 정리는 도달 가능한 모든 상태에서 no-op이다. _failures에 항목을
        새로 넣는 것은 배치를 끊는 RETRY 하나뿐이고, 그 항목은 offset이 지나가지 못한 가장
        작은 id라 다음 배치의 맨 앞으로 정렬된다 — 거기서 pop되거나 다시 배치를 끊으므로,
        유일한 호출부(그 pop 바로 다음 줄)가 보는 dict는 항상 비어 있다.

        그래도 남기는 이유는 skip 통지 병합(_notify_updates_skipped 호출부 주석)과 같다.
        "항목이 최대 1개"는 배치 루프의 모양에 딸린 성질이라 루프가 다시 바뀌면 무한 증가가
        되살아나는데, 그것을 막는 코드는 여기뿐이다. 정리 자체는
        test_forget_passed_updates_drops_only_what_the_offset_passed가 helper 단위로 고정한다.
        """
        self._failures = {
            update_id: failure
            for update_id, failure in self._failures.items()
            if update_id >= offset
        }

    async def _sleep(self, seconds: float) -> None:
        """테스트가 전역 asyncio.sleep 대신 이 인스턴스만 대체할 수 있게 하는 간접층 (#241).

        전역 패치는 핸들러 내부의 실제 sleep(_handle_watch의 조회 간격)까지 가로채
        루프가 엉뚱한 지점에서 끊긴 채 테스트가 통과할 수 있다 (PR #242 리뷰).
        """
        await asyncio.sleep(seconds)

    def _now(self) -> float:
        """재시도 예산용 단조 시계. 테스트에서 대체할 수 있도록 메서드로 둔다 (#241)."""
        return time.monotonic()

    def _wall_now(self) -> float:
        """예산을 재시작 너머로 나르기 위한 벽시계 (#350).

        _now와 갈라 두는 이유는 _UpdateFailure 독스트링에 있다. 여기 값은 영속화 payload와
        복원 시 경과 계산에만 쓰이고, 살아 있는 프로세스 안의 판정에는 쓰이지 않는다.
        """
        return time.time()

    async def _setup_bot_profile(self) -> None:
        # notifier는 주입 가능하고 이 능력은 선택 사항이라, 선언 타입이 아니라
        # 런타임 존재 여부로 판정한다. 선언 타입에 없는 속성이라 getattr의 결과는
        # object로 추론되는데, 그러면 callable() 통과 뒤에도 await가 막힌다.
        load_bot_username: Callable[..., Any] | None = getattr(
            self.notifier, "load_bot_username", None
        )
        if callable(load_bot_username):
            try:
                await load_bot_username()
            except Exception as exc:
                logger.error("Telegram bot username setup failed: %s", exc)

        set_bot_commands: Callable[..., Any] | None = getattr(
            self.notifier, "set_bot_commands", None
        )
        if callable(set_bot_commands):
            try:
                await set_bot_commands(TELEGRAM_BOT_COMMANDS)
            except Exception as exc:
                logger.error("Telegram bot command menu setup failed: %s", exc)

    async def _get_updates(self) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 25, "limit": GET_UPDATES_LIMIT}
        if self.offset is not None:
            payload["offset"] = self.offset

        # URL을 직접 만들지 않는다. 여기서 만들면 raise_for_status의 예외에 토큰이 실려
        # 아래 run()의 "polling failed" 로그로 흘러간다 — 실제로 그랬다 (PR #253 2차 리뷰).
        # fetch_telegram_api가 URL 없는 TelegramApiError로 바꿔 던진다 (#257).
        body = await fetch_telegram_api(
            self.notifier.bot_token, "getUpdates", payload=payload, timeout=30.0
        )
        if body.get("ok") is not True:
            return []
        return body.get("result") or []


def start_telegram_commands() -> None:
    global _telegram_command_task
    if not telegram_notifier.enabled or _telegram_command_task is not None:
        return
    poller = TelegramCommandPoller()
    _telegram_command_task = asyncio.create_task(poller.run())


async def stop_telegram_commands() -> None:
    global _telegram_command_task
    if _telegram_command_task is None:
        return
    _telegram_command_task.cancel()
    try:
        await _telegram_command_task
    except asyncio.CancelledError:
        pass
    finally:
        _telegram_command_task = None
