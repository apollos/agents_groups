"""Business-event identity for structured_events (Codex review F3, 2026-10-04).

The persistence key used to be (target, event_type, event_date, hash(summary)).
Two media copies of the same award — reworded, or labelled ``tender`` by one
model call and ``major_order`` by the next — therefore became two "new" events.

A business event is identified by *who did what, at what size*:

* ``subject``   normalised ``entities.subject`` (company suffixes and spacing removed)
* ``family``    the action family of ``event_type`` (award, capacity, price, ...)
* quantities    capacity in MWh and/or amount in 万元, parsed from the reviewed
                metrics block (canonical, raw and pending-review candidates)

Two source rows describe the same business event when subject and family match,
every quantity known on both sides is equal, at least one quantity is shared (or,
lacking any quantity, the product matches), known event dates agree and the
publications fall in the same news cycle. Anything less is kept apart; a row
without subject or family is ``unresolved`` and is never merged. The rule is
deliberately conservative: hard-merging different lots or projects is a worse
failure than one duplicate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

ACTION_FAMILY = {
    "major_order": "award", "tender": "award", "customer_order": "award", "order": "award",
    "contract": "award", "bid": "award", "contract_award": "award", "tender_result": "award",
    "capacity_change": "capacity", "capacity_commissioning": "capacity",
    "price_change": "price",
    "policy_change": "policy",
    "customer_change": "relationship", "supplier_change": "relationship",
    "financing": "financing", "mna": "mna", "product_launch": "product",
    "management_change": "management", "earnings_change": "earnings", "risk_event": "risk",
}
SAME_NEWS_CYCLE_DAYS = 45
COMPANY_SUFFIX = re.compile(r"(股份有限公司|有限责任公司|有限公司|集团公司|集团|公司|股份|\(.*?\)|（.*?）)")
NUMBER = r"\d+(?:\.\d+)?"


def _norm(text: Any) -> str:
    value = re.sub(r"\s+", "", str(text or "")).lower()
    value = COMPANY_SUFFIX.sub("", value)
    return re.sub(r"[·•・,，。；;:：/／\-—_]", "", value)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(rf"(?<![\d.])({NUMBER})", str(value).replace(",", ""))
    return float(match.group(1)) if match else None


def capacity_mwh(metrics: dict[str, Any]) -> float | None:
    for key in ("capacity", "volume"):
        raw = metrics.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        text = str(raw)
        match = re.search(rf"({NUMBER})\s*MWh", text, re.I)
        if match:
            return float(match.group(1))
        match = re.search(rf"({NUMBER})\s*GWh", text, re.I)
        if match:
            return float(match.group(1)) * 1000
    return None


def amount_wan(metrics: dict[str, Any]) -> float | None:
    """Amount in 万元 from the reviewed metrics block; None when the unit is not certain."""
    if metrics.get("amount") is not None and metrics.get("amount_unit") == "元":
        return round(float(metrics["amount"]) / 10000, 3)
    if metrics.get("amount_raw") is not None and metrics.get("amount_raw_unit") == "万元":
        return round(float(metrics["amount_raw"]), 3)
    candidate = metrics.get("amount_candidate", metrics.get("amount"))
    value = _num(candidate)
    if value is None:
        return None
    unit_text = " ".join(str(metrics.get(k) or "") for k in ("currency", "unit", "amount_unit", "amount_raw_unit"))
    if "亿元" in unit_text:
        return round(value * 10000, 3)
    if "万元" in unit_text:
        return round(value, 3)
    return None


@dataclass(frozen=True)
class EventSignature:
    subject: str
    family: str
    capacity_mwh: float | None
    amount_wan: float | None
    product: str
    event_date: str | None
    published_at: str | None

    @property
    def business_key(self) -> str:
        return f"{self.subject}|{self.family}"

    def compatible_with(self, other: "EventSignature") -> bool:
        if self.business_key != other.business_key:
            return False
        shared = 0
        for mine, theirs in ((self.capacity_mwh, other.capacity_mwh), (self.amount_wan, other.amount_wan)):
            if mine is not None and theirs is not None:
                if abs(mine - theirs) > 0.005 * max(abs(mine), abs(theirs), 1.0):
                    return False
                shared += 1
        if shared == 0 and not (self.product and self.product == other.product
                                and self.capacity_mwh is None and other.capacity_mwh is None
                                and self.amount_wan is None and other.amount_wan is None):
            return False
        if self.event_date and other.event_date and self.event_date[:10] != other.event_date[:10]:
            return False
        if self.published_at and other.published_at:
            gap = _days_between(self.published_at, other.published_at)
            if gap is not None and gap > SAME_NEWS_CYCLE_DAYS:
                return False
        return True


def _days_between(a: str, b: str) -> int | None:
    try:
        da, db = datetime.fromisoformat(a[:19]), datetime.fromisoformat(b[:19])
    except ValueError:
        return None
    return abs((da - db).days)


def signature(event: dict[str, Any], *, published_at: str | None) -> EventSignature | None:
    entities = event.get("entities") or {}
    metrics = event.get("metrics") or {}
    subject = _norm(entities.get("subject"))
    family = ACTION_FAMILY.get(str(event.get("event_type") or ""), str(event.get("event_type") or ""))
    if not subject or not family or family in ("other", "unknown"):
        return None
    return EventSignature(
        subject=subject, family=family,
        capacity_mwh=capacity_mwh(metrics), amount_wan=amount_wan(metrics),
        product=_norm(entities.get("product")),
        event_date=event.get("event_date") or event.get("date"),
        published_at=published_at,
    )
