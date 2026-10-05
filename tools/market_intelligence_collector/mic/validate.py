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
from mic.money import cny_amount_supported, normalize_bundle_amounts
from mic.quantity_units import normalize_bundle_quantities, repair_metric_value_strings
from mic.relation_evidence import quarantine_relations
from mic.evidence_review import EvidenceReview, date_supported, quantity_supported

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
    relation_reviews: list[dict] = field(default_factory=list)
    quality_reviews: list[dict] = field(default_factory=list)


class BundleValidator:
    def __init__(self, output_limits: dict, target_names: list[str] | None = None,
                 *, require_content_review: bool = True):
        self.limits = output_limits or {}
        # Disabling this is reserved for explicit legacy/offline replay. Every
        # current caller requires the shared semantic protocol by default.
        self.require_content_review = require_content_review
        # Set per run by the pipeline (canonical name + aliases of the current target).
        self.target_names: list[str] = list(target_names or [])

    def validate(self, raw: dict, passages: list[Passage]) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        try:
            bundle = BundleExtraction.model_validate(repair_metric_value_strings(raw, warnings))
        except ValidationError as exc:
            return ValidationReport(False, errors=[f"schema: {e['msg']}" for e in exc.errors()])

        if self.require_content_review or bundle.content_review:
            from mic.content_review import ContentReview, seal
            bundle = bundle.model_copy(deep=True)
            reviews = ContentReview(bundle, passages, warnings).apply()
            self._enforce_limits(bundle, warnings, truncate_brief=False)
            self._check_evidence(bundle, {p.passage_id for p in passages}, warnings)
            self._attach_excerpts(bundle, {p.passage_id: p.text for p in passages})
            seal(bundle)
            return ValidationReport(True, warnings=warnings, bundle=bundle, quality_reviews=reviews)

        strict = self.limits.get("strict_evidence_review") is True
        if strict:
            bundle = bundle.model_copy(deep=True)
        self._enforce_limits(bundle, warnings)
        self._normalize_relations(bundle, warnings)
        relation_reviews = quarantine_relations(bundle, passages, warnings)
        passage_text = {p.passage_id: p.text for p in passages}
        valid_pids = set(passage_text)
        self._check_evidence(bundle, valid_pids, warnings)
        review = EvidenceReview(bundle, passages, warnings, target_names=self.target_names) if strict else None
        if review:
            review.separate_prices()
        normalize_bundle_amounts(bundle, passage_text, warnings)
        normalize_bundle_quantities(bundle, passage_text, warnings)
        quality_reviews = review.apply() if review else []
        if strict:
            warnings[:] = [w.replace("; unchanged", "; held for review") for w in warnings]
        self._check_evidence_support(bundle, passage_text, warnings)
        self._attach_excerpts(bundle, passage_text)
        return ValidationReport(True, errors=errors, warnings=warnings,
                                bundle=bundle, relation_reviews=relation_reviews,
                                quality_reviews=quality_reviews)

    EXCERPT_CHARS = 600

    def _attach_excerpts(self, bundle: BundleExtraction, passage_text: dict[str, str]) -> None:
        """Copy the cited passage into each locator so saved records are reviewable.

        Only passages that exist in the model input are copied (``_check_evidence``
        already cleared unknown ids). A model-supplied ``excerpt`` is never
        trusted: it is replaced by the actual input text or cleared.
        """
        for attr in ("facts", "metrics", "events", "relations", "risks", "catalysts",
                     "customer_supplier_signals", "price_cost_margin_signals",
                     "policy_signals"):
            for item in getattr(bundle, attr):
                loc = getattr(item, "evidence_locator", None)
                if loc is None:
                    continue
                text = passage_text.get(loc.passage_id) if loc.passage_id else None
                loc.excerpt = text[: self.EXCERPT_CHARS] if text else None

    def _enforce_limits(self, bundle: BundleExtraction, warnings: list[str], *, truncate_brief=True) -> None:
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

        # Reviewed prose is assembled from whole claims: slicing can remove a
        # qualification or negation and change the approved meaning.
        if not truncate_brief:
            return
        max_chars = self.limits.get("max_summary_chars", 500)
        for field_name in ("what_happened", "why_it_matters", "one_sentence"):
            val = getattr(bundle.brief, field_name)
            if val and len(val) > max_chars:
                setattr(bundle.brief, field_name, val[:max_chars])

    def _check_evidence(self, bundle: BundleExtraction, valid_pids: set[str],
                       warnings: list[str]) -> None:
        if not valid_pids:
            return
        for attr in ("facts", "metrics", "events", "relations", "risks",
                     "customer_supplier_signals", "price_cost_margin_signals",
                     "policy_signals"):
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
            if unsupported(f, amt) and not cny_amount_supported(f.metrics, text_for(f) or ""):
                discount(f, f"fact amount {amt} not found in cited passage")
        for mtr in bundle.metrics:
            derived_supported = self.limits.get("strict_evidence_review") is True and quantity_supported(
                text_for(mtr) or "", mtr.metric_value, mtr.unit)
            if unsupported(mtr, mtr.metric_value) and not derived_supported:
                discount(mtr, f"metric value {mtr.metric_value} not found in cited passage")
        for e in bundle.events:
            cp = (e.entities or {}).get("counterparty")
            if cp and unsupported(e, cp):
                discount(e, f"event counterparty '{cp}' not found in cited passage")
            date_verified = self.limits.get("strict_evidence_review") is True and date_supported(
                text_for(e) or "", e.event_date)
            if e.event_date and unsupported(e, e.event_date) and not date_verified:
                discount(e, f"event_date '{e.event_date}' not found in cited passage")
        for r in bundle.relations:
            obj = r.object_entity.name if r.object_entity else None
            if obj and unsupported(r, obj):
                discount(r, f"relation object '{obj}' not found in cited passage")
        for cs in bundle.customer_supplier_signals:
            if cs.customer_or_supplier and unsupported(cs, cs.customer_or_supplier):
                discount(cs, f"signal counterparty '{cs.customer_or_supplier}' "
                             "not found in cited passage")
        for pcm in bundle.price_cost_margin_signals:
            if unsupported(pcm, pcm.value):
                discount(pcm, f"price/cost value {pcm.value} not found in cited passage")

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
