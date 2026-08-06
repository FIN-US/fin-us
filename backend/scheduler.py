import os
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Callable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session
from .catalyst_repo import CatalystEventInput, SqliteCatalystEventRepo
from .ws_manager import manager
from .database import engine
from .config import NEWS_MCP_PARAMS, TRADING_MCP_PARAMS, DART_MCP_PARAMS
from .redis_state import RedisSchedulerState, signal_hash, redis_state
from .services import (
    perform_stock_analysis,
    run_mcp_tool,
    check_signal_significance,
    generate_morning_briefing,
)
from .watchlist_repo import SqliteWatchlistRepo
from .telegram_notifier import telegram_notifier
from .telegram_notifier import should_send_telegram_alert

logger = logging.getLogger(__name__)

# 비동기 스케줄러 인스턴스 생성
KST = ZoneInfo("Asia/Seoul")
scheduler = AsyncIOScheduler(timezone=KST)

# 뉴스 필터링에 사용할 모델 제공자 (ollama 또는 openai)
FILTER_PROVIDER = os.environ.get("NEWS_FILTER_PROVIDER", "ollama")

# 종목별 마지막으로 분석된 signal 내용을 저장하는 인메모리 캐시 (LLM 호출 최적화용)
# Redis 장애 시에만 사용하는 프로세스 로컬 fallback입니다.
last_analyzed_signal_cache = {}
last_analyzed_news_cache = last_analyzed_signal_cache

DEFAULT_MONITOR_STOCKS = [
    "삼성전자",
    "SK하이닉스",
    "현대차",
    "NAVER"
]


@dataclass(frozen=True)
class SignalSource:
    name: str
    mcp_params: Any
    tool_name: str
    stock_arg_name: str = "stock_name"


SIGNAL_SOURCES = [
    SignalSource(
        name="news",
        mcp_params=NEWS_MCP_PARAMS,
        tool_name="get_market_news",
    ),
    SignalSource(
        name="disclosure",
        mcp_params=DART_MCP_PARAMS,
        tool_name="get_disclosure_signal",
    ),
]

CATALYST_EVENT_LABELS = {
    "earnings": "실적",
    "dividend": "배당",
    "disclosure": "공시",
    "agm": "주주총회",
}


# get_balance 연속 실패 횟수. 감시 잡이 10분 주기로 상시 도는 탓에, 장애가 지속되면
# 동일한 에러 로그가 무한 반복돼 알림 피로를 낳고 정작 중요한 장애를 묻는다(#185).
# 이 카운터로 "장애당 1회"만 error를 남기고 복구 시 누적 횟수를 집계해 보고한다.
# 잡이 단일 이벤트 루프에서 순차 실행되고(다중 worker는 Redis 스케줄러 lock으로
# 이미 배제) 정확도가 로그 억제에만 쓰이므로 프로세스 로컬 정수로 충분하다.
_balance_failure_streak = 0


def _default_watchlist_repo() -> SqliteWatchlistRepo:
    return SqliteWatchlistRepo(lambda: Session(engine))


def _default_catalyst_repo() -> SqliteCatalystEventRepo:
    return SqliteCatalystEventRepo(lambda: Session(engine))

