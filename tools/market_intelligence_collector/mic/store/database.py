"""Database engine + session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from mic.store.models import Base


class Database:
    def __init__(self, url: str):
        self.url = url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, connect_args=connect_args)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._apply_column_migrations()

    def _apply_column_migrations(self) -> None:
        """Idempotent ALTERs for columns added after a table already exists.

        create_all only creates missing tables; databases created by earlier versions
        need the new columns added in place (no data backfill required).
        """
        inspector = inspect(self.engine)
        if "event_card" in inspector.get_table_names():
            columns = {c["name"] for c in inspector.get_columns("event_card")}
            if "tracking_variables" not in columns:
                with self.engine.begin() as con:
                    con.execute(text("ALTER TABLE event_card ADD COLUMN tracking_variables JSON"))

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


_DB: Database | None = None


def get_database(url: str) -> Database:
    """Process-wide singleton keyed by the first URL seen."""
    global _DB
    if _DB is None or _DB.url != url:
        _DB = Database(url)
        _DB.create_all()
    return _DB
