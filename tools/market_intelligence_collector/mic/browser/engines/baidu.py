"""Baidu adapter - selectors verified on live DOM 2026-10-02 (this machine, windowed Edge).

Verified structure (``baidu-dom-20261002``):

* Result root ``#content_left``; one card per direct child ``div.c-container``.
  Organic cards carry class ``result`` (``tpl="www_index"``); Baidu-operated
  modules (百科, 寻标宝, ...) carry ``result-op`` and are excluded.
* Each card has a ``mu`` attribute holding the destination URL declared by the
  engine; the visible ``h3 a[href]`` is a ``baidu.com/link?url=...`` redirect
  wrapper. ``mu`` is used as the candidate URL (``url_resolution="engine_declared"``);
  the reader records the final URL after the real fetch, so a wrong ``mu`` is
  caught there. Cards without a usable ``mu`` stay ``pending_redirect``.
* Snippet ``[class*=summary-gap]`` (new DOM) or ``.c-abstract`` (legacy); the
  snippet starts with the date span ``[class*=prefix-time]`` which is split out
  into ``date_text``. Source name ``.cosc-source`` / ``.c-showurl``.
* Pagination ``#page a.n`` with text 下一页 and ``pn=<10*(page-1)>``.
* Query echo ``input#kw[value]``. CAPTCHA: redirect to ``wappass.baidu.com``
  (observed on the English query) or ``#seccodeImage``.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from mic.browser.contracts import NextPage, RawResult
from mic.browser.engines.base import EngineAdapter, ParsedPage

NO_RESULT_MARKERS = ("很抱歉，没有找到与", "抱歉没有找到", "没有找到相关结果")
EXCLUDED_CLASS_RE = r"(ec_|\bad\b|c-recommend|result-op|op_|new-pmd-ai|wenda|xpath-log-ai)"
EXCLUDED_TPL_PREFIXES = ("sp_", "ai_", "sg_kg", "xbb_", "bk_", "wenda", "video", "img_")
_DATE_RE = re.compile(r"^\s*(\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}|\d+\s*(?:天|小时|分钟)前|昨天|今天)\s*")


class BaiduAdapter(EngineAdapter):
    name = "baidu"
    adapter_version = "baidu-dom-20261002"
    host = "baidu.com"
    verified = True

    def search_url(self, query: str) -> str:
        return f"https://www.baidu.com/s?wd={self._quote(query)}&ie=utf-8"

    def ready_selectors(self) -> list[str]:
        return ["#content_left", "#wrapper_wrapper", ".passMod_dialog-container", "#seccodeImage"]

    def parse(self, html: str, final_url: str | None, query: str, cap: int) -> ParsedPage:
        soup = BeautifulSoup(html, "lxml")
        base = final_url or self.search_url(query)
        if (final_url and "wappass.baidu.com" in final_url) or soup.select_one("#seccodeImage") is not None:
            return ParsedPage(status="captcha", query_observed=None, diagnostics={"challenge": "baidu_wappass"})
        observed = self._observed_query(soup, final_url)
        blocked = self.detect_blocked(html, final_url, soup)
        root = soup.select_one("#content_left")
        if root is None:
            if blocked:
                return ParsedPage(status=blocked, query_observed=observed)
            if any(m in soup.get_text(" ", strip=True) for m in NO_RESULT_MARKERS):
                return ParsedPage(status="no_results", query_observed=observed)
            return ParsedPage(status="parse_error", error_code="parse_error", query_observed=observed,
                              diagnostics={"reason": "content_left_missing"})

        cards = [c for c in root.find_all("div", recursive=False)
                 if self._has_class_like(c, r"(c-container|\bresult\b)")]
        results: list[RawResult] = []
        skipped = {"excluded_module": 0, "no_link": 0, "non_http": 0}
        rank = 0
        seen: set[str] = set()
        for card in cards:
            tpl = str(card.get("tpl") or "")
            if self._has_class_like(card, EXCLUDED_CLASS_RE) or tpl.startswith(EXCLUDED_TPL_PREFIXES):
                skipped["excluded_module"] += 1
                continue
            a = card.select_one("h3 a[href]")
            if a is None:
                skipped["no_link"] += 1
                continue
            rank += 1
            raw_href = str(a.get("href"))
            href = self._abs(base, raw_href)
            if not href or not href.startswith(("http://", "https://")):
                skipped["non_http"] += 1
                continue
            declared = self._declared_url(card)
            if declared is not None:
                url, resolution = declared, "engine_declared"
            elif self.is_engine_url(href):
                url, resolution = None, "pending_redirect"
            else:
                url, resolution = href, "direct"
            key = url or raw_href
            if key in seen:
                continue
            seen.add(key)
            h3 = card.select_one("h3")
            snippet, date_text = self._snippet_and_date(card)
            results.append(RawResult(
                title=self._text(h3) or self._text(a), raw_href=raw_href, url=url,
                snippet=snippet, display_url=self._display(card), date_text=date_text,
                url_resolution=resolution, rank_in_page=rank,
            ))
            if len(results) >= cap:
                break

        if not results:
            if any(m in root.get_text(" ", strip=True) for m in NO_RESULT_MARKERS):
                return ParsedPage(status="no_results", query_observed=observed)
            if blocked:
                return ParsedPage(status=blocked, query_observed=observed)
            return ParsedPage(status="parse_error", error_code="parse_error", query_observed=observed,
                              diagnostics={"reason": "no_organic_cards", "cards": len(cards),
                                           "skipped": skipped})
        return ParsedPage(status="ok", results=results, next_page=self._next_page(soup, base),
                          query_observed=observed, diagnostics={"cards": len(cards), "skipped": skipped})

    # --- helpers -------------------------------------------------------------------

    def _declared_url(self, card) -> str | None:
        mu = str(card.get("mu") or "").strip()
        if not mu.startswith(("http://", "https://")):
            return None
        if self.is_engine_url(mu) and "/link?" in mu:
            return None  # a redirect wrapper is not a destination
        return mu

    def _snippet_and_date(self, card) -> tuple[str, str | None]:
        node = (card.select_one("[class*='summary-gap']") or card.select_one(".c-abstract")
                or card.select_one("span[class*='content-right']"))
        date_node = card.select_one("[class*='prefix-time'], span[class*='time'], .c-color-gray2")
        date_text = self._text(date_node) or None
        if date_text and not _DATE_RE.match(date_text):
            date_text = None
        snippet = self._text(node)
        if date_text and snippet.startswith(date_text):
            snippet = snippet[len(date_text):].strip()
        elif snippet:
            m = _DATE_RE.match(snippet)
            if m:
                date_text = date_text or m.group(1)
                snippet = snippet[m.end():].strip()
        return snippet, date_text

    def _display(self, card) -> str | None:
        node = (card.select_one(".cosc-source") or card.select_one(".c-showurl")
                or card.select_one("span[class*='showurl']") or card.select_one("[class*='source_']"))
        return self._text(node) or None

    def _observed_query(self, soup: BeautifulSoup, final_url: str | None) -> str | None:
        box = soup.select_one("input#kw")
        if box is not None and box.get("value"):
            return str(box.get("value"))
        return self.query_in_url(final_url) if final_url else None

    def _next_page(self, soup: BeautifulSoup, base: str) -> NextPage | None:
        links = [a for a in soup.select("#page a.n[href]") if "下一页" in a.get_text()]
        if not links:
            return None
        a = links[-1]
        href = self._abs(base, a.get("href"))
        if not href:
            return None
        qs = parse_qs(urlparse(href).query)
        pn = qs.get("pn", [None])[0]
        expected = int(pn) // 10 + 1 if pn and pn.isdigit() else None
        return NextPage(kind="link", href=href, label=self._text(a), expected_page_index=expected)
