from __future__ import annotations

import importlib.util

import pandas as pd
import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec("pyarrow") is None, reason="pyarrow is required for ParquetStore tests")


def test_parquet_store_deduplicates_by_business_key_not_whole_row(tmp_path):
    from stock_data_ingestion.storage.parquet_store import ParquetStore

    store = ParquetStore(tmp_path / "parquet")
    business_key = ["normalized_ticker", "trade_date", "frequency", "adjust", "effective_provider"]
    first = {
        "record_id": "rec_old",
        "normalized_ticker": "600519.SH",
        "trade_date": "2026-05-29",
        "frequency": "1d",
        "adjust": "none",
        "effective_provider": "tushare",
        "close": 10.0,
        "ingested_at": "2026-05-29T20:00:00+08:00",
    }
    second = {
        **first,
        "record_id": "rec_new",
        "close": 10.5,
        "ingested_at": "2026-05-29T20:05:00+08:00",
    }

    store.write_records("bars", [first], partition_cols=["trade_date", "effective_provider"], business_key=business_key)
    paths = store.write_records("bars", [second], partition_cols=["trade_date", "effective_provider"], business_key=business_key)

    df = pd.read_parquet(paths[0])
    assert len(df) == 1
    assert df.iloc[0]["record_id"] == "rec_new"
    assert df.iloc[0]["close"] == 10.5


def test_parquet_store_serializes_empty_nested_metadata(tmp_path):
    from stock_data_ingestion.storage.parquet_store import ParquetStore

    store = ParquetStore(tmp_path / "parquet")
    rows = [
        {
            "record_id": "rec_security",
            "normalized_ticker": "600519.SH",
            "effective_provider": "tushare",
            "ingested_at": "2026-06-08T15:38:25+08:00",
            "field_provenance": {"name": {"provider": "tushare"}},
            "supplement_flags": {},
            "conflict_ids": [],
        }
    ]

    paths = store.write_records(
        "securities",
        rows,
        partition_cols=["effective_provider"],
        business_key=["normalized_ticker", "effective_provider"],
    )

    df = pd.read_parquet(paths[0])
    assert df.iloc[0]["field_provenance"] == '{"name": {"provider": "tushare"}}'
    assert df.iloc[0]["supplement_flags"] == "{}"
    assert df.iloc[0]["conflict_ids"] == "[]"
