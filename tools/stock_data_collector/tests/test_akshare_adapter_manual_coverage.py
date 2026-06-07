from __future__ import annotations

import importlib.util
import sys
import types
from datetime import date

import pytest

from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter
from stock_data_ingestion.schemas.records import AdapterFetchStatus
from stock_data_ingestion.schemas.requests import Frequency, RequestType, StockDataRequest


class FakeDF:
    def __init__(self, rows):
        self._rows = rows
        self.empty = len(rows) == 0
        self.columns = list(rows[0].keys()) if rows else []

    def to_dict(self, orient="records"):
        assert orient == "records"
        return list(self._rows)


def install_fake_ak(monkeypatch: pytest.MonkeyPatch, fake: types.SimpleNamespace) -> None:
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object() if name == "akshare" else None)


def request(request_type: RequestType, **kwargs):
    return StockDataRequest(request_id="req_test", request_type=request_type, provider_priority=["akshare"], canonical_provider="akshare", **kwargs)


def test_security_master_enriches_code_name_with_detail_and_exchange_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    fake = types.SimpleNamespace(
        stock_info_a_code_name=lambda: FakeDF([{"code": "600519", "name": "贵州茅台"}]),
        stock_info_sh_name_code=lambda symbol="主板A股": FakeDF(
            [{"证券代码": "600519", "证券简称": "贵州茅台", "公司全称": "贵州茅台酒股份有限公司", "上市日期": "2001-08-27"}]
            if symbol == "主板A股"
            else []
        ),
        stock_info_sz_name_code=lambda symbol="A股列表": FakeDF([]),
        stock_info_bj_name_code=lambda: FakeDF([]),
        stock_individual_info_em=lambda symbol: calls.append(symbol)
        or FakeDF(
            [
                {"item": "行业", "value": "白酒"},
                {"item": "总股本", "value": 1256197800.0},
                {"item": "流通股", "value": 1256197800.0},
                {"item": "上市时间", "value": "20010827"},
            ]
        ),
    )
    install_fake_ak(monkeypatch, fake)

    result = AKShareAdapter().fetch_security_master(request(RequestType.security_master, tickers=["600519.SH"]))

    assert result.status == AdapterFetchStatus.success
    assert calls == ["600519"]
    row = result.raw_records[0]
    assert row["normalized_ticker"] == "600519.SH"
    assert row["name"] == "贵州茅台"
    assert row["company_full_name"] == "贵州茅台酒股份有限公司"
    assert row["industry"] == "白酒"
    assert row["list_date"] == "20010827"
    assert row["total_share"] == 1256197800.0


def test_minute_historical_bars_use_stock_zh_a_hist_min_em(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {}

    def stock_zh_a_hist(**kwargs):  # pragma: no cover - should not be called
        raise AssertionError("daily endpoint must not be used for minute requests")

    def stock_zh_a_hist_min_em(**kwargs):
        called.update(kwargs)
        return FakeDF([{"时间": "2025-01-02 09:31:00", "开盘": 1.0, "最高": 1.2, "最低": 0.9, "收盘": 1.1, "成交量": 10, "成交额": 1000.0}])

    fake = types.SimpleNamespace(stock_zh_a_hist=stock_zh_a_hist, stock_zh_a_hist_min_em=stock_zh_a_hist_min_em)
    install_fake_ak(monkeypatch, fake)

    result = AKShareAdapter().fetch_historical_bars(
        request(RequestType.historical_bars, tickers=["600519.SH"], start_date="20250102", end_date="20250102", frequency=Frequency.m1)
    )

    assert result.status == AdapterFetchStatus.success
    assert called["symbol"] == "600519"
    assert called["period"] == "1"
    assert result.raw_records[0]["frequency"] == "1m"
    assert result.raw_records[0]["trade_date"] == "20250102"


def test_financial_statement_and_indicator_use_documented_symbol_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"statement": [], "indicator_em": []}

    def profit(symbol):
        calls["statement"].append(symbol)
        return FakeDF([{"REPORT_DATE": "2025-03-31", "NOTICE_DATE": "2025-04-30", "OPERATE_INCOME": 1.0}])

    fake = types.SimpleNamespace(
        stock_profit_sheet_by_report_em=profit,
        stock_balance_sheet_by_report_em=lambda symbol: FakeDF([]),
        stock_cash_flow_sheet_by_report_em=lambda symbol: FakeDF([]),
        stock_financial_analysis_indicator=lambda symbol, start_year=None: FakeDF([]),
        stock_financial_analysis_indicator_em=lambda **kwargs: calls["indicator_em"].append(kwargs)
        or FakeDF([{"REPORT_DATE": "2025-03-31", "EPSJB": 1.23, "ROEJQ": 10.0}]),
    )
    install_fake_ak(monkeypatch, fake)

    statement = AKShareAdapter().fetch_financial_statement(
        request(RequestType.financial_statement, tickers=["600519.SH"], start_date="20250101", end_date="20251231", extra_params={"statement_types": ["income"]})
    )
    indicator = AKShareAdapter().fetch_financial_indicator(
        request(RequestType.financial_indicator, tickers=["600519.SH"], start_date="20250101", end_date="20251231")
    )

    assert statement.status == AdapterFetchStatus.success
    assert calls["statement"] == ["SH600519"]
    assert indicator.status == AdapterFetchStatus.success
    assert calls["indicator_em"][0]["symbol"] == "600519.SH"
    assert calls["indicator_em"][0]["indicator"] == "按报告期"


