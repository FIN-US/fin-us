import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Callable

import httpx

from .redis_state import RedisSchedulerState, redis_state
from .telegram_notifier import TELEGRAM_ALERT_MODES, TelegramNotifier, telegram_notifier

logger = logging.getLogger(__name__)

ALERT_COMMAND_HELP = "사용법: /alerts urgent | all | off | status"
_telegram_command_task: asyncio.Task | None = None


class TelegramCommandHandler:
    def __init__(
        self,
        *,
        notifier: TelegramNotifier,
        state_factory: Callable[[], Any] = redis_state,
    ):
        self.notifier = notifier
        self.state_factory = state_factory

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if str(chat.get("id", "")).strip() != self.notifier.chat_id:
            return

        text = (message.get("text") or "").strip()
        if not text.startswith("/alerts"):
            return

        parts = text.split()
        action = parts[1].lower() if len(parts) > 1 else "status"
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
