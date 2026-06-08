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



def test_daily_historical_bars_use_eastmoney_default_first(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"tx": [], "em": 0}

    def stock_zh_a_hist(**kwargs):
        calls["em"] += 1
        return FakeDF([{"日期": "2025-01-02", "开盘": 11.73, "收盘": 11.43, "最高": 11.77, "最低": 11.39, "成交量": 1819597, "成交额": 207000000.0}])

    def stock_zh_a_hist_tx(**kwargs):
        calls["tx"].append(kwargs)
        raise AssertionError("Tencent fallback should not be used when Eastmoney succeeds")

    fake = types.SimpleNamespace(stock_zh_a_hist=stock_zh_a_hist, stock_zh_a_hist_tx=stock_zh_a_hist_tx)
    install_fake_ak(monkeypatch, fake)

    result = AKShareAdapter().fetch_historical_bars(
        request(RequestType.historical_bars, tickers=["000001.SZ"], start_date="20250101", end_date="20250630", frequency=Frequency.d1)
    )

    assert result.status == AdapterFetchStatus.success
    assert result.source_api == "stock_zh_a_hist"
    assert calls["em"] == 1
    assert calls["tx"] == []
    row = result.raw_records[0]
    assert row["normalized_ticker"] == "000001.SZ"
    assert row["provider_symbol"] == "000001"
    assert row["日期"] == "2025-01-02"
    assert row["开盘"] == 11.73
    assert row["收盘"] == 11.43
    assert row["raw_source_api"] == "stock_zh_a_hist"


def test_daily_historical_bars_fallback_to_tencent_when_eastmoney_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"tx": [], "em": 0}

    def stock_zh_a_hist(**kwargs):
        calls["em"] += 1
        raise ConnectionError("eastmoney disconnected")

    def stock_zh_a_hist_tx(**kwargs):
        calls["tx"].append(kwargs)
        return FakeDF([{"date": "2025-01-02", "open": 11.73, "close": 11.43, "high": 11.77, "low": 11.39, "amount": 1819597}])

    fake = types.SimpleNamespace(stock_zh_a_hist=stock_zh_a_hist, stock_zh_a_hist_tx=stock_zh_a_hist_tx)
    install_fake_ak(monkeypatch, fake)

    result = AKShareAdapter().fetch_historical_bars(
        request(RequestType.historical_bars, tickers=["000001.SZ"], start_date="20250101", end_date="20250630", frequency=Frequency.d1)
    )

    assert result.status == AdapterFetchStatus.success
    assert result.source_api == "stock_zh_a_hist+stock_zh_a_hist_tx"
    assert calls["em"] == 1
    assert calls["tx"] == [{"symbol": "sz000001", "start_date": "20250101", "end_date": "20250630", "adjust": ""}]
    row = result.raw_records[0]
    assert row["normalized_ticker"] == "000001.SZ"
    assert row["provider_symbol"] == "sz000001"
    assert row["volume_unit"] == "hand"
    assert row["amount_is_estimated"] is True
    assert row["raw_source_api"] == "stock_zh_a_hist_tx"

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
    monkeypatch.delenv("EASTMONEY_COOKIE", raising=False)
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


def test_money_flow_with_cookie_still_uses_akshare_default_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EASTMONEY_COOKIE", "wsc_checkuser_ok=1; nid18=test")
    calls = {"fund": []}

    fake = types.SimpleNamespace(
        stock_individual_fund_flow=lambda stock, market: calls["fund"].append((stock, market))
        or FakeDF([{"日期": "2026-06-05", "主力净流入-净额": 123.0, "主力净流入-净占比": 1.5}])
    )
    install_fake_ak(monkeypatch, fake)

    result = AKShareAdapter().fetch_money_flow(
        request(RequestType.money_flow, tickers=["600519.SH"], start_date="20260601", end_date="20260605")
    )

    assert result.status == AdapterFetchStatus.success
    assert result.source_api == "stock_individual_fund_flow"
    assert calls["fund"] == [("600519", "sh")]
    assert result.raw_records[0]["main_net_inflow"] == 123.0


