from __future__ import annotations

import sys
import types
from datetime import date

import pandas as pd

from stock_data_ingestion.adapters.joinquant_adapter import JoinQuantAdapter
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import AdapterFetchStatus
from stock_data_ingestion.schemas.requests import StockDataRequest


class FakeJQDataSDK(types.SimpleNamespace):
    def __init__(self, df):
        super().__init__()
        self.df = df
        self.auth_calls = []
        self.get_all_securities_calls = []

    def auth(self, username, password):
        self.auth_calls.append((username, password))
        return True

    def get_all_securities(self, types=None):  # noqa: A002 - mirrors jqdatasdk API
        self.get_all_securities_calls.append({"types": types})
        return self.df


def _install_fake_jq(monkeypatch, df):
    fake_jq = FakeJQDataSDK(df)
    monkeypatch.setitem(sys.modules, "jqdatasdk", fake_jq)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "jqdatasdk" else None)
    return fake_jq


def test_joinquant_fetch_security_master_filters_requested_tickers(monkeypatch):
    monkeypatch.setenv("JQDATA_USERNAME", "user")
    monkeypatch.setenv("JQDATA_PASSWORD", "pass")
    fake_jq = _install_fake_jq(
        monkeypatch,
        pd.DataFrame(
            {
                "display_name": ["贵州茅台", "平安银行", "宁德时代"],
                "name": ["GZMT", "PAYH", "NDSD"],
                "start_date": [date(2001, 8, 27), date(1991, 4, 3), date(2018, 6, 11)],
                "end_date": [date(2200, 1, 1), date(2200, 1, 1), date(2200, 1, 1)],
                "type": ["stock", "stock", "stock"],
            },
            index=["600519.XSHG", "000001.XSHE", "300750.XSHE"],
        ),
    )

    adapter = JoinQuantAdapter()
    request = StockDataRequest(request_id="req_jq_sm", request_type="security_master", tickers=["600519.SH", "000001.SZ"])

    result = adapter.fetch_security_master(request)

    assert result.status == AdapterFetchStatus.success
    assert result.source_api == "get_all_securities"
    assert result.rows_fetched == 2
    assert fake_jq.auth_calls == [("user", "pass")]
    assert fake_jq.get_all_securities_calls == [{"types": ["stock"]}]

    rows = {row["normalized_ticker"]: row for row in result.raw_records}
    assert set(rows) == {"600519.SH", "000001.SZ"}
    assert rows["600519.SH"]["provider_symbol"] == "600519.XSHG"
    assert rows["600519.SH"]["symbol"] == "600519"
    assert rows["600519.SH"]["exchange"] == "SH"
    assert rows["600519.SH"]["name"] == "贵州茅台"
    assert rows["600519.SH"]["jq_original_name"] == "GZMT"
    assert rows["600519.SH"]["list_date"] == "2001-08-27"
    assert rows["600519.SH"]["list_status"] == "L"
    assert rows["600519.SH"]["delist_date"] is None


def test_joinquant_fetch_security_master_skips_invalid_provider_symbols(monkeypatch):
    monkeypatch.setenv("JQDATA_USERNAME", "user")
    monkeypatch.setenv("JQDATA_PASSWORD", "pass")
    _install_fake_jq(
        monkeypatch,
        pd.DataFrame(
            {
                "display_name": ["贵州茅台", "异常代码"],
                "name": ["GZMT", "BAD"],
                "start_date": [date(2001, 8, 27), date(2000, 1, 1)],
                "end_date": [date(2200, 1, 1), date(2200, 1, 1)],
                "type": ["stock", "stock"],
            },
            index=["600519.XSHG", "TS0018.XSHG"],
        ),
    )

    adapter = JoinQuantAdapter()
    request = StockDataRequest(request_id="req_jq_all", request_type="security_master")

    result = adapter.fetch_security_master(request)

    assert result.status == AdapterFetchStatus.success
    assert [row["normalized_ticker"] for row in result.raw_records] == ["600519.SH"]


def test_joinquant_fetch_security_master_missing_credentials(monkeypatch):
    monkeypatch.delenv("JQDATA_USERNAME", raising=False)
    monkeypatch.delenv("JQDATA_PASSWORD", raising=False)

    adapter = JoinQuantAdapter()
    request = StockDataRequest(request_id="req_jq_no_auth", request_type="security_master", tickers=["600519.SH"])

    result = adapter.fetch_security_master(request)

    assert result.status == AdapterFetchStatus.unavailable
    assert result.error is not None
    assert result.error.error_code == ErrorCode.AUTH_FAILED
