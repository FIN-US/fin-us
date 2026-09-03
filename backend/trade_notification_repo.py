"""체결 통지 outbox의 저장소 (#259 2단계).

`TradeHistory.notified_at` 하나가 outbox의 전부다. 주문 경로가 체결을 기록하면서
`notified_at`을 비워 두고, 통지가 나가면 그 자리를 채운다. 채워지지 않은 행이 곧
"체결은 됐는데 사용자가 아무것도 못 받은 주문"이며, `scheduler.trade_notification_task`가
그것을 다음 주기에 다시 알린다.

`catalyst_repo`의 촉매 알림과 같은 모양이다 — 성공했을 때만 마킹하고 미통지분은 다음
주기에 재배달한다. 그쪽이 이미 이 레포에서 돌고 있는 검증된 패턴이라 형태를 맞췄다.

여기 있는 async 메서드는 안에서 동기 SQLite 호출을 한다. `catalyst_repo`와 같은 선택이며,
이유도 같다 — 호출부(스케줄러 작업)가 async라 계약을 async로 맞추는 편이 주입 지점이
단순하다.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from sqlmodel import Session, col, select

from .models import TradeHistory


@dataclass(frozen=True)
class PendingTradeNotification:
    """재배달해야 할 체결 하나. `TradeHistory` 행에서 통지에 필요한 것만 뽑아 온다.

    ORM 인스턴스를 그대로 넘기지 않는 이유는 세션 수명이다. 조회 세션이 닫힌 뒤에
    속성을 읽으면 만료된 인스턴스가 다시 DB를 물거나 터진다 — 통지 문구를 만드는
    자리에서 그런 일이 나면 outbox 자신이 통지를 못 보내는 원인이 된다.
    """

    id: int
    stock_code: str
    stock_name: str
    trade_type: str
    quantity: int
    # 0이면 "0원 거래"가 아니라 **금액 모름**이다 (#309). 통지 문구가 이를 구분해야 한다.
    price: float
    # tz 없는 UTC. `order_assist._kst_day_start_utc`의 설명과 같은 축이다.
    trade_date: datetime


class TradeNotificationRepo(Protocol):
    """`scheduler.trade_notification_task`가 outbox에 요구하는 전부 (#319의 방식).

    주문 경로가 쓰는 기록·마킹은 여기 없다. 그쪽은 동기 경로이고 `TradeRecorder`가
    맡는다 — 이 계약에 넣으면 대역이 쓰지도 않는 메서드까지 갖춰야 주입 지점을 통과한다.

    첫 인자는 위치 전용이다. 호출부가 위치로 넘기므로 이름은 계약이 아니다
    (`catalyst_repo.CatalystNotificationRepo`와 같은 규칙).
    """

    async def list_unnotified(
        self, /, *, now: datetime, grace: timedelta, max_age: timedelta, limit: int
    ) -> list[PendingTradeNotification]: ...

    async def mark_notified(self, trade_id: int, /, *, notified_at: datetime) -> None: ...


def to_naive_utc(moment: datetime) -> datetime:
    """`TradeHistory`의 시각 컬럼과 같은 축(tz 없는 UTC)으로 바꾼다.

    비교 값과 저장 값의 축이 갈리면 SQLite가 문자열로 비교하면서 조용히 어긋난다.
    읽는 쪽(`list_unnotified`)과 쓰는 쪽(`mark_trade_notified`)이 같은 함수를 쓰게 해
    한쪽만 고쳐지는 일을 막는다.
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def mark_trade_notified(
    session_factory: Callable[[], Any],
    trade_id: int,
    *,
    notified_at: datetime,
) -> None:
    """`notified_at`을 채운다. 이 컬럼에 쓰는 코드는 이 함수 하나뿐이다.

    주문 경로(`TradeRecorder.mark_notified`)와 재배달 경로(`SqliteTradeNotificationRepo`)가
    같은 컬럼을 쓴다. 두 곳이 각자 UPDATE를 들고 있으면 한쪽만 축(tz)이나 조건이 바뀌는
    날 절반의 통지가 다시 미통지로 보인다.

    이미 채워진 행은 덮지 않는다. 재배달과 주문 경로가 겹치는 경우(전송은 성공했는데
    마킹 전에 죽어 다음 주기가 같은 행을 집는 경우) 처음 통지 시각이 살아남아야 지연을
    재는 값이 뒤로 밀리지 않는다.

    세션을 ``with``가 아니라 손으로 열고 닫는다. **``TradeRecorder.record``와 같은
    ``session_factory``를 받기 때문이다** — 저쪽은 컨텍스트 매니저가 아닌 세션도 받도록
    ``rollback``·``close``를 getattr로 다루는데, 이쪽만 ``with``를 요구하면 같은 팩토리로
    한 메서드는 되고 다른 메서드는 AttributeError가 난다. 그 예외는 ``_handle_confirm``의
    ``except``에 삼켜져 로그 한 줄로 끝나고, 행은 미통지로 남아 1분 뒤 중복 배달된다.
    두 경로가 하나의 팩토리를 나눠 쓰는 한 요구 조건도 하나여야 한다.

    같은 모듈의 ``SqliteTradeNotificationRepo.list_unnotified``는 ``with``를 쓴다. 그쪽은
    스케줄러 전용이라 팩토리를 주문 경로와 나눠 쓰지 않는다.
    """
    session = session_factory()
    try:
        trade = session.get(TradeHistory, trade_id)
        if trade is None:
            return
        if trade.notified_at is not None:
            return
        trade.notified_at = to_naive_utc(notified_at)
        session.add(trade)
        session.commit()
    except Exception:
        rollback = getattr(session, "rollback", None)
        if callable(rollback):
            rollback()
        raise
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()


class SqliteTradeNotificationRepo:
    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    async def list_unnotified(
        self,
        *,
        now: datetime,
        grace: timedelta,
        max_age: timedelta,
        limit: int,
    ) -> list[PendingTradeNotification]:
        """재배달 대상 체결을 오래된 것부터 돌려준다.

        창이 양쪽으로 닫혀 있다.

        - ``grace``보다 최근인 체결은 건너뛴다. 주문 경로는 기록 → 전송 → 마킹 순서라
          전송 중인 행이 잠시 미통지로 보인다. 그 행을 집으면 사용자가 같은 체결을 두 번
          받는다 — outbox가 없앤 무응답을 중복으로 바꾸는 것은 거래가 아니다.
        - ``max_age``보다 오래된 체결도 건너뛴다. 봇이 며칠 꺼져 있었다면 그 사이 체결을
          지금 알리는 것은 복구가 아니라 소음이고, 사용자는 이미 증권사 앱에서 봤다.
          여기서 걸러진 행은 ``notified_at``이 null로 남아 "통지되지 않았다"는 사실
          자체는 원장에 그대로 보존된다.
        """
        cutoff_new = to_naive_utc(now - grace)
        cutoff_old = to_naive_utc(now - max_age)
        with self._session_factory() as session:
            rows = session.exec(
                select(TradeHistory)
                .where(
                    col(TradeHistory.notified_at).is_(None),
                    TradeHistory.trade_date <= cutoff_new,
                    TradeHistory.trade_date >= cutoff_old,
                )
                .order_by(col(TradeHistory.trade_date))
                .limit(limit)
            ).all()

        pending: list[PendingTradeNotification] = []
        for row in rows:
            if row.id is None:
                continue
            pending.append(
                PendingTradeNotification(
                    id=row.id,
                    stock_code=row.stock_code,
                    stock_name=row.stock_name,
                    trade_type=row.trade_type,
                    quantity=row.quantity,
                    price=row.price,
                    trade_date=row.trade_date,
                )
            )
        return pending

    async def mark_notified(self, trade_id: int, *, notified_at: datetime) -> None:
        mark_trade_notified(self._session_factory, trade_id, notified_at=notified_at)
