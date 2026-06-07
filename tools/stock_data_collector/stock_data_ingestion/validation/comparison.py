from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel

from stock_data_ingestion.schemas.records import BarRecord, ProviderComparisonResult
from stock_data_ingestion.validation.conflict import detect_field_conflict



def _is_present(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, dict)) and not value:
        return False
    try:
        # pandas/float NaN should not be treated as a comparable value.
        return not (value != value)
    except Exception:  # noqa: BLE001
        return True

DEFAULT_BAR_FIELDS = [
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_change",
    "volume",
    "amount",
    "vwap",
    "turnover_rate",
    "adj_factor",
]


def build_comparison_key(record: BaseModel | dict[str, Any]) -> str:
    data = record.model_dump(mode="python") if isinstance(record, BaseModel) else record
    record_type = data.get("record_type")
    if record_type == "security_master":
        return str(data["normalized_ticker"])
    if record_type == "trade_calendar":
        return f"{data['exchange']}|{data['calendar_date']}"
    if record_type == "trading_status":
        return f"{data['normalized_ticker']}|{data['trade_date']}"
    if record_type == "bar":
        ts = data.get("timestamp") or data.get("trade_date")
        return f"{data['normalized_ticker']}|{data['frequency']}|{ts}|{data['adjust']}"
    if record_type == "realtime_quote":
        return f"{data['normalized_ticker']}|{data['quote_time_bucket']}"
    if record_type == "adj_factor":
        return f"{data['normalized_ticker']}|{data['trade_date']}"
    if record_type == "financial_statement":
        return f"{data['normalized_ticker']}|{data['report_period']}|{data['statement_type']}|{data['report_type']}"
    if record_type == "financial_indicator":
        return f"{data['normalized_ticker']}|{data['report_period']}"
    if record_type == "valuation_metric":
        return f"{data['normalized_ticker']}|{data['trade_date']}"
    if record_type == "industry_membership":
        return f"{data['normalized_ticker']}|{data['industry_system']}|{data.get('effective_date')}"
    if record_type == "concept_membership":
        return f"{data['normalized_ticker']}|{data.get('concept_code') or data.get('concept_name')}|{data.get('provider')}"
    if record_type == "money_flow":
        return f"{data['normalized_ticker']}|{data['trade_date']}|{data.get('frequency')}|{data.get('source_methodology')}"
    if record_type == "index_constituent":
        return f"{data['index_code']}|{data['normalized_ticker']}|{data['effective_date']}"
    if record_type == "corporate_action":
        return f"{data['normalized_ticker']}|{data['action_type']}|{data.get('announcement_date')}|{data.get('ex_date')}"
    return "|".join(str(data.get(k)) for k in sorted(data.keys()) if k.endswith("_id") or k.endswith("code"))


def _uses_tencent_hist(data: dict[str, Any]) -> bool:
    return "stock_zh_a_hist_tx" in str(data.get("source_api", ""))


def compare_standard_records(
    canonical: BaseModel,
    other: BaseModel,
    fields: Iterable[str],
    tolerances: dict[str, dict[str, float]] | None = None,
) -> ProviderComparisonResult:
    c = canonical.model_dump(mode="python")
    o = other.model_dump(mode="python")
    comparison_key = build_comparison_key(c)
    checked: list[str] = []
    matched: list[str] = []
    conflicts = []
    for field in fields:
        if field not in c and field not in o:
            continue
        canonical_value = c.get(field)
        other_value = o.get(field)
        # Missing values are not evidence of a conflict. A field can only be
        # cross-validated when both providers actually supplied comparable
        # values. This prevents lightweight AKShare endpoints, such as
        # stock_info_a_code_name, from generating false conflicts against
        # richer Tushare fields. Canonical-missing/other-present remains a
        # supplement-candidate case handled by merge_policy, not comparison.
        if not _is_present(canonical_value) or not _is_present(other_value):
            continue
        # AKShare's Tencent daily fallback does not provide trading amount or
        # true VWAP. The adapter supplies a marked estimate only to satisfy the
        # current standard BarRecord shape. Do not let that estimate produce
        # false amount/vwap conflicts against richer canonical providers.
        if field in {"amount", "vwap"} and (_uses_tencent_hist(c) or _uses_tencent_hist(o)):
            continue
        checked.append(field)
        conflict = detect_field_conflict(
            record_type=str(c.get("record_type")),
            comparison_key=comparison_key,
            field_name=field,
            canonical_provider=str(c.get("provider")),
            canonical_value=canonical_value,
            other_provider=str(o.get("provider")),
            other_value=other_value,
            tolerances=tolerances,
            request_id=c.get("request_id"),
            ingestion_run_id=c.get("ingestion_run_id"),
            canonical_record_id=c.get("record_id"),
            other_record_id=o.get("record_id"),
        )
        if conflict is None:
            matched.append(field)
        else:
            conflicts.append(conflict)
    return ProviderComparisonResult(
        record_type=str(c.get("record_type")),
        comparison_key=comparison_key,
        canonical_provider=str(c.get("provider")),
        compared_provider=str(o.get("provider")),
        status="matched" if not conflicts else "conflicted",
        checked_fields=checked,
        matched_fields=matched,
        conflicted_fields=[conflict.field_name for conflict in conflicts],
        conflicts=conflicts,
        request_id=c.get("request_id"),
        ingestion_run_id=c.get("ingestion_run_id"),
    )


def compare_bar_records(canonical: BarRecord, other: BarRecord, tolerances: dict[str, dict[str, float]] | None = None) -> ProviderComparisonResult:
    return compare_standard_records(canonical, other, DEFAULT_BAR_FIELDS, tolerances)
