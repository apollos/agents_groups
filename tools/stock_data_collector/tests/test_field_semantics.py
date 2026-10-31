from __future__ import annotations

import json

import pytest

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.config import load_config
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.services.query_service import QueryService
from stock_data_ingestion.storage.database import Database
from stock_data_ingestion.storage.raw_object_store import RawObjectStore
from stock_data_ingestion.validation.merge_policy import apply_canonical_merge_policy

TICKER = "603986.SH"

# Shapes and values follow the r3 raw rows of 603986.SH (trade date 2026-10-30).
TUSHARE_DAILY_BASIC = {
    "ts_code": TICKER, "trade_date": "20261030", "pe": 120.5, "pe_ttm": 95.3, "pb": 10.2, "ps": 19.7191,
    "ps_ttm": 8.0057, "total_mv": 18148383.3881, "circ_mv": 18100000.0, "turnover_rate": 2.1, "volume_ratio": 1.1,
}
AKSHARE_VALUE_EM = {
    "provider_symbol": TICKER, "raw_source_api": "stock_value_em", "trade_date": "20261030", "PE(TTM)": 95.3,
    "PE(静)": 120.5, "市净率": 10.2, "市销率": 8.00574495, "总市值": 181483833880.6, "流通市值": 181000000000.0,
}
BAOSTOCK_K_DATA = {
    "provider_symbol": TICKER, "raw_source_api": "query_history_k_data_plus", "date": "2026-10-30",
    "peTTM": 95.3, "pbMRQ": 10.2, "psTTM": 8.0057, "pcfNcfTTM": 50.0,
}
TUSHARE_FINA_INDICATOR = {
    "ts_code": TICKER, "ann_date": "20261030", "end_date": "20260930", "eps": 1.6, "roe": 8.5,
    "gross_margin": 13441877926.57, "grossprofit_margin": 66.2247, "netprofit_margin": 54.7926,
    "current_ratio": 5.0, "debt_to_assets": 15.0,
}
BAOSTOCK_FINANCIAL = {
    "provider_symbol": TICKER, "raw_source_api": "query_balance_data", "report_period": "20260930",
    "statDate": "2026-09-30", "pubDate": "2026-10-30", "roeAvg": 0.085, "gpMargin": 0.662247,
    "npMargin": 0.547926, "epsTTM": 1.6, "currentRatio": 5.0, "liabilityToAsset": 0.15,
}


class FakeAdapter(BaseDataAdapter):
    def __init__(self, provider: str, source_api: str, records_by_type: dict[str, list[dict]]) -> None:
        super().__init__()
        self.provider_name = provider
        self.source_site = provider
        self.source_api = source_api
        self.records_by_type = records_by_type

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

    def _result(self, request_type: str) -> ProviderFetchResult:
        records = [dict(row) for row in self.records_by_type.get(request_type, [])]
        return ProviderFetchResult(
            provider=self.provider_name,
            source_api=self.source_api,
            source_site=self.source_site,
            adapter_version=self.adapter_version,
            status=AdapterFetchStatus.success if records else AdapterFetchStatus.empty_result,
            raw_records=records,
            rows_fetched=len(records),
            started_at=now_asia_shanghai(),
            completed_at=now_asia_shanghai(),
        )

    def fetch_valuation_metric(self, request):
        return self._result("valuation_metric")

    def fetch_financial_indicator(self, request):
        return self._result("financial_indicator")


def _runner(tmp_path, adapters):
    load_config.cache_clear()
    config = load_config().model_copy(deep=True)
    config.storage.raw_object_root = tmp_path / "raw_objects"
    config.storage.parquet_root = tmp_path / "parquet"
    config.storage.sqlite_path = tmp_path / "stock_data.db"
    config.data_sources.active_providers = None
    for name in ("tushare", "akshare", "baostock"):
        config.data_sources.providers[name].enabled = True
    database = Database(config.storage.sqlite_path)
    database.init()
    return IngestionRunner(config, RawObjectStore(config.storage.raw_object_root), database=database, adapters=adapters), config


def _request(request_type: str, providers: list[str]) -> StockDataRequest:
    return StockDataRequest(
        request_id=f"req_{request_type}", request_type=request_type, tickers=[TICKER], start_date="2026-09-01",
        end_date="2026-10-30", export_parquet=False, canonical_provider="tushare", provider_priority=providers,
        cross_validate=True,
    )


