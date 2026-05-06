import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session
from .ws_manager import manager
from .database import engine
from .config import NEWS_MCP_PARAMS
from .services import perform_stock_analysis, run_mcp_tool

logger = logging.getLogger(__name__)

# 비동기 스케줄러 인스턴스 생성
scheduler = AsyncIOScheduler()

# TODO: 나중에 종목리스트 한투 mcp 또는 db에서 가져와야함
# 모니터링 대상 종목 리스트
MONITORING_STOCKS = ["삼성전자", "SK하이닉스", "현대차"]

# 종목별 마지막으로 분석된 뉴스 내용을 저장하는 인메모리 캐시 (LLM 호출 최적화용)
last_analyzed_news_cache = {}

async def monitor_market_task():
    """
    주기적으로 시장 상황을 모니터링합니다.
    뉴스가 업데이트된 경우에만 LLM 분석을 수행하여 비용을 절감합니다.
    """
    logger.info(f"Checking market updates for: {MONITORING_STOCKS}")
    
    with Session(engine) as session:
        for stock in MONITORING_STOCKS:
            try:
                # 1. 최신 뉴스 수집 (Pre-filtering)
                current_news = await run_mcp_tool(NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock})
                
                # 2. 이전 분석 시점의 뉴스와 비교 (Caching/State check)
                if stock in last_analyzed_news_cache and last_analyzed_news_cache[stock] == current_news:
                    logger.info(f"Skipping LLM analysis for {stock}: No new news detected.")
                    continue
                
                # 3. 새로운 정보가 있을 때만 에이전트 분석 실행
                logger.info(f"New information detected for {stock}. Triggering LLM analysis...")
                analysis_data = await perform_stock_analysis(stock, "nat", session)
                
                # 상태 업데이트
                last_analyzed_news_cache[stock] = current_news
                
                # 분석 결과를 WebSocket으로 실시간 전송
                await manager.broadcast({
                    "type": "AGENT_ANALYSIS",
                    "stock": stock,
                    "data": analysis_data,
                    "reason": "new_info_detected"
                })
                logger.info(f"Broadcasted analysis update for {stock}")
                
            except Exception as e:
                logger.error(f"Error in monitor_market_task for {stock}: {e}")

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
