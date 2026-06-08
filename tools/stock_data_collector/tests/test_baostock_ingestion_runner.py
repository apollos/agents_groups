from __future__ import annotations

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.config import load_config
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.storage.raw_object_store import RawObjectStore


class BaoStockAdjFakeAdapter(BaseDataAdapter):
    provider_name = "baostock"
    source_site = "baostock"

    def is_available(self) -> bool:
        return True

    def authenticate(self) -> bool:
        return True

    def normalize_raw_data(self, result, request):
        return []

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return symbol

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        return ticker

    def fetch_adj_factor(self, request):
        now = now_asia_shanghai()
        return ProviderFetchResult(
            provider="baostock",
            source_api="query_adjust_factor",
            source_site="baostock",
            adapter_version="0.1.0",
            status=AdapterFetchStatus.success,
            raw_records=[
                {
                    "provider_symbol": "sh.600000",
                    "normalized_ticker": "600000.SH",
                    "exchange": "SH",
                    "market": "A_share",
                    "trade_date": "2026-05-29",
                    "factor_event_date": "2026-05-29",
                    "adj_factor": None,
                    "fore_adjust_factor": "0.75",
                    "back_adjust_factor": "12.3",
                    "event_adjust_factor": "0.75",
                    "factor_method": "baostock_pct_change_adjustment_factor",
                }
            ],
            started_at=now,
            completed_at=now,
        )


def test_runner_normalizes_baostock_adjust_factor_without_generic_adj_factor(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_DISABLE_ENV_AUTOLOAD", "true")
    load_config.cache_clear()
    config = load_config().model_copy(deep=True)
    config.storage.raw_object_root = tmp_path / "raw"
    config.storage.parquet_root = tmp_path / "parquet"
    config.data_sources.active_providers = ["baostock"]
    config.data_sources.provider_priority = ["baostock"]
    for provider, provider_config in config.data_sources.providers.items():
        provider_config.enabled = provider == "baostock"

    runner = IngestionRunner(config, RawObjectStore(config.storage.raw_object_root), adapters={"baostock": BaoStockAdjFakeAdapter()})
    request = StockDataRequest(
        request_id="req_bs_runner_adj",
        request_type="adj_factor",
        tickers=["600000.SH"],
        start_date="2026-05-01",
        end_date="2026-05-29",
        provider_priority=["baostock"],
        canonical_provider="tushare",
        export_parquet=False,
    )
    response = runner.run(request)

    assert response.status == "success"
    assert len(response.data.adj_factors) == 1
    row = response.data.adj_factors[0]
    assert row["adj_factor"] is None
    assert row["fore_adjust_factor"] == 0.75
    assert row["back_adjust_factor"] == 12.3
    assert row["field_provenance"]["fore_adjust_factor"]["provider"] == "baostock"
