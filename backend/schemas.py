from typing import Any, Literal
from pydantic import BaseModel, Field

class TradingSignal(BaseModel):
    decision: str = Field(..., description="BUY, SELL, 또는 HOLD")
    confidence_score: float = Field(..., ge=0, le=1)
    reason: str
    target_stock: str


class AnalysisReport(BaseModel):
    summary: str
    details: TradingSignal
    source_news: list[str]
    source_signals: list[str] | None = None
    trading_trend: str | None = None
    urgency: Literal["low", "normal", "high", "critical"] = "normal"
    urgency_reason: str | None = None
    telegram_alert: bool = False


class CommonResponse(BaseModel):
    status: str = "success"
    data: Any | None = None
    message: str | None = None


class DiaryCreate(BaseModel):
    title: str = Field(..., description="일지 제목")
    content: str = Field(..., description="일지 내용")
