from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Awaitable, Callable, Literal

from fastapi import HTTPException

from .timeutil import KST

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT", "MARKET"]


@dataclass(frozen=True)
class PendingOrder:
    chat_id: str
    stock_name: str
    stock_code: str
    side: OrderSide
    quantity: int
    price: int
    created_at: datetime
    order_type: OrderType = "LIMIT"
    callback_token: str = ""
    # 아래 두 필드는 #247(멱등성) 전용이다. 전송 실패(TelegramSendError)로 poller가
    # handle_update를 재시도할 때, 이미 확정된 부수효과(저장·체결)를 다시 실행하지 않고
    # "같은 사용자 응답"으로 수렴시키기 위한 상태다.
    #
    # prompt_delivered: set_if_absent로 주문이 저장된 뒤 확인 프롬프트 전송까지
    # 성공했는지. False인 채로 재시도를 만나면 새 주문을 또 만들지 않고 이
    # 프롬프트를 재전송한다(telegram_commands._handle_order_command).
    prompt_delivered: bool = False
    # settled: place_order(체결)까지 끝났는지. True면 claim()이 이 레코드를 돌려줘도
    # place_order를 다시 부르지 않고 settled_message만 재전송한다
    # (telegram_commands._handle_confirm).
    settled: bool = False
    settled_message: str | None = None


@dataclass(frozen=True)
class OrderExecutionResult:
    stock_code: str
    stock_name: str
    side: OrderSide
    quantity: int
    price: int
    message: str
    raw_result: str
    order_type: OrderType = "LIMIT"


class TradeRecorder:
    def __init__(self, session_factory: Callable[[], Any]):
        self.session_factory = session_factory

    def record(self, result: OrderExecutionResult) -> None:
        from .models import TradeHistory

        session = self.session_factory()
        try:
            session.add(
                TradeHistory(
                    stock_code=result.stock_code,
                    stock_name=result.stock_name,
                    trade_type=result.side,
                    quantity=result.quantity,
                    price=float(result.price),
                )
            )
            session.commit()
        except Exception:
            rollback = getattr(session, "rollback", None)
            if callable(rollback):
                rollback()
            raise
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()


McpRunner = Callable[[Any, str, dict[str, Any]], Awaitable[str]]


def is_korean_market_open(now: datetime | None = None) -> bool:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)

    if current.weekday() >= 5:
        return False
    return time(9, 0) <= current.time() <= time(15, 30)


class McpTradingOrderGateway:
    def __init__(
        self,
        *,
        server_params: Any,
        mcp_runner: McpRunner,
        order_env: Literal["real", "demo"],
        real_order_enabled: bool,
    ):
        self.server_params = server_params
        self.mcp_runner = mcp_runner
        self.order_env = order_env
        self.real_order_enabled = real_order_enabled

    async def place_order(self, order: PendingOrder) -> OrderExecutionResult:
        if self.order_env == "real" and not self.real_order_enabled:
            raise HTTPException(
                status_code=403,
                detail="실계좌 주문은 KIS_REAL_ORDER_ENABLED=true 설정이 필요합니다.",
            )

        arguments: dict[str, Any] = {
            "stock_name": order.stock_name,
            "stock_code": order.stock_code,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "order_env": self.order_env,
        }
        if order.order_type == "MARKET":
            arguments["order_type"] = "MARKET"

        raw_result = await self.mcp_runner(
            self.server_params,
            "place_order",
            arguments,
        )

        return OrderExecutionResult(
            stock_code=order.stock_code,
            stock_name=order.stock_name,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            message=_extract_order_message(raw_result),
            raw_result=raw_result,
            order_type=order.order_type,
        )


def _extract_order_message(raw_result: str) -> str:
    text = str(raw_result or "").strip()
    if not text:
        return "주문 요청이 접수되었습니다."

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:500]

    if isinstance(data, dict):
        for key in ("msg1", "message", "rt_msg", "output"):
            value = data.get(key)
            if value:
                return str(value)[:500]
    return text[:500]
