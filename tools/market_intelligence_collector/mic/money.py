"""Evidence-backed normalization of CNY amount fields, never price/unit fields.

This checks a number's scale against its cited passage; it does not establish
the owner of an amount, a business relationship, or overall factual accuracy.
Ambiguous, missing-currency and unsupported amounts are left unchanged with a
warning. Callers must still apply their semantic/quality checks before saving.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

_FACTORS = {"元": Decimal(1), "万元": Decimal(10000), "亿元": Decimal(100000000)}
_UNIT_ALIASES = {"CNY": "元", "RMB": "元", "人民币元": "元", "人民币万元": "万元", "人民币亿元": "亿元"}
_CURRENCIES = {"CNY", "RMB", "人民币"}
_MONEY = re.compile(
    r"(?<![\d.,+\-])(?P<value>[+\-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(?P<unit>亿元|万元|元)(?!\s*[/／每])(?![A-Za-z])"
)
_FOREIGN = re.compile(r"美元|美金|港元|港币|澳元|加元|新台币|新臺幣|新加坡元|新元|欧元|歐元|日元|韩元|韓元|英镑|英鎊|(?<![A-Za-z])(?:USD|HKD|TWD|SGD|AUD|CAD|EUR|JPY|KRW|GBP)(?![A-Za-z])|[$€£]", re.I)


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


def _components(fields):
    """Parse currency+scale syntax; this does not infer currency from a number."""
    values = dict(fields)
    currency = str(values.get("currency") or "").strip().upper()
    if currency not in _CURRENCIES:
        for unit in sorted(_FACTORS, key=len, reverse=True):
            if currency.endswith(unit) and currency[:-len(unit)].strip() in _CURRENCIES:
                explicit = str(values.get("amount_unit") or "").strip()
                if explicit and _UNIT_ALIASES.get(explicit, explicit) != unit:
                    return None, "conflicting_currency_unit"
                values.update(currency=currency[:-len(unit)].strip(), amount_unit=unit)
                break
    return values, None


def _resolve(fields: dict[str, Any], text: str) -> tuple[dict[str, Any] | None, str]:
    fields, error = _components(fields)
    if error:
        return None, error
    value = _decimal(fields.get("amount"))
    if value is None:
        return None, "invalid_amount"
    currency = str(fields.get("currency") or "").strip().upper()
    if currency not in _CURRENCIES:
        return None, "missing_or_non_cny_currency"
    if not text:
        return None, "missing_cited_passage"
    # A mixed-currency passage needs a richer currency/entity resolver.
    if _FOREIGN.search(text):
        return None, "foreign_or_mixed_currency_passage"
    unit = str(fields.get("amount_unit") or "").strip()
    unit = _UNIT_ALIASES.get(unit.upper(), _UNIT_ALIASES.get(unit, unit))
    if unit and unit not in _FACTORS:
        return None, "unsupported_amount_unit"
    expected = value * _FACTORS[unit] if unit else None
    matches: dict[Decimal, dict[str, Any]] = {}
    for match in _MONEY.finditer(text):
        source_value = _decimal(match.group("value"))
        if source_value is None:
            continue
        source_unit = match.group("unit")
        normalized = source_value * _FACTORS[source_unit]
        if (expected is not None and expected == normalized) or (
            expected is None and value in (source_value, normalized)
        ):
            matches.setdefault(normalized, {
                "normalized": normalized,
                "source_value": source_value,
                "source_unit": source_unit,
                "quote": match.group(0),
            })
    if len(matches) != 1:
        return None, "ambiguous_scale" if matches else "amount_not_supported_by_citation"
    return next(iter(matches.values())), "supported"


def normalize_cny_fields(fields: dict, passages: dict[str, str], pid: str | None,
                         specifications: list[dict]) -> tuple[dict | None, str]:
    """Apply an evidence-backed model decomposition, or unambiguous numeric syntax.

    Ownership belongs to the shared semantic claim. Neither this function nor
    its success promotes an unsupported role, event, comparison or inference.
    """
    original, error = _components(fields)
    if error:
        return None, error
    evidence, reason = _resolve(original, passages.get(pid, ""))
    evidence_pid = pid
    if specifications:
        resolved = {}
        for spec in specifications:
            citation = spec.get("evidence") or {}
            if not isinstance(citation, dict):
                return None, "normalization_citation_unverified"
            quote, spec_pid = citation.get("quote"), citation.get("passage_id")
            if (not isinstance(quote, str) or not quote.strip() or not isinstance(spec_pid, str) or spec_pid == "title"
                    or quote not in passages.get(spec_pid, "")):
                return None, "normalization_citation_unverified"
            proposal = {"amount": spec.get("value"), "currency": spec.get("currency"),
                        "amount_unit": spec.get("unit")}
            proof, status = _resolve(proposal, quote)
            if proof is None:
                return None, "normalization_" + status
            # Formatting repair cannot silently replace the extracted number.
            value = _decimal(original.get("amount"))
            if value not in (proof["source_value"], proof["normalized"]):
                return None, "normalization_value_conflict"
            currency = str(original.get("currency") or "").strip().upper()
            if currency and currency not in _CURRENCIES:
                return None, "normalization_currency_conflict"
            if evidence and evidence["normalized"] != proof["normalized"]:
                return None, "normalization_scale_conflict"
            explicit_unit = original.get("amount_unit")
            if isinstance(explicit_unit, str) and explicit_unit in _FACTORS and value * _FACTORS[explicit_unit] != proof["normalized"]:
                return None, "normalization_scale_conflict"
            resolved[proof["normalized"]] = (proof, spec_pid)
        if len(resolved) != 1:
            return None, "normalization_scale_conflict"
        evidence, evidence_pid = next(iter(resolved.values()))
    if evidence is None:
        return None, reason
    return {**fields, "amount": _number(evidence["normalized"]), "currency": "CNY",
            "amount_unit": "元", "amount_raw": _number(evidence["source_value"]),
            "amount_raw_unit": evidence["source_unit"],
            "amount_input": dict(fields),
            "amount_evidence": {"passage_id": evidence_pid, "quote": evidence["quote"]}}, "supported"


def cny_amount_supported(fields: dict[str, Any], text: str) -> bool:
    """Compare units numerically instead of trusting model-supplied metadata."""
    return _resolve(fields, text)[0] is not None


def normalize_bundle_amounts(bundle: Any, passages: dict[str, str], warnings: list[str]) -> None:
    for group, field in (("facts", "metrics"), ("events", "metrics"), ("relations", "qualifiers")):
        for index, item in enumerate(getattr(bundle, group)):
            values = getattr(item, field)
            if not isinstance(values, dict) or values.get("amount") is None:
                continue
            pid = item.evidence_locator.passage_id
            evidence, status = _resolve(values, passages.get(pid, ""))
            if evidence is None:
                warnings.append(f"{group}[{index}].{field}.amount: {status}; unchanged")
                continue
            # Copy: validation must not mutate the original model-response dict.
            updated = dict(values)
            updated["amount"] = _number(evidence["normalized"])
            updated["currency"] = "CNY"
            updated["amount_unit"] = "元"
            updated["amount_raw"] = _number(evidence["source_value"])
            updated["amount_raw_unit"] = evidence["source_unit"]
            updated["amount_evidence"] = {"passage_id": pid, "quote": evidence["quote"]}
            setattr(item, field, updated)
