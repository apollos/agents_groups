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


def test_query_service_filters_quarantined_trading_ready_bars(tmp_path, bar_factory):
    from stock_data_ingestion.services.query_service import QueryService
    from stock_data_ingestion.storage.database import Database
    from stock_data_ingestion.storage.repositories import Repository

    db = Database(tmp_path / "stock_data.db")
    db.init()
    good_bar = bar_factory(adjust="none", trade_date=__import__("datetime").date(2026, 5, 28), data_quality=0.95)
    bad_bar = bar_factory(adjust="none", validation_status="quarantined", data_quality=0.99)
    with db.session() as session:
        repo = Repository(session)
        assert repo.insert_bar(good_bar) is True
        assert repo.insert_bar(bad_bar) is True
    with db.session() as session:
        df = QueryService(session).get_trading_ready_daily_bars("600519.SH", "2026-05-28", "2026-05-29", adjust="none", minimum_quality=0.8)
    assert len(df) == 1
    assert str(df.iloc[0]["trade_date"]) == "2026-05-28"


def test_query_service_uses_baostock_fore_adjust_factor_directly_for_qfq(tmp_path, bar_factory):
    from datetime import date

    from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
    from stock_data_ingestion.schemas.records import AdjFactorRecord, STANDARD_RECORD_METADATA_FIELDS
    from stock_data_ingestion.services.query_service import QueryService
    from stock_data_ingestion.storage.database import Database
    from stock_data_ingestion.storage.repositories import Repository

    def add_provenance(data: dict) -> dict:
        provenance = {}
        for field, value in data.items():
            if field in STANDARD_RECORD_METADATA_FIELDS or value is None or (isinstance(value, (list, dict)) and not value):
                continue
            provenance[field] = {
                "provider": data["provider"],
                "source_api": data["source_api"],
                "source_role": data["source_role"],
                "raw_payload_id": data["raw_payload_id"],
            }
        data["field_provenance"] = provenance
        return data

    db = Database(tmp_path / "stock_data.db")
    db.init()
    bar = bar_factory(
        provider="baostock",
        normalized_ticker="600000.SH",
        provider_symbol="sh.600000",
        exchange="SH",
        adjust="none",
        open=10.0,
        high=12.0,
        low=9.0,
        close=10.0,
        pre_close=9.8,
        adj_factor=None,
        data_quality=0.90,
        source_api="query_history_k_data_plus",
    )
    now = now_asia_shanghai()
    factor_data = {
        "record_type": "adj_factor",
        "normalized_ticker": "600000.SH",
        "provider_symbol": "sh.600000",
        "exchange": "SH",
        "market": "A_share",
        "asset_type": "stock",
        "trade_date": date(2026, 5, 29),
        "adj_factor": None,
        "fore_adjust_factor": 0.75,
        "back_adjust_factor": 12.3,
        "event_adjust_factor": 0.75,
        "factor_event_date": date(2026, 5, 29),
        "factor_method": "baostock_pct_change_adjustment_factor",
        "provider": "baostock",
        "source_api": "query_adjust_factor",
        "source_site": "baostock",
        "adapter_version": "0.1.0",
        "canonical_provider": "tushare",
        "effective_provider": "baostock",
        "source_role": "validator",
        "merge_method": "fallback_single_source",
        "validation_status": "unvalidated",
        "supplement_flags": {},
        "conflict_ids": [],
        "canonical_value_suspect": False,
        "fetch_time": now,
        "provider_update_time": None,
        "ingested_at": now,
        "request_id": "req_adj",
        "ingestion_run_id": "run_adj",
        "request_params_hash": "sha256:test",
        "idempotency_key": "key_adj",
        "raw_payload_id": "raw_baostock_adj",
        "raw_payload_ref": "raw://local/baostock/adj_factor.jsonl.gz",
        "raw_hash": "sha256:adj",
        "raw_format": "jsonl.gz",
        "raw_row_index": 0,
        "data_quality": 0.90,
        "quality_flags": [],
    }
    factor = AdjFactorRecord(**add_provenance(factor_data))
    with db.session() as session:
        repo = Repository(session)
        assert repo.insert_bar(bar) is True
        assert repo.insert_standard_record(factor)[0] is True
    with db.session() as session:
        df = QueryService(session).get_trading_ready_daily_bars("600000.SH", "2026-05-29", "2026-05-29", adjust="qfq", minimum_quality=0.8)
    assert len(df) == 1
    assert df.iloc[0]["adjust"] == "qfq"
    assert df.iloc[0]["close"] == 7.5
    assert df.iloc[0]["raw_close"] == 10.0
    assert df.iloc[0]["applied_adjust_factor"] == 0.75