# "- 종목명 (코드) ..." 형식의 잔고 종목 줄을 검증하는 정규식.
# 줄에 "(코드)" 그룹이 없는 경우(예: "- 삼성전자 · 3주")를 계약 위반으로 거부한다.
# greedy .+가 마지막 " (코드)" 직전까지를 이름으로 잡으므로 이전의 rsplit("(", 1)과
# 동일하게 다중 괄호 이름(CJ4우(전환), 룩셈부르크코어오피스(파생형)(A))을 올바르게
# 처리한다. 단, 코드 자리 검증은 아래 문자 클래스로 rsplit보다 엄격해 "(합성)" 같은
# 비코드 괄호를 코드로 인정하지 않는 점에서 rsplit과 의도적으로 다르다(아래 설명 참고).
# ^- 가 "- " 접두사를 소비하므로 종목명 중간의 "- "(한국 - 전력)는 보존된다.
# "(코드)" 뒤의 구분자(· 또는 :)는 매칭 대상에 포함하지 않아 포맷 변형에 유연하게
# 대응한다.
#
# 코드 클래스를 [0-9A-Z]{6,}으로 한정하는 이유:
#   - services.py의 _STOCK_CODE_EXTRACT_RE, telegram_commands.py와 동일한 컨벤션.
#   - 하한 6: KIS 표준 6자리 코드(005930 등). 상한 없음({6,}): ETN 7자리(389종목),
#     펀드 9자리(75종목 예: F70102B96)까지 포함.
#   - [^()]*를 쓰면 두 가지 문제가 생긴다:
#     1) 빈 코드 통과 — "- 삼성전자 () · 3주"의 빈 괄호가 코드 자리에 매치돼
#        (name="삼성전자", code="") 무효 줄이 살아남는다.
#     2) KODEX 오염 — "- KODEX 200 (합성) · 3주"에서 "(합성)"이 코드 자리에 들어가
#        이름이 "KODEX 200"으로 잘려 이슈 #157이 막으려던 오염이 재현된다.
STOCK_LINE_RE = re.compile(r"^- (?P<name>.+) \((?P<code>[0-9A-Z]{6,})\)")


def extract_stocks_from_balance(balance_text: str) -> list[str]:
    """mcp-trading의 get_balance 결과에서 종목명 리스트를 추출합니다.

    [보유 종목 리스트] 섹션 이후 "- "로 시작하는 줄 중 STOCK_LINE_RE에 매치되는
    줄에서만 종목명을 추출합니다. 매치되지 않는 줄(코드 괄호 누락 등 계약 위반)은
    호출당 1회 집계 경고 로그를 남기고 건너뜁니다(줄마다 로그를 남기면 템플릿
    변경 시 종목 수만큼 WARNING이 반복 발생한다).

    mcp-trading/balance.js의 formatTruncationNote가 그 섹션 뒤에 덧붙이는 잘림
    안내 문구는 "- "로 시작하지 않으므로, 정규식 매치 이전에 startswith("- ")
    가드와 정규식 거부 중 어느 쪽이든 걸러집니다.
    """
    stocks: list[str] = []
    malformed: list[str] = []
    if "[보유 종목 리스트]" in balance_text:
        lines = balance_text.split("[보유 종목 리스트]")[1].strip().split("\n")
        for line in lines:
            if line.startswith("- "):
                # "- 종목명 (코드) · 수량주" 형식을 정규식으로 앵커링해 검증한다.
                # 이 파서는 mcp-trading/balance.js 의 formatBalanceReport() 출력 형식에 의존한다.
                # 종목 한 건은 3줄 블록(헤더 줄 + 공백 2칸 들여쓰기 2줄)이며, 들여쓴 줄은
                # "- " 로 시작하지 않으므로 이 조건에 걸리지 않는다.
                #
                # STOCK_LINE_RE의 greedy .+는 다음 두 가지를 의도적으로 처리한다.
                # 1) 종목코드는 항상 줄의 마지막 "(코드)" 그룹에 있으므로 greedy 매칭으로
                #    마지막 " (코드) · " 직전까지를 이름으로 잡는다. split("(")[0]을 쓰면
                #    종목명 자체에 괄호가 있을 때 첫 "(" 에서 잘려 오인식한다.
                #    예: "CJ4우(전환) (00104K) · 1주" → name 그룹 "CJ4우(전환)"
                #    (split만 쓰면 "CJ4우"로 잘못 잘림). mcp-trading/data/stocks.json 기준
                #    종목명 4,353개 중 348개가 "(" 를 포함하므로 드문 경우가 아니다.
                # 2) ^- 가 "- " 접두사를 소비하므로 종목명 중간의 "- " 가 있어도
                #    name 그룹에 그대로 보존된다. 예: "- 한국 - 전력 (015760) · 1주"는
                #    name "한국 - 전력"(정상). stocks.json 기준으로는 "- "를 포함하는
                #    종목명이 0건이지만, prdt_name은 마스터가 아닌 KIS output1 응답에서
                #    오므로 완전히 배제할 수는 없다.
                #
                # 매치 실패(코드 괄호 누락 등 계약 위반 줄)는 malformed에 모아 호출 후
                # 1회 집계 경고로 남긴다.
                match = STOCK_LINE_RE.match(line)
                if match is None:
                    malformed.append(line)
                    continue
                name = match.group("name").strip()
                if name:
                    stocks.append(name)
    if malformed:
        logger.warning(
            "예상치 못한 잔고 종목 줄 형식 %d건을 건너뛰었습니다(예시 최대 3건): %r",
            len(malformed), malformed[:3],
        )
    return stocks


