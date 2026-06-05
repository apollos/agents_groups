from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic import BaseModel

from stock_data_ingestion.schemas.quality import ConflictSeverity, DataQualityConflict, MergeMethod, SourceRole, ValidationStatus
from stock_data_ingestion.validation.comparison import compare_standard_records

PROVIDER_SPECIFIC_RECORD_TYPES = {"industry_membership", "concept_membership", "money_flow"}


@dataclass
class MergeResult:
    records: list[BaseModel]
    conflicts: list[DataQualityConflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _severity_rank(severity: ConflictSeverity | str) -> int:
    order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return order[str(severity)]


def _mark(record: BaseModel, **updates: Any) -> BaseModel:
    return record.model_copy(update=updates, deep=True)


def _update_provenance_role(record: BaseModel, role: str, *, reason: str | None = None, validated_by: list[str] | None = None) -> dict[str, Any]:
    provenance = dict(getattr(record, "field_provenance", {}) or {})
    for field_name, entry in list(provenance.items()):
        if isinstance(entry, dict):
            updated = dict(entry)
            updated["source_role"] = role
            if reason:
                updated["reason"] = reason
            if validated_by:
                updated["validated_by"] = sorted(set(updated.get("validated_by", []) + validated_by))
            provenance[field_name] = updated
    return provenance


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, (list, dict)) and not value)


def _fill_missing_fields_from_supplements(
    canonical: BaseModel,
    supplements: list[BaseModel],
    whitelist: Iterable[str],
) -> tuple[BaseModel, list[str]]:
    allowed = set(whitelist)
    if not allowed or not supplements:
        return canonical, []
    updates: dict[str, Any] = {}
    provenance = dict(getattr(canonical, "field_provenance", {}) or {})
    supplement_flags = dict(getattr(canonical, "supplement_flags", {}) or {})
    for field_name in allowed:
        if not hasattr(canonical, field_name) or not _is_missing(getattr(canonical, field_name, None)):
            continue
        for supplement in supplements:
            if not hasattr(supplement, field_name):
                continue
            value = getattr(supplement, field_name, None)
            if _is_missing(value):
                continue
            updates[field_name] = value
            source_entry = (getattr(supplement, "field_provenance", {}) or {}).get(field_name, {})
            provenance[field_name] = {
                **(source_entry if isinstance(source_entry, dict) else {}),
                "provider": getattr(supplement, "provider", None),
                "source_api": getattr(supplement, "source_api", None),
                "source_role": "supplement",
                "raw_payload_id": getattr(supplement, "raw_payload_id", None),
                "reason": "canonical_field_missing_whitelisted_supplement",
            }
            supplement_flags[field_name] = {
                "provider": getattr(supplement, "provider", None),
                "reason": "canonical_field_missing_whitelisted_supplement",
            }
            break
    if not updates:
        return canonical, []
    return _mark(canonical, **updates, field_provenance=provenance, supplement_flags=supplement_flags), sorted(updates)


def mark_provider_specific_append(records: Iterable[BaseModel]) -> MergeResult:
    marked = [
        _mark(
            record,
            source_role=SourceRole.provider_specific,
            merge_method=MergeMethod.provider_specific_append,
            validation_status=ValidationStatus.validated_with_warning,
            field_provenance=_update_provenance_role(record, "provider_specific", reason="provider_specific_methodology"),
        )
        for record in records
    ]
    return MergeResult(records=marked, warnings=["provider_specific_append: methodology differs and records are kept by provider"])


