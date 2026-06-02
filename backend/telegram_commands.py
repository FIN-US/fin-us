import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote as _url_quote

import httpx
from fastapi import HTTPException
from sqlmodel import Session

from .config import (
    DART_MCP_PARAMS,
    KIS_ORDER_ENV,
    KIS_REAL_ORDER_ENABLED,
    NEWS_MCP_PARAMS,
    TRADING_MCP_PARAMS,
    VISUALIZATION_URL,
)
from .database import engine
from .redis_state import RedisSchedulerState, redis_state
from .services import llm_chat, run_mcp_tool
from .watchlist_repo import SqliteWatchlistRepo
from .telegram_notifier import TELEGRAM_ALERT_MODES, TelegramNotifier, telegram_notifier
from .trading_orders import (
    KST,
    McpTradingOrderGateway,
    OrderType,
    PendingOrder,
    TradeRecorder,
    is_korean_market_open,
)

logger = logging.getLogger(__name__)

ALERT_COMMAND_HELP = "사용법: /alerts urgent | all | off | status"
BUY_COMMAND_HELP = "사용법: /buy <종목명> <수량> [지정가]"
SELL_COMMAND_HELP = "사용법: /sell <종목명> <수량> [지정가]"
WATCH_COMMAND_HELP = "사용법: /watch add <종목명> | remove <종목명> | list"
EARNINGS_COMMAND_HELP = "사용법: /earnings <종목명> [기간]  예: /earnings 삼성전자 2025Q1"
NATURAL_ORDER_HELP = "자연어 주문을 해석할 수 없습니다. /buy 또는 /sell 형식으로 입력하세요."
ORDER_EXPIRES_AFTER = timedelta(seconds=60)
ORDER_CONFIRM_CALLBACK = "order:confirm"
ORDER_CANCEL_CALLBACK = "order:cancel"
ORDER_STALE_CALLBACK_TEXT = "이전 주문 버튼입니다. 최신 주문 메시지에서 다시 선택하세요."
ALERT_CALLBACK_PREFIX = "alerts:"
BALANCE_REFRESH_CALLBACK = "balance:refresh"
TRADE_CALLBACK_PREFIX = "trade:"
LOOKUP_CALLBACK_PREFIX = "lookup:"
WATCH_LIST_CALLBACK = "watch:list"
WATCH_LIST_QUOTE_DELAY_SECONDS = 1.1
MARKET_QUOTE_CALLBACK = "market:quote"
MARKET_TREND_CALLBACK = "market:trend"
MARKET_STALE_CALLBACK_TEXT = "이전 조회 버튼입니다. 최신 조회 메시지에서 다시 선택하세요."
MARKET_CALLBACK_LIMIT = 100
ALERT_MODE_EMOJIS = {
    "urgent": "🚨",
    "all": "📣",
    "off": "🔕",
}
TELEGRAM_INTERACTIVE_HELP = "\n".join(
    [
        "사용 가능한 명령:",
        "/alerts urgent|all|off|status - Telegram 알림 모드 변경",
        "/balance - 예수금·총자산·보유 종목 조회",
        "/watch add <종목명>|remove <종목명>|list - 관심 종목 관리",
        "/trade - 매수·매도 주문 입력 안내",
        "/lookup - 현재가·수급 조회 입력 안내",
        "/visualize - Unity 포트폴리오 시각화 링크",
        "/quote <종목명> - 현재가 조회",
        "/trend <종목명> - 외국인·기관·개인 수급 조회",
        "/earnings <종목명> [기간] - DART 실적·뉴스 분석",
        "/buy <종목명> <수량> [지정가] - 매수 주문 준비",
        "/sell <종목명> <수량> [지정가] - 매도 주문 준비",
        "/confirm - 대기 주문 확정",
        "/cancel - 대기 주문 취소",
        "일반 문장은 NAT에게 바로 질문합니다.",
    ]
)
TELEGRAM_BOT_COMMANDS = [
    {"command": "help", "description": "사용 가능한 명령 확인"},
    {"command": "balance", "description": "예수금·총자산·보유 종목 조회"},
    {"command": "watch", "description": "관심 종목 추가·삭제·조회 (add/remove/list)"},
    {"command": "quote", "description": "종목 현재가 조회"},
    {"command": "trend", "description": "종목 외국인·기관·개인 수급 조회"},
    {"command": "earnings", "description": "DART 실적·뉴스 분석"},
    {"command": "alerts", "description": "Telegram 알림 모드 변경"},
    {"command": "visualize", "description": "Unity 포트폴리오 시각화 링크"},
    {"command": "trade", "description": "매수·매도 주문 입력 안내"},
    {"command": "lookup", "description": "현재가·수급 조회 입력 안내"},
    {"command": "buy", "description": "매수 주문 준비 (예: /buy 삼성전자 1)"},
    {"command": "sell", "description": "매도 주문 준비 (예: /sell 삼성전자 1)"},
    {"command": "confirm", "description": "대기 주문 확정"},
    {"command": "cancel", "description": "대기 주문 취소"},
]
QUOTE_COMMAND_HELP = "사용법: /quote <종목명>"
TREND_COMMAND_HELP = "사용법: /trend <종목명>"
TRADE_COMMAND_HELP = "\n".join(
    [
        "매매 주문 입력 안내:",
        f"{BUY_COMMAND_HELP}  예: /buy 삼성전자 1",
        f"{SELL_COMMAND_HELP}  예: /sell NAVER 1 200000",
        "주문은 실제 제출 전 반드시 확정이 필요합니다.",
    ]
)
LOOKUP_COMMAND_HELP = "\n".join(
    [
        "조회 입력 안내:",
        f"{QUOTE_COMMAND_HELP}  예: /quote 삼성전자",
        f"{TREND_COMMAND_HELP}  예: /trend 삼성전자",
    ]
)
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


