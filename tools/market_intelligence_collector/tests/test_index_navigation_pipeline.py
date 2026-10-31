"""Offline index -> body contracts through the real reader/planner/review/store.

The pages, decisions and claims below are synthetic. These tests establish
execution and evidence isolation, never real-model understanding or live E2E.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from mic.api import AnalystAPI
from mic.browser.contracts import FetchResult
from mic.content_review_policy import PROTOCOL
from mic.modeling.adapter import ModelAdapter, ModelCallResult
from mic.planner import PlannedQuery
from mic.publication_time import extract_publication
from mic.schemas import SearchHit, TriageResult
from mic.search import SearchProvider
from tests.browser_doubles import FakeClock, browser_runtime_block


TARGET = "synthetic_observatory"
INDEX = "https://records.example.test/list"
CHILD = "https://documents.example.test/report"
SPARE = "https://documents.example.test/prior"
TAIL = "https://reports.example.test/background"
NEXT = "https://records.example.test/list?page=2"
QUESTION = "核对澄川研究公司的定期报告与营业收入，并保留披露日期和缺口。"


def _index(entries, *, next_url=None):
    rows = "".join(
        f'<li><a href="{url}">澄川研究公司材料 {n}</a>'
        '<time datetime="2026-08-18">2026-08-18</time></li>'
        for n, url in enumerate(entries, 1)
    )
    pagination = (f'<nav class="pagination"><a rel="next" href="{next_url}">下一页</a></nav>'
                  if next_url else "")
    return f"<html><head><title>澄川研究公司文件目录</title></head><body><main><ul>{rows}</ul>{pagination}</main></body></html>"


def _article(*, publication="metadata"):
    meta = ('<meta property="article:published_time" content="2026-08-18">'
            if publication == "metadata" else "")
    date = ("公开披露日期：2026年8月18日。" if publication == "typed"
            else "签署日期：2026年8月18日。" if publication == "ambiguous" else "")
    text = ("澄川研究公司披露，本期营业收入为42元。"
            "该数值仅对应本材料所列期间，其他报告期间的数值未在本选段列示。"
            "本文为人工构造的回归材料，只有已读正文能够支持具体指标。")
    return (f"<html><head><title>澄川研究公司报告正文</title>{meta}</head>"
            f"<body><article><h1>澄川研究公司报告正文</h1><p>{date}{text}</p>"
            "</article></body></html>")


class _Pages(SearchProvider):
    name = "offline-index-navigation"
    browser_backed = True

    def __init__(self, state):
        self.state = state

    def search(self, query, query_family=None, limit=10):
        return [SearchHit(
            query=query, query_family=query_family, title=f"澄川研究公司候选 {n}",
            snippet="本摘要只用于导航，不证明披露日期、官方性或财务数值。",
            url=url, domain=url.split("/")[2], rank=n + 1, provider=self.name,
            discovery={"relevance": {
                "target_match": True, "task_match": True,
                "site_ok": url not in self.state.blocked,
                "content_form_ok": url not in self.state.index_urls,
            }},
        ) for n, url in enumerate(self.state.search_urls)]


def _extraction(payload, state):
    is_index = payload["source_metadata"].get("material_scope", {}).get("kind") == "index"
    passage = next(p for p in payload["selected_passages"] if p["passage_id"] != "title")
    text, pid = passage["text"], passage["passage_id"]
    claim = {
        "id": "listing" if is_index else "revenue", "kind": "observation",
        "status": "source_supported", "statement": text,
        "reason": "人工测试响应，仅复述当前输入中的连续正文。",
        "evidence": [{"passage_id": pid, "quote": text}], "depends_on": [],
    }
    bundle = {
        "schema_version": "bundle_extraction_v0.3", "decision": "save_structured", "overall_score": 80, "confidence": .9,
        "facts": [{"fact_type": "document_listing" if is_index else "claim",
                   "fact_statement": text, "confidence": .9,
                   "evidence_locator": {"passage_id": pid}}],
        "content_review": {
            "protocol": PROTOCOL, "claims": [claim],
            "bindings": {"/facts/0": [claim["id"]]},
            "task_coverage": [{"question_id": "q1", "status": "partial",
                               "answer_claim_ids": [claim["id"]],
                               "uncovered_aspects": ["最新性与共同报表口径"],
                               "reason": "该替身仅证明当前选段内容。"}],
        },
    }
    if is_index:
        return bundle
    if state.bad_child_quote:
        # Parent listing is not part of this child's selected passages.
        claim["evidence"] = [{"passage_id": "index_entry_1",
                              "quote": "澄川研究公司材料 1 2026-08-18"}]
    bundle["metrics"] = [{
        "metric_name": "营业收入", "metric_value": 42, "unit": "CNY",
        "scope": {"segment": "合并口径"} if state.bad_child_scope else {},
        "confidence": .9, "evidence_locator": {"passage_id": pid},
    }]
    bundle["content_review"]["bindings"]["/metrics/0"] = ["revenue"]
    mode = state.publication.get(payload["source_metadata"]["url"], "metadata")
    if mode in {"typed", "ambiguous"}:
        date_passage = next(p for p in payload["selected_passages"] if "2026年8月18日" in p["text"])
        date_quote = date_passage["text"]
        roles = ["source_publication"] if mode == "typed" else ["signature", "source_publication"]
        date_claim = {
            "id": "publication", "kind": "observation", "status": "source_supported",
            "statement": date_quote, "reason": "人工日期角色判断。",
            "evidence": [{"passage_id": date_passage["passage_id"], "quote": date_quote}],
            "depends_on": [],
            "field_values": {"source_publication": {
                "published_at": "2026-08-18", "date_role": "source_publication",
                "assessment": {
                    "protocol": "date_review_v1", "possible_roles": roles,
                    "selected_role": "source_publication", "basis": "explicit_publication",
                    "evidence": [{"passage_id": date_passage["passage_id"], "quote": date_quote}],
                },
            }},
        }
        bundle["content_review"]["claims"].append(date_claim)
        bundle["content_review"]["bindings"]["/source_publication"] = ["publication"]
    return bundle


@pytest.fixture
def index_navigation(config, monkeypatch):
    config.raw["target_profiles"][TARGET] = {
        "target_id": TARGET, "type": "company", "canonical_name": "澄川研究公司",
        "aliases": ["澄川研究"], "official_domains": ["records.example.test"],
    }
    config.raw["search_providers"]["active"] = "mock"
    config.raw["call_governance"]["batching"].update(serp_batch_triage=True, serp_batch_size=20)
    config.raw["call_governance"]["budgets"]["max_batch_triage_calls"] = 5
    config.raw["output_schema"].setdefault("limits", {})["strict_evidence_review"] = True
    config.set_browser_runtime(browser_runtime_block(limits={
        "max_links_to_read": 4, "max_http_read_attempts": 8,
        "max_browser_read_attempts": 4, "max_model_calls": 8,
        "max_gateway_requests": 8, "max_run_seconds": 600,
    }))
    for policy in config.model_policies["tasks"].values():
        policy.update(call_mode="single_model", models=[{"model_id": "qwen_plus", "priority": 1}])
    state = SimpleNamespace(
        search_urls=[INDEX], index_urls={INDEX}, blocked=set(),
        entries={INDEX: [CHILD, SPARE]}, next_urls={}, publication={},
        decisions={SPARE: "link_record_only"}, initial_decisions={},
        priorities={INDEX: 95, CHILD: 90, SPARE: 10, TAIL: 30, NEXT: 85},
        requests=[], reads=[], fetches=[], read_options=[], failed=set(),
        final_urls={}, navigation_invalid=None, bad_child_quote=False, bad_child_scope=False,
        index_max_passages=None, index_extraction_invalid=False, index_publication=None,
        cancelled=False, stop_during_navigation=None, gateway=False,
        clock=FakeClock(),
    )
    api = AnalystAPI(config)
    pipe = api.pipeline
    pipe.search = _Pages(state)
    pipe.reader.search_provider = pipe.search
    pipe.reader.browser_fetch_cfg = {"default_mode": "http_only"}
    monkeypatch.setattr(pipe.planner, "plan", lambda *args, **kwargs: [
        PlannedQuery(QUESTION, "task_question", 100)])
    monkeypatch.setattr("mic.publication_time.utcnow",
                        lambda: datetime(2026, 8, 20, tzinfo=timezone.utc))

    def triage(hit, source_link_id, **kwargs):
        return TriageResult(
            source_link_id=source_link_id, triage_decision="read", need_model=True,
            read_priority=state.priorities.get(hit.url, 50),
            matched_signals=["target_entity_match"],
            reason="人工设定规则分值，正文无任何默认审核。",
        )

    def fetch(url):
        state.fetches.append(url)
        assert url in state.index_urls or url in {CHILD, SPARE, TAIL}, "unexpected synthetic destination"
        if url in state.failed:
            return FetchResult(transport="http", requested_url=url, final_url=url,
                               http_status=404, blocked_reason="http_status",
                               counted_as="http_read_attempts")
        html = (_index(state.entries[url], next_url=state.next_urls.get(url))
                if url in state.index_urls else _article(publication=state.publication.get(url, "metadata")))
        return FetchResult(
            transport="http", requested_url=url, final_url=state.final_urls.get(url, url),
            http_status=200, content_type="text/html", html=html,
            counted_as="http_read_attempts",
        )

    real_read = pipe.reader.read

    def read(source_link_id, url, profile, **kwargs):
        state.reads.append(url)
        state.read_options.append({"url": url, "source_link_id": source_link_id,
                                   "read_intent": kwargs.get("read_intent", "content")})
        original_limit = pipe.reader.max_passages
        if url in state.index_urls and state.index_max_passages is not None:
            pipe.reader.max_passages = state.index_max_passages
        try:
            result = real_read(source_link_id, url, profile, **kwargs)
            if url in state.index_urls and state.index_publication:
                # Exercise the generic Pipeline's known-index-date branch.
                # The current structural index reader itself returns unknown;
                # this represents a reader with independently bound page metadata.
                html = _article().replace("2026-08-18", state.index_publication)
                result.publication_time = extract_publication(BeautifulSoup(html, "lxml"), url)
                result.publish_time = result.publication_time.get("published_at")
            return result
        finally:
            pipe.reader.max_passages = original_limit

    def complete(adapter, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        is_navigation = bool(payload.get("navigation_context"))
        if state.gateway:
            error = adapter.reserve_gateway_request()
            if error:
                return ModelCallResult(
                    model_config_id=adapter.model_config_id, provider="offline",
                    provider_type="stub", model_name="synthetic-index-navigation",
                    status="error", error_type="budget_exhausted", error_message=error,
                    is_mock=True)
        state.requests.append(deepcopy(payload))
        status = "success"
        if payload["task"] == "serp_batch_triage":
            ordered = sorted(payload["items"], key=lambda item: -state.priorities.get(item["url"], 50))
            decisions = state.decisions if is_navigation else state.initial_decisions
            rows = [{
                "id": item["id"], "triage_decision": decisions.get(item["url"], "read"),
                "read_priority": state.priorities.get(item["url"], 50), "need_model": True,
                "task_read_rank": rank,
                "read_intent": "index" if item["url"] in state.index_urls else "content",
                "reason": "人工模型选择已列出的目标；没有生成新 URL 或批准业务内容。",
            } for rank, item in enumerate(ordered, 1)]
            parsed = {"results": rows}
            if is_navigation and state.navigation_invalid == "missing_id":
                parsed["results"] = rows[:-1]
            elif is_navigation and state.navigation_invalid == "invented_id":
                parsed["results"].append({**rows[0], "id": "invented_unseen_destination"})
            elif is_navigation and state.navigation_invalid == "request_failed":
                status, parsed = "json_invalid", None
            elif is_navigation and state.navigation_invalid == "unreviewed_child":
                for row in rows:
                    row["need_model"] = False
            if is_navigation and state.stop_during_navigation == "cancel":
                state.cancelled = True
            elif is_navigation and state.stop_during_navigation == "deadline":
                state.clock.advance(31)
        else:
            assert payload["task"] == "bundle_extraction"
            parsed = _extraction(payload, state)
            if state.index_extraction_invalid and payload["source_metadata"]["url"] in state.index_urls:
                status, parsed = "json_invalid", None
        return ModelCallResult(
            model_config_id=adapter.model_config_id, provider="offline",
            provider_type="stub", model_name="synthetic-index-navigation",
            status=status, parsed=parsed, raw_text=json.dumps(parsed, ensure_ascii=False),
            is_mock=True,
        )

    monkeypatch.setattr(pipe.triage, "triage", triage)
    monkeypatch.setattr(pipe.reader, "_fetch_http_result", fetch)
    monkeypatch.setattr(pipe.reader, "read", read)
    monkeypatch.setattr(ModelAdapter, "complete", complete)
    return api, state


def _collect(api, state, *, time_window="", **budget):
    return api.collect_intelligence(TARGET, {
        "time_window": time_window, "questions": [QUESTION],
        "budget_profile": {
            "max_queries": 1, "max_search_hits": 10, "max_links_to_read": 4,
            "max_http_read_attempts": 8, "max_browser_read_attempts": 0,
            "max_model_calls": 8, "max_gateway_requests": 8, "max_run_seconds": 600,
            **budget,
        },
    }, run_options={"clock": state.clock, "deadline_seconds": 30,
                    "cancel_check": lambda: state.cancelled})


def _links(api, report):
    return {row["url"]: row for row in api.repo.source_links_for_run(report["search_run_id"], limit=100)}


def _navigation_requests(state):
    return [p for p in state.requests if p.get("navigation_context")]


def test_unknown_date_index_can_navigate_without_publishing_listing_in_recent_window(index_navigation):
    api, state = index_navigation
    report = _collect(api, state, time_window="30d")
    assert state.reads == [INDEX, CHILD]
    assert [p["task"] for p in state.requests] == [
        "serp_batch_triage", "bundle_extraction", "serp_batch_triage", "bundle_extraction"]
    parent, child = _links(api, report)[INDEX], _links(api, report)[CHILD]
    assert parent["metadata"]["publication_time"]["status"] == "unknown"
    assert parent["read_status"] == "link_record_only"
    assert child["metadata"]["publication_time"]["status"] == "known"
    assert child["source_type"] != "official"
    assert child["metadata"]["task_triage"]["basis"] == "index_result_navigation_only"
    facts = api.search_facts(TARGET)
    assert facts and all(f["fact_type"] != "document_listing" for f in facts)
    assert [m["metric_value"] for m in api.get_metric_observations(TARGET)] == [42]
    assert report["summary"]["model_calls"] == 4
    assert report["summary"]["links_read"] == report["summary"]["http_read_attempts"] == 2
    assert report["summary"]["browser_read_attempts"] == 0
    nav, = _navigation_requests(state)
    assert {item["url"] for item in nav["items"]} == {CHILD, SPARE}
    assert all(item.get("navigation") for item in nav["items"])
    assert parent["source_link_id"] in json.dumps(nav["navigation_context"])
    child_navigation = next(item["navigation"] for item in nav["items"] if item["url"] == CHILD)
    child_extract = next(p for p in state.requests
                         if p["task"] == "bundle_extraction" and p["source_metadata"]["url"] == CHILD)
    assert child_extract["source_metadata"]["navigation"] == child_navigation
    assert child["metadata"]["task_triage"]["navigation"] == child_navigation
    attempt, = api.repo.read_attempts_for_link(child["source_link_id"])
    assert attempt["diagnostics"]["fetch"]["navigation"] == child_navigation
    explain = api.explain_source_analysis(child["source_link_id"])
    assert parent["source_link_id"] in json.dumps(explain)
    assert all(c["status"] != "covered" for c in report["task_coverage"])


@pytest.mark.parametrize(("date_mode", "published"), [
    ("unknown", False), ("ambiguous", False), ("typed", True),
])
def test_child_date_uses_its_own_shared_review_not_listing_date(index_navigation, date_mode, published):
    api, state = index_navigation
    state.publication[CHILD] = date_mode
    report = _collect(api, state, time_window="30d")
    assert state.reads == [INDEX, CHILD]
    child = _links(api, report)[CHILD]
    assert (child["metadata"]["publication_time"]["status"] == "known") is published
    assert bool(api.get_metric_observations(TARGET)) is published
    extract = next(p for p in state.requests
                   if p["task"] == "bundle_extraction" and p["source_metadata"]["url"] == CHILD)
    assert extract["source_metadata"]["publication_time"]["status"] == "unknown"
    assert all(p["section"] not in {"index_entry", "index_scope"} for p in extract["selected_passages"])


def test_child_numeric_claim_cannot_borrow_parent_listing_quote(index_navigation):
    api, state = index_navigation
    state.bad_child_quote = True
    report = _collect(api, state)
    assert state.reads == [INDEX, CHILD]
    assert api.get_metric_observations(TARGET) == []
    child = _links(api, report)[CHILD]
    explain = api.explain_source_analysis(child["source_link_id"])
    assert "citation_missing_or_unverified" in json.dumps(explain)


@pytest.mark.parametrize("invalid", ["missing_id", "invented_id", "request_failed", "unreviewed_child"])
def test_invalid_navigation_decision_never_promotes_children_or_retries(index_navigation, invalid):
    api, state = index_navigation
    state.navigation_invalid = invalid
    report = _collect(api, state)
    assert len(_navigation_requests(state)) == 1
    assert state.reads == [INDEX]
    assert report["summary"]["model_calls"] == 3
    assert all(row["read_status"] == "pending" for url, row in _links(api, report).items() if url != INDEX)
    assert api.get_metric_observations(TARGET) == []


@pytest.mark.parametrize("budget", [
    {"max_links_to_read": 1}, {"max_http_read_attempts": 1},
    {"max_model_calls": 3}, {"max_gateway_requests": 2},
])
def test_navigation_never_expands_caps_or_spends_last_body_call(index_navigation, budget):
    api, state = index_navigation
    state.gateway = True
    report = _collect(api, state, **budget)
    assert state.reads == [INDEX]
    assert _navigation_requests(state) == []
    assert report["summary"]["links_read"] == report["summary"]["http_read_attempts"] == 1
    assert report["summary"]["model_calls"] == report["summary"]["gateway_requests_sent"] == 2


def test_child_priority_competes_with_unexecuted_search_tail(index_navigation):
    api, state = index_navigation
    state.search_urls = [INDEX, TAIL]
    report = _collect(api, state, max_links_to_read=2)
    assert state.reads == [INDEX, CHILD]
    assert _links(api, report)[TAIL]["read_status"] == "pending"


def test_existing_unread_search_destination_reuses_source_id(index_navigation):
    api, state = index_navigation
    state.search_urls = [INDEX, CHILD]
    state.initial_decisions[CHILD] = "link_record_only"
    report = _collect(api, state)
    assert state.reads == [INDEX, CHILD]
    initial_id = next(item["id"] for item in state.requests[0]["items"] if item["url"] == CHILD)
    nav_id = next(item["id"] for item in _navigation_requests(state)[0]["items"] if item["url"] == CHILD)
    assert initial_id == nav_id == _links(api, report)[CHILD]["source_link_id"]
    assert len(api.repo.source_links_for_run(report["search_run_id"], limit=100)) == 3


def test_existing_search_read_gate_rejection_survives_index_promotion(index_navigation):
    api, state = index_navigation
    state.search_urls = [INDEX, CHILD]
    state.blocked.add(CHILD)
    report = _collect(api, state)
    assert _navigation_requests(state)
    assert state.reads == [INDEX]
    assert _links(api, report)[CHILD]["triage_decision"] == "link_record_only"
    assert report["summary"]["read_gate_demoted"]["site_rule"] == 1


def test_index_and_pagination_cycle_never_rereads_already_attempted_url(index_navigation):
    api, state = index_navigation
    state.index_urls.add(NEXT)
    state.next_urls[INDEX] = NEXT
    state.next_urls[NEXT] = INDEX
    state.entries[NEXT] = [INDEX, CHILD]
    state.decisions[CHILD] = "link_record_only"
    report = _collect(api, state)
    assert state.reads == [INDEX, NEXT]
    assert len(state.reads) == len(set(state.reads))
    assert report["summary"]["links_read"] == report["summary"]["http_read_attempts"] == 2
    assert all(INDEX not in {item["url"] for item in p["items"]}
               for p in _navigation_requests(state))


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_stop_during_navigation_response_prevents_child_fetch(index_navigation, stop):
    api, state = index_navigation
    state.stop_during_navigation = stop
    report = _collect(api, state)
    assert len(_navigation_requests(state)) == 1
    assert state.reads == [INDEX]
    assert report["collection_diagnostics"]["execution_status"] == (
        "cancelled" if stop == "cancel" else "timed_out")
    assert api.get_metric_observations(TARGET) == []
    assert report["summary"]["model_calls"] == 3
    assert report["summary"]["batch_triage_calls"] == 2
    assert len(report["task_triage_reviews"]) == 2
    assert len(report["collection_diagnostics"]["model_requests"]) == 3
    assert report["collection_diagnostics"]["budget_used"]["model_calls"] == 3


def test_truncated_index_only_exposes_selected_entries_to_navigation(index_navigation):
    api, state = index_navigation
    state.index_max_passages = 1
    state.decisions[SPARE] = "read"
    report = _collect(api, state)
    assert state.reads == [INDEX, CHILD]
    nav, = _navigation_requests(state)
    assert {item["url"] for item in nav["items"]} == {CHILD}
    assert SPARE not in _links(api, report)
    extract = state.requests[1]
    assert extract["source_metadata"]["material_scope"]["truncated"] is True
    assert extract["source_metadata"]["material_scope"]["completeness"] == "incomplete"


def test_index_business_extraction_failure_does_not_preapprove_or_block_navigation(index_navigation):
    api, state = index_navigation
    state.index_extraction_invalid = True
    report = _collect(api, state, time_window="30d")
    assert state.reads == [INDEX, CHILD]
    assert len(_navigation_requests(state)) == 1
    assert [m["metric_value"] for m in api.get_metric_observations(TARGET)] == [42]
    assert all(f["fact_type"] != "document_listing" for f in api.search_facts(TARGET))
    assert _links(api, report)[INDEX]["metadata"]["publication_time"]["status"] == "unknown"


def test_child_unknown_accounting_scope_is_withheld_by_shared_review(index_navigation):
    api, state = index_navigation
    state.bad_child_scope = True
    report = _collect(api, state)
    assert state.reads == [INDEX, CHILD]
    metric, = api.get_metric_observations(TARGET)
    assert metric["metric_value"] == 42
    assert metric["scope"].get("segment") is None
    assert all(c["status"] != "covered" for c in report["task_coverage"])


def test_previously_failed_search_destination_is_not_retried_from_index(index_navigation):
    api, state = index_navigation
    state.search_urls = [CHILD, INDEX]
    state.priorities[CHILD] = 99
    state.failed.add(CHILD)
    report = _collect(api, state)
    assert state.reads == [CHILD, INDEX]
    assert all(CHILD not in {item["url"] for item in p["items"]}
               for p in _navigation_requests(state))
    assert report["summary"]["http_read_attempts"] == 2
    assert report["summary"]["links_read"] == 1


def test_redirect_final_url_cannot_be_read_again_as_index_child(index_navigation):
    api, state = index_navigation
    state.final_urls[INDEX] = CHILD
    report = _collect(api, state)
    assert state.reads == [INDEX]
    assert all(CHILD not in {item["url"] for item in p["items"]}
               for p in _navigation_requests(state))
    assert report["summary"]["links_read"] == 1


@pytest.mark.parametrize("budget", [{"max_http_read_attempts": 2}, {"max_links_to_read": 2}])
def test_multiple_approved_children_still_share_parent_physical_caps(index_navigation, budget):
    api, state = index_navigation
    state.decisions[SPARE] = "read"
    report = _collect(api, state, **budget)
    assert state.reads == [INDEX, CHILD]
    assert _links(api, report)[SPARE]["read_status"] == "pending"
    assert report["summary"]["http_read_attempts"] == report["summary"]["links_read"] == 2
    assert report["summary"]["model_calls"] == 4


def test_old_index_publication_filters_index_business_but_not_child_navigation(index_navigation):
    api, state = index_navigation
    state.index_publication = "2025-12-01"
    report = _collect(api, state, time_window="90d")
    assert state.reads == [INDEX, CHILD]
    assert [p["task"] for p in state.requests] == [
        "serp_batch_triage", "serp_batch_triage", "bundle_extraction"]
    links = _links(api, report)
    assert links[INDEX]["read_status"] == "link_record_only"
    assert links[INDEX]["metadata"]["publication_time"]["time_window"]["status"] == "outside_time_window"
    assert links[CHILD]["metadata"]["publication_time"]["time_window"]["status"] == "in_window"
    assert [m["metric_value"] for m in api.get_metric_observations(TARGET)] == [42]
    assert all(f["fact_type"] != "document_listing" for f in api.search_facts(TARGET))


def test_exhausted_search_hit_cap_does_not_relabel_index_links_as_search_hits(index_navigation):
    api, state = index_navigation
    report = _collect(api, state, max_search_hits=1)
    assert state.reads == [INDEX, CHILD]
    assert report["summary"]["search_hits"] == 1
    assert report["summary"]["unique_source_links"] == 3
    assert report["summary"]["links_read"] == 2


def test_input_char_omission_is_not_reported_as_model_rejection(index_navigation):
    api, state = index_navigation
    # Reader was built with its ordinary 8k selection bound. The smaller
    # call-planner bound exercises truncation of complete navigation items
    # after parent/task/budget context is added, without changing source text.
    api.pipeline.config.raw["call_governance"]["budgets"]["max_input_chars_per_model_call"] = 2500
    report = _collect(api, state)
    audit, = report["index_navigation"]
    char_omissions = [item for item in audit["skipped"] if item["reason"] == "model_input_char_limit"]
    assert char_omissions
    omitted = {item["source_link_id"] for item in char_omissions}
    sent = {item["id"] for payload in _navigation_requests(state) for item in payload["items"]}
    assert omitted.isdisjoint(sent)
    assert omitted.isdisjoint(item["source_link_id"] for item in audit["skipped"]
                              if item["reason"] == "navigation_not_accepted")
    assert omitted.isdisjoint(item["source_link_id"] for item in state.read_options)


def test_queued_child_redirect_prevents_later_fetch_of_known_final_destination(index_navigation):
    api, state = index_navigation
    state.decisions[SPARE] = "read"
    state.final_urls[CHILD] = SPARE
    report = _collect(api, state)
    # Both destinations were initially approved. The first child then resolved
    # to the second URL, so that now-known duplicate must be skipped before fetch.
    assert state.reads == state.fetches == [INDEX, CHILD]
    assert report["summary"]["links_read"] == report["summary"]["http_read_attempts"] == 2
    assert report["summary"]["duplicate_body_reads"] == 0
    assert report["summary"]["model_calls"] == 4
