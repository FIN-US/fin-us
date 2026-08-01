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


def _default_watchlist_repo() -> SqliteWatchlistRepo:
    return SqliteWatchlistRepo(lambda: Session(engine))


def _default_catalyst_repo() -> SqliteCatalystEventRepo:
    return SqliteCatalystEventRepo(lambda: Session(engine))

def extract_stocks_from_balance(balance_text: str) -> list[str]:
    """mcp-trading의 get_balance 결과에서 종목명 리스트를 추출합니다."""
    stocks = []
    if "[보유 종목 리스트]" in balance_text:
        lines = balance_text.split("[보유 종목 리스트]")[1].strip().split("\n")
        for line in lines:
            if line.startswith("- "):
                # "- 종목명 (코드) · 수량주" 형식(첫 줄만)에서 종목명 추출.
                # 이 파서는 mcp-trading/balance.js 의 formatBalanceReport() 출력 형식에 의존한다.
                # 종목 한 건은 3줄 블록(헤더 줄 + 공백 2칸 들여쓰기 2줄)이며, 들여쓴 줄은
                # "- " 로 시작하지 않으므로 이 조건에 걸리지 않는다.
                name = line.split("(")[0].replace("- ", "").strip()
                if name:
                    stocks.append(name)
    return stocks


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
    try:
        # 1. 실시간 잔고 조회 및 모니터링 대상 확정
        balance_text = await run_mcp_tool(TRADING_MCP_PARAMS, "get_balance", {})
        owned_stocks = extract_stocks_from_balance(balance_text)

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
