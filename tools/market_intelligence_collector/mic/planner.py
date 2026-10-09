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
from mic.task_questions import QUESTION_FAMILY, TaskQuestion, parse_task_questions

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# Fixed plan score for explicit task questions: above any template score so the plan
# order / records make the "asked for explicitly" origin visible.
QUESTION_SCORE = 200.0

# Source packs are another way to ask the same research question. In particular,
# a tender source-pack query must not use a second coverage slot after orders_tender.
_COVERAGE_GROUPS = {
    "source_pack:tender": "orders_tender",
    "source_pack:china_exchange": "official_ir",
    "source_pack:hk_exchange": "official_ir",
    "source_pack:us_filing": "official_ir",
    "source_pack:policy": "policy",
}


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

    def plan(self, profile: TargetProfile, task_profile: dict[str, Any],
             family_feedback: dict[str, float] | None = None, *,
             coverage_first: bool = False) -> list[PlannedQuery]:
        focus = task_profile.get("focus", [])
        budget = task_profile.get("budget_profile", {})
        max_queries = budget.get("max_queries", 80)

        # Explicit research questions come first: the caller asked for exactly these
        # searches, so they are not scored against templates and never dropped by the
        # score floor. They do consume the same query budget (no inflation).
        question_queries = self._expand_questions(profile, parse_task_questions(task_profile))
        question_queries = question_queries[:max_queries]
        taken = {_normalize_query(q.query_text) for q in question_queries}
        remaining = max_queries - len(question_queries)

        candidates = self._expand_families(profile, focus)
        candidates += self._expand_source_packs(profile, focus)
        candidates = [c for c in candidates if _normalize_query(c.query_text) not in taken]

        entity_terms = profile.all_entity_terms()
        scored = self._score_all(candidates, entity_terms)

        # Feedback loop (spec 22.2): families that historically produced
        # useful/correct objects get up-weighted, noisy ones down-weighted.
        if family_feedback:
            for q in scored:
                weight = family_feedback.get(q.query_family)
                if weight is not None and weight != 1.0:
                    q.score *= weight
                    q.why.append(f"历史反馈权重 x{weight}")

        # Keep the score floor and feedback. Browser runs reserve the first slots
        # for distinct research groups before adding same-group variants; prefixes
        # remain diverse if the search-hit budget stops execution early.
        eligible = [q for q in scored if q.score >= self.min_score]
        eligible.sort(key=lambda q: q.score, reverse=True)
        if coverage_first:
            heads, variants = [], []
            seen_groups: set[str] = set()
            for q in eligible:
                group = _COVERAGE_GROUPS.get(q.query_family, q.query_family)
                if group in seen_groups:
                    variants.append(q)
                else:
                    seen_groups.add(group)
                    q.why.append(f"覆盖优先：{group} 类最高分查询")
                    heads.append(q)
            return question_queries + (heads + variants)[:remaining]
        return question_queries + eligible[:remaining]

    # --- expansion ---------------------------------------------------------

    def _expand_questions(self, profile: TargetProfile,
                          questions: list[TaskQuestion]) -> list[PlannedQuery]:
        """One query per search phrase; the target name is prefixed when the phrase
        names no profile entity (so the engine still anchors on the target)."""
        entity_terms = [t for t in profile.all_entity_terms() if t]
        out: list[PlannedQuery] = []
        seen: set[str] = set()
        for qi, question in enumerate(questions, start=1):
            for phrase in question.search_phrases():
                text = phrase
                if not any(t.casefold() in phrase.casefold() for t in entity_terms):
                    text = f"{profile.primary_name} {phrase}".strip()
                norm = _normalize_query(text)
                if norm in seen:
                    continue
                seen.add(norm)
                why = [f"任务问题 {qi}：{question.question}"]
                if question.period:
                    why.append(f"报告期：{question.period}")
                why.append("显式检索词" if question.search_terms else "以问题原文作为检索词")
                out.append(PlannedQuery(
                    query_text=text, query_family=QUESTION_FAMILY, base_priority=100.0,
                    score=QUESTION_SCORE, why=why))
        return out

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
            if existing is None or q.score > existing.score:
                # Near-duplicates collapse to the single highest-scoring query;
                # the duplicate itself is dropped, the survivor keeps its score.
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
