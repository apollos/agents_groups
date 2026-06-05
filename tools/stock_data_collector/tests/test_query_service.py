from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec("sqlalchemy") is None, reason="SQLAlchemy is required for DB tests")


def test_query_service_get_bars_and_unique_skip(tmp_path, bar_factory):
    from stock_data_ingestion.services.query_service import QueryService
    from stock_data_ingestion.storage.database import Database
    from stock_data_ingestion.storage.repositories import Repository

    db = Database(tmp_path / "stock_data.db")
    db.init()
    bar = bar_factory()
    with db.session() as session:
        repo = Repository(session)
        assert repo.insert_bar(bar) is True
    with db.session() as session:
        repo = Repository(session)
        assert repo.insert_bar(bar) is False
    with db.session() as session:
        df = QueryService(session).get_bars("600519.SH", "2026-05-29", "2026-05-29", "1d", "qfq")
    assert len(df) == 1
    assert df.iloc[0]["normalized_ticker"] == "600519.SH"
