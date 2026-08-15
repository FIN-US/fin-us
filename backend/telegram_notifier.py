import logging
import re
from numbers import Real
from typing import Any

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, is_placeholder_secret

logger = logging.getLogger(__name__)
_TELEGRAM_BOT_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)[^/\s\"]+")

# 텔레그램 sendMessage 본문 상한(4096자)보다 여유를 둔 실사용 상한.
TELEGRAM_MESSAGE_LIMIT = 4000

URGENT_TELEGRAM_LEVELS = {"high", "critical"}
TELEGRAM_ALERT_MODES = {"urgent", "all", "off"}


def _redact_telegram_bot_token(value: str) -> str:
    return _TELEGRAM_BOT_URL_RE.sub(r"\1<redacted>", value)


def _retry_after_seconds(exc: Exception) -> int | None:
    """429 응답의 parameters.retry_after. 429가 아니거나 본문이 없으면 None.

    send_text가 bool만 돌려주는 탓에 호출부는 이 값을 볼 수 없다. 최소한 로그에는
    남겨 두어야 재시도 간격 가정이 실제 ban 길이와 맞는지 판단할 수 있다.
    """
    if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 429:
        return None
    try:
        retry_after = exc.response.json()["parameters"]["retry_after"]
    except Exception:
        return None
    # bool은 int의 서브클래스라 isinstance만으로는 True/False가 통과한다 (PR #253 리뷰).
    if isinstance(retry_after, bool) or not isinstance(retry_after, int):
        return None
    return retry_after


