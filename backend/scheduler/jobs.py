"""APScheduler 작업 등록과 단순 주기 작업(핑·모닝 브리핑·신호 기록 정리)."""

import logging
from datetime import datetime

from sqlmodel import Session

from backend.ai.analysis import generate_morning_briefing
from backend.core.config import FILTERED_SIGNAL_RETENTION_DAYS
from backend.core.database import engine
from backend.core.ws_manager import manager
from backend.repositories.filtered_signal import FilteredSignalRecorder
from backend.repositories.watchlist import WatchlistReader
from backend.scheduler import deps
from backend.scheduler.catalyst import _telegram_user_level, catalyst_calendar_task
from backend.scheduler.monitor import monitor_market_task
from backend.scheduler.trade_notification import (
    trade_notification_task,
    TRADE_NOTIFY_INTERVAL_SECONDS,
)
from backend.telegram.notifier import telegram_notifier
from backend.telegram.presentation import KIND_BRIEFING, render

logger = logging.getLogger(__name__)


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


async def morning_briefing_task(
    watchlist_repo: WatchlistReader | None = None,
    *,
    level: str | None = None,
):
    """모닝 브리핑. ``level``을 주지 않으면 저장된 설명 수준을 직접 읽는다.

    cron으로 등록될 때는 인자가 없으므로 여기서 읽어야 한다 — 기본값을 그대로 쓰면 /level
    설정이 브리핑에만 적용되지 않는다 (#297 자가리뷰).
    """
    try:
        if level is None:
            level = await _telegram_user_level()
        if watchlist_repo is None:
            watchlist_repo = deps.SqliteWatchlistRepo(lambda: Session(engine))
        try:
            watchlist = await watchlist_repo.get_watchlist()
        except Exception as e:
            logger.error("모닝 브리핑 관심 종목 조회 중 오류: %s", e)
            watchlist = []

        briefing = await generate_morning_briefing(watchlist)
        message = telegram_notifier.format_morning_briefing(briefing)
        await telegram_notifier.send_text(render(message, KIND_BRIEFING, level))
    except Exception as e:
        logger.error("모닝 브리핑 작업 중 오류: %s", e)


async def purge_filtered_signals_task(
    filtered_signal_repo: FilteredSignalRecorder | None = None,
) -> None:
    """보존 기간이 지난 걸러진 신호 기록을 정리한다 (#304).

    감시 루프가 종목·소스마다 10분 주기로 돌아 행이 빠르게 쌓이므로, 보존 정책을
    두지 않으면 이 테이블이 SQLite 파일을 조용히 불린다. 하루 한 번 잘라 낸다.

    monitor_market_task와 달리 Redis 스케줄러 락을 잡지 않는다. 워커가 둘 다 같은
    조건으로 지우면 늦게 도착한 쪽이 0건을 지울 뿐이라 락으로 지킬 상태가 없다.
    """
    repo = filtered_signal_repo or deps._default_filtered_signal_repo()
    try:
        deleted = await repo.purge_expired(FILTERED_SIGNAL_RETENTION_DAYS)
    except Exception as e:
        # 정리 실패는 다음 주기에 다시 시도된다. 재시도하면 같은 조건으로 지우므로
        # 이번에 못 지운 행도 함께 정리된다.
        logger.error(
            "걸러진 신호 기록 정리 실패 (보존 %d일): %s",
            FILTERED_SIGNAL_RETENTION_DAYS,
            e,
        )
        return
    logger.info(
        "걸러진 신호 기록 정리 완료: %d건 삭제 (보존 %d일)",
        deleted,
        FILTERED_SIGNAL_RETENTION_DAYS,
    )


def start_scheduler():
    """스케줄러를 시작하고 작업을 등록합니다."""
    if not deps.scheduler.running:
        # 60초마다 실행되는 테스트 작업 추가
        deps.scheduler.add_job(ping_task, "interval", seconds=60, id="periodic_ping")
        
        # 10분마다 시장 모니터링 수행 (뉴스 기반 필터링이 있으므로 주기를 짧게 조정 가능)
        deps.scheduler.add_job(
            monitor_market_task,
            "interval",
            minutes=10,
            id="market_monitoring",
            next_run_time=datetime.now(deps.scheduler.timezone),
        )

        deps.scheduler.add_job(
            catalyst_calendar_task,
            "cron",
            hour=8,
            minute=30,
            id="catalyst_calendar",
        )
        deps.scheduler.add_job(
            morning_briefing_task,
            "cron",
            day_of_week="mon-fri",
            hour=8,
            minute=30,
            id="morning_briefing",
        )
        # 체결 통지 outbox (#259 2단계). 대상은 전송이 실패했을 때만 생기므로 평소에는
        # 빈 조회 한 번으로 끝난다. 주기를 늘리면 그만큼 사용자가 "주문이 나갔나?"를
        # 모르는 시간이 늘어난다.
        deps.scheduler.add_job(
            trade_notification_task,
            "interval",
            seconds=TRADE_NOTIFY_INTERVAL_SECONDS,
            id="trade_notification",
        )
        # 걸러진 신호 기록 정리 (#304). 장 시작 전 한산한 시각에 돌려 감시 주기와
        # DB 접근이 겹치지 않게 한다.
        deps.scheduler.add_job(
            purge_filtered_signals_task,
            "cron",
            hour=4,
            minute=10,
            id="purge_filtered_signals",
        )
        
        deps.scheduler.start()
        logger.info("APScheduler started with optimized monitoring tasks.")

def stop_scheduler():
    """스케줄러를 종료합니다."""
    if deps.scheduler.running:
        deps.scheduler.shutdown()
        logger.info("APScheduler stopped.")
