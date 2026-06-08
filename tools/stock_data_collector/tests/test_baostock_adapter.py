from __future__ import annotations

from datetime import date

import pytest

from stock_data_ingestion.adapters.baostock_adapter import BaoStockAdapter
from stock_data_ingestion.schemas.records import AdapterFetchStatus
from stock_data_ingestion.schemas.requests import StockDataRequest


class FakeBaoStockResult:
    def __init__(self, fields: list[str], rows: list[list[str]], error_code: str = "0", error_msg: str = "") -> None:
        self.fields = fields
        self._rows = rows
        self._index = -1
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._index]


class FakeBaoStockModule:
    def __init__(self) -> None:
        self.history_calls: list[dict] = []
        self.adjust_calls: list[dict] = []
        self.financial_calls: list[tuple[str, dict]] = []

    def query_history_k_data_plus(self, code: str, fields: str, start_date=None, end_date=None, frequency="d", adjustflag="3"):
        self.history_calls.append(
            {
                "code": code,
                "fields": fields,
                "start_date": start_date,
                "end_date": end_date,
                "frequency": frequency,
                "adjustflag": adjustflag,
            }
        )
        return FakeBaoStockResult(
            fields.split(","),
            [["2026-05-29", code, "10", "11", "9", "10.5", "10", "123456", "1300000", adjustflag, "1.23", "1", "5.0", "12.3", "1.5", "2.1", "8.8", "0"]],
        )

    def query_adjust_factor(self, code: str, start_date=None, end_date=None):
        self.adjust_calls.append({"code": code, "start_date": start_date, "end_date": end_date})
        return FakeBaoStockResult(
            ["code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"],
            [[code, "2026-05-29", "0.75", "12.3", "0.75"]],
        )

    def query_stock_basic(self, code=None, code_name=None):
        rows = [
            ["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"],
            ["sz.159001", "ETF示例", "2026-01-01", "", "5", "1"],
            ["sz.000001", "退市示例", "1991-04-03", "2020-01-01", "1", "0"],
        ]
        if code:
            rows = [row for row in rows if row[0] == code]
        return FakeBaoStockResult(["code", "code_name", "ipoDate", "outDate", "type", "status"], rows)

    def query_stock_industry(self, date=None):
        return FakeBaoStockResult(
            ["updateDate", "code", "code_name", "industry", "industryClassification"],
            [["2026-05-25", "sh.600000", "浦发银行", "银行", "申万一级行业"]],
        )

    def query_profit_data(self, code: str, year: int, quarter: int):
        self.financial_calls.append(("query_profit_data", {"code": code, "year": year, "quarter": quarter}))
        return FakeBaoStockResult(
            ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM", "MBRevenue", "totalShare", "liqaShare"],
            [[code, "2026-08-30", "2026-06-30", "7.1", "12.5", "34.5", "1000000", "0.50", "9000000", "100000", "80000"]],
        )

    def query_growth_data(self, code: str, year: int, quarter: int):
        self.financial_calls.append(("query_growth_data", {"code": code, "year": year, "quarter": quarter}))
        return FakeBaoStockResult(
            ["code", "pubDate", "statDate", "YOYEquity", "YOYAsset", "YOYNI", "YOYEPSBasic", "YOYPNI"],
            [[code, "2026-08-30", "2026-06-30", "8.0", "9.0", "10.0", "11.0", "12.0"]],
        )


@pytest.fixture
def baostock_adapter(monkeypatch):
    monkeypatch.setenv("STOCK_DATA_DISABLE_ENV_AUTOLOAD", "true")
    fake_bs = FakeBaoStockModule()
    adapter = BaoStockAdapter()
    monkeypatch.setattr(adapter, "is_available", lambda: True)
    adapter._bs = fake_bs
    adapter._authenticated = True
    return adapter, fake_bs


