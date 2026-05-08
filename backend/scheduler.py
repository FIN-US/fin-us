import os
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session
from .ws_manager import manager
from .database import engine
from .config import NEWS_MCP_PARAMS, TRADING_MCP_PARAMS
from .redis_state import RedisSchedulerState, news_hash, redis_state
from .services import perform_stock_analysis, run_mcp_tool, check_news_significance

logger = logging.getLogger(__name__)

# 비동기 스케줄러 인스턴스 생성
scheduler = AsyncIOScheduler()

# 뉴스 필터링에 사용할 모델 제공자 (ollama 또는 openai)
FILTER_PROVIDER = os.environ.get("NEWS_FILTER_PROVIDER", "ollama")

# 종목별 마지막으로 분석된 뉴스 내용을 저장하는 인메모리 캐시 (LLM 호출 최적화용)
# Redis 장애 시에만 사용하는 프로세스 로컬 fallback입니다.
last_analyzed_news_cache = {}

def extract_stocks_from_balance(balance_text: str) -> list[str]:
    """mcp-trading의 get_balance 결과에서 종목명 리스트를 추출합니다."""
    stocks = []
    if "[보유 종목 리스트]" in balance_text:
        lines = balance_text.split("[보유 종목 리스트]")[1].strip().split("\n")
        for line in lines:
            if line.startswith("- "):
                # "- 종목명 (코드): ..." 형식에서 종목명 추출
                name = line.split("(")[0].replace("- ", "").strip()
                if name:
                    stocks.append(name)
    return stocks

async def monitor_market_task():
    """
    주기적으로 시장 상황을 모니터링합니다.
    mcp-trading에서 실시간 잔고를 가져와 보유 종목들을 대상으로 감시를 수행합니다.
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
                await _monitor_market_task(state)
            finally:
                try:
                    await state.release_lock(state.keys.scheduler_lock(), scheduler_token)
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
        await _monitor_market_task(None)


async def _monitor_market_task(state: RedisSchedulerState | None):
    try:
        # 1. 실시간 잔고 조회 및 모니터링 대상 확정
        balance_text = await run_mcp_tool(TRADING_MCP_PARAMS, "get_balance", {})
        stocks_to_monitor = extract_stocks_from_balance(balance_text)

        if not stocks_to_monitor:
            logger.info("보유 종목이 없습니다. 기본 종목을 감시합니다.")
            stocks_to_monitor = ["삼성전자"] # 기본 감시 종목

        logger.info(f"실시간 모니터링 시작 (대상: {stocks_to_monitor}, 필터: {FILTER_PROVIDER})")
        
        with Session(engine) as session:
            for stock in stocks_to_monitor:
                await _monitor_stock(stock, session, state)
                    
    except Exception as e:
        logger.error(f"모니터링 태스크 시작 중 오류: {e}")


async def _monitor_stock(stock: str, session: Session, state: RedisSchedulerState | None):
    analysis_token = None
    try:
        # 1. 최신 뉴스 수집
        current_news = await run_mcp_tool(NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock})

        current_digest = news_hash(current_news)
        if state is None:
            last_news = last_analyzed_news_cache.get(stock)
            if last_news is not None and news_hash(last_news) == current_digest:
                logger.info(f"[{stock}] 동일 뉴스입니다. 분석을 건너뜁니다.")
                return
        else:
            if await state.in_cooldown(stock):
                logger.info(f"[{stock}] 분석 cooldown 중입니다. 이번 실행을 건너뜁니다.")
                return

            last_digest = await state.get_last_news_hash(stock)
            if last_digest == current_digest:
                logger.info(f"[{stock}] 동일 뉴스입니다. 분석을 건너뜁니다.")
                return

            analysis_token = await state.acquire_analysis_lock(stock)
            if analysis_token is None:
                logger.info(f"[{stock}] 다른 worker가 분석 중입니다. 이번 실행을 건너뜁니다.")
                return

            last_news = await state.get_last_news_text(stock)

        # 2. 유의미성 판단 (Local Model or Mini LLM API)
        is_significant = await check_news_significance(
            stock,
            current_news,
            last_news,
            provider=FILTER_PROVIDER,
        )

        # 상태 업데이트 (유의미성 여부와 상관없이 현재 뉴스를 캐시)
        if state is None:
            last_analyzed_news_cache[stock] = current_news
        else:
            await state.set_last_news(stock, current_news, current_digest)

        if not is_significant:
            logger.info(f"[{stock}] 유의미한 변화 없음. 분석을 건너뜁니다.")
            return

        # 3. 유의미한 변화가 있을 때만 고성능 에이전트 분석 실행
        logger.info(f"[{stock}] 유의미한 변화 감지! 상세 분석을 시작합니다.")
        analysis_data = await perform_stock_analysis(stock, "nat", session)

        # 분석 결과를 WebSocket으로 실시간 전송
        await manager.broadcast({
            "type": "AGENT_ANALYSIS",
            "stock": stock,
            "data": analysis_data,
            "reason": "significant_change_detected"
        })
        logger.info(f"[{stock}] 분석 결과 브로드캐스트 완료")

    except Exception as e:
        if state is not None and analysis_token is not None:
            await state.set_cooldown(stock, type(e).__name__)
        logger.error(f"[{stock}] 개별 종목 모니터링 중 오류: {e}")
    finally:
        if state is not None and analysis_token is not None:
            await state.release_lock(state.keys.analysis_lock(stock), analysis_token)

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
        
        # 10분마다 시장 모니터링 수행 (뉴스 기반 필터링이 있으므로 주기를 짧게 조정 가능)
        scheduler.add_job(monitor_market_task, "interval", minutes=10, id="market_monitoring")
        
        scheduler.start()
        logger.info("APScheduler started with optimized monitoring tasks.")

def stop_scheduler():
    """스케줄러를 종료합니다."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")
