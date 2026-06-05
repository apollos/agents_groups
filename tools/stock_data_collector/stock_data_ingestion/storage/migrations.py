from __future__ import annotations

from pathlib import Path

from stock_data_ingestion.storage.database import create_sqlite_engine, init_database


def init_db(sqlite_path: str | Path, enable_wal: bool = True) -> None:
    engine = create_sqlite_engine(sqlite_path, enable_wal=enable_wal)
    init_database(engine)
