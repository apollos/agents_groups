"""Build bounded navigation candidates from already selected directory material.

This module performs no reads and makes no relevance, publication or content
approval decisions. A candidate still needs the ordinary model triage and
budgeted reading pipeline. Literal-address checks below do not resolve DNS or
inspect redirects, and are not a complete SSRF defense.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from ipaddress import ip_address
import json
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mic.schemas import SearchHit
from mic.utils import canonicalize_url, domain_of

if TYPE_CHECKING:
    from mic.reader import ReadResult


PROTOCOL = "index_navigation_v1"
_SCOPE_KEYS = (
    "kind", "parser_version", "coverage", "visibility", "eligible_entries",
    "returned_entries", "selected_entries", "truncated", "truncation_reasons",
    "ordering", "completeness",
)
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_NUMERIC_HOST_LABEL = re.compile(r"(?:[0-9]+|0x[0-9a-f]+)\Z", re.I)


def _url_error(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return "missing_url"
    # urlsplit and browsers can normalize these differently. Do not silently
    # repair a source URL before binding it to selected material.
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url) or "\\" in url:
        return "invalid_url_characters"
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return "absolute_http_url_required"
        if parsed.username is not None or parsed.password is not None:
            return "url_credentials"
        host = parsed.hostname
        port = parsed.port  # Validate malformed / out-of-range ports.
    except (ValueError, UnicodeError):
        return "invalid_host_or_port"
    if (not host or port == 0 or "%" in host or parsed.netloc.endswith(":")
            or (":" in host and not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc))):
        return "invalid_host_or_port"
    try:
        address = ip_address(host)
    except ValueError:
        host = host.removesuffix(".").lower()
        if (host == "localhost" or host.endswith((".localhost", ".local", ".localdomain", ".home.arpa"))
                or "." not in host):
            return "local_hostname"
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return "invalid_hostname"
        labels = ascii_host.split(".")
        if len(ascii_host) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
            return "invalid_hostname"
        # Reject non-canonical numeric IP spellings that browsers may resolve
        # as literal addresses (127.1, octal / hexadecimal components).
        if all(_NUMERIC_HOST_LABEL.fullmatch(label) for label in labels):
            return "noncanonical_ip_literal"
    else:
        if not address.is_global or address.is_multicast:
            return "non_global_ip_literal"
    return None


def discover_index_links(read: ReadResult, parent_hit: SearchHit, *,
                         max_candidates: int, max_chars: int) -> tuple[list[SearchHit], dict]:
    """Return candidates in material order, plus a content-neutral audit.

    Entry links require an exact, unambiguous selected-passage ID/text binding.
    Pagination links come only from the selected material's pagination metadata.
    ``max_chars`` bounds the JSON serialization of the complete returned
    ``SearchHit.model_dump()`` list, using ``ensure_ascii=False``. Candidates are
    never sliced to fit; the first count/character limit stops discovery.
    """
    audit = {
        "protocol": PROTOCOL, "parent_source_link_id": read.source_link_id,
        "status": "not_applicable", "limits": {
            "max_candidates": max_candidates, "max_chars": max_chars,
        },
        "candidate_count": 0, "candidate_json_chars": 2, "input_link_count": 0,
        "checked_link_count": 0, "unexamined_link_count": 0,
        "omissions": [], "stop_reason": None,
        "limitations": ["no_dns_resolution_or_redirect_validation",
                        "navigation_candidates_are_not_content_approval"],
    }
    if (type(max_candidates) is not int or max_candidates < 0
            or type(max_chars) is not int or max_chars < 0):
        audit.update(status="rejected", stop_reason="invalid_limits")
        return [], audit
    scope = read.material_scope
    if read.read_status != "read" or not isinstance(scope, dict) or scope.get("kind") != "index":
        return [], audit
    if not isinstance(read.source_link_id, str) or not read.source_link_id:
        audit.update(status="rejected", stop_reason="missing_parent_source_link_id")
        return [], audit
    audit["status"] = "completed"
    parent_url = read.final_url or parent_hit.url
    scope_summary = {key: deepcopy(scope[key]) for key in _SCOPE_KEYS if key in scope}
    pagination = scope.get("pagination")
    if isinstance(pagination, dict):
        scope_summary["pagination_detected"] = pagination.get("detected")

    passages = [p for p in read.passages if p.section == "index_entry"]
    passage_counts = Counter(p.passage_id for p in read.passages)
    passage_text = {p.passage_id: p.text for p in passages if passage_counts[p.passage_id] == 1}
    entries = scope.get("entries", [])
    if not isinstance(entries, list):
        audit["omissions"].append({"kind": "entry", "reason": "entries_not_list"})
        entries = []
    entry_counts = Counter(e.get("entry_id") for e in entries
                           if isinstance(e, dict) and isinstance(e.get("entry_id"), str))
    items: list[dict] = []
    for entry_index, entry in enumerate(entries):
        omission = {"kind": "entry", "entry_index": entry_index}
        if not isinstance(entry, dict):
            audit["omissions"].append({**omission, "reason": "entry_not_object"})
            continue
        entry_id, text = entry.get("entry_id"), entry.get("text")
        if not isinstance(entry_id, str) or not entry_id or not isinstance(text, str) or not text:
            audit["omissions"].append({**omission, "reason": "missing_entry_binding"})
            continue
        omission["entry_id"] = entry_id
        if entry_counts[entry_id] != 1 or passage_counts[entry_id] > 1:
            audit["omissions"].append({**omission, "reason": "ambiguous_entry_binding"})
            continue
        if passage_text.get(entry_id) != text:
            audit["omissions"].append({**omission, "reason": "unselected_or_mismatched_entry"})
            continue
        links = entry.get("links")
        if not isinstance(links, list):
            audit["omissions"].append({**omission, "reason": "links_not_list"})
            continue
        for link_index, link in enumerate(links):
            items.append({"kind": "entry", "entry_id": entry_id, "entry_text": text,
                          "link_index": link_index, "link": link})
    if isinstance(pagination, dict):
        links = pagination.get("links", [])
        if isinstance(links, list):
            for link_index, link in enumerate(links):
                items.append({"kind": "pagination", "entry_id": None, "entry_text": "",
                              "link_index": link_index, "link": link})
        else:
            audit["omissions"].append({"kind": "pagination", "reason": "links_not_list"})
    audit["input_link_count"] = len(items)
    candidates: list[SearchHit] = []
    seen: set[str] = set()
    for ordinal, item in enumerate(items):
        if len(candidates) >= max_candidates or max_chars < 2:
            audit["stop_reason"] = "max_candidates" if len(candidates) >= max_candidates else "max_chars"
            audit["unexamined_link_count"] = len(items) - ordinal
            break
        audit["checked_link_count"] += 1
        provenance = {key: value for key, value in item.items() if key != "link"}
        link = item["link"]
        if not isinstance(link, dict) or not isinstance(link.get("text"), str):
            audit["omissions"].append({**_item_locator(item), "reason": "invalid_link"})
            continue
        url = link.get("url")
        error = _url_error(url)
        if error:
            audit["omissions"].append({**_item_locator(item), "reason": error})
            continue
        canonical = canonicalize_url(url)
        if canonical in seen:
            audit["omissions"].append({**_item_locator(item), "reason": "duplicate_canonical_url"})
            continue
        provenance.update(protocol=PROTOCOL, parent_source_link_id=read.source_link_id,
                          parent_url=parent_url, link_text=link["text"], original_url=url,
                          scope=deepcopy(scope_summary))
        hit = SearchHit(
            query=parent_hit.query, title=link["text"] or item["entry_text"],
            snippet=item["entry_text"], url=url, domain=domain_of(url),
            rank=len(candidates) + 1, provider="index_navigation",
            query_family="index_navigation", discovery={"index_navigation": provenance},
        )
        candidate_chars = len(json.dumps(
            [candidate.model_dump() for candidate in [*candidates, hit]], ensure_ascii=False))
        if candidate_chars > max_chars:
            audit["omissions"].append({**_item_locator(item), "reason": "max_chars"})
            audit["stop_reason"] = "max_chars"
            audit["unexamined_link_count"] = len(items) - ordinal - 1
            break
        seen.add(canonical)
        candidates.append(hit)
        audit["candidate_json_chars"] = candidate_chars
    audit["candidate_count"] = len(candidates)
    if audit["stop_reason"]:
        audit["status"] = "truncated"
    return candidates, audit


def _item_locator(item: dict) -> dict:
    """Small locator for an omitted candidate, excluding source body/URL."""
    return {key: item[key] for key in ("kind", "entry_id", "link_index")}
