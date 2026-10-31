"""Offline date admission through the real planner, review, store and formal API.

Human-authored model responses test execution of the contract. They do not
establish that a live model will correctly identify a publication or latest report.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from mic.api import AnalystAPI
from mic.content_review_policy import PROTOCOL
from mic.modeling.adapter import ModelAdapter, ModelCallResult
from mic.planner import PlannedQuery
from mic.reader import ReadResult
from mic.schemas import Passage, SearchHit, TriageResult
from mic.search import SearchProvider
from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository


TARGET = "company_300750"
NOW = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
QUESTION = "确认最新已披露报告，并提供报告期内出货量"
FACT = "2026年上半年出货量为120台。"
UNKNOWN = {"status": "unknown", "published_at": None, "reason": "pdf_publication_not_verified"}


def _date_text(date):
    return f"本报告公开披露日期为 {date}。报告期末为2026-06-30。"


def _raw(date="2026-09-15"):
    return {
        "schema_version": "bundle_extraction_v0.3", "decision": "save_structured", "overall_score": 85, "confidence": .9,
        "facts": [{"fact_type": "claim", "fact_statement": FACT,
                   "evidence_locator": {"passage_id": "p1"}}],
        "metrics": [{"metric_name": "出货量", "metric_value": 120, "unit": "台",
                     "period": "2026H1", "evidence_locator": {"passage_id": "p1"}}],
        "content_review": {
            "protocol": PROTOCOL,
            "claims": [
                {"id": "quantity", "kind": "observation", "status": "source_supported",
                 "statement": FACT, "reason": "人工标签：正文明确给出报告期、数值和单位。",
                 "evidence": [{"passage_id": "p1", "quote": FACT}], "depends_on": []},
                {"id": "publication", "kind": "observation", "status": "source_supported",
                 "statement": f"本报告于 {date} 公开披露。",
                 "reason": "人工标签：日期属于当前报告公开披露，不是报告期末。",
                 "evidence": [{"passage_id": "p2", "quote": _date_text(date)}], "depends_on": [],
                 "field_values": {"source_publication": {
                     "published_at": date, "date_role": "source_publication",
                     "assessment": {"protocol": "date_review_v1", "possible_roles": ["source_publication"],
                         "selected_role": "source_publication", "basis": "explicit_publication",
                         "evidence": [{"passage_id": "p2", "quote": _date_text(date)}]}}}},
            ],
            "bindings": {"/facts/0": ["quantity"], "/metrics/0": ["quantity"],
                         "/source_publication": ["publication"]},
            "task_coverage": [{"question_id": "q1", "status": "partial",
                               "answer_claim_ids": ["quantity"],
                               "uncovered_aspects": ["最新已披露报告"],
                               "reason": "仅有当前报告，缺少截至任务时点的完整披露目录。"}],
        },
    }


class _Sources(SearchProvider):
    name = "mock"

    def __init__(self, state):
        self.state = state

    def search(self, query, query_family=None, limit=10):
        self.state.searches += 1
        return [SearchHit(query=query, query_family=query_family, title="离线半年报告",
                          snippet=FACT, url=f"https://example.invalid/{self.state.source}", domain="example.invalid",
                          rank=1, provider="mock", publish_time_guess="2026-10-03")]


@pytest.fixture
def harness(config, monkeypatch):
    monkeypatch.setattr("mic.publication_time.utcnow", lambda: NOW)
    config.raw["search_providers"]["active"] = "mock"
    config.raw["call_governance"]["batching"]["serp_batch_triage"] = False
    config.raw["model_policies"]["tasks"] = {
        "bundle_extraction": {"call_mode": "single_model", "models": [
            {"model_id": "offline-publication-0", "priority": 1, "weight": 1.0}]},
    }
    state = SimpleNamespace(raws=[_raw()], dates=["2026-09-15"], extracted=deepcopy(UNKNOWN),
                            model_calls=0, reads=0, searches=0, after_model=None, after_read=None,
                            mode="output", prompts=[], source="report.pdf")
    api = AnalystAPI(config)
    pipe = api.pipeline
    pipe.search = _Sources(state)
    pipe.reader.search_provider = pipe.search
    monkeypatch.setattr(pipe.planner, "plan", lambda *a, **kw: [
        PlannedQuery("离线报告研究问题", "task_question", 100)])
    monkeypatch.setattr(pipe.triage, "triage", lambda hit, link_id, **kw: TriageResult(
        source_link_id=link_id, triage_decision="read", read_priority=90, need_model=True))

    def read(link_id, url, profile, context=None, **kwargs):
        state.reads += 1
        passages = [Passage(passage_id="p1", section="正文", text=FACT),
                    Passage(passage_id="p2", section="正文", text="\n".join(_date_text(d) for d in state.dates))]
        result = ReadResult(
            source_link_id=link_id, read_status="read", title="离线半年报告", http_status=200,
            document_type="pdf", content_type="application/pdf", content_hash="publication-offline-body",
            simhash="0", content_length=sum(len(p.text) for p in passages), passages=passages,
            publish_time=state.extracted.get("published_at"), publication_time=deepcopy(state.extracted))
        if state.after_read:
            state.after_read()
        return result

    def complete(model_id, messages):
        state.model_calls += 1
        state.prompts.append(json.loads(messages[-1]["content"]))
        index = int(model_id.rsplit("-", 1)[-1])
        result = ModelCallResult(
            model_config_id=model_id, provider="offline", provider_type="offline",
            model_name="human-labelled-publication", status="request_failed" if state.mode == "failure" else "success",
            parsed=None if state.mode == "failure" else deepcopy(state.raws[index]), is_mock=True)
        if state.after_model:
            state.after_model()
        return result

    def adapter(model_id):
        if state.mode == "unavailable":
            return None
        assert model_id.startswith("offline-publication-"), "unexpected model policy"
        return SimpleNamespace(usable=True, complete=lambda messages: complete(model_id, messages))

    def forbidden(*args, **kwargs):
        pytest.fail("real adapter call prohibited in publication pipeline fixtures")

    monkeypatch.setattr(pipe.reader, "read", read)
    monkeypatch.setattr(pipe.registry, "get", adapter)
    monkeypatch.setattr(ModelAdapter, "complete", forbidden)
    return api, state


def _collect(api, *, max_model_calls=1, time_window="30d", **run_options):
    return api.collect_intelligence(TARGET, {
        "time_window": time_window, "focus": ["operating_update"], "questions": [QUESTION],
        "budget_profile": {"max_queries": 1, "max_search_hits": 1,
                           "max_links_to_read": 1, "max_model_calls": max_model_calls},
    }, run_options=run_options)


def _source(report):
    row, = report["source_publications"]
    return row["source_link_id"], row["publication_time"]


def _assert_no_formal_content(api, report):
    assert api.search_facts(TARGET) == []
    assert api.get_metric_observations(TARGET) == []
    assert report["structured_outputs"]["facts"] == report["structured_outputs"]["metrics"] == 0
    with api.repo.db.session() as session:
        assert session.query(m.MergedAnalysis).count() == 0


def test_unknown_pdf_uses_one_existing_extraction_and_date_survives_formal_readback(harness):
    api, state = harness
    report = _collect(api)
    link_id, publication = _source(report)
    assert report["summary"]["model_calls"] == state.model_calls == 1
    assert report["summary"]["gateway_requests_sent"] == 0
    assert publication["status"] == "known"
    assert publication["published_at"] == "2026-09-15T00:00:00+08:00"
    assert publication["time_window"]["allowed"] is True
    assert publication["extracted_publication"] == UNKNOWN
    assert publication["evidence"] == [{"passage_id": "p2", "quote": _date_text("2026-09-15")}]
    assert [fact["fact_statement"] for fact in api.search_facts(TARGET)] == [FACT]
    assert report["structured_outputs"]["metrics"] == 1
    prompt = state.prompts[0]
    assert prompt["source_metadata"]["publication_time"] == UNKNOWN
    assert prompt["source_metadata"]["collection_time_window"]["days"] == 30

    # Reopen the database through a fresh repository; do not rely on in-memory hit mutation.
    database = Database(api.config.database_url)
    try:
        fresh = Repository(database)
        source = fresh.explain_source_analysis(link_id)["source"]
        assert source == api.explain_source_analysis(link_id)["source"]
        assert source["publish_time"] == publication["published_at"]
        assert source["publish_time_guess"].startswith("2026-10-03")
        assert source["publication_time"] == publication
        attempt, = fresh.read_attempts_for_link(link_id)
        assert attempt["diagnostics"]["publication_time"] == UNKNOWN
        assert attempt["diagnostics"]["fetch"]["time_window"]["allowed"] is False
        with database.session() as session:
            original = session.query(m.LinkReadAttempt).filter_by(source_link_id=link_id).one()
            assert original.extracted_publish_time is None
    finally:
        database.engine.dispose()


@pytest.mark.parametrize("damage,expected", [
    ("report_period", "publication_time_unverified"),
    ("no_binding", "publication_time_unverified"),
    ("invalid_quote", "publication_time_unverified"),
    ("old", "outside_time_window"),
    ("future", "future_publication_time"),
    ("conflict", "publication_time_unverified"),
])
def test_model_date_failure_keeps_audit_without_publishing_source_content(harness, damage, expected):
    api, state = harness
    if damage in {"old", "future"}:
        date = "2026-01-01" if damage == "old" else "2026-10-05"
        state.raws, state.dates = [_raw(date)], [date]
    elif damage == "report_period":
        state.raws[0]["content_review"]["claims"][1]["field_values"]["source_publication"]["date_role"] = "report_period"
    elif damage == "no_binding":
        state.raws[0]["content_review"]["bindings"].pop("/source_publication")
    elif damage == "invalid_quote":
        state.raws[0]["content_review"]["claims"][1]["evidence"][0]["quote"] = "不存在于材料的日期"
    else:
        state.raws.append(_raw("2026-09-16"))
        state.dates.append("2026-09-16")
        api.config.raw["model_policies"]["tasks"]["bundle_extraction"] = {
            "call_mode": "parallel_ensemble", "models": [
                {"model_id": f"offline-publication-{index}", "weight": 1.0} for index in range(2)]}
    expected_calls = 2 if damage == "conflict" else 1
    report = _collect(api, max_model_calls=expected_calls)
    _assert_no_formal_content(api, report)
    link_id, publication = _source(report)
    assert publication["time_window"]["status"] == expected
    assert publication["time_window"]["allowed"] is False
    if damage == "conflict":
        assert publication["status"] == "conflict"
    assert report["summary"]["model_calls"] == state.model_calls == expected_calls
    assert report["collection_diagnostics"]["output_status"] == "time_window_filtered"
    with api.repo.db.session() as session:
        outputs = session.query(m.ModelOutput).filter_by(source_link_id=link_id).all()
        assert len(outputs) == expected_calls
        assert [row.output_json for row in outputs] == state.raws
    assert any(audit.get("publication_filtered") for audit in report["content_reviews"])
    assert any(gap["gap_type"] == expected for gap in report["coverage_gaps"])


@pytest.mark.parametrize("publication,expected", [
    ({"status": "known", "published_at": "2026-01-01", "precision": "day", "source": "meta"}, "outside_time_window"),
    ({"status": "known", "published_at": "2026-10-05", "precision": "day", "source": "meta"}, "future_publication_time"),
    ({"status": "conflict", "published_at": None, "reason": "conflicting_publication_dates"}, "publication_time_unverified"),
])
def test_known_stale_future_and_conflicting_metadata_never_reach_model(harness, publication, expected):
    api, state = harness
    state.extracted = publication
    report = _collect(api)
    _assert_no_formal_content(api, report)
    _, resolved = _source(report)
    assert resolved["time_window"]["status"] == expected
    assert state.model_calls == report["summary"]["model_calls"] == 0
    with api.repo.db.session() as session:
        assert session.query(m.ModelRun).count() == 0


@pytest.mark.parametrize("mode,budget,expected_calls", [
    ("output", 0, 0), ("unavailable", 1, 0), ("failure", 1, 1),
])
def test_no_budget_or_no_model_output_stays_unknown_without_extra_attempts(harness, mode, budget, expected_calls):
    api, state = harness
    state.mode = mode
    report = _collect(api, max_model_calls=budget)
    _assert_no_formal_content(api, report)
    _, publication = _source(report)
    assert publication["status"] == "unknown"
    assert publication["time_window"]["allowed"] is False
    assert state.model_calls == report["summary"]["model_calls"] == expected_calls
    assert report["summary"]["gateway_requests_sent"] == 0


@pytest.mark.parametrize("stop", ["cancelled", "timed_out"])
@pytest.mark.parametrize("stage", ["after_read", "after_model"])
def test_stop_fence_prevents_late_date_admission_and_business_writes(harness, stop, stage):
    api, state = harness
    stopped = {"cancelled": False, "clock": 0.0}

    def stop_now():
        stopped["cancelled"] = stop == "cancelled"
        stopped["clock"] = 10.0 if stop == "timed_out" else 0.0

    setattr(state, stage, stop_now)
    report = _collect(api, deadline_seconds=5, clock=lambda: stopped["clock"],
                      cancel_check=lambda: stopped["cancelled"])
    assert report["collection_diagnostics"]["execution_status"] == stop
    assert state.model_calls == (1 if stage == "after_model" else 0)
    assert report["source_publications"] == []
    assert report["task_coverage"] == [] and report["coverage_gaps"] == []
    _assert_no_formal_content(api, report)
    with api.repo.db.session() as session:
        # Completed calls survive the stop in audit only, never as business data.
        expected_calls = 1 if stage == "after_model" else 0
        assert session.query(m.ModelOutput).count() == expected_calls
        assert session.query(m.ModelRun).count() == expected_calls
        link = session.query(m.SourceLink).one()
        assert "publication_time" not in (link.metadata_ or {})


def test_verified_report_date_and_metric_do_not_replace_missing_latestness_evidence(harness):
    api, state = harness
    report = _collect(api)
    link_id, publication = _source(report)
    assert publication["status"] == "known"
    coverage, = report["task_coverage"]
    assert coverage["status"] == "partial"
    assert coverage["uncovered_aspects"] == ["最新已披露报告"]
    assert coverage["answer_claim_ids"] == ["quantity"]
    gaps = [gap for gap in api.get_coverage_gaps(TARGET)
            if gap["search_run_id"] == report["search_run_id"]]
    gap, = [gap for gap in gaps if gap["gap_type"] == "task_question_coverage"]
    assert gap["source_link_id"] == link_id
    assert "未覆盖的需求原文：最新已披露报告" in gap["description"]
    metrics, = api.get_metric_observations(TARGET)
    assert metrics["metric_name"] == "出货量" and metrics["metric_value"] == 120
    assert metrics["period"] == "2026H1" and metrics["unit"] == "台"
    delivered = api.explain_source_analysis(link_id)
    assert delivered["facts"] == [FACT]
    assert "最新" not in "".join(delivered["facts"])
    assert report["summary"]["model_calls"] == state.model_calls == 1


def test_content_hash_cache_preserves_current_publication_and_namespaces_historical_date(harness):
    api, state = harness
    first = _collect(api)
    old_link, old_publication = _source(first)
    historical_review = deepcopy(api.explain_source_analysis(old_link)["content_review"]["source_publication"])
    assert old_publication["published_at"].startswith("2026-09-15")

    # The body is unchanged, but this fetched URL carries independent newer publisher metadata.
    state.source = "republished-report.pdf"
    state.extracted = {"status": "known", "published_at": "2026-09-16T00:00:00+08:00",
                       "precision": "day", "source": "meta:article:published_time"}
    second = _collect(api)
    new_link, current = _source(second)
    assert new_link != old_link
    assert second["summary"]["cached_or_reused_results"] == 1
    assert second["summary"]["model_calls"] == 0 and state.model_calls == 1
    assert second["structured_outputs"]["facts"] == second["structured_outputs"]["metrics"] == 1
    explanation = api.explain_source_analysis(new_link)
    assert explanation["source"]["publish_time"] == current["published_at"] == state.extracted["published_at"]
    assert explanation["source"]["publish_time_guess"].startswith("2026-10-03")
    reported, = [review for review in second["content_reviews"] if review.get("cache_reused")]
    for audit in (explanation["content_review"], reported):
        assert audit["source_publication"]["published_at"] == state.extracted["published_at"]
        assert audit["source_publication"]["source"] == "meta:article:published_time"
        historical = audit["cached_source_publication"]
        assert historical["source_link_id"] == old_link
        assert historical["publication_time"] == historical_review
    assert api.explain_source_analysis(old_link)["content_review"]["source_publication"] == historical_review


@pytest.mark.parametrize("reuse", ["canonical", "content_hash"])
def test_cancel_during_cache_read_update_cannot_clone_new_facts(harness, monkeypatch, reuse):
    api, state = harness
    first = _collect(api)
    old_link, _ = _source(first)
    assert len(api.search_facts(TARGET)) == 1
    if reuse == "content_hash":
        state.source = "cached-report.pdf"
        state.extracted = {"status": "known", "published_at": "2026-09-16T00:00:00+08:00",
                           "precision": "day", "source": "meta:article:published_time"}
    stopped = {"cancelled": False, "updates": 0}
    original_update = api.pipeline.repo.update_link_read

    def update_then_cancel(*args, **kwargs):
        original_update(*args, **kwargs)
        stopped["updates"] += 1
        stopped["cancelled"] = True

    monkeypatch.setattr(api.pipeline.repo, "update_link_read", update_then_cancel)
    second = _collect(api, time_window=None if reuse == "canonical" else "30d",
                      cancel_check=lambda: stopped["cancelled"])
    assert stopped["updates"] == 1
    assert second["collection_diagnostics"]["execution_status"] == "cancelled"
    assert state.model_calls == 1  # The first run's extraction only.
    assert second["structured_outputs"]["facts"] == second["structured_outputs"]["metrics"] == 0
    assert len(api.search_facts(TARGET)) == 1
    assert second["coverage_gaps"] == [] and second["task_coverage"] == []
    with api.repo.db.session() as session:
        new_link = session.query(m.SourceLink).filter_by(search_run_id=second["search_run_id"]).one()
        assert new_link.id != old_link
        assert session.query(m.MergedAnalysis).filter_by(source_link_id=new_link.id).count() == 0
        assert session.query(m.FactItemRow).filter_by(source_link_id=new_link.id).count() == 0
        assert session.query(m.MetricObservationRow).filter_by(source_link_id=new_link.id).count() == 0
