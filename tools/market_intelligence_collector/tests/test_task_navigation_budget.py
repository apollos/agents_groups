"""Synthetic navigation/physical-budget contracts; no real model or network."""
from copy import deepcopy
import json

import pytest

from mic.modeling.adapter import ModelAdapter, ModelCallResult
from mic.reader import ReadResult
from mic.schemas import Passage, SearchHit, TriageResult
from tests.browser_doubles import FakeClock, browser_runtime_block
from tests.test_task_triage_pipeline import TARGET, QUESTION, _bundle, harness


@pytest.fixture
def navigation(harness, monkeypatch):
    api, state = harness
    pipe = api.pipeline
    state.browser = True
    state.urls = [f"https://documents.example/{n}" for n in range(5)]
    state.rules = dict(zip(state.urls, [99, 98, 40, 30, 10]))
    state.priorities = dict(zip(state.urls, [95, 90, 85, 80, 75]))
    state.decisions, state.hashes, state.final_urls, state.read_ids = {}, {}, {}, {}
    state.failed, state.invalid_extract = set(), set()
    state.conflicting_rank = False
    state.clock = FakeClock()
    pipe.config.set_browser_runtime(browser_runtime_block(limits={
        "max_links_to_read": 4, "max_http_read_attempts": 8,
        "max_browser_read_attempts": 4, "max_model_calls": 8,
        "max_gateway_requests": 8, "max_run_seconds": 600,
    }))
    pipe.config.raw["call_governance"]["batching"]["serp_batch_size"] = 20

    def search(query, query_family=None, limit=10):
        return [SearchHit(query=query, title=f"材料 {n}", snippet="人工导航样本。",
                          url=url, domain="documents.example", rank=n + 1, provider="offline",
                          discovery={"relevance": {"target_match": True, "task_match": True,
                                                   "site_ok": True, "content_form_ok": True}})
                for n, url in enumerate(state.urls)]

    def triage(hit, source_link_id, **kwargs):
        return TriageResult(source_link_id=source_link_id, triage_decision="read",
                            read_priority=state.rules[hit.url], need_model=True,
                            matched_signals=["target_entity_match"])

    def read(source_link_id, url, profile, context=None, **kwargs):
        context.budget.reserve("http_read_attempts")
        state.reads.append(url)
        state.read_ids[url] = source_link_id
        if url in state.failed:
            return ReadResult(source_link_id=source_link_id, read_status="failed",
                              failure_reason="synthetic_read_failure", final_url=url)
        content_hash = state.hashes.get(url, url)
        text = f"人工材料 {content_hash} 披露部分信息。"
        return ReadResult(source_link_id=source_link_id, read_status="read", title="人工材料",
                          http_status=200, content_hash=content_hash, simhash="0",
                          content_length=len(text), document_type="html",
                          final_url=state.final_urls.get(url, url),
                          passages=[Passage(passage_id="p0", section="正文", text=text)],
                          publication_time={"status": "unknown", "published_at": None})

    def complete(adapter, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        state.requests.append(deepcopy(payload))
        if payload["task"] == "serp_batch_triage":
            ordered = sorted(payload["items"], key=lambda item: -state.priorities[item["url"]])
            rows = [{"id": item["id"], "triage_decision": state.decisions.get(item["url"], "read"),
                     "read_priority": state.priorities[item["url"]], "need_model": True,
                     "task_read_rank": rank, "reason": "人工模型判断互补阅读价值。"}
                    for rank, item in enumerate(ordered, 1)]
            if state.conflicting_rank:
                rows[0]["task_read_rank"], rows[1]["task_read_rank"] = 2, 1
            parsed, status = {"results": rows}, "success"
        else:
            assert payload["task"] == "bundle_extraction"
            status = "json_invalid" if payload["source_metadata"]["url"] in state.invalid_extract else "success"
            parsed = _bundle(payload, 80) if status == "success" else None
        return ModelCallResult(model_config_id=adapter.model_config_id, provider="offline",
                               provider_type="stub", model_name="synthetic-navigation",
                               status=status, parsed=parsed, raw_text=json.dumps(parsed), is_mock=True)

    monkeypatch.setattr(pipe.search, "search", search)
    monkeypatch.setattr(pipe.triage, "triage", triage)
    monkeypatch.setattr(pipe.reader, "read", read)
    monkeypatch.setattr(ModelAdapter, "complete", complete)
    return api, state


def collect(api, state, **budget):
    return api.collect_intelligence(TARGET, {
        "time_window": "", "questions": [QUESTION],
        "budget_profile": {"max_queries": 1, "max_search_hits": 10,
                           "max_links_to_read": 99, "max_http_read_attempts": 99,
                           "max_browser_read_attempts": 0,
                           "max_model_calls": 99, "max_gateway_requests": 99,
                           "max_run_seconds": 900, **budget},
    }, run_options={"clock": state.clock, "deadline_seconds": 17})


def test_cross_batch_model_priority_and_actual_remaining_budget_reach_pipeline(navigation):
    api, state = navigation
    state.urls = state.urls[:4]
    state.priorities = dict(zip(state.urls, [70, 60, 95, 90]))
    api.pipeline.config.raw["call_governance"]["batching"]["serp_batch_size"] = 2
    report = collect(api, state, max_links_to_read=2)
    assert state.reads == state.urls[2:]
    initial = [r for r in state.requests if r["task"] == "serp_batch_triage"]
    assert len(initial) == 2 and all(len(r["items"]) == 2 for r in initial)
    assert initial[0]["task_context"] == initial[1]["task_context"]
    for n, request in enumerate(initial):
        budget = request["reading_budget"]
        assert budget["scope"] == "entire_task_run"
        assert budget["limits"] == {"max_links_to_read": 2, "max_http_read_attempts": 8,
                                    "max_browser_read_attempts": 0, "max_model_calls": 8,
                                    "max_gateway_requests": 8, "max_run_seconds": 600}
        assert budget["remaining"]["successful_reads"] == 2
        assert budget["remaining"]["model_calls"] == 8 - n
        assert budget["remaining"]["run_seconds"] == 17
        assert budget["batch_size_limit"] == 2
        assert report["task_triage_reviews"][n]["reading_budget"] == budget
    links = api.repo.source_links_for_run(report["search_run_id"])
    for row in links:
        assert row["triage_score"] == state.rules[row["url"]]
        assert row["metadata"]["task_triage"]["task_read_priority"] == state.priorities[row["url"]]
    assert report["summary"]["model_calls"] == 4  # two existing triage + two extraction


@pytest.mark.parametrize("redirect", [False, True])
def test_duplicate_success_still_consumes_four_read_cap_and_keeps_physical_diagnostics(navigation, redirect):
    api, state = navigation
    a, b, mirror, c, untouched = state.urls
    state.hashes[mirror] = a
    if redirect:
        state.final_urls[mirror] = a
    report = collect(api, state)
    assert state.reads == [a, b, mirror, c] and untouched not in state.reads
    summary = report["summary"]
    assert summary["links_read"] == summary["http_read_attempts"] == 4
    assert summary["unique_materials_read"] == 3 and summary["duplicate_body_reads"] == 1
    assert summary["model_calls"] == 4  # triage + three unique extracts; no mirror extract
    attempts = api.repo.read_attempts_for_link(state.read_ids[mirror])
    assert len(attempts) == 1
    assert attempts[0]["diagnostics"]["fetch"]["physical_read"] == {
        "successful": True, "counts_toward_read_limit": True, "duplicate_body": True}
    if redirect:
        # Stricter than the old redirect exception: success counts before the
        # duplicate-final-URL rejection, and cannot permit a fifth physical read.
        assert attempts[0]["failure_reason"] == "duplicate_final_url"
    assert all(cov["status"] == "partial" for cov in report["task_coverage"])
    assert len(report["task_coverage"]) == 3


def test_http_cap_stops_even_when_successful_read_slots_remain(navigation):
    api, state = navigation
    state.failed.add(state.urls[0])
    report = collect(api, state, max_http_read_attempts=3)
    assert state.reads == state.urls[:3]
    assert report["summary"]["http_read_attempts"] == 3
    assert report["summary"]["links_read"] == report["summary"]["unique_materials_read"] == 2
    assert report["summary"]["read_queue_stopped_by_budget"] == 2


def test_model_demotion_of_mirror_preserves_slots_without_semantic_keyword_code(navigation):
    api, state = navigation
    state.decisions[state.urls[2]] = "link_record_only"
    report = collect(api, state)
    assert state.reads == [state.urls[n] for n in (0, 1, 3, 4)]
    assert report["summary"]["links_read"] == report["summary"]["unique_materials_read"] == 4
    assert report["summary"]["duplicate_body_reads"] == 0


def test_conflicting_model_order_falls_back_without_retry_or_extra_stage(navigation):
    api, state = navigation
    state.conflicting_rank = True
    report = collect(api, state, max_links_to_read=1)
    assert state.reads == [state.urls[0]]
    assert report["summary"]["model_calls"] == 2
    assert report["task_triage_reviews"][0]["errors"] == ["task_priority_rank_conflict"]


def test_invalid_extraction_and_duplicate_do_not_create_covered_or_refund_slots(navigation):
    api, state = navigation
    state.hashes[state.urls[2]] = state.urls[0]
    state.invalid_extract.add(state.urls[0])
    report = collect(api, state)
    assert state.reads == state.urls[:4]
    assert report["summary"]["links_read"] == 4
    assert report["summary"]["unique_materials_read"] == 3
    assert report["summary"]["duplicate_body_reads"] == 1
    assert sorted(cov["status"] for cov in report["task_coverage"]) == ["not_assessed", "partial", "partial"]
