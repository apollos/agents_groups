"""Labelled model doubles exercise scope enforcement, not live semantics.

Numbers, dimension labels and source text are synthetic. The accepted labels
are supplied by the model double; production must not recognize their words.
"""
from copy import deepcopy

import pytest

from mic.content_review import finalize_task_coverage
from mic.content_review_policy import PROTOCOL
from mic.evidence_review import EvidenceReview
from mic.schemas import BundleExtraction, Passage
from mic.validate import BundleValidator


TEXT = ("In FY1, Family A sales in Region R, on a consolidated basis, were "
        "120 units. The independent operational note reports 6 service sites.")
DIMENSIONS = {"product": "Family A", "region": "Region R", "segment": "consolidated"}
PASSAGES = [Passage(passage_id="body", section="body", text=TEXT)]


def scope_case():
    observation = {
        "id": "sales", "kind": "observation", "status": "source_supported",
        "statement": "FY1 sales were 120 units.",
        "reason": "Labelled model review of the number and explicit dimensions.",
        "evidence": [{"passage_id": "body", "quote": TEXT}], "depends_on": [],
        "field_values": {"scope": deepcopy(DIMENSIONS)},
    }
    return {
        "decision": "save_structured", "overall_score": 90, "confidence": .9,
        "metrics": [{"metric_name": "Sales", "metric_value": 120, "unit": "units",
                     "period": "FY1", "scope": deepcopy(DIMENSIONS),
                     "evidence_locator": {"passage_id": "body"}}],
        "content_review": {"protocol": PROTOCOL, "claims": [observation],
                           "bindings": {"/metrics/0": ["sales"]}},
    }


def validate_scope(raw):
    result = BundleValidator({}, require_content_review=True).validate(raw, PASSAGES)
    assert result.schema_valid, result.errors
    return result.bundle


def assert_number_preserved(bundle):
    metric, = bundle.metrics
    assert (metric.metric_name, metric.metric_value, metric.unit, metric.period) == (
        "Sales", 120, "units", "FY1")
    return metric


def test_explicit_dimensions_keep_exact_model_review_values_without_new_claims():
    raw = scope_case()
    original = deepcopy(raw)
    bundle = validate_scope(raw)
    metric = assert_number_preserved(bundle)
    assert {key: metric.scope[key] for key in DIMENSIONS} == DIMENSIONS
    assert set(bundle.content_review["claims"]) == {"sales"}
    assert raw == original
    assert metric.scope["usable_as_price_benchmark"] is False


@pytest.mark.parametrize("typed", [None, "Region R", {}])
def test_missing_or_untyped_dimensions_do_not_inherit_numeric_approval(typed):
    raw = scope_case()
    raw["content_review"]["claims"][0]["field_values"] = {"scope": typed}
    original = deepcopy(raw)
    bundle = validate_scope(raw)
    metric = assert_number_preserved(bundle)
    assert all(not metric.scope.get(key) for key in DIMENSIONS)
    assert metric.content_review["held_fields"]
    assert raw == original


def test_conflicting_dimension_is_not_replaced_with_either_unapproved_value():
    raw = scope_case()
    raw["content_review"]["claims"][0]["field_values"]["scope"]["region"] = "Region S"
    bundle = validate_scope(raw)
    metric = assert_number_preserved(bundle)
    assert not metric.scope.get("region")
    # Value conflict is auditable; it must not silently rewrite R into S.
    assert any("scope" in field["path"] for field in metric.content_review["held_fields"])


def test_observation_cannot_authorize_a_price_benchmark_flag():
    raw = scope_case()
    raw["metrics"][0]["scope"]["usable_as_price_benchmark"] = True
    raw["content_review"]["claims"][0]["field_values"]["scope"]["usable_as_price_benchmark"] = True
    bundle = validate_scope(raw)
    metric = assert_number_preserved(bundle)
    assert metric.scope["usable_as_price_benchmark"] is False
    assert {key: metric.scope[key] for key in DIMENSIONS} == DIMENSIONS


@pytest.mark.parametrize("diagnostic", [
    "value_evidence", "period_review", "economic_interpretation_review",
    "materiality_review", "source_price_basis_review", "source_price_analysis_review",
])
def test_model_supplied_system_diagnostic_cannot_authorize_scope_or_benchmark(diagnostic):
    raw = scope_case()
    raw["content_review"]["claims"][0]["field_values"].pop("scope")
    raw["metrics"][0]["scope"][diagnostic] = {
        "status": "source_supported", "verified": True,
        "region": "Region R", "usable_as_price_benchmark": True,
        "quote": TEXT, "passage_id": "body",
    }
    raw["metrics"][0]["scope"]["usable_as_price_benchmark"] = True
    original = deepcopy(raw)
    metric = assert_number_preserved(validate_scope(raw))
    assert diagnostic not in metric.scope
    assert all(metric.scope.get(key) is None for key in DIMENSIONS)
    assert metric.scope["usable_as_price_benchmark"] is False
    assert metric.content_review["held_fields"]
    assert raw == original


