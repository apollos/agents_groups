"""HK-connect snapshot service tests (offline: akshare/requests are faked)."""

from __future__ import annotations

import json
import sys
import time

import pytest

import stock_data_ingestion.services.hk_connect_service as hk
from stock_data_ingestion.services.hk_connect_service import (
    HKConnectCollector,
    collect_hk_connect_snapshots,
)


class _FakeDF:
    def __init__(self, records: list[dict]):
        self._records = records

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return self._records


class _FakeAkshare:
    @staticmethod
    def stock_hk_ggt_components_em():
        return _FakeDF([{"代码": "00700", "名称": "腾讯控股", "最新价": 512.0, "成交额": 8.1e9}])

    @staticmethod
    def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
        assert symbol == "南向持股"
        return _FakeDF(
            [
                {
                    "股票代码": "00700",
                    "股票简称": "腾讯控股",
                    "持股数量": 8.2e8,
                    "持股市值": 4.2e11,
                    "持股数量占发行股百分比": 8.9,
                    "持股市值变化-1日": 1.2e9,
                    "持股市值变化-5日": -3.0e9,
                    "持股市值变化-10日": 5.5e9,
                }
            ]
        )


def test_collects_and_maps_snapshot_fields(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkshare())
    payload = collect_hk_connect_snapshots(["0700.HK"], as_of="2026-07-06T16:30:00+08:00")
    assert payload["status"] == "success"
    assert payload["errors"] == []
    (snap,) = payload["data"]["hk_connect_snapshots"]
    assert snap["ticker"] == "00700.HK"
    assert snap["as_of"] == "2026-07-06"
    assert snap["hk_connect_eligible"] is True
    assert snap["last_price_hkd"] == 512.0
    assert snap["turnover_hkd"] == 8.1e9
    assert snap["southbound_holding_pct"] == 8.9
    assert snap["southbound_mv_change_5d"] == -3.0e9
    quality = snap["quality"]
    assert quality["usable"] is True
    assert quality["has_holding"] is True
    assert quality["holding_data_date"] == "20260706"
    assert quality["missing_fields"] == []
    assert quality["field_completeness"]["ratio"] == 1.0
    assert set(quality["unsourced_fields"]) == {
        "buyback_amount_hkd", "ah_premium_pct", "hk_liquidity_score",
    }


def test_components_fall_back_to_direct_fetch_on_waf_reset(monkeypatch):
    """Eastmoney WAF resets akshare's bare client; components must fall back to the
    direct clist fetch with browser headers + EASTMONEY_COOKIE and still succeed."""

    class _WafBlockedAkshare(_FakeAkshare):
        @staticmethod
        def stock_hk_ggt_components_em():
            raise ConnectionError("Remote end closed connection without response")

    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"total": 1, "diff": [
                {"f12": "00700", "f14": "腾讯控股", "f2": 512.0, "f6": 8.1e9}]}}

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _FakeResp()

    monkeypatch.setitem(sys.modules, "akshare", _WafBlockedAkshare())
    monkeypatch.setitem(sys.modules, "requests", _FakeRequests())
    monkeypatch.setenv("EASTMONEY_COOKIE", "qgqp_b_id=test-cookie")

    payload = collect_hk_connect_snapshots(["0700.HK"], as_of="2026-07-06")
    assert payload["status"] == "success"
    (snap,) = payload["data"]["hk_connect_snapshots"]
    assert snap["hk_connect_eligible"] is True
    assert snap["turnover_hkd"] == 8.1e9
    assert captured["url"] == hk._GGT_CLIST_URL
    assert captured["headers"]["Cookie"] == "qgqp_b_id=test-cookie"
    assert "User-Agent" in captured["headers"]
    assert captured["timeout"] is not None


