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


class TelegramApiError(Exception):
    """텔레그램 API 호출 실패. str()에 요청 URL이 — 따라서 봇 토큰이 — 들어가지 않는다 (#257).

    httpx가 만드는 예외, 특히 raise_for_status의 HTTPStatusError는 메시지에 요청 URL을
    그대로 담는다. 텔레그램 URL은 경로에 봇 토큰이 있으므로 그 예외를 호출부까지 올려
    보내면 "이 예외를 %s로 찍는 모든 로거"에 리댁션을 걸어야 한다. 그 로거 이름 목록은
    두 번 뚫렸다 (PR #253 1차 리뷰: 이 모듈, 2차 리뷰: telegram_commands 폴러).

    발생 지점에서 URL 없는 예외로 바꿔 던지면 목록을 유지할 이유 자체가 없어진다.
    대신 httpx 예외 타입이 소비자에게 도달하지 않으므로, 429 판정에 필요한
    status_code와 본문은 이 예외가 직접 들고 다닌다.
    """

    def __init__(
        self,
        method: str,
        *,
        status_code: int | None = None,
        body: dict[str, Any] | None = None,
        reason: str = "",
    ) -> None:
        self.method = method
        self.status_code = status_code
        self.body = body

        # 텔레그램은 실패 본문에 사람이 읽을 수 있는 description을 준다
        # ("Too Many Requests: retry after 42"). URL이 빠진 만큼 이쪽을 남겨야
        # 진단 정보가 오히려 줄지 않는다.
        description = (body or {}).get("description")
        if isinstance(description, str) and description.strip():
            reason = description.strip()

        details = []
        if status_code is not None:
            details.append(f"HTTP {status_code}")
        if reason:
            # reason은 httpx 예외 메시지에서 오기도 한다. UnsupportedProtocol처럼 URL을
            # 담는 타입이 있어 여기서 한 번 더 거른다 — 이 클래스의 계약은 "str()에
            # 토큰 없음"이고, 그 계약이 리댁션 목록을 대신한다.
            details.append(_redact_telegram_bot_token(reason))
        super().__init__(
            f"telegram {method} failed: {', '.join(details) or 'unknown error'}"
        )


