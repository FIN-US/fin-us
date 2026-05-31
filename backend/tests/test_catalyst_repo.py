from datetime import date

import pytest
from sqlmodel import SQLModel, Session, create_engine

from backend.catalyst_repo import CatalystEventInput, SqliteCatalystEventRepo


@pytest.fixture()
def repo():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return SqliteCatalystEventRepo(lambda: Session(engine))


@pytest.mark.asyncio
async def test_upsert_events_deduplicates_same_catalyst(repo):
    event = CatalystEventInput(
        stock_name="삼성전자",
        stock_code="005930",
        event_type="earnings",
        event_date=date(2026, 1, 28),
        description="분기 실적 발표",
        source="manual",
    )

    first = await repo.upsert_events([event])
    second = await repo.upsert_events([event])

    assert len(first) == 1
    assert second == []
    upcoming = await repo.list_upcoming("삼성전자", today=date(2026, 1, 1))
    assert [(item.event_date, item.description) for item in upcoming] == [
        (date(2026, 1, 28), "분기 실적 발표")
    ]


@pytest.mark.asyncio
async def test_list_due_for_notification_returns_d1_and_d0_watchlist_events(repo):
    await repo.upsert_events(
        [
            CatalystEventInput(
                stock_name="삼성전자",
                stock_code="005930",
                event_type="earnings",
                event_date=date(2026, 1, 28),
                description="분기 실적 발표",
                source="manual",
            ),
            CatalystEventInput(
                stock_name="NAVER",
                stock_code="035420",
                event_type="agm",
                event_date=date(2026, 1, 29),
                description="정기 주주총회",
                source="manual",
            ),
            CatalystEventInput(
                stock_name="카카오",
                stock_code="035720",
                event_type="dividend",
                event_date=date(2026, 1, 30),
                description="배당락일",
                source="manual",
            ),
        ]
    )

    due = await repo.list_due_for_notification(
        ["삼성전자", "NAVER", "카카오"],
        today=date(2026, 1, 28),
    )

    assert [(item.stock_name, item.days_until_event) for item in due] == [
        ("삼성전자", 0),
        ("NAVER", 1),
    ]


@pytest.mark.asyncio
async def test_mark_notification_sent_suppresses_matching_d0_or_d1(repo):
    [event] = await repo.upsert_events(
        [
            CatalystEventInput(
                stock_name="삼성전자",
                stock_code="005930",
                event_type="earnings",
                event_date=date(2026, 1, 29),
                description="분기 실적 발표",
                source="manual",
            )
        ]
    )

    await repo.mark_notification_sent(event.id, days_until_event=1)

    d1_due = await repo.list_due_for_notification(["삼성전자"], today=date(2026, 1, 28))
    d0_due = await repo.list_due_for_notification(["삼성전자"], today=date(2026, 1, 29))

    assert d1_due == []
    assert [(item.id, item.days_until_event) for item in d0_due] == [(event.id, 0)]
