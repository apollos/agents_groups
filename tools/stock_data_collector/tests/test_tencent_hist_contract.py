from __future__ import annotations

from types import SimpleNamespace

import pytest

from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.validation.comparison import compare_bar_records


def normalize_tencent(row, ticker="600519.SH"):
    request = StockDataRequest(
        request_id="req_tx_contract", request_type="historical_bars", tickers=[ticker],
        start_date="2026-10-19", end_date="2026-10-19", frequency="1d", adjust="none",
    )
    mapped = AKShareAdapter()._normalize_tencent_hist_row(
        row, normalized=ticker, tx_symbol=ticker[-2:].lower() + ticker[:6], request=request,
    )
    result = ProviderFetchResult(
        provider="akshare", source_api="stock_zh_a_hist+stock_zh_a_hist_tx", source_site="akshare",
        adapter_version="0.5.1", status=AdapterFetchStatus.success, raw_records=[mapped], rows_fetched=1,
        raw_payload_id="raw_tx_contract", raw_payload_ref="raw://test/tencent-contract.jsonl.gz", raw_hash="sha256:test",
        started_at=now_asia_shanghai(), completed_at=now_asia_shanghai(),
    )
    # Exercise production record normalization without constructing a store or
    # calling any provider. Only the settings consumed by normalization are needed.
    runner = object.__new__(IngestionRunner)
    runner.config = SimpleNamespace(
        storage=SimpleNamespace(timezone="Asia/Shanghai"),
        data_quality=SimpleNamespace(provider_reliability={}),
    )
    bar = runner._normalize_bar(result, mapped, 0, request, "run_tx_contract")[0]
    return mapped, bar


def modern_row(**updates):
    # Shape/units follow installed AKShare 1.19.1. Volume reproduces the
    # acceptance failure; the monetary value is an explicit comparison fixture.
    return {
        "date": "2026-10-19", "open": 326.0, "close": 326.75,
        "high": 335.0, "low": 325.0, "volume": 4_339_056,
        "turnover": 0.0775, "amount": 1_431_196_860.0, **updates,
    }


@pytest.mark.parametrize("ticker", ["600519.SH", "000001.SZ", "688795.SH"])
def test_modern_tencent_preserves_sdk_shares_cny_and_turnover_contract(ticker):
    source = modern_row()
    mapped, bar = normalize_tencent(source, ticker)

    assert mapped["raw_source_record"] == source
    assert source == modern_row()  # Adapter leaves the SDK object untouched.
    assert mapped["volume_unit"] == "share"
    assert bar.volume == 4_339_056
    assert bar.amount == 1_431_196_860.0
    assert bar.turnover_rate == pytest.approx(7.75)
    assert bar.vwap == pytest.approx(bar.amount / bar.volume)
    assert mapped["amount_is_estimated"] is False
    assert bar.field_provenance["amount"]["is_estimated"] is False
    assert bar.field_provenance["vwap"]["is_estimated"] is False
    canonical = bar.model_copy(update={"provider": "tushare", "source_api": "daily"})
    comparison = compare_bar_records(canonical, bar)
    assert comparison.conflicts == []
    assert {"volume", "amount", "vwap", "turnover_rate"}.issubset(comparison.checked_fields)


def test_modern_tencent_actual_amount_mismatch_is_not_silently_skipped():
    _, bar = normalize_tencent(modern_row())
    canonical = bar.model_copy(update={
        "provider": "tushare", "source_api": "daily",
        "amount": bar.amount * 2, "vwap": bar.vwap * 2,
    })
    comparison = compare_bar_records(canonical, bar)
    assert {"amount", "vwap"}.issubset(comparison.conflicted_fields)
    assert "volume" in comparison.matched_fields


def test_legacy_tencent_hands_and_estimated_amount_remain_explicit():
    source = {
        "date": "2026-10-19", "open": 10.0, "close": 11.0,
        "high": 12.0, "low": 9.0, "amount": 123.0,
    }
    mapped, bar = normalize_tencent(source)
    assert mapped["raw_source_record"]["amount"] == 123.0
    assert mapped["volume_unit"] == "hand"
    assert bar.volume == 12_300
    assert bar.amount == 10.5 * 12_300
    assert bar.turnover_rate is None
    assert mapped["amount_is_estimated"] is True
    for field in ("amount", "vwap"):
        assert bar.field_provenance[field]["is_estimated"] is True
        assert bar.field_provenance[field]["estimation_method"] == "typical_price_x_tencent_volume_hands_x_100"
    canonical = bar.model_copy(update={
        "provider": "tushare", "source_api": "daily", "amount": bar.amount * 2,
        "vwap": bar.vwap * 2, "field_provenance": {},
    })
    comparison = compare_bar_records(canonical, bar)
    assert comparison.conflicts == []
    assert "volume" in comparison.checked_fields
    assert not {"amount", "vwap"}.intersection(comparison.checked_fields)


def test_modern_missing_amount_estimate_uses_shares_and_never_turnover():
    mapped, bar = normalize_tencent(modern_row(amount=None))
    assert mapped["raw_source_record"]["amount"] is None
    assert bar.volume == 4_339_056
    assert bar.amount == pytest.approx((326.0 + 326.75 + 335.0 + 325.0) / 4 * 4_339_056)
    assert mapped["amount_is_estimated"] is True
    assert bar.field_provenance["amount"]["estimation_method"] == "typical_price_x_tencent_volume_shares"


@pytest.mark.parametrize("updates", [{"volume": None}, {"amount": 0, "volume": 0, "turnover": 0}])
def test_modern_unit_contract_does_not_depend_on_value_presence(updates):
    source = modern_row(**updates)
    if source["volume"] is None:
        request = StockDataRequest(request_id="req_missing", request_type="historical_bars", tickers=["600519.SH"])
        mapped = AKShareAdapter()._normalize_tencent_hist_row(source, normalized="600519.SH", tx_symbol="sh600519", request=request)
        assert mapped["volume"] is None
        assert mapped["volume_unit"] == "share"
        assert mapped["amount"] == source["amount"]
    else:
        mapped, bar = normalize_tencent(source)
        assert bar.volume == bar.amount == bar.turnover_rate == 0
        assert mapped["amount_is_estimated"] is False


@pytest.mark.parametrize("row", [
    {"date": "2026-10-19", "open": 10, "close": 10, "high": 11, "low": 9, "volume": 123},
    {"date": "2026-10-19", "open": 10, "close": 10, "high": 11, "low": 9, "amount": 123, "turnover": 0.1},
    {"date": "2026-10-19", "open": 10, "close": 10, "high": 11, "low": 9, "amount": 123, "vol": 456},
])
def test_unknown_tencent_schema_rejects_ambiguous_units(row):
    with pytest.raises(KeyError, match="volume/amount schema changed"):
        normalize_tencent(row)
