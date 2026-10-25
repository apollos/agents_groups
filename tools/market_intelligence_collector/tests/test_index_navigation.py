"""Offline candidate construction contracts, not a live navigation acceptance."""

from copy import deepcopy
import json
import socket

import pytest

from mic.index_navigation import discover_index_links
from mic.reader import ReadResult
from mic.schemas import Passage, SearchHit


def parent():
    return SearchHit(query="issuer reporting period site:example.org",
                     title="Original index", snippet="Parent search result",
                     url="https://example.org/investor", provider="bing",
                     query_family="task_question",
                     discovery={"relevance": {"target_match": True, "site_ok": True}})


def material(*, urls=None, pagination=None):
    urls = urls if urls is not None else ["https://files.example.net/report.pdf"]
    entries = [{
        "entry_id": f"index_entry_{i + 1}", "text": f"2026/10/01 原始文件 {i + 1}",
        "links": [{"text": f"原始链接 {i + 1}", "url": url}],
    } for i, url in enumerate(urls)]
    return ReadResult(
        source_link_id="link_index", read_status="read",
        final_url="https://example.org/investor?lang=zh",
        passages=[Passage(passage_id=e["entry_id"], section="index_entry", text=e["text"])
                  for e in entries],
        material_scope={
            "kind": "index", "parser_version": "index_scope_v1",
            "entries": entries, "eligible_entries": len(entries) + 2,
            "returned_entries": len(entries) + 2, "selected_entries": len(entries),
            "truncated": True, "truncation_reasons": ["max_selected_passages_per_link"],
            "ordering": "unknown", "completeness": "incomplete",
            "pagination": pagination or {"detected": True, "links": []},
        },
        publication_time={"status": "unknown", "published_at": None},
    )


def discover(read=None, **limits):
    return discover_index_links(read or material(), parent(),
                                max_candidates=limits.get("max_candidates", 20),
                                max_chars=limits.get("max_chars", 50000))


def reasons(audit):
    return {item["reason"] for item in audit["omissions"]}


def test_complete_provenance_preserves_text_and_query_without_inheriting_parent_approval():
    read, hit = material(), parent()
    original_read, original_hit = deepcopy(read), hit.model_copy(deep=True)
    candidates, audit = discover_index_links(read, hit, max_candidates=5, max_chars=10000)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.query == hit.query
    assert candidate.provider == candidate.query_family == "index_navigation"
    assert candidate.url == "https://files.example.net/report.pdf"
    assert candidate.domain == "files.example.net"
    assert candidate.title == "原始链接 1"
    assert candidate.snippet == "2026/10/01 原始文件 1"
    assert candidate.publish_time_guess is None
    assert set(candidate.discovery) == {"index_navigation"}
    provenance = candidate.discovery["index_navigation"]
    assert provenance["protocol"] == "index_navigation_v1"
    assert provenance["parent_source_link_id"] == "link_index"
    assert provenance["parent_url"] == read.final_url
    assert provenance["kind"] == "entry"
    assert provenance["entry_id"] == "index_entry_1"
    assert provenance["link_index"] == 0
    assert provenance["original_url"] == candidate.url
    assert provenance["entry_text"] == candidate.snippet
    assert provenance["link_text"] == candidate.title
    assert provenance["scope"]["completeness"] == "incomplete"
    assert provenance["scope"]["ordering"] == "unknown"
    assert provenance["scope"]["truncated"] is True
    assert provenance["scope"]["pagination_detected"] is True
    assert audit["candidate_json_chars"] == len(json.dumps(
        [candidate.model_dump()], ensure_ascii=False))
    assert read == original_read and hit == original_hit


@pytest.mark.parametrize("change,reason", [
    ("wrong_text", "unselected_or_mismatched_entry"),
    ("missing_passage", "unselected_or_mismatched_entry"),
    ("wrong_section", "unselected_or_mismatched_entry"),
    ("duplicate_passage", "ambiguous_entry_binding"),
    ("duplicate_entry", "ambiguous_entry_binding"),
    ("missing_entry_id", "missing_entry_binding"),
    ("missing_text", "missing_entry_binding"),
    ("missing_links", "links_not_list"),
])
def test_entry_requires_exact_unambiguous_selected_passage_binding(change, reason):
    read = material()
    entry = read.material_scope["entries"][0]
    if change == "wrong_text":
        entry["text"] += " "
    elif change == "missing_passage":
        read.passages = []
    elif change == "wrong_section":
        read.passages[0].section = "body"
    elif change == "duplicate_passage":
        read.passages.append(read.passages[0].model_copy())
    elif change == "duplicate_entry":
        read.material_scope["entries"].append(deepcopy(entry))
    elif change == "missing_entry_id":
        del entry["entry_id"]
    elif change == "missing_text":
        del entry["text"]
    else:
        del entry["links"]
    candidates, audit = discover(read)
    assert candidates == []
    assert reason in reasons(audit)


