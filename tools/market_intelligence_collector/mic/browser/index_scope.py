"""Bounded, structural evidence from an index page explicitly selected as such.

This is not an article fallback or a semantic approval step. Links are retained
as evidence and never fetched. Entry dates do not date the index publication.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from mic.utils import normalize_ws


MAX_HTML_CHARS = 2_000_000
MAX_DOM_NODES = 20_000
MAX_DOM_DEPTH = 128
MAX_ENTRIES = 80
MAX_ENTRY_CHARS = 2_000
MAX_TOTAL_CHARS = 20_000
MAX_LINK_URL_CHARS = 2_048
MAX_ENTRY_MATERIAL_CHARS = 8_000
MAX_SCOPE_METADATA_CHARS = 8_000
MAX_PAGINATION_CHARS = 4_000
MAX_TOTAL_MATERIAL_CHARS = 60_000
_CHROME = re.compile(
    r"(?:^|[\s_-])(?:nav(?:igation|bar)?|menu|footer|header|sidebar|breadcrumb|"
    r"related|recommend(?:ed|ation|ations)?|comments?|share|social|advert(?:isement)?|ads)(?:$|[\s_-])",
    re.I,
)
_PAGER = re.compile(r"(?:^|[\s_-])(?:pagination|pager|paging)(?:$|[\s_-])", re.I)
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important)?\s*(?:;|$)", re.I)
_DATE_SHAPE = re.compile(r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日)(?!\d)")
_DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt", ".odt", ".ods")


class _MaterialLimit(ValueError):
    """An entire entry/link must be omitted, never a sliced evidence value."""


def _json_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _mark_truncated(scope: dict, reason: str) -> None:
    scope["truncated"] = True
    if reason not in scope["truncation_reasons"]:
        scope["truncation_reasons"].append(reason)


def _signature(node: Tag) -> str:
    return " ".join([str(node.get("id", "")), *node.get("class", [])])


def _hidden(node: Tag) -> bool:
    return (node.has_attr("hidden") or str(node.get("aria-hidden", "")).lower() == "true"
            or bool(_HIDDEN_STYLE.search(str(node.get("style", "")))))


def _original_locators(nodes: list[Tag]) -> dict | None:
    """Record original CSS paths in one pass, without sibling rescans."""
    paths, depths, counts = {}, {}, {}
    for node in nodes:
        parent_key = id(node.parent)
        depth = depths.get(parent_key, 0) + 1
        if depth > MAX_DOM_DEPTH:
            return None
        ordinal_key = (parent_key, node.name)
        ordinal = counts.get(ordinal_key, 0) + 1
        counts[ordinal_key] = ordinal
        prefix = paths.get(parent_key, "")
        paths[id(node)] = (prefix + " > " if prefix else "") + f"{node.name}:nth-of-type({ordinal})"
        depths[id(node)] = depth
    return paths


def _link(node: Tag, base_url: str) -> dict | None:
    href = str(node.get("href", "")).strip()
    if not href or href.startswith("#"):
        return None
    if len(href) > MAX_LINK_URL_CHARS:
        raise _MaterialLimit("link_url_chars")
    try:
        url = urljoin(base_url, href)
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if len(url) > MAX_LINK_URL_CHARS:
        raise _MaterialLimit("link_url_chars")
    return {"text": normalize_ws(node.get_text(" ", strip=True)), "url": url}


def _entry(node: Tag, base_url: str, locators: dict) -> dict | None:
    links = []
    for anchor in ([node] if node.name == "a" else node.find_all("a", href=True)):
        link = _link(anchor, base_url)
        if link and link not in links:
            links.append(link)
    if not links or len(links) > 4 or not any(link["text"] for link in links):
        return None
    text = normalize_ws(node.get_text(" ", strip=True))
    if len(text) < 8:
        return None
    # A plain list of website destinations is indistinguishable from a menu.
    # Require structural metadata: adjacent date text, a time node, a document
    # resource. A layout table alone is not evidence of an index. No business
    # keyword determines this.
    outside_links = normalize_ws(" ".join(
        str(part) for part in node.find_all(string=True)
        if part.find_parent("a") is None
    ))
    times = node.find_all("time")
    document_link = any(urlsplit(link["url"]).path.lower().endswith(_DOCUMENT_EXTENSIONS) for link in links)
    if not (times or _DATE_SHAPE.search(outside_links) or document_link):
        return None
    return {
        "text": text, "links": links, "locator": locators[id(node)],
        "date_hints": [{"text": normalize_ws(t.get_text(" ", strip=True)),
                        "datetime": t.get("datetime"), "locator": locators[id(t)]} for t in times],
    }


def extract_index_scope(html: str, *, base_url: str = "") -> dict:
    """Return one unambiguous index container, with whole-entry size bounds.

    Only static visibility attributes can be checked. Absence of pagination
    controls never proves the listing complete or its first entry latest.
    """
    publication = {"status": "unknown", "published_at": None,
                   "reason": "index_entry_dates_are_not_source_publication"}
    scope = {
        "kind": "index", "containers": [], "candidate_containers": 0,
        "eligible_entries": 0, "returned_entries": 0, "truncated": False,
        "truncation_reasons": [], "removed_containers": 0,
        "pagination": {"detected": False, "links": [], "current": []},
        "coverage": "visible_entries_only", "visibility": "static_dom_attributes_only",
        "followed_links": 0,
        "limits": {"max_entries": MAX_ENTRIES, "max_entry_chars": MAX_ENTRY_CHARS,
                   "max_total_chars": MAX_TOTAL_CHARS, "max_html_chars": MAX_HTML_CHARS,
                   "max_dom_nodes": MAX_DOM_NODES, "max_dom_depth": MAX_DOM_DEPTH,
                   "max_link_url_chars": MAX_LINK_URL_CHARS,
                   "max_entry_material_chars": MAX_ENTRY_MATERIAL_CHARS,
                   "max_scope_metadata_chars": MAX_SCOPE_METADATA_CHARS,
                   "max_pagination_chars": MAX_PAGINATION_CHARS,
                   "max_total_material_chars": MAX_TOTAL_MATERIAL_CHARS},
    }
    result = {"status": "unresolved", "title": "", "entries": [], "scope": scope,
              "reason": "index_container_not_found", "publication_time": publication}
    if len(html) > MAX_HTML_CHARS:
        result["reason"] = "index_html_limit_exceeded"
        scope.update(truncated=True, truncation_reasons=["html_chars"])
        return result
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.find_all(True)
    if len(nodes) > MAX_DOM_NODES:
        result["reason"] = "index_dom_limit_exceeded"
        scope.update(truncated=True, truncation_reasons=["dom_nodes"])
        return result
    locators = _original_locators(nodes)
    if locators is None:
        result["reason"] = "index_dom_depth_exceeded"
        scope.update(truncated=True, truncation_reasons=["dom_depth"])
        return result
    result["title"] = normalize_ws(soup.title.get_text(" ", strip=True)) if soup.title else ""
    pagination = scope["pagination"]
    for node in nodes:
        if node.attrs is None:
            continue
        excluded = (node.name in {"script", "style", "template", "noscript", "footer", "aside", "header", "form"}
                    or node.get("role") in {"complementary", "dialog", "menu", "menubar"}
                    or _hidden(node))
        if excluded:
            node.decompose()
            scope["removed_containers"] += 1
            continue
        is_pager = bool(_PAGER.search(_signature(node)))
        if node.name == "nav" or node.get("role") == "navigation":
            is_pager |= bool(node.select('a[rel~="next"], a[rel~="prev"], [aria-current="page"]'))
        rels = node.get("rel", [])
        if is_pager or (node.name == "a" and set(rels).intersection({"next", "prev"})):
            pagination["detected"] = True
            for anchor in ([node] if node.name == "a" else node.find_all("a", href=True)):
                if _hidden(anchor) or any(_hidden(p) for p in anchor.parents if isinstance(p, Tag)):
                    continue
                try:
                    link = _link(anchor, base_url)
                except _MaterialLimit as exc:
                    _mark_truncated(scope, str(exc))
                    continue
                if not link:
                    continue
                entry = {**link, "rel": list(anchor.get("rel", []))}
                candidate = {**pagination, "links": [*pagination["links"], entry]}
                if len(pagination["links"]) >= 20 or _json_chars(candidate) > MAX_PAGINATION_CHARS:
                    _mark_truncated(scope, "pagination_metadata_chars")
                else:
                    pagination["links"].append(entry)
            for current in node.select('[aria-current="page"]'):
                value = normalize_ws(current.get_text(" ", strip=True))
                candidate = {**pagination, "current": [*pagination["current"], value]}
                if len(pagination["current"]) >= 20 or _json_chars(candidate) > MAX_PAGINATION_CHARS:
                    _mark_truncated(scope, "pagination_metadata_chars")
                else:
                    pagination["current"].append(value)
            node.decompose()
            scope["removed_containers"] += 1
            continue
        if (node.name == "nav" or node.get("role") == "navigation" or _CHROME.search(_signature(node))):
            node.decompose()
            scope["removed_containers"] += 1
    if not result["title"] and soup.h1:
        result["title"] = normalize_ws(soup.h1.get_text(" ", strip=True))
    if len(result["title"]) > MAX_ENTRY_CHARS:
        result["title"] = ""
        _mark_truncated(scope, "title_chars")

    candidates = []
    for container in soup.find_all(["ul", "ol", "table", "div", "section"]):
        if container.name in {"ul", "ol"}:
            rows = container.find_all("li", recursive=False)
        elif container.name == "table":
            rows = [row for row in container.find_all("tr")
                    if row.find_parent("table") is container and row.find("td", recursive=False)]
        else:
            rows = [child for child in container.find_all(["div", "p", "article"], recursive=False)
                    if not child.find(["ul", "ol", "table", "article"])]
        entries = []
        for row in rows:
            try:
                entry = _entry(row, base_url, locators)
            except _MaterialLimit as exc:
                _mark_truncated(scope, str(exc))
                continue
            if entry:
                entries.append(entry)
        if len(entries) >= 2:
            candidates.append((container, entries))
    # An outer layout wrapping an independently scoped list adds no evidence.
    candidates = [(node, entries) for node, entries in candidates
                  if not any(other is not node and any(parent is node for parent in other.parents)
                             for other, _ in candidates)]
    scope["candidate_containers"] = len(candidates)
    if len(candidates) != 1:
        if candidates:
            result["reason"] = "ambiguous_index_containers"
        return result
    container, entries = candidates[0]
    scope["containers"] = [{"locator": locators[id(container)], "tag": container.name,
                            "table_headers": [normalize_ws(th.get_text(" ", strip=True))
                                              for th in container.find_all("th")][:30]}]
    if _json_chars({"title": result["title"], "scope": scope}) > MAX_SCOPE_METADATA_CHARS:
        # Huge table headers/metadata must not leak to the model, even when
        # each visible row has only a short title and date.
        scope["containers"] = []
        _mark_truncated(scope, "scope_metadata_chars")
        result["reason"] = "index_scope_metadata_limit_exceeded"
        return result
    scope["eligible_entries"] = len(entries)
    total_chars = 0
    material_chars = _json_chars({"title": result["title"], "scope": scope})
    for ordinal, entry in enumerate(entries):
        material_entry = {"entry_id": f"index_entry_{ordinal + 1}", **entry}
        entry_material_chars = _json_chars(material_entry)
        reason = None
        if len(entry["text"]) > MAX_ENTRY_CHARS:
            reason = "entry_chars"
        elif len(result["entries"]) >= MAX_ENTRIES:
            reason = "entry_count"
        elif total_chars + len(entry["text"]) > MAX_TOTAL_CHARS:
            reason = "total_chars"
        elif entry_material_chars > MAX_ENTRY_MATERIAL_CHARS:
            reason = "entry_metadata_chars"
        elif material_chars + entry_material_chars + 2 + 1024 > MAX_TOTAL_MATERIAL_CHARS:
            reason = "total_material_chars"
        if reason:
            _mark_truncated(scope, reason)
            continue
        result["entries"].append(material_entry)
        total_chars += len(entry["text"])
        material_chars += entry_material_chars + 2
    scope["returned_entries"] = len(result["entries"])
    scope["returned_chars"] = total_chars
    if result["entries"]:
        result.update(status="scoped", reason="unique_structural_index")
    else:
        result["reason"] = "index_entries_exceed_limits"
    return result