def _response_body(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def call_telegram_api(
    bot_token: str,
    method: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> None:
    """텔레그램 API를 호출하고 성공 여부만 본다. 응답 본문은 읽지 않는다.

    본문이 필요하면 fetch_telegram_api를 쓴다. 플래그 하나로 합치지 않고 함수를 나눈
    이유는, 합치면 "본문을 읽어야 하는데 플래그를 빠뜨린" 호출부가 조용히 빈 dict를
    받기 때문이다 — getUpdates라면 로그도 백오프도 없이 "update 없음"으로 계속 돌고,
    getMe라면 username이 ""로 퇴화한다. 나뉘어 있으면 잘못 고른 쪽이 None을 돌려주므로
    첫 실행에서 깨진다 (#257 자가리뷰).
    """
    await _request_telegram_api(
        bot_token, method, payload=payload, timeout=timeout, parse_body=False
    )


async def fetch_telegram_api(
    bot_token: str,
    method: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """텔레그램 API를 호출하고 응답 본문을 파싱해 돌려준다.

    파싱 실패는 TelegramApiError다. 조용히 {}로 넘기면 폴러가 "update 없음"으로 읽고
    로그도 백오프도 없이 다음 폴링으로 넘어간다.
    """
    return await _request_telegram_api(
        bot_token, method, payload=payload, timeout=timeout, parse_body=True
    )


async def _request_telegram_api(
    bot_token: str,
    method: str,
    *,
    payload: dict[str, Any] | None,
    timeout: float,
    parse_body: bool,
) -> dict[str, Any]:
    """봇 토큰이 URL에 실리는 유일한 지점.

    여기서 나가는 실패는 전부 TelegramApiError다. httpx 예외는 이 경계를 넘지 않는다 (#257).
    """
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if payload is None:
                response = await client.post(url)
            else:
                response = await client.post(url, json=payload)
            response.raise_for_status()
            # 본문을 쓰지 않는 호출은 파싱하지 않는다. 무조건 파싱하면 200에 비-JSON이
            # 온 경우 본문을 읽지도 않는 sendMessage까지 실패하는데, 이는 이 리팩터링
            # 전에 없던 동작이다 (#257 자가리뷰).
            body = response.json() if parse_body else {}
    except httpx.HTTPStatusError as exc:
        raise TelegramApiError(
            method,
            status_code=exc.response.status_code,
            body=_response_body(exc.response),
            reason=exc.response.reason_phrase,
        ) from None
    except Exception as exc:
        # 네트워크 오류, 본문 파싱 실패 등. from None으로 예외 체인을 끊는다 —
        # 원본 메시지에 URL이 들어 있을 수 있고, 체인이 남으면 exc_info 로깅이나
        # 처리되지 않은 트레이스백이 그 원본을 그대로 찍는다.
        #
        # 감수한 비용: except Exception이라 우리 쪽 버그(직렬화 불가한 payload로 나는
        # TypeError 같은 것)도 한 줄로 뭉개지고 발생 위치를 잃는다. 그럼에도 넓게 잡는
        # 이유는, 타입을 모르는 예외야말로 메시지에 URL이 없다고 가정할 수 없기
        # 때문이다. 좁히려면 "URL을 담지 않는다고 확인된 타입"의 목록이 필요한데,
        # 그건 이 이슈가 없애려는 allowlist와 같은 종류의 물건이다.
        raise TelegramApiError(method, reason=f"{type(exc).__name__}: {exc}") from None
    return body if isinstance(body, dict) else {}


def _retry_after_seconds(exc: Exception) -> int | None:
    """429 응답의 parameters.retry_after. 429가 아니거나 본문이 없으면 None.

    send_text가 bool만 돌려주는 탓에 호출부는 이 값을 볼 수 없다. 최소한 로그에는
    남겨 두어야 재시도 간격 가정이 실제 ban 길이와 맞는지 판단할 수 있다.
    """
    if not isinstance(exc, TelegramApiError) or exc.status_code != 429:
        return None
    parameters = (exc.body or {}).get("parameters")
    if not isinstance(parameters, dict):
        return None
    retry_after = parameters.get("retry_after")
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
    # 애플리케이션 코드가 만드는 예외에는 더 이상 URL이 없다 — telegram 호출이 전부
    # _request_telegram_api를 지나고(call_telegram_api와 fetch_telegram_api 둘 다 그리로
    # 모인다), 거기서 나오는 TelegramApiError는 str()에 URL을 담지 않는다 (#257).
    # 그래서 backend.* 로거는 이 목록에 없어도 된다. 예전에는 있어야 했고, 빠뜨려서
    # 두 번 뚫렸다 (PR #253 1·2차 리뷰).
    #
    # httpx만 남는다. 이쪽은 예외 경로가 아니라 라이브러리 자신의 정상 로깅이 문제다 —
    # 요청마다 INFO로 'HTTP Request: %s %s ...'를 request.url과 함께 찍고, 그 URL에
    # 토큰이 들어 있다. 우리 코드를 어떻게 고치든 이 로그는 httpx가 직접 만든다.
    #
    # httpcore는 뺐다. 여기 있었지만 한 번도 동작한 적이 없다 — 필터는 그 로거가 직접
    # 만든 레코드에만 걸리고 자식에서 전파된 레코드는 타지 않는데, httpcore가 맨
    # "httpcore"로 찍는 곳은 없다(전부 httpcore.connection/.http11/.http2/.proxy/.socks).
    # 애초에 걸 이유도 없었다: httpcore의 trace 로그는 request=%r로 찍고 그 repr이
    # <Request [b'POST']>라 URL을 담지 않는다. httpx.Request의 repr과 달리 안전하다.
    #
    # 남은 항목이 하나라 해도 "httpx가 로거 이름을 바꾸면 조용히 무력화된다"는 성질은
    # 그대로다. 그래서 test_httpx_request_logging_is_redacted_end_to_end가 실제 httpx
    # 요청을 태워 이 결합을 검사한다 — 이름이 바뀌면 조용히가 아니라 빨갛게 깨진다.
    for logger_name in ("httpx",):
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


def _message_id_from_send_body(body: Any) -> int | None:
    """sendMessage 응답 본문에서 ``message_id``를 뽑는다 (#260).

    형식이 다르면 ``None``을 반환해 "후속 조작 불가"로 떨어뜨린다. 전송 자체는 이미
    성공한 뒤이므로 예외로 올리지 않는다 — 호출부는 진행 메시지 정리를 건너뛰면 된다.

    #260은 httpx 응답 객체를 받아 여기서 .json()을 불렀지만, 이제 본문 파싱은
    fetch_telegram_api가 맡는다. 응답 객체가 호출부까지 올라오지 않는 것이 #257의
    핵심이라 — 올라오면 그 객체를 로깅하는 순간 URL이 샌다 — 파싱된 dict를 받는다.
    """
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
            body = await self._post_message_returning_body(
                text[:TELEGRAM_MESSAGE_LIMIT], reply_markup=reply_markup
            )
        except Exception as exc:
            logger.error("Telegram message send failed: %s", exc)
            return None
        return _message_id_from_send_body(body)

    async def delete_message(self, message_id: int) -> bool:
        """``deleteMessage``로 메시지를 지운다. 거부되면 ``False`` (#260).

        진행 메시지를 치우는 데 쓴다. 실패해도 예외로 올리지 않는다 — 진행 표시
        정리는 답변 전달보다 덜 중요하다.
        """
        if not self.enabled:
            return False

        try:
            await call_telegram_api(
                self.bot_token,
                "deleteMessage",
                payload={"chat_id": self.chat_id, "message_id": message_id},
            )
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

        try:
            await call_telegram_api(
                self.bot_token,
                "editMessageText",
                payload={
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "text": text[:TELEGRAM_MESSAGE_LIMIT],
                    "disable_web_page_preview": True,
                },
            )
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

        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            await call_telegram_api(
                self.bot_token, "answerCallbackQuery", payload=payload
            )
            return True
        except Exception as exc:
            logger.error("Telegram callback answer failed: %s", exc)
            return False

    async def send_chat_action(self, action: str = "typing") -> bool:
        if not self.enabled:
            return False

        try:
            await call_telegram_api(
                self.bot_token,
                "sendChatAction",
                payload={"chat_id": self.chat_id, "action": action},
            )
            return True
        except Exception as exc:
            logger.error("Telegram chat action send failed: %s", exc)
            return False

    async def set_bot_commands(self, commands: list[dict[str, str]]) -> bool:
        if not self.enabled:
            return False

        try:
            await call_telegram_api(
                self.bot_token, "setMyCommands", payload={"commands": commands}
            )
            return True
        except Exception as exc:
            logger.error("Telegram bot command menu setup failed: %s", exc)
            return False

    async def load_bot_username(self) -> str:
        if not self.enabled:
            return ""
        if self.bot_username:
            return self.bot_username

        try:
            body = await fetch_telegram_api(self.bot_token, "getMe")
            result = body.get("result") or {}
            username = str(result.get("username") or "").strip().lstrip("@")
            self.bot_username = username.lower()
            return self.bot_username
        except Exception as exc:
            logger.error("Telegram bot username lookup failed: %s", exc)
            return ""

    def _send_message_payload(
        self,
        text: str,
        reply_markup: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return payload

    async def _post_message(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """sendMessage를 호출하고 성공 여부만 본다. :meth:`send_text`의 경로다.

        #260은 여기서 httpx 응답을 반환해 message_id를 뽑게 했지만, 응답 객체가
        호출부까지 올라오면 그것을 로깅하는 순간 URL이 — 따라서 토큰이 — 샌다.
        본문이 필요한 쪽은 아래 :meth:`_post_message_returning_body`로 갈라 뒀다 (#257).
        """
        await call_telegram_api(
            self.bot_token,
            "sendMessage",
            payload=self._send_message_payload(text, reply_markup),
        )

    async def _post_message_returning_body(
        self,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """sendMessage를 호출하고 응답 본문을 돌려준다. message_id가 필요한 경로다 (#260)."""
        return await fetch_telegram_api(
            self.bot_token,
            "sendMessage",
            payload=self._send_message_payload(text, reply_markup),
        )


telegram_notifier = TelegramNotifier()
