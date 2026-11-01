"""Offline same-run publication corroboration through the real reader/review/store.

The target text reuses a passage saved from a real acceptance read (an investor
relations record whose only date is its activity date). Every other page, every
triage decision and every model claim below is synthetic: a positive result here
proves that the protocol executes and is enforced, never that a real model would
make the same judgment or that any real document was published on these dates.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from mic.api import AnalystAPI
from mic.browser.contracts import FetchResult
from mic.content_review import enforce_source_publication_dates
from mic.content_review_policy import LINKED_PUBLICATION_PROTOCOL, PROTOCOL
from mic.modeling.adapter import ModelAdapter, ModelCallResult
from mic.planner import PlannedQuery
from mic.schemas import SearchHit, TriageResult
from mic.search import SearchProvider
from mic.task_triage import PUBLICATION_BASIS, validate_task_triage
from tests.browser_doubles import FakeClock, browser_runtime_block


TARGET = "synthetic_ir_issuer"
DOC = "https://files.example.test/ir/2026-003.html"
COPY = "https://copy.example.test/notice/2026-003"
DIRECTORY = "https://disclosure.example.test/ir/list"
OTHER = "https://files.example.test/ir/2026-002.html"
SIMILAR = "https://similar.example.test/ir/2026-002"
MIRROR = "https://mirror.example.test/repost/2026-003"
QUESTION = "核对兆易创新投资者关系活动记录表的经营信息，并保留原始披露日期与缺口。"
TITLE = "兆易创新科技集团股份有限公司投资者关系活动记录表"

# Saved read (link_f312cd143e5d, pdf_start), first lines only. It names an
# activity date ("调研日期") and no publication date.
SAVED_PDF_START = [
    "证券代码：603986 证券简称：兆易创新",
    "兆易创新科技集团股份有限公司投资者关系活动记录表",
    "编号：2026-003",
    "调研日期：2026年8月18日 调研时间：20:00-21:00",
    "调研地点：线上",
]
SAVED_BODY = "2026年上半年，公司MCU产品营收同比增长49.07%，营收的增长主要来自工业与汽车领域。"
SERP_DATE_SNIPPET = "兆易创新投资者关系活动记录表（2026-003） 2026-08-24"


def _paragraphs(lines):
    return "".join(f"<p>{line}</p>" for line in lines)


def _doc_html():
    return (f"<html><head><title>{TITLE}</title></head><body><div class=\"head\"><h1>{TITLE}</h1></div>"
            f"<article>{_paragraphs([*SAVED_PDF_START, SAVED_BODY])}</article></body></html>")


def _copy_html(extra=""):
    return (f"<html><head><title>{TITLE}（转载）</title></head><body><article><h1>{TITLE}（转载）</h1>"
            f"{_paragraphs([*SAVED_PDF_START, *( [extra] if extra else [])])}</article></body></html>")


def _mirror_html():
    # Same body as DOC, its own repost header outside the body container.
    return (f"<html><head><title>{TITLE}</title></head><body>"
            f"<div class=\"head\"><h1>{TITLE}</h1><span>转载时间：2026年8月25日 来源：镜像站</span></div>"
            f"<article>{_paragraphs([*SAVED_PDF_START, SAVED_BODY])}</article></body></html>")


def _directory_html(entries):
    rows = "".join(f'<li><a href="{url}">{text}</a><time datetime="{date}">{date}</time></li>'
                   for url, text, date in entries)
    return (f"<html><head><title>兆易创新公告目录</title></head><body><main><ul>{rows}</ul>"
            "</main></body></html>")


DIRECTORY_ENTRIES = [
    (DOC, "兆易创新：投资者关系活动记录表（编号：2026-003）", "2026-08-24"),
    (OTHER, "兆易创新：投资者关系活动记录表（编号：2026-002）", "2026-05-12"),
]
SIMILAR_ENTRIES = [
    (SIMILAR + "/a", "兆易创新科技集团股份有限公司投资者关系活动记录表", "2026-08-21"),
    (SIMILAR + "/b", "兆易创新科技集团股份有限公司业绩说明会公告", "2026-08-20"),
]


class _Pages(SearchProvider):
    name = "offline-publication-corroboration"
    browser_backed = True

    def __init__(self, state):
        self.state = state

    def search(self, query, query_family=None, limit=10):
        return [SearchHit(
            query=query, query_family=query_family, title=f"{TITLE} 候选 {n}",
            # SERP text may carry a date; it is navigation context only.
            snippet=SERP_DATE_SNIPPET, url=url, domain=url.split("/")[2], rank=n + 1,
            provider=self.name,
            discovery={"relevance": {"target_match": True, "task_match": True,
                                     "site_ok": True, "content_form_ok": url not in self.state.index_urls}},
        ) for n, url in enumerate(self.state.search_urls)]


def _passage(payload, needle):
    return next(p for p in payload["selected_passages"] if needle in p["text"])


def _quote(passage, text=None):
    return {"passage_id": passage["passage_id"], "quote": text or passage["text"]}


def _target_quote(target, needle):
    passage = next(p for p in target["identity_passages"] if needle in p["text"])
    return {"passage_id": passage["passage_id"], "quote": needle}


def _linked_claim(cid, target, *, published_at, current, bases, subject="target_original_disclosure",
                  relation="lists_target_document", date_quote=None):
    date_quote = date_quote or current
    return {
        "id": cid, "kind": "observation", "status": "source_supported",
        "statement": current["quote"], "reason": "人工构造的跨材料判断，仅用于协议执行测试。",
        "evidence": [current], "depends_on": [],
        "field_values": {"linked_publication": {
            "protocol": LINKED_PUBLICATION_PROTOCOL,
            "target_source_link_id": target["source_link_id"],
            "published_at": published_at, "date_role": "source_publication", "date_subject": subject,
            "assessment": {"protocol": "date_review_v1", "possible_roles": ["source_publication"],
                           "selected_role": "source_publication", "basis": "explicit_publication",
                           "evidence": [date_quote]},
            "target_identity": {"status": "source_supported",
                                "evidence": [_target_quote(target, "编号：2026-003")]},
            "correspondence": {"status": "source_supported", "relation": relation, "bases": bases},
        }},
    }


def _supplier_claims(payload, state):
    """Synthetic model judgments for a page read to corroborate a pending material."""
    url = payload["source_metadata"]["url"]
    targets = payload["source_metadata"].get("pending_publication_targets") or []
    mode = state.linked.get(url)
    if not targets or not mode:
        return []
    target = targets[0]
    tid = target["source_link_id"]
    if mode in {"file_link", "conflict_directory"}:
        entry = next(e for e in payload["source_metadata"]["material_scope"]["entries"]
                     if any(link["url"] == DOC for link in e["links"]))
        current = {"passage_id": entry["entry_id"], "quote": entry["text"]}
        bases = [
            {"kind": "file_link", "current_evidence": [current],
             "current_link": {"passage_id": entry["entry_id"], "url": DOC}},
            {"kind": "document_number", "current_evidence": [current],
             "target_evidence": [_target_quote(target, "编号：2026-003")]},
        ]
        return [_linked_claim("listed", target, published_at="2026-08-24", current=current, bases=bases)]
    if mode == "title_only":
        entry = payload["source_metadata"]["material_scope"]["entries"][0]
        current = {"passage_id": entry["entry_id"], "quote": entry["text"]}
        return [_linked_claim("similar", target, published_at="2026-08-21", current=current, bases=[
            {"kind": "document_title", "current_evidence": [current],
             "target_evidence": [_target_quote(target, "编号：2026-003")]}])]
    if mode == "mirror_repost":
        header = next(p for p in payload["selected_passages"] if p["section"] == "文章头部")
        current = _quote(header)
        return [_linked_claim("repost", target, published_at="2026-08-25", current=current,
                              subject="current_page_publication", relation="same_document_copy", bases=[
            {"kind": "document_title", "current_evidence": [current],
             "target_evidence": [_target_quote(target, "编号：2026-003")]}])]
    if mode == "serp_date":
        # A model echoing the search snippet date: the quote is not on the page.
        number = _passage(payload, "编号：2026-003")
        current = {"passage_id": number["passage_id"], "quote": SERP_DATE_SNIPPET}
        return [_linked_claim("serp", target, published_at="2026-08-24", current=current,
                              relation="same_document_copy", bases=[
            {"kind": "document_number", "current_evidence": [current],
             "target_evidence": [_target_quote(target, "编号：2026-003")]}])]
    if mode == "activity_as_publication":
        activity = _passage(payload, "调研日期")
        current = _quote(activity)
        claim = _linked_claim("activity", target, published_at="2026-08-18", current=current,
                              relation="same_document_copy", bases=[
            {"kind": "document_number", "current_evidence": [current],
             "target_evidence": [_target_quote(target, "编号：2026-003")]}])
        assessment = claim["field_values"]["linked_publication"]["assessment"]
        assessment.update(possible_roles=["event_occurrence"], selected_role="event_occurrence",
                          basis="explicit_occurrence")
        return [claim]
    if mode == "typed_original_date":
        number, typed = _passage(payload, "编号：2026-003"), _passage(payload, "原公告披露日期")
        return [_linked_claim("typed", target, published_at="2026-08-20", current=_quote(typed),
                              relation="same_document_copy", bases=[
            {"kind": "document_number", "current_evidence": [_quote(number)],
             "target_evidence": [_target_quote(target, "编号：2026-003")]}],
                              date_quote=_quote(typed))
                | {"evidence": [_quote(typed), _quote(number)]}]
    raise AssertionError(mode)


def _extraction(payload, state):
    url = payload["source_metadata"]["url"]
    claims, bindings, facts = [], {}, []
    if url == DOC:
        body = _passage(payload, "49.07%")
        claims.append({"id": "mcu", "kind": "observation", "status": "source_supported",
                       "statement": body["text"], "reason": "人工测试响应，复述已读正文。",
                       "evidence": [_quote(body)], "depends_on": []})
        facts.append({"fact_type": "claim", "fact_statement": body["text"], "confidence": .9,
                      "evidence_locator": {"passage_id": body["passage_id"]}})
        bindings["/facts/0"] = ["mcu"]
    for claim in _supplier_claims(payload, state):
        claims.append(claim)
        bindings[f"/linked_publications/{claim['field_values']['linked_publication']['target_source_link_id']}"] = [claim["id"]]
    return {
        "schema_version": "bundle_extraction_v0.3", "decision": "save_structured",
        "overall_score": 80, "confidence": .9, "facts": facts,
        "content_review": {"protocol": PROTOCOL, "claims": claims, "bindings": bindings,
                           "task_coverage": [{"question_id": "q1", "status": "partial",
                                              "answer_claim_ids": [c["id"] for c in claims[:1]],
                                              "uncovered_aspects": ["完整文档"],
                                              "reason": "人工替身仅证明当前选段。"}]},
    }


@pytest.fixture
def corroboration(config, monkeypatch):
    config.raw["target_profiles"][TARGET] = {
        "target_id": TARGET, "type": "company", "canonical_name": "兆易创新",
        "aliases": ["兆易创新科技集团"], "official_domains": ["gigadevice.example.test"],
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
        search_urls=[DOC, COPY], index_urls=set(), pages={}, initial={DOC: "read"},
        corroborate={}, linked={}, requests=[], reads=[], clock=FakeClock(),
    )
    api = AnalystAPI(config)
    pipe = api.pipeline
    pipe.search = _Pages(state)
    pipe.reader.search_provider = pipe.search
    pipe.reader.browser_fetch_cfg = {"default_mode": "http_only"}
    monkeypatch.setattr(pipe.planner, "plan", lambda *args, **kwargs: [
        PlannedQuery(QUESTION, "task_question", 100)])
    monkeypatch.setattr("mic.publication_time.utcnow",
                        lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(pipe.triage, "triage", lambda hit, source_link_id, **kw: TriageResult(
        source_link_id=source_link_id, triage_decision="read", need_model=True, read_priority=50,
        matched_signals=["target_entity_match"], reason="人工规则分值，不审核正文。"))

    def fetch(url):
        html = state.pages.get(url) or (_doc_html() if url == DOC else _copy_html())
        return FetchResult(transport="http", requested_url=url, final_url=url, http_status=200,
                           content_type="text/html", html=html, counted_as="http_read_attempts")

    real_read = pipe.reader.read

    def read(source_link_id, url, profile, **kwargs):
        state.reads.append(url)
        return real_read(source_link_id, url, profile, **kwargs)

    def complete(adapter, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        error = adapter.reserve_gateway_request()
        if error:
            return ModelCallResult(model_config_id=adapter.model_config_id, provider="offline",
                                   provider_type="stub", model_name="synthetic-corroboration",
                                   status="error", error_type="budget_exhausted", error_message=error,
                                   is_mock=True)
        state.requests.append(deepcopy(payload))
        if payload["task"] == "serp_batch_triage":
            corroboration = payload.get("corroboration_context")
            targets = [t["source_link_id"] for t in (corroboration or {}).get("pending_targets", [])]
            rows = []
            for rank, item in enumerate(payload["items"], 1):
                if corroboration:
                    decision = "read" if item["url"] in state.corroborate else "link_record_only"
                elif payload.get("navigation_context"):
                    decision = "link_record_only"
                else:
                    decision = state.initial.get(item["url"], "link_record_only")
                row = {"id": item["id"], "triage_decision": decision, "read_priority": 90 - rank,
                       "need_model": True, "task_read_rank": rank,
                       "read_intent": "index" if item["url"] in state.index_urls else "content",
                       "reason": "人工模型导航选择；不核实日期或内容。"}
                if corroboration:
                    row["publication_targets"] = targets if decision == "read" else []
                rows.append(row)
            parsed = {"results": rows}
        else:
            assert payload["task"] == "bundle_extraction"
            parsed = _extraction(payload, state)
        return ModelCallResult(model_config_id=adapter.model_config_id, provider="offline",
                               provider_type="stub", model_name="synthetic-corroboration",
                               status="success", parsed=parsed,
                               raw_text=json.dumps(parsed, ensure_ascii=False), is_mock=True)

    monkeypatch.setattr(pipe.reader, "_fetch_http_result", fetch)
    monkeypatch.setattr(pipe.reader, "read", read)
    monkeypatch.setattr(ModelAdapter, "complete", complete)
    return api, state


def _collect(api, state, *, time_window="90d", **budget):
    return api.collect_intelligence(TARGET, {
        "time_window": time_window, "questions": [QUESTION],
        "budget_profile": {
            "max_queries": 4, "max_search_hits": 10, "max_links_to_read": 4,
            "max_http_read_attempts": 8, "max_browser_read_attempts": 4,
            "max_model_calls": 8, "max_gateway_requests": 8, "max_run_seconds": 600,
            **budget,
        },
    }, run_options={"clock": state.clock, "deadline_seconds": 600})


def _links(api, report):
    return {row["url"]: row for row in api.repo.source_links_for_run(report["search_run_id"], limit=100)}


def _gaps(api, link_id):
    return [g for g in api.get_coverage_gaps(TARGET) if g.get("source_link_id") == link_id]


def _use_directory(state, entries=DIRECTORY_ENTRIES, mode="file_link"):
    state.search_urls = [DOC, DIRECTORY]
    state.index_urls.add(DIRECTORY)
    state.pages[DIRECTORY] = _directory_html(entries)
    state.corroborate[DIRECTORY] = True
    state.linked[DIRECTORY] = mode


def _assert_budget(report):
    summary = report["summary"]
    assert summary["model_calls"] <= 8 and summary["links_read"] <= 4
    assert summary["http_read_attempts"] <= 8 and summary["browser_read_attempts"] == 0
    used = report["collection_diagnostics"]["budget_used"]
    assert used["model_calls"] == summary["model_calls"] == len(report["collection_diagnostics"]["model_requests"])


def _audit(report, link_id):
    audit, = [a for a in report["publication_corroboration"] if a["target_source_link_id"] == link_id]
    return audit


def _corroboration_triage_calls(state):
    return [p for p in state.requests if p.get("corroboration_context")]


# --- scenario 1: activity-only original, SERP-only copy ---------------------

@pytest.mark.parametrize("copy_mode", [None, "serp_date", "activity_as_publication"])
def test_activity_date_and_serp_date_never_verify_publication(corroboration, copy_mode):
    api, state = corroboration
    state.corroborate[COPY] = True
    if copy_mode:
        state.linked[COPY] = copy_mode
    report = _collect(api, state)
    assert state.reads == [DOC, COPY]
    links = _links(api, report)
    doc = links[DOC]
    publication = doc["metadata"]["publication_time"]
    assert publication["status"] == "unknown" and publication["published_at"] is None
    assert publication["source"] == "linked_publication_review"
    assert publication["time_window"]["allowed"] is False
    assert api.search_facts(TARGET) == []
    corroboration_audit = _audit(report, doc["source_link_id"])
    assert corroboration_audit["admitted_to_time_window"] is False
    assert corroboration_audit["triage"]["requested_source_link_ids"] == [links[COPY]["source_link_id"]]
    rows = publication["linked_evidence"]
    if copy_mode is None:
        assert rows == [] and publication["reason"] == "linked_publication_not_found"
    else:
        row, = rows
        assert row["status"] == "rejected"
        assert row["reason"] == ("citation_missing_or_unverified" if copy_mode == "serp_date"
                                 else "date_role_ambiguous_or_incompatible")
    assert [g["gap_type"] for g in _gaps(api, doc["source_link_id"])
            if g["gap_type"].startswith("publication_")] == ["publication_date_unverified"]
    # The target was extracted once; the copy only received its own passages.
    extracts = [p for p in state.requests if p["task"] == "bundle_extraction"]
    assert [p["source_metadata"]["url"] for p in extracts] == [DOC, COPY]
    assert "pending_publication_targets" not in extracts[0]["source_metadata"]
    assert extracts[1]["source_metadata"]["pending_publication_targets"][0]["source_link_id"] == doc["source_link_id"]
    # The copy, itself undated, is deferred too but starts no second triage round.
    assert _audit(report, links[COPY]["source_link_id"])["triage"]["status"] == "supplier_not_retriaged"
    assert len(_corroboration_triage_calls(state)) == 1
    _assert_budget(report)


# --- scenario 2: read directory lists target, date and link -----------------

@pytest.mark.parametrize(("window", "admitted"), [("90d", True), ("30d", False)])
def test_directory_entry_binds_date_and_rechecks_window(corroboration, window, admitted):
    """Synthetic positive: proves protocol execution only, not a real disclosure date."""
    api, state = corroboration
    _use_directory(state)
    report = _collect(api, state, time_window=window)
    assert state.reads == [DOC, DIRECTORY]
    links = _links(api, report)
    doc, directory = links[DOC], links[DIRECTORY]
    publication = doc["metadata"]["publication_time"]
    assert publication["status"] == "known" and publication["published_at"].startswith("2026-08-24")
    assert publication["precision"] == "day" and publication["source"] == "linked_publication_review"
    row, = publication["linked_evidence"]
    assert row["status"] == "supported"
    assert row["supplier_source_link_id"] == directory["source_link_id"]
    assert row["supplier_material_kind"] == "index"
    assert row["supplier_model_run_ids"]
    value = row["linked_publication"]
    assert value["date_subject"] == "target_original_disclosure"
    assert {b["kind"] for b in value["correspondence"]["bases"]} == {"file_link", "document_number"}
    assert publication["time_window"]["allowed"] is admitted
    # Directory's own business content and its listing date are never the target's facts.
    statements = [f["fact_statement"] for f in api.search_facts(TARGET)]
    assert bool(statements) is admitted
    assert all("49.07%" in s for s in statements)
    audit = _audit(report, doc["source_link_id"])
    assert audit["admitted_to_time_window"] is admitted
    assert audit["final_publication"]["published_at"].startswith("2026-08-24")
    gaps = [g["gap_type"] for g in _gaps(api, doc["source_link_id"]) if g["gap_type"].startswith("publication_")]
    assert gaps == ([] if admitted else ["publication_outside_time_window"])
    # The deferred target is not re-extracted after corroboration.
    assert [p["source_metadata"]["url"] for p in state.requests if p["task"] == "bundle_extraction"] == [DOC, DIRECTORY]
    assert len(_corroboration_triage_calls(state)) == 1
    _assert_budget(report)


# --- scenario 3: insufficient correspondence, mirror repost date ------------

def test_similar_title_without_correspondence_does_not_bind(corroboration):
    api, state = corroboration
    _use_directory(state, entries=SIMILAR_ENTRIES, mode="title_only")
    report = _collect(api, state)
    doc = _links(api, report)[DOC]
    publication = doc["metadata"]["publication_time"]
    assert publication["status"] == "unknown"
    row, = publication["linked_evidence"]
    assert (row["status"], row["reason"]) == ("rejected", "correspondence_title_only")
    assert api.search_facts(TARGET) == []
    assert any(g["gap_type"] == "publication_date_unverified" for g in _gaps(api, doc["source_link_id"]))


def test_mirror_repost_with_duplicate_body_sends_only_header_and_cannot_date_original(corroboration):
    api, state = corroboration
    state.search_urls = [DOC, MIRROR]
    state.pages[MIRROR] = _mirror_html()
    state.corroborate[MIRROR] = True
    state.linked[MIRROR] = "mirror_repost"
    report = _collect(api, state)
    assert state.reads == [DOC, MIRROR]
    mirror_extract = next(p for p in state.requests if p["task"] == "bundle_extraction"
                          and p["source_metadata"]["url"] == MIRROR)
    assert {p["section"] for p in mirror_extract["selected_passages"]} <= {"title", "文章头部"}
    links = _links(api, report)
    publication = links[DOC]["metadata"]["publication_time"]
    row, = publication["linked_evidence"]
    assert row["supplier_passages"] == "non_body_only"
    assert (row["status"], row["reason"]) == ("rejected", "current_page_date_not_target_disclosure")
    assert publication["status"] == "unknown"
    assert api.search_facts(TARGET) == []


# --- scenario 4: conflict, outside window (above), budget exhaustion --------

def test_conflicting_linked_dates_keep_conflict_gap(corroboration):
    api, state = corroboration
    _use_directory(state)
    state.search_urls = [DOC, DIRECTORY, COPY]
    state.pages[COPY] = _copy_html("原公告披露日期：2026年8月20日")
    state.corroborate[COPY] = True
    state.linked[COPY] = "typed_original_date"
    report = _collect(api, state)
    assert set(state.reads) == {DOC, DIRECTORY, COPY}
    doc = _links(api, report)[DOC]
    publication = doc["metadata"]["publication_time"]
    assert [r["status"] for r in publication["linked_evidence"]] == ["supported", "supported"]
    assert (publication["status"], publication["reason"]) == ("conflict", "conflicting_publication_dates")
    assert publication["published_at"] is None
    assert api.search_facts(TARGET) == []
    assert [g["gap_type"] for g in _gaps(api, doc["source_link_id"])
            if g["gap_type"].startswith("publication_")] == ["publication_date_conflict"]
    _assert_budget(report)


@pytest.mark.parametrize("budget", [
    {"max_links_to_read": 1}, {"max_model_calls": 3}, {"max_gateway_requests": 3},
])
def test_budget_exhaustion_keeps_explicit_gap_without_fallback_date(corroboration, budget):
    api, state = corroboration
    _use_directory(state)
    report = _collect(api, state, **budget)
    assert state.reads == [DOC]
    doc = _links(api, report)[DOC]
    publication = doc["metadata"]["publication_time"]
    assert publication["status"] == "unknown" and publication["published_at"] is None
    assert publication["unavailable_reason"] == "corroboration_triage_insufficient_budget"
    assert _corroboration_triage_calls(state) == []
    assert [g["gap_type"] for g in _gaps(api, doc["source_link_id"])
            if g["gap_type"].startswith("publication_")] == ["publication_corroboration_budget_exhausted"]
    assert report["summary"]["model_calls"] <= budget.get("max_model_calls", 8)
    assert report["summary"]["links_read"] <= budget.get("max_links_to_read", 4)


# --- triage protocol and stored re-validation --------------------------------

def test_corroboration_triage_rejects_unknown_or_unread_targets():
    items = [{"id": "a"}, {"id": "b"}]
    base = {"read_priority": 80, "need_model": True, "reason": "人工"}
    good = {"results": [{**base, "id": "a", "triage_decision": "read", "task_read_rank": 1,
                         "publication_targets": ["t1"]},
                        {**base, "id": "b", "triage_decision": "link_record_only", "read_priority": 10,
                         "task_read_rank": 2, "publication_targets": []}]}
    rows = validate_task_triage(items, good, basis=PUBLICATION_BASIS, publication_targets={"t1"})
    assert rows["a"]["task_review"]["publication_targets"] == ["t1"]
    for broken, error in [
        ({**good["results"][0], "publication_targets": ["t9"]}, "invalid_publication_targets"),
        ({**good["results"][0], "triage_decision": "link_record_only"}, "publication_target_requires_model_read"),
    ]:
        review = {}
        assert validate_task_triage(items, {"results": [broken, good["results"][1]]}, review=review,
                                    basis=PUBLICATION_BASIS, publication_targets={"t1"}) == {}
        assert error in review["errors"]
    review = {}
    assert validate_task_triage(items, good, review=review, basis=PUBLICATION_BASIS) == {}
    assert "missing_publication_targets" in review["errors"]


def test_stored_linked_publication_is_revalidated_on_readback(corroboration):
    api, state = corroboration
    _use_directory(state)
    report = _collect(api, state)
    stored = _links(api, report)[DOC]["metadata"]["publication_time"]
    assert enforce_source_publication_dates(deepcopy(stored))["status"] == "known"
    tampered = deepcopy(stored)
    tampered["linked_evidence"][0]["linked_publication"]["date_subject"] = "current_page_publication"
    assert enforce_source_publication_dates(tampered)["status"] == "unknown"
    tampered = deepcopy(stored)
    tampered["published_at"] = "2026-09-30T00:00:00+08:00"
    assert enforce_source_publication_dates(tampered)["status"] == "unknown"


# --- scenario 5: readback in a new process ----------------------------------

_READBACK = r"""
import json, sys
from mic.api import AnalystAPI
from mic.config import load_config
from mic.modeling.adapter import ModelAdapter
from mic.reader import LinkReader
from mic.search import SearchProvider