class _TelegramTokenRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_telegram_bot_token(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact_arg(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: self._redact_arg(value)
                for key, value in record.args.items()
            }
        return True

    @staticmethod
    def _redact_arg(arg: Any) -> Any:
        text = str(arg)
        redacted = _redact_telegram_bot_token(text)
        return redacted if redacted != text else arg


def _install_telegram_token_redaction_filter() -> None:
    # raise_for_status가 만드는 str(HTTPStatusError)에는 요청 URL이 그대로 들어 있고,
    # telegram URL에는 봇 토큰이 들어 있다. 그 예외를 %s로 찍는 모듈이 전부 여기 있어야 한다.
    #
    # __name__(backend.telegram_notifier) — send_text 등 5곳 (PR #253 리뷰)
    # backend.telegram_commands — 폴러의 _get_updates가 같은 형태로 URL을 만들고
    #   "Telegram command polling failed: %s"로 찍는다. 401·409(인스턴스 중복)·429·5xx에서
    #   폴링 루프가 5초마다 재시도하며 매 회 기록하므로 전송 경로보다 트리거가 잦다.
    #   전송 경로만 막고 이쪽을 놓쳤었다 (PR #253 2차 리뷰).
    #
    # 이 목록은 로거 이름 allowlist라 구조적으로 취약하다 — telegram 예외를 로깅하는 모듈이
    # 새로 생기면 같은 누출이 다시 열린다. 실제로 이미 한 번 그랬다. 근본 대응은 #257.
    for logger_name in (__name__, "backend.telegram_commands", "httpx", "httpcore"):
        target_logger = logging.getLogger(logger_name)
        if any(
            isinstance(filter_, _TelegramTokenRedactionFilter)
            for filter_ in target_logger.filters
        ):
            continue
        target_logger.addFilter(_TelegramTokenRedactionFilter())


_install_telegram_token_redaction_filter()


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


def _message_id_from_send_response(response: Any) -> int | None:
    """sendMessage 응답 본문에서 ``message_id``를 뽑는다 (#260).

    본문을 읽을 수 없거나 형식이 다르면 ``None``을 반환해 "후속 조작 불가"로
    떨어뜨린다. 전송 자체는 이미 성공한 뒤이므로 예외로 올리지 않는다 — 호출부는
    진행 메시지 정리를 건너뛰면 된다.
    """
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("ok") is not True:
        return None
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    message_id = result.get("message_id")
    return message_id if isinstance(message_id, int) else None


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str | None = TELEGRAM_BOT_TOKEN,
        chat_id: str | None = TELEGRAM_CHAT_ID,
    ):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.bot_username = ""
        # 직전 send_text가 429로 실패했을 때의 flood-wait 길이(초). 성공하거나 429가
        # 아니면 None. 호출부가 재시도 여부를 판단하는 데 쓴다 (PR #253 리뷰).
        self.last_retry_after_seconds: int | None = None
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
        # Improvement #3 (#162 리뷰): details=None은 도구 없는 provider — 판단이 존재하지 않는다.
        # "HOLD"로 채우면 이 PR이 없애려는 "지어낸 판단" 문제를 알림 채널에서 재생산한다.
        decision = details.get("decision") or "판단 없음 (도구 미사용 provider)"
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
        return "\n".join(lines)[:TELEGRAM_MESSAGE_LIMIT]

    def format_morning_briefing(self, briefing: dict[str, Any]) -> str:
        lines = [
            "📰 오늘의 시장 요약",
            str(briefing.get("market_summary") or "요약 없음"),
            "",
            "📊 관심종목 동향",
            *self._format_bullets(briefing.get("watchlist")),
            "",
            "🎯 오늘의 트레이딩 아이디어",
            *self._format_bullets(briefing.get("trading_ideas")),
            "",
            "⚡ 주요 촉매 이벤트",
            *self._format_bullets(briefing.get("catalysts")),
        ]
        return "\n".join(lines)[:TELEGRAM_MESSAGE_LIMIT]

    @staticmethod
    def _format_bullets(items: Any) -> list[str]:
        if not items:
            return ["- 없음"]
        if isinstance(items, str):
            return [f"- {items}"]
        return [f"- {item}" for item in items if str(item).strip()] or ["- 없음"]

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

    async def send_text(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        if not self.enabled:
            return False

        try:
            await self._post_message(text[:TELEGRAM_MESSAGE_LIMIT], reply_markup=reply_markup)
            self.last_retry_after_seconds = None
            return True
        except Exception as exc:
            retry_after = _retry_after_seconds(exc)
            # 반환형은 bool로 두되 flood-wait 길이는 호출부가 읽을 수 있게 남긴다.
            # telegram_commands._send_text_settled가 이 값으로 "남은 예산 안에 풀리는
            # ban인가"를 판단한다 — 없으면 재시도 간격이 순전히 추측이 된다 (PR #253 리뷰).
            self.last_retry_after_seconds = retry_after
            if retry_after is None:
                logger.error("Telegram message send failed: %s", exc)
            else:
                # 호출부(telegram_commands._send_text_settled)의 재시도 간격은 이 값을
                # 볼 수 없어 고정 추측값이다. 실제 ban 길이가 그 가정과 맞는지 판단할
                # 근거를 남긴다 — 어긋나면 간격을 조정하거나 값을 전달하도록 바꿔야 한다.
                logger.error(
                    "Telegram message send failed (429, retry_after=%ss): %s",
                    retry_after,
                    exc,
                )
            return False

    async def send_text_returning_id(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> int | None:
        """메시지를 보내고 ``message_id``를 반환한다. 실패하면 ``None`` (#260).

        진행 메시지처럼 나중에 지우거나 고칠 메시지에 쓴다. 성공/실패 bool을 주는
        :meth:`send_text`와 달리 후속 조작에 필요한 식별자를 준다.
        """
        if not self.enabled:
            return None

        try:
            response = await self._post_message(text[:TELEGRAM_MESSAGE_LIMIT], reply_markup=reply_markup)
        except Exception as exc:
            logger.error("Telegram message send failed: %s", exc)
            return None
        return _message_id_from_send_response(response)

    async def delete_message(self, message_id: int) -> bool:
        """``deleteMessage``로 메시지를 지운다. 거부되면 ``False`` (#260).

        진행 메시지를 치우는 데 쓴다. 실패해도 예외로 올리지 않는다 — 진행 표시
        정리는 답변 전달보다 덜 중요하다.
        """
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/deleteMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    json={"chat_id": self.chat_id, "message_id": message_id},
                )
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram message delete failed: %s", exc)
            return False

    async def edit_message_text(self, message_id: int, text: str) -> bool:
        """``editMessageText``로 기존 메시지 본문을 교체한다 (#260).

        메시지가 너무 오래됐거나(48시간 초과) 이미 삭제된 경우 등 편집이 거부되면
        ``False``를 반환한다.

        최종 답변을 내보내는 용도로는 쓰지 않는다 — 텔레그램은 편집에 대해 푸시 알림을
        보내지 않는다. 진행 메시지 삭제가 실패했을 때 "분석 중"이 영원히 남지 않도록
        종료 표시로 바꾸는 폴백 경로에 쓴다.
        """
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "message_id": message_id,
                        "text": text[:TELEGRAM_MESSAGE_LIMIT],
                        "disable_web_page_preview": True,
                    },
                )
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram message edit failed: %s", exc)
            return False

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
    ) -> bool:
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram callback answer failed: %s", exc)
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

    async def set_bot_commands(self, commands: list[dict[str, str]]) -> bool:
        if not self.enabled:
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/setMyCommands"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json={"commands": commands})
                response.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram bot command menu setup failed: %s", exc)
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

    async def _post_message(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """sendMessage를 호출하고 HTTP 응답을 반환한다.

        반환값은 message_id가 필요한 :meth:`send_text_returning_id`만 사용한다.
        :meth:`send_text`는 예전처럼 성공/실패만 보고 무시한다.
        """
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        return response


telegram_notifier = TelegramNotifier()
