from __future__ import annotations

from typing import Any, Iterable

from stock_data_ingestion.schemas.quality import ConflictSeverity, QualityScore

DEFAULT_PROVIDER_RELIABILITY = {
    "tushare": 0.90,
    "joinquant": 0.85,
    "akshare": 0.75,
    "manual_import": 0.60,
    "unknown": 0.30,
}

DEFAULT_WEIGHTS = {
    "completeness_score": 0.25,
    "consistency_score": 0.30,
    "timeliness_score": 0.15,
    "provider_reliability_score": 0.10,
    "anomaly_score": 0.10,
    "provenance_score": 0.10,
}

ADJUSTMENTS = {
    "canonical_validated_by_two_sources": 0.08,
    "canonical_validated_by_one_source": 0.04,
    "canonical_only_not_validated": -0.05,
    "single_source_fallback": -0.15,
    "multi_source_fallback_agreed": -0.08,
    "low_conflict": -0.05,
    "medium_conflict": -0.12,
    "high_conflict": -0.25,
    "critical_conflict": -0.50,
    "missing_raw_payload_ref": -0.30,
    "missing_field_provenance": -0.20,
}


def clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def weighted_quality_score(
    *,
    completeness_score: float,
    consistency_score: float,
    timeliness_score: float,
    provider_reliability_score: float,
    anomaly_score: float,
    provenance_score: float,
    weights: dict[str, float] | None = None,
) -> QualityScore:
    weights = weights or DEFAULT_WEIGHTS
    total = (
        weights["completeness_score"] * completeness_score
        + weights["consistency_score"] * consistency_score
        + weights["timeliness_score"] * timeliness_score
        + weights["provider_reliability_score"] * provider_reliability_score
        + weights["anomaly_score"] * anomaly_score
        + weights["provenance_score"] * provenance_score
    )
    return QualityScore(
        completeness_score=clamp(completeness_score),
        consistency_score=clamp(consistency_score),
        timeliness_score=clamp(timeliness_score),
        provider_reliability_score=clamp(provider_reliability_score),
        anomaly_score=clamp(anomaly_score),
        provenance_score=clamp(provenance_score),
        data_quality_score=clamp(total),
    )


def apply_quality_adjustments(base_score: float, adjustment_keys: Iterable[str]) -> float:
    score = base_score
    for key in adjustment_keys:
        score += ADJUSTMENTS.get(key, 0.0)
    return clamp(score)


SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}
CONSISTENCY_BY_SEVERITY = {"low": 0.85, "medium": 0.70, "high": 0.45, "critical": 0.10}


def _severity_value(severity: Any) -> str:
    value = str(severity)
    if "." in value:
        value = value.rsplit(".", 1)[-1]
    return value


def _max_severity(conflicts: Iterable[Any]) -> str | None:
    best: str | None = None
    best_rank = -1
    for conflict in conflicts:
        severity = _severity_value(getattr(conflict, "severity", "low"))
        rank = SEVERITY_ORDER.get(severity, 1)
        if rank > best_rank:
            best = severity
            best_rank = rank
    return best


def score_record(
    *,
    provider: str,
    required_fields: Iterable[str],
    record_values: dict[str, Any],
    field_provenance: dict[str, Any] | None,
    raw_payload_ref: str | None,
    merge_method: str,
    conflicts: Iterable[Any] = (),
    provider_reliability: dict[str, float] | None = None,
) -> QualityScore:
    required = list(required_fields)
    present = sum(1 for field in required if record_values.get(field) is not None)
    completeness = present / max(1, len(required))
    conflict_list = list(conflicts)
    if not conflict_list:
        consistency = 1.0
    else:
        max_severity = _max_severity(conflict_list) or "low"
        consistency = CONSISTENCY_BY_SEVERITY.get(max_severity, 0.7)
    reliability = (provider_reliability or DEFAULT_PROVIDER_RELIABILITY).get(provider, DEFAULT_PROVIDER_RELIABILITY["unknown"])
    provenance_score = 1.0 if field_provenance else 0.0
    if raw_payload_ref is None:
        provenance_score = min(provenance_score, 0.4)
    quality = weighted_quality_score(
        completeness_score=completeness,
        consistency_score=consistency,
        timeliness_score=1.0,
        provider_reliability_score=reliability,
        anomaly_score=1.0 if consistency >= 0.7 else 0.5,
        provenance_score=provenance_score,
    )
    adjustments: list[str] = []
    if merge_method == "canonical_validated":
        adjustments.append("canonical_validated_by_two_sources")
    elif merge_method == "canonical_only":
        adjustments.append("canonical_only_not_validated")
    elif merge_method == "fallback_single_source":
        adjustments.append("single_source_fallback")
    elif merge_method == "fallback_multi_source_agreed":
        adjustments.append("multi_source_fallback_agreed")
    for conflict in conflict_list:
        sev = _severity_value(getattr(conflict, "severity", "low"))
        adjustments.append(f"{sev}_conflict")
    if not raw_payload_ref:
        adjustments.append("missing_raw_payload_ref")
    if not field_provenance:
        adjustments.append("missing_field_provenance")
    quality.data_quality_score = apply_quality_adjustments(quality.data_quality_score, adjustments)
    return quality
