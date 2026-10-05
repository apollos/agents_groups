"""Power / energy unit normalisation for facts, events and metrics (Codex review R2).

Source text such as ``100MW/400MWh`` names two quantities: power (MW) and energy
(MWh). The model tends to flatten them into one scalar — ``metric_value=100,
unit="MW/400MWh"``, event ``capacity=100, volume=400`` without units, or a fact
``volume=40, unit="万元/MWh"`` after the amount has been separated. Downstream
code that treats "the first number" as MWh then compares 100 against 400 for
the same project.

This pass works like ``money.normalize_bundle_amounts``: the cited passage is
the only authority. Every power and energy quantity in that passage is parsed
with its unit; a numeric field is mapped to canonical ``power_mw`` /
``energy_mwh`` only when the passage states that exact number with a power /
energy unit. The original fields are kept (``unit_raw``, ``*_unit``); nothing
is guessed — a number the passage does not pair with a unit is marked
``unit_unverified`` and never becomes a canonical quantity.
"""
from __future__ import annotations

import re
from typing import Any

NUMBER = r"\d+(?:\.\d+)?"
POWER_UNITS = {"kw": 0.001, "mw": 1.0, "gw": 1000.0}
ENERGY_UNITS = {"kwh": 0.001, "mwh": 1.0, "gwh": 1000.0}
# Energy first so "MWh" is not consumed as "MW" + "h".
QUANTITY = re.compile(rf"(?<![\d.])({NUMBER})\s*(GWh|MWh|kWh|GW|MW|kW)(?![A-Za-z])", re.I)
# Metric unit strings carrying a second quantity: "MW/400MWh", "MWh / 100MW".
EMBEDDED_UNIT = re.compile(rf"^\s*(GWh|MWh|kWh|GW|MW|kW)\s*/\s*({NUMBER})\s*(GWh|MWh|kWh|GW|MW|kW)\s*$", re.I)
# Fact ``unit`` after amount separation: "万元/MWh", "元 / MWh".
MIXED_UNIT = re.compile(rf"^\s*(?:亿元|万元|元|CNY)\s*/\s*(GWh|MWh|kWh|GW|MW|kW)\s*$", re.I)


def _kind(unit: str) -> str:
    return "energy" if unit.lower().endswith("h") else "power"


def _canonical(value: float, unit: str) -> float:
    table = ENERGY_UNITS if _kind(unit) == "energy" else POWER_UNITS
    return round(value * table[unit.lower()], 6)


def passage_quantities(text: str) -> list[dict[str, Any]]:
    """Every power / energy quantity stated in the passage, with its quote."""
    out = []
    for match in QUANTITY.finditer(text or ""):
        value, unit = float(match.group(1)), match.group(2)
        out.append({"value": value, "unit": unit, "kind": _kind(unit),
                    "canonical": _canonical(value, unit), "quote": match.group(0)})
    return out


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def _additive(text: str, value: float, unit: str | None) -> dict[str, Any] | None:
    """An explicit sum stated in the passage ("120MWh+240MWh" → 360 MWh), same rule as the gate."""
    from mic.evidence_review import additive_quantity   # local import: evidence_review imports widely
    for candidate in ([unit] if unit else ["MWh", "MW"]):
        derived = additive_quantity(text, value, candidate)
        if derived:
            return {"value": value, "unit": candidate, "kind": _kind(candidate),
                    "canonical": _canonical(value, candidate), "quote": derived.get("quote", ""),
                    "operands": derived.get("operands")}
    return None


def _find(quantities: list[dict[str, Any]], value: float, kind: str | None = None,
          unit: str | None = None) -> dict[str, Any] | None:
    hits = [q for q in quantities if _same(q["value"], value)
            and (kind is None or q["kind"] == kind)
            and (unit is None or q["unit"].lower() == unit.lower())]
    kinds = {q["kind"] for q in hits}
    # A number the passage states as both power and energy is ambiguous: do not resolve.
    if len(hits) == 1 or (hits and len(kinds) == 1):
        return hits[0]
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(rf"(?<![\d.])({NUMBER})", str(value).replace(",", ""))
    return float(match.group(1)) if match else None


