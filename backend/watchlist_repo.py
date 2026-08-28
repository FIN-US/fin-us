from typing import Callable

from sqlmodel import Session, col, select

from .models import WatchlistItem


class SqliteWatchlistRepo:
    def __init__(self, session_factory: Callable[[], Session]):
        self._session_factory = session_factory

    async def get_watchlist(self) -> list[str]:
        with self._session_factory() as session:
            # str 필드는 order_by가 SQL 라벨 문자열로도 받아 주지만, 그러면 체커의
            # 보호가 없다. 같은 str인 CatalystEvent.event_type과 기준을 맞춘다.
            items = session.exec(
                select(WatchlistItem).order_by(col(WatchlistItem.stock_name))
            ).all()
            return [item.stock_name for item in items]

    async def add_to_watchlist(self, stock: str) -> None:
        with self._session_factory() as session:
            existing = session.exec(
                select(WatchlistItem).where(WatchlistItem.stock_name == stock)
            ).first()
            if existing is None:
                session.add(WatchlistItem(stock_name=stock))
                session.commit()

    async def remove_from_watchlist(self, stock: str) -> None:
        with self._session_factory() as session:
            item = session.exec(
                select(WatchlistItem).where(WatchlistItem.stock_name == stock)
            ).first()
            if item is not None:
                session.delete(item)
                session.commit()