# mcp-trading/balance.js의 formatTruncationNote()가 잘림 시 항상 덧붙이는 문구 중,
# truncated 사유(max_pages/time_budget/no_cursor/repeated_cursor/error, 그리고
# 목록에 없는 사유의 fallback "연속조회가 완료되지 않아")에 관계없이 고정으로 남는
# 부분만 매칭한다:
#   `\n\n[안내] ${reason} 조회가 중단되어 일부 보유 종목이 위 목록에서 ...`
# ${reason}만 사유별로 바뀌고 앞뒤 리터럴은 고정이므로, 사유 문구 자체(예: "페이지
# 상한")를 매칭 대상에 넣지 않아도 모든 현재·미래 사유를 잡는다. 사유별 문구까지
# 매칭하면 새 사유가 추가될 때마다 이 목록도 함께 고쳐야 해서 더 잘 깨진다.
#
# 판별은 "조회가 중단되어" 하나로만 한다. "[안내]"는 mcp-trading/index.js:570의
# 모의투자 실현손익 안내와도 공유하는 표지라 get_balance 밖에서도 등장할 수 있어
# 판별력이 없고, 실제 discriminator는 잘림 문구에만 존재하는 이 접미사다. 고정할
# 리터럴이 하나로 줄어 아래 결합 테스트도 이 한 줄만 지키면 된다.
#
# get_balance는 MCP 텍스트 응답만 backend로 넘어오고 truncated 값 자체(구조화된
# 필드)는 MCP 경계를 넘어오지 않으므로, 이 함수는 문자열 매칭에 의존할 수밖에
# 없다. mcp-trading/balance.js의 리터럴이 바뀌면 이 매칭도 함께 깨진다 — 그
# 결합을 mcp-trading/tests/order.test.js의 잘림 노트 테스트가 `조회가 중단되어`를
# 직접 단언해 고정하고, backend/tests/test_balance_parser.py가 픽스처로 재현한다.
_BALANCE_TRUNCATION_MARKER = "조회가 중단되어"


def is_balance_truncated(balance_text: str) -> bool:
    """get_balance 응답에 mcp-trading/balance.js의 잘림 안내 문구가 포함되어 있는지 확인합니다.

    extract_stocks_from_balance()는 이 문구를 의도적으로 무시하므로(안내 문구가
    종목명으로 오인식되지 않도록), 잘림 여부는 이 함수로 별도 확인해야 한다.
    """
    return _BALANCE_TRUNCATION_MARKER in balance_text


def _infer_catalyst_event_type(description: str) -> str:
    text = description.lower()
    if any(keyword in description for keyword in ("실적", "분기보고서", "반기보고서", "사업보고서")):
        return "earnings"
    if "배당" in description:
        return "dividend"
    if any(keyword in description for keyword in ("주주총회", "주총")):
        return "agm"
    if any(keyword in text for keyword in ("major stock", "executive")):
        return "disclosure"
    return "disclosure"


