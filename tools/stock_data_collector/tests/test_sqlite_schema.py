from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec("sqlalchemy") is None, reason="SQLAlchemy is required for DB tests")


def test_sqlite_schema_contains_required_tables(tmp_path):
    from sqlalchemy import inspect

    from stock_data_ingestion.storage.database import Database

    db = Database(tmp_path / "stock_data.db", enable_wal=True)
    db.init()
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    required = {
        "securities",
        "ticker_mappings",
        "trade_calendar",
        "trading_status",
        "daily_bars",
        "weekly_bars",
        "minute_bars",
        "realtime_quotes",
        "adj_factors",
        "financial_statements",
        "financial_indicators",
        "valuation_metrics",
        "industry_memberships",
        "concept_memberships",
        "money_flow",
        "indices",
        "index_bars",
        "index_constituents",
        "corporate_actions",
        "source_fetch_logs",
        "provider_comparisons",
        "data_quality_conflicts",
        "raw_payload_index",
        "ingestion_requests",
        "ingestion_runs",
    }
    assert required.issubset(tables)
