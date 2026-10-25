"""Old date approvals are audit history, not new API/cache authorization."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from mic.api import AnalystAPI
from mic.content_review_policy import DATE_REVIEW_PROTOCOL, PROTOCOL
from mic.publication_time import PublicationWindow
from mic.schemas import Passage, SearchHit
from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository
from mic.validate import BundleValidator


URL = "https://example.invalid/notice.pdf"


def _assessment(role, basis, evidence):
    return {"protocol": DATE_REVIEW_PROTOCOL, "possible_roles": [role],
            "selected_role": role, "basis": basis, "evidence": deepcopy(evidence)}


def _stored(tmp_path, *, date="2026-09-15"):
    db = Database(f"sqlite:///{tmp_path / 'dates.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("target", {}, {}, "q", "m")
    hit = SearchHit(query="offline", title="材料", url=URL, domain="example.invalid")
    link = repo.save_source_link(run, "q", hit, URL, "official")
    evidence = [{"passage_id": "p1", "quote": f"项目已于 {date} 完成交付。"}]
    public_evidence = [{"passage_id": "p2", "quote": "本公告公开发布日期为2026-09-16。"}]
    claim = {"id": "delivery", "kind": "observation", "status": "source_supported",
             "statement": "项目已完成交付。", "reason": "人工语义标签：日期明确属于已发生事项。",
             "evidence": evidence, "depends_on": [],
             "field_values": {"event_type": "capacity_expansion", "event_date": date,
                              "event_date_assessment": _assessment("event_occurrence", "explicit_occurrence", evidence)}}
    public = {"id": "publication", "kind": "observation", "status": "source_supported",
              "statement": "本公告于2026-09-16公开发布。", "reason": "人工标签：公开发布日期。",
              "evidence": public_evidence, "depends_on": [],
              "field_values": {"source_publication": {"published_at": "2026-09-16",
                                "date_role": "source_publication",
                                "assessment": _assessment("source_publication", "explicit_publication", public_evidence)}}}
    raw = {"decision": "save_structured", "overall_score": 85, "confidence": .9,
           "events": [{"event_type": "capacity_expansion", "event_date": date,
                       "summary": claim["statement"], "evidence_locator": {"passage_id": "p1"}}],
           "content_review": {"protocol": PROTOCOL, "claims": [claim, public],
                              "bindings": {"/events/0": ["delivery"], "/source_publication": ["publication"]}}}
    passages = [Passage(passage_id=e["passage_id"], section="正文", text=e["quote"])
                for e in evidence + public_evidence]
    validation = BundleValidator({}, require_content_review=True).validate(raw, passages)
    assert validation.schema_valid and len(validation.bundle.events) == 1
    bundle = validation.bundle
    assert bundle.events[0].event_date == date
    assert bundle.content_review["source_publication"]["status"] == "known"
    repo.save_merged_analysis("target", link, bundle, {}, search_run_id=run)
    publication = deepcopy(bundle.content_review["source_publication"])
    publication["time_window"] = {"status": "in_window", "allowed": True}
    repo.update_link_publication(link, publication)
    repo.update_link_read(link, "read", "body-hash", "simhash")
    return db, repo, run, link, bundle, publication


def _old_event(db, link):
    with db.session() as session:
        row = session.scalars(select(m.EventCardRow).where(m.EventCardRow.source_link_id == link)).one()
        audit = deepcopy(row.content_review)
        audit["fields"]["event_date"].pop("date_review")
        audit["claims"]["delivery"]["field_values"].pop("event_date")
        audit["claims"]["delivery"]["field_values"].pop("event_date_assessment")
        row.content_review = audit
        return deepcopy(audit), row.event_date.isoformat()


def _old_publication(db, link):
    with db.session() as session:
        row = session.get(m.SourceLink, link)
        metadata = deepcopy(row.metadata_)
        publication = metadata["publication_time"]
        publication.pop("date_review")
        publication.pop("date_assessments")
        row.metadata_ = metadata
        merged = session.scalars(select(m.MergedAnalysis).where(m.MergedAnalysis.source_link_id == link)).one()
        review = deepcopy(merged.content_review)
        review["source_publication"] = deepcopy(publication)
        merged.content_review = review
        return deepcopy(publication)


@pytest.mark.parametrize("date,stored", [("2026-09-15", "2026-09-15T00:00:00"),
                                        ("2026-09-15T09:30:00+08:00", "2026-09-15T01:30:00")])
def test_current_date_review_survives_sqlite_readback_and_cache(tmp_path, date, stored):
    db, repo, _, link, bundle, publication = _stored(tmp_path, date=date)
    event, = repo.get_recent_events("target")
    assert event["event_date"] == stored
    assert event["content_review"] == bundle.events[0].content_review
    assert repo.find_analyzed_link_by_canonical(URL, "target", reviewed_only=True).id == link
    assert repo.find_analyzed_link_by_content_hash("body-hash", "target", reviewed_only=True).id == link
    cloned = repo.clone_latest_analysis(link, "copy", "target", reviewed_only=True, publication=publication)
    assert cloned["events"] == 1 and cloned["cloned_events"][0]["event_date"] == stored
    assert all(e["event_date"] == stored for e in repo.get_recent_events("target"))


def test_legacy_event_date_projects_null_without_changing_db_or_resealing(tmp_path):
    db, repo, _, link, _, _ = _stored(tmp_path)
    audit, original_date = _old_event(db, link)
    api = AnalystAPI.__new__(AnalystAPI)
    api.repo = repo
    event, = api.get_recent_events("target")
    assert event["event_date"] is None and event["summary"] == "项目已完成交付。"
    review = event["content_review"]
    assert review["claims"] == audit["claims"] and review["fields"] == audit["fields"]
    assert review["prior_materialized_digest"] == audit["materialized_digest"]
    assert "materialized_digest" not in review
    correction, = review["materialization_corrections"]
    assert correction["original_materialized_value"] == original_date
    with db.session() as session:
        stored = session.scalars(select(m.EventCardRow)).one()
        assert stored.event_date.isoformat() == original_date and stored.content_review == audit


def test_legacy_publication_projects_unknown_for_source_api_and_metadata(tmp_path):
    db, repo, run, link, _, _ = _stored(tmp_path)
    old = _old_publication(db, link)
    explanation = repo.explain_source_analysis(link)
    assert explanation["source"]["publish_time"] is None
    projections = [explanation["source"]["publication_time"],
                   explanation["content_review"]["source_publication"],
                   repo.source_links_for_run(run)[0]["metadata"]["publication_time"]]
    for publication in projections:
        assert publication["status"] == "unknown" and publication["published_at"] is None
        assert publication["time_window"]["allowed"] is False
        assert publication["date_review_correction"]["original_materialized_value"] == old
    with db.session() as session:
        assert session.get(m.SourceLink, link).metadata_["publication_time"] == old
        assert session.scalars(select(m.MergedAnalysis)).one().content_review["source_publication"] == old


@pytest.mark.parametrize("legacy", ["event", "publication"])
def test_legacy_date_analysis_cannot_be_reused_as_current_review(tmp_path, legacy):
    db, repo, _, link, _, _ = _stored(tmp_path)
    (_old_event if legacy == "event" else _old_publication)(db, link)
    assert repo.find_analyzed_link_by_canonical(URL, "target", reviewed_only=True) is None
    assert repo.find_analyzed_link_by_content_hash("body-hash", "target", reviewed_only=True) is None
    refused = repo.clone_latest_analysis(link, "copy", "target", reviewed_only=True)
    assert refused["cache_reuse_rejected"] == "date_review_requires_refresh"
    assert refused["events"] == 0 and refused["cloned_events"] == []
    with db.session() as session:
        assert session.query(m.MergedAnalysis).count() == session.query(m.EventCardRow).count() == 1


def test_non_strict_clone_cannot_reissue_legacy_model_publication_or_dependent_text(tmp_path):
    db, repo, _, link, _, _ = _stored(tmp_path)
    _old_publication(db, link)
    refused = repo.clone_latest_analysis(link, "copy", "target")
    assert refused["cache_reuse_rejected"] == "date_review_requires_refresh"
    assert refused["events"] == refused["briefs"] == 0


def test_explicit_legacy_clone_keeps_audit_but_never_reissues_old_event_date(tmp_path):
    db, repo, _, link, _, _ = _stored(tmp_path)
    audit, original_date = _old_event(db, link)
    cloned = repo.clone_latest_analysis(link, "copy", "target")
    assert cloned["events"] == 1 and cloned["cloned_events"][0]["event_date"] is None
    assert cloned["cloned_events"][0]["content_review"]["prior_materialized_digest"] == audit["materialized_digest"]
    with db.session() as session:
        rows = {row.source_link_id: row for row in session.scalars(select(m.EventCardRow))}
        assert rows[link].event_date.isoformat() == original_date
        assert rows["copy"].event_date is None


def test_parser_publication_metadata_keeps_its_independent_provenance(tmp_path):
    _, repo, run, link, _, _ = _stored(tmp_path)
    publication = {"status": "known", "published_at": "2026-09-16T00:00:00+08:00",
                   "source": "meta:article:published_time", "precision": "day"}
    repo.update_link_publication(link, publication)
    assert repo.source_links_for_run(run)[0]["metadata"]["publication_time"] == publication
    assert repo.explain_source_analysis(link)["source"]["publication_time"] == publication


def test_pipeline_window_finalization_refuses_legacy_model_publication(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from mic.pipeline import Pipeline, RunStats
    from mic.reader import ReadResult
    from mic.schemas import TriageResult
    from tests.browser_doubles import make_context

    db, repo, _, link, _, _ = _stored(tmp_path)
    old = _old_publication(db, link)
    pipe = Pipeline.__new__(Pipeline)
    pipe.repo = repo
    pipe.reader = SimpleNamespace(access_profile_id="offline")
    read = ReadResult(source_link_id=link, read_status="read")
    hit = SearchHit(query="offline", title="材料", url=URL, domain="example.invalid")
    triage = TriageResult(source_link_id=link, triage_decision="read")
    window = PublicationWindow(90, datetime(2026, 10, 1, tzinfo=timezone.utc))
    stats = RunStats()
    stats.time_window_filter = {"passed": 0, "filtered_by_reason": {}, "filtered_links": []}
    context = make_context()
    assert not pipe._finish_publication(read, hit, triage, stats, context, window, old,
                                        {"status": "unknown", "published_at": None})
    assert read.publish_time is None and read.publication_time["status"] == "unknown"
    assert read.publication_time["time_window"]["allowed"] is False
    assert old["status"] == "known" and old["time_window"]["allowed"] is True