def _parse_catalyst_date(raw: str) -> date | None:
    compact_match = re.search(r"\b(20\d{2})(\d{2})(\d{2})\b", raw)
    if compact_match is not None:
        year, month, day = compact_match.groups()
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            return None

    dashed_match = re.search(r"\b(20\d{2})[-.](\d{1,2})[-.](\d{1,2})\b", raw)
    if dashed_match is None:
        return None
    year, month, day = dashed_match.groups()
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _parse_disclosure_signal_events(stock_name: str, raw_signal: str) -> list[CatalystEventInput]:
    events: list[CatalystEventInput] = []
    for line in str(raw_signal).splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue

        event_date = _parse_catalyst_date(stripped)
        if event_date is None:
            continue

        parts = [part.strip(" -") for part in stripped.split("|")]
        description = next(
            (part for part in parts if part and _parse_catalyst_date(part) is None),
            stripped.lstrip("-").strip(),
        )
        events.append(
            CatalystEventInput(
                stock_name=stock_name,
                event_type=_infer_catalyst_event_type(description),
                event_date=event_date,
                description=description,
                source="dart",
            )
        )
    return events


def _format_catalyst_alert(event: Any) -> str:
    d_day = "D-Day" if event.days_until_event == 0 else f"D-{event.days_until_event}"
    label = CATALYST_EVENT_LABELS.get(event.event_type, event.event_type or "기타")
    return "\n".join(
        [
            f"📅 촉매 이벤트 알림 ({d_day})",
            f"- 종목: {event.stock_name}",
            f"- 날짜: {event.event_date.isoformat()}",
            f"- 유형: {label}",
            f"- 내용: {event.description}",
        ]
    )


async def _collect_catalyst_events(
    watchlist: list[str],
    catalyst_repo: SqliteCatalystEventRepo,
) -> None:
    for stock in watchlist:
        try:
            signal = await run_mcp_tool(
                DART_MCP_PARAMS,
                "get_disclosure_signal",
                {"stock_name": stock},
            )
            events = _parse_disclosure_signal_events(stock, str(signal))
            if events:
                await catalyst_repo.upsert_events(events)
        except Exception as e:
            logger.error("[%s] 촉매 이벤트 수집 중 오류: %s", stock, e)


async def _send_due_catalyst_alerts(
    watchlist: list[str],
    catalyst_repo: SqliteCatalystEventRepo,
    *,
    notifier: Any,
    today: date,
) -> None:
    try:
        due_events = await catalyst_repo.list_due_for_notification(watchlist, today=today)
    except Exception as e:
        logger.error("촉매 이벤트 알림 대상 조회 중 오류: %s", e)
        return

    for event in due_events:
        try:
            sent = await notifier.send_text(_format_catalyst_alert(event))
            if sent is True:
                await catalyst_repo.mark_notification_sent(
                    event.id,
                    days_until_event=event.days_until_event,
                )
        except Exception as e:
            logger.error("[%s] 촉매 이벤트 Telegram 알림 중 오류: %s", event.stock_name, e)


async def catalyst_calendar_task(
    *,
    watchlist_repo: SqliteWatchlistRepo | None = None,
    catalyst_repo: SqliteCatalystEventRepo | None = None,
    notifier: Any = telegram_notifier,
    today_factory: Callable[[], date] | None = None,
    use_redis_lock: bool = True,
) -> None:
    if use_redis_lock:
        try:
            async with redis_state() as state:
                scheduler_token = await state.acquire_scheduler_lock("catalyst_calendar")
                if scheduler_token is None:
                    logger.info("다른 worker가 촉매 이벤트 캘린더를 실행 중입니다. 이번 실행을 건너뜁니다.")
                    return
                try:
                    await catalyst_calendar_task(
                        watchlist_repo=watchlist_repo,
                        catalyst_repo=catalyst_repo,
                        notifier=notifier,
                        today_factory=today_factory,
                        use_redis_lock=False,
                    )
                finally:
                    await state.release_lock(
                        state.keys.scheduler_lock("catalyst_calendar"),
                        scheduler_token,
                    )
        except Exception as e:
            logger.error("Redis 기반 촉매 이벤트 캘린더 실행 중 오류: %s", e)
        return

    if watchlist_repo is None:
        watchlist_repo = _default_watchlist_repo()
    if catalyst_repo is None:
        catalyst_repo = _default_catalyst_repo()
    today = today_factory() if today_factory is not None else datetime.now(scheduler.timezone).date()

    try:
        watchlist = await watchlist_repo.get_watchlist()
    except Exception as e:
        logger.error("촉매 이벤트 관심 종목 조회 중 오류: %s", e)
        return

    if not watchlist:
        logger.info("관심 종목이 없어 촉매 이벤트 캘린더를 건너뜁니다.")
        return

    await _collect_catalyst_events(watchlist, catalyst_repo)
    await _send_due_catalyst_alerts(
        watchlist,
        catalyst_repo,
        notifier=notifier,
        today=today,
    )

