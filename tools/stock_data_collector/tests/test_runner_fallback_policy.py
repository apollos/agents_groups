from __future__ import annotations

from stock_data_ingestion.schemas.requests import StockDataRequest
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.storage.raw_object_store import RawObjectStore
from tests.test_ingestion_runner_e2e import FakeAdapter, _config


def test_runner_respects_fallback_disabled(tmp_path):
    config = _config(tmp_path)
    runner = IngestionRunner(
        config,
        RawObjectStore(config.storage.raw_object_root),
        adapters={
            "tushare": FakeAdapter("tushare", {"historical_bars": []}),
            "akshare": FakeAdapter("akshare", {"historical_bars": [{"provider_symbol": "600519.SH", "trade_date": "20260529", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "amount": 1000}]}),
        },
    )
    req = StockDataRequest(
        request_id="req_no_fb",
        request_type="historical_bars",
        tickers=["600519.SH"],
        start_date="2026-05-29",
        end_date="2026-05-29",
        frequency="1d",
        fallback_enabled=False,
        cross_validate=False,
        export_parquet=False,
    )
    resp = runner.run(req)
    assert resp.status == "failed"
    assert resp.data.bars == []
    assert [r.provider for r in resp.provider_results] == ["tushare"]
