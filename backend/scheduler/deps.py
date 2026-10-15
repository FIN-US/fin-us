"""스케줄러 작업들이 함께 부르는 외부 의존성과 APScheduler 인스턴스.

여러 작업 모듈이 같은 이름을 쓰므로 한 곳에 둔다. 테스트는 이 모듈의 이름을 바꿔 끼운다
(예: ``backend.scheduler.deps.run_mcp_tool``) — 작업 모듈마다 따로 import하면 한쪽만
바뀌어 바꿔치기가 조용히 일부만 먹는다.
"""


from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session

from backend.ai.services import run_mcp_tool
from backend.core.database import engine
from backend.core.redis_state import redis_state
from backend.core.timeutil import KST
from backend.repositories.catalyst import SqliteCatalystEventRepo
from backend.repositories.filtered_signal import SqliteFilteredSignalRepo
from backend.repositories.watchlist import SqliteWatchlistRepo


# 비동기 스케줄러 인스턴스 생성
scheduler = AsyncIOScheduler(timezone=KST)


def _default_watchlist_repo() -> SqliteWatchlistRepo:
    return SqliteWatchlistRepo(lambda: Session(engine))


def _default_catalyst_repo() -> SqliteCatalystEventRepo:
    return SqliteCatalystEventRepo(lambda: Session(engine))


def _default_filtered_signal_repo() -> SqliteFilteredSignalRepo:
    return SqliteFilteredSignalRepo(lambda: Session(engine))