async def monitor_market_task(watchlist_repo: SqliteWatchlistRepo | None = None):
    """
    주기적으로 시장 상황을 모니터링합니다.
    """
    fallback_to_memory = True
    try:
        async with redis_state() as state:
            scheduler_token = await state.acquire_scheduler_lock()
            if scheduler_token is None:
                logger.info("다른 worker가 시장 모니터링을 실행 중입니다. 이번 실행을 건너뜁니다.")
                return

            fallback_to_memory = False
            try:
                await _monitor_market_task(state, watchlist_repo)
            finally:
                try:
                    await state.release_lock(state.keys.scheduler_lock("market_monitoring"), scheduler_token)
                except Exception as e:
                    logger.error("시장 모니터링 Redis lock 해제 중 오류: %s", e)
    except Exception as e:
        if not fallback_to_memory:
            logger.error("Redis 기반 시장 모니터링 실행 중 오류: %s", e)
            return

        logger.warning(
            "Redis 스케줄러 상태를 사용할 수 없어 인메모리 fallback으로 시장 모니터링을 실행합니다: %s",
            e,
        )
        await _monitor_market_task(None, watchlist_repo)


async def _monitor_market_task(
    state: RedisSchedulerState | None,
    watchlist_repo: SqliteWatchlistRepo | None = None,
):
    global _balance_failure_streak

    try:
        # 1. 실시간 잔고 조회 및 모니터링 대상 확정
        #
        # get_balance만 자체 try/except로 격리하는 이유(#185): 아래에서 도는
        # _monitor_signal의 신호는 SIGNAL_SOURCES(mcp-news/mcp-dart)에서 오고 KIS와
        # 아무 관련이 없는데, 이 호출이 태스크 전체 try의 첫 문장이라 KIS 장애 한 번이
        # 뉴스·공시 감시까지 통째로 정지시켰다. 관심 종목 조회(아래)가 이미 쓰는
        # fail-open 관용구를 그대로 적용해, 잔고를 못 읽으면 owned_stocks만 비우고
        # 관심 종목·기본 종목 감시는 계속 진행한다. 장애 중 보유 종목 감시 공백은
        # 어차피 조회 자체가 불가능하므로 불가피한 대가다.
        owned_stocks: list[str] = []
        try:
            balance_text = await run_mcp_tool(TRADING_MCP_PARAMS, "get_balance", {})
        except Exception as e:
            _balance_failure_streak += 1
            if _balance_failure_streak == 1:
                logger.error(
                    "잔고 조회에 실패해 이번 주기의 보유 종목 감시를 건너뜁니다"
                    "(관심 종목·기본 종목 감시는 계속): %s",
                    e,
                )
            else:
                # 10분 주기 잡이라 장애가 지속되면 같은 error가 무한 반복돼 알림 피로를
                # 낳고 진짜 장애를 묻는다. 장애당 첫 실패만 error로 남기고 이후는
                # debug로 내린 뒤, 복구 시점에 누적 횟수를 집계해 한 번에 보고한다.
                logger.debug(
                    "잔고 조회 실패가 %d회 연속됩니다: %s", _balance_failure_streak, e
                )
        else:
            if _balance_failure_streak:
                logger.warning(
                    "잔고 조회가 복구되었습니다. 직전까지 %d회 연속 실패해 보유 종목 감시를 건너뛰었습니다.",
                    _balance_failure_streak,
                )
                _balance_failure_streak = 0

            owned_stocks = extract_stocks_from_balance(balance_text)

            if is_balance_truncated(balance_text):
                # 잘림 사유(max_pages/time_budget/error/...)마다 운영 대응이 다르므로,
                # 안내 문구 줄을 그대로 실어 사유가 로그에 남게 한다. 사유 문자열을 따로
                # 파싱하지 않으므로 balance.js에 새 사유가 추가돼도 자동으로 따라간다.
                notice = next(
                    (
                        line
                        for line in balance_text.splitlines()
                        if _BALANCE_TRUNCATION_MARKER in line
                    ),
                    "",
                )
                logger.warning(
                    "잔고 연속조회가 잘려 감시 대상이 불완전할 수 있습니다: 보유 종목 %d건만 확보 — %s",
                    len(owned_stocks),
                    notice.strip(),
                )

        if watchlist_repo is None:
            watchlist_repo = SqliteWatchlistRepo(lambda: Session(engine))
        try:
            watchlist = await watchlist_repo.get_watchlist()
        except Exception as e:
            logger.error("관심 종목 조회 중 오류: %s", e)
            watchlist = []

        # 보유 종목 + 관심 종목 합산 (순서 유지, 중복 제거)
        seen: set[str] = set()
        stocks_to_monitor: list[str] = []
        for stock in owned_stocks + watchlist:
            if stock not in seen:
                seen.add(stock)
                stocks_to_monitor.append(stock)

        if not stocks_to_monitor:
            logger.info("보유 종목 및 관심 종목이 없습니다. 기본 종목을 감시합니다.")
            stocks_to_monitor = DEFAULT_MONITOR_STOCKS

        with Session(engine) as session:
            for stock in stocks_to_monitor:
                for source in SIGNAL_SOURCES:
                    await _monitor_signal(stock, source, session, state)
                    
    except Exception as e:
        logger.error(f"모니터링 태스크 시작 중 오류: {e}")


