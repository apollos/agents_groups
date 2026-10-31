"""Offline Pipeline/Repository/API coverage delivery with human-authored model doubles.

Search, reading and extraction are substituted; review, merge, persistence and
formal API readback are real. Passing these tests proves contract enforcement,
not that a live model understands every research direction.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from mic.api import AnalystAPI
from mic.content_review_policy import PROTOCOL
from mic.modeling.adapter import ModelAdapter, ModelCallResult
from mic.modeling.call_planner import LinkModelResult, ModelCallPlanner
from mic.planner import PlannedQuery
from mic.reader import ReadResult
from mic.schemas import CoverageGap, Passage, SearchHit, TriageResult
from mic.search import SearchProvider


TARGET = "company_300750"
QUESTION = "核查交付、经营和融资进展，并提供原文依据"
TEXT = "本季度已经交付设备。"


def _raw(*, assessment="partial"):
    review = {
        "protocol": PROTOCOL,
        "claims": [{"id": "answer", "kind": "observation", "status": "source_supported",
                    "statement": TEXT, "reason": "正文描述交付进度。",
                    "evidence": [{"passage_id": "p1", "quote": TEXT}], "depends_on": []}],
        "bindings": {"/facts/0": ["answer"]},
    }
    if assessment is not None:
        review["task_coverage"] = [{
            "question_id": "q1", "status": assessment, "answer_claim_ids": ["answer"],
            "uncovered_aspects": [] if assessment == "covered" else ["经营", "融资进展"],
            "reason": "人工标注的测试响应；不能作为真实模型理解正确的证据。",
        }]
    return {
        "schema_version": "bundle_extraction_v0.3", "decision": "save_structured", "overall_score": 80, "confidence": .9,
        "facts": [{"fact_type": "claim", "fact_statement": TEXT, "confidence": .9,
                   "evidence_locator": {"passage_id": "p1"}}],
        "content_review": review,
    }


class _Sources(SearchProvider):
    name = "mock"

    def __init__(self, state):
        self.state = state

    def search(self, query, query_family=None, limit=10):
        self.state.searches += 1
        if self.state.empty_search:
            return []
        return [SearchHit(
            query=query, query_family=query_family, title="离线资料", snippet=TEXT,
            url=f"https://example.invalid/{self.state.source}", domain="example.invalid",
            rank=1, provider="mock",
        )]


@pytest.fixture
def harness(config, monkeypatch):
    config.raw["search_providers"]["active"] = "mock"
    config.raw["call_governance"]["batching"]["serp_batch_triage"] = False
    state = SimpleNamespace(
        raw=_raw(), additional_raw=None, source="source-a", empty_search=False, mode="output",
        searches=0, reads=0, model_calls=0, after_model=None, contexts=[],
    )
    api = AnalystAPI(config)
    pipe = api.pipeline
    pipe.search = _Sources(state)
    pipe.reader.search_provider = pipe.search
    monkeypatch.setattr(pipe.planner, "plan", lambda *a, **kw: [
        PlannedQuery("离线研究问题", "task_question", 100),
    ])
    monkeypatch.setattr(pipe.triage, "triage", lambda hit, link_id, **kw: TriageResult(
        source_link_id=link_id, triage_decision="read", read_priority=90, need_model=True,
    ))

    def read(link_id, url, profile, context=None, **kwargs):
        state.reads += 1
        return ReadResult(
            source_link_id=link_id, read_status="read", title="离线资料", http_status=200,
            content_hash="offline-body-" + state.source, simhash="0", content_length=len(TEXT),
            passages=[Passage(passage_id="p1", section="正文", text=TEXT)],
            publication_time={"status": "unknown"},
        )

    def extract(planner, profile, read, source_metadata, triage, source_type, materiality):
        state.contexts.append(deepcopy(planner.task_context))
        if state.mode == "no_model":
            return LinkModelResult(read.source_link_id, "bundle_extraction", "no_model")
        responses = [state.raw]
        if state.additional_raw is not None:
            responses.append(state.additional_raw)
        assert planner.budget.can_call(len(responses))
        planner.budget.record(len(responses))
        state.model_calls += len(responses)
        result = LinkModelResult(
            read.source_link_id, "bundle_extraction",
            "parallel_ensemble" if len(responses) > 1 else "single_model",
            outputs=[ModelCallResult(
                model_config_id=f"offline-contract-model-{index}", provider="offline", provider_type="offline",
                model_name="human-authored-contract", status="success", parsed=deepcopy(raw),
                is_mock=True,
            ) for index, raw in enumerate(responses)],
        )
        if state.after_model:
            state.after_model()
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected real model adapter call in offline task coverage test")

    monkeypatch.setattr(pipe.reader, "read", read)
    monkeypatch.setattr(ModelCallPlanner, "run_for_link", extract)
    monkeypatch.setattr(ModelAdapter, "complete", forbidden)
    return api, state


def _collect(api, question=QUESTION, *, max_model_calls=1, **run_options):
    return api.collect_intelligence(TARGET, {
        "time_window": "", "focus": ["operating_update"], "questions": [question],
        "budget_profile": {"max_queries": 1, "max_search_hits": 1,
                           "max_links_to_read": 1, "max_model_calls": max_model_calls},
    }, run_options=run_options)


def _assert_formal_gaps(api, report):
    actual = [gap for gap in api.get_coverage_gaps(TARGET)
              if gap["search_run_id"] == report["search_run_id"]]
    assert sorted(report["coverage_gaps"], key=lambda gap: gap["gap_id"]) == sorted(
        actual, key=lambda gap: gap["gap_id"],
    )
    assert report["structured_outputs"]["coverage_gaps"] == len(actual)
    for gap in actual:
        assert gap["gap_id"] and gap["search_run_id"] == report["search_run_id"]
        assert "source_link_id" in gap and gap["status"] == "open"
    return actual


def test_cached_legacy_risk_keeps_old_audit_but_never_republishes_held_defaults(harness, monkeypatch):
    from sqlalchemy import select
    from mic.content_review import seal
    from mic.store import models as m

    api, state = harness
    state.raw["risks"] = [{"risk_summary": TEXT, "severity": "medium",
                           "time_horizon": "medium_term", "evidence_locator": {"passage_id": "p1"}}]
    state.raw["content_review"]["claims"].append({
        "id": "risk", "kind": "analysis", "status": "source_supported",
        "statement": TEXT, "reason": "离线替身标注：仅正文陈述有支持，等级期限未审核。",
        "evidence": [{"passage_id": "p1", "quote": TEXT}], "depends_on": [],
    })
    state.raw["content_review"]["bindings"]["/risks/0"] = ["risk"]
    save = api.pipeline.repo.save_merged_analysis

    def legacy_save(target_id, source_link_id, bundle, *args, **kwargs):
        bundle.risks[0].severity = "low"
        bundle.risks[0].time_horizon = "near_term"
        seal(bundle)  # the old sealed materialization, not a post-seal mutation
        return save(target_id, source_link_id, bundle, *args, **kwargs)

    monkeypatch.setattr(api.pipeline.repo, "save_merged_analysis", legacy_save)
    first = _collect(api)
    monkeypatch.setattr(api.pipeline.repo, "save_merged_analysis", save)
    second = _collect(api)
    assert first["summary"]["model_calls"] == state.model_calls == 1
    assert second["summary"]["cached_or_reused_results"] == 1
    assert second["summary"]["model_calls"] == 0
    formal = api.get_risks(TARGET)
    assert len(formal) == 2
    assert all(row["severity"] == "unknown" and row["time_horizon"] == "unclear" for row in formal)
    assert all("materialized_digest" not in row["content_review"] for row in formal)
    assert any(review.get("cache_reused") for review in second["content_reviews"])
    with api.repo.db.session() as session:
        originals = session.scalars(select(m.ModelOutput)).all()
        assert len(originals) == 1  # raw model JSON is audit, not cloned publication
        assert originals[0].output_json["risks"][0]["severity"] == "medium"
        rows = session.scalars(select(m.RiskFlagRow).order_by(m.RiskFlagRow.created_at)).all()
        assert [row.severity for row in rows] == ["low", "unknown"]
        assert [row.time_horizon for row in rows] == ["near_term", "unclear"]
        assert "materialized_digest" in rows[0].content_review
        assert "materialized_digest" not in rows[1].content_review
        merged = session.scalars(select(m.MergedAnalysis)).all()
        assert all("risks" not in trace and "bundle" not in trace
                   for row in merged for trace in row.model_outputs)


def test_partial_model_assessment_reaches_report_and_formal_api_without_extra_calls(harness):
    api, state = harness
    report = _collect(api)
    coverage, = report["task_coverage"]
    assert coverage["question_id"] == "q1" and coverage["question"] == QUESTION
    assert coverage["status"] == "partial"
    assert coverage["uncovered_aspects"] == ["经营", "融资进展"]
    assert coverage["answer_claim_ids"] == ["answer"]
    assert coverage["source_link_id"]
    gaps = _assert_formal_gaps(api, report)
    matching = [gap for gap in gaps if gap["gap_type"] == "task_question_coverage"]
    assert len(matching) == 1
    assert matching[0]["source_link_id"] == coverage["source_link_id"]
    assert "未覆盖的需求原文：经营；融资进展" in matching[0]["description"]
    assert "人工标注" not in matching[0]["description"]
    assert [fact["fact_statement"] for fact in api.search_facts(TARGET)] == [TEXT]
    assert report["summary"]["model_calls"] == state.model_calls == 1
    assert report["summary"]["gateway_requests_sent"] == 0
    assert state.contexts[0]["questions"][0]["question"] == QUESTION


def test_old_model_output_keeps_supported_fact_without_claiming_question_covered(harness):
    api, state = harness
    state.raw = _raw(assessment=None)
    report = _collect(api)
    coverage, = report["task_coverage"]
    assert coverage["status"] == "not_assessed"
    assert coverage["answer_claim_ids"] == []
    assert report["structured_outputs"]["facts"] == 1
    assert [fact["fact_statement"] for fact in api.search_facts(TARGET)] == [TEXT]
    gaps = _assert_formal_gaps(api, report)
    assert any(QUESTION in gap["description"] and "尚未完成" in gap["description"] for gap in gaps)
    assert report["summary"]["model_calls"] == state.model_calls == 1


def test_skipped_bundle_cannot_publish_fact_or_claim_question_covered(harness):
    api, state = harness
    state.raw = {**_raw(assessment="covered"), "decision": "skip"}
    report = _collect(api)
    coverage, = report["task_coverage"]
    assert coverage["status"] == "not_assessed" and coverage["answer_claim_ids"] == []
    assert report["structured_outputs"]["facts"] == 0
    assert api.search_facts(TARGET) == []
    gaps = _assert_formal_gaps(api, report)
    assert len(gaps) == 1 and gaps[0]["gap_type"] == "task_question_coverage"
    assert gaps[0]["source_link_id"] == coverage["source_link_id"]
    assert report["summary"]["model_calls"] == state.model_calls == 1


def test_multiple_contributions_keep_more_than_eight_formal_gaps(harness):
    api, state = harness
    descriptions = [f"已有来源缺口 {index}，等待更多材料。" for index in range(10)]
    state.additional_raw = _raw()
    for raw, start in ((state.raw, 0), (state.additional_raw, 5)):
        raw["coverage_gaps"] = [{
            "gap_type": "missing_metric", "description": description, "priority": "medium",
        } for description in descriptions[start:start + 5]]
    report = _collect(api, max_model_calls=2)
    gaps = _assert_formal_gaps(api, report)
    assert {gap["description"] for gap in gaps if gap["gap_type"] == "missing_metric"} == set(descriptions)
    assert len(gaps) == 11  # Ten existing source gaps plus the reviewed task-coverage gap.
    assert report["task_coverage"][0]["status"] == "partial"
    assert report["summary"]["model_calls"] == state.model_calls == 2


def test_empty_run_has_explicit_unassessed_gap_without_model_call(harness):
    api, state = harness
    state.empty_search = True
    report = _collect(api)
    coverage, = report["task_coverage"]
    assert coverage["status"] == "not_assessed" and coverage["source_link_id"] is None
    gaps = _assert_formal_gaps(api, report)
    assert len(gaps) == 1 and gaps[0]["source_link_id"] is None
    assert QUESTION in gaps[0]["description"]
    assert report["summary"]["model_calls"] == state.model_calls == 0
    assert not report["collection_diagnostics"]["usable"]


@pytest.mark.parametrize("mode", ["no_model", "invalid_output"])
def test_read_source_without_valid_contribution_still_reports_unassessed(harness, mode):
    api, state = harness
    state.mode = mode
    if mode == "invalid_output":
        state.raw = {"facts": "not-a-list"}
    report = _collect(api)
    coverage, = report["task_coverage"]
    assert coverage["status"] == "not_assessed" and coverage["source_link_id"]
    gaps = _assert_formal_gaps(api, report)
    assert len(gaps) == 1 and gaps[0]["source_link_id"] == coverage["source_link_id"]
    assert api.search_facts(TARGET) == []
    assert report["summary"]["model_calls"] == state.model_calls == (0 if mode == "no_model" else 1)


def test_cached_source_does_not_reuse_old_question_coverage(harness):
    api, state = harness
    state.raw = _raw(assessment="covered")
    first = _collect(api, "核查交付进展并提供原文依据")
    assert first["task_coverage"][0]["status"] == "covered"
    assert _assert_formal_gaps(api, first) == []
    changed_question = "核查融资进展并给出金额和来源"
    second = _collect(api, changed_question)
    coverage, = second["task_coverage"]
    assert coverage["question"] == changed_question and coverage["status"] == "not_assessed"
    assert coverage["answer_claim_ids"] == []
    assert coverage["source_link_id"] != first["task_coverage"][0]["source_link_id"]
    assert second["summary"]["cached_or_reused_results"] == 1
    assert second["summary"]["model_calls"] == 0 and state.model_calls == 1
    assert second["structured_outputs"]["facts"] == 1
    gaps = _assert_formal_gaps(api, second)
    assert len(gaps) == 1 and changed_question in gaps[0]["description"]
    assert gaps[0]["source_link_id"] == coverage["source_link_id"]
    merged_audit = api.explain_source_analysis(coverage["source_link_id"])["content_review"]
    report_audit, = [review for review in second["content_reviews"] if review.get("cache_reused")]
    for audit in (merged_audit, report_audit):
        assert "task_coverage" not in audit
        assert "task_coverage_generated_gaps" not in audit
        historical = audit["cached_task_coverage"]
        assert historical["source_link_id"] == first["task_coverage"][0]["source_link_id"]
        assert historical["task_coverage"] == first["task_coverage"]
        assert historical["task_coverage"][0]["question"] != changed_question


def test_report_exposes_current_run_gaps_without_leaking_prior_run(harness):
    api, state = harness
    api.repo.save_coverage_gaps("offline-historical-run", TARGET, [CoverageGap(
        gap_type="historical_test_gap", description="仅属于历史运行的人工样例。",
    )])
    state.empty_search = True
    report = _collect(api)
    assert any(gap["search_run_id"] == "offline-historical-run" for gap in api.get_coverage_gaps(TARGET))
    gaps = _assert_formal_gaps(api, report)
    assert len(gaps) == 1
    assert {gap["search_run_id"] for gap in gaps} == {report["search_run_id"]}
    assert all(gap["gap_type"] != "historical_test_gap" for gap in gaps)


@pytest.mark.parametrize("stop", ["cancelled", "timed_out"])
def test_cancel_or_deadline_after_model_does_not_write_new_coverage(harness, stop):
    api, state = harness
    stopped = {"cancelled": False, "clock": 0.0}

    def stop_after_model():
        stopped["cancelled"] = stop == "cancelled"
        stopped["clock"] = 10.0 if stop == "timed_out" else 0.0

    state.after_model = stop_after_model
    report = _collect(api, deadline_seconds=5, clock=lambda: stopped["clock"],
                      cancel_check=lambda: stopped["cancelled"])
    assert report["collection_diagnostics"]["execution_status"] == stop
    assert state.model_calls == 1
    assert report["task_coverage"] == []
    assert _assert_formal_gaps(api, report) == []
    assert api.search_facts(TARGET) == []
