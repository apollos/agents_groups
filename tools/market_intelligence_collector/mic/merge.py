"""Multi-model result merging (spec section 14).

Merges multiple validated BundleExtraction outputs for one source link into a
single merged bundle plus merge metadata. Merging is per-object-type, not a
naive vote: decisions use weighted vote, scores use weighted median, and each
object type dedups on its key fields. Field-level conflicts are recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mic.config import MICConfig
from mic.schemas import (
    BundleExtraction,
    CatalystItem,
    EventCard,
    FactItem,
    MetricObservation,
    RelationRecord,
    RiskFlag,
)

# Inverse relation pairs used to detect ordered-pair direction conflicts.
_INVERSE = {
    "customer_of": "supplier_of",
    "supplier_of": "customer_of",
    "parent_of": "subsidiary_of",
    "subsidiary_of": "parent_of",
}


@dataclass
class ModelContribution:
    model_config_id: str
    provider: str
    bundle: BundleExtraction
    schema_validity_score: float = 1.0
    evidence_locator_score: float = 1.0
    historical_feedback_score: float = 1.0
    configured_weight: float = 1.0
    task_weight: float = 1.0
    effective_weight: float = 0.0


@dataclass
class MergeResult:
    bundle: BundleExtraction
    disagreement_level: str
    merge_method: str
    field_conflicts: list[dict] = field(default_factory=list)
    model_outputs: list[dict] = field(default_factory=list)


class MultiModelMerger:
    def __init__(self, config: MICConfig):
        self.config = config
        mp = config.merge_policy or {}
        self.decision_values = mp.get("decision_merge", {}).get("decision_values", {
            "save_structured": 1.0, "link_only": 0.5, "skip": 0.0})
        self.rules = mp.get("rules", {})
        ew = mp.get("effective_weight", {})
        self.family_map = ew.get("provider_family_independence", {})
        self.correlated_discount = ew.get("correlated_family_discount", 0.6)

    # --- entry -------------------------------------------------------------

    def merge(self, source_link_id: str, target_id: str,
              contributions: list[ModelContribution],
              feedback_scores: dict[str, float] | None = None) -> MergeResult:
        contributions = [c for c in contributions if c.bundle is not None]
        if not contributions:
            empty = BundleExtraction(source_link_id=source_link_id, decision="skip")
            return MergeResult(empty, "low", "none")

        # Apply historical feedback to per-model weight (spec 14.2 + 22.2).
        if feedback_scores:
            for c in contributions:
                c.historical_feedback_score = feedback_scores.get(c.model_config_id, 1.0)

        self._compute_effective_weights(contributions)

        if len(contributions) == 1:
            c = contributions[0]
            c.bundle.source_link_id = source_link_id
            c.bundle.relations = self._sanitize_relations(c.bundle.relations)
            self._apply_decision_rules(c.bundle)
            return MergeResult(
                c.bundle, "low", "single_model",
                model_outputs=[self._trace(c)],
            )

        decision, decision_score = self._merge_decision(contributions)
        overall = self._weighted_median(
            [(c.bundle.overall_score, c.effective_weight) for c in contributions])
        confidence = self._weighted_median(
            [(c.bundle.confidence, c.effective_weight) for c in contributions])

        facts = self._merge_facts(contributions)
        metrics = self._merge_metrics(contributions)
        events, event_conflicts = self._merge_events(contributions)
        relations, rel_conflicts = self._merge_relations(contributions)
        risks = self._merge_risks(contributions)
        catalysts = self._merge_catalysts(contributions)
        cs_signals = self._merge_signals(
            contributions, "customer_supplier_signals",
            lambda s: f"{s.signal_type}|{_norm(s.customer_or_supplier)}|{_norm(s.product or '')}")
        pcm_signals = self._merge_signals(
            contributions, "price_cost_margin_signals",
            lambda s: f"{s.signal_type}|{_norm(s.product_or_material)}|{s.period}")
        policy_signals = self._merge_signals(
            contributions, "policy_signals",
            lambda s: f"{s.policy_type}|{_norm(s.issuer)}|{_norm(s.summary)}")
        questions = [q for c in contributions for q in c.bundle.analyst_questions]
        gaps = self._dedup_gaps(
            [g for c in contributions for g in c.bundle.coverage_gaps])
        brief = max(contributions, key=lambda c: c.effective_weight).bundle.brief
        source_quality = max(contributions,
                            key=lambda c: c.effective_weight).bundle.source_quality

        conflicts = event_conflicts + rel_conflicts
        disagreement = self._disagreement_level(contributions, decision_score, conflicts)

        merged = BundleExtraction(
            source_link_id=source_link_id, decision=decision,
            overall_score=round(overall, 1), confidence=round(confidence, 3),
            source_quality=source_quality, brief=brief, facts=facts, metrics=metrics,
            events=events, relations=relations, risks=risks, catalysts=catalysts,
            customer_supplier_signals=cs_signals, price_cost_margin_signals=pcm_signals,
            policy_signals=policy_signals,
            analyst_questions=self._dedup_questions(questions)[:8],
            coverage_gaps=gaps[:8],
        )
        self._apply_decision_rules(merged)
        return MergeResult(
            merged, disagreement, "weighted_merge",
            field_conflicts=conflicts,
            model_outputs=[self._trace(c) for c in contributions],
        )

    def _apply_decision_rules(self, bundle: BundleExtraction) -> None:
        """Apply configured decision thresholds after score aggregation.

        The merge policy has two gates for ``save_structured``: a weighted
        decision score and a minimum overall score. The original code applied
        only the decision-score gate, so a low-quality single model output could
        still be persisted as structured data. Downgrading to ``link_only`` keeps
        the source traceable while preventing weak structured objects from being
        treated as analyst-grade facts.
        """
        save_rule = self.rules.get("save_structured", {})
        min_overall = save_rule.get("min_overall_score")
        if (bundle.decision == "save_structured" and min_overall is not None
                and bundle.overall_score < float(min_overall)):
            bundle.decision = "link_only"
        if bundle.decision == "link_only":
            self._clear_structured_objects(bundle)

    @staticmethod
    def _clear_structured_objects(bundle: BundleExtraction) -> None:
        """Keep a link-only result from leaking weak structured claims.

        ``link_only`` means the source should stay traceable, but its extracted
        facts/events/relations/signals should not enter analyst-grade tables.
        We intentionally keep brief/questions/gaps because they help explain why
        the source was retained and what should be checked next.
        """
        for attr in (
            "facts", "metrics", "events", "relations", "risks", "catalysts",
            "customer_supplier_signals", "price_cost_margin_signals", "policy_signals",
        ):
            setattr(bundle, attr, [])

    # --- effective weight (spec 14.2) -------------------------------------

    def _compute_effective_weights(self, contributions: list[ModelContribution]) -> None:
        family_counts: dict[str, int] = {}
        for c in contributions:
            fam = self.family_map.get(c.provider, c.provider)
            family_counts[fam] = family_counts.get(fam, 0) + 1
        for c in contributions:
            fam = self.family_map.get(c.provider, c.provider)
            independence = 1.0 if family_counts.get(fam, 1) == 1 else self.correlated_discount
            c.effective_weight = (
                c.configured_weight * c.task_weight * c.schema_validity_score
                * c.evidence_locator_score * max(c.bundle.confidence, 0.05)
                * c.historical_feedback_score * independence
            )

    # --- decision + score merge -------------------------------------------

    def _merge_decision(self, contributions: list[ModelContribution]) -> tuple[str, float]:
        num = sum(self.decision_values.get(c.bundle.decision, 0.0) * c.effective_weight
                  for c in contributions)
        den = sum(c.effective_weight for c in contributions) or 1.0
        score = num / den
        save_rule = self.rules.get("save_structured", {})
        link_rule = self.rules.get("link_only", {})
        if score >= save_rule.get("min_decision_score", 0.65):
            return "save_structured", score
        if score >= link_rule.get("min_decision_score", 0.35):
            return "link_only", score
        return "skip", score

    @staticmethod
    def _weighted_median(pairs: list[tuple[float, float]]) -> float:
        pairs = [(v, w) for v, w in pairs if v is not None and w > 0]
        if not pairs:
            return 0.0
        pairs.sort(key=lambda x: x[0])
        total = sum(w for _, w in pairs)
        acc = 0.0
        for v, w in pairs:
            acc += w
            if acc >= total / 2:
                return v
        return pairs[-1][0]

    # --- object merging ----------------------------------------------------

    def _merge_facts(self, contributions: list[ModelContribution]) -> list[FactItem]:
        seen: dict[str, FactItem] = {}
        for c in contributions:
            for f in c.bundle.facts:
                key = f"{f.fact_type}|{_norm(f.fact_statement)}|{f.period}"
                if key not in seen or f.confidence > seen[key].confidence:
                    seen[key] = f
        return list(seen.values())

    def _merge_metrics(self, contributions: list[ModelContribution]) -> list[MetricObservation]:
        seen: dict[str, MetricObservation] = {}
        for c in contributions:
            for mtr in c.bundle.metrics:
                key = f"{mtr.metric_name}|{_norm(str(mtr.scope))}|{mtr.period}"
                if key not in seen or mtr.confidence > seen[key].confidence:
                    seen[key] = mtr
        return list(seen.values())

    def _merge_events(self, contributions: list[ModelContribution]
                      ) -> tuple[list[EventCard], list[dict]]:
        clusters: dict[str, list[tuple[EventCard, float]]] = {}
        for c in contributions:
            for e in c.bundle.events:
                key = f"{e.event_type}|{_norm(e.entities.get('counterparty',''))}"
                clusters.setdefault(key, []).append((e, c.effective_weight))

        merged: list[EventCard] = []
        conflicts: list[dict] = []
        for key, members in clusters.items():
            base = max(members, key=lambda x: x[1])[0].model_copy(deep=True)
            # Amount: weighted median across members; mark conflict if they differ
            # (spec 14.4: 单位标准化后加权中位数 + keep_all_and_mark_conflict).
            amount_pairs = [(m[0].metrics.get("amount"), m[1]) for m in members
                            if m[0].metrics.get("amount") is not None]
            distinct_amounts = {a for a, _ in amount_pairs}
            if amount_pairs:
                base.metrics["amount"] = self._weighted_median(amount_pairs)
            if len(distinct_amounts) > 1:
                conflicts.append({"object": "event", "key": key, "field": "amount",
                                  "values": sorted(distinct_amounts),
                                  "merged_amount": base.metrics.get("amount"),
                                  "resolution": "keep_all_and_mark_conflict"})
                base.source_corroboration_status = "conflicting"
            # Impact direction vote.
            dirs = [m[0].impact.direction for m in members]
            if len(set(dirs)) > 1:
                base.impact.direction = "mixed"
                conflicts.append({"object": "event", "key": key,
                                  "field": "impact_direction", "values": dirs,
                                  "resolution": "mark_mixed_or_unclear"})
            # Note: source_corroboration_status stays single_source here even when
            # several MODELS agree — corroboration means independent SOURCES
            # (different links/publishers), not multiple models reading the same
            # article (spec 13.4). Cross-source upgrade is a higher-level concern.
            base.confidence = self._weighted_median([(m[0].confidence, m[1]) for m in members])
            merged.append(base)
        return merged, conflicts

    @staticmethod
    def _sanitize_relations(relations: list[RelationRecord]) -> list[RelationRecord]:
        """Drop vague-entity relations and collapse alias duplicates.

        Used on the single-model path, which bypasses ``_merge_relations``;
        the multi-model path applies the same rules inline during merging.
        """
        seen: dict[str, RelationRecord] = {}
        for r in relations:
            if r.subject_entity.is_vague() or r.object_entity.is_vague():
                continue
            key = (f"{r.subject_entity.identity_key()}|{r.relation_type}|"
                   f"{r.object_entity.identity_key()}|"
                   f"{_norm(str(r.qualifiers.get('product', '')))}")
            if key not in seen or r.confidence > seen[key].confidence:
                seen[key] = r
        return list(seen.values())

    def _merge_relations(self, contributions: list[ModelContribution]
                         ) -> tuple[list[RelationRecord], list[dict]]:
        seen: dict[str, RelationRecord] = {}
        conflicts: list[dict] = []
        # Direction conflict (spec 14.5): for the SAME ordered (subject, object,
        # product), one model asserts a relation while another asserts its inverse
        # (e.g. company supplier_of client vs company customer_of client).
        # Entities dedup on identity_key (ticker over name spelling) so that
        # alias spellings of the same company collapse into one relation, and
        # vague collective entities ("多家锂电设备商") are dropped outright.
        ordered: dict[tuple[str, str, str], set[str]] = {}
        for c in contributions:
            for r in c.bundle.relations:
                if r.subject_entity.is_vague() or r.object_entity.is_vague():
                    continue
                subj = r.subject_entity.identity_key()
                obj = r.object_entity.identity_key()
                product = _norm(str(r.qualifiers.get("product", "")))
                key = f"{subj}|{r.relation_type}|{obj}|{product}"
                if key not in seen or r.confidence > seen[key].confidence:
                    seen[key] = r
                if subj and obj:
                    ordered.setdefault((subj, obj, product), set()).add(r.relation_type)

        conflicting_keys: set[str] = set()
        for (subj, obj, product), rels in ordered.items():
            for rt in rels:
                inv = _INVERSE.get(rt)
                if inv and inv in rels:
                    conflicts.append({
                        "object": "relation",
                        "subject": subj, "object_name": obj, "product": product,
                        "field": "relation_direction", "values": sorted(rels),
                        "resolution": "require_arbitration",
                    })
                    for rt2 in (rt, inv):
                        conflicting_keys.add(f"{subj}|{rt2}|{obj}|{product}")
                    break

        for key in conflicting_keys:
            if key in seen:
                seen[key].qualifiers = dict(seen[key].qualifiers or {})
                seen[key].qualifiers["conflict"] = "relation_direction_conflict"
                seen[key].qualifiers["resolution_status"] = "needs_arbitration"
        return list(seen.values()), conflicts

    @staticmethod
    def _merge_signals(contributions: list[ModelContribution], attr: str, key_fn):
        seen: dict = {}
        for c in contributions:
            for s in getattr(c.bundle, attr):
                key = key_fn(s)
                if key not in seen or s.confidence > seen[key].confidence:
                    seen[key] = s
        return list(seen.values())

    def _merge_risks(self, contributions: list[ModelContribution]) -> list[RiskFlag]:
        seen: dict[str, RiskFlag] = {}
        for c in contributions:
            for r in c.bundle.risks:
                key = f"{r.risk_type}|{_norm(r.risk_summary)}"
                if key not in seen or r.confidence > seen[key].confidence:
                    seen[key] = r
        return list(seen.values())

    def _merge_catalysts(self, contributions: list[ModelContribution]) -> list[CatalystItem]:
        seen: dict[str, CatalystItem] = {}
        for c in contributions:
            for cat in c.bundle.catalysts:
                key = f"{cat.catalyst_type}|{cat.expected_date}|{_norm(cat.description)}"
                if key not in seen:
                    seen[key] = cat
        return list(seen.values())

    @staticmethod
    def _dedup_questions(questions: list) -> list:
        seen, out = set(), []
        for q in questions:
            k = _norm(q.question)
            if k and k not in seen:
                seen.add(k)
                out.append(q)
        return out

    @staticmethod
    def _dedup_gaps(gaps: list) -> list:
        seen, out = set(), []
        for g in gaps:
            k = f"{g.gap_type}|{_norm(g.description)}"
            if k not in seen:
                seen.add(k)
                out.append(g)
        return out

    # --- diagnostics -------------------------------------------------------

    def _disagreement_level(self, contributions: list[ModelContribution],
                           decision_score: float, conflicts: list[dict]) -> str:
        decisions = {c.bundle.decision for c in contributions}
        if conflicts or len(decisions) > 1:
            confs = [c.bundle.confidence for c in contributions]
            spread = (max(confs) - min(confs)) if confs else 0
            if len(conflicts) >= 2 or spread > 0.4:
                return "high"
            return "medium"
        return "low"

    @staticmethod
    def _trace(c: ModelContribution) -> dict[str, Any]:
        return {
            "model_config_id": c.model_config_id,
            "provider": c.provider,
            "decision": c.bundle.decision,
            "confidence": c.bundle.confidence,
            "overall_score": c.bundle.overall_score,
            "effective_weight": round(c.effective_weight, 4),
            "counts": {
                "facts": len(c.bundle.facts), "metrics": len(c.bundle.metrics),
                "events": len(c.bundle.events), "relations": len(c.bundle.relations),
                "risks": len(c.bundle.risks),
            },
        }


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()
