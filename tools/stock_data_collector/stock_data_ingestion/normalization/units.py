from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value).replace(",", "")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"NORMALIZATION_FAILED: cannot convert {value!r} to number") from exc


def normalize_volume(value: Any, unit: str | None = None, provider: str | None = None) -> tuple[float | None, str]:
    number = _to_float(value)
    if number is None:
        return None, "share"
    u = (unit or "").lower()
    if not u and provider == "tushare":
        u = "hand"
    if u in {"hand", "hands", "手"}:
        return number * 100.0, "share"
    if u in {"share", "shares", "股", ""}:
        return number, "share"
    if u in {"lot"}:
        return number * 100.0, "share"
    raise ValueError(f"NORMALIZATION_FAILED: unsupported volume unit {unit!r}")


def normalize_amount(value: Any, unit: str | None = None, provider: str | None = None) -> tuple[float | None, str]:
    number = _to_float(value)
    if number is None:
        return None, "CNY"
    u = (unit or "").lower()
    if not u and provider == "tushare":
        u = "thousand_cny"
    if u in {"cny", "yuan", "元", ""}:
        return number, "CNY"
    if u in {"thousand_cny", "千元"}:
        return number * 1000.0, "CNY"
    if u in {"ten_thousand_cny", "万", "万元"}:
        return number * 10000.0, "CNY"
    raise ValueError(f"NORMALIZATION_FAILED: unsupported amount unit {unit!r}")


def normalize_currency(value: str | None) -> str:
    if value is None or not value.strip():
        return "CNY"
    upper = value.strip().upper()
    aliases = {"RMB": "CNY", "人民币": "CNY", "YUAN": "CNY"}
    return aliases.get(upper, upper)


def compute_vwap(amount: float | None, volume: float | None) -> float | None:
    if amount is None or volume is None or volume == 0:
        return None
    return amount / volume


def normalize_turnover_rate(value: Any) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    return number
