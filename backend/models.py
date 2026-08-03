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
    decision: str = Field(description="투자 결정 (BUY/SELL/HOLD)")
    confidence_score: float = Field(description="신뢰도 점수 (0.0~1.0)")
    reason: str = Field(description="투자 결정 근거")
    provider_supports_tools: bool = Field(
        default=False,
        description=(
            "이 리포트를 만든 provider가 도구(MCP/KIS/뉴스)를 호출할 수 있는 경로로 "
            "구성돼 있는지 여부. services.provider_supports_tools()가 provider 자체에서 "
            "파생하며 호출부가 직접 True/False를 넘기지 않는다. "
            "주의: 이것은 provider 차원의 '능력' 신호이지, 이 리포트를 만들 때 실제로 "
            "도구가 호출됐다는 관측이 아니다. openai/anthropic/ollama는 tools 파라미터 "
            "없이 모델을 그대로 호출하므로 False는 코드로 증명 가능하다. 반면 nat=True는 "
            "NAT 멀티에이전트로 라우팅됐다는 뜻일 뿐이며, 그 안에서 실제로 도구가 실행됐는지는 "
            "이 필드가 보장하지 않는다 — NAT ReAct 에이전트가 도구 없이도 답을 낼 수 있는 "
            "문제(#152, finus_nat/configs/agents/*.yml 전부 raise_on_parsing_failure: false)가 "
            "열려 있는 한, false negative는 구조적으로 불가능해도 false positive는 가능하다. "
            "실제 도구 호출 이력(ledger)은 #152에서 다룰 몫이며 이 필드는 그것을 대신하지 않는다. "
            "이 컬럼이 없던 스키마에서 만들어진 기존 행은 database._run_schema_migrations()가 "
            "ALTER TABLE ... DEFAULT 0으로 채운다 — False는 '도구 미지원'과 "
            "'과거 행이라 확인 불가'를 모두 의미하며, 절대 True로 소급되지 않는다."
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
