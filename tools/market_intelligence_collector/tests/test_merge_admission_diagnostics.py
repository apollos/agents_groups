"""A rejected bundle must remain auditable without changing admission policy."""
from copy import deepcopy
from types import SimpleNamespace

from mic.budget import RunBudget
from mic.config import MICConfig
from mic.merge import ModelContribution, MultiModelMerger
from mic.pipeline import Pipeline, RunStats
from mic.schemas import BundleExtraction


def config():
    return MICConfig(raw={"merge_policy": {"rules": {"save_structured": {"min_overall_score": 70}}}})


def contribution(score=68, decision="save_structured", model="fixture"):
    return ModelContribution(model_config_id=model, provider=model,
        bundle=BundleExtraction.model_validate({"source_link_id": "original", "decision": decision,
            "overall_score": score, "confidence": .82,
            "facts": [{"fact_statement": "来源称中标", "evidence_locator": {"passage_id": "p2"}}],
            "metrics": [{"metric_value": 40, "unit": "MWh", "evidence_locator": {"passage_id": "p2"}}],
            "events": [{"event_type": "tender", "summary": "来源中标线索", "evidence_locator": {"passage_id": "p2"}}]}))


def test_single_model_trace_retains_validated_input_after_score_rejection():
    c = contribution()
    original = deepcopy(c.bundle.model_dump())
    result = MultiModelMerger(config()).merge("new-source", "target", [c])
    assert c.bundle.model_dump() == original
    assert result.bundle.decision == "link_only" and not result.bundle.facts
    trace = result.model_outputs[0]
    assert trace["decision"] == "save_structured" and trace["counts"]["facts"] == 1
    d = result.decision_diagnostics
    assert d["reason"] == "below_min_overall_score"
    assert (d["overall_score"], d["min_overall_score"]) == (68, 70)
    assert d["validated_counts_before_admission"]["events"] == 1
    assert sum(d["retained_counts_after_admission"].values()) == 0
    # Admission must not poison a later arbitration merge with the same input.
    later = MultiModelMerger(config()).merge("source", "target", [c, contribution(80, model="arbiter")])
    assert c.bundle.model_dump() == original
    assert later.model_outputs[0]["counts"]["facts"] == 1


def test_model_link_only_is_distinct_from_score_rejection():
    c = contribution(90, "link_only")
    result = MultiModelMerger(config()).merge("source", "target", [c])
    assert result.decision_diagnostics["reason"] == "link_only_decision"
    assert result.bundle.decision == "link_only" and not result.bundle.events
    assert len(c.bundle.events) == 1


def test_score_boundary_still_accepts_seventy_and_result_does_not_alias_input():
    c = contribution(70)
    result = MultiModelMerger(config()).merge("source", "target", [c])
    assert result.bundle.decision == "save_structured" and len(result.bundle.facts) == 1
    assert result.decision_diagnostics["reason"] == "accepted"
    result.bundle.facts[0].fact_statement = "changed only in merged output"
    assert c.bundle.facts[0].fact_statement == "来源称中标"


def test_sixty_nine_is_rejected_and_seventy_one_is_accepted_with_identical_bundles():
    # R7: only the score differs; everything else about the bundle is equal.
    rejected = MultiModelMerger(config()).merge("source", "target", [contribution(69)])
    accepted = MultiModelMerger(config()).merge("source", "target", [contribution(71)])
    assert rejected.bundle.decision == "link_only"
    assert rejected.decision_diagnostics["reason"] == "below_min_overall_score"
    assert (rejected.decision_diagnostics["overall_score"], rejected.decision_diagnostics["min_overall_score"]) == (69, 70)
    assert sum(rejected.decision_diagnostics["retained_counts_after_admission"].values()) == 0
    assert accepted.bundle.decision == "save_structured"
    assert accepted.decision_diagnostics["reason"] == "accepted"
    assert (len(accepted.bundle.facts), len(accepted.bundle.metrics), len(accepted.bundle.events)) == (1, 1, 1)
    # The rejected candidate's counts are reported, never promoted.
    before = rejected.decision_diagnostics["validated_counts_before_admission"]
    assert (before["facts"], before["metrics"], before["events"]) == (1, 1, 1)


def test_multi_model_score_gate_reports_same_rejection_without_changing_inputs():
    inputs = [contribution(68, model="a"), contribution(68, model="b")]
    original = [c.bundle.model_dump() for c in inputs]
    result = MultiModelMerger(config()).merge("source", "target", inputs)
    assert result.bundle.decision == "link_only"
    assert result.decision_diagnostics["reason"] == "below_min_overall_score"
    assert [c.bundle.model_dump() for c in inputs] == original
    assert all(t["decision"] == "save_structured" for t in result.model_outputs)


def test_report_explains_empty_output_without_promoting_candidates():
    merged = MultiModelMerger(config()).merge("source", "target", [contribution()])
    stats = RunStats(queries_attempted=1, search_hits=1, links_selected_for_read=1,
                     links_read=1, links_model_analyzed=1,
                     output_decisions=[{"source_link_id": "source", **merged.decision_diagnostics}])
    pipe = Pipeline.__new__(Pipeline)
    pipe.config = config()
    pipe.search = SimpleNamespace(name="offline-fixture")
    context = SimpleNamespace(budget=RunBudget(), attempt_id="offline", config_fingerprint="fixture")
    diag = pipe._collection_diagnostics("run", stats, context, {"cleanup": "complete"})
    assert diag["execution_status"] == "completed"
    assert diag["output_status"] == "no_structured_output" and diag["usable"] is False
    assert diag["output_decisions"][0]["reason"] == "below_min_overall_score"
    assert diag["output_decisions"][0]["validated_counts_before_admission"]["facts"] == 1
    assert sum(stats.structured.values()) == 0
