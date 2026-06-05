from __future__ import annotations

from datetime import date

import pytest

from stock_data_ingestion.normalization.datetime_utils import infer_bar_start_end, normalize_timestamp, now_asia_shanghai
from stock_data_ingestion.schemas.records import BarRecord


@pytest.fixture
def bar_factory():
    def _factory(provider: str = "tushare", close: float = 10.0, volume: float = 10000.0, amount: float = 100000.0, **updates):
        trade_date = date(2026, 5, 29)
        ts = normalize_timestamp(trade_date)
        start, end = infer_bar_start_end(trade_date, "1d", ts)
        data = {
            "record_type": "bar",
            "normalized_ticker": "600519.SH",
            "provider_symbol": "600519.SH" if provider == "tushare" else "sh600519",
            "exchange": "SH",
            "market": "A_share",
            "asset_type": "stock",
            "currency": "CNY",
            "trade_date": trade_date,
            "timestamp": ts,
            "timezone": "Asia/Shanghai",
            "frequency": "1d",
            "bar_start_time": start,
            "bar_end_time": end,
            "trading_session": "regular",
            "is_complete": True,
            "open": 9.5,
            "high": max(10.5, close + 0.5),
            "low": min(9.2, close - 0.8),
            "close": close,
            "pre_close": 9.4,
            "change": close - 9.4,
            "pct_change": (close - 9.4) / 9.4 * 100,
            "volume": volume,
            "volume_unit": "share",
            "amount": amount,
            "amount_unit": "CNY",
            "vwap": amount / volume if volume else None,
            "turnover_rate": 1.2,
            "turnover_rate_free_float": None,
            "adjust": "qfq",
            "adj_factor": 1.0,
            "provider": provider,
            "source_api": "daily" if provider == "tushare" else "stock_zh_a_hist",
            "source_site": provider,
            "adapter_version": "0.1.0",
            "canonical_provider": "tushare",
            "effective_provider": provider,
            "source_role": "canonical" if provider == "tushare" else "validator",
            "merge_method": "canonical_only",
            "validation_status": "unvalidated",
            "field_provenance": {
                "close": {"provider": provider, "source_api": "daily", "source_role": "canonical", "raw_payload_id": f"raw_{provider}"},
                "open": {"provider": provider, "source_api": "daily", "source_role": "canonical", "raw_payload_id": f"raw_{provider}"},
                "high": {"provider": provider, "source_api": "daily", "source_role": "canonical", "raw_payload_id": f"raw_{provider}"},
                "low": {"provider": provider, "source_api": "daily", "source_role": "canonical", "raw_payload_id": f"raw_{provider}"},
                "volume": {"provider": provider, "source_api": "daily", "source_role": "canonical", "raw_payload_id": f"raw_{provider}"},
                "amount": {"provider": provider, "source_api": "daily", "source_role": "canonical", "raw_payload_id": f"raw_{provider}"},
            },
            "supplement_flags": {},
            "conflict_ids": [],
            "canonical_value_suspect": False,
            "fetch_time": now_asia_shanghai(),
            "provider_update_time": None,
            "ingested_at": now_asia_shanghai(),
            "request_id": "req_test",
            "ingestion_run_id": "run_test",
            "request_params_hash": "sha256:test",
            "idempotency_key": "key_test",
            "raw_payload_id": f"raw_{provider}",
            "raw_payload_ref": f"raw://local/provider={provider}/request_type=historical_bars/date=2026-05-29/raw_{provider}.jsonl.gz",
            "raw_hash": "sha256:abc",
            "raw_format": "jsonl.gz",
            "raw_row_index": 0,
            "data_quality": 0.9 if provider == "tushare" else 0.75,
            "quality_flags": [],
        }
        data.update(updates)
        metadata_fields = {
            "record_id", "schema_version", "record_type", "provider", "source_api", "source_site",
            "adapter_version", "canonical_provider", "effective_provider", "source_role", "merge_method",
            "validation_status", "field_provenance", "supplement_flags", "conflict_ids",
            "canonical_value_suspect", "fetch_time", "provider_update_time", "ingested_at",
            "request_id", "ingestion_run_id", "request_params_hash", "idempotency_key",
            "raw_payload_id", "raw_payload_ref", "raw_hash", "raw_format", "raw_row_index",
            "data_quality", "quality_flags",
        }
        for field, value in list(data.items()):
            if field in metadata_fields or value is None or (isinstance(value, (list, dict)) and not value):
                continue
            data["field_provenance"].setdefault(
                field,
                {
                    "provider": provider,
                    "source_api": data["source_api"],
                    "source_role": data["source_role"],
                    "raw_payload_id": f"raw_{provider}",
                },
            )
        return BarRecord(**data)

    return _factory
