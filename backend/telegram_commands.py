import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable

import httpx

from .config import TRADING_MCP_PARAMS
from .redis_state import RedisSchedulerState, redis_state
from .services import run_mcp_tool
from .telegram_notifier import TELEGRAM_ALERT_MODES, TelegramNotifier, telegram_notifier

logger = logging.getLogger(__name__)

ALERT_COMMAND_HELP = "사용법: /alerts urgent | all | off | status"
TELEGRAM_INTERACTIVE_HELP = "\n".join(
    [
        "사용 가능한 명령:",
        "/alerts urgent|all|off|status - Telegram 알림 모드 변경",
        "/balance - 예수금·총자산·보유 종목 조회",
        "/quote <종목명> - 현재가 조회",
        "/trend <종목명> - 외국인·기관·개인 수급 조회",
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


def _telegram_command_parts(text: str) -> tuple[str, str]:
    command, _, argument = text.partition(" ")
    return command.lower(), argument.strip()


class TelegramCommandHandler:
    def __init__(
        self,
        *,
        notifier: TelegramNotifier,
        state_factory: Callable[[], Any] = redis_state,
        mcp_runner: Callable[[Any, str, dict[str, Any]], Any] = run_mcp_tool,
    ):
        self.notifier = notifier
        self.state_factory = state_factory
        self.mcp_runner = mcp_runner

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")).strip() != self.notifier.chat_id:
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        command, argument = _telegram_command_parts(text)
        if command == "/alerts":
            await self._handle_alerts(argument)
            return
        if command == "/help":
            await self.notifier.send_text(TELEGRAM_INTERACTIVE_HELP)
            return
        if command == "/balance":
            await self._handle_balance()
            return
        if command == "/quote":
            await self._handle_quote(argument)
            return
        if command == "/trend":
            await self._handle_trend(argument)
            return
        if text.startswith("/"):
            await self.notifier.send_text(TELEGRAM_INTERACTIVE_HELP)

    async def _handle_alerts(self, argument: str) -> None:
        parts = argument.split()
        action = parts[0].lower() if parts else "status"
        async with self._state() as state:
            if action == "status":
                mode = await state.get_telegram_alert_mode()
                await self.notifier.send_text(f"현재 Telegram 알림 모드: {mode}")
                return

            if action not in TELEGRAM_ALERT_MODES:
                await self.notifier.send_text(ALERT_COMMAND_HELP)
                return

            await state.set_telegram_alert_mode(action)
            await self.notifier.send_text(f"Telegram 알림 모드가 {action}(으)로 변경되었습니다.")

    async def _handle_balance(self) -> None:
        await self.notifier.send_chat_action("typing")
        try:
            result = await self.mcp_runner(TRADING_MCP_PARAMS, "get_balance", {})
        except Exception as exc:
            await self.notifier.send_text(f"조회 실패: {_short_error(exc)}")
            return
        await self.notifier.send_text(_telegram_text(str(result)))

    async def _handle_quote(self, argument: str) -> None:
        if not argument:
            await self.notifier.send_text(QUOTE_COMMAND_HELP)
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
            await self.notifier.send_text(f"조회 실패: {_short_error(exc)}")
            return
        await self.notifier.send_text(_telegram_text(str(result)))

    async def _handle_trend(self, argument: str) -> None:
        if not argument:
            await self.notifier.send_text(TREND_COMMAND_HELP)
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
            await self.notifier.send_text(f"조회 실패: {_short_error(exc)}")
            return
        await self.notifier.send_text(_telegram_text(str(result)))

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
        self.handler = handler or TelegramCommandHandler(notifier=notifier)
        self.offset: int | None = None

    async def run(self) -> None:
        if not self.notifier.enabled:
            return

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
