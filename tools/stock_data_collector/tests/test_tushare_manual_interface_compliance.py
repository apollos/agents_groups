from __future__ import annotations

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


class ManualComplianceFakePro:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def stock_basic(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("stock_basic", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs.get("ts_code", "600519.SH"),
                    "symbol": "600519",
                    "name": "贵州茅台",
                    "fullname": None,
                    "exchange": "SSE",
                    "market": "主板",
                    "list_status": "L",
                    "list_date": "20010827",
                }
            ]
        )

    def stock_company(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("stock_company", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "com_name": "贵州茅台酒股份有限公司",
                    "exchange": "SSE",
                    "province": "贵州",
                    "main_business": "茅台酒及系列酒生产销售",
                }
            ]
        )

    def stk_limit(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("stk_limit", kwargs))
        return FakeDataFrame(
            [
                {
                    "trade_date": "20250630",
                    "ts_code": kwargs["ts_code"],
                    "pre_close": 100.0,
                    "up_limit": 110.0,
                    "down_limit": 90.0,
                }
            ]
        )

    def suspend_d(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("suspend_d", kwargs))
        return FakeDataFrame([])

    def income(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("income", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "ann_date": "20250425",
                    "end_date": "20250331",
                    "report_type": "1",
                    "revenue": 1000.0,
                    "operate_profit": 300.0,
                    "n_income": 220.0,
                    "n_income_attr_p": 200.0,
                }
            ]
        )

    def balancesheet(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("balancesheet", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "ann_date": "20250425",
                    "end_date": "20250331",
                    "report_type": "1",
                    "total_assets": 9000.0,
                    "total_liab": 3000.0,
                    "total_hldr_eqy_exc_min_int": 5800.0,
                }
            ]
        )

    def cashflow(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("cashflow", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "ann_date": "20250425",
                    "end_date": "20250331",
                    "report_type": "1",
                    "n_cashflow_act": 180.0,
                    "free_cashflow": 150.0,
                }
            ]
        )

    def moneyflow(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("moneyflow", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "trade_date": "20250630",
                    "buy_elg_amount": 12.0,
                    "sell_elg_amount": 5.0,
                    "buy_lg_amount": 6.0,
                    "sell_lg_amount": 4.0,
                    "buy_md_amount": 3.0,
                    "sell_md_amount": 2.0,
                    "buy_sm_amount": 1.0,
                    "sell_sm_amount": 0.5,
                    "net_mf_amount": 10.5,
                }
            ]
        )

    def daily_basic(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("daily_basic", kwargs))
        return FakeDataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "trade_date": "20250630",
                    "pe": 20.0,
                    "pb": 5.0,
                    "total_share": 100.0,
                    "float_share": 90.0,
                    "total_mv": 2000.0,
                    "circ_mv": 1800.0,
                }
            ]
        )

    def repurchase(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("repurchase", kwargs))
        return FakeDataFrame(
            [
                {"ts_code": "600519.SH", "ann_date": "20250115", "end_date": "20250630", "proc": "实施", "vol": 1.0, "amount": 2.0},
                {"ts_code": "000001.SZ", "ann_date": "20250115", "end_date": "20250630", "proc": "实施", "vol": 3.0, "amount": 4.0},
            ]
        )


class MinimalFakePro(ManualComplianceFakePro):
    pass


def _adapter(fake_pro: ManualComplianceFakePro) -> TushareAdapter:
    adapter = TushareAdapter()
    adapter.token = "fake-token"
    adapter._pro = fake_pro
    adapter.is_available = lambda: True  # type: ignore[method-assign]
    adapter.authenticate = lambda: True  # type: ignore[method-assign]
    return adapter


def _request(request_type: str, **kwargs: Any) -> StockDataRequest:
    return StockDataRequest(
        request_id=f"req_{request_type}",
        request_type=request_type,
        tickers=["600519.SH"],
        start_date="20250101",
        end_date="20251231",
        export_parquet=False,
        **kwargs,
    )


