import logging
from typing import Optional
from telegram import Bot
from telegram.constants import ParseMode

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  알림 전송 전용 클래스 (단방향)
# ─────────────────────────────────────────────

class TelegramNotifier:
    """
    설정된 채팅방(TELEGRAM_CHAT_ID)으로 뉴스 및 알림을 전송하는 전용 클래스.
    사용자로부터 명령어를 받지 않는 단방향 푸시 알림 전용입니다.
    """
    # 토큰이 없으면 스킵됩니다
    def __init__(self):
        self._enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
        if not self._enabled:
            logger.warning(
                "Telegram notifier disabled: TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다."
            )
        self._bot: Optional[Bot] = Bot(token=TELEGRAM_BOT_TOKEN) if self._enabled else None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send_message(self, text: str, parse_mode: str = ParseMode.MARKDOWN) -> bool:
        """지정된 채팅방으로 텍스트 메시지를 전송합니다."""
        if not self._enabled or not self._bot:
            return False
        try:
            # 텔레그램 메시지 길이 제한(4096자) 대응
            if len(text) > 4000:
                text = text[:3900] + "\n\n...(내용 생략)"

            await self._bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=parse_mode,
            )
            return True
        except Exception as e:
            logger.error(f"Telegram 메시지 전송 실패: {e}")
            return False

    async def send_news_alert(self, stock: str, news_content: str) -> bool:
        """
        수집된 뉴스 내용을 포맷팅하여 전송합니다.
        """
        lines = [
            f" *[{stock}]* 최신 뉴스 알림",
            "",
            news_content
        ]
        return await self.send_message("\n".join(lines))

    async def send_analysis_alert(self, stock: str, analysis_data: dict) -> bool:
        """종목 분석 결과를 포맷팅하여 전송합니다."""
        try:
            summary = analysis_data.get("summary", "") or analysis_data.get("report", "")
            action = analysis_data.get("recommended_action", "")
            confidence = analysis_data.get("confidence", "")

            lines = [
                f" *{stock}* AI 분석 리포트",
                "",
            ]
            if action:
                lines.append(f" *추천 액션:* {action}")
            if confidence:
                lines.append(f" *신뢰도:* {confidence}")
            if summary:
                lines.append(f"\n *분석 요약:*\n{summary}")

            return await self.send_message("\n".join(lines))
        except Exception as e:
            logger.error(f"[{stock}] 분석 알림 생성 실패: {e}")
            return False


# 싱글턴 인스턴스 (다른 모듈에서 import하여 사용)
notifier = TelegramNotifier()


# ─────────────────────────────────────────────
#  라이프사이클 헬퍼 (수신 기능이 없으므로 단순화)
# ─────────────────────────────────────────────

async def start_bot() -> None:
    """알림 시스템 활성화 확인"""
    if notifier.enabled:
        logger.info("Telegram 알림 시스템이 활성화되었습니다.")
    else:
        logger.warning("Telegram 설정이 없어 알림 시스템이 작동하지 않습니다.")


async def stop_bot() -> None:
    """알림 시스템 종료 (필요 시 세션 정리)"""
    logger.info("Telegram 알림 시스템 종료.")