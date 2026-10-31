"""Missing semantic judgments remain unknown through schema, storage and reuse.

These small model doubles check enforcement; they do not certify model semantics.
"""
from copy import deepcopy

import pytest
from sqlalchemy import select

from mic.content_review import enforce_reviewed_defaults, seal
from mic.content_review_policy import PROTOCOL
from mic.merge import ModelContribution, MultiModelMerger
from mic.schemas import EntityRef, Passage, SearchHit
from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository
from mic.validate import BundleValidator


TEXT = "来源指出客户回款尚待验证，并将这一近期风险评为低等级。"
PASSAGES = [Passage(passage_id="p1", section="正文", text=TEXT)]


def risk_case(severity="medium", horizon="medium_term", *, approved=False):
    claim = {"id": "risk", "kind": "analysis", "status": "source_supported",
             "statement": "来源指出客户回款尚待验证。", "reason": "风险文字有来源归属。",
             "evidence": [{"passage_id": "p1", "quote": TEXT}], "depends_on": []}
    if approved:
        claim["field_values"] = {"severity": severity, "time_horizon": horizon}
    return {"schema_version": "bundle_extraction_v0.3", "confidence": 0.0, "decision": "save_structured", "overall_score": 80,
            "risks": [{"risk_summary": claim["statement"], "severity": severity,
                       "time_horizon": horizon, "evidence_locator": {"passage_id": "p1"}}],
            "content_review": {"protocol": PROTOCOL, "claims": [claim],
                               "bindings": {"/risks/0": ["risk"]}}}


def validated(raw):
    report = BundleValidator({}, require_content_review=True).validate(raw, PASSAGES)
    assert report.schema_valid, report.errors
    return report.bundle


@pytest.mark.parametrize("severity,horizon", [("medium", "medium_term"), ("low", "near_term")])
def test_unreviewed_risk_fields_never_fall_back_to_a_concrete_judgment(severity, horizon):
    bundle = validated(risk_case(severity, horizon))
    risk = bundle.risks[0]
    assert risk.risk_summary == "来源指出客户回款尚待验证。"
    assert (risk.severity, risk.time_horizon) == ("unknown", "unclear")
    assert risk.content_review["fields"]["severity"]["status"] == "pending_review"
    assert risk.content_review["fields"]["time_horizon"]["status"] == "pending_review"
    held = {h["path"].rsplit("/", 1)[-1]: h["original"] for h in risk.content_review["held_fields"]}
    assert held["severity"] == severity and held["time_horizon"] == horizon


def test_explicitly_reviewed_low_rating_and_near_term_survive():
    risk = validated(risk_case("low", "near_term", approved=True)).risks[0]
    assert (risk.severity, risk.time_horizon) == ("low", "near_term")
    assert enforce_reviewed_defaults("risks", risk.model_dump()) == risk.model_dump()


def test_one_approved_field_does_not_authorize_another():
    raw = risk_case("low", "near_term", approved=True)
    del raw["content_review"]["claims"][0]["field_values"]["time_horizon"]
    risk = validated(raw).risks[0]
    assert (risk.severity, risk.time_horizon) == ("low", "unclear")


def test_other_schema_defaults_do_not_invent_entity_type_or_question_priority():
    assert EntityRef(name="机构甲").type == "unknown"
    raw = risk_case()
    raw["analyst_questions"] = [{"question": "请核查客户回款。", "priority": "medium"}]
    raw["content_review"]["claims"].append({
        "id": "question", "kind": "limitation", "status": "source_supported",
        "statement": "请核查客户回款。", "reason": "原文说明尚待验证。",
        "evidence": [{"passage_id": "p1", "quote": TEXT}], "depends_on": [],
    })
    raw["content_review"]["bindings"]["/analyst_questions/0"] = ["question"]
    question = validated(raw).analyst_questions[0]
    assert question.priority == "unknown"
    assert question.content_review["fields"]["priority"]["status"] == "pending_review"


