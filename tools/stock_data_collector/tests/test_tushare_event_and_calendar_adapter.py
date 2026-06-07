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


class FakeTusharePro:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def trade_cal(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("trade_cal", kwargs))
        assert kwargs.get("exchange") == "SSE"
        return FakeDataFrame(
            [
                {
                    "exchange": "SSE",
                    "cal_date": "20250630",
                    "is_open": 1,
                    "pretrade_date": "20250627",
                }
            ]
        )

    def dividend(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("dividend", kwargs))
        assert "start_date" not in kwargs
        assert "end_date" not in kwargs
        return FakeDataFrame(
            [
                {
                    "ts_code": "600519.SH",
                    "end_date": "20240630",
                    "ann_date": "20240809",
                    "record_date": "20240918",
                    "ex_date": "20240919",
                    "pay_date": "20240919",
                    "cash_div_tax": 30.876,
                    "stk_div": 0.0,
                },
                {
                    "ts_code": "600519.SH",
                    "end_date": "20250630",
                    "ann_date": "20250809",
                    "record_date": "20250918",
                    "ex_date": "20250919",
                    "pay_date": "20250919",
                    "cash_div_tax": 31.23,
                    "stk_div": 0.1,
                },
            ]
        )

    def share_float(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("share_float", kwargs))
        start_date = kwargs.get("start_date")
        if start_date == "19000101":
            return FakeDataFrame(
                [
                    {
                        "ts_code": "600519.SH",
                        "ann_date": "20010101",
                        "float_date": "20020101",
                        "float_share": 1000.0,
                        "float_ratio": 0.1,
                        "holder_name": "holder",
                        "share_type": "首发原股东限售股份",
                    }
                ]
            )
        return FakeDataFrame([])


    def stk_limit(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("stk_limit", kwargs))
        # Existing event-empty test should remain non-error when neither limit
        # prices nor suspend/resume events exist in the requested range.
        return FakeDataFrame([])

    def suspend_d(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(("suspend_d", kwargs))
        return FakeDataFrame([])


def _adapter(fake_pro: FakeTusharePro) -> TushareAdapter:
    adapter = TushareAdapter()
    adapter.token = "fake-token"
    adapter._pro = fake_pro
    adapter.is_available = lambda: True  # type: ignore[method-assign]
    adapter.authenticate = lambda: True  # type: ignore[method-assign]
    return adapter


def test_trade_calendar_bse_is_derived_from_sse() -> None:
    fake = FakeTusharePro()
    adapter = _adapter(fake)
    request = StockDataRequest(
        request_id="req_bse_calendar",
        request_type="trade_calendar",
        exchanges=["BSE"],
        start_date="20250630",
        end_date="20250630",
        export_parquet=False,
    )

    result = adapter.fetch_trade_calendar(request)

    assert result.status == AdapterFetchStatus.success
    assert result.raw_records[0]["exchange"] == "BSE"
    assert result.raw_records[0]["provider_exchange"] == "SSE"
    assert result.raw_records[0]["calendar_derivation"] == "derived_from_sse_trade_calendar"
    assert fake.calls[0][1]["exchange"] == "SSE"


def test_dividend_queries_ts_code_only_and_filters_locally() -> None:
    fake = FakeTusharePro()
    adapter = _adapter(fake)
    request = StockDataRequest(
        request_id="req_dividend_filter",
        request_type="corporate_action",
        tickers=["600519.SH"],
        start_date="20250101",
        end_date="20251231",
        extra_params={"action_types": ["dividend"]},
        export_parquet=False,
    )

    result = adapter.fetch_corporate_action(request)

    assert result.status == AdapterFetchStatus.success
    assert len(result.raw_records) == 1
    row = result.raw_records[0]
    assert row["action_type"] == "dividend"
    assert row["ann_date"] == "20250809"
    assert row["cash_dividend_per_share"] == 31.23
    assert row["stock_bonus_ratio"] == 0.1
    dividend_call = [kwargs for name, kwargs in fake.calls if name == "dividend"][0]
    assert dividend_call["ts_code"] == "600519.SH"
    assert "start_date" not in dividend_call
    assert "end_date" not in dividend_call


def test_event_empty_result_has_no_error_for_sparse_share_float() -> None:
    fake = FakeTusharePro()
    adapter = _adapter(fake)
    request = StockDataRequest(
        request_id="req_share_float_empty",
        request_type="corporate_action",
        tickers=["600519.SH"],
        start_date="20250101",
        end_date="20251231",
        extra_params={"action_types": ["share_float"]},
        export_parquet=False,
    )

    result = adapter.fetch_corporate_action(request)

    assert result.status == AdapterFetchStatus.empty_result
    assert result.error is None
    assert result.raw_records == []


def test_corporate_action_defaults_to_full_history_when_dates_omitted() -> None:
    fake = FakeTusharePro()
    adapter = _adapter(fake)
    request = StockDataRequest(
        request_id="req_corporate_full_history_default",
        request_type="corporate_action",
        tickers=["600519.SH"],
        extra_params={"action_types": ["share_float"]},
        export_parquet=False,
    )

    result = adapter.fetch_corporate_action(request)

    assert result.status == AdapterFetchStatus.success
    assert len(result.raw_records) == 1
    share_float_call = [kwargs for name, kwargs in fake.calls if name == "share_float"][0]
    assert share_float_call["start_date"] == "19000101"
    assert share_float_call["end_date"] >= "20260607"


def test_suspend_empty_result_has_no_error() -> None:
    fake = FakeTusharePro()
    adapter = _adapter(fake)
    request = StockDataRequest(
        request_id="req_suspend_empty",
        request_type="trading_status",
        tickers=["600519.SH"],
        start_date="20250101",
        end_date="20251231",
        export_parquet=False,
    )

    result = adapter.fetch_trading_status(request)

    assert result.status == AdapterFetchStatus.empty_result
    assert result.error is None
    assert result.raw_records == []