def _unit_in(value: Any) -> str | None:
    match = QUANTITY.search(str(value)) if isinstance(value, str) else None
    return match.group(2) if match else None


def _write_canonical(block: dict[str, Any], hit: dict[str, Any], pid: str, source_key: str) -> None:
    evidence = {"passage_id": pid, "quote": hit["quote"], "field": source_key}
    if hit.get("operands"):
        evidence["operands"] = hit["operands"]
    if hit["kind"] == "energy":
        block["energy_mwh"] = hit["canonical"]
        block["energy_evidence"] = evidence
    else:
        block["power_mw"] = hit["canonical"]
        block["power_evidence"] = evidence
    block[f"{source_key}_unit"] = hit["unit"]


QUANTITY_STRING = re.compile(rf"^\s*({NUMBER})\s*(GWh|MWh|kWh|GW|MW|kW)(\s*/\s*{NUMBER}\s*(?:GWh|MWh|kWh|GW|MW|kW))?\s*$", re.I)


def repair_metric_value_strings(raw: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """``metric_value="10MW/40MWh"`` → value 10, unit "MW/40MWh" (then split with evidence).

    Batch v5 (2026-10-05): a whole article's extraction failed schema validation because
    two of six metrics carried the source's "power/energy" token as the value. Only this
    exact token shape is repaired, before validation, on a copy; anything else stays a
    schema error.
    """
    metrics = raw.get("metrics") if isinstance(raw, dict) else None
    if not isinstance(metrics, list):
        return raw
    repaired = None
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict) or not isinstance(metric.get("metric_value"), str):
            continue
        match = QUANTITY_STRING.match(metric["metric_value"])
        if not match:
            continue
        if repaired is None:
            repaired = {**raw, "metrics": [dict(m) if isinstance(m, dict) else m for m in metrics]}
        item = repaired["metrics"][index]
        item["metric_value"] = float(match.group(1))
        item["unit"] = match.group(2) + (re.sub(r"\s+", "", match.group(3)) if match.group(3) else "")
        warnings.append(f"metrics[{index}].metric_value: quantity string {metric['metric_value']!r} split into value and unit")
    return repaired or raw


def normalize_metrics_block(values: dict[str, Any], passage: str, pid: str) -> tuple[dict[str, Any], list[str]]:
    """Resolve ``capacity`` / ``volume`` of a fact or event metrics dict. Returns (copy, notes)."""
    quantities = passage_quantities(passage)
    updated = dict(values)
    notes: list[str] = []
    unit_field = updated.get("unit")
    mixed = MIXED_UNIT.match(str(unit_field)) if isinstance(unit_field, str) else None
    if mixed:
        updated["unit_raw"] = unit_field
        updated["unit"] = mixed.group(1)   # the amount already carries its own currency unit
    for key in ("capacity", "volume"):
        raw = updated.get(key)
        if raw is None:
            continue
        if isinstance(raw, str) and len(QUANTITY.findall(raw)) >= 2:
            # "10MW/40MWh" as a field value: both quantities, each verified against the passage.
            for stated in passage_quantities(raw):
                hit = _find(quantities, stated["value"], unit=stated["unit"]) or _additive(passage, stated["value"], stated["unit"])
                if hit:
                    _write_canonical(updated, hit, pid, key)
                else:
                    notes.append(f"{key}={raw}: {stated['quote']} not stated in passage {pid}")
            updated[f"{key}_unit"] = "mixed"
            continue
        value = _number(raw)
        if value is None:
            continue
        stated_unit = updated.get(f"{key}_unit") or _unit_in(raw) \
            or (updated.get("unit") if key == "volume" and isinstance(updated.get("unit"), str)
                and QUANTITY.match(f"1{updated['unit']}") else None)
        hit = _find(quantities, value, unit=stated_unit) if stated_unit else _find(quantities, value)
        if hit is None:
            hit = _additive(passage, value, stated_unit)
        if hit is None and stated_unit and stated_unit.lower() in {**POWER_UNITS, **ENERGY_UNITS}:
            # Unit stated by the model but the passage never pairs it with this number.
            updated[f"{key}_unit_status"] = "unit_unverified"
            notes.append(f"{key}={raw} {stated_unit}: not stated in passage {pid}")
            continue
        if hit is None:
            updated[f"{key}_unit_status"] = "unit_unverified"
            notes.append(f"{key}={raw}: no unit evidence in passage {pid}")
            continue
        _write_canonical(updated, hit, pid, key)
    return updated, notes