def test_security_master_uses_stock_basic_and_stock_company_documented_params() -> None:
    fake = ManualComplianceFakePro()
    result = _adapter(fake).fetch_security_master(_request("security_master"))

    assert result.status == AdapterFetchStatus.success
    assert result.raw_records[0]["company_full_name"] == "贵州茅台酒股份有限公司"
    assert result.raw_records[0]["main_business"] == "茅台酒及系列酒生产销售"
    stock_basic_call = [kwargs for name, kwargs in fake.calls if name == "stock_basic"][0]
    stock_company_call = [kwargs for name, kwargs in fake.calls if name == "stock_company"][0]
    assert stock_basic_call["ts_code"] == "600519.SH"
    assert "start_date" not in stock_basic_call
    assert stock_company_call["ts_code"] == "600519.SH"
    assert "exchange" not in stock_company_call


def test_trading_status_uses_stk_limit_and_suspend_d_documented_params() -> None:
    fake = ManualComplianceFakePro()
    result = _adapter(fake).fetch_trading_status(_request("trading_status"))

    assert result.status == AdapterFetchStatus.success
    row = result.raw_records[0]
    assert row["limit_up_price"] == 110.0
    assert row["limit_down_price"] == 90.0
    calls = {name: kwargs for name, kwargs in fake.calls}
    assert calls["stk_limit"]["ts_code"] == "600519.SH"
    assert calls["stk_limit"]["start_date"] == "20250101"
    assert calls["stk_limit"]["end_date"] == "20251231"
    assert calls["suspend_d"]["ts_code"] == "600519.SH"
    assert calls["suspend_d"]["start_date"] == "20250101"
    assert calls["suspend_d"]["end_date"] == "20251231"


def test_financial_statement_calls_income_balancesheet_cashflow_and_decorates_rows() -> None:
    fake = ManualComplianceFakePro()
    result = _adapter(fake).fetch_financial_statement(_request("financial_statement"))

    assert result.status == AdapterFetchStatus.success
    assert {row["statement_type"] for row in result.raw_records} == {"income_statement", "balance_sheet", "cash_flow"}
    income_row = next(row for row in result.raw_records if row["statement_type"] == "income_statement")
    balance_row = next(row for row in result.raw_records if row["statement_type"] == "balance_sheet")
    cashflow_row = next(row for row in result.raw_records if row["statement_type"] == "cash_flow")
    assert income_row["operating_revenue"] == 1000.0
    assert balance_row["total_liabilities"] == 3000.0
    assert cashflow_row["operating_cash_flow"] == 180.0
    for name, kwargs in fake.calls:
        if name in {"income", "balancesheet", "cashflow"}:
            assert kwargs["ts_code"] == "600519.SH"
            assert kwargs["start_date"] == "20250101"
            assert kwargs["end_date"] == "20251231"


def test_money_flow_uses_moneyflow_and_computes_net_buckets() -> None:
    fake = ManualComplianceFakePro()
    result = _adapter(fake).fetch_money_flow(_request("money_flow"))

    assert result.status == AdapterFetchStatus.success
    row = result.raw_records[0]
    assert row["super_large_net_inflow"] == 7.0
    assert row["large_net_inflow"] == 2.0
    assert row["medium_net_inflow"] == 1.0
    assert row["small_net_inflow"] == 0.5
    call = [kwargs for name, kwargs in fake.calls if name == "moneyflow"][0]
    assert call["ts_code"] == "600519.SH"
    assert call["start_date"] == "20250101"
    assert call["end_date"] == "20251231"


def test_valuation_metric_uses_daily_basic_documented_fields() -> None:
    fake = ManualComplianceFakePro()
    result = _adapter(fake).fetch_valuation_metric(_request("valuation_metric"))

    assert result.status == AdapterFetchStatus.success
    call = [kwargs for name, kwargs in fake.calls if name == "daily_basic"][0]
    assert call["ts_code"] == "600519.SH"
    assert call["start_date"] == "20250101"
    assert call["end_date"] == "20251231"
    assert "total_mv" in call["fields"]
    assert "circ_mv" in call["fields"]


def test_repurchase_uses_documented_announcement_window_and_filters_tickers() -> None:
    fake = ManualComplianceFakePro()
    result = _adapter(fake).fetch_corporate_action(
        _request("corporate_action", extra_params={"action_types": ["repurchase"]})
    )

    assert result.status == AdapterFetchStatus.success
    assert [row["ts_code"] for row in result.raw_records] == ["600519.SH"]
    call = [kwargs for name, kwargs in fake.calls if name == "repurchase"][0]
    assert call["start_date"] == "20250101"
    assert call["end_date"] == "20251231"
    assert "ts_code" not in call
