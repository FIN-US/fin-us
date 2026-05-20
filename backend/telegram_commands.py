import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx
from fastapi import HTTPException
from sqlmodel import Session

from .config import (
    KIS_ORDER_ENV,
    KIS_REAL_ORDER_ENABLED,
    TRADING_MCP_PARAMS,
)
from .database import engine
from .redis_state import RedisSchedulerState, redis_state
from .services import llm_chat, run_mcp_tool
from .telegram_notifier import TELEGRAM_ALERT_MODES, TelegramNotifier, telegram_notifier
from .trading_orders import (
    KST,
    McpTradingOrderGateway,
    PendingOrder,
    TradeRecorder,
    is_korean_market_open,
)

logger = logging.getLogger(__name__)

ALERT_COMMAND_HELP = "사용법: /alerts urgent | all | off | status"
BUY_COMMAND_HELP = "사용법: /buy <종목명> <수량> <지정가>"
SELL_COMMAND_HELP = "사용법: /sell <종목명> <수량> <지정가>"
ORDER_EXPIRES_AFTER = timedelta(seconds=60)
TELEGRAM_INTERACTIVE_HELP = "\n".join(
    [
        "사용 가능한 명령:",
        "/alerts urgent|all|off|status - Telegram 알림 모드 변경",
        "/balance - 예수금·총자산·보유 종목 조회",
        "/quote <종목명> - 현재가 조회",
        "/trend <종목명> - 외국인·기관·개인 수급 조회",
        "/buy <종목명> <수량> <지정가> - 지정가 매수 주문 준비",
        "/sell <종목명> <수량> <지정가> - 지정가 매도 주문 준비",
        "/confirm - 대기 주문 확정",
        "/cancel - 대기 주문 취소",
        "일반 문장은 NAT에게 바로 질문합니다.",
    ]
)
QUOTE_COMMAND_HELP = "사용법: /quote <종목명>"
TREND_COMMAND_HELP = "사용법: /trend <종목명>"
TELEGRAM_MESSAGE_LIMIT = 4000
TELEGRAM_TRUNCATION_SUFFIX = "...(이하 생략)"
_telegram_command_task: asyncio.Task | None = None