def test_baostock_historical_bars_maps_a_share_daily_fields_and_skips_hk_bj(baostock_adapter):
    adapter, fake_bs = baostock_adapter
    request = StockDataRequest(
        request_id="req_bs_bar",
        request_type="historical_bars",
        tickers=["600000.SH", "00005.HK", "430047.BJ"],
        start_date="2026-05-01",
        end_date="2026-05-29",
        frequency="1d",
        adjust="none",
        provider_priority=["baostock"],
    )
    result = adapter.fetch_historical_bars(request)

    assert result.status == AdapterFetchStatus.success
    assert len(result.raw_records) == 1
    row = result.raw_records[0]
    assert fake_bs.history_calls[0]["code"] == "sh.600000"
    assert fake_bs.history_calls[0]["adjustflag"] == "3"
    assert row["normalized_ticker"] == "600000.SH"
    assert row["volume_unit"] == "share"
    assert row["amount_unit"] == "CNY"
    assert row["peTTM"] == "12.3"
    assert row["pcfNcfTTM"] == "8.8"


def test_baostock_security_master_filters_non_stock_and_delisted(baostock_adapter):
    adapter, _ = baostock_adapter
    request = StockDataRequest(request_id="req_bs_sm", request_type="security_master", tickers=[], provider_priority=["baostock"])
    result = adapter.fetch_security_master(request)

    assert result.status == AdapterFetchStatus.success
    assert len(result.raw_records) == 1
    row = result.raw_records[0]
    assert row["normalized_ticker"] == "600000.SH"
    assert row["name"] == "浦发银行"
    assert row["list_status"] == "L"
    assert row["industry"] == "银行"


def test_baostock_adjust_factor_keeps_method_specific_fields(baostock_adapter):
    adapter, fake_bs = baostock_adapter
    request = StockDataRequest(
        request_id="req_bs_adj",
        request_type="adj_factor",
        tickers=["600000.SH"],
        start_date="2026-01-01",
        end_date="2026-05-29",
        provider_priority=["baostock"],
    )
    result = adapter.fetch_adj_factor(request)

    assert result.status == AdapterFetchStatus.success
    row = result.raw_records[0]
    assert fake_bs.adjust_calls[0]["code"] == "sh.600000"
    assert row["adj_factor"] is None
    assert row["fore_adjust_factor"] == "0.75"
    assert row["back_adjust_factor"] == "12.3"
    assert row["factor_method"] == "baostock_pct_change_adjustment_factor"


def test_baostock_financial_indicator_merges_selected_quarterly_endpoints(baostock_adapter):
    adapter, fake_bs = baostock_adapter
    request = StockDataRequest(
        request_id="req_bs_fin",
        request_type="financial_indicator",
        tickers=["600000.SH"],
        end_date=date(2026, 6, 30),
        provider_priority=["baostock"],
        extra_params={"year": 2026, "quarter": 2, "baostock_financial_endpoints": ["profit", "growth"]},
    )
    result = adapter.fetch_financial_indicator(request)

    assert result.status == AdapterFetchStatus.success
    assert len(result.raw_records) == 1
    row = result.raw_records[0]
    assert row["report_period"] == "20260630"
    assert row["roe"] == "7.1"
    assert row["net_profit_yoy"] == "10.0"
    assert row["eps"] == "0.50"
    assert {name for name, _ in fake_bs.financial_calls} == {"query_profit_data", "query_growth_data"}


def test_baostock_money_flow_is_explicitly_empty_because_api_is_not_documented(baostock_adapter):
    adapter, _ = baostock_adapter
    request = StockDataRequest(
        request_id="req_bs_mf",
        request_type="money_flow",
        tickers=["600000.SH"],
        start_date="2026-05-29",
        end_date="2026-05-29",
        provider_priority=["baostock"],
    )
    result = adapter.fetch_money_flow(request)
    assert result.status == AdapterFetchStatus.empty_result
    assert result.raw_records == []
