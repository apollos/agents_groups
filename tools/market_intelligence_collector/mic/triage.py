"""Search Hit Triage (spec section 9).

Rule-first, model-free triage that decides read / link_record_only /
skip_for_now and whether a model call is warranted. The goal is to cut
downstream reads and model calls before any model is invoked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mic.config import MICConfig
from mic.profile import TargetProfile
from mic.schemas import SearchHit, TriageResult

_AMOUNT_RE = re.compile(r"\d+(\.\d+)?\s*(亿元|万元|亿|万吨|吨|GWh|MWh|%|个百分点)")


@dataclass
class TriageThresholds:
    read_threshold: float = 60.0
    record_threshold: float = 35.0
    model_lo: float = 40.0
    model_hi: float = 90.0


class SearchHitTriage:
    def __init__(self, config: MICConfig):
        self.config = config
        self.entity_terms: list[str] = []
        self.strong_fact_keywords = config.strong_fact_keywords or []
        self.source_type_by_domain = config.source_type_by_domain or {}
        gov = (config.call_governance or {}).get("triggers", {}).get("call_model_when", {})
        rng = gov.get("rule_score_between", [40, 90])
        self.thresholds = TriageThresholds(model_lo=rng[0], model_hi=rng[1])

    def for_profile(self, profile: TargetProfile) -> "SearchHitTriage":
        self.entity_terms = profile.all_entity_terms()
        return self

    def source_type(self, domain: str) -> str:
        for known_domain, stype in self.source_type_by_domain.items():
            if domain == known_domain or domain.endswith("." + known_domain):
                return stype
        return "media" if domain else "unknown"

    def triage(self, hit: SearchHit, source_link_id: str,
               is_duplicate: bool = False, seen_low_value: bool = False) -> TriageResult:
        text = f"{hit.title} {hit.snippet}"
        signals: list[str] = []
        score = 0.0

        matched_entities = [t for t in self.entity_terms if t and t in text]
        if matched_entities:
            signals.append("target_entity_match")
            score += 30 + 5 * min(len(matched_entities), 3)

        if _AMOUNT_RE.search(text):
            signals.append("amount_mentioned")
            score += 20

        matched_strong = [k for k in self.strong_fact_keywords if k in text]
        if matched_strong:
            signals.append("strong_fact_keyword")
            score += 6 * min(len(matched_strong), 4)

        for kw, sig in (("客户", "customer_keyword"), ("供应商", "supplier_keyword"),
                        ("订单", "order_keyword"), ("中标", "tender_keyword"),
                        ("政策", "policy_keyword"), ("处罚", "risk_keyword")):
            if kw in text:
                signals.append(sig)
                score += 6

        stype = self.source_type(hit.domain)
        if stype in ("official", "exchange", "regulator"):
            signals.append("high_credibility_source")
            score += 20
        elif stype == "company":
            score += 10

        # Rank: earlier results are stronger.
        score += max(0, 10 - hit.rank)

        # Source pack families are already curated for credibility.
        if hit.query_family and hit.query_family.startswith("source_pack:"):
            score += 8

        decision = "skip_for_now"
        if is_duplicate:
            decision = "link_record_only"
            signals.append("duplicate_url")
        elif score >= self.thresholds.read_threshold:
            decision = "read"
        elif score >= self.thresholds.record_threshold:
            decision = "link_record_only"

        # Call a model for anything we decide to read that clears the lower
        # value floor. A very high rule score never suppresses the call; it only
        # routes to a stronger (e.g. parallel-ensemble) policy downstream.
        need_model = decision == "read" and score >= self.thresholds.model_lo
        reason = self._reason(matched_entities, matched_strong, stype, score)

        return TriageResult(
            source_link_id=source_link_id, triage_decision=decision,
            read_priority=round(score, 2), matched_signals=signals,
            need_model=need_model, suggested_task="bundle_extraction", reason=reason,
        )

    @staticmethod
    def _reason(entities: list[str], strong: list[str], stype: str, score: float) -> str:
        bits = []
        if entities:
            bits.append(f"命中目标实体({', '.join(entities[:2])})")
        if strong:
            bits.append(f"含强事实词({', '.join(strong[:2])})")
        bits.append(f"来源类型={stype}")
        bits.append(f"score={score:.0f}")
        return "；".join(bits)
