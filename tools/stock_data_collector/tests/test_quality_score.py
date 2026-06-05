from __future__ import annotations

from stock_data_ingestion.schemas.quality import ConflictSeverity, DataQualityConflict
from stock_data_ingestion.validation.quality_score import apply_quality_adjustments, score_record, weighted_quality_score


def test_quality_formula_and_clamping():
    q = weighted_quality_score(
        completeness_score=1.0,
        consistency_score=1.0,
        timeliness_score=1.0,
        provider_reliability_score=0.9,
        anomaly_score=1.0,
        provenance_score=1.0,
    )
    assert 0.98 <= q.data_quality_score <= 1.0
    assert apply_quality_adjustments(0.9, ["critical_conflict", "missing_field_provenance"]) == 0.2


def test_quality_deductions_for_conflict_and_missing_provenance():
    conflict = DataQualityConflict(
        record_type="bar",
        comparison_key="k",
        field_name="close",
        canonical_provider="tushare",
        canonical_value=10,
        other_provider="akshare",
        other_value=12,
        severity=ConflictSeverity.high,
        reason="test",
    )
    q = score_record(
        provider="akshare",
        required_fields=["open", "high", "low", "close", "volume", "amount"],
        record_values={"open": 1, "high": 2, "low": 1, "close": 1, "volume": 1, "amount": 1},
        field_provenance={},
        raw_payload_ref=None,
        merge_method="fallback_single_source",
        conflicts=[conflict],
    )
    assert q.data_quality_score < 0.5
