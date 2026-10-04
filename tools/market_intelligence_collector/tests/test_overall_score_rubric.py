"""Acceptance follow-up (2026-10-04, batch v1 → v2): overall_score must be defined.

Batch v1 of the real acceptance run showed the class-wide loss: every concrete,
in-window, evidence-backed contract award republished by industry media scored
62–66 from the model (``decision=save_structured`` but ``overall_score<70``),
because the prompt described ``overall_score`` only as ``"0-100"``. The gate
(``merge_policy.rules.save_structured.min_overall_score=70``) therefore acted
on an uncalibrated number.

The fix defines the scale in the stable system prompt: content materiality,
explicitly separated from source credibility (which has its own fields). The
threshold itself is unchanged; these tests pin the rubric and guard against
drift between the prompt and ``merge_policy.yaml``.
"""

from __future__ import annotations

import json

from mic.merge import MultiModelMerger
from mic.modeling.prompts import (
    OVERALL_SCORE_ADMISSION_THRESHOLD,
    SCHEMA_HINT,
    SYSTEM_PROMPT,
    build_arbitration_messages,
    build_bundle_messages,
)
from mic.profile import TargetProfile
from mic.schemas import BundleExtraction, EventCard, FactItem


def _profile() -> TargetProfile:
    return TargetProfile.from_config(
        {"target_id": "company_300750", "type": "company", "canonical_name": "宁德时代"}
    )


def test_system_prompt_defines_overall_score_scale_and_separates_source_credibility():
    assert "overall_score（0-100）" in SYSTEM_PROMPT
    # Four bands, so the model is not left to invent its own scale.
    for band in ("85-100", "70-84", "50-69", "0-49"):
        assert band in SYSTEM_PROMPT, band
    # Materiality, not source reputation: credibility and corroboration have their own fields.
    assert "不是来源信誉分" in SYSTEM_PROMPT
    assert "source_quality.source_credibility_score" in SYSTEM_PROMPT
    assert "source_corroboration_status" in SYSTEM_PROMPT
    # Media republication of a public notice with complete key elements is explicitly ≥70 material.
    assert "媒体或行业站转载" in SYSTEM_PROMPT
    # The schema hint points at the rubric instead of a bare range.
    assert "0-100" in SCHEMA_HINT["overall_score"]
    assert "不是来源信誉分" in SCHEMA_HINT["overall_score"]


def test_rubric_threshold_matches_merge_policy_and_is_disclosed(config):
    policy_threshold = config.merge_policy["rules"]["save_structured"]["min_overall_score"]
    assert OVERALL_SCORE_ADMISSION_THRESHOLD == policy_threshold == 70
    assert f"overall_score >= {policy_threshold}" in SYSTEM_PROMPT
    # The merger still enforces the same threshold; the prompt only discloses it.
    merger = MultiModelMerger(config)
    assert merger.rules["save_structured"]["min_overall_score"] == policy_threshold


def test_rubric_reaches_both_extraction_and_arbitration_calls():
    profile = _profile()
    bundle_messages = build_bundle_messages(profile, {"source_link_id": "l1"}, [], {})
    arb_messages = build_arbitration_messages(profile, {"source_link_id": "l1"}, [], {}, [])
    for messages in (bundle_messages, arb_messages):
        assert messages[0]["role"] == "system"
        assert "overall_score（0-100）" in messages[0]["content"]
        hint = json.loads(messages[1]["content"])["output_schema_hint"]
        assert "不是来源信誉分" in hint["overall_score"]
    # Prompt-cache stability: the system prompt is identical across calls.
    assert bundle_messages[0]["content"] == build_bundle_messages(
        profile, {"source_link_id": "l2"}, [], {}
    )[0]["content"]


def test_rubric_does_not_change_the_gate_itself(config):
    """A score below the threshold is still downgraded regardless of the prompt wording."""
    merger = MultiModelMerger(config)
    low = BundleExtraction(
        source_link_id="l1", decision="save_structured", overall_score=64.0, confidence=0.82,
        facts=[FactItem(fact_type="order", fact_statement="中标 4141.622 万元", confidence=0.9)],
        events=[EventCard(event_type="tender", summary="中标公示", confidence=0.9)],
    )
    diag = merger._apply_decision_rules(low)
    assert diag["decision_after"] == "link_only"
    assert diag["reason"] == "below_min_overall_score"
    assert low.facts == [] and low.events == []
