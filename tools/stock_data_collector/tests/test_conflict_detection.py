from __future__ import annotations

from stock_data_ingestion.schemas.quality import ConflictSeverity
from stock_data_ingestion.validation.comparison import compare_bar_records
from stock_data_ingestion.validation.conflict import detect_field_conflict


def test_close_consistency_within_tolerance(bar_factory):
    canonical = bar_factory(provider="tushare", close=10.0000)
    akshare = bar_factory(provider="akshare", close=10.0005)
    cmp = compare_bar_records(canonical, akshare)
    assert cmp.status == "matched"
    assert "close" in cmp.matched_fields


def test_close_conflict_generates_high_conflict(bar_factory):
    canonical = bar_factory(provider="tushare", close=10.0)
    akshare = bar_factory(provider="akshare", close=10.5)
    cmp = compare_bar_records(canonical, akshare)
    assert cmp.status == "conflicted"
    close_conflict = [c for c in cmp.conflicts if c.field_name == "close"][0]
    assert close_conflict.severity in {ConflictSeverity.high, ConflictSeverity.critical}


def test_critical_boolean_status_conflict():
    conflict = detect_field_conflict(
        record_type="trading_status",
        comparison_key="600519.SH|2026-05-29",
        field_name="is_suspended",
        canonical_provider="tushare",
        canonical_value=False,
        other_provider="akshare",
        other_value=True,
    )
    assert conflict is not None
    assert conflict.severity == ConflictSeverity.critical