@pytest.mark.parametrize("empty_style", ["raises", "empty_frame"])
def test_holding_falls_back_to_latest_published_row(monkeypatch, empty_style):
    """Southbound holding stats publish after settlement, so the as_of date often has
    no data yet: akshare either crashes on the empty payload or returns an empty
    frame. Either way the service must fall back to the direct datacenter fetch,
    which returns the latest published row <= as_of."""

    class _NoHoldingYetAkshare(_FakeAkshare):
        @staticmethod
        def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
            if empty_style == "raises":
                raise TypeError("'NoneType' object is not subscriptable")
            return _FakeDF([])

    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"data": [
                {
                    "TRADE_DATE": "2026-07-03 00:00:00",
                    "SECURITY_CODE": "00700",
                    "SECURITY_NAME": "腾讯控股",
                    "CLOSE_PRICE": 442.4,
                    "HOLD_SHARES": 8.2e8,
                    "HOLD_MARKET_CAP": 4.2e11,
                    "HOLD_SHARES_RATIO": 8.9,
                    "HOLD_MARKETCAP_CHG1": 1.2e9,
                    "HOLD_MARKETCAP_CHG5": -3.0e9,
                    "HOLD_MARKETCAP_CHG10": 5.5e9,
                }
            ]}}

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            captured["params"] = params
            captured["timeout"] = timeout
            return _FakeResp()

    monkeypatch.setitem(sys.modules, "akshare", _NoHoldingYetAkshare())
    monkeypatch.setitem(sys.modules, "requests", _FakeRequests())
    payload = collect_hk_connect_snapshots(["0700.HK"], as_of="2026-07-06")
    assert payload["status"] == "success"
    (snap,) = payload["data"]["hk_connect_snapshots"]
    assert snap["southbound_holding_pct"] == 8.9
    assert snap["quality"]["holding_data_date"] == "20260703"
    assert snap["quality"]["has_holding"] is True
    assert captured["timeout"] is not None
    assert 'SECURITY_CODE="00700"' in captured["params"]["filter"]
    assert "TRADE_DATE<='2026-07-06'" in captured["params"]["filter"]


def test_hanging_fetches_are_deadline_bounded(monkeypatch):
    """A wedged DNS lookup / WAF black-hole must surface as a fast, actionable error,
    not block the CLI indefinitely (akshare sends requests without any timeout)."""

    class _HangingAkshare(_FakeAkshare):
        @staticmethod
        def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
            time.sleep(30)
            raise AssertionError("unreachable")

    class _HangingRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            time.sleep(30)
            raise AssertionError("unreachable")

    monkeypatch.setattr(hk, "AKSHARE_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(hk, "DIRECT_FETCH_DEADLINE_SECONDS", 0.05)
    monkeypatch.setitem(sys.modules, "akshare", _HangingAkshare())
    monkeypatch.setitem(sys.modules, "requests", _HangingRequests())
    monkeypatch.setenv("EASTMONEY_COOKIE", "qgqp_b_id=test-cookie")

    start = time.monotonic()
    payload = collect_hk_connect_snapshots(["0700.HK"], as_of="2026-07-06")
    assert time.monotonic() - start < 5
    assert payload["status"] == "failed"
    err = payload["errors"][0]
    assert err["error_code"] == "HK_CONNECT_COLLECT_FAILED"
    assert "did not finish within" in err["error_message"]
    assert "may have expired" in err["suggested_action"]


def test_components_blocked_degrades_to_holding_only_snapshot(monkeypatch):
    """The push2 quotes cluster (component list) gets WAF-blocked independently of
    datacenter-web (holding stats): a blocked component list must degrade to a
    usable holding-only snapshot — eligibility inferred from the holding row,
    close price from the direct holding fetch, only turnover missing."""

    class _ComponentsBlockedAkshare(_FakeAkshare):
        @staticmethod
        def stock_hk_ggt_components_em():
            raise ConnectionError("Remote end closed connection without response")

        @staticmethod
        def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
            raise TypeError("'NoneType' object is not subscriptable")

    class _FakeRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            if url == hk._GGT_CLIST_URL:
                raise ConnectionError("Remote end closed connection without response")

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"result": {"data": [
                        {"TRADE_DATE": "2026-08-19 00:00:00", "SECURITY_CODE": "00700",
                         "SECURITY_NAME": "腾讯控股", "CLOSE_PRICE": 442.4, "HOLD_SHARES": 8.2e8,
                         "HOLD_MARKET_CAP": 4.2e11, "HOLD_SHARES_RATIO": 8.9,
                         "HOLD_MARKETCAP_CHG1": 1.2e9, "HOLD_MARKETCAP_CHG5": -3.0e9,
                         "HOLD_MARKETCAP_CHG10": 5.5e9}
                    ]}}

            return _Resp()

    monkeypatch.setitem(sys.modules, "akshare", _ComponentsBlockedAkshare())
    monkeypatch.setitem(sys.modules, "requests", _FakeRequests())
    payload = collect_hk_connect_snapshots(["0700.HK"], as_of="2026-08-20")
    assert payload["status"] == "success"
    assert payload["warnings"][0]["warning_code"] == "HK_CONNECT_COMPONENTS_UNAVAILABLE"
    (snap,) = payload["data"]["hk_connect_snapshots"]
    assert snap["hk_connect_eligible"] is True  # inferred: southbound holding exists
    assert snap["last_price_hkd"] == 442.4  # close price from the direct holding row
    assert snap["southbound_holding_pct"] == 8.9
    quality = snap["quality"]
    assert quality["usable"] is True
    assert quality["components_available"] is False
    assert quality["missing_fields"] == ["turnover_hkd"]
    assert quality["field_completeness"]["filled_count"] == 7