def test_does_not_expose_unselected_suffix_or_invent_missing_pagination_links():
    read = material()
    read.material_scope["entries"].append({
        "entry_id": "index_entry_9", "text": "A document not selected",
        "links": [{"text": "New document", "url": "https://example.org/unselected.pdf"}],
    })
    candidates, audit = discover(read)
    assert len(candidates) == 1
    assert "unselected_or_mismatched_entry" in reasons(audit)
    assert not any(c.discovery["index_navigation"]["kind"] == "pagination" for c in candidates)


def test_pagination_uses_only_observed_url_and_keeps_original_order_after_entries():
    read = material(pagination={"detected": True, "links": [
        {"text": "2", "url": "https://example.org/investor?page=2", "rel": ["next"]},
        {"text": "", "url": "https://example.org/investor?page=3"},
    ]})
    candidates, audit = discover(read)
    assert [h.url for h in candidates] == [
        "https://files.example.net/report.pdf",
        "https://example.org/investor?page=2",
        "https://example.org/investor?page=3",
    ]
    page = candidates[1].discovery["index_navigation"]
    assert page["kind"] == "pagination"
    assert page["entry_id"] is None and page["entry_text"] == ""
    assert page["link_index"] == 0 and page["link_text"] == "2"
    assert candidates[1].title == "2" and candidates[1].snippet == ""
    assert candidates[2].title == ""
    assert audit["candidate_count"] == 3


def test_empty_link_label_falls_back_to_intact_entry_text():
    read = material()
    read.material_scope["entries"][0]["links"][0]["text"] = ""
    candidates, _ = discover(read)
    assert candidates[0].title == read.passages[0].text
    assert candidates[0].discovery["index_navigation"]["link_text"] == ""


def test_multiple_links_per_entry_keep_indexes_and_canonical_duplicates_keep_first():
    read = material(urls=["https://example.org/report/?b=2&a=1#first",
                          "https://EXAMPLE.org/report?a=1&b=2&utm_source=search"])
    read.material_scope["entries"][0]["links"].append({
        "text": "Second format", "url": "https://files.example.net/second.pdf",
    })
    read.material_scope["pagination"]["links"] = [
        {"text": "Repeat", "url": "https://files.example.net/second.pdf#page=1"}]
    candidates, audit = discover(read)
    assert len(candidates) == 2
    assert candidates[0].url == "https://example.org/report/?b=2&a=1#first"
    assert candidates[1].discovery["index_navigation"]["link_index"] == 1
    assert [h.rank for h in candidates] == [1, 2]
    assert [r["reason"] for r in audit["omissions"]].count("duplicate_canonical_url") == 2


def test_count_limit_stops_at_prefix_and_audits_unexamined_tail():
    read = material(urls=["https://example.org/a.pdf", "https://example.org/b.pdf",
                          "https://example.org/c.pdf"])
    candidates, audit = discover(read, max_candidates=1)
    assert [h.url for h in candidates] == ["https://example.org/a.pdf"]
    assert audit["status"] == "truncated" and audit["stop_reason"] == "max_candidates"
    assert audit["checked_link_count"] == 1 and audit["unexamined_link_count"] == 2
    empty, zero_audit = discover(read, max_candidates=0)
    assert empty == [] and zero_audit["unexamined_link_count"] == 3


def test_character_boundary_is_complete_serialized_hit_list_without_slicing_or_skipping():
    read = material(urls=["https://example.org/a.pdf", "https://example.org/" + "long" * 500,
                          "https://example.org/b.pdf"])
    first, _ = discover(read, max_candidates=1)
    exact = len(json.dumps([first[0].model_dump()], ensure_ascii=False))
    candidates, audit = discover(read, max_chars=exact)
    assert candidates == first
    assert audit["candidate_json_chars"] == exact
    assert audit["stop_reason"] == "max_chars" and audit["unexamined_link_count"] == 1
    assert "max_chars" in reasons(audit)
    empty, smaller = discover(read, max_chars=exact - 1)
    assert empty == [] and smaller["stop_reason"] == "max_chars"
    assert smaller["checked_link_count"] == 1 and smaller["unexamined_link_count"] == 2


