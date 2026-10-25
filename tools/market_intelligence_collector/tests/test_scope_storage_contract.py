"""Read projections and cache admission must not reissue legacy scope labels."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from mic.api import AnalystAPI
from mic.content_review import enforce_reviewed_scope, finalize_task_coverage
from mic.schemas import SearchHit
from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository
from tests.test_scope_review_contract import DIMENSIONS, TEXT, scope_case, validate_scope, with_dependents


URL = "https://example.invalid/scope-report"


def stored_scope(tmp_path, *, include_scope=True, benchmark=False, coverage=False, derived_metric=False):
    db = Database(f"sqlite:///{tmp_path / 'scope.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("entity", {}, {}, "offline", "offline")
    hit = SearchHit(query="offline", title="Report", url=URL, domain="example.invalid")
    link = repo.save_source_link(run, "offline", hit, URL, "official")
    raw = with_dependents(scope_case())
    if not include_scope:
        raw = scope_case()
        raw["metrics"][0]["scope"] = {}
        raw["content_review"]["claims"][0]["field_values"] = {}
    else:
        dependent = raw["content_review"]["claims"][1]
        raw["facts"].append({"fact_statement": dependent["statement"],
                             "evidence_locator": {"passage_id": "body"}})
        raw["content_review"]["bindings"]["/facts/1"] = [dependent["id"]]
        raw["facts"].append({"fact_statement": raw["content_review"]["claims"][0]["statement"],
                             "evidence_locator": {"passage_id": "body"}})
        raw["content_review"]["bindings"]["/facts/2"] = ["sales"]
        analysis = raw["content_review"]["claims"][2]
        analysis["field_values"]["uncertainty"] = analysis["statement"]
        raw["brief"]["uncertainty"] = analysis["statement"]
        raw["content_review"]["bindings"]["/brief/uncertainty"] = [analysis["id"]]
    if benchmark:
        raw["metrics"][0]["interpretation"] = "A valid comparable benchmark."
        raw["content_review"]["claims"].append({
            "id": "benchmark", "kind": "comparability", "status": "source_supported",
            "statement": "A valid comparable benchmark.", "reason": "Synthetic model label.",
            "evidence": [{"passage_id": "body", "quote": TEXT}], "depends_on": ["sales"],
            "field_values": {"interpretation": "A valid comparable benchmark.",
                             "scope": {"usable_as_price_benchmark": True}},
        })
        raw["content_review"]["bindings"]["/metrics/0/interpretation"] = ["benchmark"]
    if coverage:
        raw["content_review"]["task_coverage"] = [{
            "question_id": "q1", "status": "covered", "answer_claim_ids": ["sales"],
            "uncovered_aspects": [], "reason": "The value and its scope are reviewed.",
        }]
    if derived_metric:
        raw["metrics"].append({
            "metric_name": "Scoped restatement", "metric_value": 120,
            "unit": "units", "period": "FY1", "scope": {},
            "evidence_locator": {"passage_id": "body"},
        })
        raw["content_review"]["claims"].append({
            "id": "scoped_restatement", "kind": "observation", "status": "source_supported",
            "statement": "The scoped restatement is 120 units.",
            "reason": "Synthetic record explicitly relying on the reviewed scoped premise.",
            "evidence": [{"passage_id": "body", "quote": TEXT}], "depends_on": ["sales"],
        })
        raw["content_review"]["bindings"]["/metrics/1"] = ["scoped_restatement"]
    bundle = validate_scope(raw)
    if coverage:
        assessment, = finalize_task_coverage(bundle, [{
            "question_id": "q1", "question": "Report the value and its scope.",
        }])
        assert assessment["status"] == "covered"
    repo.save_merged_analysis("entity", link, bundle, {}, search_run_id=run)
    repo.update_link_read(link, "read", "scope-body-hash", "scope-simhash")
    return db, repo, run, link, bundle


def strip_scope_approval(value):
    """Simulate a historical fixture; deliberately retain its original seal."""
    if isinstance(value, dict):
        value.pop("scope_review", None)
        fields = value.get("field_values")
        if value.get("id") == "sales" and isinstance(fields, dict):
            fields.pop("scope", None)
        for child in value.values():
            strip_scope_approval(child)
    elif isinstance(value, list):
        for child in value:
            strip_scope_approval(child)


def make_legacy(db):
    with db.session() as session:
        for model in (m.MergedAnalysis, m.MetricObservationRow, m.AnalysisBrief, m.FactItemRow):
            for row in session.scalars(select(model)):
                audit = deepcopy(row.content_review)
                strip_scope_approval(audit)
                row.content_review = audit


def original_rows(db):
    with db.session() as session:
        result = {}
        for model in (m.MergedAnalysis, m.MetricObservationRow, m.AnalysisBrief, m.FactItemRow):
            result[model.__name__] = [
                {column.key: deepcopy(getattr(row, column.key)) for column in model.__table__.columns}
                for row in session.scalars(select(model).order_by(model.id))
            ]
        return result


def api_for(repo):
    api = AnalystAPI.__new__(AnalystAPI)
    api.repo = repo
    return api


@pytest.mark.parametrize("strict", [False, True])
def test_current_explicit_scope_can_be_read_and_reused_with_same_audit(tmp_path, strict):
    _, repo, _, link, bundle = stored_scope(tmp_path)
    api = api_for(repo)
    metric, = api.get_metric_observations("entity")
    assert {key: metric["scope"][key] for key in DIMENSIONS} == DIMENSIONS
    assert metric["content_review"] == bundle.metrics[0].content_review
    explained, = api.explain_source_analysis(link)["metrics"]
    assert explained["value"] == metric["metric_value"] == 120
    assert explained["scope"] == metric["scope"]
    assert explained["content_review"] == metric["content_review"]
    assert repo.find_analyzed_link_by_canonical(URL, "entity", reviewed_only=strict).id == link
    assert repo.find_analyzed_link_by_content_hash("scope-body-hash", "entity", reviewed_only=strict).id == link
    cloned = repo.clone_latest_analysis(link, "copy", "entity", reviewed_only=strict)
    assert cloned["metrics"] == 1 and "cache_reuse_rejected" not in cloned
    assert all({key: row["scope"][key] for key in DIMENSIONS} == DIMENSIONS
               for row in api.get_metric_observations("entity"))


def test_legacy_scope_projects_unknown_but_retains_value_original_audit_and_seal_history(tmp_path):
    db, repo, _, link, _ = stored_scope(tmp_path)
    make_legacy(db)
    before = original_rows(db)
    original = before["MetricObservationRow"][0]
    assert {key: original["scope"][key] for key in DIMENSIONS} == DIMENSIONS
    api = api_for(repo)
    metric, = api.get_metric_observations("entity")
    assert (metric["metric_value"], metric["unit"], metric["period"]) == (120, "units", "FY1")
    assert all(not metric["scope"].get(key) for key in DIMENSIONS)
    projected = metric["content_review"]
    audit = original["content_review"]
    assert projected["claims"] == audit["claims"]
    assert projected["fields"] == audit["fields"]
    assert projected["prior_materialized_digest"] == audit["materialized_digest"]
    assert "materialized_digest" not in projected
    assert projected["materialization_corrections"]
    assert any(c["reason"] == "stored_scope_review_missing_or_conflicting"
               for c in projected["materialization_corrections"])
    explanation = api.explain_source_analysis(link)
    explained, = explanation["metrics"]
    assert explained["value"] == 120 and explained["scope"] == metric["scope"]
    assert explained["content_review"] == projected
    # Read-only correction must not mutate either business rows or old audit/seal.
    assert original_rows(db) == before


def test_legacy_scope_blocks_explicit_dependent_text_but_preserves_unrelated_fact(tmp_path):
    db, repo, _, link, _ = stored_scope(tmp_path)
    make_legacy(db)
    before = original_rows(db)
    api = api_for(repo)
    facts = api.search_facts("entity")
    statements = [row["fact_statement"] for row in facts]
    assert "The operational note reports 6 service sites." in statements
    assert "Consolidated Region R sales reached 120 units." not in statements
    # Without semantic text scanning, a direct reuse of the unsafe premise is
    # also withheld; the metric's independently usable numeric core survives.
    assert "FY1 sales were 120 units." not in statements
    explanation = api.explain_source_analysis(link)
    assert "Consolidated Region R sales reached 120 units." not in explanation["facts"]
    assert not explanation["uncertainty"]
    assert original_rows(db) == before


@pytest.mark.parametrize("strict", [False, True])
def test_legacy_scope_cache_rejects_whole_analysis_in_both_modes_without_writing(tmp_path, strict):
    db, repo, _, link, _ = stored_scope(tmp_path)
    make_legacy(db)
    before = original_rows(db)
    assert repo.find_analyzed_link_by_canonical(URL, "entity", reviewed_only=strict) is None
    assert repo.find_analyzed_link_by_content_hash("scope-body-hash", "entity", reviewed_only=strict) is None
    result = repo.clone_latest_analysis(link, "copy", "entity", reviewed_only=strict)
    assert result["cache_reuse_rejected"] == "scope_review_requires_refresh"
    assert all(result[key] == 0 for key in ("briefs", "facts", "metrics", "events", "relations", "risks"))
    assert original_rows(db) == before


def test_legacy_numeric_record_without_business_scope_is_not_a_false_cache_rejection(tmp_path):
    db, repo, _, link, _ = stored_scope(tmp_path, include_scope=False)
    make_legacy(db)
    before = original_rows(db)
    metric, = repo.get_metric_observations("entity")
    assert metric["metric_value"] == 120
    assert all(not metric["scope"].get(key) for key in DIMENSIONS)
    assert repo.find_analyzed_link_by_canonical(URL, "entity", reviewed_only=True).id == link
    assert original_rows(db) == before


def test_legacy_scope_invalidates_derived_benchmark_permission(tmp_path):
    db, repo, _, link, bundle = stored_scope(tmp_path, benchmark=True)
    assert bundle.metrics[0].scope["usable_as_price_benchmark"] is True
    make_legacy(db)
    before = original_rows(db)
    metric, = repo.get_metric_observations("entity")
    assert metric["metric_value"] == 120
    assert metric["scope"]["usable_as_price_benchmark"] is False
    assert not metric["interpretation"]
    explained, = repo.explain_source_analysis(link)["metrics"]
    assert explained["scope"] == metric["scope"]
    assert original_rows(db) == before


def test_orphan_legacy_metric_uses_its_local_dependencies_to_withhold_benchmark(tmp_path):
    db, repo, _, _, bundle = stored_scope(tmp_path, benchmark=True)
    assert bundle.metrics[0].scope["usable_as_price_benchmark"] is True
    make_legacy(db)
    with db.session() as session:
        merged, = session.scalars(select(m.MergedAnalysis)).all()
        session.delete(merged)
    before = original_rows(db)
    assert before["MergedAnalysis"] == []
    original = before["MetricObservationRow"][0]
    assert original["interpretation"] and original["scope"]["usable_as_price_benchmark"] is True
    metric, = api_for(repo).get_metric_observations("entity")
    assert (metric["metric_value"], metric["unit"], metric["period"]) == (120, "units", "FY1")
    assert all(not metric["scope"].get(key) for key in DIMENSIONS)
    assert not metric["interpretation"]
    assert metric["scope"]["usable_as_price_benchmark"] is False
    assert metric["content_review"]["claims"] == original["content_review"]["claims"]
    assert "materialized_digest" not in metric["content_review"]
    assert metric["content_review"]["prior_materialized_digest"] == original["content_review"]["materialized_digest"]
    assert original_rows(db) == before


def test_explanation_aliases_cannot_republish_scope_dependent_metric_values(tmp_path):
    db, repo, _, link, bundle = stored_scope(tmp_path, derived_metric=True)
    assert [(row.metric_name, row.metric_value) for row in bundle.metrics] == [
        ("Sales", 120), ("Scoped restatement", 120)]
    make_legacy(db)
    before = original_rows(db)
    api = api_for(repo)
    canonical = api.get_metric_observations("entity")
    by_claim = {next(iter(row["content_review"]["claims"])): row for row in canonical}
    independent, dependent = by_claim["sales"], by_claim["scoped_restatement"]
    assert (independent["metric_name"], independent["metric_value"]) == ("Sales", 120)
    assert dependent["metric_name"] == "" and dependent["metric_value"] is None
    explained = api.explain_source_analysis(link)["metrics"]
    explanations = {next(iter(row["content_review"]["claims"])): row for row in explained}
    for claim_id, row in by_claim.items():
        displayed = explanations[claim_id]
        assert (displayed["name"], displayed["value"]) == (row["metric_name"], row["metric_value"])
        assert displayed["scope"] == row["scope"]
    assert original_rows(db) == before


def test_explanation_cannot_repeat_legacy_full_coverage_after_scope_is_withheld(tmp_path):
    db, repo, _, link, _ = stored_scope(tmp_path, coverage=True)
    make_legacy(db)
    before = original_rows(db)
    assert before["MergedAnalysis"][0]["content_review"]["task_coverage"][0]["status"] == "covered"
    view = repo.explain_source_analysis(link)["content_review"]
    assert view["task_coverage"][0]["status"] != "covered"
    assert original_rows(db) == before


def test_same_claim_id_in_another_contribution_is_not_an_unsafe_scope_dependency():
    raw_a, raw_b = scope_case(), scope_case()
    raw_b["content_review"]["claims"][0]["statement"] = "The source reports FY1 sales of 120 units."
    for raw in (raw_a, raw_b):
        raw["brief"] = {"one_sentence": raw["content_review"]["claims"][0]["statement"]}
        raw["content_review"]["bindings"]["/brief/one_sentence"] = ["sales"]
    old, current = validate_scope(raw_a), validate_scope(raw_b)
    old_review = deepcopy(old.content_review)
    old_metric = old.metrics[0].model_dump(mode="json")
    old_brief = old.brief.model_dump(mode="json")
    for value in (old_review, old_metric, old_brief):
        strip_scope_approval(value)
    review = {"contributions": [old_review, current.content_review]}
    records = [old_metric, current.metrics[0].model_dump(mode="json")]
    rejected = enforce_reviewed_scope("brief", old_brief, review, scope_records=records)
    assert not rejected["one_sentence"]
    preserved = enforce_reviewed_scope("brief", current.brief.model_dump(mode="json"),
                                       review, scope_records=records)
    assert preserved == current.brief.model_dump(mode="json")


@pytest.mark.parametrize("strict", [False, True])
def test_clone_uses_only_selected_latest_analysis_for_a_reused_source(tmp_path, strict):
    db, repo, run, link, _ = stored_scope(tmp_path)
    make_legacy(db)
    fresh = scope_case()
    fresh["metrics"][0].update(metric_name="Service sites", metric_value=6, unit="sites", scope={})
    fresh["content_review"]["claims"][0].update(
        statement="The operational note reports 6 service sites.", field_values={})
    fresh["brief"] = {"one_sentence": "The operational note reports 6 service sites."}
    fresh["content_review"]["bindings"]["/brief/one_sentence"] = ["sales"]
    bundle = validate_scope(fresh)
    repo.save_merged_analysis("entity", link, bundle, {}, search_run_id=run)
    with db.session() as session:
        analyses = list(session.scalars(select(m.MergedAnalysis).order_by(m.MergedAnalysis.created_at)))
        assert len(analyses) == 2
        latest_id = analyses[-1].id
        latest_briefs = list(session.scalars(select(m.AnalysisBrief).where(
            m.AnalysisBrief.merged_analysis_id == latest_id)))
        expected_briefs = [row.one_sentence for row in latest_briefs]
    assert repo.find_analyzed_link_by_canonical(URL, "entity", reviewed_only=strict).id == link
    result = repo.clone_latest_analysis(link, "copy", "entity", reviewed_only=strict)
    assert "cache_reuse_rejected" not in result
    assert result["metrics"] == 1
    assert result["facts"] == 0
    with db.session() as session:
        copied_metrics = list(session.scalars(select(m.MetricObservationRow).where(
            m.MetricObservationRow.source_link_id == "copy")))
        assert len(copied_metrics) == 1
        copied, = copied_metrics
        assert (copied.metric_name, copied.metric_value, copied.unit) == ("Service sites", 6, "sites")
        assert all(not copied.scope.get(key) for key in DIMENSIONS)
        copied_briefs = list(session.scalars(select(m.AnalysisBrief).where(
            m.AnalysisBrief.source_link_id == "copy")))
        assert [row.one_sentence for row in copied_briefs] == expected_briefs
