"""Portable model-labelled comparisons; no prose parsing or live calls."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from mic.content_review import enforce_reviewed_comparisons, finalize_task_coverage
from mic.content_review_policy import PROTOCOL, RULE_TEXT
from mic.merge import ModelContribution, MultiModelMerger
from mic.modeling.prompts import SYSTEM_PROMPT
from mic.schemas import Passage, SearchHit
from mic.store.database import Database
from mic.store.repository import Repository
from mic.store import models as m
from mic.validate import BundleValidator


TEXT = "报告期收入240万元，同比增长147.42%；研发占比下降35.03个百分点。"
PASSAGES = [Passage(passage_id="p0", section="正文", text=TEXT)]
QUESTION = [{"question_id": "q1", "question": "核查收入同比和现金流"}]


def fixture(key="yoy", original=147.42, unit="percent", output=1.4742):
    evidence = [{"passage_id": "p0", "quote": TEXT}]
    claims = [
        {"id": "amount", "kind": "observation", "status": "source_supported", "reason": "原文直接披露金额。",
         "statement": "报告期收入240万元。", "evidence": evidence, "depends_on": []},
        {"id": "rate", "kind": "comparability", "status": "source_supported", "reason": "原文给出比较值与口径。",
         "statement": "报告期收入同比增长147.42%。", "evidence": evidence, "depends_on": ["amount"],
         "field_values": {"comparison": {key: output}, "comparison_input": {key: {
             "value": original, "unit": unit, "evidence": [{"passage_id": "p0", "quote": "同比增长147.42%"}]}}}},
        {"id": "summary", "kind": "observation", "status": "source_supported", "reason": "汇总受支持金额和同比。",
         "statement": "报告期收入240万元，同比增长147.42%。", "evidence": evidence, "depends_on": ["amount", "rate"]},
    ]
    return {"decision": "save_structured", "overall_score": 85,
        "brief": {"one_sentence": claims[2]["statement"]},
        "facts": [{"fact_statement": claims[0]["statement"], "evidence_locator": {"passage_id": "p0"}}],
        "metrics": [{"metric_name": "收入", "metric_value": 240, "unit": "万元", "comparison": {key: output},
                     "evidence_locator": {"passage_id": "p0"}}],
        "content_review": {"protocol": PROTOCOL, "claims": claims,
            "bindings": {"/facts/0": ["amount"], "/metrics/0": ["amount"],
                         "/metrics/0/comparison": ["rate"], "/brief/one_sentence": ["summary"]},
            "task_coverage": [{"question_id": "q1", "status": "partial", "answer_claim_ids": ["amount", "rate"],
                               "uncovered_aspects": ["现金流"], "reason": "已回答收入同比，现金流仍缺。"}]}}


def validate(raw, passages=PASSAGES):
    result = BundleValidator({}).validate(raw, passages)
    assert result.schema_valid, result.errors
    return result.bundle


def test_explicit_percent_is_checked_without_rewriting_or_extra_claims():
    raw = fixture()
    before = deepcopy(raw)
    bundle = validate(raw)
    assert raw == before
    assert bundle.metrics[0].comparison == {"yoy": 1.4742}
    assert bundle.brief.one_sentence
    assert len(bundle.content_review["claims"]) == 3
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"


def test_tenfold_wrong_output_is_held_and_propagates_but_amount_survives():
    raw = fixture(output=.14742)
    # The same claim used by a second metric is held consistently.
    raw["metrics"].append(deepcopy(raw["metrics"][0]))
    raw["content_review"]["bindings"].update({"/metrics/1": ["amount"], "/metrics/1/comparison": ["rate"]})
    bundle = validate(raw)
    assert all(metric.comparison == {} and metric.metric_value == 240 for metric in bundle.metrics)
    assert len(bundle.facts) == 1 and bundle.brief.one_sentence == ""
    assert bundle.content_review["claims"]["rate"]["reason"] == "comparison_value_conflicting"
    assert bundle.content_review["claims"]["summary"]["reason"] == "dependency_not_supported"
    assert bundle.content_review["model_review"] == raw["content_review"]
    assert finalize_task_coverage(bundle, QUESTION)[0]["reason"] == "coverage_answer_not_published"


@pytest.mark.parametrize("child", [False, True])
def test_correct_input_cannot_silently_repair_a_conflicting_bound_output(child):
    raw = fixture()
    raw["metrics"][0]["comparison"]["yoy"] = .14742
    if child:
        bindings = raw["content_review"]["bindings"]
        bindings["/metrics/0/comparison/yoy"] = bindings.pop("/metrics/0/comparison")
    bundle = validate(raw)
    assert not bundle.metrics[0].comparison.get("yoy")
    assert bundle.brief.one_sentence == ""
    assert bundle.content_review["claims"]["rate"]["reason"] == "comparison_materialized_value_conflicting"


@pytest.mark.parametrize("damage", ["missing", "unit", "value", "boolean", "nan", "overflow", "empty_quote", "different_passage",
                                    "stitched", "title", "unverified_parent", "unknown_member"])
def test_missing_or_unverified_input_cannot_reuse_an_approved_rate(damage):
    raw = fixture()
    claim = raw["content_review"]["claims"][1]
    fields = claim["field_values"]
    item = fields["comparison_input"]["yoy"]
    if damage == "missing": fields.pop("comparison_input")
    elif damage == "unit": item["unit"] = "unknown"
    elif damage == "value": item["value"] = "147.42%"
    elif damage == "boolean": item["value"] = True
    elif damage == "nan": item["value"] = "NaN"
    elif damage == "overflow": item["value"] = "1e99999999999999999"
    elif damage == "empty_quote": item["evidence"] = []
    elif damage == "different_passage": item["evidence"][0]["passage_id"] = "other"
    elif damage == "stitched": item["evidence"][0]["quote"] = "同比147.42%"
    elif damage == "title": item["evidence"][0]["passage_id"] = "title"
    elif damage == "unverified_parent": claim["evidence"] = [{"passage_id": "p0", "quote": TEXT + "不存在。"}]
    else: fields["comparison_input"]["unexpected"] = item
    bundle = validate(raw, [*PASSAGES, Passage(passage_id="other", section="正文", text=TEXT)])
    assert bundle.metrics[0].comparison == {} and bundle.brief.one_sentence == ""
    assert bundle.metrics[0].metric_value == 240


@pytest.mark.parametrize("key,original,unit,output,text", [
    ("yoy", 0, "percent", 0, "同比增长0%"),
    ("yoy", -5, "percent", -.05, "同比增长-5%"),
    ("qoq", .15, "ratio", .15, "环比增长比例0.15"),
    ("change_ratio", 25, "basis_point", .0025, "变化25个基点"),
    ("yoy_change_pp", -35.03, "percentage_point", -35.03, "同比下降35.03个百分点"),
    ("change_pp", -25, "basis_point", -.25, "下降25个基点"),
])
def test_explicit_dimensions_preserve_zero_sign_and_percentage_points(key, original, unit, output, text):
    raw = fixture(key, original, unit, output)
    text = "报告期收入240万元；" + text + "。"
    for claim in raw["content_review"]["claims"]:
        claim["evidence"] = [{"passage_id": "p0", "quote": text}]
    raw["content_review"]["claims"][1]["statement"] = text
    raw["content_review"]["claims"][2]["statement"] = text
    raw["content_review"]["claims"][1]["field_values"]["comparison_input"][key]["evidence"] = [{"passage_id": "p0", "quote": text}]
    bundle = validate(raw, [Passage(passage_id="p0", section="正文", text=text)])
    assert bundle.metrics[0].comparison == {key: output}


@pytest.mark.parametrize("key,unit", [("yoy", "percentage_point"), ("yoy_change_pp", "percent")])
def test_relative_rate_and_percentage_point_are_not_interchangeable(key, unit):
    bundle = validate(fixture(key, 1, unit, .01))
    assert bundle.metrics[0].comparison == {}
    assert bundle.content_review["claims"]["rate"]["reason"] == "comparison_unit_incompatible"


def test_not_applicable_retains_its_supported_semantics_without_numeric_input():
    raw = fixture()
    claim = raw["content_review"]["claims"][1]
    value = {"yoy": None, "yoy_status": "not_applicable"}
    raw["metrics"][0]["comparison"] = value
    claim["field_values"] = {"comparison": value}
    claim["statement"] = "来源标注比较不适用。"
    text = "报告期收入240万元，同比增长列为不适用。"
    for c in raw["content_review"]["claims"]:
        c["evidence"] = [{"passage_id": "p0", "quote": text}]
    raw["content_review"]["claims"][2]["statement"] = text
    bundle = validate(raw, [Passage(passage_id="p0", section="正文", text=text)])
    assert bundle.metrics[0].comparison == value
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"


def test_pending_rate_stays_held_through_merge_storage_api_and_cache(config, tmp_path):
    bundle = validate(fixture(output=.14742))
    db = Database(f"sqlite:///{tmp_path / 'review.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("target", {}, {}, "query", "model")
    hit = SearchHit(query="report", title="report", url="https://example.org/report", domain="example.org")
    lid = repo.save_source_link(run, "query", hit, hit.url, "media")
    merged = MultiModelMerger(config).merge(lid, "target", [ModelContribution("model", "fixture", bundle)]).bundle
    repo.save_merged_analysis("target", lid, merged, {}, search_run_id=run)
    assert repo.get_metric_observations("target")[0]["comparison"] == {}
    assert repo.search_facts("target")[0]["fact_statement"]
    assert repo.explain_source_analysis(lid)["content_review"]["claims"]["rate"]["reason"] == "comparison_value_conflicting"
    cloned = repo.clone_latest_analysis(lid, "copy", "target", reviewed_only=True)
    assert cloned and cloned["metrics"] == 1
    assert all(row["comparison"] == {} for row in repo.get_metric_observations("target"))
    with db.session() as session:
        assert all(row.one_sentence == "" for row in session.scalars(select(m.AnalysisBrief)))


def test_scope_and_numeric_contracts_are_in_the_canonical_model_prompt():
    assert RULE_TEXT in SYSTEM_PROMPT
    for phrase in ("comparison_input", "观察范围", "相同 passage_id", "不能把报告发布时的动作", "不从正文搜索数字"):
        assert phrase in RULE_TEXT


def test_legacy_comparison_and_dependent_text_are_projected_without_rewriting_db(config, tmp_path):
    raw = fixture()
    raw["facts"].append({"fact_statement": raw["brief"]["one_sentence"], "evidence_locator": {"passage_id": "p0"}})
    raw["events"] = [{"event_type": "earnings_change", "summary": raw["brief"]["one_sentence"],
                      "evidence_locator": {"passage_id": "p0"}}]
    raw["content_review"]["claims"][2]["field_values"] = {"event_type": "earnings_change"}
    raw["content_review"]["bindings"].update({"/facts/1": ["summary"], "/events/0": ["summary"]})
    bundle = validate(raw)
    db = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("target", {}, {}, "q", "m")
    hit = SearchHit(query="report", title="report", url="https://example.org/legacy", domain="example.org")
    lid = repo.save_source_link(run, "query", hit, hit.url, "media")
    merged = MultiModelMerger(config).merge(lid, "target", [ModelContribution("m", "fixture", bundle)]).bundle
    repo.save_merged_analysis("target", lid, merged, {}, search_run_id=run)
    # Simulate genuinely older protocol audit, both merged raw claims and every
    # record audit. Merely changing one SQL value would not cover cache reuse.
    with db.session() as session:
        merged_row = session.scalars(select(m.MergedAnalysis)).one()
        review = deepcopy(merged_row.content_review)
        next(c for c in review["model_review"]["claims"] if c["id"] == "rate")["field_values"].pop("comparison_input")
        merged_row.content_review = review
        for model in (m.MetricObservationRow, m.FactItemRow, m.EventCardRow, m.AnalysisBrief):
            for row in session.scalars(select(model)):
                audit = deepcopy(row.content_review)
                if "rate" in audit.get("claims", {}):
                    audit["claims"]["rate"]["field_values"].pop("comparison_input")
                row.content_review = audit
        metric = session.scalars(select(m.MetricObservationRow)).one()
        old_seal = metric.content_review["materialized_digest"]
        brief = session.scalars(select(m.AnalysisBrief)).one()
        original_brief = {"one_sentence": brief.one_sentence, "content_review": deepcopy(brief.content_review)}
        projected_brief = enforce_reviewed_comparisons("brief", original_brief, review)
        assert projected_brief["one_sentence"] == ""
        assert original_brief["one_sentence"]  # no mutation or fake new approval
    metric = repo.get_metric_observations("target")[0]
    assert metric["comparison"] == {} and metric["metric_value"] == 240
    assert metric["content_review"]["prior_materialized_digest"] == old_seal
    assert "materialized_digest" not in metric["content_review"]
    facts = repo.search_facts("target")
    assert [bool(row["fact_statement"]) for row in facts] == [True, False]
    assert repo.get_recent_events("target")[0]["summary"] == ""
    explanation = repo.explain_source_analysis(lid)
    assert explanation["facts"] == ["报告期收入240万元。", ""]
    assert explanation["events"] == [""]
    assert repo.find_analyzed_link_by_canonical(hit.url, "target", reviewed_only=True) is None
    assert repo.clone_latest_analysis(lid, "copy", "target", reviewed_only=True)["cache_reuse_rejected"] == "comparison_review_requires_refresh"
    assert repo.clone_latest_analysis(lid, "copy", "target", reviewed_only=False)["cache_reuse_rejected"] == "comparison_review_requires_refresh"
    with db.session() as session:
        metric = session.scalars(select(m.MetricObservationRow)).one()
        assert metric.comparison == {"yoy": 1.4742}
        assert metric.content_review["materialized_digest"] == old_seal
        assert session.scalars(select(m.AnalysisBrief)).one().one_sentence == original_brief["one_sentence"]


def test_legacy_risk_depending_on_a_comparison_is_unknown_at_formal_exits(config, tmp_path):
    raw = fixture()
    text = "来源基于报告同比，将收入波动风险评为低等级，影响近期。"
    risk = {"id": "risk", "kind": "analysis", "status": "source_supported",
            "statement": text, "reason": "记录来源明确给出的风险评级及其比较前提。",
            "evidence": [{"passage_id": "p1", "quote": text}], "depends_on": ["rate"],
            "field_values": {"severity": "low", "time_horizon": "near_term"}}
    raw["risks"] = [{"risk_summary": text, "severity": "low", "time_horizon": "near_term",
                     "evidence_locator": {"passage_id": "p1"}}]
    raw["content_review"]["claims"].append(risk)
    raw["content_review"]["bindings"]["/risks/0"] = ["risk"]
    bundle = validate(raw, [*PASSAGES, Passage(passage_id="p1", section="正文", text=text)])
    db = Database(f"sqlite:///{tmp_path / 'legacy_risk.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("target", {}, {}, "q", "m")
    hit = SearchHit(query="risk", title="risk", url="https://example.org/risk", domain="example.org")
    lid = repo.save_source_link(run, "query", hit, hit.url, "media")
    merged = MultiModelMerger(config).merge(lid, "target", [ModelContribution("m", "fixture", bundle)]).bundle
    repo.save_merged_analysis("target", lid, merged, {}, search_run_id=run)
    current = repo.get_risks("target")[0]
    assert (current["risk_summary"], current["severity"], current["time_horizon"]) == (text, "low", "near_term")
    with db.session() as session:
        row = session.scalars(select(m.MergedAnalysis)).one()
        review = deepcopy(row.content_review)
        next(c for c in review["model_review"]["claims"] if c["id"] == "rate")["field_values"].pop("comparison_input")
        row.content_review = review
        metric = session.scalars(select(m.MetricObservationRow)).one()
        audit = deepcopy(metric.content_review)
        audit["claims"]["rate"]["field_values"].pop("comparison_input")
        metric.content_review = audit
    formal = repo.get_risks("target")[0]
    assert (formal["risk_summary"], formal["severity"], formal["time_horizon"]) == ("", "unknown", "unclear")
    assert repo.get_risks("target", severity=["low"]) == []
    assert repo.get_risks("target", severity=["unknown"])[0] == formal
    assert repo.explain_source_analysis(lid)["risks"] == [""]
    assert formal["content_review"]["claims"] == current["content_review"]["claims"]
    assert formal["content_review"]["prior_materialized_digest"] == current["content_review"]["materialized_digest"]
    assert "materialized_digest" not in formal["content_review"]
    assert repo.clone_latest_analysis(lid, "copy", "target")["cache_reuse_rejected"] == "comparison_review_requires_refresh"
    with db.session() as session:
        row = session.scalars(select(m.RiskFlagRow)).one()
        assert (row.risk_summary, row.severity, row.time_horizon) == (text, "low", "near_term")
        assert row.content_review == current["content_review"]
