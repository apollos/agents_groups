"""Provider/API-specific semantics for valuation and financial-indicator fields.

Each standard field is read from one named raw field of one provider interface,
with its unit, metric definition and period basis declared here. Nothing is
inferred from a value's magnitude, and a field is never filled by trying other
providers' column names. Conversions happen only here, exactly once.

``definition=None`` records "口径未确认": the value is kept with that status but is
neither compared nor used to supplement another provider. ``raw_unit=None`` means
the scale itself is unconfirmed; such a value stays in the raw payload only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

SEMANTIC_RECORD_TYPES = frozenset({"valuation_metric", "financial_indicator"})

TRADE_DATE = "trade_date_point"
YTD = "report_period_ytd"
PERIOD_END = "report_period_end"

UNIT_SCALE = {
    ("CNY_10k", "CNY"): 10000.0,
    ("CNY", "CNY"): 1.0,
    ("decimal", "percent"): 100.0,
    ("percent", "percent"): 1.0,
    ("times", "times"): 1.0,
    ("CNY_per_share", "CNY_per_share"): 1.0,
}


@dataclass(frozen=True)
class FieldSpec:
    raw_field: str
    raw_unit: str | None
    standard_unit: str
    definition: str | None
    period_basis: str | None
    note: str = ""


def _spec(raw, raw_unit, unit, definition, period, note=""):
    return FieldSpec(raw, raw_unit, unit, definition, period, note)


# Standard definitions a supplement must carry before it may fill a missing field.
STANDARD_DEFINITIONS: dict[str, dict[str, tuple[str, str, str]]] = {
    "valuation_metric": {
        "pe_ttm": ("pe_ttm", "times", TRADE_DATE),
        "ps_ttm": ("ps_ttm", "times", TRADE_DATE),
        "pcf_ncf_ttm": ("pcf_ncf_ttm", "times", TRADE_DATE),
        "total_market_value": ("total_market_value", "CNY", TRADE_DATE),
        "float_market_value": ("float_market_value", "CNY", TRADE_DATE),
        "volume_ratio": ("volume_ratio", "times", TRADE_DATE),
    },
    "financial_indicator": {
        "gross_margin": ("sales_gross_margin", "percent", YTD),
        "net_margin": ("sales_net_margin", "percent", YTD),
        "eps": ("basic_eps", "CNY_per_share", YTD),
        "revenue_yoy": ("operating_revenue_yoy", "percent", YTD),
        "net_profit_yoy": ("parent_net_profit_yoy", "percent", YTD),
        "debt_asset_ratio": ("debt_to_assets", "percent", PERIOD_END),
        "current_ratio": ("current_ratio", "times", PERIOD_END),
        "ocf_to_net_profit": ("ocf_to_net_profit", "times", YTD),
        "bps": ("bps", "CNY_per_share", PERIOD_END),
    },
}

_TUSHARE_DAILY_BASIC = {
    "pe": _spec("pe", "times", "times", None, TRADE_DATE, "daily_basic.pe 文档为总市值/净利润，未写明净利润期间"),
    "pe_ttm": _spec("pe_ttm", "times", "times", "pe_ttm", TRADE_DATE),
    "pb": _spec("pb", "times", "times", None, TRADE_DATE, "daily_basic.pb 文档为总市值/净资产，未写明净资产期间"),
    "ps": _spec("ps", "times", "times", None, TRADE_DATE, "daily_basic.ps 为非 TTM 市销率，未写明营收期间"),
    "ps_ttm": _spec("ps_ttm", "times", "times", "ps_ttm", TRADE_DATE),
    "total_market_value": _spec("total_mv", "CNY_10k", "CNY", "total_market_value", TRADE_DATE),
    "float_market_value": _spec("circ_mv", "CNY_10k", "CNY", "float_market_value", TRADE_DATE),
    "turnover_rate": _spec("turnover_rate", "percent", "percent", None, TRADE_DATE, "daily_basic.turnover_rate 未写明股本基数"),
    "volume_ratio": _spec("volume_ratio", "times", "times", "volume_ratio", TRADE_DATE),
}

# stock_value_em renames Eastmoney PE_TTM / PE_LAR / PB_MRQ / PS_TTM / TOTAL_MARKET_CAP /
# NOTLIMITED_MARKETCAP_A (akshare/stock_feature/stock_value_em.py).
_AKSHARE_VALUE_EM = {
    "pe_ttm": _spec("PE(TTM)", "times", "times", "pe_ttm", TRADE_DATE),
    "pe": _spec("PE(静)", "times", "times", "pe_lar", TRADE_DATE, "上游 PE_LAR"),
    "pb": _spec("市净率", "times", "times", "pb_mrq", TRADE_DATE, "上游 PB_MRQ"),
    "ps_ttm": _spec("市销率", "times", "times", "ps_ttm", TRADE_DATE, "上游 PS_TTM"),
    "total_market_value": _spec("总市值", "CNY", "CNY", "total_market_value", TRADE_DATE, "上游 TOTAL_MARKET_CAP"),
    "float_market_value": _spec("流通市值", "CNY", "CNY", "float_market_value", TRADE_DATE, "上游 NOTLIMITED_MARKETCAP_A"),
}

_AKSHARE_SPOT_EM = {
    "total_market_value": _spec("总市值", "CNY", "CNY", "total_market_value", TRADE_DATE),
    "float_market_value": _spec("流通市值", "CNY", "CNY", "float_market_value", TRADE_DATE),
    "pe_ttm": _spec("市盈率-动态", "times", "times", None, TRADE_DATE, "动态市盈率不等同 TTM"),
    "pb": _spec("市净率", "times", "times", None, TRADE_DATE),
    "turnover_rate": _spec("换手率", "percent", "percent", None, TRADE_DATE),
    "volume_ratio": _spec("量比", "times", "times", "volume_ratio", TRADE_DATE),
}

_BAOSTOCK_K_DATA = {
    "pe_ttm": _spec("peTTM", "times", "times", "pe_ttm", TRADE_DATE),
    "pb": _spec("pbMRQ", "times", "times", "pb_mrq", TRADE_DATE),
    "ps_ttm": _spec("psTTM", "times", "times", "ps_ttm", TRADE_DATE),
    "pcf_ncf_ttm": _spec("pcfNcfTTM", "times", "times", "pcf_ncf_ttm", TRADE_DATE),
}

# fina_indicator.gross_margin is the gross-profit amount; the margin is grossprofit_margin.
_TUSHARE_FINA_INDICATOR = {
    "roe": _spec("roe", "percent", "percent", None, YTD, "fina_indicator.roe 未写明平均/期末/加权口径"),
    "roa": _spec("roa", "percent", "percent", None, YTD),
    "gross_margin": _spec("grossprofit_margin", "percent", "percent", "sales_gross_margin", YTD),
    "net_margin": _spec("netprofit_margin", "percent", "percent", "sales_net_margin", YTD),
    "revenue_yoy": _spec("or_yoy", "percent", "percent", "operating_revenue_yoy", YTD),
    "net_profit_yoy": _spec("netprofit_yoy", "percent", "percent", "parent_net_profit_yoy", YTD),
    "debt_asset_ratio": _spec("debt_to_assets", "percent", "percent", "debt_to_assets", PERIOD_END),
    "current_ratio": _spec("current_ratio", "times", "times", "current_ratio", PERIOD_END),
    "eps": _spec("eps", "CNY_per_share", "CNY_per_share", "basic_eps", YTD),
    "bps": _spec("bps", "CNY_per_share", "CNY_per_share", "bps", PERIOD_END),
}

# Sina financial guideline: column names are stable, but the period basis of the
# per-report ratios is not documented, so flow ratios stay unconfirmed.
_AKSHARE_SINA_INDICATOR = {
    "roe": _spec("净资产收益率(%)", "percent", "percent", None, None, "新浪口径未说明平均/期末/加权"),
    "gross_margin": _spec("销售毛利率(%)", "percent", "percent", "sales_gross_margin", None, "期间口径未确认"),
    "net_margin": _spec("销售净利率(%)", "percent", "percent", "sales_net_margin", None, "期间口径未确认"),
    "debt_asset_ratio": _spec("资产负债率(%)", "percent", "percent", "debt_to_assets", PERIOD_END),
    "current_ratio": _spec("流动比率", "times", "times", "current_ratio", PERIOD_END),
}

# BaoStock quarterly ratios are returned as decimals (npMargin 0.547926 vs Tushare 54.7926).
_BAOSTOCK_FINANCIAL = {
    "roe": _spec("roeAvg", "decimal", "percent", "roe_average_equity", YTD),
    "gross_margin": _spec("gpMargin", "decimal", "percent", "sales_gross_margin", YTD),
    "net_margin": _spec("npMargin", "decimal", "percent", "sales_net_margin", YTD),
    "net_profit_yoy": _spec("YOYPNI", "decimal", "percent", "parent_net_profit_yoy", YTD),
    "debt_asset_ratio": _spec("liabilityToAsset", "decimal", "percent", "debt_to_assets", PERIOD_END),
    "current_ratio": _spec("currentRatio", "times", "times", "current_ratio", PERIOD_END),
    "ocf_to_net_profit": _spec("CFOToNP", "times", "times", "ocf_to_net_profit", YTD),
}

REGISTRY: dict[tuple[str, str, str], dict[str, FieldSpec]] = {
    ("valuation_metric", "tushare", "daily_basic"): _TUSHARE_DAILY_BASIC,
    ("valuation_metric", "akshare", "stock_value_em"): _AKSHARE_VALUE_EM,
    ("valuation_metric", "akshare", "stock_zh_a_spot_em"): _AKSHARE_SPOT_EM,
    ("valuation_metric", "baostock", "query_history_k_data_plus"): _BAOSTOCK_K_DATA,
    ("financial_indicator", "tushare", "fina_indicator"): _TUSHARE_FINA_INDICATOR,
    ("financial_indicator", "akshare", "stock_financial_analysis_indicator"): _AKSHARE_SINA_INDICATOR,
    ("financial_indicator", "baostock", "*"): _BAOSTOCK_FINANCIAL,
}


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def lookup(record_type: str, provider: str, source_api: str | None) -> dict[str, FieldSpec] | None:
    return REGISTRY.get((record_type, provider, source_api or "")) or REGISTRY.get((record_type, provider, "*"))


def map_semantic_fields(
    record_type: str, provider: str, source_api: str | None, raw: dict[str, Any], fields: list[str]
) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
    """Return ``(values, semantics)`` for ``fields`` of one raw row.

    Interfaces without a registry entry yield no values: their columns have no
    declared meaning and stay in the raw payload.
    """
    specs = lookup(record_type, provider, source_api) or {}
    values: dict[str, float | None] = {}
    semantics: dict[str, dict[str, Any]] = {}
    for name in fields:
        spec = specs.get(name)
        if spec is None or spec.raw_unit is None:
            values[name] = None
            continue
        raw_value = _number(raw.get(spec.raw_field))
        if raw_value is None:
            values[name] = None
            continue
        scale = UNIT_SCALE[(spec.raw_unit, spec.standard_unit)]
        values[name] = raw_value if scale == 1.0 else float(Decimal(str(raw_value)) * Decimal(str(scale)))
        semantics[name] = {
            "source_interface": source_api,
            "raw_field": spec.raw_field,
            "raw_value": raw.get(spec.raw_field),
            "raw_unit": spec.raw_unit,
            "standard_unit": spec.standard_unit,
            "metric_definition": spec.definition,
            "definition_status": "confirmed" if spec.definition else "unconfirmed",
            "period_basis": spec.period_basis,
            "conversion": "none" if scale == 1.0 else f"multiply:{scale:g}",
            **({"note": spec.note} if spec.note else {}),
        }
    return values, semantics


def comparability(record_type: str, field: str, left: dict[str, Any] | None, right: dict[str, Any] | None) -> str | None:
    """Return ``None`` when both sides are the same metric, else the reason they are not."""
    if record_type not in SEMANTIC_RECORD_TYPES:
        return None
    left, right = left or {}, right or {}
    if not left.get("metric_definition") or not right.get("metric_definition"):
        return "definition_unconfirmed"
    if left["metric_definition"] != right["metric_definition"]:
        return "definition_mismatch"
    if not left.get("period_basis") or left.get("period_basis") != right.get("period_basis"):
        return "period_basis_mismatch_or_unconfirmed"
    if left.get("standard_unit") != right.get("standard_unit"):
        return "unit_mismatch"
    return None


def supplement_reason(record_type: str, field: str, semantics: dict[str, Any] | None) -> str | None:
    """Return ``None`` when a supplement value carries the standard definition of ``field``."""
    if record_type not in SEMANTIC_RECORD_TYPES:
        return None
    expected = STANDARD_DEFINITIONS.get(record_type, {}).get(field)
    semantics = semantics or {}
    if expected is None or not semantics.get("metric_definition"):
        return "definition_unconfirmed"
    definition, unit, period = expected
    if semantics["metric_definition"] != definition:
        return "definition_mismatch"
    if semantics.get("period_basis") != period:
        return "period_basis_mismatch_or_unconfirmed"
    if semantics.get("standard_unit") != unit:
        return "unit_mismatch"
    return None