def test_valuation_money_flow_and_corporate_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"fund": [], "dividend": [], "repurchase": 0}

    fake = types.SimpleNamespace(
        stock_value_em=lambda symbol: FakeDF([{"数据日期": "2025-01-02", "当日收盘价": 10.0, "总市值": 100000.0, "PE(TTM)": 8.0, "市净率": 1.1}]),
        stock_zh_a_spot_em=lambda: FakeDF([]),
        stock_individual_fund_flow=lambda stock, market: calls["fund"].append((stock, market))
        or FakeDF([{"日期": "2025-01-02", "主力净流入-净额": 123.0, "主力净流入-净占比": 1.5}]),
        stock_history_dividend_detail=lambda symbol, indicator: calls["dividend"].append((symbol, indicator))
        or FakeDF([{"公告日期": "2025-01-10", "送股": 0, "转增": 0, "派息": 10.0, "股权登记日": "2025-01-20", "除权除息日": "2025-01-21"}]),
        stock_dividend_cninfo=lambda symbol: FakeDF([]),
        stock_repurchase_em=lambda: calls.__setitem__("repurchase", calls["repurchase"] + 1)
        or FakeDF([{"股票代码": "600519", "最新公告日期": "2025-02-01"}]),
    )
    install_fake_ak(monkeypatch, fake)

    adapter = AKShareAdapter()
    valuation = adapter.fetch_valuation_metric(request(RequestType.valuation_metric, tickers=["600519.SH"], start_date="20250101", end_date="20251231"))
    money_flow = adapter.fetch_money_flow(request(RequestType.money_flow, tickers=["600519.SH"], start_date="20250101", end_date="20251231"))
    action = adapter.fetch_corporate_action(
        request(RequestType.corporate_action, tickers=["600519.SH"], start_date="20250101", end_date="20251231", extra_params={"action_types": ["dividend", "repurchase"]})
    )

    assert valuation.status == AdapterFetchStatus.success
    assert valuation.raw_records[0]["pe_ttm"] == 8.0
    assert calls["fund"] == [("600519", "sh")]
    assert money_flow.raw_records[0]["main_net_inflow"] == 123.0
    assert ("600519", "分红") in calls["dividend"]
    assert calls["repurchase"] == 1
    assert {r["action_type"] for r in action.raw_records} == {"dividend", "repurchase"}


def test_industry_and_concept_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(
        stock_board_industry_name_em=lambda: FakeDF([{"板块代码": "BK1", "板块名称": "白酒"}]),
        stock_board_industry_cons_em=lambda symbol: FakeDF([{"代码": "600519", "名称": "贵州茅台"}]),
        stock_board_concept_name_em=lambda: FakeDF([{"板块代码": "BK2", "板块名称": "国企改革"}]),
        stock_board_concept_cons_em=lambda symbol: FakeDF([{"代码": "600519", "名称": "贵州茅台"}]),
    )
    install_fake_ak(monkeypatch, fake)

    result = AKShareAdapter().fetch_industry_membership(
        request(RequestType.industry_concept, tickers=["600519.SH"], extra_params={"akshare_max_boards": 10})
    )

    assert result.status == AdapterFetchStatus.success
    assert any(r.get("industry_name") == "白酒" for r in result.raw_records)
    assert any(r.get("concept_name") == "国企改革" for r in result.raw_records)
