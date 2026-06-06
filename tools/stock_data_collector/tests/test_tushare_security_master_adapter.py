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
        self.calls: list[dict[str, Any]] = []

    def stock_basic(self, **kwargs: Any) -> FakeDataFrame:
        self.calls.append(kwargs)
        ts_code = kwargs.get("ts_code")
        list_status = kwargs.get("list_status")

        if ts_code == "600519.SH":
            return FakeDataFrame(
                [
                    {
                        "ts_code": "600519.SH",
                        "symbol": "600519",
                        "name": "贵州茅台",
                        "market": "主板",
                        "exchange": "SSE",
                        "list_status": "L",
                    }
                ]
            )
        if ts_code == "000001.SZ":
            return FakeDataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "symbol": "000001",
                        "name": "平安银行",
                        "market": "主板",
                        "exchange": "SZSE",
                        "list_status": "L",
                    }
                ]
            )

        if list_status == "L":
            return FakeDataFrame(
                [
                    {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台", "list_status": "L"},
                    {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "list_status": "L"},
                ]
            )
        if list_status == "D":
            return FakeDataFrame(
                [
                    {
                        "ts_code": "TS0018.SH",
                        "symbol": "TS0018",
                        "name": "上港集箱(退)",
                        "list_status": "D",
                    }
                ]
            )
        return FakeDataFrame([])


def _adapter_with_fake_pro(fake_pro: FakeTusharePro) -> TushareAdapter:
    adapter = TushareAdapter()
    adapter.token = "fake-token"
    adapter._pro = fake_pro
    adapter.is_available = lambda: True  # type: ignore[method-assign]
    adapter.authenticate = lambda: True  # type: ignore[method-assign]
    return adapter


def test_security_master_targeted_tickers_do_not_scan_delisted_universe() -> None:
    fake_pro = FakeTusharePro()
    adapter = _adapter_with_fake_pro(fake_pro)
    request = StockDataRequest(
        request_id="req_targeted_security_master",
        request_type="security_master",
        tickers=["600519.SH", "000001.SZ"],
        export_parquet=False,
    )

    result = adapter.fetch_security_master(request)

    assert result.status == AdapterFetchStatus.success
    assert {row["ts_code"] for row in result.raw_records} == {"600519.SH", "000001.SZ"}
    assert all(call.get("ts_code") in {"600519.SH", "000001.SZ"} for call in fake_pro.calls)
    assert all(call.get("list_status") != "D" for call in fake_pro.calls)


def test_security_master_full_universe_preserves_nonstandard_delisted_raw_rows() -> None:
    fake_pro = FakeTusharePro()
    adapter = _adapter_with_fake_pro(fake_pro)
    request = StockDataRequest(
        request_id="req_full_security_master",
        request_type="security_master",
        tickers=[],
        export_parquet=False,
    )

    result = adapter.fetch_security_master(request)

    assert result.status == AdapterFetchStatus.success
    assert any(row["ts_code"] == "TS0018.SH" for row in result.raw_records)
    assert any(call.get("list_status") == "D" for call in fake_pro.calls)