def _default_watchlist_repo() -> SqliteWatchlistRepo:
    return SqliteWatchlistRepo(lambda: Session(engine))


class TelegramCommandHandler:
    def __init__(
        self,
        *,
        notifier: TelegramNotifier,
        state_factory: Callable[[], Any] = redis_state,
        watchlist_repo: Any | None = None,
        mcp_runner: Callable[[Any, str, dict[str, Any]], Any] = run_mcp_tool,
        llm_runner: Callable[..., Any] = llm_chat,
        order_gateway: Any | None = None,
        trade_recorder: Any | None = None,
        now_factory: Callable[[], datetime] | None = None,
        visualization_url: str = VISUALIZATION_URL,
    ):
        self.notifier = notifier
        self.state_factory = state_factory
        self.watchlist_repo = watchlist_repo if watchlist_repo is not None else _default_watchlist_repo()
        self.mcp_runner = mcp_runner
        self.llm_runner = llm_runner
        self.order_gateway = order_gateway
        self.trade_recorder = trade_recorder
        self.now_factory = now_factory or (lambda: datetime.now(KST))
        self.visualization_url = visualization_url.strip()
        self.pending_orders: dict[str, PendingOrder] = {}
        self.market_callbacks: dict[str, tuple[str, str]] = {}

    async def handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query") or {}
        if callback_query:
            await self._handle_callback_query(callback_query)
            return

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
            await self._send_text_or_raise(
                TELEGRAM_INTERACTIVE_HELP,
                reply_markup=self._help_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/balance"):
            await self._handle_balance()
            return
        if self._matches_command(command, bot_username, "/watch"):
            await self._handle_watch(argument)
            return
        if self._matches_command(command, bot_username, "/trade"):
            await self._send_text_or_raise(
                TRADE_COMMAND_HELP,
                reply_markup=self._trade_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/lookup"):
            await self._send_text_or_raise(
                LOOKUP_COMMAND_HELP,
                reply_markup=self._lookup_reply_markup(),
            )
            return
        if self._matches_command(command, bot_username, "/visualize"):
            await self._handle_visualize()
            return
        if self._matches_command(command, bot_username, "/quote"):
            await self._handle_quote(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/trend"):
            await self._handle_trend(argument, str(chat.get("id", "")).strip())
            return
        if self._matches_command(command, bot_username, "/earnings"):
            await self._handle_earnings(argument, str(chat.get("id", "")).strip())
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
            await self._send_text_or_raise(
                TELEGRAM_INTERACTIVE_HELP,
                reply_markup=self._help_reply_markup(),
            )
            return

        if self._looks_like_natural_order(text):
            natural_order = self._parse_natural_order_text(text)
            if natural_order is None:
                await self._send_text_or_raise(NATURAL_ORDER_HELP)
                return

            side, order_argument = natural_order
            await self._handle_order_command(
                side,
                order_argument,
                str(chat.get("id", "")).strip(),
            )
            return

        await self._handle_chat_fallback(text, str(chat.get("id", "")).strip())

    async def _send_text_or_raise(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        sent = await self.notifier.send_text(text, reply_markup=reply_markup)
        if sent is False:
            raise RuntimeError("telegram send failed")

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        if chat_id != self.notifier.chat_id:
            return

        callback_query_id = str(callback_query.get("id", "")).strip()
        data = str(callback_query.get("data") or "").strip()
        if data == ORDER_CONFIRM_CALLBACK or data.startswith(f"{ORDER_CONFIRM_CALLBACK}:"):
            await self._handle_order_callback(
                callback_query_id,
                chat_id,
                "confirm",
                data,
            )
            return
        if data == ORDER_CANCEL_CALLBACK or data.startswith(f"{ORDER_CANCEL_CALLBACK}:"):
            await self._handle_order_callback(
                callback_query_id,
                chat_id,
                "cancel",
                data,
            )
            return

        if data.startswith(ALERT_CALLBACK_PREFIX):
            await self._handle_alerts_callback(callback_query_id, data)
            return

        if data == BALANCE_REFRESH_CALLBACK:
            await self._answer_callback_query(callback_query_id)
            await self._handle_balance()
            return

        if data == WATCH_LIST_CALLBACK:
            await self._answer_callback_query(callback_query_id)
            await self._handle_watch("list")
            return

        if data.startswith(TRADE_CALLBACK_PREFIX):
            await self._handle_trade_callback(callback_query_id, data)
            return

        if data.startswith(LOOKUP_CALLBACK_PREFIX):
            await self._handle_lookup_callback(callback_query_id, data)
            return

        if data.startswith(f"{MARKET_QUOTE_CALLBACK}:"):
            await self._handle_market_callback(callback_query_id, chat_id, "quote", data)
            return

        if data.startswith(f"{MARKET_TREND_CALLBACK}:"):
            await self._handle_market_callback(callback_query_id, chat_id, "trend", data)
            return

        await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")

    async def _handle_order_callback(
        self,
        callback_query_id: str,
        chat_id: str,
        action: str,
        data: str,
    ) -> None:
        order = self.pending_orders.get(chat_id)
        token = self._extract_order_callback_token(data)
        if order is None or not token or token != order.callback_token:
            await self._answer_callback_query(
                callback_query_id,
                text=ORDER_STALE_CALLBACK_TEXT,
            )
            return

        await self._answer_callback_query(callback_query_id)
        if action == "confirm":
            await self._handle_confirm(chat_id)
            return
        await self._handle_cancel(chat_id)

    async def _answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
    ) -> None:
        if not callback_query_id:
            return
        await self.notifier.answer_callback_query(callback_query_id, text=text)

    async def _handle_alerts_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(ALERT_CALLBACK_PREFIX).strip()
        if action != "status" and action not in TELEGRAM_ALERT_MODES:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._handle_alerts(action)

    async def _handle_visualize(self) -> None:
        if not self.visualization_url:
            await self._send_text_or_raise(
                "시각화 URL이 설정되지 않았습니다. VISUALIZATION_URL 환경변수를 설정하세요."
            )
            return

        await self._send_text_or_raise(f"Unity 포트폴리오 시각화:\n{self.visualization_url}")

    async def _handle_trade_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(TRADE_CALLBACK_PREFIX).strip()
        if action == "menu":
            text = TRADE_COMMAND_HELP
            reply_markup = self._trade_reply_markup()
        elif action == "buy":
            text = BUY_COMMAND_HELP
            reply_markup = None
        elif action == "sell":
            text = SELL_COMMAND_HELP
            reply_markup = None
        else:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._send_text_or_raise(text, reply_markup=reply_markup)

    async def _handle_lookup_callback(
        self,
        callback_query_id: str,
        data: str,
    ) -> None:
        action = data.removeprefix(LOOKUP_CALLBACK_PREFIX).strip()
        if action == "menu":
            text = LOOKUP_COMMAND_HELP
            reply_markup = self._lookup_reply_markup()
        elif action == "quote":
            text = QUOTE_COMMAND_HELP
            reply_markup = None
        elif action == "trend":
            text = TREND_COMMAND_HELP
            reply_markup = None
        else:
            await self._answer_callback_query(callback_query_id, text="지원하지 않는 버튼입니다.")
            return

        await self._answer_callback_query(callback_query_id)
        await self._send_text_or_raise(text, reply_markup=reply_markup)

    async def _handle_market_callback(
        self,
        callback_query_id: str,
        chat_id: str,
        action: str,
        data: str,
    ) -> None:
        token = self._extract_callback_token(data)
        context = self.market_callbacks.pop(token, None) if token else None
        if context is None:
            await self._answer_callback_query(
                callback_query_id,
                text=MARKET_STALE_CALLBACK_TEXT,
            )
            return

        callback_chat_id, stock = context
        if callback_chat_id != chat_id:
            await self._answer_callback_query(
                callback_query_id,
                text=MARKET_STALE_CALLBACK_TEXT,
            )
            return

        await self._answer_callback_query(callback_query_id)
        if action == "quote":
            await self._handle_quote(stock, chat_id)
            return
        await self._handle_trend(stock, chat_id)

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
                await self._send_text_or_raise(
                    f"현재 Telegram 알림 모드: {self._format_alert_mode(mode)}",
                    reply_markup=self._alerts_reply_markup(),
                )
                return

            if action not in TELEGRAM_ALERT_MODES:
                await self._send_text_or_raise(
                    ALERT_COMMAND_HELP,
                    reply_markup=self._alerts_reply_markup(),
                )
                return

            await state.set_telegram_alert_mode(action)
            await self._send_text_or_raise(
                f"Telegram 알림 모드가 {self._format_alert_mode(action)}(으)로 변경되었습니다.",
                reply_markup=self._alerts_reply_markup(),
            )

    async def _handle_watch(self, argument: str) -> None:
        parts = argument.split(None, 1)
        subcommand = parts[0].lower() if parts else "list"
        stock = parts[1].strip() if len(parts) > 1 else ""

        if subcommand == "list":
            watchlist = await self.watchlist_repo.get_watchlist()
            if not watchlist:
                text = "관심 종목이 없습니다.\n/watch add <종목명> 으로 추가하세요."
            else:
                lines = []
                for index, stock in enumerate(watchlist):
                    if index > 0:
                        await asyncio.sleep(WATCH_LIST_QUOTE_DELAY_SECONDS)
                    try:
                        raw = await self.mcp_runner(
                            TRADING_MCP_PARAMS, "get_stock_quote", {"stock_name": stock}
                        )
                        summary = self._parse_quote_summary(str(raw))
                        if summary:
                            price, rate = summary
                            try:
                                rate_float = float(rate.rstrip("%"))
                            except ValueError:
                                line = f"• {stock}  (조회 실패)"
                                lines.append(line)
                                continue
                            if rate_float < 0:
                                line = f"🔵 {stock}  {price}  ▼ {rate}"
                            elif rate_float == 0.0:
                                line = f"⬜ {stock}  {price}  {rate}"
                            else:
                                sign = "" if rate.startswith("+") else "+"
                                line = f"🔴 {stock}  {price}  ▲ {sign}{rate}"
                        else:
                            line = f"• {stock}  (조회 실패)"
                    except Exception:
                        line = f"• {stock}  (조회 실패)"
                    lines.append(line)
                text = "관심 종목 목록:\n" + "\n".join(lines)
            await self._send_text_or_raise(text, reply_markup=self._watch_reply_markup())
            return

        if subcommand == "add":
            if not stock:
                await self._send_text_or_raise(WATCH_COMMAND_HELP)
                return
            await self.watchlist_repo.add_to_watchlist(stock)
            await self._send_text_or_raise(
                f"관심 종목에 {stock}을(를) 추가했습니다.",
                reply_markup=self._watch_reply_markup(),
            )
            return

        if subcommand == "remove":
            if not stock:
                await self._send_text_or_raise(WATCH_COMMAND_HELP)
                return
            await self.watchlist_repo.remove_from_watchlist(stock)
            await self._send_text_or_raise(
                f"관심 종목에서 {stock}을(를) 삭제했습니다.",
                reply_markup=self._watch_reply_markup(),
            )
            return

        await self._send_text_or_raise(WATCH_COMMAND_HELP)

    async def _handle_balance(self) -> None:
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {})
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return
        await self._send_text_or_raise(
            _telegram_text(str(result)),
            reply_markup=self._balance_reply_markup(),
        )

    async def _handle_quote(self, argument: str, chat_id: str) -> None:
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
        await self._send_text_or_raise(
            _telegram_text(str(result)),
            reply_markup=self._market_reply_markup("trend", chat_id, stock),
        )

    async def _handle_order_command(self, side: str, argument: str, chat_id: str) -> None:
        usage = BUY_COMMAND_HELP if side == "BUY" else SELL_COMMAND_HELP
        parsed = self._parse_order_argument(argument)
        if parsed is None:
            await self._send_text_or_raise(usage)
            return

        stock_name, quantity, price, order_type = parsed
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
            order_type=order_type,
            callback_token=secrets.token_urlsafe(8),
        )
        self.pending_orders[chat_id] = order
        await self._send_text_or_raise(
            self._format_order_prompt(order, str(quote_result), str(balance_result)),
            reply_markup=self._order_reply_markup(order),
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

    def _parse_order_argument(self, argument: str) -> tuple[str, int, int, OrderType] | None:
        parts = argument.split()
        if len(parts) < 2:
            return None

        last_value = self._parse_positive_int(parts[-1])
        if last_value is None:
            return None

        previous_value = self._parse_positive_int(parts[-2]) if len(parts) >= 3 else None
        if previous_value is None:
            stock_name = " ".join(parts[:-1]).strip()
            quantity = last_value
            price = 0
            order_type: OrderType = "MARKET"
        else:
            stock_name = " ".join(parts[:-2]).strip()
            quantity = previous_value
            price = last_value
            order_type = "LIMIT"

        if not stock_name:
            return None
        return stock_name, quantity, price, order_type

    def _looks_like_natural_order(self, text: str) -> bool:
        return (
            ("매수" in text or "매도" in text)
            and re.search(r"\d[\d,]*\s*주", text) is not None
        )

    def _parse_natural_order_text(self, text: str) -> tuple[str, str] | None:
        buy_count = text.count("매수")
        sell_count = text.count("매도")
        if buy_count + sell_count != 1:
            return None
        side = "BUY" if buy_count == 1 else "SELL"

        quantity_match = re.search(r"(?P<quantity>\d[\d,]*)\s*주", text)
        if quantity_match is None:
            return None
        quantity = self._parse_positive_int(quantity_match.group("quantity"))
        if quantity is None:
            return None

        has_market = "시장가" in text
        price_matches = list(re.finditer(r"(?P<price>\d[\d,]*)\s*원", text))
        if has_market and price_matches:
            return None
        price = 0
        if price_matches:
            price = self._parse_positive_int(price_matches[-1].group("price")) or 0
            if price <= 0:
                return None

        stock_name = text[: quantity_match.start()].strip()
        if not stock_name:
            side_word = "매수" if side == "BUY" else "매도"
            side_index = text.find(side_word)
            if side_index < quantity_match.start():
                stock_name = text[
                    side_index + len(side_word) : quantity_match.start()
                ].strip()
        if not stock_name:
            return None

        if price > 0:
            return side, f"{stock_name} {quantity} {price}"
        return side, f"{stock_name} {quantity}"

    def _parse_positive_int(self, raw_value: str) -> int | None:
        try:
            value = int(raw_value.replace(",", ""))
        except ValueError:
            return None
        return value if value > 0 else None

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

    def _extract_order_callback_token(self, data: str) -> str | None:
        if data in {ORDER_CONFIRM_CALLBACK, ORDER_CANCEL_CALLBACK}:
            return None
        return self._extract_callback_token(data)

    def _extract_callback_token(self, data: str) -> str | None:
        _, separator, token = data.rpartition(":")
        if not separator:
            return None
        return token.strip() or None

    def _help_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "💰 잔고", "callback_data": BALANCE_REFRESH_CALLBACK},
                    {"text": "🔔 알림", "callback_data": f"{ALERT_CALLBACK_PREFIX}status"},
                ],
                [
                    {"text": "🧾 매매", "callback_data": f"{TRADE_CALLBACK_PREFIX}menu"},
                    {"text": "🔎 조회", "callback_data": f"{LOOKUP_CALLBACK_PREFIX}menu"},
                ],
            ]
        }

    def _alerts_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🚨 긴급만", "callback_data": f"{ALERT_CALLBACK_PREFIX}urgent"},
                    {"text": "📣 전체", "callback_data": f"{ALERT_CALLBACK_PREFIX}all"},
                    {"text": "🔕 끄기", "callback_data": f"{ALERT_CALLBACK_PREFIX}off"},
                ],
                [
                    {"text": "🔎 현재 상태", "callback_data": f"{ALERT_CALLBACK_PREFIX}status"}
                ],
            ]
        }

    def _format_alert_mode(self, mode: str) -> str:
        emoji = ALERT_MODE_EMOJIS.get(mode)
        if emoji is None:
            return mode
        return f"{emoji} {mode}"

    def _balance_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "🔄 새로고침", "callback_data": BALANCE_REFRESH_CALLBACK}]
            ]
        }

    def _watch_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "📋 목록 새로고침", "callback_data": WATCH_LIST_CALLBACK}]
            ]
        }

    def _trade_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🛒 매수 입력법", "callback_data": f"{TRADE_CALLBACK_PREFIX}buy"},
                    {"text": "💸 매도 입력법", "callback_data": f"{TRADE_CALLBACK_PREFIX}sell"},
                ]
            ]
        }

    def _lookup_reply_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "💵 현재가 입력법",
                        "callback_data": f"{LOOKUP_CALLBACK_PREFIX}quote",
                    },
                    {
                        "text": "📊 수급 입력법",
                        "callback_data": f"{LOOKUP_CALLBACK_PREFIX}trend",
                    },
                ]
            ]
        }

    def _market_reply_markup(
        self,
        action: str,
        chat_id: str,
        stock: str,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(8)
        if len(self.market_callbacks) >= MARKET_CALLBACK_LIMIT:
            self.market_callbacks.pop(next(iter(self.market_callbacks)), None)
        self.market_callbacks[token] = (chat_id, stock)
        if action == "quote":
            return {
                "inline_keyboard": [
                    [
                        {
                            "text": "💵 현재가 보기",
                            "callback_data": f"{MARKET_QUOTE_CALLBACK}:{token}",
                        }
                    ]
                ]
            }
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "📊 수급 보기",
                        "callback_data": f"{MARKET_TREND_CALLBACK}:{token}",
                    }
                ]
            ]
        }

    def _order_reply_markup(self, order: PendingOrder) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ 확정",
                        "callback_data": (
                            f"{ORDER_CONFIRM_CALLBACK}:{order.callback_token}"
                        ),
                    },
                    {
                        "text": "❌ 취소",
                        "callback_data": (
                            f"{ORDER_CANCEL_CALLBACK}:{order.callback_token}"
                        ),
                    },
                ]
            ]
        }

    def _format_order_prompt(
        self,
        order: PendingOrder,
        quote_result: str,
        balance_result: str,
    ) -> str:
        side_text = "매수" if order.side == "BUY" else "매도"
        lines = [
            f"{order.stock_name} {side_text} 주문 확인",
            f"종목코드: {order.stock_code}",
            f"수량: {order.quantity:,}주",
            f"주문유형: {'시장가' if order.order_type == 'MARKET' else '지정가'}",
        ]
        if order.order_type == "LIMIT":
            amount = order.quantity * order.price
            lines.extend(
                [
                    f"지정가: {order.price:,}원",
                    f"주문금액: {amount:,}원",
                ]
            )

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

    def _parse_quote_summary(self, raw: str) -> tuple[str, str] | None:
        price_line = self._first_line_containing(raw, ("현재가:",))
        rate_line = self._first_line_containing(raw, ("전일 대비:",))
        if not price_line or not rate_line:
            return None
        price_match = re.search(r"현재가:\s*(.+)", price_line)
        rate_match = re.search(r"\(([^)]+%)\)", rate_line)
        if not price_match or not rate_match:
            return None
        return price_match.group(1).strip(), rate_match.group(1).strip()

    async def _handle_trend(self, argument: str, chat_id: str) -> None:
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
        await self._send_text_or_raise(
            _telegram_text(str(result)),
            reply_markup=self._market_reply_markup("quote", chat_id, stock),
        )

    async def _handle_earnings(self, argument: str, chat_id: str) -> None:
        parsed = self._parse_earnings_argument(argument)
        if parsed is None:
            await self._send_text_or_raise(EARNINGS_COMMAND_HELP)
            return

        stock, period = parsed
        dart_arguments: dict[str, Any] = {"stock_name": stock}
        if period:
            dart_arguments["period"] = period

        await self.notifier.send_chat_action("typing")
        try:
            dart_result, news_result = await asyncio.gather(
                self.mcp_runner(DART_MCP_PARAMS, "get_earnings_report", dart_arguments),
                self.mcp_runner(NEWS_MCP_PARAMS, "get_market_news", {"stock_name": stock}),
            )
            result = await self.llm_runner(
                "nat",
                self._earnings_analysis_prompt(stock, period, str(dart_result), str(news_result)),
                conversation_id=f"telegram:{chat_id}:earnings:{_url_quote(stock, safe='')}",
            )
        except Exception as exc:
            await self._send_text_or_raise(f"조회 실패: {_short_error(exc)}")
            return

        await self._send_text_or_raise(_telegram_text(self._format_earnings_response(str(result))))

    def _parse_earnings_argument(self, argument: str) -> tuple[str, str | None] | None:
        parts = argument.split()
        if not parts:
            return None

        period = None
        if re.match(r"^\d{4}(?:[-_\s]?Q[1-4]|[-_\s]?FY)$", parts[-1], flags=re.IGNORECASE):
            period = parts[-1].upper().replace("-", "").replace("_", "")
            parts = parts[:-1]

        stock = " ".join(parts).strip()
        if not stock:
            return None
        return stock, period

    def _earnings_analysis_prompt(
        self,
        stock: str,
        period: str | None,
        dart_result: str,
        news_result: str,
    ) -> str:
        period_line = f"조회 기간: {period}" if period else "조회 기간: DART MCP 기본값"
        return (
            "News Analyst 실적 분석 모드로 다음 종목의 구조화된 실적 리포트를 작성하라.\n"
            f"종목: {stock}\n"
            f"{period_line}\n\n"
            "[DART 실적 데이터]\n"
            f"{dart_result}\n\n"
            "[최신 뉴스]\n"
            f"{news_result}\n\n"
            "반드시 다음 항목을 포함하라:\n"
            "- 매출/영업이익/순이익 전년 동기 대비 증감\n"
            "- 컨센서스 대비 서프라이즈/미스 판단. 컨센서스 데이터가 없으면 추정하지 말고 데이터 없음으로 표시\n"
            "- 주요 뉴스 기반 정성적 코멘트\n"
            "- 다음 분기 전망\n"
            "첫 줄은 반드시 `호재`, `악재`, `중립` 중 하나의 판정과 한 줄 근거로 시작하라.\n"
            "Markdown 문법(`#`, `**`, 표, 코드블록)을 쓰지 말고 Telegram에서 읽기 쉬운 일반 텍스트로 답하라.\n"
            "투자 조언은 단정하지 말고, 확인된 DART·뉴스 근거와 한계를 구분해 한국어로 답하라."
        )

    def _format_earnings_response(self, text: str) -> str:
        plain = self._telegram_plain_text(text)
        verdict = self._earnings_verdict(plain)
        if plain.startswith(("🟢 호재", "🔴 악재", "⚪ 중립")):
            return plain
        lines = plain.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines:
            first = lines[0].strip()
            for label in ("호재", "악재", "중립"):
                if first == label:
                    lines = lines[1:]
                    while lines and not lines[0].strip():
                        lines.pop(0)
                    body = "\n".join(lines).strip()
                    return f"{verdict}\n{body}".strip()
                if first.startswith(f"{label}:") or first.startswith(f"{label} -"):
                    lines[0] = first.replace(label, verdict, 1)
                    return "\n".join(lines).strip()
        return f"{verdict}\n{plain}".strip()

    def _earnings_verdict(self, text: str) -> str:
        if "악재" in text:
            return "🔴 악재"
        if "호재" in text:
            return "🟢 호재"
        return "⚪ 중립"

    def _telegram_plain_text(self, text: str) -> str:
        lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            line = re.sub(r"^#{1,6}\s*", "", line)
            line = re.sub(r"^[-*]\s+", "• ", line)
            line = line.replace("**", "").replace("__", "").replace("`", "")
            lines.append(line)
        return "\n".join(lines).strip()

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
        await self._setup_bot_profile()

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

    async def _setup_bot_profile(self) -> None:
        load_bot_username = getattr(self.notifier, "load_bot_username", None)
        if callable(load_bot_username):
            try:
                await load_bot_username()
            except Exception as exc:
                logger.error("Telegram bot username setup failed: %s", exc)

        set_bot_commands = getattr(self.notifier, "set_bot_commands", None)
        if callable(set_bot_commands):
            try:
                await set_bot_commands(TELEGRAM_BOT_COMMANDS)
            except Exception as exc:
                logger.error("Telegram bot command menu setup failed: %s", exc)

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
