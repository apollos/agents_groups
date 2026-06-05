from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Optional

from stock_data_ingestion.schemas.quality import ConflictSeverity, DataQualityConflict

DEFAULT_TOLERANCES: dict[str, dict[str, float]] = {
    "price": {"absolute": 0.01, "relative": 0.0001},
    "volume": {"relative": 0.005},
    "amount": {"relative": 0.01},
    "turnover_rate": {"absolute": 0.05},
    "adj_factor": {"relative": 0.0001},
    "market_value": {"relative": 0.01},
    "valuation_ratio": {"relative": 0.01},
    "financial_amount": {"relative": 0.001, "absolute": 10000.0},
    "financial_ratio": {"absolute": 0.01, "relative": 0.01},
}

PRICE_FIELDS = {"open", "high", "low", "close", "pre_close", "change", "limit_up_price", "limit_down_price", "latest_price", "bid1_price", "ask1_price"}
VOLUME_FIELDS = {"volume", "bid1_volume", "ask1_volume"}
AMOUNT_FIELDS = {"amount", "operating_revenue", "operating_profit", "net_profit", "parent_net_profit", "total_assets", "total_liabilities", "parent_equity", "operating_cash_flow"}
TURNOVER_FIELDS = {"turnover_rate", "turnover_rate_free_float"}
VALUATION_FIELDS = {"pe", "pe_ttm", "pb", "ps", "ps_ttm", "dividend_yield"}
FINANCIAL_RATIO_FIELDS = {"pct_change", "roe", "roa", "gross_margin", "net_margin", "revenue_yoy", "net_profit_yoy", "debt_asset_ratio", "current_ratio", "ocf_to_net_profit", "eps", "bps"}
BOOLEAN_STATUS_FIELDS = {"is_open", "is_trading", "is_suspended", "is_st", "is_star_st", "has_delisting_risk", "hit_limit_up", "hit_limit_down", "list_status", "tradability_status"}
CRITICAL_FIELDS = {
    "is_open",
    "is_trading",
    "is_suspended",
    "is_st",
    "is_star_st",
    "has_delisting_risk",
    "hit_limit_up",
    "hit_limit_down",
    "list_status",
    "tradability_status",
    "limit_up_price",
    "limit_down_price",
    "adj_factor",
    "close",
    "volume",
    "amount",
}

COMMON_COMPANY_SUFFIXES = [
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "集团",
    "公司",
    "inc.",
    "ltd.",
    "limited",
]


def normalize_text_for_compare(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"\s+", "", text)
    for suffix in COMMON_COMPANY_SUFFIXES:
        text = text.removesuffix(suffix.lower())
    synonyms = {"st": "st", "*st": "*st", "暂停上市": "停牌", "停牌中": "停牌"}
    return synonyms.get(text, text)


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _relative_diff(a: float, b: float) -> float:
    denominator = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denominator


def _category(field_name: str) -> str:
    if field_name in PRICE_FIELDS:
        return "price"
    if field_name in VOLUME_FIELDS:
        return "volume"
    if field_name in AMOUNT_FIELDS:
        return "amount"
    if field_name in TURNOVER_FIELDS:
        return "turnover_rate"
    if field_name == "adj_factor":
        return "adj_factor"
    if field_name in VALUATION_FIELDS or field_name in {"total_market_value", "float_market_value"}:
        return "market_value" if field_name.endswith("market_value") else "valuation_ratio"
    if field_name in FINANCIAL_RATIO_FIELDS:
        return "financial_ratio"
    return "generic"


