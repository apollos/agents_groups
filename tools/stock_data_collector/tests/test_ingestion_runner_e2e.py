from __future__ import annotations

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.config import load_config
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorCode, ErrorRecord
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.storage.raw_object_store import RawObjectStore


class FakeAdapter(BaseDataAdapter):
    def __init__(self, provider: str, records_by_api: dict[str, list[dict]] | None = None, fail: bool = False) -> None:
        super().__init__()
        self.provider_name = provider
        self.source_site = provider
        self.records_by_api = records_by_api or {}
        self.fail = fail

    def is_available(self) -> bool:
        return True

    def authenticate(self) -> bool:
        return True

    def normalize_raw_data(self, result, request):  # pragma: no cover - runner owns normalization
        return []

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return symbol

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        return ticker

    def _result(self, api: str) -> ProviderFetchResult:
        started = now_asia_shanghai()
        if self.fail:
            return ProviderFetchResult(
                provider=self.provider_name,
                source_api=api,
                source_site=self.source_site,
                adapter_version=self.adapter_version,
                status=AdapterFetchStatus.failed,
                started_at=started,
                completed_at=now_asia_shanghai(),
                error=ErrorRecord(
                    provider=self.provider_name,
                    source_api=api,
                    source_site=self.source_site,
                    error_code=ErrorCode.PROVIDER_UNAVAILABLE,
                    error_message="fake failure",
                ),
            )
        records = self.records_by_api.get(api, [])
        return ProviderFetchResult(
            provider=self.provider_name,
            source_api=api,
            source_site=self.source_site,
            adapter_version=self.adapter_version,
            status=AdapterFetchStatus.success if records else AdapterFetchStatus.empty_result,
            raw_records=records,
            rows_fetched=len(records),
            started_at=started,
            completed_at=now_asia_shanghai(),
            error=None if records else ErrorRecord(provider=self.provider_name, source_api=api, source_site=self.source_site, error_code=ErrorCode.EMPTY_RESULT, error_message="empty"),
        )

    def fetch_security_master(self, request):
        return self._result("security_master")

    def fetch_historical_bars(self, request):
        return self._result("historical_bars")

    def fetch_industry_membership(self, request):
        return self._result("industry_membership")


def _config(tmp_path):
    load_config.cache_clear()
    config = load_config().model_copy(deep=True)
    config.storage.raw_object_root = tmp_path / "raw_objects"
    config.storage.parquet_root = tmp_path / "parquet"
    config.storage.sqlite_path = tmp_path / "stock_data.db"
    return config


def test_runner_security_master_standardizes_raw_and_response(tmp_path):
    config = _config(tmp_path)
    runner = IngestionRunner(
        config,
        RawObjectStore(config.storage.raw_object_root),
        adapters={
            "tushare": FakeAdapter(
                "tushare",
                {"security_master": [{"ts_code": "600519.SH", "name": "贵州茅台", "fullname": "贵州茅台酒股份有限公司", "list_status": "L", "industry": "白酒"}]},
            )
        },
    )
    req = StockDataRequest(request_id="req_sm", request_type="security_master", tickers=["600519.SH"], export_parquet=False)
    resp = runner.run(req)
    assert resp.status == "success"
    assert len(resp.data.securities) == 1
    row = resp.data.securities[0]
    assert row["normalized_ticker"] == "600519.SH"
    assert row["field_provenance"]["name"]["provider"] == "tushare"
    assert resp.persistence.raw_payload_ids


def test_runner_returns_structured_error_when_provider_fails(tmp_path):
    config = _config(tmp_path)
    runner = IngestionRunner(config, RawObjectStore(config.storage.raw_object_root), adapters={"tushare": FakeAdapter("tushare", fail=True)})
    req = StockDataRequest(request_id="req_fail", request_type="security_master", tickers=["600519.SH"], export_parquet=False)
    resp = runner.run(req)
    assert resp.status == "failed"
    assert resp.errors
    assert resp.errors[0].error_code == ErrorCode.PROVIDER_UNAVAILABLE


def test_runner_historical_bars_records_conflict_without_overriding_tushare(tmp_path):
    config = _config(tmp_path)
    runner = IngestionRunner(
        config,
        RawObjectStore(config.storage.raw_object_root),
        adapters={
            "tushare": FakeAdapter("tushare", {"historical_bars": [{"ts_code": "600519.SH", "trade_date": "20260529", "open": 10, "high": 11, "low": 9, "close": 10, "vol": 100, "amount": 1000}]}),
            "akshare": FakeAdapter("akshare", {"historical_bars": [{"provider_symbol": "600519.SH", "trade_date": "20260529", "open": 10, "high": 12, "low": 9, "close": 12, "volume": 10000, "amount": 100000}]}),
        },
    )
    req = StockDataRequest(request_id="req_bar", request_type="historical_bars", tickers=["600519.SH"], start_date="2026-05-29", end_date="2026-05-29", frequency="1d", adjust="none", export_parquet=False)
    resp = runner.run(req)
    assert len(resp.data.bars) == 1
    assert resp.data.bars[0]["provider"] == "tushare"
    assert resp.data.bars[0]["close"] == 10.0
    assert resp.quality_report.conflicts


def test_runner_provider_specific_append_for_industry(tmp_path):
    config = _config(tmp_path)
    runner = IngestionRunner(
        config,
        RawObjectStore(config.storage.raw_object_root),
        adapters={"akshare": FakeAdapter("akshare", {"industry_membership": [{"provider_symbol": "600519.SH", "industry_system": "eastmoney", "industry_name": "白酒", "source_methodology": "eastmoney industry"}]})},
    )
    req = StockDataRequest(request_id="req_ind", request_type="industry_concept", tickers=["600519.SH"], export_parquet=False, canonical_provider="tushare", provider_priority=["akshare"])
    resp = runner.run(req)
    assert resp.status == "success"
    assert len(resp.data.industry_memberships) == 1
    assert resp.data.industry_memberships[0]["merge_method"] == "provider_specific_append"
    assert resp.data.industry_memberships[0]["field_provenance"]["industry_name"]["source_role"] == "provider_specific"
