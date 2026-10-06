"""Market-context pipeline: adapter bindings, runner normalization, service semantics, CLI."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
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
from stock_data_ingestion.services.query_service import QueryService
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
        # Test knobs: shift BOC quotes (same-day update scenario) and snapshot shape.
        self.fx_shift = 0.0
        self.spot_mode = "holiday"  # holiday | live | full_timestamp

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
            {"日期": d.isoformat(), "中行汇买价": 85.25 + i * 0.01, "中行钞买价": 85.25 + i * 0.01, "中行钞卖价/汇卖价": 85.59 + i * 0.01 + self.fx_shift, "央行中间价": float("nan"), "中行折算价": 85.84}
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
        last_settle = 108900 + 29 * 10
        prev_settle = 108900 + 28 * 10
        if self.spot_mode == "live":
            # Trading in progress: price moved away from the last close, previous settle is
            # the last bar's settle. Only the clock could date this -> must stay unknown.
            return pd.DataFrame([{"symbol": "铜连续", "time": "103000", "open": 109300, "high": 109800, "low": 109100, "current_price": 109650, "hold": 210.0, "volume": 50, "last_close": last_close, "last_settle_price": last_settle}])
        if self.spot_mode == "full_timestamp":
            return pd.DataFrame([{"symbol": "铜连续", "time": "2026-09-30 15:00:00", "open": 109000, "high": 110000, "low": 108000, "current_price": last_close, "hold": 200.0, "volume": 100, "last_close": last_close, "last_settle_price": prev_settle}])
        # Holiday: vendor keeps serving the last session's closing snapshot with HHMMSS only.
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


def _make_runner(tmp_path) -> IngestionRunner:
    load_config.cache_clear()
    config = load_config().model_copy(deep=True)
    config.storage.raw_object_root = tmp_path / "raw"
    config.storage.parquet_root = tmp_path / "parquet"
    config.storage.sqlite_path = tmp_path / "db.sqlite"
    db = Database(config.storage.sqlite_path)
    db.init()
    return IngestionRunner(config, RawObjectStore(config.storage.raw_object_root), database=db)


@pytest.fixture
def runner(tmp_path, fake_ak):
    return _make_runner(tmp_path)


def _restart(runner: IngestionRunner) -> IngestionRunner:
    """A fresh runner + Database on the same SQLite file (process restart)."""
    db = Database(runner.config.storage.sqlite_path)
    db.init()
    return IngestionRunner(runner.config, RawObjectStore(runner.config.storage.raw_object_root), database=db)


@pytest.fixture
def clock(monkeypatch):
    """Pin the idempotency-key clock so 'same minute / next hour' are deterministic."""
    from stock_data_ingestion.services import market_context_requests as mcr

    state = {"now": datetime(2026, 10, 6, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))}
    monkeypatch.setattr(mcr, "now_asia_shanghai", lambda: state["now"])

    def set_time(hour: int, minute: int = 0) -> None:
        state["now"] = state["now"].replace(hour=hour, minute=minute)

    return set_time


def _req(**kw) -> MarketContextRequest:
    return MarketContextRequest(**{"context_id": "ctx", **kw})


def _rows(runner: IngestionRunner, sql: str) -> list[dict]:
    from sqlalchemy import text

    with runner.database.session() as session:
        return [dict(r._mapping) for r in session.execute(text(sql))]


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


# ---------------------------------------------------------------------------
# Review item 1: commodity snapshot date is confirmed once, before the standard record is
# written; repeated reads never re-guess.
# ---------------------------------------------------------------------------
def _cu_realtime():
    return _req(context_type="commodity", symbol="CU0", frequency="realtime", as_of=AS_OF)


def _assert_confirmed_last_session(resp):
    r = resp.result
    assert r.data_date == LAST_TRADING and r.quality.is_fresh is False
    assert r.observed_at.date() == LAST_TRADING and r.observed_at.strftime("%H:%M:%S") == "15:00:00"
    assert r.quality.status == "stale" and r.quality.usable
    assert any(w.startswith("snapshot_date_confidence=confirmed_last_session") for w in r.quality.warnings)


def test_snapshot_date_first_request_confirms_previous_session_before_storage(runner, fake_ak, clock):
    resp = MarketContextService(runner).fetch(_cu_realtime())
    _assert_confirmed_last_session(resp)
    rows = _rows(runner, "select trade_date, observed_at, date_confidence, date_resolution_details from commodity_prices where frequency='realtime'")
    assert len(rows) == 1
    assert str(rows[0]["trade_date"]) == LAST_TRADING.isoformat(), "stored trade_date is the confirmed date, not the collection day"
    assert str(rows[0]["observed_at"]).startswith(LAST_TRADING.isoformat())
    assert rows[0]["date_confidence"] == "confirmed_last_session"
    details = json.loads(rows[0]["date_resolution_details"])
    assert details["observed_date_inferred_from_fetch"] is True and details["vendor_time_value"] == "150000"
    assert len(details["confirmation_bar_record_ids"]) == 2 and details["confirmation_bar_dates"][0] == LAST_TRADING.isoformat()
    assert "== close of" in details["confirmation_reason"]
    # The confirmation ran through the tool's own daily chain: daily bars are stored, raw untouched.
    assert _rows(runner, "select count(*) as n from commodity_prices where frequency='1d'")[0]["n"] == 30
    raw = json.loads(json.dumps(resp.stock_data_response))
    assert raw["status"] in {"success", "partial_success"}


def test_snapshot_date_repeat_same_minute_and_after_restart_reuse_confirmed_record(runner, fake_ak, clock):
    service = MarketContextService(runner)
    first = service.fetch(_cu_realtime())
    _assert_confirmed_last_session(first)
    vendor_calls = len(fake_ak.calls)
    confirm_calls = {"n": 0}
    original_confirm = IngestionRunner._confirm_snapshot_dates

    def counting(self, *a, **kw):
        confirm_calls["n"] += 1
        return original_confirm(self, *a, **kw)

    IngestionRunner._confirm_snapshot_dates = counting  # type: ignore[method-assign]
    try:
        second = service.fetch(_cu_realtime())
        third = MarketContextService(_restart(runner)).fetch(_cu_realtime())
    finally:
        IngestionRunner._confirm_snapshot_dates = original_confirm  # type: ignore[method-assign]
    assert len(fake_ak.calls) == vendor_calls, "same-minute repeats are idempotent hits, no vendor call"
    assert confirm_calls["n"] == 0, "repeat reads must not re-run date confirmation"
    for resp in (second, third):
        _assert_confirmed_last_session(resp)
        assert any(w.startswith("served_from_store") for w in resp.warnings)
    for a, b in ((first, second), (first, third)):
        assert [o.data_date for o in a.result.series] == [o.data_date for o in b.result.series]
        assert [o.observed_at for o in a.result.series] == [o.observed_at for o in b.result.series]
        assert {k: (c.from_date, c.to_date, c.value) for k, c in a.result.changes.items()} == {
            k: (c.from_date, c.to_date, c.value) for k, c in b.result.changes.items()
        }
        assert set(a.result.provenance.record_ids) == set(b.result.provenance.record_ids)
    assert first.result.changes["1p"].from_date == date(2026, 9, 29) and first.result.changes["1p"].to_date == LAST_TRADING
    assert _rows(runner, "select count(*) as n from commodity_prices where frequency='realtime'")[0]["n"] == 1


def test_snapshot_date_vendor_full_timestamp_is_kept_without_confirmation(runner, fake_ak, clock):
    fake_ak.spot_mode = "full_timestamp"
    resp = MarketContextService(runner).fetch(_cu_realtime())
    assert resp.result.data_date == LAST_TRADING and resp.result.quality.is_fresh is False
    assert any(w.startswith("snapshot_date_confidence=vendor_timestamp") for w in resp.result.quality.warnings)
    assert not any(w.startswith("snapshot_belongs_to_previous_session") for w in resp.warnings)
    rows = _rows(runner, "select date_confidence from commodity_prices where frequency='realtime'")
    assert [r["date_confidence"] for r in rows] == ["vendor_timestamp"]


def _assert_unknown_date(resp):
    r = resp.result
    assert resp.status == "failed"
    assert r.data_date is None and r.observed_at is None and r.value is None
    assert r.quality.usable is False and r.quality.is_fresh is None and r.quality.status == "unknown_date"
    err = next(e for e in resp.errors if e.error_code == "SNAPSHOT_DATE_UNCONFIRMED")
    assert err.retryable is True


@pytest.mark.parametrize("spot_mode", ["holiday", "live"])
def test_snapshot_date_unknown_when_daily_chain_fails_never_today_fresh(runner, fake_ak, clock, spot_mode):
    fake_ak.spot_mode = spot_mode
    fake_ak.fail.add("futures_zh_daily_sina")
    resp = MarketContextService(runner).fetch(_cu_realtime())
    _assert_unknown_date(resp)
    assert _rows(runner, "select count(*) as n from commodity_prices")[0]["n"] == 0, "no standard record with a guessed date"
    assert _rows(runner, "select count(*) as n from raw_payload_index")[0]["n"] >= 1, "raw payload is still saved"


def test_snapshot_date_live_session_time_only_is_unknown_even_with_daily_bars(runner, fake_ak, clock):
    """In-session snapshot: daily bars exist but do not match -> unknown, never 'today'."""
    fake_ak.spot_mode = "live"
    resp = MarketContextService(runner).fetch(_cu_realtime())
    _assert_unknown_date(resp)
    assert _rows(runner, "select count(*) as n from commodity_prices where frequency='realtime'")[0]["n"] == 0
    assert _rows(runner, "select count(*) as n from commodity_prices where frequency='1d'")[0]["n"] == 30


@pytest.mark.parametrize("blocked_date, label", [(LAST_TRADING, "last"), (date(2026, 9, 29), "previous")])
def test_snapshot_date_blocked_reference_bar_fails_confirmation(runner, fake_ak, clock, monkeypatch, blocked_date, label):
    """A quarantined reference bar (last or previous session) cannot date the snapshot; no
    earlier bar is substituted; the snapshot ends as unknown_date with the evidence recorded."""
    original = runner._rescore_record

    def mark(record, conflicts):
        record = original(record, conflicts)
        if getattr(record, "frequency", None) == "1d" and record.trade_date == blocked_date:
            return record.model_copy(update={"validation_status": "quarantined"})
        return record

    monkeypatch.setattr(runner, "_rescore_record", mark)
    resp = MarketContextService(runner).fetch(_cu_realtime())
    _assert_unknown_date(resp)
    err = next(e for e in resp.errors if e.error_code == "SNAPSHOT_DATE_UNCONFIRMED")
    blocked_row = _rows(runner, f"select record_id from commodity_prices where frequency='1d' and trade_date='{blocked_date}'")[0]
    assert blocked_row["record_id"] in err.error_message and "validation_status=quarantined" in err.error_message
    assert f"{label} daily bar" in err.error_message and "no earlier bar is substituted" in err.error_message
    assert _rows(runner, "select count(*) as n from commodity_prices where frequency='realtime'")[0]["n"] == 0
    assert _rows(runner, "select count(*) as n from commodity_prices where frequency='1d'")[0]["n"] == 30
    assert _rows(runner, "select count(*) as n from raw_payload_index")[0]["n"] >= 1


def test_blocking_statuses_are_shared_between_confirmation_and_summary():
    from stock_data_ingestion.schemas.quality import BLOCKING_VALIDATION_STATUSES, is_blocking_validation_status
    from stock_data_ingestion.services import market_context_service as svc

    assert set(BLOCKING_VALIDATION_STATUSES) == {"quarantined", "manual_review_required", "conflicted_high", "failed"}
    assert svc._BLOCKING_VALIDATION_STATUSES is BLOCKING_VALIDATION_STATUSES
    assert is_blocking_validation_status("quarantined") and not is_blocking_validation_status("conflicted_low")
    bars = [
        {"record_id": "a", "trade_date": date(2026, 9, 29), "close": 1.0, "settle": 109180.0, "validation_status": "validated"},
        {"record_id": "b", "trade_date": LAST_TRADING, "close": 109290.0, "settle": 2.0, "validation_status": "manual_review_required"},
    ]
    from stock_data_ingestion.schemas.records import CommodityPriceRecord

    snap = SimpleNamespace(latest=109290.0, pre_settle=109180.0)
    match, reason = IngestionRunner._match_snapshot_to_last_session(snap, bars)  # type: ignore[arg-type]
    assert match is None and "b (2026-09-30) has validation_status=manual_review_required" in reason
    bars[1]["validation_status"] = "validated"
    match, reason = IngestionRunner._match_snapshot_to_last_session(snap, bars)  # type: ignore[arg-type]
    assert reason is None and match[0] == LAST_TRADING
    assert CommodityPriceRecord  # imported for type parity with the runner


def test_snapshot_date_legacy_record_without_confidence_is_unknown_not_vendor_timestamp(runner, fake_ak, clock):
    from sqlalchemy import text

    service = MarketContextService(runner)
    first = service.fetch(_cu_realtime())
    _assert_confirmed_last_session(first)
    with runner.database.session() as session:
        session.execute(text("update commodity_prices set date_confidence=NULL, date_resolution_details='{}' where frequency='realtime'"))
        session.commit()
    replay = service.fetch(_cu_realtime())  # idempotent hit -> store read, no re-confirmation
    assert any(w.startswith("served_from_store") for w in replay.warnings)
    _assert_unknown_date(replay)


def test_snapshot_date_columns_are_added_to_legacy_sqlite(tmp_path):
    from sqlalchemy import inspect, text

    from stock_data_ingestion.storage.database import ensure_columns

    # Build a "pre-upgrade" database file: same tables, without the two new columns.
    legacy = Database(tmp_path / "legacy.sqlite")
    legacy.init()
    with legacy.engine.begin() as conn:
        conn.execute(text("drop index if exists ix_commodity_prices_date_confidence"))
        conn.execute(text("alter table commodity_prices drop column date_confidence"))
        conn.execute(text("alter table commodity_prices drop column date_resolution_details"))
    assert "date_confidence" not in {c["name"] for c in inspect(legacy.engine).get_columns("commodity_prices")}
    legacy.engine.dispose()

    # New process opens the old file: create_all() alone would not add the columns.
    upgraded = Database(tmp_path / "legacy.sqlite")
    added = ensure_columns(upgraded.engine)
    assert set(added["commodity_prices"]) == {"date_confidence", "date_resolution_details"}
    cols = {c["name"] for c in inspect(upgraded.engine).get_columns("commodity_prices")}
    assert {"date_confidence", "date_resolution_details"} <= cols
    assert ensure_columns(upgraded.engine) == {}
    upgraded.init()  # idempotent


# ---------------------------------------------------------------------------
# Review item 2: symbol decides the object; other business params are consistency-checked.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"context_type": "commodity", "symbol": "CU0"},
        {"context_type": "commodity", "symbol": "CU0", "contract": "CU0", "instrument_type": "futures"},
        {"context_type": "commodity", "symbol": "CU0", "market": "SHFE"},
        {"context_type": "interest_rate", "symbol": "US_CGB_10Y", "tenor": "10Y", "rate_type": "government_bond_yield", "market": "US"},
        {"context_type": "fx", "symbol": "HKDCNY", "market": "BOC"},
        {"context_type": "equity_index", "symbol": "000300", "market": "A_share"},
    ],
)
def test_business_params_consistent_with_symbol_are_accepted(runner, fake_ak, kwargs):
    resp = MarketContextService(runner).fetch(_req(as_of=AS_OF, **kwargs))
    assert resp.status in {"success", "partial_success"}
    assert fake_ak.calls, "vendor is called when the request is consistent"


@pytest.mark.parametrize(
    "kwargs, field, configured, requested",
    [
        ({"context_type": "commodity", "symbol": "CU0", "contract": "CU2612"}, "contract", "CU0", "CU2612"),
        ({"context_type": "commodity", "symbol": "CU0", "instrument_type": "spot"}, "instrument_type", "futures", "spot"),
        ({"context_type": "commodity", "symbol": "CU0", "market": "DCE"}, "market", "SHFE", "DCE"),
        ({"context_type": "interest_rate", "symbol": "US_CGB_10Y", "tenor": "2Y"}, "tenor", "10Y", "2Y"),
        ({"context_type": "interest_rate", "symbol": "CN_CGB_10Y", "rate_type": "policy_rate"}, "rate_type", "government_bond_yield", "policy_rate"),
        ({"context_type": "fx", "symbol": "HKDCNY", "market": "CFETS"}, "market", "BOC", "CFETS"),
        ({"context_type": "hk_index", "symbol": "HSTECH", "market": "A_share"}, "market", "HK", "A_share"),
    ],
)
def test_business_params_conflicting_with_symbol_fail_without_vendor_call(runner, fake_ak, kwargs, field, configured, requested):
    resp = MarketContextService(runner).fetch(_req(as_of=AS_OF, **kwargs))
    assert resp.status == "failed" and fake_ak.calls == []
    assert resp.result.value is None and resp.result.data_date is None and not resp.result.quality.usable
    err = resp.errors[0]
    assert err.error_code == "INVALID_REQUEST" and err.retryable is False
    assert field in err.error_message and configured in err.error_message and requested in err.error_message
    assert "与 symbol 绑定不一致" in err.error_message
    with runner.database.session() as session:
        from sqlalchemy import text

        for table in ("commodity_prices", "interest_rates", "fx_rates", "index_bars", "raw_payload_index"):
            assert session.execute(text(f"select count(*) from {table}")).scalar() == 0


def test_business_params_example_message_matches_review_wording(runner):
    resp = MarketContextService(runner).fetch(_req(context_type="commodity", symbol="CU0", contract="CU2612", as_of=AS_OF))
    assert resp.errors[0].error_message == (
        "symbol=CU0 的 contract 配置为 CU0，请求 contract=CU2612，与 symbol 绑定不一致。请使用已注册的 CU2612 symbol。"
    )


def test_identity_and_vendor_args_come_from_config_not_request(runner, fake_ak):
    resp = MarketContextService(runner).fetch(_req(context_type="commodity", symbol="CU0", market="SHFE", contract="CU0", as_of=AS_OF))
    assert resp.result.identity["market"] == "SHFE" and resp.result.identity["contract"] == "CU0"
    assert fake_ak.calls[0] == ("futures_zh_daily_sina", {"symbol": "CU0"})
    from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter

    adapter = AKShareAdapter(load_config())
    entry = runner.config.market_context.symbol_entry("akshare", "commodity", "CU0")
    identity = adapter._market_context_identity("commodity", entry, {"market": "DCE", "contract": "CU2612", "instrument_type": "spot"})
    assert identity["market"] == "SHFE" and identity["contract"] == "CU0" and identity["instrument_type"] == "futures"


# ---------------------------------------------------------------------------
# Review item 3: current table keeps the latest record, replaced records go to the history table.
# ---------------------------------------------------------------------------
def _fx_latest():
    return _req(context_type="fx", symbol="HKDCNY", as_of=AS_OF)


def _current_fx_row(runner) -> dict:
    rows = _rows(runner, "select * from fx_rates where rate_type='spot_sell' and rate_date='2026-10-06'")
    assert len(rows) == 1
    return rows[0]


def test_same_day_update_replaces_current_and_archives_full_old_record(runner, fake_ak, clock):
    service = MarketContextService(runner)
    clock(9)
    nine = service.fetch(_fx_latest())
    assert nine.result.value == 85.59
    old = _current_fx_row(runner)
    old_record_id = old["record_id"]
    assert old_record_id in nine.result.provenance.record_ids

    clock(10)
    fake_ak.fx_shift = 1.0
    ten = service.fetch(_fx_latest())
    assert ten.status == "success" and ten.result.value == 86.59
    assert ten.result.quality.usable and ten.result.quality.is_fresh
    new = _current_fx_row(runner)
    assert new["rate"] == 86.59 and new["record_id"] != old_record_id
    assert new["record_id"] in ten.result.provenance.record_ids
    # Whole record replaced: no new value on top of old provenance.
    for col in ("record_id", "request_id", "ingestion_run_id", "raw_payload_id", "raw_payload_ref", "raw_hash", "fetch_time"):
        assert new[col] != old[col], col
    assert json.loads(new["field_provenance"])["rate"]["raw_payload_id"] == new["raw_payload_id"]
    assert "market_context_record_revisions" in ten.result.provenance.tables_written

    revisions = _rows(runner, f"select * from market_context_record_revisions where record_id='{old_record_id}'")
    assert len(revisions) == 1
    rev = revisions[0]
    assert rev["record_id"] == old_record_id and rev["record_type"] == "fx_rate" and rev["table_name"] == "fx_rates"
    assert rev["superseded_by_record_id"] == new["record_id"] and rev["archived_at"]
    archived = json.loads(rev["record_json"])
    assert archived["rate"] == 85.59 and archived["raw_payload_id"] == old["raw_payload_id"]
    assert archived["request_id"] == old["request_id"] and archived["field_provenance"]["rate"]["raw_payload_id"] == old["raw_payload_id"]
    assert json.loads(rev["business_key"])["rate_type"] == "spot_sell"

    # Old record ids cited by earlier reports still resolve to their raw payload.
    with runner.database.session() as session:
        ref = QueryService(session).get_raw_ref_by_record_id(old_record_id)
        assert ref["raw_payload_id"] == old["raw_payload_id"] and ref["archived"] is True
        assert ref["superseded_by_record_id"] == new["record_id"]
        assert QueryService(session).get_raw_ref_by_record_id(new["record_id"])["raw_payload_id"] == new["raw_payload_id"]


def test_same_day_update_repeat_and_restart_return_latest_collection(runner, fake_ak, clock):
    service = MarketContextService(runner)
    clock(9)
    service.fetch(_fx_latest())
    clock(10)
    fake_ak.fx_shift = 1.0
    ten = service.fetch(_fx_latest())
    calls = len(fake_ak.calls)
    repeat = service.fetch(_fx_latest())
    restarted = MarketContextService(_restart(runner)).fetch(_fx_latest())
    assert len(fake_ak.calls) == calls
    current = _current_fx_row(runner)
    for resp in (repeat, restarted):
        assert any(w.startswith("served_from_store") for w in resp.warnings)
        assert resp.result.value == 86.59
        assert current["record_id"] in resp.result.provenance.record_ids
        assert current["raw_payload_id"] in resp.result.provenance.raw_payload_ids
        assert set(resp.result.provenance.record_ids) == set(ten.result.provenance.record_ids)
        assert 85.59 not in {o.values.get("rate") for o in resp.result.series}


def test_same_day_update_late_arriving_older_result_does_not_overwrite(runner, fake_ak, clock, monkeypatch):
    from stock_data_ingestion.adapters import base as adapter_base

    service = MarketContextService(runner)
    clock(9)
    service.fetch(_fx_latest())
    clock(10)
    fake_ak.fx_shift = 1.0
    ten = service.fetch(_fx_latest())
    current = _current_fx_row(runner)
    revisions_before = len(_rows(runner, "select record_id from market_context_record_revisions"))

    # A new refresh cycle whose vendor fetch completed *before* the 10:00 collection (fetch_time 08:00).
    clock(11)
    fake_ak.fx_shift = 0.0
    early = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(adapter_base, "now_asia_shanghai", lambda: early)
    late = service.fetch(_fx_latest())
    assert late.result.value == 86.59, "a late-arriving older result must not roll the current value back"
    assert current["record_id"] in late.result.provenance.record_ids
    assert any(w.startswith("stale_update_rejected") for w in late.warnings)
    after = _current_fx_row(runner)
    assert after["rate"] == 86.59 and after["record_id"] == current["record_id"] and after["raw_payload_id"] == current["raw_payload_id"]
    assert len(_rows(runner, "select record_id from market_context_record_revisions")) == revisions_before, "nothing new archived"
    assert set(late.result.provenance.record_ids) == set(ten.result.provenance.record_ids)

    # Request B (the rejected one) is linked to the records it finally adopted, so its idempotent
    # replays -- same process and after a restart -- return 86.59 with that record's provenance,
    # although the record's own request_id still names the 10:00 request that collected it.
    late_request_id = late.result.provenance.stock_data_request_id
    links = _rows(runner, f"select record_type, record_id from market_context_request_records where request_id='{late_request_id}'")
    assert {l["record_id"] for l in links} >= {current["record_id"]} and {l["record_type"] for l in links} == {"fx_rate"}
    assert after["request_id"] == ten.result.provenance.stock_data_request_id != late_request_id
    calls = len(fake_ak.calls)
    monkeypatch.setattr(adapter_base, "now_asia_shanghai", lambda: datetime(2026, 10, 6, 11, 30, tzinfo=timezone(timedelta(hours=8))))
    repeat = service.fetch(_fx_latest())
    restarted = MarketContextService(_restart(runner)).fetch(_fx_latest())
    assert len(fake_ak.calls) == calls, "same-hour replays of request B are idempotent hits"
    for resp in (repeat, restarted):
        assert any(w.startswith("served_from_store") for w in resp.warnings)
        assert resp.result.value == 86.59 and resp.result.data_date == AS_OF
        assert current["record_id"] in resp.result.provenance.record_ids
        assert current["raw_payload_id"] in resp.result.provenance.raw_payload_ids
        assert resp.result.source.source_api == "currency_boc_sina"
        assert set(resp.result.provenance.record_ids) == set(late.result.provenance.record_ids)
        assert 85.59 not in {o.values.get("rate") for o in resp.result.series}


def test_request_record_links_cover_insert_replace_and_keep(runner, fake_ak, clock):
    """Every retained record is linked to the request, whatever the write action was."""
    service = MarketContextService(runner)
    clock(9)
    nine = service.fetch(_fx_latest())
    nine_id = nine.result.provenance.stock_data_request_id
    inserted_links = _rows(runner, f"select record_id from market_context_request_records where request_id='{nine_id}' and record_type='fx_rate'")
    assert len(inserted_links) == len(nine.result.provenance.record_ids) > 0
    assert {l["record_id"] for l in inserted_links} == set(nine.result.provenance.record_ids)
    assert "market_context_request_records" in nine.result.provenance.tables_written

    clock(10)
    fake_ak.fx_shift = 1.0
    ten = service.fetch(_fx_latest())
    ten_id = ten.result.provenance.stock_data_request_id
    replaced_links = {l["record_id"] for l in _rows(runner, f"select record_id from market_context_request_records where request_id='{ten_id}'")}
    assert replaced_links == set(ten.result.provenance.record_ids) and not (replaced_links & {l["record_id"] for l in inserted_links})

    # Request 9's links point at archived ids: resolving them follows the chain to the current rows.
    with runner.database.session() as session:
        resolved = QueryService(session).resolve_request_records("fx", nine_id)
    assert resolved["source"] == "request_record_links" and resolved["missing"] == []
    assert {r["record_id"] for r in resolved["rows"]} == set(ten.result.provenance.record_ids)
    assert all(chain and chain[-1] in replaced_links for chain in resolved["chains"].values())
    assert len(resolved["chains"]) == len(inserted_links)

    # Legacy request without links (pre-upgrade database) still resolves by record.request_id.
    with runner.database.session() as session:
        from sqlalchemy import text

        session.execute(text(f"delete from market_context_request_records where request_id='{ten_id}'"))
        session.commit()
    with runner.database.session() as session:
        legacy = QueryService(session).resolve_request_records("fx", ten_id)
    assert legacy["source"] == "legacy_record_request_id" and {r["record_id"] for r in legacy["rows"]} == set(ten.result.provenance.record_ids)


def test_repository_upsert_rules_direct(runner, fake_ak, clock):
    from stock_data_ingestion.storage.repositories import Repository

    from sqlalchemy import select

    from stock_data_ingestion.storage.models import FxRateModel

    clock(9)
    MarketContextService(runner).fetch(_fx_latest())
    with runner.database.session() as session:
        repo = Repository(session)
        row = session.execute(select(FxRateModel).where(FxRateModel.rate_type == "spot_sell", FxRateModel.rate_date == AS_OF)).scalar_one()
        base = repo._row_to_record("fx_rate", row)
        older = base.model_copy(update={"record_id": "old_late", "rate": 1.0, "fetch_time": base.fetch_time - timedelta(hours=1)}, deep=True)
        out = repo.upsert_market_context_record(older)
        assert out.action == "kept_existing" and out.table == "fx_rates" and out.record.rate == base.rate and out.record.record_id == base.record_id
        assert out.incoming_record_id == "old_late" and out.existing_record_id == base.record_id and out.comparison_basis == "fetch_time"
        same_age = base.model_copy(update={"record_id": "same_age", "rate": 2.0}, deep=True)
        assert repo.upsert_market_context_record(same_age).action == "kept_existing"
        newer = base.model_copy(update={"record_id": "newer", "rate": 3.0, "fetch_time": base.fetch_time + timedelta(hours=1)}, deep=True)
        out = repo.upsert_market_context_record(newer)
        assert out.action == "replaced" and out.record.record_id == "newer" and out.record.rate == 3.0 and out.archived_record_ids == [base.record_id]
        # provider_update_time wins over fetch_time when both sides carry it
        with_put = out.record.model_copy(update={"record_id": "put_a", "rate": 4.0, "provider_update_time": base.fetch_time, "fetch_time": base.fetch_time + timedelta(hours=2)}, deep=True)
        assert repo.upsert_market_context_record(with_put).action == "replaced"
        older_put = with_put.model_copy(update={"record_id": "put_b", "rate": 5.0, "provider_update_time": base.fetch_time - timedelta(hours=1), "fetch_time": base.fetch_time + timedelta(hours=3)}, deep=True)
        out = repo.upsert_market_context_record(older_put)
        assert out.action == "kept_existing" and out.comparison_basis == "provider_update_time"
        session.commit()
        assert QueryService(session).get_raw_ref_by_record_id(base.record_id)["archived"] is True


def test_legacy_duplicate_rows_collapse_into_one_current_record(runner, fake_ak, clock):
    """Pre-upgrade databases may hold two rows per business key (NULL observed_at bypasses the
    UNIQUE constraint). The next newer collection archives every duplicate and leaves one current
    row; snapshot-date confirmation sees one bar per session."""
    from sqlalchemy import select

    from stock_data_ingestion.storage.models import CommodityPriceModel
    from stock_data_ingestion.storage.repositories import Repository

    clock(9)
    MarketContextService(runner).fetch(_req(context_type="commodity", symbol="CU0", as_of=AS_OF))
    with runner.database.session() as session:
        repo = Repository(session)
        row = session.execute(select(CommodityPriceModel).where(CommodityPriceModel.trade_date == LAST_TRADING, CommodityPriceModel.frequency == "1d")).scalar_one()
        base = repo._row_to_record("commodity_price", row)
        legacy_dup = base.model_copy(update={"record_id": "legacy_dup", "fetch_time": base.fetch_time - timedelta(hours=5)}, deep=True)
        session.add(CommodityPriceModel(**repo._to_model_kwargs(CommodityPriceModel, legacy_dup)))  # bypass the upsert, like old code did
        session.commit()
    assert _rows(runner, f"select count(*) as n from commodity_prices where frequency='1d' and trade_date='{LAST_TRADING}'")[0]["n"] == 2

    clock(10)
    MarketContextService(runner).fetch(_req(context_type="commodity", symbol="CU0", as_of=AS_OF))
    rows = _rows(runner, f"select record_id from commodity_prices where frequency='1d' and trade_date='{LAST_TRADING}'")
    assert len(rows) == 1 and rows[0]["record_id"] not in {"legacy_dup", base.record_id}
    archived = {r["record_id"]: r for r in _rows(runner, f"select record_id, superseded_by_record_id from market_context_record_revisions where json_extract(business_key,'$.trade_date')='{LAST_TRADING}'")}
    assert set(archived) == {"legacy_dup", base.record_id}
    assert {r["superseded_by_record_id"] for r in archived.values()} == {rows[0]["record_id"]}

    resp = MarketContextService(runner).fetch(_cu_realtime())
    _assert_confirmed_last_session(resp)


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------
REQUIRED_LOG_FIELDS = {"event", "timestamp", "level", "trace_id", "request_id", "parent_request_id", "ingestion_run_id", "context_id", "context_type", "symbol", "idempotency_key"}


@pytest.fixture
def jsonl_log():
    import io
    import logging as _logging

    from stock_data_ingestion.logging_config import PACKAGE_LOGGER, setup_logging

    buffer = io.StringIO()
    setup_logging(None, debug=True, stream=buffer)

    def events():
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    yield events
    _logging.getLogger(PACKAGE_LOGGER).handlers.clear()


def test_logging_events_carry_correlation_fields_and_trace(runner, fake_ak, clock, jsonl_log):
    from stock_data_ingestion.logging_config import set_default_log_fields

    set_default_log_fields(data_mode="simulated")
    try:
        resp = MarketContextService(runner).fetch(_req(context_type="commodity", symbol="CU0", frequency="realtime", as_of=AS_OF, trace_id="trace-abc"))
    finally:
        set_default_log_fields(data_mode="live")
    events = jsonl_log()
    assert events, "DEBUG run must produce events"
    for ev in events:
        assert REQUIRED_LOG_FIELDS <= set(ev), ev
        assert ev["trace_id"] == "trace-abc" and ev["data_mode"] == "simulated"
    names = [e["event"] for e in events]
    for required in ("identity_check", "provider_call", "snapshot_date_resolution", "record_write", "request_record_link", "quality_decision", "request_summary"):
        assert required in names, required
    # The daily sub-request inherits the trace and names its parent.
    outer_id = resp.result.provenance.stock_data_request_id
    daily = [e for e in events if e["event"] == "record_write" and e["record_type"] == "commodity_price" and e["business_key"]["frequency"] == "1d"]
    assert daily and all(e["parent_request_id"] == outer_id and e["request_id"] != outer_id for e in daily)
    snap_write = next(e for e in events if e["event"] == "record_write" and e["business_key"]["frequency"] == "realtime")
    assert snap_write["request_id"] == outer_id and snap_write["parent_request_id"] is None and snap_write["action"] == "inserted"
    resolution = next(e for e in events if e["event"] == "snapshot_date_resolution")
    assert resolution["decision"] == "saved" and resolution["confirmed_date"] == LAST_TRADING.isoformat()
    assert len(resolution["reference_bars"]) == 2 and resolution["reference_bars"][-1]["trade_date"] == LAST_TRADING.isoformat()
    summary = next(e for e in events if e["event"] == "request_summary")
    assert summary["level"] == "INFO" and summary["status"] == "partial_success" and summary["value"] == 109290 and summary["data_date"] == LAST_TRADING.isoformat()
    # A store replay logs the original request and the chain it followed.
    MarketContextService(runner).fetch(_req(context_type="commodity", symbol="CU0", frequency="realtime", as_of=AS_OF, trace_id="trace-def"))
    replay = [e for e in jsonl_log() if e["event"] == "store_replay"]
    assert replay and replay[-1]["original_request_id"] == outer_id and replay[-1]["resolution_source"] == "request_record_links"
    assert replay[-1]["trace_id"] == "trace-def"


def test_logging_warnings_for_rejection_unknown_date_and_block(runner, fake_ak, clock, jsonl_log, monkeypatch):
    service = MarketContextService(runner)
    service.fetch(_req(context_type="commodity", symbol="CU0", contract="CU2612", as_of=AS_OF))
    fake_ak.fail.add("futures_zh_daily_sina")
    service.fetch(_cu_realtime())
    fake_ak.fail.discard("futures_zh_daily_sina")
    original = runner._rescore_record
    monkeypatch.setattr(runner, "_rescore_record", lambda r, c: original(r, c).model_copy(update={"validation_status": "quarantined"}))
    service.fetch(_req(context_type="hk_index", symbol="HSTECH", as_of=AS_OF))
    warnings = [(e["event"], e["level"]) for e in jsonl_log() if e["level"] == "WARNING"]
    assert ("business_param_rejected", "WARNING") in warnings
    assert ("snapshot_date_unknown", "WARNING") in warnings
    assert ("upstream_validation_blocked", "WARNING") in warnings


def test_logging_info_level_has_no_per_record_detail(runner, fake_ak, clock):
    import io
    import logging as _logging

    from stock_data_ingestion.logging_config import PACKAGE_LOGGER, setup_logging

    buffer = io.StringIO()
    setup_logging(None, debug=False, stream=buffer)
    try:
        MarketContextService(runner).fetch(_fx_latest())
    finally:
        _logging.getLogger(PACKAGE_LOGGER).handlers.clear()
    events = [json.loads(l) for l in buffer.getvalue().splitlines() if l.strip()]
    assert [e["event"] for e in events] == ["request_summary"]
    assert all(e["level"] in {"INFO", "WARNING", "ERROR"} for e in events)


def test_cli_debug_and_log_file_flags(runner, monkeypatch, tmp_path, capsys):
    import logging as _logging

    from stock_data_ingestion import cli
    from stock_data_ingestion.logging_config import PACKAGE_LOGGER

    collector = StockDataCollector(runner)
    monkeypatch.setattr(cli, "_build_collector", lambda config_dir=None: collector)
    monkeypatch.setattr(cli, "load_config", lambda config_dir=None: runner.config)
    log_file = tmp_path / "logs" / "debug.jsonl"
    try:
        cli.main(["--debug", "--log-file", str(log_file), "fetch", "market-context", "--context-type", "fx", "--symbol", "HKDCNY", "--as-of", "2026-10-06", "--trace-id", "cli-trace", "--compact"])
    finally:
        _logging.getLogger(PACKAGE_LOGGER).handlers.clear()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success", "stdout stays business JSON"
    events = [json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(e["event"] == "record_write" for e in events) and all(e["trace_id"] == "cli-trace" for e in events)
    assert {e["level"] for e in events} >= {"DEBUG", "INFO"}


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