@pytest.mark.parametrize("url", [
    "/relative.pdf", "//example.org/document.pdf", "file:///etc/passwd",
    "javascript:alert(1)", "https:///missing-host", "https://user@example.org/a",
    "https://user:password@example.org/a", "https://@example.org/a",
    "https://example.org:bad/a", "https://example.org:65536/a",
    "https://example.org:0/a", "https://example.org:/a",
    "https://localhost/a", "https://localhost./a", "https://sub.localhost/a",
    "https://printer/a", "https://printer.local/a", "https://router.home.arpa/a",
    "https://localhost.localdomain/a",
    "http://127.0.0.1/a", "http://10.0.0.1/a", "http://172.16.0.1/a",
    "http://192.168.1.1/a", "http://169.254.169.254/a", "http://100.64.0.1/a",
    "http://192.0.2.1/a", "http://224.0.0.1/a", "http://0.0.0.0/a",
    "http://[::1]/a", "http://[fe80::1]/a", "http://[fc00::1]/a",
    "http://[::ffff:127.0.0.1]/a", "http://[ff02::1]/a",
    "http://127.1/a", "http://0177.0.0.1/a", "http://0x7f.0.0.1/a",
    "http://2130706433/a", "http://%31%32%37.0.0.1/a",
    "https://exa_mple.org/a", "https://-example.org/a", "https://example..org/a",
    "https://example.org../a",
    "https://[2001:4860:4860::8888]extra/a", "https://example.org\\@127.0.0.1/a",
    "https://example.org/a\n", " https://example.org/a", "https://example.org/a b",
])
def test_invalid_credentials_or_obvious_local_targets_are_omitted_without_repairs(url):
    candidates, audit = discover(material(urls=[url]))
    assert candidates == []
    assert len(audit["omissions"]) == 1
    assert "password" not in json.dumps(audit)


@pytest.mark.parametrize("url", [
    "https://8.8.8.8/a", "https://[2001:4860:4860::8888]:443/a",
    "https://files.example.net:8443/a", "https://例子.公司/公开文件.pdf",
])
def test_public_literal_or_well_formed_domain_candidates_preserve_original_url(url):
    candidates, _ = discover(material(urls=[url]))
    assert [h.url for h in candidates] == [url]


@pytest.mark.parametrize("key,value", [
    ("max_candidates", -1), ("max_candidates", True), ("max_candidates", 1.5),
    ("max_chars", -1), ("max_chars", False), ("max_chars", "1000"),
])
def test_invalid_limits_reject_without_candidates(key, value):
    candidates, audit = discover(**{key: value})
    assert candidates == [] and audit["stop_reason"] == "invalid_limits"


def test_zero_character_budget_never_emits_candidate():
    candidates, audit = discover(max_chars=0)
    assert candidates == [] and audit["stop_reason"] == "max_chars"
    assert audit["checked_link_count"] == 0


@pytest.mark.parametrize("mode", ["failed", "article", "missing_parent", "missing_scope"])
def test_failed_or_non_index_read_does_not_authorize_navigation(mode):
    read = material()
    if mode == "failed":
        read.read_status = "failed"
    elif mode == "article":
        read.material_scope["kind"] = "article"
    elif mode == "missing_parent":
        read.source_link_id = ""
    else:
        read.material_scope = {}
    candidates, _ = discover(read)
    assert candidates == []


def test_malformed_structural_members_are_audited_not_interpreted_as_links():
    read = material()
    read.material_scope["entries"].extend([None, {"entry_id": "x"}])
    read.material_scope["entries"][0]["links"].extend([
        None, {"url": "https://example.org/no-label"}, {"text": "Missing URL"},
    ])
    read.material_scope["pagination"]["links"] = "https://example.org/page"
    candidates, audit = discover(read)
    assert len(candidates) == 1
    assert {"entry_not_object", "missing_entry_binding", "invalid_link",
            "missing_url", "links_not_list"}.issubset(reasons(audit))


def test_discovery_never_resolves_dns_or_opens_sockets(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("candidate discovery must be offline")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    candidates, audit = discover()
    assert len(candidates) == 1
    assert "no_dns_resolution_or_redirect_validation" in audit["limitations"]
