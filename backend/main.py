import hmac
import os
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from fastapi import FastAPI, HTTPException, Query, Depends, Request, WebSocket, WebSocketDisconnect, Body
from fastapi.responses import JSONResponse
from sqlmodel import Session, col, select

from .config import (
    NEWS_MCP_PARAMS,
    TRADING_MCP_PARAMS,
    DART_MCP_PARAMS,
    ALLOW_ORIGINS,
    FINUS_API_KEY,
    FILTERED_SIGNAL_RETENTION_DAYS,
    SIGNAL_SCORE_MAX,
    SIGNAL_SCORE_MIN,
    SIGNAL_SCORE_THRESHOLD,
)
from .filtered_signal_repo import fill_score_axis, score_histogram
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
    _log_api_auth_state()
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

# CORSMiddleware는 제거했다(#246). #245로 nginx가 /api를 프록시하고 #262로 번들이
# 상대 경로를 쓰도록 재빌드되면서, 브라우저 요청은 대시보드와 같은 오리진으로 나가
# CORS 자체가 개입하지 않는다. 남겨 두면 이제 아무도 타지 않는 경로가 "동작 중인
# 보호"처럼 보이고, 실제로는 응답을 *읽는* 것만 막을 뿐 요청 *실행*은 막지 못한다.
# 오리진 단위로 실제 호출을 막는 것은 nginx 레이트리밋(#266 1단계, frontend/nginx.conf)과
# 아래 API 키 인증(#266 2단계)의 몫이다.
#
# ALLOW_ORIGINS는 남는다 — 아래 is_allowed_ws_origin이 /api/v1/ws 핸드셰이크의 Origin
# 허용목록으로 계속 쓴다(#256). CORSMiddleware는 애초에 WS 핸드셰이크에 적용되지 않아,
# 이 미들웨어를 걷어내도 그쪽 검사는 그대로다.


# ──────────────────────────────────────────────────────────────────────────
# API 정적 키 인증 (#266 2단계)
# ──────────────────────────────────────────────────────────────────────────
# 1단계(nginx 레이트리밋)는 비용의 뚜껑이지 접근 제어가 아니었다. 여기서 거는 것이
# 접근 제어다. 키가 설정된 배포에서만 걸린다 — 미설정이 기본이고, 그 이유는
# config.FINUS_API_KEY 주석에 있다.

# REST가 키를 받는 헤더 이름.
API_KEY_HEADER = "X-API-Key"
# WebSocket이 같은 키를 받는 쿼리 파라미터 이름. 헤더가 아닌 것은 취향이 아니라
# 제약이다 — 브라우저 WebSocket API에는 커스텀 헤더를 붙일 자리가 없다(#266 방향 결정).
WS_API_KEY_QUERY_PARAM = "api_key"

# 인증이 걸리는 경로 접두사. nginx 레이트리밋이 덮는 범위(#266 1단계의 `location /api/`)와
# 같은 /api/다. 두 보호의 경계가 어긋나면 "제한은 걸리는데 인증은 안 걸리는" 경로가
# 생기고, 그런 경로는 설정을 나란히 읽어야만 보인다.
_GUARDED_PATH_PREFIX = "/api/"


def api_auth_enabled() -> bool:
    """이 배포에 API 인증이 걸려 있는지."""
    return bool(FINUS_API_KEY)


def matches_api_key(presented: str | None) -> bool:
    """제시된 키가 FINUS_API_KEY와 정확히 같은지 판정합니다.

    hmac.compare_digest를 쓰는 이유는 타이밍 공격만이 아니다. `==`나 startswith 같은
    비교로 바뀌는 것을 막을 근거를 함수 하나에 못박아 두려는 것이다 — 비교는 항상
    전체 문자열 완전 일치다(ALLOW_ORIGINS 대조가 같은 이유로 완전 일치인 것과 같다).

    키가 설정되지 않았으면 무엇을 제시하든 False다. "인증이 꺼져 있다"는 판정은 이
    함수가 아니라 api_auth_enabled가 한다. 둘을 한 함수에 섞으면 키 미설정이 "아무 키나
    맞음"으로 읽히는 자리가 생기고, 그 자리는 나중에 인증을 켰을 때 조용히 열려 있다.

    비교 전에 UTF-8 바이트로 바꾸는 것은 필수다. hmac.compare_digest는 str을 받으면
    ASCII만 허용하고 그 밖의 문자에는 TypeError를 낸다 — 그대로 두면 헤더에 한글 한
    글자를 실은 요청이 401이 아니라 500이 되고, 인증 실패가 서버 오류로 둔갑한다.
    """
    if not FINUS_API_KEY or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), FINUS_API_KEY.encode("utf-8"))


