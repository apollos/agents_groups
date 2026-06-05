from __future__ import annotations

import pytest
from pydantic import ValidationError

from stock_data_ingestion.schemas.requests import StockDataRequest
from stock_data_ingestion.schemas.responses import StockDataResponse


def test_stock_data_request_example_is_valid_and_normalizes_tickers():
    req = StockDataRequest(
        request_id="req_20260529_000001",
        schema_version="stock_data_request.v0.1",
        request_type="historical_bars",
        tickers=["600519.SH", "sz000001"],
        names=["贵州茅台", "平安银行"],
        universe_id="trading_candidates_v0",
        market="A_share",
        exchanges=["SSE", "SZSE", "BSE"],
        start_date="2024-01-01",
        end_date="2026-05-29",
        frequency="1d",
        adjust="qfq",
        fields=["open", "high", "low", "close", "volume", "amount"],
        provider_priority=["tushare", "akshare", "joinquant"],
        canonical_provider="tushare",
        fallback_enabled=True,
        cross_validate=True,
        save_raw=True,
        save_cleaned=True,
        export_parquet=True,
        requested_by="manual",
        created_at="2026-05-29T10:00:00+08:00",
    )
    assert req.tickers == ["600519.SH", "000001.SZ"]
    assert req.idempotency_key is not None


def test_stock_data_request_rejects_bad_date_range():
    with pytest.raises(ValidationError):
        StockDataRequest(
            request_id="req_bad",
            request_type="historical_bars",
            tickers=["600519.SH"],
            start_date="2026-05-29",
            end_date="2024-01-01",
            frequency="1d",
        )


def test_stock_data_response_minimal_shape():
    req = StockDataRequest(request_id="req_ok", request_type="historical_bars", tickers=["600519.SH"], start_date="2026-05-01", end_date="2026-05-29", frequency="1d")
    resp = StockDataResponse(request_id="req_ok", status="success", request=req)
    assert resp.data.bars == []
    assert resp.quality_report.data_quality_score == 0.0
