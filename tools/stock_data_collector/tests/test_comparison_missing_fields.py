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
