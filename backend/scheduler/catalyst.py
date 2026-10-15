"""촉매 이벤트 캘린더 수집과 D-1/D-0 사전 알림."""

import logging
import re
from datetime import date, datetime
from typing import Any, Callable

from backend.core.config import DART_MCP_PARAMS
from backend.repositories.catalyst import CatalystEventInput, CatalystNotificationRepo
from backend.repositories.watchlist import WatchlistReader
from backend.scheduler import deps
from backend.telegram.notifier import telegram_notifier, TelegramTextSender
from backend.telegram.presentation import DEFAULT_TELEGRAM_USER_LEVEL, KIND_ALERT, render

logger = logging.getLogger(__name__)


CATALYST_EVENT_LABELS = {
    "earnings": "실적",
    "dividend": "배당",
    "disclosure": "공시",
    "agm": "주주총회",
}


def _infer_catalyst_event_type(description: str) -> str:
    text = description.lower()
    if any(keyword in description for keyword in ("실적", "분기보고서", "반기보고서", "사업보고서")):
        return "earnings"
    if "배당" in description:
        return "dividend"
    if any(keyword in description for keyword in ("주주총회", "주총")):
        return "agm"
    if any(keyword in text for keyword in ("major stock", "executive")):
        return "disclosure"
    return "disclosure"


def _parse_catalyst_date(raw: str) -> date | None:
    compact_match = re.search(r"\b(20\d{2})(\d{2})(\d{2})\b", raw)
    if compact_match is not None:
        year, month, day = compact_match.groups()
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            return None

    dashed_match = re.search(r"\b(20\d{2})[-.](\d{1,2})[-.](\d{1,2})\b", raw)
    if dashed_match is None:
        return None
    year, month, day = dashed_match.groups()
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _parse_disclosure_signal_events(stock_name: str, raw_signal: str) -> list[CatalystEventInput]:
    events: list[CatalystEventInput] = []
    for line in str(raw_signal).splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue

        event_date = _parse_catalyst_date(stripped)
        if event_date is None:
            continue

        parts = [part.strip(" -") for part in stripped.split("|")]
        description = next(
            (part for part in parts if part and _parse_catalyst_date(part) is None),
            stripped.lstrip("-").strip(),
        )
        events.append(
            CatalystEventInput(
                stock_name=stock_name,
                event_type=_infer_catalyst_event_type(description),
                event_date=event_date,
                description=description,
                source="dart",
            )
        )
    return events


def _format_catalyst_alert(event: Any) -> str:
    d_day = "D-Day" if event.days_until_event == 0 else f"D-{event.days_until_event}"
    label = CATALYST_EVENT_LABELS.get(event.event_type, event.event_type or "기타")
    return "\n".join(
        [
            f"📅 촉매 이벤트 알림 ({d_day})",
            f"- 종목: {event.stock_name}",
            f"- 날짜: {event.event_date.isoformat()}",
            f"- 유형: {label}",
            f"- 내용: {event.description}",
        ]
    )


async def _collect_catalyst_events(
    watchlist: list[str],
    catalyst_repo: CatalystNotificationRepo,
) -> None:
    for stock in watchlist:
        try:
            signal = await deps.run_mcp_tool(
                DART_MCP_PARAMS,
                "get_disclosure_signal",
                {"stock_name": stock},
            )
            events = _parse_disclosure_signal_events(stock, str(signal))
            if events:
                await catalyst_repo.upsert_events(events)
        except Exception as e:
            logger.error("[%s] 촉매 이벤트 수집 중 오류: %s", stock, e)


