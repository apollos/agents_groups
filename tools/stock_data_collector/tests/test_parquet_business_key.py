from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from stock_data_ingestion.storage.parquet_store import ParquetStore


def test_parquet_business_key_keeps_latest_row_even_when_record_id_changes() -> None:
    rows = pd.DataFrame(
        [
            {
                "record_id": "rec_old",
                "normalized_ticker": "600519.SH",
                "trade_date": "2026-05-29",
                "frequency": "1d",
                "adjust": "none",
                "effective_provider": "tushare",
                "close": 100.0,
                "ingested_at": "2026-05-29T20:00:00+08:00",
            },
            {
                "record_id": "rec_new",
                "normalized_ticker": "600519.SH",
                "trade_date": "2026-05-29",
                "frequency": "1d",
                "adjust": "none",
                "effective_provider": "tushare",
                "close": 101.0,
                "ingested_at": "2026-05-29T21:00:00+08:00",
            },
        ]
    )

    deduped = ParquetStore._deduplicate(
        rows,
        business_key=["normalized_ticker", "trade_date", "frequency", "adjust", "effective_provider"],
    )

    assert len(deduped) == 1
    assert deduped.iloc[0]["record_id"] == "rec_new"
    assert deduped.iloc[0]["close"] == 101.0


def test_normalize_for_parquet_serializes_empty_json_metadata() -> None:
    df = pd.DataFrame(
        [
            {
                "normalized_ticker": "600519.SH",
                "effective_provider": "tushare",
                "supplement_flags": {},
                "conflict_ids": [],
                "quality_flags": [],
                "field_provenance": {
                    "name": {
                        "provider": "tushare",
                        "as_of": date(2026, 6, 8),
                        "seen_at": datetime(2026, 6, 8, 15, 38, 25),
                    }
                },
            }
        ]
    )

    prepared = ParquetStore._normalize_for_parquet(df)

    assert prepared.loc[0, "supplement_flags"] == "{}"
    assert prepared.loc[0, "conflict_ids"] == "[]"
    assert prepared.loc[0, "quality_flags"] == "[]"
    assert (
        prepared.loc[0, "field_provenance"]
        == '{"name": {"as_of": "2026-06-08", "provider": "tushare", "seen_at": "2026-06-08T15:38:25"}}'
    )