@pytest.mark.parametrize("valid_arithmetic", [True, False])
def test_legacy_value_evidence_is_recomputed_from_actual_cited_passage(valid_arithmetic):
    forged = {"kind": "explicit_sum", "operator": "+", "operands": [1, 359],
              "value": 360, "unit": "MWh", "passage_id": "invented",
              "verified": True, "usable_as_price_benchmark": True}
    bundle = BundleExtraction.model_validate({"metrics": [{
        "metric_name": "Capacity", "metric_value": 360, "unit": "MWh",
        "scope": {"value_evidence": deepcopy(forged)},
        "evidence_locator": {"passage_id": "body"},
    }]})
    text = "120MWh + 240MWh" if valid_arithmetic else "120MWh and 240MWh"
    EvidenceReview(bundle, [Passage(passage_id="body", section="body", text=text)], []).check_fields()
    metric, = bundle.metrics
    if valid_arithmetic:
        diagnostic = metric.scope["value_evidence"]
        assert diagnostic["operands"] == [120, 240]
        assert diagnostic["passage_id"] == "body"
        assert diagnostic["value"] == 360
        assert "verified" not in diagnostic
        assert "usable_as_price_benchmark" not in diagnostic
    else:
        assert "value_evidence" not in metric.scope
    assert not metric.scope.get("usable_as_price_benchmark")
    assert not metric.content_review


@pytest.mark.parametrize("valid", [True, False])
def test_child_binding_requires_nested_typed_scope_and_keeps_other_dimensions(valid):
    raw = scope_case()
    raw["content_review"]["claims"][0]["field_values"]["scope"].pop("region")
    child = deepcopy(raw["content_review"]["claims"][0])
    child.update(id="regional", statement="The reported dimension is Region R.")
    child["field_values"] = {"scope": {"region": "Region R"}} if valid else {"region": "Region R"}
    raw["content_review"]["claims"].append(child)
    raw["content_review"]["bindings"]["/metrics/0/scope/region"] = ["regional"]
    metric = assert_number_preserved(validate_scope(raw))
    assert metric.scope["product"] == DIMENSIONS["product"]
    assert metric.scope["segment"] == DIMENSIONS["segment"]
    assert metric.scope.get("region") == ("Region R" if valid else None)


@pytest.mark.parametrize("status", ["source_supported", "pending_review", "inference"])
def test_benchmark_requires_its_own_supported_comparability(status):
    raw = scope_case()
    judgment = "The explicitly reviewed series is usable as a benchmark."
    raw["metrics"][0]["interpretation"] = judgment
    comparison = {
        "id": "benchmark", "kind": "comparability", "status": status,
        "statement": judgment, "reason": "Synthetic model comparability label.",
        "evidence": [{"passage_id": "body", "quote": TEXT}], "depends_on": ["sales"],
        "field_values": {"interpretation": judgment,
                         "scope": {"usable_as_price_benchmark": True}},
    }
    raw["content_review"]["claims"].append(comparison)
    raw["content_review"]["bindings"]["/metrics/0/interpretation"] = ["benchmark"]
    metric = assert_number_preserved(validate_scope(raw))
    assert metric.scope["usable_as_price_benchmark"] is (status == "source_supported")
    assert {key: metric.scope[key] for key in DIMENSIONS} == DIMENSIONS


@pytest.mark.parametrize("status", ["source_supported", "pending_review"])
def test_explicit_benchmark_child_binding_is_checked_as_comparability(status):
    raw = scope_case()
    raw["metrics"][0]["scope"]["usable_as_price_benchmark"] = True
    raw["content_review"]["claims"].append({
        "id": "benchmark", "kind": "comparability", "status": status,
        "statement": "The reviewed scope permits use as a comparable price benchmark.",
        "reason": "Synthetic model comparability label.",
        "evidence": [{"passage_id": "body", "quote": TEXT}], "depends_on": ["sales"],
        "field_values": {"scope": {"usable_as_price_benchmark": True}},
    })
    raw["content_review"]["bindings"]["/metrics/0/scope/usable_as_price_benchmark"] = ["benchmark"]
    metric = assert_number_preserved(validate_scope(raw))
    assert metric.scope["usable_as_price_benchmark"] is (status == "source_supported")
    assert {key: metric.scope[key] for key in DIMENSIONS} == DIMENSIONS


