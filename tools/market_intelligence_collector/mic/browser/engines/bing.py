"""Bing adapter.

Selectors observed on 2026-10-01 on a real windowed Edge session (design
section 7.2): organic cards are ``#b_results > li.b_algo``; the title link is
``h2 a``; snippet is ``.b_caption p`` (or ``p``); the pagination control is
``a.sb_pagN`` / ``nav[aria-label] a[title^="下一页"]`` inside ``.b_pag``.
Non-organic modules (``li.b_ad``, ``li.b_ans``, ``.b_algoBigWiki`` knowledge
cards, video/image/news carousels) are excluded by never selecting them.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from mic.browser.contracts import NextPage, RawResult
from mic.browser.engines.base import EngineAdapter, ParsedPage

NO_RESULT_MARKERS = ("没有与此相关的结果", "There are no results for", "没有找到与", "无结果")
_FIRST_PAGE_RE = re.compile(r"[?&]first=(\d+)")


class BingAdapter(EngineAdapter):
    name = "bing"
    adapter_version = "bing-dom-20261001"
    host = "bing.com"
    verified = True

    def search_url(self, query: str) -> str:
        # Plain first-page URL only. Pagination follows the on-page control.
        return f"https://www.bing.com/search?q={self._quote(query)}&ensearch=0"

    def ready_selectors(self) -> list[str]:
        return ["#b_results", "#b_content", "#b_header"]

    def parse(self, html: str, final_url: str | None, query: str, cap: int) -> ParsedPage:
        soup = BeautifulSoup(html, "lxml")
        base = final_url or self.search_url(query)
        blocked = self.detect_blocked(html, final_url, soup)
        results_root = soup.select_one("#b_results")
        if blocked and results_root is None:
            return ParsedPage(status=blocked, query_observed=self._observed_query(soup, final_url))

        observed = self._observed_query(soup, final_url)
        if results_root is None:
            if soup.select_one("#b_content") is not None or soup.select_one("#b_header") is not None:
                text = soup.get_text(" ", strip=True)
                if any(m in text for m in NO_RESULT_MARKERS):
                    return ParsedPage(status="no_results", query_observed=observed)
                return ParsedPage(status="parse_error", error_code="parse_error",
                                  query_observed=observed,
                                  diagnostics={"reason": "b_results_missing"})
            return ParsedPage(status="parse_error", error_code="parse_error", query_observed=observed,
                              diagnostics={"reason": "unrecognised_dom"})

        cards = results_root.select(":scope > li.b_algo")
        results: list[RawResult] = []
        skipped = {"no_link": 0, "non_http": 0, "engine_internal": 0}
        for rank, card in enumerate(cards, start=1):
            a = card.select_one("h2 a[href]") or card.select_one("a.tilk[href]")
            if a is None:
                skipped["no_link"] += 1
                continue
            href = self._abs(base, a.get("href"))
            if not href or not href.startswith(("http://", "https://")):
                skipped["non_http"] += 1
                continue
            if self.is_engine_url(href):
                # Bing redirect wrappers (bing.com/ck/a?...&u=...) are not accepted;
                # only direct external links count as organic results.
                skipped["engine_internal"] += 1
                continue
            title = self._text(a)
            snippet_node = card.select_one(".b_caption p") or card.select_one("p")
            cite = card.select_one("cite")
            results.append(RawResult(
                title=title, raw_href=str(a.get("href")), url=href,
                snippet=self._text(snippet_node), display_url=self._text(cite) or None,
                date_text=self._published(card), rank_in_page=rank,
            ))
            if len(results) >= cap:
                break

        if not results:
            text = results_root.get_text(" ", strip=True)
            if any(m in text for m in NO_RESULT_MARKERS) or soup.select_one(".b_no") is not None:
                return ParsedPage(status="no_results", query_observed=observed,
                                  diagnostics={"cards": len(cards), "skipped": skipped})
            if cards:
                return ParsedPage(status="parse_error", error_code="parse_error", query_observed=observed,
                                  diagnostics={"reason": "cards_without_links", "cards": len(cards),
                                               "skipped": skipped})
            if blocked:
                return ParsedPage(status=blocked, query_observed=observed)
            return ParsedPage(status="no_results", query_observed=observed,
                              diagnostics={"cards": 0, "skipped": skipped})

        return ParsedPage(status="ok", results=results, next_page=self._next_page(soup, base, final_url),
                          query_observed=observed, diagnostics={"cards": len(cards), "skipped": skipped})

    # --- helpers -----------------------------------------------------------

    def _observed_query(self, soup: BeautifulSoup, final_url: str | None) -> str | None:
        box = soup.select_one("input#sb_form_q") or soup.select_one("textarea#sb_form_q")
        if box is not None:
            val = box.get("value") or box.get_text(strip=True)
            if val:
                return str(val)
        if final_url:
            return self.query_in_url(final_url)
        return None

    def _next_page(self, soup: BeautifulSoup, base: str, final_url: str | None) -> NextPage | None:
        a = soup.select_one(".b_pag a.sb_pagN[href]") or soup.select_one(
            'nav[aria-label] a[title^="下一页"][href]') or soup.select_one(
            'nav[aria-label] a[title^="Next page"][href]')
        if a is None:
            return None
        href = self._abs(base, a.get("href"))
        if not href:
            return None
        expected = None
        m = _FIRST_PAGE_RE.search(href)
        if m:
            # Observed live (2026-10): page 2 is `first=10` on cn.bing.com and
            # `first=11` on www.bing.com; both map to page index 2.
            first = int(m.group(1))
            expected = max(1, first // 10 + 1)
        return NextPage(kind="link", href=href, label=self._text(a) or (a.get("title") or ""),
                        expected_page_index=expected)

    @staticmethod
    def _published(card) -> str | None:
        node = card.select_one(".news_dt") or card.select_one("span.b_attribution + span")
        return node.get_text(strip=True) if node is not None else None

    @staticmethod
    def page_index_from_url(url: str) -> int:
        m = _FIRST_PAGE_RE.search(url)
        if not m:
            return 1
        return max(1, (int(m.group(1)) - 1) // 10 + 1)

    def query_in_url(self, url: str) -> str | None:
        qs = parse_qs(urlparse(url).query)
        return qs.get("q", [None])[0]
