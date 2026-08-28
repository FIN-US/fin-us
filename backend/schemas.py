from typing import Any, Literal
from pydantic import BaseModel, Field

# AnalysisReport.urgency의 허용값. services._URGENCY_LEVELS가 이 별칭에서
# 파생하므로(get_args) 별칭 하나만 고치면 검증 집합도 함께 따라온다.
UrgencyLevel = Literal["low", "normal", "high", "critical"]


class TradingSignal(BaseModel):
    decision: str = Field(..., description="BUY, SELL, 또는 HOLD")
    confidence_score: float = Field(..., ge=0, le=1)
    reason: str
    target_stock: str


class AnalysisReport(BaseModel):
    summary: str
    # A (#162): 도구 없는 provider(openai/anthropic/ollama)는 매매 판단을 생성하지 않으므로
    # details=None이다. provider=nat만 TradingSignal을 채운다.
    details: TradingSignal | None = None
    source_news: list[str]
    source_signals: list[str] | None = None
    trading_trend: str | None = None
    urgency: UrgencyLevel = "normal"
    urgency_reason: str | None = None
    telegram_alert: bool = False
    # #162: LLM 원문 JSON에는 없는 필드. services.perform_stock_analysis가
    # analysis_from_nat_text 호출 이후 provider_supports_tools()로 파생한 값을
    # 덮어써 채운다. 기본값(None/False)은 이 스키마가 (드물게) LLM 응답 파싱
    # 경로에서 직접 쓰일 때를 위한 안전값일 뿐, /api/v1/analyze 응답에서는 항상
    # perform_stock_analysis가 실제 provider 값으로 덮어쓴다.
    provider: str | None = None
    provider_supports_tools: bool = False


class CommonResponse(BaseModel):
    status: str = "success"
    data: Any | None = None
    message: str | None = None


class DiaryCreate(BaseModel):
    title: str = Field(..., description="일지 제목")
    content: str = Field(..., description="일지 내용")