def stored_risk(config, tmp_path, *, approved=False, legacy=False):
    db = Database(f"sqlite:///{tmp_path / 'risk.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("target", {}, {}, "q", "m")
    hit = SearchHit(query="风险", title="风险", url="https://example.test/risk", domain="example.test")
    lid = repo.save_source_link(run, "q", hit, hit.url, "media")
    bundle = validated(risk_case("medium" if legacy else "low",
                                 "medium_term" if legacy else "near_term", approved=approved))
    if legacy:
        # Reproduce the previous reviewer: medium judgments were held, then
        # low/near_term defaults were materialized and sealed as the old payload.
        bundle.risks[0].severity = "low"
        bundle.risks[0].time_horizon = "near_term"
        seal(bundle)
    merged = MultiModelMerger(config).merge(lid, "target", [ModelContribution("m", "fixture", bundle)])
    repo.save_merged_analysis("target", lid, merged.bundle, {}, search_run_id=run)
    return db, repo, lid, merged.bundle.risks[0]


def test_unknown_fields_survive_merge_storage_formal_api_and_cache(config, tmp_path):
    db, repo, lid, risk = stored_risk(config, tmp_path)
    first = repo.get_risks("target")[0]
    assert (first["severity"], first["time_horizon"]) == ("unknown", "unclear")
    assert first["content_review"] == risk.content_review
    assert repo.clone_latest_analysis(lid, "copy", "target", reviewed_only=True)["risks"] == 1
    assert all(r["severity"] == "unknown" and r["time_horizon"] == "unclear"
               and r["content_review"] == risk.content_review for r in repo.get_risks("target"))
    with db.session() as session:
        assert all(r.severity == "unknown" and r.time_horizon == "unclear"
                   for r in session.scalars(select(m.RiskFlagRow)))


def test_legacy_held_defaults_are_projected_safely_without_rewriting_evidence(config, tmp_path):
    db, repo, lid, risk = stored_risk(config, tmp_path, legacy=True)
    formal = repo.get_risks("target")[0]
    assert (formal["severity"], formal["time_horizon"]) == ("unknown", "unclear")
    assert formal["content_review"]["held_fields"] == risk.content_review["held_fields"]
    assert len(formal["content_review"]["materialization_corrections"]) == 2
    assert formal["content_review"]["prior_materialized_digest"] == risk.content_review["materialized_digest"]
    assert "materialized_digest" not in formal["content_review"]
    assert repo.get_risks("target", severity=["low"]) == []
    assert repo.get_risks("target", severity=["unknown"]) == [formal]
    assert repo.clone_latest_analysis(lid, "copy", "target", reviewed_only=True)["risks"] == 1
    with db.session() as session:
        rows = {r.source_link_id: r for r in session.scalars(select(m.RiskFlagRow))}
        assert (rows[lid].severity, rows[lid].time_horizon) == ("low", "near_term")
        assert rows[lid].content_review == risk.content_review
        assert (rows["copy"].severity, rows["copy"].time_horizon) == ("unknown", "unclear")


@pytest.mark.parametrize("review", [{}, {"fields": []}, {"protocol": PROTOCOL, "fields": {"severity": None}}])
def test_missing_or_malformed_review_does_not_authorize_legacy_ratings(review):
    old = {"severity": "low", "time_horizon": "near_term", "content_review": review}
    original = deepcopy(old)
    projected = enforce_reviewed_defaults("risks", old)
    assert old == original
    assert (projected["severity"], projected["time_horizon"]) == ("unknown", "unclear")
    if review.get("protocol") != PROTOCOL:
        assert projected["content_review"]["read_review_status"] == "legacy_unreviewed"
        assert "protocol" not in projected["content_review"]


def test_approved_ratings_survive_storage_and_cached_projection(config, tmp_path):
    _, repo, lid, risk = stored_risk(config, tmp_path, approved=True)
    repo.clone_latest_analysis(lid, "copy", "target", reviewed_only=True)
    formal = repo.get_risks("target", severity=["low"])
    assert len(formal) == 2
    assert all(r["severity"] == "low" and r["time_horizon"] == "near_term"
               and r["content_review"] == risk.content_review for r in formal)


def test_legacy_unknown_values_still_disclose_missing_review():
    old = {"severity": "unknown", "time_horizon": "unclear", "content_review": {}}
    result = enforce_reviewed_defaults("risks", old)
    assert result["content_review"] == {"read_review_status": "legacy_unreviewed"}
    assert old["content_review"] == {}
