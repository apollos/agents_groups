from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from stock_data_ingestion.storage.models import Base


def build_sqlite_url(path: str | Path) -> str:
    return f"sqlite:///{Path(path)}"


def create_sqlite_engine(sqlite_path: str | Path, enable_wal: bool = True, echo: bool = False) -> Engine:
    path = Path(sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(build_sqlite_url(path), future=True, echo=echo)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        if enable_wal:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def init_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA optimize"))


class Database:
    def __init__(self, sqlite_path: str | Path, enable_wal: bool = True, echo: bool = False) -> None:
        self.engine = create_sqlite_engine(sqlite_path, enable_wal=enable_wal, echo=echo)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, class_=Session, future=True)

    def init(self) -> None:
        init_database(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