def is_authorized_api_call(presented: str | None) -> bool:
    """인증이 꺼져 있으면 통과, 켜져 있으면 키 일치만 통과합니다.

    REST(헤더)와 WebSocket(쿼리 파라미터)이 같은 판정을 쓴다. 전달 경로만 다르고
    "무엇을 인정하는가"는 하나여야, 한쪽만 느슨해지는 드리프트가 생기지 않는다.
    """
    return not api_auth_enabled() or matches_api_key(presented)


def _log_api_auth_state() -> None:
    """기동 시점에 인증 상태를 로그로 남긴다.

    꺼져 있는 것이 기본값이라 조용히 지나가기 쉽다. 무인증으로 열려 있다는 사실은
    운영자가 매번 다시 확인해야 하는 값이므로 경고로 올린다 — config가 잘못된 env를
    기본값으로 되돌릴 때 경고를 남기는 것(_warn_bad_env)과 같은 원칙이다.
    """
    if api_auth_enabled():
        logger.info("API 인증이 켜져 있습니다 (#266 2단계) — /api/ 와 /api/v1/ws 가 키를 요구합니다.")
        return
    logger.warning(
        "FINUS_API_KEY가 비어 있어 API 인증이 꺼져 있습니다 — /api/ 전체와 /api/v1/ws가 "
        "무인증으로 열립니다(#266 2단계). 남은 보호는 nginx 레이트리밋(비용 상한)과 "
        "WebSocket Origin 검사뿐입니다."
    )


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """/api/ 아래 모든 HTTP 요청에 정적 키를 요구합니다 (#266 2단계).

    라우트마다 Depends를 다는 대신 미들웨어 한 곳에 둔다. 이 파일은 라우트를 계속
    늘려 왔고(그중 /api/v1/analyze는 호출 한 번이 LLM 과금이다), 데코레이터를 빠뜨린
    라우트는 아무 테스트도 빨간불로 만들지 않는다. 인증이 "대부분의 경로에 있는" 상태는
    없는 것과 크게 다르지 않으므로, 접두사 하나로 판정해 새 라우트가 자동으로 덮이게 한다.

    라우팅보다 먼저 걸리는 것도 의도다. 존재하지 않는 /api/ 경로도 404가 아니라 401이
    되므로, 키 없는 호출자가 어떤 엔드포인트가 있는지 훑을 수 없다.

    /health는 접두사 밖이라 그대로 열려 있다. 이것도 의도다 — compose 헬스체크가 curl로
    부르고(docker-compose.yml), 응답에는 {"status": "alive"} 말고 아무것도 없다(#252
    리뷰에서 nat_base_url을 뺐다). 키를 요구하면 컨테이너가 스스로를 unhealthy로 만든다.

    WebSocket 핸드셰이크는 여기로 오지 않는다 — scope["type"]이 "websocket"이라
    BaseHTTPMiddleware가 그대로 통과시킨다. /api/v1/ws는 엔드포인트에서 직접 검사한다.
    """
    if (
        request.url.path.startswith(_GUARDED_PATH_PREFIX)
        and not is_authorized_api_call(request.headers.get(API_KEY_HEADER))
    ):
        # 제시된 키 자체는 로그에 남기지 않는다. 오타 진단에는 도움이 되겠지만, 그
        # 편의를 위해 비밀값을 로그 파일에 영구히 남기는 거래는 하지 않는다(#257에서
        # 텔레그램 토큰을 URL 로그에서 걷어낸 것과 같은 판단).
        logger.warning(
            "API 키 검증 실패 — 요청을 거부합니다: %s %s", request.method, request.url.path
        )
        # 본문은 FastAPI의 {"detail": ...}와 같은 모양으로 낸다. Unity의
        # ApiClient.ExtractErrorMessage가 그 키를 읽어 배너에 싣고, nginx의 429 본문
        # (#266 1단계)도 이미 같은 계약을 쓴다.
        #
        # WWW-Authenticate는 붙이지 않는다. RFC 9110은 401에 그 헤더를 요구하지만 여기서
        # 쓰는 것은 HTTP 인증 스킴이 아니라 커스텀 헤더다. 없는 스킴 이름을 지어 challenge를
        # 붙이면 규격 문구는 만족하면서 브라우저에 인증 대화상자를 띄울 여지를 남긴다.
        # 403으로 내리는 안도 있으나 "키를 보내면 된다"는 정보를 지운다.
        return JSONResponse(status_code=401, content={"detail": "API 키가 필요합니다."})
    return await call_next(request)


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
    reports = session.exec(select(AgentReport).order_by(col(AgentReport.created_at).desc())).all()
    return {"status": "success", "data": reports}