async def _send_telegram_alert_if_needed(
    stock: str,
    source: str,
    analysis_data: dict[str, Any],
    state: RedisSchedulerState | None,
) -> None:
    try:
        alert_mode = await state.get_telegram_alert_mode() if state is not None else "urgent"
        if not should_send_telegram_alert(analysis_data, alert_mode=alert_mode):
            return
        await telegram_notifier.send_analysis_alert(
            stock,
            source,
            analysis_data,
            alert_mode=alert_mode,
        )
    except Exception as e:
        logger.error("[%s:%s] Telegram 알림 처리 중 오류: %s", source, stock, e)


async def _monitor_signal(
    stock: str,
    source: SignalSource,
    session: Session,
    state: RedisSchedulerState | None,
):
    analysis_token = None
    try:
        # 1. 최신 signal 수집
        current_signal = await run_mcp_tool(
            source.mcp_params,
            source.tool_name,
            {source.stock_arg_name: stock},
        )

        current_digest = signal_hash(current_signal)
        if state is None:
            cache_key = (source.name, stock)
            last_signal = last_analyzed_signal_cache.get(cache_key)
            if last_signal is not None and signal_hash(last_signal) == current_digest:
                logger.info(f"[{source.name}:{stock}] 동일 signal입니다. 분석을 건너뜁니다.")
                return
        else:
            if await state.in_cooldown(source.name, stock):
                logger.info(f"[{source.name}:{stock}] 분석 cooldown 중입니다. 이번 실행을 건너뜁니다.")
                return

            last_digest = await state.get_last_signal_hash(source.name, stock)
            if last_digest == current_digest:
                logger.info(f"[{source.name}:{stock}] 동일 signal입니다. 분석을 건너뜁니다.")
                return

            analysis_token = await state.acquire_analysis_lock(source.name, stock)
            if analysis_token is None:
                logger.info(f"[{source.name}:{stock}] 다른 worker가 분석 중입니다. 이번 실행을 건너뜁니다.")
                return

            last_signal = await state.get_last_signal_text(source.name, stock)

        # 2. 유의미성 판단 (Local Model or Mini LLM API)
        is_significant = await check_signal_significance(
            stock,
            current_signal,
            last_signal,
            source=source.name,
            provider=FILTER_PROVIDER,
        )

        if not is_significant:
            await _set_last_signal_state(state, source.name, stock, current_signal, current_digest)
            logger.info(f"[{source.name}:{stock}] 유의미한 변화 없음. 분석을 건너뜁니다.")
            return

        # 3. 유의미한 변화가 있을 때만 고성능 에이전트 분석 실행
        logger.info(f"[{source.name}:{stock}] 유의미한 변화 감지! 상세 분석을 시작합니다.")
        analysis_data = await perform_stock_analysis(
            stock,
            "nat",
            session,
            trigger_source=source.name,
            trigger_signal=current_signal,
        )
        await _set_last_signal_state(state, source.name, stock, current_signal, current_digest)

        await _send_telegram_alert_if_needed(stock, source.name, analysis_data, state)

        # 분석 결과를 WebSocket으로 실시간 전송
        await manager.broadcast({
            "type": "AGENT_ANALYSIS",
            "stock": stock,
            "source": source.name,
            "data": analysis_data,
            "reason": "significant_change_detected"
        })
        logger.info(f"[{source.name}:{stock}] 분석 결과 브로드캐스트 완료")

    except Exception as e:
        if state is not None and analysis_token is not None:
            await state.set_cooldown(source.name, stock, type(e).__name__)
        logger.error(f"[{source.name}:{stock}] 개별 종목 모니터링 중 오류: {e}")
    finally:
        if state is not None and analysis_token is not None:
            try:
                await state.release_lock(state.keys.analysis_lock(source.name, stock), analysis_token)
            except Exception as e:
                logger.error(f"[{source.name}:{stock}] Redis analysis lock 해제 중 오류: {e}")


