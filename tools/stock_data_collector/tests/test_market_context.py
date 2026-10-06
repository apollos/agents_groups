"""Market-context pipeline: adapter bindings, runner normalization, service semantics, CLI."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter
from stock_data_ingestion.config import MarketContextSourcesConfig, load_config
from stock_data_ingestion.schemas.market_context import MarketContextRequest
from stock_data_ingestion.schemas.requests import RequestType, StockDataRequest
from stock_data_ingestion.services.collector import StockDataCollector
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.services.market_context_service import MarketContextService
from stock_data_ingestion.storage.database import Database
from stock_data_ingestion.storage.raw_object_store import RawObjectStore

AS_OF = date(2026, 10, 6)
LAST_TRADING = date(2026, 9, 30)


# ---------------------------------------------------------------------------
# Fake AKShare namespace with the real column layouts observed on 2026-10-06
# ---------------------------------------------------------------------------
def _dates(n: int, end: date) -> list[date]:
    out: list[date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


class FakeAK:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        self.hk_days = _dates(30, date(2026, 10, 5))
        self.cn_days = _dates(30, LAST_TRADING)

    def _rec(self, name, kwargs):
        self.calls.append((name, kwargs))
        if name in self.fail:
            raise ConnectionError(f"{name} blocked")

    def stock_hk_index_daily_sina(self, **kw):
        self._rec("stock_hk_index_daily_sina", kw)
        rows = [{"date": d.isoformat(), "open": 4000 + i, "high": 4010 + i, "low": 3990 + i, "close": 4000 + i * 2, "volume": 1000, "amount": 5000} for i, d in enumerate(self.hk_days)]
        return pd.DataFrame(rows)

    def stock_hk_index_daily_em(self, **kw):
        self._rec("stock_hk_index_daily_em", kw)
        return pd.DataFrame([{"日期": d.isoformat(), "开盘": 1, "最高": 2, "最低": 0.5, "最新": 1.5} for d in self.hk_days])

    def currency_boc_sina(self, **kw):
        self._rec("currency_boc_sina", kw)
        days = [AS_OF - timedelta(days=i) for i in range(25)]
        rows = [
            {"日期": d.isoformat(), "中行汇买价": 85.25 + i * 0.01, "中行钞买价": 85.25 + i * 0.01, "中行钞卖价/汇卖价": 85.59 + i * 0.01, "央行中间价": float("nan"), "中行折算价": 85.84}
            for i, d in enumerate(days)
        ]
        return pd.DataFrame(rows)

    def futures_zh_daily_sina(self, **kw):
        self._rec("futures_zh_daily_sina", kw)
        rows = [{"date": d.isoformat(), "open": 109000, "high": 110000, "low": 108000, "close": 109000 + i * 10, "volume": 100, "hold": 200, "settle": 108900 + i * 10} for i, d in enumerate(self.cn_days)]
        return pd.DataFrame(rows)

    def futures_zh_spot(self, **kw):
        self._rec("futures_zh_spot", kw)
        last_close = 109000 + 29 * 10
        prev_settle = 108900 + 28 * 10
        return pd.DataFrame([{"symbol": "铜连续", "time": "150000", "open": 109000, "high": 110000, "low": 108000, "current_price": last_close, "hold": 200.0, "volume": 100, "last_close": last_close, "last_settle_price": prev_settle}])

    def bond_china_yield(self, **kw):
        self._rec("bond_china_yield", kw)
        rows = []
        for i, d in enumerate(self.cn_days):
            rows.append({"曲线名称": "中债国债收益率曲线", "日期": d.isoformat(), "3月": 1.1, "10年": 1.60 + i * 0.001, "30年": 2.1})
            rows.append({"曲线名称": "中债中短期票据收益率曲线(AAA)", "日期": d.isoformat(), "3月": 1.4, "10年": 2.0, "30年": float("nan")})
        return pd.DataFrame(rows)

    def bond_zh_us_rate(self, **kw):
        self._rec("bond_zh_us_rate", kw)
        rows = [{"日期": d.isoformat(), "中国国债收益率10年": 1.7, "美国国债收益率10年": 4.8 + i * 0.01} for i, d in enumerate(self.cn_days)]
        rows.append({"日期": "2026-10-05", "中国国债收益率10年": float("nan"), "美国国债收益率10年": 5.31})
        return pd.DataFrame(rows)

    def stock_zh_index_daily_em(self, **kw):
        self._rec("stock_zh_index_daily_em", kw)
        raise ConnectionError("Remote end closed connection without response")

    def stock_zh_index_daily(self, **kw):
        self._rec("stock_zh_index_daily", kw)
        rows = [{"date": d.isoformat(), "open": 4300, "high": 4400, "low": 4200, "close": 4300 + i, "volume": 1} for i, d in enumerate(self.cn_days)]
        # Sina returns the full history: rows far outside any request window too.
        rows.insert(0, {"date": "2002-01-04", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0})
        return pd.DataFrame(rows)


@pytest.fixture
def fake_ak(monkeypatch):
    fake = FakeAK()
    monkeypatch.setattr(AKShareAdapter, "_import_ak", lambda self, source_api, started: fake)
    monkeypatch.setattr(AKShareAdapter, "is_available", lambda self: True)
    return fake


@pytest.fixture
def runner(tmp_path, fake_ak):
    load_config.cache_clear()
    config = load_config().model_copy(deep=True)
    config.storage.raw_object_root = tmp_path / "raw"
    config.storage.parquet_root = tmp_path / "parquet"
    config.storage.sqlite_path = tmp_path / "db.sqlite"
    db = Database(config.storage.sqlite_path)
    db.init()
    return IngestionRunner(config, RawObjectStore(config.storage.raw_object_root), database=db)


def _req(**kw) -> MarketContextRequest:
    return MarketContextRequest(**{"context_id": "ctx", **kw})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def test_market_context_config_loads_and_resolves_sources():
    config = load_config()
    mc = config.market_context
    assert config.data_sources.providers_for_request("market_context") == ["akshare"]
    assert "akshare" in config.data_sources.providers_for_request("index_data")
    entry = mc.symbol_entry("akshare", "interest_rate", "CN_CGB_10Y")
    funcs = [s["func"] for s in mc.sources_for("akshare", "interest_rate", entry, "1d")]
    assert funcs == ["bond_china_yield", "bond_zh_us_rate"]
    assert mc.sources_for("akshare", "interest_rate", entry, "1d")[0]["curve"] == "中债国债收益率曲线"
    # FX pairs derive from the currency-name table even when not listed explicitly.
    derived = mc.symbol_entry("akshare", "fx", "EURCNY")
    assert derived == {"name": "EUR兑人民币（中行牌价，CNY/100EUR）", "base_currency": "EUR", "quote_currency": "CNY", "provider_symbol": "欧元"}
    assert mc.symbol_entry("akshare", "fx", "XXXCNY") is None
    assert mc.symbol_entry("akshare", "hk_index", "hstech")["provider_symbol"] == "HSTECH"


def test_market_context_config_rejects_unknown_layout():
    with pytest.raises(ValueError, match="unsupported layout"):
        MarketContextSourcesConfig.model_validate({"providers": {"akshare": {"fx": {"sources": [{"func": "x", "layout": "weird"}]}}}})


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------
def _stock_request(context_type: str, symbol: str, frequency: str = "1d", start: date | None = None, end: date | None = None) -> StockDataRequest:
    return StockDataRequest(
        request_id="req_t",
        request_type=RequestType.market_context,
        start_date=start,
        end_date=end,
        provider_priority=["akshare"],
        canonical_provider="akshare",
        extra_params={"market_context": {"context_type": context_type, "symbol": symbol, "frequency": frequency}},
    )


def test_adapter_fx_emits_one_row_per_quote_type_with_direction(fake_ak):
    adapter = AKShareAdapter()
    result = adapter.fetch_market_context(_stock_request("fx", "HKDCNY", start=AS_OF - timedelta(days=2), end=AS_OF))
    assert result.status == "success"
    assert fake_ak.calls[0] == ("currency_boc_sina", {"symbol": "港币", "start_date": (AS_OF - timedelta(days=2)).strftime("%Y%m%d"), "end_date": AS_OF.strftime("%Y%m%d")})
    rows = result.raw_records
    # 3 days x 4 quote types (central parity is NaN -> no row, not zero)
    assert len(rows) == 12
    assert {r["rate_type"] for r in rows} == {"spot_buy", "cash_buy", "spot_sell", "boc_conversion"}
    sample = next(r for r in rows if r["rate_type"] == "spot_sell" and r["rate_date"] == AS_OF.strftime("%Y%m%d"))
    assert (sample["base_currency"], sample["quote_currency"], sample["quote_basis"], sample["rate"]) == ("HKD", "CNY", 100.0, 85.59)
    assert sample["raw_source_api"] == "currency_boc_sina" and sample["context_type"] == "fx"
    # Original vendor columns are preserved for raw provenance.
    assert "中行钞卖价/汇卖价" in sample


def test_adapter_hk_index_uses_window_and_falls_back_in_configured_order(fake_ak):
    adapter = AKShareAdapter()
    fake_ak.fail.add("stock_hk_index_daily_sina")
    result = adapter.fetch_market_context(_stock_request("hk_index", "HSTECH", start=date(2026, 9, 1), end=date(2026, 9, 15)))
    assert result.status == "success"
    assert result.source_api == "stock_hk_index_daily_em"
    assert [c[0] for c in fake_ak.calls] == ["stock_hk_index_daily_sina", "stock_hk_index_daily_em"]
    assert all("20260901" <= r["trade_date"] <= "20260915" for r in result.raw_records)
    assert result.raw_records[0]["fallback_reason"].startswith("stock_hk_index_daily_sina")
    assert result.raw_records[0]["index_code"] == "HSTECH" and result.raw_records[0]["currency"] == "HKD" and result.raw_records[0]["market"] == "HK"


def test_adapter_all_sources_failing_is_retryable_error(fake_ak):
    adapter = AKShareAdapter()
    fake_ak.fail.update({"stock_hk_index_daily_sina", "stock_hk_index_daily_em"})
    result = adapter.fetch_market_context(_stock_request("hk_index", "HSI"))
    assert result.status == "failed" and result.error is not None and result.error.retryable
    assert "stock_hk_index_daily_sina" in result.error.error_message and "stock_hk_index_daily_em" in result.error.error_message


def test_adapter_unknown_symbol_is_non_retryable_config_error(fake_ak):
    result = AKShareAdapter().fetch_market_context(_stock_request("commodity", "NOPE"))
    assert result.status == "failed" and result.error.error_code == "INVALID_REQUEST" and not result.error.retryable
    assert "market_context_sources.yaml" in result.error.error_message


def test_adapter_curve_tenor_and_series_layouts(fake_ak):
    adapter = AKShareAdapter()
    rows = adapter.fetch_market_context(_stock_request("interest_rate", "CN_CGB_10Y", start=date(2026, 9, 1), end=AS_OF)).raw_records
    assert all(r["curve_name"] == "中债国债收益率曲线" and r["tenor"] == "10Y" and r["unit"] == "percent" for r in rows)
    assert rows[-1]["rate_value"] == pytest.approx(1.60 + 29 * 0.001)
    # US series from the wide table; the NaN CN value on 2026-10-05 produces no CN row.
    us = adapter.fetch_market_context(_stock_request("interest_rate", "US_CGB_10Y", start=date(2026, 9, 1), end=AS_OF)).raw_records
    assert us[-1]["rate_date"] == "20261005" and us[-1]["market"] == "US" and us[-1]["rate_value"] == 5.31
    fake_ak.fail.add("bond_china_yield")
    cn_fallback = adapter.fetch_market_context(_stock_request("interest_rate", "CN_CGB_10Y", start=date(2026, 9, 1), end=AS_OF))
    assert cn_fallback.source_api == "bond_zh_us_rate"
    assert all(r["rate_date"] != "20261005" for r in cn_fallback.raw_records)


def test_adapter_snapshot_flags_inferred_date(fake_ak):
    result = AKShareAdapter().fetch_market_context(_stock_request("commodity", "CU0", frequency="realtime"))
    row = result.raw_records[0]
    assert fake_ak.calls[0] == ("futures_zh_spot", {"symbol": "CU0", "market": "CF", "adjust": "0"})
    assert row["frequency"] == "realtime" and row["latest"] == 109290 and row["pre_settle"] == 109180
    assert row["observed_date_inferred_from_fetch"] is True
    assert row["observed_at"].endswith("15:00:00+08:00")


# ---------------------------------------------------------------------------
# Service semantics
# ---------------------------------------------------------------------------
def test_service_fx_latest_is_fresh_and_persisted(runner):
    service = MarketContextService(runner)
    resp = service.fetch(_req(context_type="fx", symbol="HKDCNY", as_of=AS_OF))
    r = resp.result
    assert resp.status == "success"
    assert r.data_date == AS_OF and r.metric == "spot_sell" and r.value == 85.59
    assert r.unit == "CNY per 100 HKD" and r.identity["quote_basis"] == 100.0
    assert "central_parity" not in r.values  # NaN -> absent, never zero
    assert r.quality.status == "fresh" and r.quality.usable and r.quality.staleness_days == 0
    assert r.changes["1p"].kind == "percent" and r.changes["1p"].period_unit == "observation"
    assert r.changes["1p"].value == pytest.approx((85.59 / 85.60 - 1) * 100, rel=1e-6)
    assert r.source.source_api == "currency_boc_sina" and r.source.source_url.startswith("https://biz.finance.sina.com.cn")
    assert "fx_rates" in r.provenance.tables_written and r.provenance.record_ids and r.provenance.raw_payload_ids
    assert r.provenance.idempotency_key.startswith("market_context:fx:HKDCNY:BOC")


def test_service_stale_value_keeps_real_date_and_is_flagged(runner):
    resp = MarketContextService(runner).fetch(_req(context_type="commodity", symbol="CU0", as_of=AS_OF))
    r = resp.result
    assert resp.status == "partial_success"
    assert r.data_date == LAST_TRADING and r.quality.status == "stale" and r.quality.usable and not r.quality.is_fresh
    assert r.quality.staleness_days == 6 and any(w.startswith("stale:") for w in r.quality.warnings)
    assert r.values["close"] == 109290 and r.values["settle"] == 109190 and r.values["latest"] is None
    assert r.identity == {"commodity": "copper", "instrument_type": "futures", "market": "SHFE", "contract": "CU0", "frequency": "1d", "price_unit": "CNY/ton", "currency": "CNY"}
    assert r.changes["1p"].period_unit == "trading_day"


def test_service_realtime_snapshot_is_reassigned_to_previous_session(runner):
    resp = MarketContextService(runner).fetch(_req(context_type="commodity", symbol="CU0", frequency="realtime", as_of=AS_OF))
    r = resp.result
    assert r.metric == "latest" and r.value == 109290
    assert r.data_date == LAST_TRADING, "holiday snapshot must not be reported as today's price"
    assert r.observed_at.date() == LAST_TRADING
    assert any(w.startswith("snapshot_belongs_to_previous_session") for w in resp.warnings)
    assert r.quality.status == "stale" and r.quality.max_staleness_days == 0
    assert r.changes["snapshot_vs_pre_settle"].value == pytest.approx((109290 / 109180 - 1) * 100)
    assert r.changes["1p"].value is not None and r.changes["1p"].from_date == date(2026, 9, 29)


def test_service_history_mode_returns_series_not_today(runner):
    resp = MarketContextService(runner).fetch(_req(context_type="hk_index", symbol="HSTECH", start_date=date(2026, 9, 1), end_date=date(2026, 9, 15)))
    r = resp.result
    assert r.request_window["mode"] == "history"
    assert all(date(2026, 9, 1) <= o.data_date <= date(2026, 9, 15) for o in r.series)
    assert r.data_date == max(o.data_date for o in r.series) and r.data_date <= date(2026, 9, 15)
    assert r.quality.status == "fresh"  # relative to end_date
    assert r.changes["20p"].value is None and r.changes["20p"].reason.startswith("insufficient_history")
    assert r.identity["currency"] == "HKD" and r.identity["market"] == "HK"


def test_service_interest_rate_changes_in_percentage_points_and_bp(runner):
    resp = MarketContextService(runner).fetch(_req(context_type="interest_rate", symbol="CN_CGB_10Y", as_of=AS_OF))
    r = resp.result
    assert r.unit == "percent" and r.identity["tenor"] == "10Y" and r.identity["rate_type"] == "government_bond_yield"
    assert r.changes["1p"].kind == "percentage_point"
    assert r.changes["1p"].value == pytest.approx(0.001) and r.changes["1p"].basis_points == pytest.approx(0.1)
    assert r.source.source_api == "bond_china_yield"


def test_service_equity_index_reuses_index_data_with_sina_fallback(runner, fake_ak):
    resp = MarketContextService(runner).fetch(_req(context_id="index_csi_300", context_type="equity_index", symbol="000300", as_of=AS_OF))
    r = resp.result
    assert resp.stock_data_response["records_returned"] == {"index_bars": 30}
    assert r.identity["index_code"] == "000300" and r.identity["market"] == "A_share"
    assert r.data_date == LAST_TRADING and r.value == 4300 + 29
    assert r.source.source_api == "stock_zh_index_daily"
    assert any(w.startswith("source_fallback: stock_zh_index_daily_em failed") for w in resp.warnings)
    assert [c[0] for c in fake_ak.calls] == ["stock_zh_index_daily_em", "stock_zh_index_daily"]
    assert "index_bars" in r.provenance.tables_written


def test_service_repeat_request_is_idempotent_and_served_from_store(runner, fake_ak):
    service = MarketContextService(runner)
    first = service.fetch(_req(context_type="fx", symbol="HKDCNY", as_of=AS_OF))
    calls_after_first = len(fake_ak.calls)
    second = service.fetch(_req(context_type="fx", symbol="HKDCNY", as_of=AS_OF))
    assert len(fake_ak.calls) == calls_after_first, "identical request must not hit the vendor again"
    assert any(w.startswith("served_from_store") for w in second.warnings)
    assert second.result.value == first.result.value and second.result.data_date == first.result.data_date
    assert set(second.result.provenance.record_ids) == set(first.result.provenance.record_ids)
    assert second.result.source.source_api == "currency_boc_sina" and second.result.source.source_site == "akshare"


def test_service_as_of_before_first_observation_is_missing(runner):
    resp = MarketContextService(runner).fetch(_req(context_type="hk_index", symbol="HSTECH", as_of=date(2026, 1, 5)))
    assert resp.status == "failed"
    assert resp.result.quality.status in {"missing", "failed"} and not resp.result.quality.usable
    assert resp.result.value is None and resp.result.data_date is None


@pytest.mark.parametrize("validation_status", ["quarantined", "manual_review_required", "conflicted_high", "failed"])
def test_service_blocks_upstream_validation_on_fetch_and_store_replay(runner, monkeypatch, validation_status):
    original = runner._rescore_record

    def mark_blocked(record, conflicts):
        return original(record, conflicts).model_copy(update={"validation_status": validation_status})

    monkeypatch.setattr(runner, "_rescore_record", mark_blocked)
    service = MarketContextService(runner)
    first = service.fetch(_req(context_type="hk_index", symbol="HSTECH", as_of=AS_OF))
    second = service.fetch(_req(context_type="hk_index", symbol="HSTECH", as_of=AS_OF))
    assert any(w.startswith("served_from_store") for w in second.warnings)
    for resp in (first, second):
        assert resp.result.value is not None, "retain the rejected value and provenance for inspection"
        assert resp.result.provenance.record_ids
        assert resp.result.quality.is_fresh, "freshness and validity are independent"
        assert resp.status == "failed" and resp.result.quality.status == "failed"
        assert not resp.result.quality.usable
        assert resp.result.series[-1].validation_status == validation_status
        assert any(validation_status in w for w in resp.result.quality.warnings)
        assert all(c.value is None for c in resp.result.changes.values())


def test_service_does_not_compute_change_from_quarantined_reference(runner, monkeypatch):
    original = runner._rescore_record

    def mark_reference(record, conflicts):
        record = original(record, conflicts)
        if record.trade_date == date(2026, 10, 2):
            return record.model_copy(update={"validation_status": "quarantined"})
        return record

    monkeypatch.setattr(runner, "_rescore_record", mark_reference)
    resp = MarketContextService(runner).fetch(_req(context_type="hk_index", symbol="HSTECH", as_of=AS_OF))
    assert resp.status == "success" and resp.result.quality.usable
    assert resp.result.changes["1p"].value is None
    assert "quarantined" in resp.result.changes["1p"].reason
    assert resp.result.changes["5p"].value is not None


def test_service_fx_aggregation_preserves_blocking_status(runner, monkeypatch):
    original = runner._rescore_record

    def mark_quote(record, conflicts):
        record = original(record, conflicts)
        status = "quarantined" if record.rate_type == "spot_sell" else "conflicted_low"
        return record.model_copy(update={"validation_status": status})

    monkeypatch.setattr(runner, "_rescore_record", mark_quote)
    resp = MarketContextService(runner).fetch(_req(context_type="fx", symbol="HKDCNY", as_of=AS_OF))
    assert resp.result.value == 85.59
    assert resp.result.series[-1].validation_status == "quarantined"
    assert resp.status == "failed" and not resp.result.quality.usable


def test_service_unknown_symbol_fails_without_vendor_call(runner, fake_ak):
    resp = MarketContextService(runner).fetch(_req(context_type="commodity", symbol="UNKNOWN"))
    assert resp.status == "failed" and fake_ak.calls == []
    assert resp.errors[0].error_code == "INVALID_REQUEST" and not resp.errors[0].retryable


def test_request_validation_rules():
    with pytest.raises(ValueError, match="together"):
        _req(context_type="fx", symbol="HKDCNY", start_date=AS_OF)
    with pytest.raises(ValueError, match="realtime"):
        _req(context_type="commodity", symbol="CU0", frequency="realtime", start_date=AS_OF, end_date=AS_OF)
    req = _req(context_type="fx", symbol="HKDCNY", as_of="2026-10-06T09:00:00+08:00")
    assert req.as_of == AS_OF and req.mode == "latest" and req.effective_max_staleness_days() == 3


# ---------------------------------------------------------------------------
# Collector + CLI
# ---------------------------------------------------------------------------
def test_collector_and_cli_market_context(runner, monkeypatch, capsys):
    collector = StockDataCollector(runner)
    resp = collector.fetch_market_context("ctx", "hk_index", "HSTECH", as_of=AS_OF)
    assert resp.result.data_date == date(2026, 10, 5) and resp.result.quality.is_fresh

    from stock_data_ingestion import cli

    monkeypatch.setattr(cli, "_build_collector", lambda config_dir=None: collector)
    cli.main(["fetch", "market-context", "--context-type", "fx", "--symbol", "HKDCNY", "--as-of", "2026-10-06", "--compact"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success" and payload["result"]["value"] == 85.59
    assert payload["result"]["unit"] == "CNY per 100 HKD" and payload["request"]["context_id"] == "fx:HKDCNY"
    assert set(payload["stock_data_response"]) == {"request_id", "status", "records_returned"}

    def _db(config):
        return runner.database

    monkeypatch.setattr(cli, "_build_database", _db)
    monkeypatch.setattr(cli, "load_config", lambda config_dir=None: runner.config)
    cli.main(["query", "market-context", "--context-type", "fx", "--base-currency", "HKD", "--quote-currency", "CNY", "--rate-type", "spot_sell", "--start-date", "2026-10-06", "--end-date", "2026-10-06"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["rate"] == 85.59 and rows[0]["quote_basis"] == 100.0