@app.get("/api/v1/db/diary", response_model=CommonResponse, tags=["Database"])
async def get_db_diary(session: Session = Depends(get_session)):
    """저장된 투자 일지 목록을 조회합니다."""
    diaries = session.exec(select(Diary).order_by(col(Diary.created_at).desc())).all()
    return {"status": "success", "data": diaries}


@app.get(
    "/api/v1/db/catalysts",
    response_model=CommonResponse,
    tags=["Database"],
    responses={422: {"description": "to_date < from_date 이거나 쿼리 파라미터 검증 실패"}},
)
async def get_db_catalysts(
    # Request는 쿼리 파라미터가 아니라 타입으로 판별돼 주입된다. 기본값이 없는 인자라
    # 기본값 있는 인자들보다 앞에 와야 한다(파이썬 문법). from_date가 기본값으로
    # 채워졌는지 판별하는 데만 쓴다 — 아래 422 분기 참고.
    request: Request,
    stock_name: str | None = Query(None, min_length=1, description="종목명 정확 일치 필터. 생략 시 전체 종목 조회."),
    # default_factory에 today_kst를 직접 넘기면 라우트 등록 시점의 함수 객체가
    # 고정돼 테스트에서 backend.main.today_kst를 monkeypatch해도 반영되지 않는다.
    # 람다로 감싸 요청마다 모듈 전역의 today_kst를 다시 조회하도록 한다.
    from_date: date = Query(default_factory=lambda: today_kst(), description="이 날짜부터(당일 포함) 이벤트만 조회. 생략 시 KST 기준 오늘."),
    # 이슈 #228: 프론트 시간 링(캘린더 시각화)이 한 번에 그릴 이벤트 수를 과도하게
    # 받아 렌더링이 느려지는 것을 막기 위한 상한(500). 1건도 없이 호출되는 것을
    # 막기 위한 하한(1). 기본값 100은 기존 catalyst_repo.list_upcoming의 기본값(20)보다
    # 넉넉하게 잡아 여러 종목을 한 번에 다루는 전체 조회 용도에 맞춘다.
    limit: int = Query(100, ge=1, le=500, description="결과 상한 (1~500, 기본 100)."),
    # 이슈 #238: 프론트 시간 링은 "이번 달", "앞으로 3개월" 같은 구간 조회다. to_date가
    # 없으면 from_date 이후 전부를 요청한 뒤 limit에 걸려 뒷부분이 잘리는데, 잘린 뒷부분을
    # 다시 가져올 방법이 없었다. 기본 상한(예: from_date+90일)은 두지 않는다 — "전체 조회"
    # 용도를 막고 기존 호출자의 동작을 조용히 바꾸기 때문이다. 생략 시 상한 없음이 유지된다.
    #
    # 남은 간극: to_date는 절단 문제를 완화할 뿐 없애지 못한다. 구간을 하루까지 좁혀도
    # (from_date == to_date) 그 하루에 limit을 넘는 이벤트가 몰리면 message == "truncated"인
    # 채로 더 좁힐 수 없어 뒷부분을 회수할 방법이 없다. 실적 시즌처럼 특정일에 공시가
    # 집중되는 도메인이라 가정적 시나리오가 아니다. 이 잔여 간극은 offset/cursor
    # 페이지네이션으로만 닫히며, 별도 이슈로 다룬다.
    to_date: date | None = Query(
        None,
        description=(
            "이 날짜까지(당일 포함) 이벤트만 조회. 생략 시 상한 없음. "
            "과거 구간을 조회하려면 from_date도 함께 보내야 한다 "
            "(생략 시 KST 기준 오늘이 적용되어 to_date < from_date로 422가 난다)."
        ),
    ),
    session: Session = Depends(get_session),
):
    """저장된 촉매 이벤트(실적/배당/공시/주총 등)를 조회합니다."""
    # to_date < from_date는 빈 결과로 조용히 응답하지 않고 422로 거부한다. 빈 결과로
    # 두면 "이 구간에 이벤트가 없다"와 "파라미터를 잘못 보냈다"를 클라이언트가 구분할
    # 수 없다. 이 엔드포인트가 이미 빈 stock_name을 min_length=1로 422 거부하는 선례가
    # 있어 일관된다. 두 파라미터 간 관계는 FastAPI Query만으로 검증할 수 없어 핸들러
    # 본문에서 직접 확인한다.
    #
    # detail은 FastAPI 자체 검증(min_length=1, ge/le 등)과 동일한 list[{loc,msg,type}]
    # 형태로 낸다. 같은 엔드포인트가 같은 422를 dict와 list 두 형태로 내면
    # `for err in resp.json()["detail"]: err["loc"]` 같은 표준 파싱이 dict를 순회해
    # 키 문자열에서 TypeError로 죽는다. 상태 코드만 선례를 따르고 body 계약은 따르지
    # 않은 셈이었다.
    #
    # 교차 필드 의미 오류를 400으로 분리하는 안도 검토했다("422 = 스키마 검증" 불변식이
    # 유지된다). 채택하지 않은 이유: 이미 422로 문서화·테스트된 계약을 바꾸는 것이라
    # 팀 합의가 필요한데, 소비자가 붙기 전인 지금은 형태 통일만으로 문제가 해소되므로
    # 계약 변경 비용을 지불할 이유가 없다.
    #
    # from_date는 생략 시 서버가 오늘(KST)로 채운다. 클라이언트가 과거 구간을 의도해
    # to_date만 보내면 보낸 적 없는 from_date와 비교돼 422가 나므로, 기본값이 적용됐다는
    # 사실을 detail에 실어 원인을 바로 알 수 있게 한다.
    from_date_defaulted = "from_date" not in request.query_params
    if to_date is not None and to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["query", "to_date"],
                    "msg": (
                        "to_date must not be earlier than from_date "
                        "(from_date was not sent and defaulted to today in KST)"
                        if from_date_defaulted
                        else "to_date must not be earlier than from_date"
                    ),
                    "type": "value_error.date_range",
                    "ctx": {
                        "from_date": from_date.isoformat(),
                        "to_date": to_date.isoformat(),
                        "from_date_defaulted": from_date_defaulted,
                    },
                }
            ],
        )
    # catalyst_repo.SqliteCatalystEventRepo.list_upcoming은 stock_name이 필수 인자라
    # 전체 종목 조회에 쓸 수 없다. 기존 /api/v1/db/* 4종과 동일하게 이 라우트에서
    # session.exec(select(...))를 직접 실행한다(catalyst_repo.py는 수정하지 않는다).
    query = select(CatalystEvent).where(CatalystEvent.event_date >= from_date)
    if to_date is not None:
        query = query.where(CatalystEvent.event_date <= to_date)
    if stock_name is not None:
        query = query.where(CatalystEvent.stock_name == stock_name)
    # event_date만으로는 동일 날짜 이벤트 간 순서가 SQL상 보장되지 않아 limit 절단이
    # 비결정적일 수 있다. event_type, id까지 더해 완전히 결정적으로 정렬한다.
    query = query.order_by(
        col(CatalystEvent.event_date),
        col(CatalystEvent.event_type),
        col(CatalystEvent.id),
    ).limit(limit + 1)

    rows = session.exec(query).all()
    truncated = len(rows) > limit
    return {
        "status": "success",
        "data": rows[:limit],
        "message": "truncated" if truncated else None,
    }