def _telegram_text(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= TELEGRAM_MESSAGE_LIMIT:
        return stripped
    keep = TELEGRAM_MESSAGE_LIMIT - len(TELEGRAM_TRUNCATION_SUFFIX)
    return f"{stripped[:keep]}{TELEGRAM_TRUNCATION_SUFFIX}"


def _short_error(exc: Exception) -> str:
    raw = getattr(exc, "detail", str(exc))
    text = str(raw or "").strip()
    if not text:
        text = exc.__class__.__name__
    return text[:300]


def _telegram_command_parts(text: str) -> tuple[str, str, str]:
    command, _, argument = text.partition(" ")
    command_name, separator, bot_username = command.partition("@")
    return command_name.lower(), bot_username.lower() if separator else "", argument.strip()


def _create_order_gateway() -> McpTradingOrderGateway:
    order_env = "real" if KIS_ORDER_ENV == "real" else "demo"
    return McpTradingOrderGateway(
        server_params=TRADING_MCP_PARAMS,
        mcp_runner=run_mcp_tool,
        order_env=order_env,
        real_order_enabled=KIS_REAL_ORDER_ENABLED,
    )


def _create_trade_recorder() -> TradeRecorder:
    return TradeRecorder(lambda: Session(engine))


class TelegramCommandHandler:
    def __init__(
        self,
        *,
        notifier: TelegramNotifier,
        state_factory: Callable[[], Any] = redis_state,
        mcp_runner: Callable[[Any, str, dict[str, Any]], Any] = run_mcp_tool,
        llm_runner: Callable[..., Any] = llm_chat,
        order_gateway: Any | None = None,
        trade_recorder: Any | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.notifier = notifier
        self.state_factory = state_factory
        self.mcp_runner = mcp_runner
        self.llm_runner = llm_runner
        self.order_gateway = order_gateway
        self.trade_recorder = trade_recorder
        self.now_factory = now_factory or (lambda: datetime.now(KST))
        self.pending_orders: dict[str, PendingOrder] = {}

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")).strip() != self.notifier.chat_id:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        command, bot_username, argument = _telegram_command_parts(text)
        if self._matches_command(command, bot_username, "/alerts"):
            await self._handle_alerts(argument)
            return
        if self._matches_command(command, bot_username, "/help"):
            await self._send_text_or_raise(TELEGRAM_INTERACTIVE_HELP)
            return
        if self._matches_command(command, bot_username, "/balance"):
            await self._handle_balance()
            return
        if self._matches_command(command, bot_username, "/quote"):
            await self._handle_quote(argument)
            return
        if self._matches_command(command, bot_username, "/trend"):
            await self._handle_trend(argument)
            return
        if self._matches_command(command, bot_username, "/buy"):
            await self._handle_order_command("BUY", argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/sell"):
            await self._handle_order_command("SELL", argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/confirm"):
            await self._handle_confirm(str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/cancel"):
            await self._handle_cancel(str(chat.get("id", "")).strip())
            return
        if text.startswith("/"):
            await self._send_text_or_raise(TELEGRAM_INTERACTIVE_HELP)
            return

        await self._handle_chat_fallback(text, str(chat.get("id", "")).strip())

    async def _send_text_or_raise(self, text: str) -> None:
        sent = await self.notifier.send_text(text)
        if sent is False:
            raise RuntimeError("telegram send failed")

    def _matches_command(self, command: str, bot_username: str, expected: str) -> bool:
        if command != expected:
            return False
        if not bot_username:
            return True
        notifier_username = str(getattr(self.notifier, "bot_username", "") or "").lower()
        return bot_username == notifier_username

    async def _handle_alerts(self, argument: str) -> None:
        parts = argument.split()
        action = parts[0].lower() if parts else "status"
        async with self._state() as state:
            if action == "status":
                mode = await state.get_telegram_alert_mode()
                await self._send_text_or_raise(f"현재 Telegram 알림 모드: {mode}")
                return

            if action not in TELEGRAM_ALERT_MODES:
                await self._send_text_or_raise(ALERT_COMMAND_HELP)
                return

            await state.set_telegram_alert_mode(action)
            await self._send_text_or_raise(
                f"Telegram 알림 모드가 {action}(으)로 변경되었습니다."
            )

    async def _handle_balance(self) -> None:
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {})
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(_telegram_text(str(result)))

    async def _handle_quote(self, argument: str) -> None:
        if not argument:
            await self._send_text_or_raise(QUOTE_COMMAND_HELP)
            return

        stock = argument.strip()
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "get_stock_quote",
                {"stock_name": stock},
            )
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(_telegram_text(str(result)))

    async def _handle_order_command(self, side: str, argument: str, chat_id: str) -> None:
        usage = BUY_COMMAND_HELP if side == "BUY" else SELL_COMMAND_HELP
        parsed = self._parse_order_argument(argument)
        if parsed is None:
            await self._send_text_or_raise(usage)
            return

        stock_name, quantity, price = parsed
        now = self.now_factory()
        if not is_korean_market_open(now):
            await self._send_text_or_raise(
                "주문 불가: 현재 장 운영 시간이 아닙니다. (평일 09:00~15:30)"
            )
            return

        self._drop_expired_pending_order(chat_id, now)
        if chat_id in self.pending_orders:
            await self._send_text_or_raise(
                "이미 대기 중인 주문이 있습니다. /confirm 또는 /cancel로 먼저 처리하세요."
            )
            return

        await self.notifier.send_chat_action("typing")
        try:
            resolved = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "resolve_stock_code",
                {"stock_name": stock_name},
            )
            stock_code = self._extract_stock_code(str(resolved))
            if stock_code is None:
                await self._send_text_or_raise("주문 준비 실패: 종목코드를 확인할 수 없습니다.")
                return
            quote_result, balance_result = await asyncio.gather(
                self.mcp_runner(
                    TRADING_MCP_PARAMS,
                    "get_stock_quote",
                    {"stock_name": stock_name},
                ),
                self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {}),
            )
        except Exception as exc:
            await self._send_text_or_raise(f"주문 준비 실패: {_short_error(exc)}")
            return

        order = PendingOrder(
            chat_id=chat_id,
            stock_name=stock_name,
            stock_code=stock_code,
            side=side,
            quantity=quantity,
            price=price,
            created_at=now,
        )
        self.pending_orders[chat_id] = order
        await self._send_text_or_raise(
            self._format_order_prompt(order, str(quote_result), str(balance_result))
        )

    async def _handle_cancel(self, chat_id: str) -> None:
        self._drop_expired_pending_order(chat_id, self.now_factory())
        if chat_id not in self.pending_orders:
            await self._send_text_or_raise("취소할 대기 주문이 없습니다.")
            return

        self.pending_orders.pop(chat_id, None)
        await self._send_text_or_raise("대기 주문을 취소했습니다.")

    async def _handle_confirm(self, chat_id: str) -> None:
        self._drop_expired_pending_order(chat_id, self.now_factory())
        order = self.pending_orders.get(chat_id)
        if order is None:
            await self._send_text_or_raise("확정할 대기 주문이 없습니다.")
            return
        if self.order_gateway is None:
            await self._send_text_or_raise("주문 실행 설정이 준비되지 않았습니다.")
            return

        await self.notifier.send_chat_action("typing")
        try:
            result = await self.order_gateway.place_order(order)
        except Exception as exc:
            if isinstance(exc, HTTPException) and exc.status_code == 403:
                await self._send_text_or_raise(f"주문 실패: {_short_error(exc)}")
                return

            self.pending_orders.pop(chat_id, None)
            await self._send_text_or_raise(
                "주문 실패 또는 상태 확인 필요: "
                f"{_short_error(exc)}\n"
                "중복 주문 방지를 위해 대기 주문을 제거했습니다."
            )
            return

        self.pending_orders.pop(chat_id, None)
        record_warning = ""
        if self.trade_recorder is not None:
            try:
                self.trade_recorder.record(result)
            except Exception as exc:
                logger.warning("Trade history recording failed: %s", exc)
                record_warning = f"\n거래 이력 기록 실패: {_short_error(exc)}"
        await self._send_text_or_raise(f"주문 완료: {result.message}{record_warning}")

    def _parse_order_argument(self, argument: str) -> tuple[str, int, int] | None:
        parts = argument.split()
        if len(parts) < 3:
            return None
        stock_name = " ".join(parts[:-2]).strip()
        raw_quantity, raw_price = parts[-2:]
        if not stock_name:
            return None
        try:
            quantity = int(raw_quantity.replace(",", ""))
            price = int(raw_price.replace(",", ""))
        except ValueError:
            return None
        if quantity <= 0 or price <= 0:
            return None
        return stock_name, quantity, price

    def _drop_expired_pending_order(self, chat_id: str, now: datetime) -> None:
        order = self.pending_orders.get(chat_id)
        if order is None:
            return
        created_at = order.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=KST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=KST)
        if now.astimezone(KST) - created_at.astimezone(KST) > ORDER_EXPIRES_AFTER:
            self.pending_orders.pop(chat_id, None)

    def _extract_stock_code(self, text: str) -> str | None:
        match = re.search(r"\b(\d{6})\b", text)
        return match.group(1) if match else None

    def _format_order_prompt(
        self,
        order: PendingOrder,
        quote_result: str,
        balance_result: str,
    ) -> str:
        side_text = "매수" if order.side == "BUY" else "매도"
        amount = order.quantity * order.price
        lines = [
            f"{order.stock_name} {side_text} 주문 확인",
            f"종목코드: {order.stock_code}",
            f"수량: {order.quantity:,}주",
            f"지정가: {order.price:,}원",
            f"주문금액: {amount:,}원",
        ]

        current_price = self._first_line_containing(
            str(quote_result),
            ("현재가", "price", "Price"),
        )
        if current_price:
            lines.append(current_price)

        balance = self._first_line_containing(
            str(balance_result),
            ("주문가능", "예수금", "총자산", "balance"),
        )
        if balance:
            lines.append(balance)

        lines.extend(
            [
                "",
                "/confirm 입력 시 대기 주문을 확정합니다.",
                "/cancel 입력 시 대기 주문을 취소합니다.",
                "이 주문은 60초 후 만료됩니다.",
            ]
        )
        return "\n".join(lines)

    def _first_line_containing(self, text: str, needles: tuple[str, ...]) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and any(needle in stripped for needle in needles):
                return stripped
        return None

    async def _handle_trend(self, argument: str) -> None:
        if not argument:
            await self._send_text_or_raise(TREND_COMMAND_HELP)
            return

        stock = argument.strip()
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(
                TRADING_MCP_PARAMS,
                "get_investor_trading",
                {"stock_name": stock},
            )
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(_telegram_text(str(result)))

    async def _handle_chat_fallback(self, text: str, chat_id: str) -> None:
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.llm_runner(
                "nat",
                text,
                conversation_id=f"telegram:{chat_id}",
            )
        except Exception as exc:
            await self._send_text_or_raise(f"응답 생성 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(_telegram_text(str(result)))

    @asynccontextmanager
    async def _state(self):
        state = self.state_factory()
        if hasattr(state, "__aenter__"):
            async with state as opened:
                yield opened
        else:
            yield state


class TelegramCommandPoller:
    def __init__(
        self,
        *,
        notifier: TelegramNotifier = telegram_notifier,
        handler: TelegramCommandHandler | None = None,
    ):
        self.notifier = notifier
        if handler is None:
            handler = TelegramCommandHandler(
                notifier=notifier,
                order_gateway=_create_order_gateway(),
                trade_recorder=_create_trade_recorder(),
            )
        self.handler = handler
        self.offset: int | None = None

    async def run(self) -> None:
        if not self.notifier.enabled:
            return
        load_bot_username = getattr(self.notifier, "load_bot_username", None)
        if callable(load_bot_username):
            await load_bot_username()

        while True:
            try:
                updates = await self._get_updates()
                for update in updates:
                    update_id = update.get("update_id")
                    await self.handler.handle_update(update)
                    if isinstance(update_id, int):
                        self.offset = update_id + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Telegram command polling failed: %s", exc)
                await asyncio.sleep(5)

    async def _get_updates(self) -> list[dict[str, Any]]:
        url = f"https://api.telegram.org/bot{self.notifier.bot_token}/getUpdates"
        payload: dict[str, Any] = {"timeout": 25}
        if self.offset is not None:
            payload["offset"] = self.offset

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
        if body.get("ok") is not True:
            return []
        return body.get("result") or []


def start_telegram_commands() -> None:
    global _telegram_command_task
    if not telegram_notifier.enabled or _telegram_command_task is not None:
        return
    poller = TelegramCommandPoller()
    _telegram_command_task = asyncio.create_task(poller.run())


async def stop_telegram_commands() -> None:
    global _telegram_command_task
    if _telegram_command_task is None:
        return
    _telegram_command_task.cancel()
    try:
        await _telegram_command_task
    except asyncio.CancelledError:
        pass
    finally:
        _telegram_command_task = None