def _provenance(row: dict) -> dict:
    value = row["field_provenance"]
    return json.loads(value) if isinstance(value, str) else value


def _comparison(resp, provider: str):
    return next(cmp for cmp in resp.provider_comparisons if cmp.compared_provider == provider)


def test_market_value_converted_once_and_ps_kept_apart_from_ps_ttm(tmp_path):
    runner, config = _runner(tmp_path, {
        "tushare": FakeAdapter("tushare", "daily_basic", {"valuation_metric": [TUSHARE_DAILY_BASIC]}),
        "akshare": FakeAdapter("akshare", "stock_value_em+stock_zh_a_spot_em", {"valuation_metric": [AKSHARE_VALUE_EM]}),
        "baostock": FakeAdapter("baostock", "query_history_k_data_plus.valuation", {"valuation_metric": [BAOSTOCK_K_DATA]}),
    })
    resp = runner.run(_request("valuation_metric", ["tushare", "akshare", "baostock"]))

    row = resp.data.valuation_metrics[0]
    assert row["provider"] == "tushare"
    assert row["total_market_value"] == pytest.approx(181483833881.0)
    assert row["ps"] == 19.7191
    assert row["ps_ttm"] == 8.0057
    prov = _provenance(row)
    assert prov["total_market_value"]["raw_field"] == "total_mv"
    assert prov["total_market_value"]["raw_value"] == 18148383.3881
    assert prov["total_market_value"]["raw_unit"] == "CNY_10k"
    assert prov["total_market_value"]["standard_unit"] == "CNY"
    assert prov["total_market_value"]["conversion"] == "multiply:10000"
    assert prov["pb"]["definition_status"] == "unconfirmed"

    akshare = _comparison(resp, "akshare")
    assert {"total_market_value", "float_market_value", "ps_ttm", "pe_ttm"} <= set(akshare.matched_fields)
    assert "ps" not in akshare.checked_fields
    assert {"field": "pb", "reason": "definition_unconfirmed"} in akshare.not_comparable_fields
    assert {"field": "pe", "reason": "definition_unconfirmed"} in akshare.not_comparable_fields
    assert not akshare.conflicts

    assert sorted(prov["total_market_value"]["validated_by"]) == ["akshare"]
    assert sorted(prov["ps_ttm"]["validated_by"]) == ["akshare", "baostock"]
    assert "validated_by" not in prov["pb"]
    assert "validated_by" not in prov["ps"]

    db = Database(config.storage.sqlite_path)
    with db.session() as session:
        stored = QueryService(session).get_valuation_metric(TICKER, "2026-10-30")
    stored_prov = _provenance(stored.iloc[0].to_dict())
    assert stored.iloc[0]["total_market_value"] == pytest.approx(181483833881.0)
    assert stored_prov["total_market_value"]["conversion"] == "multiply:10000"
    assert stored_prov["ps_ttm"]["metric_definition"] == "ps_ttm"


def test_margin_units_unified_and_eps_variants_never_match(tmp_path):
    runner, config = _runner(tmp_path, {
        "tushare": FakeAdapter("tushare", "fina_indicator", {"financial_indicator": [TUSHARE_FINA_INDICATOR]}),
        "baostock": FakeAdapter("baostock", "quarterly_financial_indicators", {"financial_indicator": [BAOSTOCK_FINANCIAL]}),
    })
    resp = runner.run(_request("financial_indicator", ["tushare", "baostock"]))

    row = resp.data.financial_indicators[0]
    assert row["gross_margin"] == 66.2247
    assert row["net_margin"] == 54.7926
    prov = _provenance(row)
    assert prov["gross_margin"]["raw_field"] == "grossprofit_margin"
    assert prov["eps"]["metric_definition"] == "basic_eps"
    assert prov["roe"]["definition_status"] == "unconfirmed"

    baostock = _comparison(resp, "baostock")
    assert {"gross_margin", "net_margin", "current_ratio", "debt_asset_ratio"} <= set(baostock.matched_fields)
    assert "eps" not in baostock.checked_fields
    assert {"field": "roe", "reason": "definition_unconfirmed"} in baostock.not_comparable_fields
    assert not baostock.conflicts
    assert "validated_by" not in prov["eps"]
    assert "validated_by" not in prov["roe"]

    baostock_records = runner._normalize_financial_indicator(
        resp.provider_results[1], BAOSTOCK_FINANCIAL, 0, _request("financial_indicator", ["baostock"]), "run_x",
    )
    supplement = baostock_records[0]
    assert supplement.net_margin == pytest.approx(54.7926)
    assert supplement.eps is None
    assert supplement.field_provenance["net_margin"]["conversion"] == "multiply:100"

    db = Database(config.storage.sqlite_path)
    with db.session() as session:
        stored = QueryService(session).get_financial_indicator(TICKER, "20260930")
    stored_prov = _provenance(stored.iloc[0].to_dict())
    assert stored.iloc[0]["gross_margin"] == 66.2247
    assert stored_prov["gross_margin"]["metric_definition"] == "sales_gross_margin"


