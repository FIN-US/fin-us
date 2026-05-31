import pytest
from sqlmodel import SQLModel, create_engine, Session

from backend.watchlist_repo import SqliteWatchlistRepo


@pytest.fixture()
def repo():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return SqliteWatchlistRepo(lambda: Session(engine))


@pytest.mark.asyncio
async def test_watchlist_is_empty_by_default(repo):
    assert await repo.get_watchlist() == []


@pytest.mark.asyncio
async def test_watchlist_add_and_list(repo):
    await repo.add_to_watchlist("삼성전자")
    await repo.add_to_watchlist("NAVER")

    result = await repo.get_watchlist()
    assert result == ["NAVER", "삼성전자"]


@pytest.mark.asyncio
async def test_watchlist_add_duplicate_is_idempotent(repo):
    await repo.add_to_watchlist("삼성전자")
    await repo.add_to_watchlist("삼성전자")

    assert await repo.get_watchlist() == ["삼성전자"]


@pytest.mark.asyncio
async def test_watchlist_remove(repo):
    await repo.add_to_watchlist("삼성전자")
    await repo.add_to_watchlist("NAVER")
    await repo.remove_from_watchlist("삼성전자")

    assert await repo.get_watchlist() == ["NAVER"]


@pytest.mark.asyncio
async def test_watchlist_remove_nonexistent_is_safe(repo):
    await repo.remove_from_watchlist("없는종목")

    assert await repo.get_watchlist() == []
