"""Offline recovery delivery through the real SDK adapter, pipeline and database.

Human-authored responses establish admission and accounting behavior, not live
model accuracy. Search, reading and the SDK transport cannot use the network.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from mic.api import AnalystAPI
from mic.content_review_policy import PROTOCOL
from mic.modeling import acceptance_trace as trace_module
from mic.modeling import adapter as adapter_module
from mic.modeling.adapter import ModelAdapter
from mic.planner import PlannedQuery
from mic.reader import ReadResult
from mic.schemas import Passage, SearchHit, TriageResult
from mic.search import SearchProvider
from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository


TARGET = "company_300750"
NOW = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
FACT = "2026年上半年出货量为120台。"
DATE_TEXT = "本报告公开披露日期为2026年9月15日，报告期末为2026年6月30日。"
BROKEN = '{"audit_marker":"broken-prior-answer-without-evidence"'
UNKNOWN = {"status": "unknown", "published_at": None,
           "reason": "pdf_publication_not_verified"}


def _reviewed_output():
    return {
        "schema_version": "bundle_extraction_v0.3", "decision": "save_structured", "overall_score": 85, "confidence": .9,
        "facts": [{"fact_type": "claim", "fact_statement": FACT, "confidence": .9,
                   "evidence_locator": {"passage_id": "p1"}}],
        "metrics": [{"metric_name": "出货量", "metric_value": 120, "unit": "台",
                     "period": "2026H1", "evidence_locator": {"passage_id": "p1"}}],
        "content_review": {
            "protocol": PROTOCOL,
            "claims": [
                {"id": "quantity", "kind": "observation", "status": "source_supported",
                 "statement": FACT, "reason": "人工标签：原文明确给出报告期、数量及单位。",
                 "evidence": [{"passage_id": "p1", "quote": FACT}], "depends_on": []},
                {"id": "publication", "kind": "observation", "status": "source_supported",
                 "statement": "本报告于2026年9月15日公开披露。",
                 "reason": "人工标签：当前材料明确区分公开披露日和报告期末。",
                 "evidence": [{"passage_id": "p2", "quote": DATE_TEXT}], "depends_on": [],
                 "field_values": {"source_publication": {
                     "published_at": "2026-09-15", "date_role": "source_publication",
                     "assessment": {"protocol": "date_review_v1",
                                    "possible_roles": ["source_publication"],
                                    "selected_role": "source_publication",
                                    "basis": "explicit_publication",
                                    "evidence": [{"passage_id": "p2", "quote": DATE_TEXT}]}}}},
            ],
            "bindings": {"/facts/0": ["quantity"], "/metrics/0": ["quantity"],
                         "/source_publication": ["publication"]},
            "task_coverage": [{"question_id": "q1", "status": "covered",
                               "answer_claim_ids": ["quantity"], "uncovered_aspects": [],
                               "reason": "人工标签：报告期、出货量与单位均有原文依据。"}],
        },
    }


class _Sources(SearchProvider):
    name = "mock"

    def search(self, query, query_family=None, limit=10):
        return [SearchHit(query=query, query_family=query_family, title="离线半年报告",
                          snippet=FACT, url="https://example.invalid/recovery.pdf",
                          domain="example.invalid", rank=1, provider="mock",
                          publish_time_guess="2026-10-03")]


@pytest.fixture
def recovery_delivery(config, monkeypatch, tmp_path):
    monkeypatch.setattr("mic.publication_time.utcnow", lambda: NOW)
    config.raw["search_providers"]["active"] = "mock"
    config.raw["call_governance"]["batching"]["serp_batch_triage"] = False
    config.raw["call_governance"]["budgets"]["max_model_calls_per_source_link"] = 2
    config.raw["call_governance"]["extraction"]["json_recovery"] = {"enabled": True}
    config.raw["model_policies"]["tasks"] = {
        "bundle_extraction": {"call_mode": "single_model", "models": [
            {"model_id": "offline-recovery", "priority": 1, "weight": 1.0}]},
    }
    fake_repo = tmp_path / "repo"
    logs = fake_repo / "tools/market_intelligence_collector/logs/acceptance/json_recovery"
    monkeypatch.setenv("MIC_LOG_DIR", str(logs))
    monkeypatch.setenv("MIC_ACCEPTANCE_MODEL_IO", "1")
    monkeypatch.setattr(trace_module, "_repo_root", lambda: fake_repo)

    def forbidden_sdk(*args, **kwargs):
        pytest.fail("real SDK construction forbidden in offline recovery delivery")

    monkeypatch.setattr(adapter_module, "OpenAI", forbidden_sdk)
    state = SimpleNamespace(raw=_reviewed_output(), requests=[], responses=[], results=[],
                            reads=0, clock=0.0, cancelled=False, after_recovery=None,
                            budget=None, logs=logs)
    adapter = ModelAdapter(
        model_config_id="offline-recovery", provider="offline",
        provider_type="openai_compatible_direct", endpoint="http://127.0.0.1:1/v1",
        model="offline-requested-model", api_key="offline-recovery-fixture-credential",
        allow_mock=False, max_output_tokens=65536, pricing={"input": 2, "output": 4})

    def create(**kwargs):
        state.requests.append(deepcopy(kwargs))
        attempt = len(state.requests)
        assert attempt <= 2, "recovery must not create an unbounded retry chain"
        state.budget = adapter.budget
        assert state.budget.used["gateway_requests_sent"] == attempt
        text = BROKEN if attempt == 1 else json.dumps(state.raw, ensure_ascii=False)
        state.responses.append(text)
        if attempt == 2 and state.after_recovery:
            state.after_recovery()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1000 * attempt, completion_tokens=500 * attempt),
            model="offline-served-model", id=f"offline-response-{attempt}")

    adapter._client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    real_complete = adapter.complete

    def record_result(messages, *args, **kwargs):
        result = real_complete(messages, *args, **kwargs)
        state.results.append(result)
        return result

    monkeypatch.setattr(adapter, "complete", record_result)
    api = AnalystAPI(config)
    pipe = api.pipeline
    pipe.registry.adapters = {adapter.model_config_id: adapter}
    pipe.search = _Sources()
    pipe.reader.search_provider = pipe.search
    monkeypatch.setattr(pipe.planner, "plan", lambda *a, **kw: [
        PlannedQuery("离线出货量核查", "task_question", 100)])
    monkeypatch.setattr(pipe.triage, "triage", lambda hit, link_id, **kw: TriageResult(
        source_link_id=link_id, triage_decision="read", read_priority=90, need_model=True))

    def read(link_id, url, profile, context=None, **kwargs):
        state.reads += 1
        passages = [Passage(passage_id="p1", section="正文", text=FACT),
                    Passage(passage_id="p2", section="正文", text=DATE_TEXT)]
        return ReadResult(
            source_link_id=link_id, read_status="read", title="离线半年报告", http_status=200,
            document_type="pdf", content_type="application/pdf", content_hash="offline-recovery-body",
            simhash="0", content_length=sum(len(p.text) for p in passages), passages=passages,
            publication_time=deepcopy(UNKNOWN))

    monkeypatch.setattr(pipe.reader, "read", read)
    return api, state


def _collect(api, state):
    return api.collect_intelligence(TARGET, {
        "time_window": "30d", "focus": ["operating_update"],
        "questions": ["核查2026年上半年的出货量和单位"],
        "budget_profile": {"max_queries": 1, "max_search_hits": 1, "max_links_to_read": 1,
                           "max_model_calls": 2, "max_gateway_requests": 2},
    }, run_options={"clock": lambda: state.clock, "deadline_seconds": 5,
                    "cancel_check": lambda: state.cancelled})


def _assert_two_attempt_audit(api, state, report):
    assert state.reads == 1
    assert len(state.requests) == len(state.results) == 2
    assert report["summary"]["model_calls"] == report["summary"]["gateway_requests_sent"] == 2
    assert report["collection_diagnostics"]["budget_used"]["model_calls"] == 2
    assert report["collection_diagnostics"]["budget_used"]["gateway_requests_sent"] == 2
    assert report["summary"]["estimated_model_cost"] == pytest.approx(.012)
    assert state.budget.used["gateway_requests_sent"] == 2
    original, regenerated = state.results
    assert original.status == "json_invalid" and original.parsed is None
    assert original.raw_text == BROKEN
    assert original.estimated_cost == pytest.approx(.004)
    assert regenerated.status == "success" and regenerated.estimated_cost == pytest.approx(.008)
    first_request, second_request = state.requests
    assert first_request["max_tokens"] == second_request["max_tokens"] == 65536
    assert first_request["response_format"] == second_request["response_format"] == {"type": "json_object"}
    assert [x for x in first_request["messages"] if x["role"] != "system"] == [
        x for x in second_request["messages"] if x["role"] != "system"]
    assert "broken-prior-answer-without-evidence" not in json.dumps(second_request)

    database = Database(api.config.database_url)
    try:
        fresh = Repository(database)
        with database.session() as session:
            link, = session.query(m.SourceLink).filter_by(search_run_id=report["search_run_id"]).all()
            link_id = link.id
            runs = session.query(m.ModelRun).filter_by(source_link_id=link_id).all()
            assert len(runs) == 2
            by_response = {row.provider_request_id: row for row in runs}
            parent, child = by_response["offline-response-1"], by_response["offline-response-2"]
            assert parent.status == "json_invalid" and child.status == "success"
            assert parent.estimated_cost + child.estimated_cost == pytest.approx(.012)
            outputs = {row.model_run_id: row for row in session.query(m.ModelOutput).filter_by(
                source_link_id=link_id).all()}
            assert len(outputs) == 2
            assert outputs[parent.id].output_json is None
            assert outputs[parent.id].schema_valid is False
            assert outputs[child.id].output_json == state.raw
            parent_id, child_id = parent.id, child.id
        delivered = fresh.explain_source_analysis(link_id)
        requests = {entry["model_run_id"]: entry for entry in delivered["model_requests"]}
        assert set(requests) == {parent_id, child_id}
        recovery = requests[child_id]["json_recovery"]
        assert recovery["strategy"] == "reextract_original_input"
        assert recovery["attempt"] == 1 and recovery["parent_output_index"] == 0
        assert recovery["parent_model_run_id"] == parent_id
        assert recovery["parent_provider_request_id"] == "offline-response-1"
        assert recovery["parent_response_sha256"] == sha256(BROKEN.encode()).hexdigest()
        assert recovery["parent_trace_id"] == requests[parent_id]["acceptance_trace"]["trace_id"]
        report_requests = {entry["model_run_id"]: entry for entry in
                           report["collection_diagnostics"]["model_requests"]}
        assert report_requests[child_id]["json_recovery"] == recovery
        assert "json_recovery" not in requests[parent_id]

        directories = [state.logs / requests[key]["acceptance_trace"]["relative_path"]
                       for key in (parent_id, child_id)]
        assert [path.joinpath("response.txt").read_text() for path in directories] == state.responses
        metadata = [json.loads(path.joinpath("result.json").read_text()) for path in directories]
        assert [entry["status"] for entry in metadata] == ["json_invalid", "success"]
        assert metadata[0]["response"]["raw_sha256"] == recovery["parent_response_sha256"]
        assert metadata[1]["json_recovery"]["parent_trace_id"] == metadata[0]["trace_id"]
        assert metadata[1]["json_recovery"]["parent_response_sha256"] == recovery["parent_response_sha256"]
        assert directories[0].joinpath("input.json").read_bytes() == directories[1].joinpath("input.json").read_bytes()
        return link_id, delivered
    finally:
        database.engine.dispose()


def _assert_no_business_content(api, report):
    assert api.search_facts(TARGET) == []
    assert api.get_metric_observations(TARGET) == []
    assert api.get_recent_events(TARGET) == []
    assert report["structured_outputs"]["facts"] == report["structured_outputs"]["metrics"] == 0
    assert report["collection_diagnostics"]["usable"] is False
    with api.repo.db.session() as session:
        assert session.query(m.MergedAnalysis).count() == 0


def test_regeneration_retains_failure_and_delivers_reviewed_content_with_both_call_costs(recovery_delivery):
    api, state = recovery_delivery
    report = _collect(api, state)
    _, delivered = _assert_two_attempt_audit(api, state, report)
    assert report["collection_diagnostics"]["usable"] is True
    assert delivered["facts"] == [FACT]
    assert delivered["source"]["publication_time"]["status"] == "known"
    assert delivered["source"]["publish_time"].startswith("2026-09-15")
    assert [fact["fact_statement"] for fact in api.search_facts(TARGET)] == [FACT]
    metric, = api.get_metric_observations(TARGET)
    assert (metric["metric_name"], metric["metric_value"], metric["unit"], metric["period"]) == (
        "出货量", 120, "台", "2026H1")
    assert delivered["content_review"]["protocol"] == PROTOCOL


@pytest.mark.parametrize("damage", ["no_review", "no_publication_binding", "ambiguous_date_role"])
def test_parseable_regeneration_does_not_bypass_review_or_date_admission(recovery_delivery, damage):
    api, state = recovery_delivery
    if damage == "no_review":
        state.raw.pop("content_review")
    elif damage == "no_publication_binding":
        state.raw["content_review"]["bindings"].pop("/source_publication")
    else:
        assessment = state.raw["content_review"]["claims"][1]["field_values"]["source_publication"]["assessment"]
        assessment["possible_roles"] = ["source_publication", "reporting_period"]
    report = _collect(api, state)
    _, delivered = _assert_two_attempt_audit(api, state, report)
    _assert_no_business_content(api, report)
    assert delivered["facts"] == [] and delivered["metrics"] == []
    assert delivered["source"]["publication_time"]["status"] != "known"
    assert delivered["source"]["publish_time"] is None


@pytest.mark.parametrize("stop", ["cancelled", "timed_out"])
def test_stop_during_regeneration_preserves_both_call_audits_and_costs_without_business_writes(
        recovery_delivery, stop):
    api, state = recovery_delivery

    def stop_now():
        if stop == "cancelled":
            state.cancelled = True
        else:
            state.clock = 10.0

    state.after_recovery = stop_now
    report = _collect(api, state)
    _, delivered = _assert_two_attempt_audit(api, state, report)
    assert report["collection_diagnostics"]["execution_status"] == stop
    _assert_no_business_content(api, report)
    assert delivered["facts"] == [] and delivered["metrics"] == []
    assert report["source_publications"] == []
    assert report["task_coverage"] == [] and report["coverage_gaps"] == []
