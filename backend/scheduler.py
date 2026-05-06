import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from .ws_manager import manager

logger = logging.getLogger(__name__)

# 비동기 스케줄러 인스턴스 생성
scheduler = AsyncIOScheduler()

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

def start_scheduler():
    """스케줄러를 시작하고 작업을 등록합니다."""
    if not scheduler.running:
        # 60초마다 실행되는 테스트 작업 추가
        scheduler.add_job(ping_task, "interval", seconds=60, id="periodic_ping")
        scheduler.start()
        logger.info("APScheduler started.")

def stop_scheduler():
    """스케줄러를 종료합니다."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")
