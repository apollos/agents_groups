from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

from stock_data_ingestion.adapters.tushare_adapter import TushareAdapter
from stock_data_ingestion.schemas.records import AdapterFetchStatus
from stock_data_ingestion.schemas.requests import StockDataRequest


class FakeDataFrame:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.empty = not records

    def to_dict(self, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self._records


class FakePro:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def hk_daily(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("hk_daily", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "trade_date": "20260529",
                    "open": 80.0,
                    "high": 83.0,
                    "low": 79.0,
                    "close": 82.0,
                    "pre_close": 81.0,
                    "change": 1.0,
                    "pct_chg": 1.23,
                    "vol": 1000000,
                    "amount": 80000000,
                }
            ]
        )


def test_tushare_hk_daily_route_and_metadata(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "tushare", SimpleNamespace(pro_bar=lambda **_: None))
    fake = FakePro()
    adapter = TushareAdapter()
    adapter.token = "fake"
    adapter._pro = fake
    adapter.is_available = lambda: True  # type: ignore[method-assign]
    adapter.authenticate = lambda: True  # type: ignore[method-assign]
    request = StockDataRequest(
        request_id="req_hk_daily",
        request_type="historical_bars",
        tickers=["00001.HK"],
        market="HK",
        start_date="20260501",
        end_date="20260529",
        frequency="1d",
        adjust="none",
        export_parquet=False,
    )

    result = adapter.fetch_historical_bars(request)

    assert result.status == AdapterFetchStatus.success
    assert result.source_api == "hk_daily"
    assert fake.calls[0][0] == "hk_daily"
    row = result.raw_records[0]
    assert row["normalized_ticker"] == "00001.HK"
    assert row["exchange"] == "HK"
    assert row["market"] == "HK"
    assert row["currency"] == "HKD"

class FakeProMixedBasic(FakePro):
    def stock_basic(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("stock_basic", kwargs))
        ts_code = kwargs.get("ts_code") or "600519.SH"
        return FakeDataFrame(
            [
                {
                    "ts_code": ts_code,
                    "symbol": "600519",
                    "name": "贵州茅台",
                    "area": "贵州",
                    "industry": "食品饮料",
                    "market": "主板",
                    "exchange": "SSE",
                    "curr_type": "CNY",
                    "list_status": "L",
                    "list_date": "20010827",
                    "delist_date": None,
                }
            ]
        )

    def stock_company(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("stock_company", kwargs))
        return FakeDataFrame([])

    def hk_basic(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("hk_basic", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "name": "长和",
                    "fullname": "CK Hutchison Holdings Limited",
                    "market": "主板",
                    "list_status": "L",
                    "list_date": "19721101",
                    "delist_date": None,
                }
            ]
        )


def test_tushare_security_master_can_mix_a_share_and_hk_tickers(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "tushare", SimpleNamespace(pro_bar=lambda **_: None))
    fake = FakeProMixedBasic()
    adapter = TushareAdapter()
    adapter.token = "fake"
    adapter._pro = fake
    adapter.is_available = lambda: True  # type: ignore[method-assign]
    adapter.authenticate = lambda: True  # type: ignore[method-assign]
    request = StockDataRequest(
        request_id="req_mixed_security",
        request_type="security_master",
        tickers=["600519.SH", "00001.HK"],
        export_parquet=False,
    )

    result = adapter.fetch_security_master(request)

    assert result.status == AdapterFetchStatus.success
    assert result.source_api == "stock_basic+hk_basic"
    assert {row["ts_code"] for row in result.raw_records} == {"600519.SH", "00001.HK"}
    hk_row = next(row for row in result.raw_records if row["ts_code"] == "00001.HK")
    assert hk_row["exchange"] == "HK"
    assert hk_row["currency"] == "HKD"
