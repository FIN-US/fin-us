from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Awaitable, Callable, Literal

from fastapi import HTTPException

from .timeutil import KST

logger = logging.getLogger(__name__)

OrderSide = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT", "MARKET"]

# 대기 주문의 앱 레벨 만료 창. PendingOrder의 성질이므로 여기 둔다 (#299).
# telegram_commands가 같은 이름으로 재수출하며(기존 import 경로 유지), order_assist도
# 여기서 직접 읽는다 — telegram_commands에 두면 order_assist와 순환 import가 된다.
# redis 쪽 PENDING_ORDER_TTL_SEC(10분)은 이 값의 10배 여유로 잡힌 별개 장치다.
ORDER_EXPIRES_AFTER = timedelta(seconds=60)


@dataclass(frozen=True)
class PendingOrder:
    chat_id: str
    stock_name: str
    stock_code: str
    side: OrderSide
    quantity: int
    # LIMIT이면 주문 조건(지정가)이다. MARKET이면 주문 조건이 아니라 **표시·기록용
    # 참고단가**로, 주문 시점 현재가가 들어온다 (#309). 시장가 주문의 체결가는 KIS
    # 현금주문 응답에 없으므로(order-cash output은 KRX_FWDG_ORD_ORGNO·ODNO·ORD_TMD뿐)
    # 이것이 주문 시점에 얻을 수 있는 최선의 단가다.
    price: int
    created_at: datetime
    order_type: OrderType = "LIMIT"
    callback_token: str = ""


# 확정·취소 콜백 데이터 접두사와 그 버튼을 만드는 함수. ORDER_EXPIRES_AFTER와 같은 이유로
# 여기 있다 — telegram_commands가 재수출하고(기존 import 경로 유지), 스케줄러의 자동
# 제안(#314)도 여기서 직접 읽는다. 이 함수가 telegram_commands의 메서드로만 있으면 자동
# 제안이 버튼을 직접 조립하게 되고, 그 순간 "확정 버튼은 한 곳에서만 만든다"가 깨진다.
# 콜백 문자열이 갈리면 _handle_callback_query가 못 알아보는 버튼이 사용자에게 나간다.
ORDER_CONFIRM_CALLBACK = "order:confirm"
ORDER_CANCEL_CALLBACK = "order:cancel"


def order_reply_markup(order: PendingOrder) -> dict[str, Any]:
    """대기 주문의 확정/취소 인라인 키보드. 수동·자동 제안이 같은 것을 쓴다."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ 확정",
                    "callback_data": f"{ORDER_CONFIRM_CALLBACK}:{order.callback_token}",
                },
                {
                    "text": "❌ 취소",
                    "callback_data": f"{ORDER_CANCEL_CALLBACK}:{order.callback_token}",
                },
            ]
        ]
    }


@dataclass(frozen=True)
class OrderExecutionResult:
    stock_code: str
    stock_name: str
    side: OrderSide
    quantity: int
    # PendingOrder.price와 같은 뜻이다 — MARKET이면 체결가가 아니라 주문 시점 현재가다.
    # TradeHistory.price로 그대로 내려가므로 0이면 "0원 거래"가 아니라 "금액 모름"이고,
    # order_assist.load_daily_usage가 그 상태에서 일 거래대금 집계를 포기한다 (#309).
    price: int
    message: str
    raw_result: str
    order_type: OrderType = "LIMIT"


class TradeRecorder:
    def __init__(self, session_factory: Callable[[], Any]):
        self.session_factory = session_factory

    def record(self, result: OrderExecutionResult) -> None:
        from .models import TradeHistory

        if result.price <= 0:
            # 여기서 막지는 않는다. 주문은 이미 나갔고, 행을 통째로 빠뜨리면 일 주문
            # **횟수** 한도까지 함께 헐거워진다 — 단가만 모르는 행이 낫다. 대신 남긴다:
            # 이 경고가 찍힌 날은 load_daily_usage가 집계를 포기해 /advise가 막힌다.
            logger.warning(
                "단가 없이 거래 이력을 기록한다 (%s %s %d주) — 오늘 /advise는 일 거래대금 "
                "집계 실패로 막힌다 (#309)",
                result.stock_code,
                result.side,
                result.quantity,
            )

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
            # 시장가에는 참고단가를 보내지 않는다. mcp-trading은 어차피 시장가면
            # ORD_UNPR을 0으로 고정하고(order.js buildCashOrderBody) 중복 방지 키에서도
            # 가격을 "0"으로 정규화하므로(order-dedup.js) 보내도 주문 결과는 같지만,
            # 기록용 값이 주문 조건처럼 읽히는 자리를 만들지 않는다 (#309).
            "price": 0 if order.order_type == "MARKET" else order.price,
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
            # 주문에 보낸 값(시장가면 0)이 아니라 대기 주문이 들고 있던 참고단가를 싣는다.
            # 여기서 0으로 덮으면 체결 기록이 다시 "금액 모름"이 된다 (#309).
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