def test_failure_hint_distinguishes_missing_vs_stale_cookie(monkeypatch):
    class _BlockedAkshare(_FakeAkshare):
        @staticmethod
        def stock_hk_ggt_components_em():
            raise ConnectionError("Remote end closed connection without response")

        @staticmethod
        def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
            raise ConnectionError("Remote end closed connection without response")

    class _BlockedRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setitem(sys.modules, "akshare", _BlockedAkshare())
    monkeypatch.setitem(sys.modules, "requests", _BlockedRequests())

    monkeypatch.delenv("EASTMONEY_COOKIE", raising=False)
    payload = collect_hk_connect_snapshots(["0700.HK"], as_of="2026-07-06")
    assert payload["status"] == "failed"
    err = payload["errors"][0]
    assert err["error_code"] == "HK_CONNECT_COLLECT_FAILED"
    assert err["retryable"] is True
    assert "EASTMONEY_COOKIE is not configured" in err["suggested_action"]
    (snap,) = payload["data"]["hk_connect_snapshots"]
    assert snap["quality"]["usable"] is False
    assert snap["quality"]["cookie_configured"] is False

    monkeypatch.setenv("EASTMONEY_COOKIE", "qgqp_b_id=stale")
    payload = collect_hk_connect_snapshots(["0700.HK"], as_of="2026-07-06")
    assert "EASTMONEY_COOKIE may have expired" in payload["errors"][0]["suggested_action"]


def test_batch_isolates_per_ticker_failures(monkeypatch):
    """One blocked ticker must not fail the whole batch: status=partial_success and
    the failed ticker carries its own error entry."""

    class _FlakyAkshare(_FakeAkshare):
        @staticmethod
        def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
            raise TypeError("'NoneType' object is not subscriptable")

    calls = {"n": 0}

    class _FlakyRequests:
        @staticmethod
        def get(url, params=None, headers=None, timeout=None):
            calls["n"] += 1
            if 'SECURITY_CODE="09988"' in (params or {}).get("filter", ""):
                raise ConnectionError("Remote end closed connection without response")

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"result": {"data": [
                        {"TRADE_DATE": "2026-07-03 00:00:00", "SECURITY_CODE": "00700",
                         "SECURITY_NAME": "腾讯控股", "CLOSE_PRICE": 442.4, "HOLD_SHARES": 8.2e8,
                         "HOLD_MARKET_CAP": 4.2e11, "HOLD_SHARES_RATIO": 8.9,
                         "HOLD_MARKETCAP_CHG1": 1.2e9, "HOLD_MARKETCAP_CHG5": -3.0e9,
                         "HOLD_MARKETCAP_CHG10": 5.5e9}
                    ]}}

            return _Resp()

    monkeypatch.setitem(sys.modules, "akshare", _FlakyAkshare())
    monkeypatch.setitem(sys.modules, "requests", _FlakyRequests())
    payload = collect_hk_connect_snapshots(["0700.HK", "9988.HK"], as_of="2026-07-06")
    assert payload["status"] == "partial_success"
    ok, bad = payload["data"]["hk_connect_snapshots"]
    assert ok["ticker"] == "00700.HK" and ok["quality"]["usable"] is True
    assert bad["ticker"] == "09988.HK" and bad["quality"]["usable"] is False
    assert payload["errors"][0]["ticker"] == "9988.HK"


