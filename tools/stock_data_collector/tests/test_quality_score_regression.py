from __future__ import annotations

from stock_data_ingestion.schemas.quality import ConflictSeverity, DataQualityConflict
from stock_data_ingestion.validation.quality_score import score_record


def _conflict(severity):
    return DataQualityConflict(record_type="bar", comparison_key="k", field_name="close", other_provider="akshare", severity=severity, reason="test")


def test_quality_score_uses_business_severity_order_not_string_order():
    q = score_record(
        provider="tushare",
        required_fields=["close"],
        record_values={"close": 10},
        field_provenance={"close": {"provider": "tushare"}},
        raw_payload_ref="raw://local/x",
        merge_method="canonical_only",
        conflicts=[_conflict(ConflictSeverity.critical), _conflict(ConflictSeverity.high)],
    )
    assert q.consistency_score == 0.10
