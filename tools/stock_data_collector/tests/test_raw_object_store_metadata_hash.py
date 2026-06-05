from __future__ import annotations

from stock_data_ingestion.storage.raw_object_store import RawObjectStore


def test_raw_metadata_contains_verifiable_raw_hash(tmp_path):
    store = RawObjectStore(tmp_path)
    idx = store.save_raw_payload(
        provider="tushare",
        request_type="security_master",
        source_api="stock_basic",
        source_site="tushare",
        adapter_version="0.1.0",
        request_id="req_hash",
        ingestion_run_id="run_hash",
        sanitized_request_params={"tickers": ["600519.SH"]},
        raw_records=[{"ts_code": "600519.SH", "name": "贵州茅台"}],
        idempotency_key="key_hash",
    )
    metadata, _ = store.load_raw_payload(idx.raw_payload_ref)
    assert metadata["raw_hash"] == idx.raw_hash
    assert store.verify_raw_hash(idx.raw_payload_ref, metadata["raw_hash"])
