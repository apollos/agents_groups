"""Google adapter - selectors verified on live DOM 2026-10-02 (this machine, windowed Edge).

Verified structure (``google-dom-20261002``):

* Result root ``#rso``; organic cards are ``div[data-hveid]`` blocks (class
  ``tF2Cxc`` observed, no ``div.g``) containing ``a[href] > h3``. The innermost
  such block is the card; wrapper blocks that merely contain a card are skipped.
* Result links are ``/goto?url=<opaque token>`` redirect wrappers. The real
  destination is NOT present in the DOM (only the ``cite`` display path and the
  ``span.VuuXrf`` source name), so these results are emitted with ``url=None`` and
  ``url_resolution="pending_redirect"``; ``raw_href`` carries the absolute
  wrapper URL and the reader resolves the destination by following it (the
  final URL is recorded there). A plain external ``href`` is kept as ``direct``.
* Snippet ``div[data-sncf='1']`` / ``.VwiC3b``; it starts with the date span
  ``span.YrbPuc`` (``2025年2月8日 —``) which is split out into ``date_text``.
  Display ``cite`` (``https://host › path``), source ``span.VuuXrf``.
* Pagination ``a#pnnext`` with ``start=<10*(page-1)>``. Query echo ``textarea[name=q]``.
* Challenge: redirect to ``/sorry/`` (its ``input[name=q]`` is a token, never the
  query). First access from a fresh profile is normally challenged; solve it once
  with ``mic browser setup --engine google --url <search url>``.

Ads (``#tads``) and modules (knowledge panel, People also ask, carousels, AI
overview) are excluded by ancestor/descendant markers; they were absent from the
captured page and the markers are retained from public knowledge of the markup.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from mic.browser.contracts import NextPage, RawResult
from mic.browser.engines.base import EngineAdapter, ParsedPage

NO_RESULT_MARKERS = ("找不到和您查询的", "did not match any documents", "没有找到相关结果")
EXCLUDED_ANCESTORS = (
    "#tads", "#bottomads", "[data-text-ad]", ".commercial-unit-desktop-top",  # ads
    "#rhs", ".kp-wholepage",  # knowledge panel
    "[data-initq]", ".related-question-pair",  # people also ask
    "g-scrolling-carousel", "video-voyager", ".ULSxyf",  # carousels / video
    "[data-attrid='SGE']", "#m-x-content",  # AI overview style modules
)
_DATE_RE = re.compile(
    r"^\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}|\d+\s*(?:天|小时|分钟)前|昨天|今天"
    r"|[A-Z][a-z]{2}\.? \d{1,2}, \d{4}|\d+ (?:days?|hours?|minutes?) ago)\s*(?:[—–-]\s*)?"
)


class GoogleAdapter(EngineAdapter):
    name = "google"
    adapter_version = "google-dom-20261002"
    host = "google.com"
    verified = True

    def search_url(self, query: str) -> str:
        return f"https://www.google.com/search?q={self._quote(query)}&hl=zh-CN"

    def ready_selectors(self) -> list[str]:
        return ["#search", "#rso", "#recaptcha", "form#captcha-form", "#captcha"]

    def parse(self, html: str, final_url: str | None, query: str, cap: int) -> ParsedPage:
        soup = BeautifulSoup(html, "lxml")
        base = final_url or self.search_url(query)
        if (final_url and "/sorry/" in final_url) or soup.select_one("#recaptcha, form#captcha-form"):
            # Observed live (2026-10): the /sorry/ form carries an ``input[name=q]`` holding a
            # challenge token, not the query. Never report it as the echoed query.
            return ParsedPage(status="captcha", query_observed=None,
                              diagnostics={"challenge": "google_sorry"})
        observed = self._observed_query(soup, final_url)
        blocked = self.detect_blocked(html, final_url, soup)
        root = soup.select_one("#rso") or soup.select_one("#search")
        if root is None:
            if blocked:
                return ParsedPage(status=blocked, query_observed=observed)
            text = soup.get_text(" ", strip=True)
            if any(m in text for m in NO_RESULT_MARKERS):
                return ParsedPage(status="no_results", query_observed=observed)
            return ParsedPage(status="parse_error", error_code="parse_error", query_observed=observed,
                              diagnostics={"reason": "search_root_missing"})

        results: list[RawResult] = []
        seen: set[str] = set()
        cards = self._cards(root)
        excluded_nodes = {id(n) for sel in EXCLUDED_ANCESTORS for n in soup.select(sel)}
        skipped = {"excluded_module": 0, "no_link": 0, "non_http": 0}
        rank = 0
        for card in cards:
            if id(card) in excluded_nodes or any(id(p) in excluded_nodes for p in card.parents) \
                    or any(card.select_one(sel) is not None for sel in EXCLUDED_ANCESTORS):
                skipped["excluded_module"] += 1
                continue
            h3 = card.select_one("a[href] h3")
            a = h3.find_parent("a", href=True) if h3 is not None else None
            if a is None:
                skipped["no_link"] += 1
                continue
            href = self._abs(base, a.get("href"))
            if not href or not href.startswith(("http://", "https://")):
                skipped["non_http"] += 1
                continue
            if self.is_engine_url(href):
                # ``/goto?url=<token>``: destination unknown until the redirect is followed.
                url, resolution = None, "pending_redirect"
            else:
                url, resolution = href, "direct"
            key = url or href
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            snippet, date_text = self._snippet_and_date(card)
            results.append(RawResult(
                title=self._text(h3), raw_href=href, url=url,
                snippet=snippet, display_url=self._display(card), date_text=date_text,
                url_resolution=resolution, rank_in_page=rank,
            ))
            if len(results) >= cap:
                break

        if not results:
            text = root.get_text(" ", strip=True)
            if any(m in text for m in NO_RESULT_MARKERS):
                return ParsedPage(status="no_results", query_observed=observed)
            if blocked:
                return ParsedPage(status=blocked, query_observed=observed)
            return ParsedPage(status="parse_error", error_code="parse_error", query_observed=observed,
                              diagnostics={"reason": "no_organic_cards", "cards": len(cards),
                                           "skipped": skipped})

        return ParsedPage(status="ok", results=results, next_page=self._next_page(soup, base),
                          query_observed=observed, diagnostics={"cards": len(cards), "skipped": skipped})

    # --- helpers -------------------------------------------------------------------

    @staticmethod
    def _cards(root) -> list:
        """Innermost ``div.g`` / ``div[data-hveid]`` blocks that hold a titled link."""
        candidates = [c for c in root.select("div.g, div[data-hveid]") if c.select_one("a[href] h3") is not None]
        ids = {id(c) for c in candidates}
        cards = []
        for c in candidates:
            inner = [d for d in c.select("div.g, div[data-hveid]") if id(d) in ids]
            if inner:
                continue  # a wrapper around one or more real cards
            cards.append(c)
        return cards

    def _snippet_and_date(self, card) -> tuple[str, str | None]:
        node = (card.select_one("div[data-sncf='1']") or card.select_one(".VwiC3b")
                or card.select_one("[data-sncf]") or card.select_one("div[style*='-webkit-line-clamp']"))
        date_node = card.select_one("span.YrbPuc, span.LEwnzc")
        date_text = self._text(date_node) or None
        if date_text:
            m = _DATE_RE.match(date_text)
            date_text = m.group(1) if m else None
        snippet = self._text(node)
        if snippet:
            m = _DATE_RE.match(snippet)
            if m:
                date_text = date_text or m.group(1)
                snippet = snippet[m.end():].strip()
        return snippet, date_text

    def _display(self, card) -> str | None:
        cite = card.select_one("cite")
        text = self._text(cite)
        if text:
            return text
        source = card.select_one("span.VuuXrf")
        return self._text(source) or None

    def _observed_query(self, soup: BeautifulSoup, final_url: str | None) -> str | None:
        box = soup.select_one("textarea[name=q]") or soup.select_one("input[name=q]")
        if box is not None:
            val = box.get("value") or box.get_text(strip=True)
            if val:
                return str(val)
        return self.query_in_url(final_url) if final_url else None

    def _next_page(self, soup: BeautifulSoup, base: str) -> NextPage | None:
        a = soup.select_one("a#pnnext[href]")
        if a is None:
            return None
        href = self._abs(base, a.get("href"))
        if not href:
            return None
        qs = parse_qs(urlparse(href).query)
        start = qs.get("start", [None])[0]
        expected = None
        if start and start.isdigit():
            expected = int(start) // 10 + 1
        return NextPage(kind="link", href=href, label=self._text(a) or "next", expected_page_index=expected)