def apply_canonical_merge_policy(
    canonical: BaseModel | None,
    supplements: list[BaseModel],
    *,
    comparison_fields: Iterable[str],
    allow_majority_override_canonical: bool = False,
    quarantine_on_critical_conflict: bool = True,
    allow_field_level_merge: bool = True,
    supplement_field_whitelist: Iterable[str] | None = None,
) -> MergeResult:
    if canonical is None:
        if not supplements:
            return MergeResult(records=[], warnings=["no provider returned data"])
        if len(supplements) == 1:
            supplement_flags = dict(getattr(supplements[0], "supplement_flags", {}) or {})
            supplement_flags["fallback_reason"] = "canonical_provider_missing"
            rec = _mark(
                supplements[0],
                source_role=SourceRole.fallback_canonical,
                merge_method=MergeMethod.fallback_single_source,
                validation_status=ValidationStatus.validated_with_warning,
                effective_provider=getattr(supplements[0], "provider"),
                field_provenance=_update_provenance_role(supplements[0], "fallback_canonical", reason="not_available_from_canonical_provider"),
                supplement_flags=supplement_flags,
                data_quality=min(getattr(supplements[0], "data_quality", 0.75), 0.85),
            )
            return MergeResult(records=[rec], warnings=["canonical_missing_single_source_fallback"])
        first = supplements[0]
        comparisons = [compare_standard_records(first, other, comparison_fields) for other in supplements[1:]]
        conflicts = [conflict for cmp in comparisons for conflict in cmp.conflicts]
        if not conflicts:
            supplement_flags = dict(getattr(first, "supplement_flags", {}) or {})
            supplement_flags["fallback_reason"] = "canonical_provider_missing"
            supplement_flags["validated_by_supplements"] = [getattr(s, "provider") for s in supplements[1:]]
            rec = _mark(
                first,
                source_role=SourceRole.fallback_canonical,
                merge_method=MergeMethod.fallback_multi_source_agreed,
                validation_status=ValidationStatus.validated,
                effective_provider=getattr(first, "provider"),
                field_provenance=_update_provenance_role(first, "fallback_canonical", reason="not_available_from_canonical_provider", validated_by=[getattr(s, "provider") for s in supplements[1:]]),
                supplement_flags=supplement_flags,
                data_quality=min(getattr(first, "data_quality", 0.85), 0.92),
            )
            return MergeResult(records=[rec], warnings=["canonical_missing_multi_source_agreed"])
        rec = _mark(
            first,
            source_role=SourceRole.fallback_canonical,
            merge_method=MergeMethod.manual_review_required,
            validation_status=ValidationStatus.manual_review_required,
            data_quality=min(getattr(first, "data_quality", 0.5), 0.65),
        )
        return MergeResult(records=[rec], conflicts=conflicts, warnings=["canonical_missing_supplements_conflict"])

    if getattr(canonical, "record_type", None) in PROVIDER_SPECIFIC_RECORD_TYPES:
        return mark_provider_specific_append([canonical, *supplements])

    if not supplements:
        rec = _mark(canonical, merge_method=MergeMethod.canonical_only, validation_status=ValidationStatus.validated_with_warning)
        return MergeResult(records=[rec], warnings=["canonical_only_not_validated"])

    filled_fields: list[str] = []
    if allow_field_level_merge:
        canonical, filled_fields = _fill_missing_fields_from_supplements(canonical, supplements, supplement_field_whitelist or [])

    comparisons = [compare_standard_records(canonical, other, comparison_fields) for other in supplements]
    conflicts = [conflict for cmp in comparisons for conflict in cmp.conflicts]
    if not conflicts:
        validated_by = [getattr(s, "provider") for s in supplements]
        field_provenance = dict(getattr(canonical, "field_provenance", {}))
        for field_name, prov in field_provenance.items():
            if isinstance(prov, dict):
                updated = dict(prov)
                updated["validated_by"] = sorted(set(updated.get("validated_by", []) + validated_by))
                field_provenance[field_name] = updated
        method = MergeMethod.fill_missing_from_supplement if filled_fields else MergeMethod.canonical_validated
        status = ValidationStatus.validated_with_warning if filled_fields else ValidationStatus.validated
        warnings = [f"filled_missing_fields_from_supplement:{','.join(filled_fields)}"] if filled_fields else []
        rec = _mark(
            canonical,
            merge_method=method,
            validation_status=status,
            field_provenance=field_provenance,
            data_quality=min(1.0, getattr(canonical, "data_quality", 0.9) + (0.08 if len(supplements) >= 2 else 0.04)),
        )
        return MergeResult(records=[rec], warnings=warnings)

    max_severity = max(_severity_rank(c.severity) for c in conflicts)
    conflict_ids = [c.conflict_id for c in conflicts]
    canonical_value_suspect = False
    if len(supplements) >= 2:
        for field_name in {c.field_name for c in conflicts}:
            values = [getattr(s, field_name, None) for s in supplements]
            if len(set(map(str, values))) == 1 and str(values[0]) != str(getattr(canonical, field_name, None)):
                canonical_value_suspect = True
                break

    if max_severity >= _severity_rank(ConflictSeverity.high):
        status = ValidationStatus.quarantined if quarantine_on_critical_conflict else ValidationStatus.manual_review_required
        method = MergeMethod.quarantined_due_to_conflict if quarantine_on_critical_conflict else MergeMethod.manual_review_required
    elif max_severity == _severity_rank(ConflictSeverity.medium):
        status = ValidationStatus.conflicted_medium
        method = MergeMethod.canonical_with_warning
    else:
        status = ValidationStatus.conflicted_low
        method = MergeMethod.canonical_with_warning

    rec = _mark(
        canonical,
        merge_method=method,
        validation_status=status,
        conflict_ids=conflict_ids,
        canonical_value_suspect=canonical_value_suspect,
        data_quality=max(0.0, getattr(canonical, "data_quality", 0.9) - (0.25 if max_severity >= 3 else 0.12)),
    )
    if allow_majority_override_canonical:
        # This branch exists for explicit future configuration only. It is deliberately not used by defaults.
        return MergeResult(records=[rec], conflicts=conflicts, warnings=["majority_override_enabled_but_not_applied_automatically"])
    return MergeResult(records=[rec], conflicts=conflicts, warnings=["canonical_retained_conflicts_recorded"])
