"""Format checks and Decimal conversion for CNY amount fields, never price fields.

Whether an amount is supported by the source, which currency/unit/scale the
source uses and which event the amount belongs to are semantic judgments made
by the model and enforced once by the shared content review. This module only
verifies format: the claim's ``amount`` structure, that every cited quote is a
substring of the input passage, that the record and its claim agree, and the
arithmetic ``value × scale = 元``. It never searches the passage for numbers,
never guesses a scale and never rejects an amount because of its unit; an
amount it cannot convert is kept as given with an explicit "not converted" note.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# Conversion table only. Units outside this table are kept unconverted; they are
# not evidence of anything and never make an amount "unsupported".
_FACTORS = {
    "元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000),
    "百万元": Decimal(1000000), "千万元": Decimal(10000000),
    "亿元": Decimal(100000000), "万亿元": Decimal(1000000000000),
}
_UNIT_ALIASES = {"CNY": "元", "RMB": "元", "人民币": "元", "人民币元": "元", "人民币千元": "千元",
                 "人民币万元": "万元", "人民币亿元": "亿元", "千人民币": "千元"}
_CURRENCIES = {"CNY", "RMB", "人民币"}
_PRICE_UNIT = re.compile(r"^(?:CNY|RMB|人民币)?(亿元|万元|千元|元)\s*[/／每]\s*([A-Za-z\u4e00-\u9fff]+)$", re.I)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (ValueError, InvalidOperation):
        return None
    return number if number.is_finite() else None


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _canonical_unit(unit: Any) -> str:
    text = str(unit or "").strip()
    return _UNIT_ALIASES.get(text.upper(), _UNIT_ALIASES.get(text, text))


def _is_cny(currency: Any) -> bool:
    return str(currency or "").strip().upper() in _CURRENCIES


def _components(fields):
    """Parse currency+scale syntax such as ``currency="CNY万元"``.

    This is format repair of the model's own record; it does not infer a
    currency or a scale from the source text.
    """
    values = dict(fields)
    currency = str(values.get("currency") or "").strip().upper()
    if currency not in _CURRENCIES:
        for unit in sorted(_FACTORS, key=len, reverse=True):
            if currency.endswith(unit) and currency[:-len(unit)].strip() in _CURRENCIES:
                explicit = str(values.get("amount_unit") or "").strip()
                if explicit and _canonical_unit(explicit) != unit:
                    return None, "conflicting_currency_unit"
                values.update(currency=currency[:-len(unit)].strip(), amount_unit=unit)
                break
    return values, None


def _citations(evidence: Any) -> list[dict] | None:
    """A claim may cite several passages (value, unit and currency separately)."""
    if isinstance(evidence, dict):
        evidence = [evidence]
    if not isinstance(evidence, list) or not evidence:
        return None
    return evidence if all(isinstance(e, dict) for e in evidence) else None


def _quote_verified(citation: dict, passages: dict[str, str]) -> bool:
    quote, pid = citation.get("quote"), citation.get("passage_id")
    return (isinstance(quote, str) and bool(quote.strip()) and isinstance(pid, str)
            and pid != "title" and quote in passages.get(pid, ""))


def convert(value: Decimal, currency: Any, unit: Any) -> tuple[Decimal | None, str]:
    """``value × scale`` for CNY scale units; anything else is left unconverted."""
    if not _is_cny(currency):
        return None, "non_cny_currency"
    canonical = _canonical_unit(unit)
    if not canonical:
        return None, "missing_amount_unit"
    if canonical not in _FACTORS:
        return None, "unknown_amount_unit"
    return value * _FACTORS[canonical], "converted"


def _materialize(fields: dict, value: Decimal, currency: str, unit: str, evidence: Any) -> dict:
    """Write the reviewed amount back; converted to 元 when the unit allows it."""
    canonical = _canonical_unit(unit) or unit
    converted, status = convert(value, currency, unit)
    if converted is not None:
        output = {**fields, "amount": _number(converted), "currency": "CNY", "amount_unit": "元"}
    else:
        output = {**fields, "amount": _number(value), "currency": str(currency).strip().upper() or None,
                  "amount_unit": canonical or None,
                  "amount_conversion": {"status": "not_converted", "reason": status}}
    output.update(amount_raw=_number(value), amount_raw_unit=canonical or None,
                  amount_input=dict(fields), amount_evidence=evidence)
    if fields.get("unit") in _FACTORS:
        output.update(unit="元" if converted is not None else fields["unit"], unit_raw=fields["unit"])
    return output


def normalize_cny_fields(fields: dict, passages: dict[str, str],
                         specifications: list[dict]) -> tuple[dict | None, str]:
    """Apply the model's reviewed amount decomposition to a record's amount fields.

    ``specifications`` are the ``amount`` objects of claims that the shared
    content review already accepted as ``source_supported`` for this record.
    The caller decides whether the semantic review passed; this function only
    checks structure, citation membership, record/claim agreement and converts.

    Returns ``(updated_fields, "supported")`` or ``(None, reason)`` where the
    reason is a format problem (``format_pending``) or a missing/unverified
    citation (``pending_review``); see :func:`reason_status`.
    """
    original, error = _components(fields)
    if error:
        return None, error
    value = _decimal(original.get("amount"))
    if value is None:
        return None, "invalid_amount"
    if not specifications:
        return None, "amount_claim_missing"
    resolved: dict[tuple, tuple] = {}
    for spec in specifications:
        if not isinstance(spec, dict):
            return None, "amount_structure_invalid"
        spec_value = _decimal(spec.get("value"))
        spec_currency = str(spec.get("currency") or "").strip().upper()
        spec_unit = _canonical_unit(spec.get("unit"))
        if spec_value is None or not spec_currency or not isinstance(spec.get("unit"), str):
            return None, "amount_structure_invalid"
        citations = _citations(spec.get("evidence"))
        if citations is None or not all(_quote_verified(c, passages) for c in citations):
            return None, "amount_citation_unverified"
        # The claim refines the record; it cannot silently replace its number or currency.
        converted, _ = convert(spec_value, spec_currency, spec_unit)
        if value != spec_value and (converted is None or value != converted):
            return None, "normalization_value_conflict"
        currency = str(original.get("currency") or "").strip().upper()
        if currency and currency != spec_currency and not (_is_cny(currency) and _is_cny(spec_currency)):
            return None, "normalization_currency_conflict"
        explicit_unit = _canonical_unit(original.get("amount_unit"))
        if explicit_unit and explicit_unit != spec_unit and not (
                explicit_unit == "元" and converted is not None and value == converted):
            return None, "normalization_scale_conflict"
        evidence = spec["evidence"] if isinstance(spec["evidence"], dict) else list(citations)
        resolved.setdefault((spec_value, spec_currency, spec_unit), (spec_value, spec_currency, spec_unit, evidence))
    if len(resolved) != 1:
        return None, "amount_claims_conflict"
    spec_value, spec_currency, spec_unit, evidence = next(iter(resolved.values()))
    return _materialize(fields, spec_value, spec_currency, spec_unit, evidence), "supported"


def reason_status(reason: str) -> str:
    """Missing or unverifiable citations are review matters; the rest is format."""
    return "pending_review" if reason in ("amount_claim_missing", "amount_citation_unverified") else "format_pending"


def normalize_quoted_price(fields: dict, passages: dict[str, str], pid: str | None,
                           specifications: list[dict]) -> tuple[dict | None, str]:
    """Preserve a quoted currency-per-unit quantity, never turn it into money.

    Only dimensional syntax and a source number are checked here. Ownership,
    comparability, price trends and economic implications remain model reviews.
    """
    units = [u for u in (fields.get("amount_unit"), fields.get("unit"))
             if isinstance(u, str) and _PRICE_UNIT.fullmatch(u.strip())]
    units += [s["unit"] for s in specifications if isinstance(s, dict) and isinstance(s.get("unit"), str)
              and _PRICE_UNIT.fullmatch(s["unit"].strip())]
    if not units:
        return None, "not_unit_price"
    currency = str(fields.get("currency") or "").strip().upper()
    if currency and currency not in _CURRENCIES:
        return None, "conflicting_price_currency"
    canonical = {re.sub(r"\s+", "", u).replace("／", "/").replace("每", "/") for u in units}
    if len(canonical) != 1:
        return None, "conflicting_price_unit"
    unit = next(iter(canonical))
    value = _decimal(fields.get("amount"))
    if value is None:
        return None, "invalid_price"
    numerator, denominator = _PRICE_UNIT.fullmatch(unit).groups()
    pattern = re.compile(r"(?<![\d.,+\-])([+\-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
                         + re.escape(numerator) + r"\s*[/／每]\s*" + re.escape(denominator) + r"(?![A-Za-z])")
    citations = [{"passage_id": pid, "quote": passages.get(pid, "")}]
    for spec in specifications:
        if isinstance(spec, dict):
            citations += _citations(spec.get("evidence")) or []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        text, source_pid = citation.get("quote"), citation.get("passage_id")
        if not isinstance(text, str) or source_pid == "title" or text not in passages.get(source_pid, ""):
            continue
        match = next((m for m in pattern.finditer(text) if _decimal(m.group(1)) == value), None)
        if match:
            if fields.get("unit_price") is not None and _decimal(fields["unit_price"]) != value:
                return None, "conflicting_price_value"
            return {**fields, "amount": None, "amount_kind": "unit_price",
                    "unit_price": _number(value), "unit_price_unit": numerator + "/" + denominator,
                    "price_input": dict(fields),
                    "price_evidence": {"passage_id": source_pid, "quote": match.group(0)}}, "source_quote"
    return None, "amount_not_supported_by_citation"


def normalize_bundle_amounts(bundle: Any, warnings: list[str]) -> None:
    """Legacy (no content_review) path: arithmetic on the model's own unit only.

    Without reviewed claims there is no source evidence to apply, so this pass
    neither searches passages nor guesses a scale. It converts ``amount`` to 元
    when the record itself states a CNY scale unit and leaves everything else
    unchanged with a warning.
    """
    for group, field in (("facts", "metrics"), ("events", "metrics"), ("relations", "qualifiers")):
        for index, item in enumerate(getattr(bundle, group)):
            values = getattr(item, field)
            if not isinstance(values, dict) or values.get("amount") is None or "amount_raw" in values:
                continue
            fields, error = _components(values)
            if error:
                warnings.append(f"{group}[{index}].{field}.amount: {error}; unchanged")
                continue
            value = _decimal(fields.get("amount"))
            if value is None:
                warnings.append(f"{group}[{index}].{field}.amount: invalid_amount; unchanged")
                continue
            unit = _canonical_unit(fields.get("amount_unit"))
            converted, status = convert(value, fields.get("currency"), unit)
            if converted is None:
                warnings.append(f"{group}[{index}].{field}.amount: {status}; unchanged")
                continue
            # Copy: validation must not mutate the original model-response dict.
            updated = dict(values)
            updated.update(amount=_number(converted), currency="CNY", amount_unit="元",
                           amount_raw=_number(value), amount_raw_unit=unit)
            setattr(item, field, updated)
