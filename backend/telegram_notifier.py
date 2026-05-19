import logging
from numbers import Real
from typing import Any

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, is_placeholder_secret

logger = logging.getLogger(__name__)

URGENT_TELEGRAM_LEVELS = {"high", "critical"}
TELEGRAM_ALERT_MODES = {"urgent", "all", "off"}


def should_send_telegram_alert(
    analysis_data: dict[str, Any],
    *,
    alert_mode: str = "urgent",
) -> bool:
    if alert_mode == "off":
        return False
    if alert_mode == "all":
        return True
    if alert_mode != "urgent":
        return False
    return (
        analysis_data.get("telegram_alert") is True
        and analysis_data.get("urgency") in URGENT_TELEGRAM_LEVELS
    )


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str | None = TELEGRAM_BOT_TOKEN,
        chat_id: str | None = TELEGRAM_CHAT_ID,
    ):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.bot_username = ""
        self.enabled = not (
            is_placeholder_secret(self.bot_token)
            or is_placeholder_secret(self.chat_id)
        )

    def format_analysis_alert(
        self,
        *,
        stock: str,
        source: str,
        analysis_data: dict[str, Any],
    ) -> str:
        details = analysis_data.get("details") or {}
        decision = details.get("decision", "HOLD")
        confidence = details.get("confidence_score", "")
        reason = details.get("reason") or analysis_data.get("summary", "")
        urgency = analysis_data.get("urgency", "normal")
        is_urgent = should_send_telegram_alert(analysis_data, alert_mode="urgent")
        urgency_reason = analysis_data.get("urgency_reason") or (
            "긴급 판단 사유 없음" if is_urgent else "판단 사유 없음"
        )
        summary = analysis_data.get("summary", "")

        confidence_text = f" ({confidence:.2f})" if isinstance(confidence, Real) else ""
        title = f"[긴급] {stock} / {source}" if is_urgent else f"{stock} / {source}"
        lines = [
            title,
            f"Decision: {decision}{confidence_text}",
            f"Reason: {reason}",
            f"Urgency: {urgency} - {urgency_reason}",
        ]
        if summary:
            lines.append(f"Summary: {summary}")
        return "\n".join(lines)[:4000]

    async def send_analysis_alert(
        self,
        stock: str,
        source: str,
        analysis_data: dict[str, Any],
        *,
        alert_mode: str = "urgent",
    ) -> bool:
        if not self.enabled:
            return False
        if not should_send_telegram_alert(analysis_data, alert_mode=alert_mode):
            return False

        try:
            await self._post_message(
                self.format_analysis_alert(
                    stock=stock,
                    source=source,
                    analysis_data=analysis_data,
                )
            )
            return True
        except Exception as exc:
            logger.error("Telegram alert send failed for %s/%s: %s", source, stock, exc)
            return False

    async def send_text(self, text: str) -> bool:
        if not self.enabled:
            return False

        try:
            await self._post_message(text[:4000])
            return True
        except Exception as exc:
            logger.error("Telegram message send failed: %s", exc)
            return False

    async def send_chat_action(self, action: str = "typing") -> bool:
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendChatAction"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    json={"chat_id": self.chat_id, "action": action},
                )
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram chat action send failed: %s", exc)
            return False

    async def load_bot_username(self) -> str:
        if not self.enabled:
            return ""
        if self.bot_username:
            return self.bot_username

        url = f"https://api.telegram.org/bot{self.bot_token}/getMe"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url)
                response.raise_for_status()
                body = response.json()
            result = body.get("result") or {}
            username = str(result.get("username") or "").strip().lstrip("@")
            self.bot_username = username.lower()
            return self.bot_username
        except Exception as exc:
            logger.error("Telegram bot username lookup failed: %s", exc)
            return ""

    async def _post_message(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()


telegram_notifier = TelegramNotifier()