def normalize_metric_observation(metric: Any, passage: str, pid: str) -> list[str]:
    """Split a mixed metric unit ("MW/400MWh") and verify the unit against the passage."""
    notes: list[str] = []
    unit = metric.unit
    if not isinstance(unit, str) or metric.metric_value is None:
        return notes
    quantities = passage_quantities(passage)
    embedded = EMBEDDED_UNIT.match(unit)
    if embedded:
        own_unit, other_value, other_unit = embedded.group(1), float(embedded.group(2)), embedded.group(3)
        metric.scope["unit_raw"] = unit
        metric.unit = own_unit
        hit = _find(quantities, other_value, unit=other_unit) or _additive(passage, other_value, other_unit)
        second = {"value": other_value, "unit": other_unit, "kind": _kind(other_unit),
                  "canonical": _canonical(other_value, other_unit)}
        if hit:
            second["evidence"] = {"passage_id": pid, "quote": hit["quote"]}
            if hit.get("operands"):
                second["operands"] = hit["operands"]
        else:
            second["status"] = "unit_unverified"
            notes.append(f"metric unit {unit!r}: second quantity not stated in passage {pid}")
        metric.scope["energy_mwh" if second["kind"] == "energy" else "power_mw"] = second
        unit = own_unit
    if unit.lower() not in {**POWER_UNITS, **ENERGY_UNITS}:
        return notes
    hit = _find(quantities, float(metric.metric_value), unit=unit) or _additive(passage, float(metric.metric_value), unit)
    if hit:
        kind_key = "energy_mwh" if hit["kind"] == "energy" else "power_mw"
        entry = {"value": hit["value"], "unit": hit["unit"], "kind": hit["kind"], "canonical": hit["canonical"],
                 "evidence": {"passage_id": pid, "quote": hit["quote"]}}
        if hit.get("operands"):
            entry["operands"] = hit["operands"]
        metric.scope.setdefault(kind_key, entry)
        return notes
    other = _find(quantities, float(metric.metric_value))
    if other is not None and other["unit"].lower() != unit.lower():
        # Passage pairs this number with a different unit (e.g. 100 MW, not 100 MWh):
        # correct with evidence and keep the model's unit on record.
        metric.scope["unit_review"] = {"status": "corrected_from_passage", "model_unit": unit,
                                       "passage_quote": other["quote"]}
        metric.unit = other["unit"]
        kind_key = "energy_mwh" if other["kind"] == "energy" else "power_mw"
        metric.scope[kind_key] = {"value": other["value"], "unit": other["unit"], "kind": other["kind"],
                                  "canonical": other["canonical"], "evidence": {"passage_id": pid, "quote": other["quote"]}}
        notes.append(f"metric {metric.metric_value} {unit}: passage states {other['quote']}; unit corrected")
        return notes
    metric.scope["unit_review"] = {"status": "unit_unverified", "model_unit": unit}
    notes.append(f"metric {metric.metric_value} {unit}: not stated in passage {pid}")
    return notes


def normalize_bundle_quantities(bundle: Any, passages: dict[str, str], warnings: list[str]) -> None:
    for group in ("facts", "events"):
        for index, item in enumerate(getattr(bundle, group)):
            values = item.metrics
            if not isinstance(values, dict) or (values.get("capacity") is None and values.get("volume") is None
                                                and not isinstance(values.get("unit"), str)):
                continue
            pid = item.evidence_locator.passage_id
            updated, notes = normalize_metrics_block(values, passages.get(pid, ""), pid)
            item.metrics = updated   # copy: the model-response dict is never mutated
            warnings.extend(f"{group}[{index}].metrics: {n}" for n in notes)
    for index, metric in enumerate(bundle.metrics):
        pid = metric.evidence_locator.passage_id
        notes = normalize_metric_observation(metric, passages.get(pid, ""), pid)
        warnings.extend(f"metrics[{index}]: {n}" for n in notes)