async def _telegram_user_level(state: Any = None) -> str:
    """설명 수준을 읽는다. 못 읽으면 기본값(초보) (#297 자가리뷰).

    브리핑·촉매 알림이 이 함수를 쓰지 않고 기본값을 그대로 넘기고 있었다. 그래서 /level
    중급으로 바꾼 사용자도 매 브리핑마다 용어 각주를 받았다 — 분석 알림만 수준을 읽고
    나머지는 안 읽는, 사용자 눈에는 설명할 수 없는 차이였다.

    ``state``가 있으면 그걸 쓰고(이미 열린 연결을 한 번 더 열 이유가 없다) 없으면 직접
    연다. 실패는 삼킨다 — 수준은 다른 메시지의 곁다리 정보이고, 못 읽었다고 브리핑 자체를
    거르면 부가 기능이 본 기능을 잡아먹는다 (telegram_commands._current_level과 같은 판단).
    """
    try:
        if state is not None:
            return await state.get_telegram_user_level()
        async with deps.redis_state() as opened:
            return await opened.get_telegram_user_level()
    except Exception as exc:
        logger.warning("설명 수준을 읽지 못해 기본값을 씁니다: %s", exc)
        return DEFAULT_TELEGRAM_USER_LEVEL


async def _send_due_catalyst_alerts(
    watchlist: list[str],
    catalyst_repo: CatalystNotificationRepo,
    *,
    notifier: TelegramTextSender,
    today: date,
    level: str = DEFAULT_TELEGRAM_USER_LEVEL,
) -> None:
    try:
        due_events = await catalyst_repo.list_due_for_notification(watchlist, today=today)
    except Exception as e:
        logger.error("촉매 이벤트 알림 대상 조회 중 오류: %s", e)
        return

    for event in due_events:
        try:
            sent = await notifier.send_text(
                render(_format_catalyst_alert(event), KIND_ALERT, level)
            )
            if sent is True:
                await catalyst_repo.mark_notification_sent(
                    event.id,
                    days_until_event=event.days_until_event,
                )
        except Exception as e:
            logger.error("[%s] 촉매 이벤트 Telegram 알림 중 오류: %s", event.stock_name, e)


async def catalyst_calendar_task(
    *,
    watchlist_repo: WatchlistReader | None = None,
    catalyst_repo: CatalystNotificationRepo | None = None,
    notifier: TelegramTextSender = telegram_notifier,
    today_factory: Callable[[], date] | None = None,
    use_redis_lock: bool = True,
    level: str | None = None,
) -> None:
    """예정 촉매를 모으고 임박한 것을 알린다.

    ``level``은 설명 수준이다. redis 잠금을 쓰는 바깥 호출이 이미 열린 연결로 읽어 안쪽
    호출에 넘긴다 — "redis를 쓰지 않는다"고 한 경로(use_redis_lock=False)가 수준을 읽으려고
    redis를 여는 것은 앞뒤가 맞지 않는다. 그 경로는 기본값으로 간다 (#297 자가리뷰).
    """
    if use_redis_lock:
        try:
            async with deps.redis_state() as state:
                scheduler_token = await state.acquire_scheduler_lock("catalyst_calendar")
                if scheduler_token is None:
                    logger.info("다른 worker가 촉매 이벤트 캘린더를 실행 중입니다. 이번 실행을 건너뜁니다.")
                    return
                try:
                    await catalyst_calendar_task(
                        watchlist_repo=watchlist_repo,
                        catalyst_repo=catalyst_repo,
                        notifier=notifier,
                        today_factory=today_factory,
                        use_redis_lock=False,
                        level=await _telegram_user_level(state),
                    )
                finally:
                    await state.release_lock(
                        state.keys.scheduler_lock("catalyst_calendar"),
                        scheduler_token,
                    )
        except Exception as e:
            logger.error("Redis 기반 촉매 이벤트 캘린더 실행 중 오류: %s", e)
        return

    if watchlist_repo is None:
        watchlist_repo = deps._default_watchlist_repo()
    if catalyst_repo is None:
        catalyst_repo = deps._default_catalyst_repo()
    today = today_factory() if today_factory is not None else datetime.now(deps.scheduler.timezone).date()

    try:
        watchlist = await watchlist_repo.get_watchlist()
    except Exception as e:
        logger.error("촉매 이벤트 관심 종목 조회 중 오류: %s", e)
        return

    if not watchlist:
        logger.info("관심 종목이 없어 촉매 이벤트 캘린더를 건너뜁니다.")
        return

    await _collect_catalyst_events(watchlist, catalyst_repo)
    await _send_due_catalyst_alerts(
        watchlist,
        catalyst_repo,
        notifier=notifier,
        today=today,
        level=level or DEFAULT_TELEGRAM_USER_LEVEL,
    )
