from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import JSON, Boolean, Column, Float, Integer, create_engine, event, inspect, text
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


def _sqlite_default_literal(column: Column) -> str | None:
    """DEFAULT clause for ALTER TABLE ADD COLUMN; None when the column can be NULL."""
    if column.server_default is not None and hasattr(column.server_default, "arg"):
        arg = column.server_default.arg
        return repr(str(arg)) if isinstance(arg, str) else str(arg)
    if column.nullable:
        return None
    if isinstance(column.type, JSON):
        return "'{}'"
    if isinstance(column.type, Boolean):
        return "0"
    if isinstance(column.type, (Integer, Float)):
        return "0"
    return "''"


def ensure_columns(engine: Engine) -> dict[str, list[str]]:
    """Add columns that exist in the models but not in an already-created SQLite table.

    ``Base.metadata.create_all`` only creates missing *tables*; databases created by an
    older version keep their old column set. Legacy rows get the column default (NULL for
    nullable columns), which callers must treat as "unknown", never as a confirmed value.
    Returns ``{table_name: [added columns]}``.
    """
    inspector = inspect(engine)
    added: dict[str, list[str]] = {}
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(dialect=engine.dialect)}"
                default = _sqlite_default_literal(column)
                if default is not None:
                    ddl += f" NOT NULL DEFAULT {default}" if not column.nullable else f" DEFAULT {default}"
                conn.execute(text(ddl))
                added.setdefault(table.name, []).append(column.name)
    return added


def init_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    ensure_columns(engine)
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
