from __future__ import annotations

from pydantic import BaseModel

from stock_data_ingestion.validation.comparison import compare_standard_records


class ComparableRecord(BaseModel):
    record_type: str = "security_master"
    provider: str
    normalized_ticker: str = "600519.SH"
    record_id: str = "rec"
    request_id: str = "req"
    ingestion_run_id: str = "run"
    name: str | None = None
    company_full_name: str | None = None
    industry: str | None = None
    total_share: float | None = None


def test_missing_provider_fields_are_not_conflicts_or_matches() -> None:
    canonical = ComparableRecord(provider="tushare", name="贵州茅台", company_full_name="贵州茅台酒股份有限公司", industry="白酒", total_share=None)
    akshare = ComparableRecord(provider="akshare", name="贵州茅台", company_full_name=None, industry=None, total_share=None)

    result = compare_standard_records(canonical, akshare, ["name", "company_full_name", "industry", "total_share"])

    assert result.status == "matched"
    assert result.checked_fields == ["name"]
    assert result.matched_fields == ["name"]
    assert result.conflicted_fields == []
    assert result.conflicts == []



class ComparableBar(BaseModel):
    record_type: str = "bar"
    provider: str
    source_api: str = ""
    normalized_ticker: str = "000001.SZ"
    frequency: str = "1d"
    timestamp: str = "2025-01-02T00:00:00"
    adjust: str = "none"
    record_id: str = "rec"
    request_id: str = "req"
    ingestion_run_id: str = "run"
    close: float
    amount: float
    vwap: float | None = None


def test_tencent_hist_estimated_amount_is_not_compared() -> None:
    canonical = ComparableBar(provider="tushare", source_api="daily", close=11.43, amount=2_102_923_078.11, vwap=11.55)
    tencent = ComparableBar(provider="akshare", source_api="stock_zh_a_hist_tx", close=11.43, amount=2_081_000_000.00, vwap=11.44)

    result = compare_standard_records(canonical, tencent, ["close", "amount", "vwap"])

    assert result.status == "matched"
    assert result.checked_fields == ["close"]
    assert result.matched_fields == ["close"]
    assert result.conflicted_fields == []
