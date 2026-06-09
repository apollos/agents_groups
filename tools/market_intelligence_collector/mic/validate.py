"""Local validation (spec section 17).

Validates raw model output BEFORE merge/persist:
  - Schema validity (parse into pydantic BundleExtraction, enforce limits)
  - Evidence locator validity (passage_id must exist in the input passages)
  - Relation direction normalization (A 向 B 供货 => A supplier_of B)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from mic.schemas import BundleExtraction, Passage

# Inverse pairs used to normalize relation direction.
_INVERSE = {
    "customer_of": "supplier_of",
    "supplier_of": "customer_of",
    "parent_of": "subsidiary_of",
    "subsidiary_of": "parent_of",
}


@dataclass
class ValidationReport:
    schema_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bundle: BundleExtraction | None = None


class BundleValidator:
    def __init__(self, output_limits: dict):
        self.limits = output_limits or {}

    def validate(self, raw: dict, passages: list[Passage]) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            bundle = BundleExtraction.model_validate(raw)
        except ValidationError as exc:
            return ValidationReport(False, errors=[f"schema: {e['msg']}" for e in exc.errors()])

        self._enforce_limits(bundle, warnings)
        passage_text = {p.passage_id: p.text for p in passages}
        valid_pids = set(passage_text)
        self._check_evidence(bundle, valid_pids, warnings)
        self._check_evidence_support(bundle, passage_text, warnings)
        self._normalize_relations(bundle, warnings)

        return ValidationReport(True, errors=errors, warnings=warnings, bundle=bundle)

    def _enforce_limits(self, bundle: BundleExtraction, warnings: list[str]) -> None:
        caps = {
            "facts": self.limits.get("max_facts", 10),
            "metrics": self.limits.get("max_metrics", 10),
            "events": self.limits.get("max_events", 5),
            "relations": self.limits.get("max_relations", 10),
            "risks": self.limits.get("max_risks", 5),
            "catalysts": self.limits.get("max_catalysts", 5),
            "customer_supplier_signals": self.limits.get("max_signals", 10),
            "price_cost_margin_signals": self.limits.get("max_signals", 10),
            "policy_signals": self.limits.get("max_signals", 10),
            "analyst_questions": self.limits.get("max_questions", 8),
        }
        for attr, cap in caps.items():
            items = getattr(bundle, attr)
            if len(items) > cap:
                warnings.append(f"{attr} exceeded limit {cap}, truncated")
                setattr(bundle, attr, items[:cap])

        max_chars = self.limits.get("max_summary_chars", 500)
        for field_name in ("what_happened", "why_it_matters", "one_sentence"):
            val = getattr(bundle.brief, field_name)
            if val and len(val) > max_chars:
                setattr(bundle.brief, field_name, val[:max_chars])

    def _check_evidence(self, bundle: BundleExtraction, valid_pids: set[str],
                       warnings: list[str]) -> None:
        if not valid_pids:
            return
        for attr in ("facts", "metrics", "events", "relations", "risks"):
            for item in getattr(bundle, attr):
                loc = getattr(item, "evidence_locator", None)
                if loc is None:
                    continue
                pid = loc.passage_id
                if pid and pid not in valid_pids:
                    warnings.append(f"{attr} evidence passage_id '{pid}' not in input; cleared")
                    loc.passage_id = None
                    # Lower confidence for unverifiable evidence.
                    if getattr(item, "confidence", 0.0) > 0.3:
                        item.confidence = round(item.confidence * 0.7, 3)

    def _check_evidence_support(self, bundle: BundleExtraction,
                               passage_text: dict[str, str], warnings: list[str]) -> None:
        """Verify cited values actually appear in the referenced passage (spec 17.2).

        Checks amounts/dates/counterparties against the cited passage text. When a
        claimed value cannot be found, the item's confidence is discounted rather
        than dropped, since paraphrasing can legitimately hide an exact token.
        """
        if not passage_text:
            return

        def text_for(item) -> str | None:
            loc = getattr(item, "evidence_locator", None)
            pid = getattr(loc, "passage_id", None) if loc else None
            return passage_text.get(pid) if pid else None

        def unsupported(item, token) -> bool:
            if token in (None, "", 0):
                return False
            txt = text_for(item)
            if txt is None:
                return False
            token_str = str(token).rstrip("0").rstrip(".") if isinstance(token, float) \
                else str(token)
            return token_str not in txt and str(token) not in txt

        def discount(item, why: str) -> None:
            warnings.append(why)
            if getattr(item, "confidence", 0.0) > 0.25:
                item.confidence = round(item.confidence * 0.6, 3)

        for f in bundle.facts:
            amt = (f.metrics or {}).get("amount")
            if unsupported(f, amt):
                discount(f, f"fact amount {amt} not found in cited passage")
        for mtr in bundle.metrics:
            if unsupported(mtr, mtr.metric_value):
                discount(mtr, f"metric value {mtr.metric_value} not found in cited passage")
        for e in bundle.events:
            cp = (e.entities or {}).get("counterparty")
            if cp and unsupported(e, cp):
                discount(e, f"event counterparty '{cp}' not found in cited passage")
            if e.event_date and unsupported(e, e.event_date):
                discount(e, f"event_date '{e.event_date}' not found in cited passage")
        for r in bundle.relations:
            obj = r.object_entity.name if r.object_entity else None
            if obj and unsupported(r, obj):
                discount(r, f"relation object '{obj}' not found in cited passage")

    def _normalize_relations(self, bundle: BundleExtraction, warnings: list[str]) -> None:
        for rel in bundle.relations:
            rt = (rel.relation_type or "").strip()
            # Normalize a few common phrasings if a model returned free text.
            if "向" in rt and "供货" in rt:
                rel.relation_type = "supplier_of"
            elif "采购" in rt or "从" in rt:
                rel.relation_type = "customer_of"
            if rel.relation_type not in (
                "customer_of", "supplier_of", "competitor_of", "partner_of",
                "distributor_of", "contractor_of", "project_owner_of",
                "regulator_of", "investor_of", "subsidiary_of", "parent_of",
                "project_participant_of", "product_of", "facility_of", "brand_of",
            ):
                warnings.append(f"relation_type '{rel.relation_type}' not in enum")