def test_money_flow_fallbacks_to_direct_eastmoney_when_akshare_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EASTMONEY_COOKIE", "wsc_checkuser_ok=1; nid18=test")
    calls = []

    fake = types.SimpleNamespace(stock_individual_fund_flow=lambda stock, market: (_ for _ in ()).throw(ConnectionError("akshare disconnected")))
    install_fake_ak(monkeypatch, fake)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "data": {
                    "klines": [
                        "2026-06-05,-25488.21,-186.58,-7214.31,-12653.98,20054.87,-1.1,-0.01,-0.3,-0.5,0.8,1400.0,-1.2",
                        "2026-05-30,1,2,3,4,5,6,7,8,9,10,11,12",
                    ]
                }
            }

    fake_requests = types.SimpleNamespace(get=lambda url, **kwargs: calls.append((url, kwargs)) or FakeResponse())
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    result = AKShareAdapter().fetch_money_flow(
        request(RequestType.money_flow, tickers=["600519.SH"], start_date="20260601", end_date="20260605")
    )

    assert result.status == AdapterFetchStatus.success
    assert result.source_api == "stock_individual_fund_flow+eastmoney_fflow_daykline"
    assert len(result.raw_records) == 1
    assert result.raw_records[0]["trade_date"] == "20260605"
    assert result.raw_records[0]["main_net_inflow"] == -25488.21
    assert result.raw_records[0]["super_large_net_inflow"] == 20054.87
    assert result.raw_records[0]["source_methodology"] == "eastmoney_fflow_daykline_browser_cookie"
    assert calls[0][1]["params"]["secid"] == "1.600519"
    assert "wsc_checkuser_ok=1" in calls[0][1]["headers"]["Cookie"]


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


def test_security_master_uses_exchange_lists_when_code_name_wrapper_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def stock_info_a_code_name():
        raise TimeoutError("szse wrapper timeout")

    fake = types.SimpleNamespace(
        stock_info_a_code_name=stock_info_a_code_name,
        stock_info_sh_name_code=lambda symbol="主板A股": FakeDF(
            [{"证券代码": "600519", "证券简称": "贵州茅台", "公司全称": "贵州茅台酒股份有限公司", "上市日期": "2001-08-27"}]
            if symbol == "主板A股"
            else []
        ),
        stock_info_sz_name_code=lambda symbol="A股列表": FakeDF([]),
        stock_info_bj_name_code=lambda: FakeDF([]),
        stock_individual_info_em=lambda symbol: FakeDF([]),
    )
    install_fake_ak(monkeypatch, fake)

    result = AKShareAdapter().fetch_security_master(request(RequestType.security_master, tickers=["600519.SH"]))

    assert result.status == AdapterFetchStatus.success
    assert len(result.raw_records) == 1
    row = result.raw_records[0]
    assert row["normalized_ticker"] == "600519.SH"
    assert row["name"] == "贵州茅台"
    assert row["company_full_name"] == "贵州茅台酒股份有限公司"
    assert row["security_master_primary_source"] == "exchange_stock_lists"
    assert "stock_info_a_code_name failed" in row["code_name_warning"]


def test_realtime_still_uses_eastmoney_when_no_tencent_all_a_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"spot": 0}

    def spot():
        called["spot"] += 1
        return FakeDF([{"代码": "600519", "名称": "贵州茅台", "最新价": 1500, "成交量": 10, "成交额": 1000}])

    fake = types.SimpleNamespace(stock_zh_a_spot_em=spot)
    install_fake_ak(monkeypatch, fake)

    result = AKShareAdapter().fetch_realtime_quote(request(RequestType.realtime_quote, tickers=["600519.SH"]))

    assert result.status == AdapterFetchStatus.success
    assert called["spot"] == 1
    assert result.raw_records[0]["raw_source_api"] == "stock_zh_a_spot_em"