async def _set_last_signal_state(
    state: RedisSchedulerState | None,
    source: str,
    stock: str,
    signal_text: str,
    digest: str,
) -> None:
    if state is None:
        last_analyzed_signal_cache[(source, stock)] = signal_text
    else:
        await state.set_last_signal(source, stock, signal_text, digest)

async def ping_task():
    """
    스케줄러와 WebSocket이 정상 작동하는지 확인하기 위한 테스트 작업입니다.
    60초마다 모든 클라이언트에게 핑을 보냅니다.
    """
    logger.info("Background task: sending periodic ping via WebSocket")
    await manager.broadcast({
        "type": "SYSTEM_PING",
        "message": "Scheduler is alive and monitoring...",
        "status": "active"
    })


async def morning_briefing_task(watchlist_repo: SqliteWatchlistRepo | None = None):
    try:
        if watchlist_repo is None:
            watchlist_repo = SqliteWatchlistRepo(lambda: Session(engine))
        try:
            watchlist = await watchlist_repo.get_watchlist()
        except Exception as e:
            logger.error("모닝 브리핑 관심 종목 조회 중 오류: %s", e)
            watchlist = []

        briefing = await generate_morning_briefing(watchlist)
        message = telegram_notifier.format_morning_briefing(briefing)
        await telegram_notifier.send_text(message)
    except Exception as e:
        logger.error("모닝 브리핑 작업 중 오류: %s", e)


def start_scheduler():
    """스케줄러를 시작하고 작업을 등록합니다."""
    if not scheduler.running:
        # 60초마다 실행되는 테스트 작업 추가
        scheduler.add_job(ping_task, "interval", seconds=60, id="periodic_ping")
        
        # 10분마다 시장 모니터링 수행 (뉴스 기반 필터링이 있으므로 주기를 짧게 조정 가능)
        scheduler.add_job(
            monitor_market_task,
            "interval",
            minutes=10,
            id="market_monitoring",
            next_run_time=datetime.now(scheduler.timezone),
        )

        scheduler.add_job(
            catalyst_calendar_task,
            "cron",
            hour=8,
            minute=30,
            id="catalyst_calendar",
        )
        scheduler.add_job(
            morning_briefing_task,
            "cron",
            day_of_week="mon-fri",
            hour=8,
            minute=30,
            id="morning_briefing",
        )
        
        scheduler.start()
        logger.info("APScheduler started with optimized monitoring tasks.")

def stop_scheduler():
    """스케줄러를 종료합니다."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")