def test_akshare_missing_reports_non_retryable(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", None)  # import akshare -> ImportError
    payload = collect_hk_connect_snapshots(["0700.HK"])
    assert payload["status"] == "failed"
    assert payload["errors"][0]["error_code"] == "AKSHARE_NOT_INSTALLED"
    assert payload["errors"][0]["retryable"] is False


def test_cli_fetch_hk_connect_prints_payload(monkeypatch, capsys):
    from stock_data_ingestion import cli

    def _fake_collect(tickers, *, as_of=None):
        return {"status": "success", "as_of": as_of, "data": {"hk_connect_snapshots": [{"ticker": tickers[0]}]}, "errors": []}

    monkeypatch.setattr(
        "stock_data_ingestion.services.hk_connect_service.collect_hk_connect_snapshots",
        _fake_collect,
    )
    cli.main(["fetch", "hk-connect", "--tickers", "00700.HK", "--as-of", "2026-07-06"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "success"
    assert payload["as_of"] == "2026-07-06"
    assert payload["data"]["hk_connect_snapshots"][0]["ticker"] == "00700.HK"


def test_partial_data_reports_field_completeness(monkeypatch):
    """Stats for the date exist but carry no row for the stock (no southbound
    holding): the snapshot is still usable — no fallback to an older session —
    with completeness marking the 6 missing fields."""

    class _NoRowForStockAkshare(_FakeAkshare):
        @staticmethod
        def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
            return _FakeDF([{"股票代码": "09988", "股票简称": "阿里巴巴-W", "持股数量": 1.0e8}])

    monkeypatch.setitem(sys.modules, "akshare", _NoRowForStockAkshare())
    payload = collect_hk_connect_snapshots(["00700.HK"], as_of="2026-07-06")
    assert payload["status"] == "success"
    (snap,) = payload["data"]["hk_connect_snapshots"]
    quality = snap["quality"]
    assert quality["usable"] is True
    assert quality["has_holding"] is False
    completeness = quality["field_completeness"]
    assert completeness["required_count"] == len(hk.HK_REQUIRED_FIELDS)
    # price + turnover filled, all 6 southbound fields missing
    assert completeness["filled_count"] == 2
    assert completeness["ratio"] == round(2 / len(hk.HK_REQUIRED_FIELDS), 4)
    assert "southbound_holding_pct" in quality["missing_fields"]
    # unwired fields are reported separately, not as collection failures
    assert set(quality["unsourced_fields"]) == {
        "buyback_amount_hkd", "ah_premium_pct", "hk_liquidity_score",
    }


def test_components_shared_across_batch(monkeypatch):
    """The component list is fetched once per invocation, not once per ticker."""
    calls = {"components": 0}

    class _CountingAkshare(_FakeAkshare):
        @staticmethod
        def stock_hk_ggt_components_em():
            calls["components"] += 1
            return _FakeAkshare.stock_hk_ggt_components_em()

    monkeypatch.setitem(sys.modules, "akshare", _CountingAkshare())
    payload = collect_hk_connect_snapshots(["0700.HK", "0700.HK"], as_of="2026-07-06")
    assert payload["status"] == "success"
    assert calls["components"] == 1