# 걸러진 신호 조회의 출처 필터가 받는 값. scheduler.SIGNAL_SOURCES의 name과 같아야
# 하며, 어긋나면 test_filtered_signal.py의 드리프트 테스트가 깨진다. 자유 문자열로 두면
# 오타가 422가 아니라 "total: 0"인 정상 응답으로 돌아와, 임계값 조정 근거를 읽는 쪽에서
# "걸러진 게 없다"와 "필터를 잘못 썼다"가 구분되지 않는다.
#
# 저장 컬럼(FilteredSignal.source)은 좁히지 않는다. 출처 목록이 나중에 바뀌어도 이미
# 쌓인 행은 그대로 남아야 하고, 그 행들을 읽는 것은 필터 없는 전체 조회로 가능하다.
SignalSourceName = Literal["news", "disclosure"]


@app.get(
    "/api/v1/db/filtered-signals/histogram",
    response_model=CommonResponse,
    tags=["Database"],
)
async def get_filtered_signal_histogram(
    source: SignalSourceName | None = Query(
        None, description="신호 출처 필터 (news | disclosure). 생략 시 전체."
    ),
    stock_name: str | None = Query(
        None, min_length=1, description="종목명 정확 일치 필터. 생략 시 전체 종목."
    ),
    days: int | None = Query(
        None,
        ge=1,
        le=365,
        description="최근 N일(UTC 기준)만 집계. 생략 시 보관 중인 전체 구간.",
    ),
    session: Session = Depends(get_session),
):
    """임계값 미만으로 걸러진 신호를 점수 구간별로 집계합니다 (#304).

    임계값(SIGNAL_SCORE_THRESHOLD)을 조정할 때 "지금 값에서 걸러지고 있는 신호가
    점수대별로 몇 건인지"를 로그 grep 없이 확인하는 경로다. 통과한 쪽의 점수는 이
    테이블이 아니라 AgentReport.signal_score에 있다.

    buckets는 -3~+3 전 구간을 0으로 채워 돌려준다. 이 응답은 사람이 임계값을 정하려고
    읽는 표라서, "그 점수를 한 건도 못 봤다"가 빈 칸이 아니라 0으로 보여야 한다.

    recorded_thresholds에 값이 둘 이상이면 집계 구간 중간에 임계값이 바뀐 것이다.
    그때 buckets는 서로 다른 설정에서 걸러진 신호가 섞인 분포이므로 한 덩어리로
    읽으면 안 된다 — days를 좁혀 다시 조회해야 한다.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(days=days) if days is not None else None
    )
    histogram = score_histogram(
        session, source=source, stock_name=stock_name, since=since
    )
    return {
        "status": "success",
        "data": {
            "threshold": SIGNAL_SCORE_THRESHOLD,
            "retention_days": FILTERED_SIGNAL_RETENTION_DAYS,
            "window_days": days,
            "since": since.isoformat() if since is not None else None,
            "total": histogram.total,
            "buckets": [
                {"score": bucket.score, "count": bucket.count}
                for bucket in fill_score_axis(
                    histogram.buckets, SIGNAL_SCORE_MIN, SIGNAL_SCORE_MAX
                )
            ],
            "recorded_thresholds": list(histogram.thresholds),
            "oldest": histogram.oldest,
            "newest": histogram.newest,
        },
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
    # nat_base_url(내부 서비스 주소)은 싣지 않는다. #245로 nginx가 /health를 8080으로도
    # 중계하면서 내부 토폴로지가 외부에 노출되는 경로가 생겼다. compose 헬스체크는
    # 상태코드만 보고, 이 필드를 읽는 코드도 없다(PR #252 리뷰).
    return {"status": "alive"}


def is_allowed_ws_origin(origin: str | None) -> bool:
    """WebSocket 핸드셰이크의 Origin이 허용 대상인지 판정합니다 (#256).

    Starlette의 CORSMiddleware는 WebSocket 핸드셰이크에 적용되지 않는다. 그래서 HTTP를
    아무리 조여도 WS는 그대로 열려 있고, 임의 사이트에 심어 둔 new WebSocket(...)이 붙어
    브로드캐스트를 수신할 수 있다(Cross-Site WebSocket Hijacking). ALLOW_ORIGINS를 WS
    핸드셰이크에서도 직접 대조해 이 비대칭을 없앤다.

    #246에서 CORSMiddleware를 걷어낸 뒤로는 이 함수가 ALLOW_ORIGINS의 **유일한**
    소비자다. 목록을 지우면 화면은 뜨는데 실시간 알림만 403으로 끊기므로, 쓰이지 않는
    설정으로 오해해 정리하지 말 것.

    Origin 헤더가 없으면 허용한다. Origin은 브라우저가 붙이는 헤더이고 CSWSH는 브라우저
    공격이다. curl·wscat·헬스체크 같은 비브라우저 클라이언트는 헤더를 보내지 않으므로,
    없음을 거부로 취급하면 막으려는 위협은 그대로 둔 채 운영 도구만 끊긴다.

    그 결과 남던 잔여 위험(Origin을 보내지 않는 클라이언트는 여전히 붙는다)은 이제 이
    함수가 아니라 API 키가 닫는다(#266 2단계, is_authorized_api_call). 두 검사는 막는
    것이 다르므로 둘 다 남는다 — Origin 검사는 키를 모르는 **브라우저**를 막고(키를 URL로
    넘기는 정상 클라이언트가 있어도 임의 사이트는 그 키를 모른다), 키 검사는 Origin을
    보내지 않거나 위조하는 **비브라우저**를 막는다. 어느 한쪽만으로는 다른 쪽이 열린다.
    """
    if origin is None:
        return True
    # "*"는 전체 허용이다. CORSMiddleware가 쓰던 관례를 그대로 따른다 — 그 미들웨어는
    # #246에서 사라졌지만, .env.example과 문서가 이 목록을 그렇게 설명해 왔고 표기의
    # 의미만 바꾸면 기존 .env가 조용히 다른 뜻이 된다.
    if "*" in ALLOW_ORIGINS:
        return True
    return origin in ALLOW_ORIGINS


@app.websocket("/api/v1/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    실시간 알림을 위한 WebSocket 엔드포인트입니다.
    """
    origin = websocket.headers.get("origin")
    if not is_allowed_ws_origin(origin):
        # accept() 전에 close()하면 핸드셰이크가 완성되지 않고 HTTP 403으로 끝난다.
        # accept() 후에 끊으면 그사이 브로드캐스트 1건이 나갈 수 있으므로 순서가 중요하다.
        logger.warning("WebSocket 연결 거부 — 허용되지 않은 Origin: %s", origin)
        await websocket.close(code=1008)
        return
    # 정적 키 검사(#266 2단계). HTTP는 미들웨어가 헤더로 받지만, 브라우저 WebSocket
    # API에는 커스텀 헤더를 붙일 자리가 없어 쿼리 파라미터로 받는다.
    #
    # Origin 검사 뒤에 둔다. 둘 다 거절이지만 키는 URL에 실려 프록시 로그·에러 리포트에
    # 남을 수 있는 값이라, 먼저 잘라낼 수 있는 것을 먼저 잘라 키가 오가는 요청 수를 줄인다.
    # 판정 자체는 순서와 무관하다(둘 다 통과해야 연결된다).
    if not is_authorized_api_call(websocket.query_params.get(WS_API_KEY_QUERY_PARAM)):
        # 위 미들웨어와 같은 이유로 제시된 키는 로그에 싣지 않는다.
        logger.warning("WebSocket 연결 거부 — API 키가 없거나 일치하지 않습니다.")
        await websocket.close(code=1008)
        return
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

    # 기본 포트는 compose·문서·에디터 폴백이 모두 쓰는 8000으로 맞춘다.
    # 이전 기본값 8787은 이 엔트리포인트에서만 쓰이던 값이라 혼선의 원인이었다.
    # 그 대신 compose의 backend와 포트가 겹치므로, 둘을 동시에 띄우면 EADDRINUSE가 난다.
    # 같은 서비스를 두 번 띄우는 것이니 정직한 실패다. 굳이 겹쳐 쓰려면
    # FIN_US_BACKEND_PORT로 다른 포트를 지정한다.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("FIN_US_BACKEND_PORT", "8000")))
