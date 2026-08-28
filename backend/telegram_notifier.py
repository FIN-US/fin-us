import asyncio
import logging
import re
import time
from numbers import Real
from typing import Any, Awaitable, Callable

import httpx

from .config import (
    SIGNAL_UNCERTAINTY_ALERT_THRESHOLD,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    is_placeholder_secret,
)
from .presentation import (
    DEFAULT_TELEGRAM_USER_LEVEL,
    TELEGRAM_MESSAGE_LIMIT,
    alert_kind,
    as_list_items,
    decision_label,
    format_signal_score_line,
    render,
    source_label,
    urgency_label,
)

logger = logging.getLogger(__name__)
_TELEGRAM_BOT_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)[^/\s\"]+")

# TELEGRAM_MESSAGE_LIMIT은 presentation이 정의하고 이 모듈이 이름만 다시 내보낸다 (#297).
# 알림도 render를 통과하므로 이 모듈이 presentation을 임포트하고, 그래서 상한의 정의는
# 반대편(잎)에 있어야 순환이 생기지 않는다. 기존 임포트 경로는 그대로 살아 있다.

# 긴급으로 취급하는 urgency. 배너(🚨 긴급 알림)는 이 집합을 따로 보지 않고
# should_send_telegram_alert의 판정 결과를 그대로 쓴다 — 판정이 두 곳에 있으면 어긋난다
# (#297 자가리뷰). presentation.alert_kind 독스트링 참조.
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

        confidence_text = f" (확신도 {confidence:.2f})" if isinstance(confidence, Real) else ""
        # 필드명도 값도 한국어다 (#297 검수). decision/urgency는 LLM 자유 텍스트가 아니라
        # AgentReport의 정해진 값이므로 출력 계층이 결정론적으로 번역할 수 있다 —
        # 표에 없는 값은 presentation의 라벨 함수가 원문 그대로 통과시킨다.
        #
        # 제목에서 "[긴급]"을 뺐다. 긴급 여부는 이제 render가 붙이는 배너(🚨 긴급 알림)가
        # 알리므로, 두 자리에서 같은 말을 하면 한쪽이 바뀔 때 어긋난다. is_urgent는
        # urgency_reason의 기본 문구를 고르는 데 계속 쓴다.
        items = [
            f"판단: {decision_label(decision)}{confidence_text}",
            f"이유: {reason}",
            # 구분자를 하이픈에서 쉼표로 바꿨다. 줄 앞에 나열 표시("- ")가 붙는 마당에
            # 줄 가운데 또 하이픈이 있으면 그게 항목 경계로 읽힌다 (#297 검수 3차).
            f"긴급도: {urgency_label(urgency)}, {urgency_reason}",
        ]
        # #298: 이 알림을 촉발한 signal이 몇 점이었는지. 점수가 없으면 줄이 통째로
        # 빠진다 — 제목 바로 아래, 판단보다 먼저 읽히도록 목록 맨 앞에 넣는다. #298은
        # 제목 뒤 lines에 직접 끼웠지만, #297에서 제목만 목록 밖으로 나가고 나머지 줄은
        # as_list_items를 거치게 됐다. 같은 자리를 지키려면 items의 0번이어야 한다 —
        # lines에 끼우면 이 줄만 나열 표시를 못 받는다.
        # 임계값은 문구가 아니라 정책이라 출력 계층이 아니라 이쪽이 넘긴다 (#308).
        score_line = format_signal_score_line(
            analysis_data.get("signal_score"),
            analysis_data.get("signal_reason"),
            analysis_data.get("signal_uncertainty"),
            uncertainty_threshold=SIGNAL_UNCERTAINTY_ALERT_THRESHOLD,
        )
        if score_line is not None:
            items.insert(0, score_line)
        if summary:
            items.append(f"요약: {summary}")
        # 제목은 목록 밖이다. 값이 늘어선 줄만 표시를 받는다.
        lines = [f"{stock} / {source_label(source)}", *as_list_items(items)]
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
        # 표시는 presentation.LIST_MARKER 하나로 모은다. 여기서 하이픈을 직접 적으면
        # 표시를 바꿀 때 이 함수만 조용히 옛 기호로 남는다 (#297 검수 3차).
        if not items:
            return as_list_items(["없음"])
        if isinstance(items, str):
            return as_list_items([items])
        values = [str(item) for item in items if str(item).strip()]
        return as_list_items(values or ["없음"])

    async def send_analysis_alert(
        self,
        stock: str,
        source: str,
        analysis_data: dict[str, Any],
        *,
        alert_mode: str = "urgent",
        level: str = DEFAULT_TELEGRAM_USER_LEVEL,
    ) -> bool:
        """분석 알림을 보낸다. 문장 조립은 format_analysis_alert, 마무리는 presentation.render.

        둘을 나눠 둔 이유: format_analysis_alert는 analysis_data를 문장으로 바꾸는 순수
        함수라 테스트가 그 매핑만 검사할 수 있고, 틀·용어 각주·마크다운 정리는 나가는 모든
        메시지에 공통이라 한 지점(render)에 있어야 한다. 여기서 합치면 알림만 규칙이
        갈라진다 (#297).

        ``level``은 호출부(scheduler)가 redis에서 읽어 넘긴다. notifier가 직접 읽지 않는
        이유는 이 클래스가 저장소를 모르는 채로 남아야 테스트에서 redis 없이 세워지기
        때문이다 — ``alert_mode``와 같은 방식이다.
        """
        if not self.enabled:
            return False
        if not should_send_telegram_alert(analysis_data, alert_mode=alert_mode):
            return False

        is_urgent = should_send_telegram_alert(analysis_data, alert_mode="urgent")
        try:
            await self._post_message(
                render(
                    self.format_analysis_alert(
                        stock=stock,
                        source=source,
                        analysis_data=analysis_data,
                    ),
                    # 배너는 전송 게이트와 같은 판정을 쓴다. "긴급 모드였어도 나갔을
                    # 알림인가"가 곧 🚨의 뜻이고, 그 판정은 여기 한 번뿐이다.
                    alert_kind(is_urgent),
                    level,
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

# 부수효과가 확정된 뒤의 전송은 update 재시도로 되살릴 수 없다(#247). 대신 그 자리에서
# 짧게 재시도해 429 같은 일시 장애를 흡수한다 — "전송만 별도로 재시도"에 해당한다.
#
# 이 값을 키우면 안 되는 이유가 셋이다.
#   1. 텔레그램 폴러 루프를 그 시간만큼 붙잡으므로, 같은 배치에서 재시도를 기다리는 다른
#      update가 시도 없이 예산(일반 실패 60초)만 잃는다. PR #242가 넓힌 예산을 되돌리는
#      방향이다.
#   2. /buy·/advise 확인 프롬프트는 대기 주문의 60초 만료 창을 나눠 쓴다. 늦게 도착할수록
#      사용자가 확인 버튼을 누를 시간이 줄어든다.
#   3. 자동 제안(#314)은 감시 주기 안에서 이 함수를 부르므로, 그 시간만큼 다음 감시가
#      밀린다. 10분 주기라 여유는 크지만 상한이 없으면 그 여유도 근거가 없다.
#
# 429의 retry_after를 읽을 수 없어(notifier.send_text가 bool만 돌려준다) 간격은 추측이다.
# Telegram이 더 긴 ban을 주면 4시도가 모두 그 안에서 소진되고 메시지는 버려진다.
SETTLED_SEND_RETRY_BACKOFF_SECONDS = (1.0, 3.0, 9.0)
# 위 백오프 합(13초)은 상한이 아니다. 시도마다 HTTP 왕복이 붙고 httpx 타임아웃이 10초라,
# Telegram이 429로 즉답하지 않고 무응답이면 4시도 × 10초 + 13초 = 53초까지 늘어난다.
# 그러면 위 대가가 모두 한계까지 간다 — /buy 프롬프트가 60초 만료 창을 거의 다 먹고,
# 같은 배치의 update는 예산을 통째로 잃는다 (PR #253 리뷰).
#
# 그래서 백오프 합이 아니라 벽시계로 상한을 강제한다. 429는 즉답이라 현실 경로는 여전히
# 백오프 합에 가깝고, 이 상한은 무응답·행 같은 병리적 경우만 잘라낸다.
# 상한이 있다는 사실과 그 값이 만료 창 안에 든다는 것을 테스트가 고정한다
# (test_settled_send_retry_is_bounded, test_settled_send_gives_up_at_the_wall_clock_bound).
SETTLED_SEND_TIMEOUT_SECONDS = 20.0


async def send_text_settled(
    notifier: TelegramNotifier,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """부수효과가 확정된 뒤의 전송. 실패해도 호출부를 재시도하지 않는다 (#247).

    호출부를 재시도하면 원래 의도를 달성하지 못하고 다른 메시지로 끝나기 때문이다. /buy는
    대기 주문이 이미 저장돼 재실행이 "이미 대기 중인 주문이 있습니다"로 끝나 사용자가 확인
    버튼을 영영 못 받고, /confirm은 claim(GETDEL)으로 주문이 소비돼 재실행이 "확정할 대기
    주문이 없습니다"로 끝나 체결된 주문을 미체결로 인식하게 만든다. 자동 제안(#314)은 제안
    왕복(≤120초)·검증 왕복(≤40초)과 재제안 냉각을 이미 소비한 뒤라, 한 번의 429로 버리면
    같은 종목은 냉각이 풀릴 때까지(기본 60분) 다시 시도되지도 않는다.

    대신 전송만 그 자리에서 재시도해 429 같은 일시 장애를 흡수한다. 그래도 실패하면 로그만
    남긴다 — 사용자에게 말을 걸 수 없는 상태에서 할 수 있는 일이 없다.

    이 함수가 핸들러 메서드가 아니라 모듈 함수인 것은 텔레그램 명령 경로와 스케줄러의 자동
    제안 경로가 **같은 재시도를 써야** 하기 때문이다 (PR #327 리뷰). 한쪽만 단발 전송이면
    같은 429에 한쪽 메시지만 조용히 버려진다.

    ``sleep``은 테스트가 전역 ``asyncio.sleep`` 대신 이 호출만 대체할 수 있게 하는 간접층이다.

    반환값은 전송 성공 여부다. 호출부가 "통지가 나갔는가"로 뒷정리를 갈라야 하는 경우가
    있다(/buy·자동 제안은 실패 시 대기 주문을 지운다).
    """
    attempts = len(SETTLED_SEND_RETRY_BACKOFF_SECONDS) + 1
    started_at = time.monotonic()
    try:
        # 시도 횟수가 아니라 벽시계로 상한을 강제한다. 무응답이면 시도마다 httpx
        # 타임아웃(10초)이 그대로 붙어 횟수만으로는 상한이 서지 않기 때문이다.
        # asyncio.timeout은 자기 데드라인이 아닌 외부 취소는 CancelledError로 그대로
        # 통과시키므로 폴러의 graceful shutdown을 방해하지 않는다.
        async with asyncio.timeout(SETTLED_SEND_TIMEOUT_SECONDS):
            for index in range(attempts):
                sent = await notifier.send_text(text, reply_markup=reply_markup)
                if sent is not False:
                    return True
                if index >= len(SETTLED_SEND_RETRY_BACKOFF_SECONDS):
                    break

                delay = SETTLED_SEND_RETRY_BACKOFF_SECONDS[index]
                # 429의 flood-wait은 흔히 30초 이상이라 (1, 3, 9) 추측으로는 4시도가
                # 전부 ban 구간에 소진된다. 게다가 ban 중 재요청은 대기 시간을 늘리는
                # 방향으로 작용한다 — 남은 예산 안에 안 풀리면 재시도가 무의미할 뿐
                # 아니라 해롭다 (PR #253 2차 리뷰).
                #
                # last_retry_after_seconds는 notifier에 걸린 공유 가변 상태다. 이 읽기가
                # 방금 그 send_text의 결과를 보는 근거는 둘뿐이다: send_text가 성공·실패
                # 양쪽에서 값을 갱신해 호출 간 이월이 없다는 것과, 위 send_text 반환과
                # 이 줄 사이에 await가 없어 이벤트 루프가 다른 코루틴에 넘어가지 않는다는
                # 것. 사이에 await를 하나 넣으면(로깅을 비동기로 바꾸는 정도로도) 다른
                # 전송의 flood-wait을 읽게 된다 (PR #253 3차 리뷰).
                retry_after = getattr(notifier, "last_retry_after_seconds", None)
                if retry_after is not None:
                    remaining = SETTLED_SEND_TIMEOUT_SECONDS - (
                        time.monotonic() - started_at
                    )
                    if retry_after > remaining:
                        logger.error(
                            "확정된 부수효과의 결과를 전송하지 못했습니다 "
                            "(%s자, flood-wait %s초 > 남은 예산 %.1f초 — 재시도 포기)",
                            len(text),
                            retry_after,
                            max(0.0, remaining),
                        )
                        return False
                    delay = max(delay, float(retry_after))
                await sleep(delay)
    except TimeoutError:
        logger.error(
            "확정된 부수효과의 결과를 전송하지 못했습니다 (%s자, 벽시계 상한 %s초 초과)",
            len(text),
            SETTLED_SEND_TIMEOUT_SECONDS,
        )
        return False
    # 본문은 남기지 않는다. 이 경로에는 체결 내역·잔고가 실려 있고, 진단에 필요한 것은
    # "어느 지점에서 몇 번 만에 포기했는가"이지 사용자에게 보내려던 문장이 아니다.
    logger.error(
        "확정된 부수효과의 결과를 전송하지 못했습니다 (%s자, %s시도 후 포기)",
        len(text),
        attempts,
    )
    return False


telegram_notifier = TelegramNotifier()
