import os
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session
from .ws_manager import manager
from .database import engine
from .config import NEWS_MCP_PARAMS
from .services import perform_stock_analysis, run_mcp_tool, check_news_significance

logger = logging.getLogger(__name__)

# 비동기 스케줄러 인스턴스 생성
scheduler = AsyncIOScheduler()

# TODO: 나중에 종목리스트 한투 mcp 또는 db에서 가져와야함
# 모니터링 대상 종목 리스트
MONITORING_STOCKS = ["삼성전자", "SK하이닉스", "현대차"]

# 뉴스 필터링에 사용할 모델 제공자 (ollama 또는 openai)
FILTER_PROVIDER = os.environ.get("NEWS_FILTER_PROVIDER", "ollama")

# 종목별 마지막으로 분석된 뉴스 내용을 저장하는 인메모리 캐시 (LLM 호출 최적화용)
last_analyzed_news_cache = {}

async def monitor_market_task():
    """
    주기적으로 시장 상황을 모니터링합니다.
    로컬 초경량 모델 또는 경량 API를 통한 1차 필터링으로 비용을 최적화합니다.
    """
    logger.info(f"시장 업데이트 확인 중 (필터링 모델: {FILTER_PROVIDER}): {MONITORING_STOCKS}")
    
    with Session(engine) as session:
        for stock in MONITORING_STOCKS:
            try:
                # 1. 최신 뉴스 수집
                current_news = await run_mcp_tool(NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock})
                
                # 2. 유의미성 판단 (Local Model or Mini LLM API)
                last_news = last_analyzed_news_cache.get(stock)
                is_significant = await check_news_significance(
                    stock, 
                    current_news, 
                    last_news, 
                    provider=FILTER_PROVIDER
                )

                if not is_significant:
                    logger.info(f"[{stock}] 유의미한 변화 없음. 분석을 건너뜁니다.")
                    continue

                # 3. 유의미한 변화가 있을 때만 고성능 에이전트 분석 실행
                logger.info(f"[{stock}] 유의미한 변화 감지! 상세 분석을 시작합니다.")
                analysis_data = await perform_stock_analysis(stock, "nat", session)

                # 상태 업데이트
                last_analyzed_news_cache[stock] = current_news

                # 분석 결과를 WebSocket으로 실시간 전송
                await manager.broadcast({
                    "type": "AGENT_ANALYSIS",
                    "stock": stock,
                    "data": analysis_data,
                    "reason": "significant_change_detected"
                })
                logger.info(f"[{stock}] 분석 결과 브로드캐스트 완료")

            except Exception as e:
                logger.error(f"[{stock}] 모니터링 워커 실행 중 오류: {e}")

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
