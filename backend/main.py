import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Depends, WebSocket, WebSocketDisconnect, Body
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from .config import NAT_BASE_URL, NEWS_MCP_PARAMS, TRADING_MCP_PARAMS, DART_MCP_PARAMS, ALLOW_ORIGINS
from .ws_manager import manager
from .scheduler import start_scheduler, stop_scheduler
from .telegram_commands import start_telegram_commands, stop_telegram_commands
from .schemas import CommonResponse, DiaryCreate
from .services import (
    run_mcp_tool,
    normalize_llm_provider,
    llm_chat,
    analysis_from_nat_text,
    perform_stock_analysis
)
from .database import init_db, get_session
from .models import Portfolio, TradeHistory, AgentReport, Diary

# 로깅 설정: 모든 모듈의 로그를 터미널에 출력하도록 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱 시작 시 실행: DB 초기화 및 스케줄러 가동
    init_db()
    start_scheduler()
    start_telegram_commands()
    logger.info("Database initialized and scheduler started.")
    yield
    # 앱 종료 시 실행: 스케줄러 안전 종료
    await stop_telegram_commands()
    stop_scheduler()
    logger.info("Scheduler stopped.")


app = FastAPI(
    title="Fin-Us + NAT (integrate)",
    description="MCP market data from fin-us; multi-agent analysis via NeMo NAT FastAPI",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/v1/news", response_model=CommonResponse, tags=["Market Data"])
async def get_news(stock: str = Query(..., examples=["삼성전자"])):
    content = await run_mcp_tool(NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock})
    return {"status": "success", "data": {"stock": stock, "news": content.split("\n")}}


@app.get("/api/v1/disclosures", response_model=CommonResponse, tags=["Market Data"])
async def get_disclosures(stock: str = Query(..., examples=["삼성전자"])):
    disclosure = await run_mcp_tool(DART_MCP_PARAMS, "get_disclosure_signal", {"stock_name": stock})
    return {"status": "success", "data": {"stock": stock, "disclosure": disclosure}}


@app.get("/api/v1/analyze", response_model=CommonResponse, tags=["AI Agent"])
async def analyze_stock(
    stock: str = Query(..., examples=["SK하이닉스"]),
    provider: str = Query(
        "openai",
        description=(
            "openai=OpenAI; anthropic=Anthropic; ollama=local OpenAI-compatible /v1; "
            "nat=NAT multi-agent (still available via API query)"
        ),
    ),
    session: Session = Depends(get_session),
):
    data = await perform_stock_analysis(stock, provider, session)
    return {"status": "success", "data": data}


@app.get("/api/v1/trading/trend", response_model=CommonResponse, tags=["Market Data"])
async def get_trading_trend(stock: str = Query(..., examples=["삼성전자"])):
    trend = await run_mcp_tool(
        TRADING_MCP_PARAMS,
        "get_investor_trading",
        {"stock_name": stock},
    )
    return {"status": "success", "data": {"stock": stock, "trend": trend}}


@app.get("/api/v1/trading/balance", response_model=CommonResponse, tags=["Trading"])
async def get_account_balance():
    balance_text = await run_mcp_tool(TRADING_MCP_PARAMS, "get_balance", {})
    return {"status": "success", "data": {"report": balance_text}}


def _portfolio_return_rate(current_price: float, avg_price: float) -> float:
    if avg_price <= 0:
        return 0.0
    return round(((current_price - avg_price) / avg_price) * 100, 4)


