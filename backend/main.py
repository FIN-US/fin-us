import os
import logging
from contextlib import asynccontextmanager
from datetime import date
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
from .models import Portfolio, TradeHistory, AgentReport, Diary, CatalystEvent
from .timeutil import today_kst

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


def _portfolio_return_rate(current_price: float, avg_price: float) -> float | None:
    # 매입가를 모르면 수익률도 모른다 — 0%로 단언하지 않는다(이슈 #122).
    if avg_price <= 0:
        return None
    return round(((current_price - avg_price) / avg_price) * 100, 4)


@app.get("/api/v1/portfolio", response_model=CommonResponse, tags=["Portfolio"])
async def get_visualization_portfolio(session: Session = Depends(get_session)):
    """Unity WebGL 시각화 화면에서 사용하는 포트폴리오 요약을 조회합니다.

    Unity는 JsonUtility로 파싱하므로 nullable 값 타입(int?, float?)을 지원하지
    않습니다. JSON null은 JsonUtility에서 예외 없이 기본값(0)으로 처리됩니다.
    이를 방지하기 위해 각 nullable 필드에 대응하는 bool 플래그를 함께 내립니다:
      - price_known: current_price가 실제 값인지 여부(이슈 #122)
      - return_rate_known: return_rate가 실제 계산된 값인지 여부(이슈 #122)
        ※ current_price는 알지만 avg_price <= 0이면 price_known=True·return_rate_known=False
      - total_asset_is_estimate: total_asset이 현재가 없는 종목의 매입가 기준 추정값인지
      - total_return_rate_known: total_return_rate가 실제 계산된 값인지 여부
    """
    portfolios = session.exec(select(Portfolio)).all()
    holdings = []
    total_asset = 0.0
    total_cost = 0.0
    total_market_for_return = 0.0
    any_price_unknown = False

    for portfolio in portfolios:
        avg_price = float(portfolio.avg_price)
        quantity = portfolio.quantity

        if portfolio.current_price is not None:
            # 현재가가 있는 종목: 평가금액과 수익률을 정확히 계산합니다.
            current_price: float | None = float(portfolio.current_price)
            return_rate = _portfolio_return_rate(current_price, avg_price)
            price_known = True  # current_price가 실제 값임을 보장 — 수익률 계산 가능 여부와 독립
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
            price_known = False
            any_price_unknown = True
            total_asset += avg_price * quantity

        holdings.append({
            "name": portfolio.stock_name,
            "current_price": current_price,
            "avg_price": avg_price,
            "return_rate": return_rate,
            "quantity": quantity,
            # Unity JsonUtility가 null을 0으로 읽는 문제를 우회하기 위한 명시 플래그.
            # price_known=False 이면 current_price는 0이 아니라 "알 수 없음"이다.
            # return_rate_known=False 이면 return_rate는 0이 아니라 "알 수 없음"이다.
            # current_price는 알지만 avg_price <= 0이면 price_known=True·return_rate_known=False.
            "price_known": price_known,
            "return_rate_known": return_rate is not None,
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
            # True이면 total_asset에 현재가 없는 종목의 매입가 추정분이 포함된다.
            "total_asset_is_estimate": any_price_unknown,
            "total_return_rate": total_return_rate,
            # Unity JsonUtility가 null→0으로 읽는 문제 우회. False이면 total_return_rate는
            # 실제 계산값이 아니며 0으로 표시해서는 안 된다.
            "total_return_rate_known": total_return_rate is not None,
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


@app.get("/api/v1/db/catalysts", response_model=CommonResponse, tags=["Database"])
async def get_db_catalysts(
    stock_name: str | None = Query(None, min_length=1, description="종목명 정확 일치 필터. 생략 시 전체 종목 조회."),
    # default_factory에 today_kst를 직접 넘기면 라우트 등록 시점의 함수 객체가
    # 고정돼 테스트에서 backend.main.today_kst를 monkeypatch해도 반영되지 않는다.
    # 람다로 감싸 요청마다 모듈 전역의 today_kst를 다시 조회하도록 한다.
    from_date: date = Query(default_factory=lambda: today_kst(), description="이 날짜 이후(포함) 이벤트만 조회. 생략 시 KST 기준 오늘."),
    # 이슈 #228: 프론트 시간 링(캘린더 시각화)이 한 번에 그릴 이벤트 수를 과도하게
    # 받아 렌더링이 느려지는 것을 막기 위한 상한(500). 1건도 없이 호출되는 것을
    # 막기 위한 하한(1). 기본값 100은 기존 catalyst_repo.list_upcoming의 기본값(20)보다
    # 넉넉하게 잡아 여러 종목을 한 번에 다루는 전체 조회 용도에 맞춘다.
    limit: int = Query(100, ge=1, le=500, description="결과 상한 (1~500, 기본 100)."),
    session: Session = Depends(get_session),
):
    """저장된 촉매 이벤트(실적/배당/공시/주총 등)를 조회합니다."""
    # catalyst_repo.SqliteCatalystEventRepo.list_upcoming은 stock_name이 필수 인자라
    # 전체 종목 조회에 쓸 수 없다. 기존 /api/v1/db/* 4종과 동일하게 이 라우트에서
    # session.exec(select(...))를 직접 실행한다(catalyst_repo.py는 수정하지 않는다).
    query = select(CatalystEvent).where(CatalystEvent.event_date >= from_date)
    if stock_name is not None:
        query = query.where(CatalystEvent.stock_name == stock_name)
    # event_date만으로는 동일 날짜 이벤트 간 순서가 SQL상 보장되지 않아 limit 절단이
    # 비결정적일 수 있다. event_type, id까지 더해 완전히 결정적으로 정렬한다.
    query = query.order_by(
        CatalystEvent.event_date,
        CatalystEvent.event_type,
        CatalystEvent.id,
    ).limit(limit + 1)

    rows = session.exec(query).all()
    truncated = len(rows) > limit
    return {
        "status": "success",
        "data": rows[:limit],
        "message": "truncated" if truncated else None,
    }


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
