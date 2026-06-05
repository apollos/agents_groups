from __future__ import annotations

import gzip
import json

from stock_data_ingestion.storage.raw_object_store import RawObjectStore


def test_raw_object_store_write_read_hash_and_index(tmp_path):
    store = RawObjectStore(tmp_path / "raw_objects")
    index = store.save_raw_payload(
        provider="tushare",
        request_type="historical_bars",
        source_api="daily",
        source_site="tushare",
        adapter_version="0.1.0",
        request_id="req_20260529_000001",
        ingestion_run_id="run_20260529_000001",
        sanitized_request_params={"tickers": ["600519.SH"], "start_date": "2026-05-01", "end_date": "2026-05-29"},
        raw_records=[{"ts_code": "600519.SH", "trade_date": "20260529", "close": 1688.0}],
        idempotency_key="key",
    )
    assert index.raw_payload_ref.startswith("raw://local/")
    assert index.rows_fetched == 1
    path = store.parse_raw_payload_ref(index.raw_payload_ref)
    assert path.suffix == ".gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        first = json.loads(f.readline())
    assert first["line_type"] == "metadata"
    assert first["raw_format"] == "jsonl.gz"
    assert store.verify_raw_hash(index.raw_payload_ref, index.raw_hash)
    metadata, rows = store.load_raw_payload(index.raw_payload_ref)
    assert metadata["raw_payload_id"] == index.raw_payload_id
    assert rows[0]["raw_row_index"] == 0
    assert store.read_raw_record_by_index(index.raw_payload_id, 0)["raw_data"]["close"] == 1688.0