def with_dependents(raw):
    review = raw["content_review"]
    dependent = {
        "id": "regional_summary", "kind": "observation", "status": "source_supported",
        "statement": "Consolidated Region R sales reached 120 units.",
        "reason": "Synthetic summary explicitly relies on the metric observation.",
        "evidence": [{"passage_id": "body", "quote": TEXT}], "depends_on": ["sales"],
    }
    analysis = {
        "id": "regional_analysis", "kind": "analysis", "status": "source_supported",
        "statement": "This is a region-specific consolidated series.",
        "reason": "Synthetic label relying transitively on the summary.",
        "evidence": [{"passage_id": "body", "quote": TEXT}],
        "depends_on": ["regional_summary"],
        "field_values": {"why_it_matters": "This is a region-specific consolidated series."},
    }
    independent = {
        "id": "sites", "kind": "observation", "status": "source_supported",
        "statement": "The operational note reports 6 service sites.",
        "reason": "An independent observation with no scope dependency.",
        "evidence": [{"passage_id": "body", "quote": TEXT}], "depends_on": [],
    }
    review["claims"].extend([dependent, analysis, independent])
    raw["brief"] = {"one_sentence": dependent["statement"],
                    "why_it_matters": analysis["statement"]}
    raw["facts"] = [{"fact_statement": independent["statement"],
                     "evidence_locator": {"passage_id": "body"}}]
    review["bindings"].update({"/brief/one_sentence": ["regional_summary"],
                               "/brief/why_it_matters": ["regional_analysis"],
                               "/facts/0": ["sites"]})
    return raw


@pytest.mark.parametrize("typed", [True, False])
def test_scope_dependency_is_conservative_without_discarding_number_or_independent_fact(typed):
    raw = with_dependents(scope_case())
    if not typed:
        raw["content_review"]["claims"][0]["field_values"].pop("scope")
    bundle = validate_scope(raw)
    assert_number_preserved(bundle)
    assert len(bundle.facts) == 1
    assert bundle.facts[0].fact_statement == "The operational note reports 6 service sites."
    assert bool(bundle.brief.one_sentence) is typed
    assert bool(bundle.brief.why_it_matters) is typed
    assert bundle.content_review["claims"]["sales"]["status"] == "source_supported"
    if not typed:
        assert bundle.metrics[0].content_review["claims"]["sales"]["dependency_status"] == "pending_review"
        assert bundle.content_review["claims"]["regional_summary"]["status"] != "source_supported"
        assert bundle.content_review["claims"]["regional_analysis"]["status"] != "source_supported"


@pytest.mark.parametrize("typed", [True, False])
def test_coverage_cannot_claim_full_answer_by_borrowing_unreviewed_scope(typed):
    raw = scope_case()
    if not typed:
        raw["content_review"]["claims"][0]["field_values"].pop("scope")
    question = {"question_id": "q1", "question": "Report the value and its geographical and accounting scope."}
    raw["content_review"]["task_coverage"] = [{
        "question_id": "q1", "status": "covered", "answer_claim_ids": ["sales"],
        "uncovered_aspects": [], "reason": "The model claims all requested dimensions are answered.",
    }]
    bundle = validate_scope(raw)
    result, = finalize_task_coverage(bundle, [question])
    assert_number_preserved(bundle)
    if typed:
        assert result["status"] == "covered"
    else:
        assert result["status"] != "covered"
        assert result["answer_claim_ids"] == []
        assert bundle.coverage_gaps


@pytest.mark.parametrize("typed", [True, False])
def test_same_observation_reused_as_narrative_cannot_bypass_failed_scope(typed):
    raw = scope_case()
    sales = raw["content_review"]["claims"][0]
    sales["statement"] = "Consolidated Region R sales were 120 units."
    if not typed:
        sales["field_values"].pop("scope")
    raw["brief"] = {"one_sentence": sales["statement"]}
    raw["facts"] = [{"fact_statement": sales["statement"],
                     "evidence_locator": {"passage_id": "body"}}]
    raw["content_review"]["bindings"].update({"/brief/one_sentence": ["sales"],
                                               "/facts/0": ["sales"]})
    bundle = validate_scope(raw)
    assert_number_preserved(bundle)
    assert bool(bundle.brief.one_sentence) is typed
    assert bool(bundle.facts) is typed
