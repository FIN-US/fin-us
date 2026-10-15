"""체결 통지 outbox 재배달과 정지 알람."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlmodel import Session

from backend.core.database import engine
from backend.core.timeutil import KST
from backend.repositories.trade_notification import (
    PendingTradeNotification,
    SqliteTradeNotificationRepo,
    TradeNotificationRepo,
)
from backend.scheduler import deps
from backend.telegram.delivery_alarm import (
    delivery_metrics,
    StallCandidate,
    StallEvent,
    trade_outbox_stall,
)
from backend.telegram.notifier import telegram_notifier, TelegramTextSender

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 체결 통지 outbox (#259 2단계)
# ---------------------------------------------------------------------------

# 재배달 주기. 사용자는 "주문이 나갔나?"를 기다리는 중이므로 촉매 알림(하루 한 번)과 같은
# 리듬을 쓸 수 없다. 대상은 전송이 실패했을 때만 생기고 조회는 작은 테이블의 부분 스캔이라,
# 1분마다 도는 비용은 ping_task와 비슷한 수준이다.
TRADE_NOTIFY_INTERVAL_SECONDS = 60
# 이만큼 지난 체결만 집는다. 주문 경로는 기록 → 전송 → 마킹 순서라, 전송 중인 행이 잠시
# 미통지로 보인다. 그 행을 집으면 같은 체결이 두 번 나간다 — outbox가 없애려는 무응답을
# 중복으로 바꾸는 것은 거래가 아니다.
#
# 그 구간의 상한은 **10초 × 조각 수**다. send_text는 split_for_telegram의 조각마다
# _post_message를 따로 치고, 각 요청에 httpx 타임아웃 10초가 붙기 때문이다. 조각 수에
# 상한이 없으면 이 유예도 근거가 없다.
#
# 체결 통지에는 상한이 있다. 문구가 "주문 완료: " + OrderExecutionResult.message인데,
# 그 message는 _extract_order_message가 모든 분기에서 500자로 자른다. 507자면
# TELEGRAM_MESSAGE_LIMIT(4000) 안이라 항상 한 조각이고, 따라서 실제 상한은 10초다.
# 60초는 그 위의 6배 여유다. 이 전제는 추론이 아니라 테스트로 고정돼 있다
# (test_fill_notification_is_always_a_single_telegram_part) — 깨지면 유예를
# 10초 × 조각 수 위로 올려야 한다.
TRADE_NOTIFY_GRACE = timedelta(seconds=60)
# 이보다 오래된 체결은 알리지 않는다. 봇이 며칠 꺼져 있었다면 그 사이 체결을 지금 알리는
# 것은 복구가 아니라 소음이고, 사용자는 이미 증권사 앱에서 봤다. 걸러진 행의 notified_at은
# null로 남아 "통지되지 않았다"는 사실 자체는 원장에 보존된다.
TRADE_NOTIFY_MAX_AGE = timedelta(hours=24)
# 한 주기에 보낼 최대 건수. 채팅당 초당 ~1건 제한이 있어 한 번에 쏟으면 429를 스스로
# 만들고, 그 429가 다시 미통지를 낳는다.
TRADE_NOTIFY_BATCH_LIMIT = 5
# 이 잡의 실행 락 만료. 기본값(SCHEDULER_LOCK_TTL_SEC, 30분)을 그대로 쓰면 누수 한 번이
# 30분 정지가 되는데, 이 잡에서 정지는 곧 "체결됐는데 아무 말도 없는" 시간이다. 주기의
# 두 배로 잘라 누수의 대가를 최대 두 주기로 묶는다. 한 주기가 이 시간을 넘길 일은 없다 —
# 배치가 5건이고 첫 실패에서 끊는다.
TRADE_NOTIFY_LOCK_TTL_SECONDS = TRADE_NOTIFY_INTERVAL_SECONDS * 2
# 정지 알람의 임계 (#259 5단계). 재배달의 맨 앞 행이 이만큼 **연속으로** 실패하면 한 번 울린다.
#
# 짧으면 일시 장애에 울린다. 이 경로의 지배적 실패는 채팅 단위 429이고, 그 flood-wait은
# _send_part_settled 주석이 적은 대로 흔히 30초 이상이다 — 1~2주기를 잃는 것은 정상 범위다.
# 10분은 주기로 열 번 연속 실패한 것이라 그 범위를 넘는다.
#
# 길면 알람의 뜻이 사라진다. 이 알람이 없을 때의 최악은 TRADE_NOTIFY_MAX_AGE(24시간)
# 동안 매 주기 error 한 줄뿐인 정지였고, 사용자는 그동안 "주문이 나갔나?"를 모른다.
TRADE_NOTIFY_STALL_ALARM_AFTER = timedelta(minutes=10)


def _default_trade_notification_repo() -> SqliteTradeNotificationRepo:
    return SqliteTradeNotificationRepo(lambda: Session(engine))


def _as_kst(moment: datetime) -> datetime:
    """tz 없는 UTC로 저장된 시각을 KST로 읽는다.

    ``TradeHistory``의 시각 컬럼은 tz 없는 UTC다(``order_assist._kst_day_start_utc``의
    설명). tzinfo를 붙이지 않고 그대로 표시하면 사용자에게 9시간 이른 체결 시각이 나간다.
    """
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(KST)


def _format_trade_notification(pending: PendingTradeNotification) -> str:
    """재배달용 체결 통지. 원문이 아니라 원장에서 다시 세운 문장이다.

    원래 통지는 KIS 응답 문구(``OrderExecutionResult.message``)를 실었지만, outbox가 들고
    있는 것은 체결 사실뿐이라 그 문구를 되살릴 수 없다. 되살리려면 응답 원문을 한 컬럼 더
    저장해야 하는데, 사용자가 이 시점에 알아야 하는 것은 "무엇이 얼마나 나갔는가"이지
    증권사 응답 문자열이 아니다.

    첫 줄이 재전송임을 밝힌다. 마킹 전에 프로세스가 죽는 경우처럼 이미 받은 통지가 한 번
    더 나갈 수 있고, 그때 이 줄이 없으면 사용자는 주문이 두 번 나간 것으로 읽는다.
    """
    side = "매수" if pending.trade_type == "BUY" else "매도"
    # 0은 "0원 거래"가 아니라 금액 모름이다 (#309). 0원으로 적으면 없던 사실이 생긴다.
    price = f"{int(pending.price):,}원" if pending.price > 0 else "단가 미상"
    when = _as_kst(pending.trade_date)
    return "\n".join(
        [
            "🕘 체결 통지가 늦었습니다 (전송 실패 후 재전송)",
            f"- 종목: {pending.stock_name} ({pending.stock_code})",
            f"- 주문: {side} {pending.quantity:,}주 · {price}",
            f"- 체결 시각: {when:%Y-%m-%d %H:%M} KST",
            "이미 받은 통지라면 같은 체결이 두 번 보인 것입니다 — 주문은 한 번만 나갔습니다.",
        ]
    )


async def trade_notification_task(
    *,
    repo: TradeNotificationRepo | None = None,
    notifier: TelegramTextSender = telegram_notifier,
    now_factory: Callable[[], datetime] | None = None,
    use_redis_lock: bool = True,
) -> None:
    """통지가 나가지 않은 체결을 다시 알린다 (#259 2단계).

    ``/confirm``의 체결 성공은 다섯 개의 settled 경로 중 유일하게 복구 불가였다 — 주문은
    나갔는데 전송이 최종 실패하면 로그 한 줄만 남고 사용자는 아무것도 받지 못했다. 그
    자리에서 더 오래 재시도하는 것은 답이 아니다. 재시도가 폴러 루프 안에 있는 한 같은
    배치의 다른 update가 예산을 잃기 때문이다. 그래서 전송 책임을 루프 밖의 이 작업으로
    옮겼다.

    ``backend/scheduler/catalyst.py``의 촉매 알림과 같은 모양이다 — 성공했을 때만 마킹하고 미통지분은
    다음 주기에 재배달한다. 그쪽이 이미 이 레포에서 돌고 있는 검증된 패턴이다.

    전송에 ``send_text_settled``를 쓰지 않는다. 여기서는 재시도의 단위가 함수 호출이 아니라
    **주기**다. 그 자리에서 20초를 더 기다려 봐야 같은 429 구간이고, 1분 뒤 주기가 같은 일을
    공짜로 한다. "재시도 간격을 어디서 결정하는가"의 답을 outbox 한 곳으로 모은다.
    """
    if use_redis_lock:
        try:
            async with deps.redis_state() as state:
                scheduler_token = await state.acquire_scheduler_lock(
                    "trade_notification", ttl_sec=TRADE_NOTIFY_LOCK_TTL_SECONDS
                )
                if scheduler_token is None:
                    logger.info("다른 worker가 체결 통지 재배달을 실행 중입니다. 이번 실행을 건너뜁니다.")
                    return
                try:
                    await trade_notification_task(
                        repo=repo,
                        notifier=notifier,
                        now_factory=now_factory,
                        use_redis_lock=False,
                    )
                finally:
                    await state.release_lock(
                        state.keys.scheduler_lock("trade_notification"),
                        scheduler_token,
                    )
        except Exception as e:
            logger.error("Redis 기반 체결 통지 재배달 실행 중 오류: %s", e)
        return

    if not getattr(notifier, "enabled", False):
        # 보낼 곳이 없다. 마킹하지 않고 그대로 두면 텔레그램이 다시 켜졌을 때 창(24시간)
        # 안의 체결은 그때 나간다.
        return

    if repo is None:
        repo = _default_trade_notification_repo()
    now = now_factory() if now_factory is not None else datetime.now(timezone.utc)

    try:
        pending = await repo.list_unnotified(
            now=now,
            grace=TRADE_NOTIFY_GRACE,
            max_age=TRADE_NOTIFY_MAX_AGE,
            limit=TRADE_NOTIFY_BATCH_LIMIT,
        )
    except Exception as e:
        logger.error("체결 통지 재배달 대상 조회 중 오류: %s", e)
        return

    # 정지 판정의 재료 (#259 5단계). 전송이 나간 행(마킹 실패 포함)과, 맨 앞 행이 실패했다면 그 행.
    delivered: set[int] = set()
    failed_head: StallCandidate | None = None
    for position, trade in enumerate(pending):
        try:
            sent = await notifier.send_text(_format_trade_notification(trade))
        except Exception as e:
            # send_text는 실패를 False로 접어 오므로 여기 걸리는 것은 duck-typed notifier뿐이다.
            logger.error("[%s] 체결 통지 재배달 중 오류: %s", trade.stock_name, e)
            failed = True
        else:
            failed = sent is False
            if failed:
                # 마킹하지 않는 것이 곧 재시도다 — 다음 주기가 같은 행을 다시 집는다.
                #
                # 남은 건을 계속 보내지 않고 배치를 끊는다. 이 경로의 지배적 실패 원인은 채팅
                # 단위 rate limit이라, 실패 뒤의 전송은 성공할 가능성이 낮으면서 ban만 늘린다.
                #
                # 대가: 목록이 오래된 순이라 맨 앞 행이 계속 실패하면 그 뒤의 신규 통지가 함께
                # 막힌다. rate limit이라는 전제에서는 뒤엣것도 어차피 못 나가므로 손해가 없지만,
                # 전제가 깨져 특정 행만 실패하는 경우(그 행 하나 때문에 뒤가 밀리는 경우)에는
                # head-of-line 정지가 된다. 영구하지는 않다 — 막는 행이 TRADE_NOTIFY_MAX_AGE를
                # 넘기면 목록에서 빠지고 뒤가 흐른다. 즉 최악이 24시간이다.
                #
                # 그 정지를 조용히 두지 않는 것이 아래 trade_outbox_stall이다 (#259 5단계).
                logger.error("체결 통지 재배달 실패 — 다음 주기로 미룬다 (trade_id=%s)", trade.id)
        if failed:
            delivery_metrics.record_failure("fill_redelivery", trade_id=trade.id)
            if position == 0:
                failed_head = StallCandidate(trade_id=trade.id, trade_date=trade.trade_date)
            break
        delivered.add(trade.id)
        try:
            await repo.mark_notified(trade.id, notified_at=now)
        except Exception as e:
            # 전송은 이미 나갔다. 마킹만 실패하면 다음 주기가 같은 체결을 한 번 더 알린다.
            # 문구가 재전송임을 밝히므로 중복은 읽을 수 있는 형태로 드러난다.
            logger.error("체결 통지 마킹 실패 — 중복 배달 가능 (trade_id=%s): %s", trade.id, e)
            delivery_metrics.record_failure("fill_mark", trade_id=trade.id)

    event = trade_outbox_stall.observe(
        failed_head=failed_head,
        delivered_ids=delivered,
        now=now,
        alarm_after=TRADE_NOTIFY_STALL_ALARM_AFTER,
        max_age=TRADE_NOTIFY_MAX_AGE,
    )
    if event is not None:
        await _report_trade_outbox_stall(event, notifier)


def _format_trade_outbox_stall_alarm(event: StallEvent) -> str | None:
    """정지 알람의 텔레그램 문구. 사용자에게 알릴 것이 없는 이벤트는 None.

    **종목·수량을 싣지 않는다.** 이 알람이 텔레그램으로 닿는 경우는 사실상 하나다 — 채팅
    전체가 막힌 rate limit이면 이 메시지도 같이 실패하므로, 닿았다면 실패가 그 행에만
    걸려 있다는 뜻이고 그 원인으로 가장 먼저 의심할 것이 행의 내용(재배달 문구)이다. 같은
    내용을 다시 실으면 알람이 같은 이유로 막힌다. 체결 시각만으로 증권사 앱에서 찾을 수 있다.
    """
    when = _as_kst(event.trade_date)
    if event.kind == "stalled":
        minutes = int(event.failing_for.total_seconds() // 60)
        return "\n".join(
            [
                "⚠️ 체결 통지가 전달되지 않고 있습니다",
                f"- 체결 시각: {when:%Y-%m-%d %H:%M} KST",
                f"- 재배달 실패: {minutes}분째 (계속 시도합니다)",
                "증권사 앱에서 체결 내역을 확인하세요.",
            ]
        )
    if event.kind == "expired":
        hours = int(TRADE_NOTIFY_MAX_AGE.total_seconds() // 3600)
        return "\n".join(
            [
                "⚠️ 체결 통지 1건을 끝내 전달하지 못했습니다",
                f"- 체결 시각: {when:%Y-%m-%d %H:%M} KST",
                f"- 체결 후 {hours}시간이 지나 더 시도하지 않습니다",
                "증권사 앱에서 체결 내역을 확인하세요.",
            ]
        )
    # delivered: 사용자는 방금 재배달된 통지 자체를 받았다. 덧붙일 말이 없다.
    # cleared: 이 잡이 보낸 것이 아니라 사용자에게 할 말이 없다. 운영 쪽 로그로 충분하다.
    return None


async def _report_trade_outbox_stall(event: StallEvent, notifier: TelegramTextSender) -> None:
    """정지 이벤트를 로그와 텔레그램으로 내보낸다 (#259 5단계).

    로그는 ``[delivery-alarm]``으로 시작한다. 이 봇에 텔레그램 밖의 운영 채널은 없다 —
    그래서 이 줄이 grep·로그 기반 경보가 붙을 자리이고, 텔레그램이 막힌 경우의 유일한 신호다.

    텔레그램 전송은 **한 번만** 시도하고 실패해도 다시 보내지 않는다. 이 함수는 재배달이
    방금 실패한 주기에 불리므로, 위의 "실패 뒤의 전송은 ban만 늘린다"와 부딪친다. 정지
    한 건에 한 번이라 추가 요청이 유계이고, 닿기만 하면 사용자가 증권사 앱을 열 이유가
    생긴다는 쪽을 골랐다. 알람을 재시도하기 시작하면 그것이 새 정지의 원인이 된다.
    """
    level = logging.CRITICAL if event.kind in ("stalled", "expired") else logging.WARNING
    logger.log(
        level,
        "[delivery-alarm] kind=%s trade_id=%s failing_for=%ds failing_since=%s",
        event.kind,
        event.trade_id,
        int(event.failing_for.total_seconds()),
        event.failing_since.isoformat(),
    )
    text = _format_trade_outbox_stall_alarm(event)
    if text is None:
        return
    try:
        sent = await notifier.send_text(text)
    except Exception as e:
        logger.error("체결 통지 정지 알람 전송 중 오류 (trade_id=%s): %s", event.trade_id, e)
        return
    if sent is False:
        logger.error(
            "체결 통지 정지 알람을 텔레그램으로 보내지 못했습니다 — 위 [delivery-alarm] 줄이 "
            "유일한 신호입니다 (trade_id=%s)",
            event.trade_id,
        )
