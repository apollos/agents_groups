"""Query Planner (spec section 8).

Generates candidate queries from query families + source packs, fills template
placeholders from the Target Profile, scores each query, deduplicates, and
returns a budget-limited, ranked plan. Goal is analyst-relevant coverage with
controlled model-call volume, not maximum query count.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any

from mic.config import MICConfig
from mic.profile import TargetProfile

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


@dataclass
class PlannedQuery:
    query_text: str
    query_family: str
    base_priority: float
    score: float = 0.0
    why: list[str] = field(default_factory=list)
    language: str = "zh"
    region: str = "中国"
    source_pack: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "query_text": self.query_text,
            "query_family": self.query_family,
            "priority_score": round(self.score, 2),
            "language": self.language,
            "region": self.region,
            "expected_value_reason": {"why": self.why, "source_pack": self.source_pack},
        }


class QueryPlanner:
    def __init__(self, config: MICConfig):
        self.config = config
        self.scoring = config.query_scoring or {}
        self.weights = self.scoring.get("weights", {})
        self.fact_keywords = self.scoring.get("concrete_fact_keywords", [])
        self.min_score = self.scoring.get("min_score_to_execute", 45)

    # --- public ------------------------------------------------------------

    def plan(self, profile: TargetProfile, task_profile: dict[str, Any]) -> list[PlannedQuery]:
        focus = task_profile.get("focus", [])
        budget = task_profile.get("budget_profile", {})
        max_queries = budget.get("max_queries", 80)

        candidates = self._expand_families(profile, focus)
        candidates += self._expand_source_packs(profile, focus)

        entity_terms = profile.all_entity_terms()
        scored = self._score_all(candidates, entity_terms)

        # Filter low-value, sort by score, cap by budget.
        eligible = [q for q in scored if q.score >= self.min_score]
        eligible.sort(key=lambda q: q.score, reverse=True)
        return eligible[:max_queries]

    # --- expansion ---------------------------------------------------------

    def _selected_families(self, focus: list[str]) -> dict[str, dict]:
        families = self.config.query_families.get("families", {})
        if not focus:
            return families
        taxonomy = self.config.analyst_taxonomy.get("focus_areas", {})
        wanted: set[str] = set()
        for f in focus:
            wanted.update(taxonomy.get(f, {}).get("families", []))
        # Always allow families whose own 'focus' intersects the requested focus.
        selected = {}
        for name, fam in families.items():
            if name in wanted or set(fam.get("focus", [])) & set(focus):
                selected[name] = fam
        return selected or families

    def _expand_families(self, profile: TargetProfile, focus: list[str]) -> list[PlannedQuery]:
        values = profile.placeholder_values()
        out: list[PlannedQuery] = []
        for name, fam in self._selected_families(focus).items():
            base_priority = float(fam.get("base_priority", 50))
            for template in fam.get("templates", []):
                for text in self._fill_template(template, values):
                    out.append(PlannedQuery(
                        query_text=text, query_family=name, base_priority=base_priority,
                    ))
        return out

    def _expand_source_packs(self, profile: TargetProfile, focus: list[str]) -> list[PlannedQuery]:
        values = profile.placeholder_values()
        out: list[PlannedQuery] = []
        for name, pack in self.config.source_packs.get("packs", {}).items():
            base_priority = float(pack.get("base_priority", 80))
            for template in pack.get("templates", []):
                for text in self._fill_template(template, values):
                    out.append(PlannedQuery(
                        query_text=text, query_family=f"source_pack:{name}",
                        base_priority=base_priority, source_pack=name,
                    ))
        return out

    @staticmethod
    def _fill_template(template: str, values: dict[str, list[str]]) -> list[str]:
        """Expand a template into concrete queries. Skip if any placeholder unfilled.

        For templates with multiple multi-valued placeholders we take a bounded
        cartesian product to avoid query explosion.
        """
        placeholders = _PLACEHOLDER_RE.findall(template)
        if not placeholders:
            return [template]
        choices = []
        for ph in placeholders:
            vals = values.get(ph, [])
            if not vals:
                return []  # cannot fill -> skip template entirely
            choices.append(vals[:3])  # bound each placeholder
        results = []
        for combo in itertools.product(*choices):
            text = template
            for ph, val in zip(placeholders, combo, strict=True):
                text = text.replace(f"{{{ph}}}", val, 1)
            results.append(text)
            if len(results) >= 6:
                break
        return results

    # --- scoring (spec 8.3) ------------------------------------------------

    def _score_all(self, candidates: list[PlannedQuery],
                  entity_terms: list[str]) -> list[PlannedQuery]:
        seen_normalized: dict[str, PlannedQuery] = {}
        for q in candidates:
            norm = _normalize_query(q.query_text)
            self._score_one(q, entity_terms)
            existing = seen_normalized.get(norm)
            if existing is None:
                seen_normalized[norm] = q
            else:
                # Duplicate query: keep the higher base, penalize duplication.
                if q.score > existing.score:
                    q.score -= self.weights.get("duplication_penalty", 25.0)
                    q.why.append("near-duplicate of existing query")
                    seen_normalized[norm] = q
        return list(seen_normalized.values())

    def _score_one(self, q: PlannedQuery, entity_terms: list[str]) -> None:
        w = self.weights
        why: list[str] = []
        score = q.base_priority * w.get("topic_priority", 1.0)

        matched_entities = [t for t in entity_terms if t and t in q.query_text]
        if matched_entities:
            score += w.get("entity_match", 12.0) * min(len(matched_entities), 3)
            why.append(f"包含目标实体: {', '.join(matched_entities[:3])}")

        if any(k in q.query_text for k in self.fact_keywords):
            score += w.get("concrete_fact_expectation", 8.0)
            why.append("可能产生具体事实")

        if q.source_pack:
            score += w.get("source_credibility_expectation", 6.0)
            why.append(f"高可信来源包: {q.source_pack}")

        # Time sensitivity heuristic.
        if any(k in q.query_text for k in ("公告", "中标", "涨价", "处罚", "投产", "业绩")):
            score += w.get("time_sensitivity", 5.0)
            why.append("时间敏感")

        q.score = score
        q.why = why


def _normalize_query(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())