def test_eps_of_other_definition_does_not_supplement_basic_eps(tmp_path):
    runner, _ = _runner(tmp_path, {})
    request = _request("financial_indicator", ["tushare", "baostock"])
    tushare_result = FakeAdapter("tushare", "fina_indicator", {"financial_indicator": [TUSHARE_FINA_INDICATOR]})._result(
        "financial_indicator"
    ).model_copy(update={"raw_payload_id": "raw_test", "raw_payload_ref": "raw://local/raw_test.jsonl.gz"})
    canonical = runner._normalize_financial_indicator(tushare_result, {**TUSHARE_FINA_INDICATOR, "eps": None}, 0, request, "run_x")[0]
    other = canonical.model_copy(update={
        "provider": "akshare",
        "eps": 1.6,
        "field_provenance": {
            **canonical.field_provenance,
            "eps": {"provider": "akshare", "metric_definition": "diluted_eps", "period_basis": "report_period_ytd", "standard_unit": "CNY_per_share"},
        },
    })
    result = apply_canonical_merge_policy(canonical, [other], comparison_fields=["eps"], supplement_field_whitelist=["eps"])
    merged = result.records[0]
    assert merged.eps is None
    assert merged.supplement_flags["not_supplemented"] == [{"field": "eps", "provider": "akshare", "reason": "definition_mismatch"}]


def test_no_comparable_fields_is_not_cross_validation(tmp_path):
    spot = {"provider_symbol": TICKER, "raw_source_api": "stock_zh_a_spot_em", "trade_date": "20261030", "市净率": 10.2, "市盈率-动态": 95.3}
    tushare = {"ts_code": TICKER, "trade_date": "20261030", "pe": 120.5, "pb": 10.2}
    runner, _ = _runner(tmp_path, {
        "tushare": FakeAdapter("tushare", "daily_basic", {"valuation_metric": [tushare]}),
        "akshare": FakeAdapter("akshare", "stock_value_em+stock_zh_a_spot_em", {"valuation_metric": [spot]}),
    })
    resp = runner.run(_request("valuation_metric", ["tushare", "akshare"]))

    row = resp.data.valuation_metrics[0]
    assert row["merge_method"] == "canonical_only"
    assert row["validation_status"] == "validated_with_warning"
    assert all("validated_by" not in entry for entry in _provenance(row).values() if isinstance(entry, dict))
    cmp = _comparison(resp, "akshare")
    assert cmp.status == "not_comparable"
    assert cmp.checked_fields == [] and cmp.matched_fields == []
    assert "canonical_not_validated_no_comparable_fields" in resp.quality_report.warnings


def test_fallback_without_comparable_fields_is_single_source(tmp_path):
    spot = {"provider_symbol": TICKER, "raw_source_api": "stock_zh_a_spot_em", "trade_date": "20261030", "市净率": 10.2}
    baostock = {"provider_symbol": TICKER, "raw_source_api": "query_history_k_data_plus", "date": "2026-10-30", "pbMRQ": 10.2}
    runner, _ = _runner(tmp_path, {
        "tushare": FakeAdapter("tushare", "daily_basic", {}),
        "akshare": FakeAdapter("akshare", "stock_value_em+stock_zh_a_spot_em", {"valuation_metric": [spot]}),
        "baostock": FakeAdapter("baostock", "query_history_k_data_plus.valuation", {"valuation_metric": [baostock]}),
    })
    resp = runner.run(_request("valuation_metric", ["tushare", "akshare", "baostock"]))

    row = resp.data.valuation_metrics[0]
    assert row["merge_method"] == "fallback_single_source"
    assert row["validation_status"] == "validated_with_warning"
    assert "validated_by_supplements" not in (row.get("supplement_flags") or {})
    assert all("validated_by" not in entry for entry in _provenance(row).values() if isinstance(entry, dict))
