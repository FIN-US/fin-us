from datetime import date, datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel

class Portfolio(SQLModel, table=True):
    """
    사용자의 현재 주식 잔고 및 포트폴리오 정보를 저장합니다.
    증권사 API(SSOT)의 데이터를 기반으로 동기화됩니다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_code: str = Field(index=True, description="종목 코드")
    stock_name: str = Field(description="종목명")
    quantity: int = Field(default=0, description="보유 수량")
    avg_price: float = Field(default=0.0, description="평균 매입가")
    current_price: Optional[float] = Field(default=None, description="현재가")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="최근 업데이트 시간")

class TradeHistory(SQLModel, table=True):
    """
    사용자의 주식 매매 내역을 저장합니다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_code: str = Field(index=True, description="종목 코드")
    stock_name: str = Field(description="종목명")
    trade_type: str = Field(description="매매 구분 (BUY/SELL)")
    quantity: int = Field(description="매매 수량")
    price: float = Field(description="매매 단가")
    trade_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="매매 일시")

class AgentReport(SQLModel, table=True):
    """
    AI 에이전트가 생성한 종목 분석 리포트 및 투자 제안을 저장합니다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_code: str = Field(index=True, description="분석 종목 코드")
    stock_name: str = Field(index=True, description="분석 종목명")
    provider: str = Field(description="사용된 LLM 제공자 (openai, anthropic 등)")
    summary: str = Field(description="분석 요약")
    # A (#162): provider_supports_tools=False인 provider(openai/anthropic/ollama)는
    # 도구 없이 매매 판단을 생성하지 않는다. 이 경우 두 필드는 null이다.
    # null과 "HOLD"/0.0의 의미가 다르다 — null은 "판단 없음"이고
    # "HOLD"/0.0은 NAT이 판단한 관망 의견이다.
    decision: Optional[str] = Field(
        default=None,
        description=(
            "투자 결정 (BUY/SELL/HOLD). provider_supports_tools=True인 provider(nat)만 생성한다. "
            "도구 없는 provider(openai/anthropic/ollama)는 null."
        ),
    )
    confidence_score: Optional[float] = Field(
        default=None,
        description=(
            "신뢰도 점수 (0.0~1.0). provider_supports_tools=True인 provider(nat)만 생성한다. "
            "도구 없는 provider(openai/anthropic/ollama)는 null."
        ),
    )
    reason: str = Field(default="", description="투자 결정 근거. 도구 없는 provider는 빈 문자열.")
    # provider 차원의 "능력" 신호 — 실제 도구 호출 관측이 아님 (#162):
    # - openai/anthropic/ollama: tools 파라미터 없이 모델 직접 호출 → False는 코드로 증명 가능
    # - nat=True: NAT 멀티에이전트로 라우팅됐다는 뜻이며, 그 안에서 실제 도구가
    #   실행됐는지는 이 필드가 보장하지 않는다 (NAT ReAct가 도구 없이 답할 수 있는
    #   문제 #152, raise_on_parsing_failure: false). false negative는 구조적으로
    #   불가능하지만 false positive는 가능한 비대칭 신호다.
    # - 실제 도구 호출 이력(ledger)은 #152의 몫이며 이 필드는 대신하지 않는다.
    # - 이 컬럼이 없던 구버전 행은 database._run_schema_migrations()가
    #   ALTER TABLE ... DEFAULT 0으로 채운다 — False는 "도구 미지원"과
    #   "과거 행이라 확인 불가" 둘 다를 의미하며, 절대 True로 소급되지 않는다.
    # - services.provider_supports_tools()가 provider 자체에서 파생하며,
    #   호출부가 직접 True/False를 넘기지 않는다.
    provider_supports_tools: bool = Field(
        default=False,
        description="provider가 도구(MCP/KIS/뉴스) 호출 경로로 구성돼 있는지 여부 (provider 능력 신호, 실제 호출 관측 아님).",
    )
    # #298: 2차 필터가 매긴 신호 점수. 셋 다 nullable이며 기본값을 채우지 않는다 —
    # "0과 모름"은 다른 값이다(#122·#162). null은 "채점 자체가 없었다"는 뜻이고,
    # LLM 호출·파싱 실패로 fail-open했거나, 점수화 이전에 저장된 행이거나,
    # 스케줄러를 거치지 않은 수동 분석인 경우다.
    #
    # 이 테이블에 실제로 남는 점수는 |score| >= SIGNAL_SCORE_THRESHOLD인 값뿐이다.
    # 임계값 미만이면 스케줄러가 상세 분석 자체를 건너뛰므로 리포트 행이 생기지
    # 않는다 — 즉 기본 임계값에서 0이나 ±1인 행은 구조적으로 존재할 수 없다.
    # 걸러진 신호의 점수 분포는 scheduler의 "유의미한 변화 없음(score=...)" 로그와
    # 평가셋(backend/scripts/build_signal_eval_set.py)에서 본다.
    signal_score: Optional[int] = Field(
        default=None,
        description=(
            "신호 영향도 점수 (-3~+3 정수). 감시 파이프라인의 2차 필터가 매긴다. "
            "채점하지 못했으면 null (fail-open)."
        ),
    )
    signal_reason: Optional[str] = Field(
        default=None,
        description="signal_score의 한 줄 근거. 점수가 null이면 함께 null.",
    )
    signal_uncertainty: Optional[float] = Field(
        default=None,
        description=(
            "기사별 점수의 표준편차. 기사가 2건 미만이면 흩어짐을 정의할 수 없으므로 null "
            "(0.0이 아니다 — 0.0은 '기사들이 완전히 일치했다'는 뜻)."
        ),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="생성 일시")

class Diary(SQLModel, table=True):
    """
    사용자의 개인 투자 일지 및 메모를 저장합니다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(description="일지 제목")
    content: str = Field(description="일지 내용")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="작성 일시")


class WatchlistItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_name: str = Field(unique=True, index=True)


class CatalystEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    stock_name: str = Field(index=True)
    stock_code: Optional[str] = Field(default=None, index=True)
    event_type: str = Field(index=True, description="earnings | dividend | disclosure | agm")
    event_date: date = Field(index=True)
    description: str
    source: str = Field(default="manual")
    notified: bool = Field(default=False, index=True)
    d1_notified: bool = Field(default=False)
    d0_notified: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="생성 일시",
    )