@app.get("/api/v1/portfolio", response_model=CommonResponse, tags=["Portfolio"])
async def get_visualization_portfolio(session: Session = Depends(get_session)):
    """Unity WebGL 시각화 화면에서 사용하는 포트폴리오 요약을 조회합니다."""
    portfolios = session.exec(select(Portfolio)).all()
    holdings = []
    total_asset = 0.0
    total_cost = 0.0
    total_market_for_return = 0.0

    for portfolio in portfolios:
        avg_price = float(portfolio.avg_price)
        quantity = portfolio.quantity

        if portfolio.current_price is not None:
            # 현재가가 있는 종목: 평가금액과 수익률을 정확히 계산합니다.
            current_price: float | None = float(portfolio.current_price)
            return_rate: float | None = _portfolio_return_rate(current_price, avg_price)
            market_value = current_price * quantity
            total_asset += market_value
            if avg_price > 0:
                total_cost += avg_price * quantity
                total_market_for_return += market_value
        else:
            # 현재가가 없는 종목: 수익률을 알 수 없으므로 None을 반환합니다.
            # None을 반환해 "실제 0%"와 구분합니다(이슈 #122).
            # 총자산은 매입금액(avg_price × quantity) 기준 근사값을 사용합니다.
            current_price = None
            return_rate = None
            total_asset += avg_price * quantity

        holdings.append({
            "name": portfolio.stock_name,
            "current_price": current_price,
            "avg_price": avg_price,
            "return_rate": return_rate,
            "quantity": quantity,
        })

    # 종목별 return_rate와 같은 이유로 총수익률도 "모름"과 "실제 0%"를 구분한다(이슈 #122).
    # 보유 종목은 있는데 현재가가 하나도 없으면 total_cost가 0으로 남는데, 이때 0.0을
    # 돌려주면 소비자는 그것을 실제 수익률 0%로 읽는다. 현재 current_price 소스가 없어
    # 항상 이 경우에 해당하므로(후속 이슈 참고), 여기서 0.0을 반환하면 종목별로 고친
    # 구분이 계정 총계에서 그대로 무너진다.
    total_return_rate: float | None
    if total_cost > 0:
        total_return_rate = round(((total_market_for_return - total_cost) / total_cost) * 100, 4)
    elif portfolios:
        total_return_rate = None
    else:
        # 보유 종목 자체가 없으면 수익률을 "모른다"고 할 것이 없다. 기존 동작을 유지한다.
        total_return_rate = 0.0

    return {
        "status": "success",
        "data": {
            "total_asset": total_asset,
            "total_return_rate": total_return_rate,
            "holdings": holdings,
        },
    }


@app.get("/api/v1/db/portfolio", response_model=CommonResponse, tags=["Database"])
async def get_db_portfolio(session: Session = Depends(get_session)):
    """저장된 포트폴리오 정보를 조회합니다."""
    portfolios = session.exec(select(Portfolio)).all()
    return {"status": "success", "data": portfolios}


@app.get("/api/v1/db/trades", response_model=CommonResponse, tags=["Database"])
async def get_db_trades(session: Session = Depends(get_session)):
    """저장된 매매 이력을 조회합니다."""
    trades = session.exec(select(TradeHistory)).all()
    return {"status": "success", "data": trades}


@app.get("/api/v1/db/reports", response_model=CommonResponse, tags=["Database"])
async def get_db_reports(session: Session = Depends(get_session)):
    """저장된 에이전트 리포트 목록을 조회합니다."""
    reports = session.exec(select(AgentReport).order_by(AgentReport.created_at.desc())).all()
    return {"status": "success", "data": reports}


@app.get("/api/v1/db/diary", response_model=CommonResponse, tags=["Database"])
async def get_db_diary(session: Session = Depends(get_session)):
    """저장된 투자 일지 목록을 조회합니다."""
    diaries = session.exec(select(Diary).order_by(Diary.created_at.desc())).all()
    return {"status": "success", "data": diaries}


@app.post("/api/v1/db/diary", response_model=CommonResponse, tags=["Database"])
async def create_db_diary(
    diary_in: DiaryCreate,
    session: Session = Depends(get_session)
):
    """새로운 투자 일지를 작성합니다."""
    diary = Diary.model_validate(diary_in)
    session.add(diary)
    session.commit()
    session.refresh(diary)
    return {"status": "success", "data": diary}


@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "alive", "nat_base_url": NAT_BASE_URL}


@app.websocket("/api/v1/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    실시간 알림을 위한 WebSocket 엔드포인트입니다.
    """
    await manager.connect(websocket)
    try:
        while True:
            # 연결 유지를 위해 클라이언트의 메시지를 대기함
            data = await websocket.receive_text()
            # 에코 응답 (테스트용)
            await websocket.send_json({"status": "received", "message": data})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("FIN_US_BACKEND_PORT", "8787")))
