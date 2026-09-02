from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Protocol

from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from .models import CatalystEvent


class CatalystEventInput(BaseModel):
    stock_name: str = Field(min_length=1)
    stock_code: str | None = None
    event_type: str = Field(min_length=1)
    event_date: date
    description: str = Field(min_length=1)
    source: str = Field(default="manual", min_length=1)


@dataclass(frozen=True)
class DueCatalystEvent:
    id: int
    stock_name: str
    stock_code: str | None
    event_type: str
    event_date: date
    description: str
    source: str
    days_until_event: int


class CatalystNotificationRepo(Protocol):
    """catalyst_calendar_task가 촉매 저장소에 요구하는 전부 (#319).

    ``list_upcoming``은 일부러 뺐다. 그건 /catalysts 명령이 사용자에게 목록을 보여
    주려고 쓰는 조회이고, 이 작업은 "오늘·내일 알릴 것"만 본다. 계약을 구체 클래스로
    두면 대역이 쓰지도 않는 메서드까지 갖춰야 주입 지점을 통과한다.

    첫 인자는 위치 전용(``/``)이다. 호출부가 전부 위치로 넘기므로 이름은 계약이
    아니고, 이름까지 고정하면 대역이 자기 도메인 이름(``watchlist`` 등)을 쓰지 못한다.
    ``today``·``days_until_event``는 반대로 호출부가 키워드로 넘기므로 그대로 둔다 —
    그 이름들은 실제로 계약이다.
    """

    async def upsert_events(
        self, events: list[CatalystEventInput], /
    ) -> list[CatalystEvent]: ...

    async def list_due_for_notification(
        self, stock_names: list[str], /, *, today: date
    ) -> list[DueCatalystEvent]: ...

    async def mark_notification_sent(
        self, event_id: int, /, *, days_until_event: int
    ) -> None: ...


class SqliteCatalystEventRepo:
    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    async def upsert_events(self, events: list[CatalystEventInput]) -> list[CatalystEvent]:
        created: list[CatalystEvent] = []
        with self._session_factory() as session:
            for event in events:
                existing = session.exec(
                    select(CatalystEvent).where(
                        CatalystEvent.stock_name == event.stock_name,
                        CatalystEvent.event_type == event.event_type,
                        CatalystEvent.event_date == event.event_date,
                        CatalystEvent.description == event.description,
                    )
                ).first()
                if existing is not None:
                    continue

                model = CatalystEvent(**event.model_dump())
                session.add(model)
                created.append(model)

            if created:
                session.commit()
                for model in created:
                    session.refresh(model)
        return created

    async def list_upcoming(
        self,
        stock_name: str,
        *,
        today: date,
        limit: int = 20,
    ) -> list[CatalystEvent]:
        with self._session_factory() as session:
            return list(
                session.exec(
                    select(CatalystEvent)
                    .where(
                        CatalystEvent.stock_name == stock_name,
                        CatalystEvent.event_date >= today,
                    )
                    .order_by(col(CatalystEvent.event_date), col(CatalystEvent.event_type))
                    .limit(limit)
                ).all()
            )

    async def list_due_for_notification(
        self,
        stock_names: list[str],
        *,
        today: date,
    ) -> list[DueCatalystEvent]:
        if not stock_names:
            return []

        tomorrow = today + timedelta(days=1)
        with self._session_factory() as session:
            events = session.exec(
                select(CatalystEvent)
                .where(
                    col(CatalystEvent.stock_name).in_(stock_names),
                    CatalystEvent.event_date >= today,
                    CatalystEvent.event_date <= tomorrow,
                )
                .order_by(col(CatalystEvent.event_date), col(CatalystEvent.stock_name))
            ).all()

        due: list[DueCatalystEvent] = []
        for event in events:
            days_until_event = (event.event_date - today).days
            if days_until_event == 1 and event.d1_notified:
                continue
            if days_until_event == 0 and event.d0_notified:
                continue
            if event.id is None:
                continue
            due.append(
                DueCatalystEvent(
                    id=event.id,
                    stock_name=event.stock_name,
                    stock_code=event.stock_code,
                    event_type=event.event_type,
                    event_date=event.event_date,
                    description=event.description,
                    source=event.source,
                    days_until_event=days_until_event,
                )
            )
        return due

    async def mark_notification_sent(self, event_id: int, *, days_until_event: int) -> None:
        with self._session_factory() as session:
            event = session.get(CatalystEvent, event_id)
            if event is None:
                return
            if days_until_event == 1:
                event.d1_notified = True
            if days_until_event == 0:
                event.d0_notified = True
                event.notified = True
            session.add(event)
            session.commit()
