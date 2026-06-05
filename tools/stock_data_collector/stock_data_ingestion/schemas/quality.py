from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai


class ValidationStatus(StrEnum):
    unvalidated = "unvalidated"
    validated = "validated"
    validated_with_warning = "validated_with_warning"
    conflicted_low = "conflicted_low"
    conflicted_medium = "conflicted_medium"
    conflicted_high = "conflicted_high"
    quarantined = "quarantined"
    manual_review_required = "manual_review_required"
    failed = "failed"


class MergeMethod(StrEnum):
    canonical_only = "canonical_only"
    canonical_validated = "canonical_validated"
    canonical_with_warning = "canonical_with_warning"
    fill_missing_from_supplement = "fill_missing_from_supplement"
    fallback_single_source = "fallback_single_source"
    fallback_multi_source_agreed = "fallback_multi_source_agreed"
    provider_specific_append = "provider_specific_append"
    quarantined_due_to_conflict = "quarantined_due_to_conflict"
    manual_review_required = "manual_review_required"


class SourceRole(StrEnum):
    canonical = "canonical"
    validator = "validator"
    supplement = "supplement"
    fallback_canonical = "fallback_canonical"
    provider_specific = "provider_specific"


class ConflictSeverity(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class DataQualityConflict(BaseModel):
    conflict_id: str = Field(default_factory=lambda: f"conf_{uuid4().hex}")
    record_type: str
    comparison_key: str
    field_name: str
    canonical_provider: str = "tushare"
    canonical_value: Any = None
    other_provider: str
    other_value: Any = None
    severity: ConflictSeverity
    tolerance: Optional[dict[str, Any]] = None
    reason: str
    resolution: Optional[str] = None
    created_at: datetime = Field(default_factory=now_asia_shanghai)
    request_id: Optional[str] = None
    ingestion_run_id: Optional[str] = None
    canonical_record_id: Optional[str] = None
    other_record_id: Optional[str] = None


class QualityScore(BaseModel):
    completeness_score: float = 0.0
    consistency_score: float = 0.0
    timeliness_score: float = 0.0
    provider_reliability_score: float = 0.0
    anomaly_score: float = 0.0
    provenance_score: float = 0.0
    data_quality_score: float = 0.0

    @field_validator("completeness_score", "consistency_score", "timeliness_score", "provider_reliability_score", "anomaly_score", "provenance_score", "data_quality_score")
    @classmethod
    def clamp_score(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


class QualityReport(QualityScore):
    cross_provider_checks: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[DataQualityConflict] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
