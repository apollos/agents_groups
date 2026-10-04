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
        # Add nullable columns only: legacy rows keep unknown evidence as NULL.
        additions = {
            "event_card": {"tracking_variables": "JSON", "evidence_locator": "JSON"},
            "metric_observation": {"evidence_locator": "JSON"},
            "analysis_brief": {"uncertainty": "TEXT"},
            # Browser-route fetch/scope diagnostics; legacy rows stay NULL.
            "link_read_attempt": {"diagnostics": "JSON"},
            # Per-request traceability (requested max_tokens, finish_reason, served
            # model, response id); legacy rows stay NULL = unknown.
            "model_run": {"request_diagnostics": "JSON"},
        }
        with self.engine.begin() as con:
            inspector = inspect(con)
            tables = set(inspector.get_table_names())
            for table, fields in additions.items():
                if table not in tables:
                    continue
                existing = {c["name"] for c in inspector.get_columns(table)}
                for name, sql_type in fields.items():
                    if name not in existing:
                        # All identifiers and types are fixed constants above.
                        con.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))

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