calls = []
def forbid(name):
    def fail(*args, **kwargs):
        calls.append(name)
        raise AssertionError(name)
    return fail
ModelAdapter.complete = forbid("model")
LinkReader.read = forbid("read")
LinkReader._fetch_http_result = forbid("fetch")
SearchProvider.search = forbid("search")

api = AnalystAPI(load_config())
link_id, target = sys.argv[1], sys.argv[2]
explain = api.explain_source_analysis(link_id)
gaps = [g for g in api.get_coverage_gaps(target) if g.get("source_link_id") == link_id]
print(json.dumps({"explain": explain, "gaps": gaps, "facts": api.search_facts(target),
                  "calls": calls}, ensure_ascii=False, default=str))
"""


@pytest.mark.parametrize("scenario", ["bound", "budget"])
def test_new_process_readback_shows_cross_page_evidence_without_calls(corroboration, scenario):
    api, state = corroboration
    _use_directory(state)
    report = _collect(api, state, **({"max_links_to_read": 1} if scenario == "budget" else {}))
    link_id = _links(api, report)[DOC]["source_link_id"]
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run([sys.executable, "-c", _READBACK, link_id, TARGET], cwd=root,
                          env=dict(os.environ),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    readback = json.loads(proc.stdout.strip().splitlines()[-1])
    assert readback["calls"] == []
    publication = readback["explain"]["source"]["publication_time"]
    assert publication["source"] == "linked_publication_review"
    if scenario == "bound":
        assert publication["status"] == "known" and publication["precision"] == "day"
        assert publication["published_at"].startswith("2026-08-24")
        assert publication["time_window"]["allowed"] is True
        row, = publication["linked_evidence"]
        assert row["supplier_url"] == DIRECTORY
        assert row["claim"]["evidence"][0]["passage_id"].startswith("index_entry_")
        assert row["linked_publication"]["correspondence"]["bases"]
        assert readback["explain"]["models_used"]
        assert readback["facts"]
    else:
        assert publication["status"] == "unknown" and publication["linked_evidence"] == []
        assert publication["unavailable_reason"] == "corroboration_triage_insufficient_budget"
        assert [g["gap_type"] for g in readback["gaps"] if g["gap_type"].startswith("publication_")] == [
            "publication_corroboration_budget_exhausted"]
        assert readback["facts"] == []
