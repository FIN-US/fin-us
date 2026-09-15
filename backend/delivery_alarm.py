"""텔레그램 전송 최종 실패의 메트릭과 체결 통지 정지 알람 (#259 5단계).

이 단계 전까지 전송 최종 실패의 신호는 실패 지점마다 남는 ``logger.error`` 한 줄이었다.
그 줄은 "실패했다"는 사실은 남기지만 두 가지를 말하지 못한다.

- **얼마나 자주**. 429 한 번과 한 시간째 이어지는 실패가 같은 모양이다.
- **멈췄다**. 체결 통지 재배달은 오래된 순이라 맨 앞 행 하나가 계속 실패하면 뒤가 함께
  막히는데(``scheduler.trade_notification_task``의 head-of-line 주석), 그동안 매 주기 같은
  error가 한 줄씩 쌓일 뿐이라 "방금 지나간 일시 장애"와 구분되지 않는다. 막는 행이 재배달
  창(24시간)을 벗어날 때까지 이 상태가 조용히 이어진다.

그래서 둘을 나눠 둔다.

**메트릭**(``DeliveryMetrics``)은 실패 종류별 누적 횟수다. 실패마다 ``[delivery-fail]``로
시작하는 줄을 남기고(``[kis-req]``와 같은 방식 — grep 한 줄로 셀 수 있다),
``GET /api/v1/system/delivery``가 같은 값을 돌려준다.

세는 범위는 **주문·체결 통지 경로**다 — ``send_text_settled``를 거치는 모든 전송과 체결
통지(첫 전송·재배달·마킹). ``send_text``를 직접 부르고 결과를 로그로만 남기는 경로(긴급 분석
알림·촉매 알림·모닝 브리핑·자동 제안 거부 통지·진행 메시지·폴러의 건너뜀 안내·정지 알람 자체)는
세지 않는다. 그 메시지들은 되살릴 근거도, 놓쳤을 때 주문 상태를 오인할 위험도 없어서다
(촉매 알림은 자기 마킹으로 이미 재배달된다). 그래서 0이 "텔레그램 전송 실패 없음"을 뜻하지
않는다 — README "전송 실패 신호"가 같은 말을 적는다 (PR #375 리뷰).

단위는 **사용자가 받지 못한 메시지 한 건**이다. 재시도 시도마다(settled_send)나 재배달
주기마다(fill_redelivery) 세지 않는다.

**알람**(``OutboxStallTracker``)은 조건이다. 체결 통지 outbox의 맨 앞 행이 임계 시간 동안
연속으로 실패하면 **정지 한 건에 한 번** 울리고, 그 정지가 끝날 때 어떻게 끝났는지(배달됐는가,
창을 벗어나 포기됐는가)를 한 번 더 알린다. 실패마다 울리는 알람은 위의 error 줄과 다를 게
없다. 이 모듈은 판정만 하고, 무엇을 어디로 보낼지는 호출부(스케줄러)가 정한다.

상태는 프로세스 메모리에 둔다. 재시작하면 누적 횟수는 0으로, 정지 추적은 처음으로
돌아간다. 받아들인 이유:

- 누적 횟수는 ``started_at``과 함께 나간다. "언제부터 센 값인지"가 같이 있으면 0이 "실패
  없음"으로 읽히지 않는다.
- 정지 추적이 리셋되면 알람이 **늦어진다**(임계 시간 한 번만큼). 정지가 계속되는 한 새
  프로세스에서 다시 쌓여 울리고, 판정의 근거(미통지 행)는 원장에 있어 리셋과 무관하다.
  예외는 재시작이 막는 행의 24시간 창 끝과 겹치는 경우뿐이다 — 그때는 알람 없이 끝난다.
- redis에 두면 재시작을 넘기지만, 재배달 잡 자체가 redis 락을 잡아야 돈다. redis가 죽으면
  알람보다 잡이 먼저 멈추므로, 알람 상태만 살리는 이득이 없다.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Collection, Literal, get_args

from .trade_notification_repo import to_naive_utc

logger = logging.getLogger(__name__)


DeliveryFailureKind = Literal[
    # send_text_settled가 끝내 False를 돌려준 경우. /buy 프롬프트·/cancel·/confirm 403·불명확·
    # /earnings·자연어 답변과 자연어·/earnings·/advise의 실패 통지(#259 4단계)·자동 제안 승인
    # 프롬프트가 여기로 온다. 되살릴 근거가 없는
    # 메시지들이라(send_text_settled 독스트링) 알람은 걸지 않고 횟수만 센다.
    "settled_send",
    # /confirm 체결 성공의 첫 전송 실패. 통지는 outbox가 받으므로 이것만으로는 사용자
    # 피해가 없다. 늘어나면 outbox가 일하고 있다는 뜻이다.
    "fill_notify",
    # outbox 재배달 실패. 같은 행에서 이어지면 정지 알람이 된다. **체결 한 건당 한 번만** 센다
    # (_COUNTED_ONCE_PER_TRADE).
    "fill_redelivery",
    # 전송은 나갔는데 notified_at 마킹이 실패한 경우. 다음 주기에 같은 체결이 한 번 더 나간다.
    # 발생마다 센다 — 한 번의 마킹 실패가 사용자 화면에 나가는 중복 메시지 한 건이다.
    "fill_mark",
]
DELIVERY_FAILURE_KINDS: tuple[DeliveryFailureKind, ...] = get_args(DeliveryFailureKind)

# 같은 체결을 두 번 세지 않는 종류 (PR #375 리뷰).
#
# 재배달은 1분 주기로 같은 행을 다시 시도한다. 주기마다 세면 24시간 막힌 체결 한 건이 약
# 1440건으로 부풀어, 메트릭의 단위("사용자가 못 받은 메시지")가 settled_send의 "재시도마다
# 세지 않는다"와 어긋난다. 반복 실패가 이어지는지는 last_failed_at과 정지 알람이 말한다.
#
# fill_mark는 넣지 않는다. 마킹 실패는 다음 주기에 실제로 한 번 더 나가는 메시지를 예고하므로
# 발생마다 세는 것이 같은 단위다.
_COUNTED_ONCE_PER_TRADE: frozenset[DeliveryFailureKind] = frozenset({"fill_redelivery"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


class DeliveryMetrics:
    """전송 최종 실패의 종류별 누적 횟수와 마지막 실패 시각."""

    def __init__(self, now_factory: Callable[[], datetime] = _utcnow):
        self._now_factory = now_factory
        self.reset()

    def reset(self) -> None:
        """재시작 직후와 같은 상태로 돌린다. 테스트가 전역 인스턴스를 격리하는 데 쓴다."""
        self.started_at = self._now_factory()
        self._counts: dict[DeliveryFailureKind, int] = {
            kind: 0 for kind in DELIVERY_FAILURE_KINDS
        }
        self._last_failed_at: dict[DeliveryFailureKind, datetime | None] = {
            kind: None for kind in DELIVERY_FAILURE_KINDS
        }
        # _COUNTED_ONCE_PER_TRADE 종류에서 이미 센 (종류, trade_id). 크기는 이 프로세스 동안
        # 재배달에 실패한 체결 수로 묶인다 — 주문 봇의 체결 수라 비울 필요가 없다.
        self._counted_trades: set[tuple[DeliveryFailureKind, int]] = set()

    def record_failure(self, kind: DeliveryFailureKind, *, trade_id: int | None = None) -> None:
        """실패 한 건을 센다.

        호출부의 error 줄을 대신하지 않는다. 원인(예외 메시지·조각 위치)은 그쪽에 있고,
        여기 남기는 줄은 세기 위한 것이다. 그래서 본문·종목을 싣지 않는다 — 체결 내역이
        집계 경로로 새지 않고, 세는 데 필요한 것은 종류와 횟수뿐이다.

        호출부는 전부 "예외를 올리면 안 되는" 자리다(확정된 부수효과 뒤의 전송, 스케줄러 잡).
        그래서 이 메서드는 I/O 없이 dict 갱신과 로그 한 줄로 끝난다.
        """
        # 시각은 반복 실패에도 갱신한다. "아직도 실패 중인가"는 횟수가 아니라 이 값이 말한다.
        self._last_failed_at[kind] = self._now_factory()
        if trade_id is not None and kind in _COUNTED_ONCE_PER_TRADE:
            key = (kind, trade_id)
            if key in self._counted_trades:
                # 줄도 남기지 않는다. [delivery-fail] 줄 수와 API의 횟수가 같아야 grep이 곧
                # 집계다. 매 주기의 원인 줄은 호출부의 error가 계속 남긴다.
                return
            self._counted_trades.add(key)
        self._counts[kind] += 1
        suffix = "" if trade_id is None else f" trade_id={trade_id}"
        logger.warning("[delivery-fail] kind=%s count=%d%s", kind, self._counts[kind], suffix)

    def count(self, kind: DeliveryFailureKind) -> int:
        return self._counts[kind]

    def snapshot(self) -> dict[str, object]:
        return {
            "started_at": _iso(self.started_at),
            "failures": {
                kind: {
                    "count": self._counts[kind],
                    "last_failed_at": _iso(self._last_failed_at[kind]),
                }
                for kind in DELIVERY_FAILURE_KINDS
            },
        }


@dataclass(frozen=True)
class StallCandidate:
    """이번 주기에 맨 앞에서 실패한 재배달 행."""

    trade_id: int
    # tz 없는 UTC. TradeHistory.trade_date와 같은 축이다.
    trade_date: datetime


StallEventKind = Literal[
    # 맨 앞 행이 임계 시간 동안 연속으로 실패했다. 정지 한 건에 한 번만 나온다.
    "stalled",
    # 알람이 울린 행이 결국 배달됐다.
    "delivered",
    # 알람이 울린 행이 배달되지 않은 채 재배달 창을 벗어났다. 이 체결의 통지는 더 시도되지 않는다.
    "expired",
    # 알람이 울린 행이 창 안에서 목록에서 사라졌는데 이 잡이 보낸 것은 아니다(수동 마킹 등).
    "cleared",
]


@dataclass(frozen=True)
class StallEvent:
    kind: StallEventKind
    trade_id: int
    trade_date: datetime
    failing_since: datetime
    # 첫 실패를 본 시각부터 이번 판정까지. 체결 이후의 지연이 아니라 "재배달이 막혀 있던 시간"이다.
    failing_for: timedelta


@dataclass
class _TrackedHead:
    trade_id: int
    trade_date: datetime
    failing_since: datetime
    alarmed: bool = False


class OutboxStallTracker:
    """체결 통지 outbox의 맨 앞 행이 계속 실패하는지 추적한다.

    임계를 **체결 시각으로부터의 나이**가 아니라 **연속 실패 시간**으로 잰다. 나이로 재면
    봇이 몇 시간 꺼져 있다 돌아온 뒤 첫 재배달이 429 한 번에 걸리는 것만으로 곧바로 울린다 —
    막힌 것이 아니라 막 시작한 것이다.
    """

    def __init__(self) -> None:
        self._head: _TrackedHead | None = None

    def reset(self) -> None:
        """재시작 직후와 같은 상태로 돌린다. 테스트가 전역 인스턴스를 격리하는 데 쓴다."""
        self._head = None

    def observe(
        self,
        *,
        failed_head: StallCandidate | None,
        delivered_ids: Collection[int],
        now: datetime,
        alarm_after: timedelta,
        max_age: timedelta,
    ) -> StallEvent | None:
        """재배달 한 주기의 결과를 받아 알릴 일이 있으면 돌려준다.

        ``failed_head``는 이번 주기에 **맨 앞 행**이 실패했을 때만 준다. 뒤쪽 행의 실패는
        정지가 아니다 — 앞 행이 나갔으니 대기열은 움직였고, 실패한 행은 다음 주기에 맨
        앞으로 온다.

        한 주기에 이벤트는 많아야 하나다. 추적하던 행이 끝나고 새 행이 실패하는 주기에는
        끝난 쪽의 이벤트만 나오고 새 행은 추적을 시작만 한다 — 시작한 주기에 임계를 넘을 수
        없기 때문이다(alarm_after > 0).
        """
        event: StallEvent | None = None
        tracked = self._head
        if tracked is not None and (
            failed_head is None or failed_head.trade_id != tracked.trade_id
        ):
            self._head = None
            if tracked.alarmed:
                event = StallEvent(
                    kind=self._ending_kind(tracked, delivered_ids, now=now, max_age=max_age),
                    trade_id=tracked.trade_id,
                    trade_date=tracked.trade_date,
                    failing_since=tracked.failing_since,
                    failing_for=now - tracked.failing_since,
                )
            tracked = None

        if failed_head is None:
            return event
        if tracked is None:
            self._head = _TrackedHead(
                trade_id=failed_head.trade_id,
                trade_date=failed_head.trade_date,
                failing_since=now,
            )
            return event
        if not tracked.alarmed and now - tracked.failing_since >= alarm_after:
            tracked.alarmed = True
            return StallEvent(
                kind="stalled",
                trade_id=tracked.trade_id,
                trade_date=tracked.trade_date,
                failing_since=tracked.failing_since,
                failing_for=now - tracked.failing_since,
            )
        return None

    @staticmethod
    def _ending_kind(
        tracked: _TrackedHead,
        delivered_ids: Collection[int],
        *,
        now: datetime,
        max_age: timedelta,
    ) -> StallEventKind:
        if tracked.trade_id in delivered_ids:
            return "delivered"
        # 목록 조회와 같은 경계(trade_date < now - max_age이면 빠진다)로 판정한다. 축은
        # 조회 쪽과 같은 함수로 맞춘다 — 한쪽만 aware로 남으면 비교가 터진다.
        if to_naive_utc(tracked.trade_date) < to_naive_utc(now - max_age):
            return "expired"
        return "cleared"

    def snapshot(self) -> dict[str, object] | None:
        head = self._head
        if head is None:
            return None
        return {
            "trade_id": head.trade_id,
            "failing_since": _iso(head.failing_since),
            "alarmed": head.alarmed,
        }


delivery_metrics = DeliveryMetrics()
trade_outbox_stall = OutboxStallTracker()