def numeric_conflict(
    field_name: str,
    canonical_value: Any,
    other_value: Any,
    tolerances: dict[str, dict[str, float]] | None = None,
) -> tuple[bool, ConflictSeverity, dict[str, Any]]:
    a = _as_float(canonical_value)
    b = _as_float(other_value)
    if a is None or b is None:
        return (a != b), ConflictSeverity.medium, {"rule": "numeric_missing_or_non_numeric"}
    category = _category(field_name)
    cfg = {**DEFAULT_TOLERANCES.get(category, {}), **(tolerances or {}).get(category, {})}
    abs_diff = abs(a - b)
    rel_diff = _relative_diff(a, b)
    abs_ok = "absolute" in cfg and abs_diff <= cfg["absolute"]
    rel_ok = "relative" in cfg and rel_diff <= cfg["relative"]
    if abs_ok or rel_ok:
        return False, ConflictSeverity.low, {"category": category, "absolute_diff": abs_diff, "relative_diff": rel_diff, **cfg}
    severity = ConflictSeverity.medium
    if field_name in CRITICAL_FIELDS:
        severity = ConflictSeverity.high
    if field_name in {"close", "adj_factor"} and rel_diff > 0.2:
        severity = ConflictSeverity.critical
    if category == "amount" and rel_diff > 10.0:
        severity = ConflictSeverity.critical
    return True, severity, {"category": category, "absolute_diff": abs_diff, "relative_diff": rel_diff, **cfg}


def detect_field_conflict(
    *,
    record_type: str,
    comparison_key: str,
    field_name: str,
    canonical_provider: str,
    canonical_value: Any,
    other_provider: str,
    other_value: Any,
    tolerances: dict[str, dict[str, float]] | None = None,
    request_id: str | None = None,
    ingestion_run_id: str | None = None,
    canonical_record_id: str | None = None,
    other_record_id: str | None = None,
) -> DataQualityConflict | None:
    if canonical_value is None and other_value is None:
        return None
    if isinstance(canonical_value, bool) or isinstance(other_value, bool) or field_name in BOOLEAN_STATUS_FIELDS:
        if canonical_value == other_value:
            return None
        severity = ConflictSeverity.high if field_name in CRITICAL_FIELDS else ConflictSeverity.medium
        if field_name in {"is_trading", "is_suspended", "is_st", "is_star_st", "hit_limit_down"}:
            severity = ConflictSeverity.critical
        return DataQualityConflict(
            record_type=record_type,
            comparison_key=comparison_key,
            field_name=field_name,
            canonical_provider=canonical_provider,
            canonical_value=canonical_value,
            other_provider=other_provider,
            other_value=other_value,
            severity=severity,
            tolerance={"rule": "boolean_or_status_mismatch"},
            reason=f"{field_name} differs between canonical and {other_provider}",
            request_id=request_id,
            ingestion_run_id=ingestion_run_id,
            canonical_record_id=canonical_record_id,
            other_record_id=other_record_id,
        )

    category = _category(field_name)
    if category != "generic":
        conflicted, severity, tolerance = numeric_conflict(field_name, canonical_value, other_value, tolerances)
        if not conflicted:
            return None
        return DataQualityConflict(
            record_type=record_type,
            comparison_key=comparison_key,
            field_name=field_name,
            canonical_provider=canonical_provider,
            canonical_value=canonical_value,
            other_provider=other_provider,
            other_value=other_value,
            severity=severity,
            tolerance=tolerance,
            reason=f"numeric field {field_name} exceeds tolerance",
            request_id=request_id,
            ingestion_run_id=ingestion_run_id,
            canonical_record_id=canonical_record_id,
            other_record_id=other_record_id,
        )

    if normalize_text_for_compare(canonical_value) == normalize_text_for_compare(other_value):
        return None
    return DataQualityConflict(
        record_type=record_type,
        comparison_key=comparison_key,
        field_name=field_name,
        canonical_provider=canonical_provider,
        canonical_value=canonical_value,
        other_provider=other_provider,
        other_value=other_value,
        severity=ConflictSeverity.medium if field_name in CRITICAL_FIELDS else ConflictSeverity.low,
        tolerance={"rule": "normalized_text_mismatch"},
        reason=f"text/status field {field_name} differs after normalization",
        request_id=request_id,
        ingestion_run_id=ingestion_run_id,
        canonical_record_id=canonical_record_id,
        other_record_id=other_record_id,
    )
