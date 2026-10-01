"""Engine adapter contract (design section 7).

An adapter knows one engine's normal public search page: how to build the
first-page URL, which DOM features mean "ready", how to recognise challenge /
consent / no-results pages, how to parse organic result cards (and exclude
ads, AI overviews, knowledge panels, PAA, video/shopping modules) and how to
identify the next-page control already present on the page.

Adapters never pick a fallback engine, never widen budgets and never judge
business facts. Unknown page structure is an explicit ``parse_error`` - there
is no "scan every <a>" fallback.
"""

from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from mic.browser.contracts import NextPage, RawResult
from mic.utils import normalize_ws

# Challenge markers shared with the reader's anti-bot detection (kept local so
# adapters have no dependency on the reader module).
CHALLENGE_MARKERS = (
    "安全验证", "请输入验证码", "人机验证", "拖动滑块", "滑动验证", "滑块验证",
    "访问异常", "异常访问", "网络环境异常", "unusual traffic", "Just a moment",
    "Verifying you are human", "Attention Required", "captcha", "CAPTCHA",
    "verify you are not a robot", "我们的系统检测到您的计算机网络中存在异常流量",
)
LOGIN_MARKERS = ("请登录", "登录后继续", "Sign in to continue", "Log in to continue", "登录百度账号")
CONSENT_MARKERS = ("Before you continue", "Accept all", "Reject all", "同意", "使用 Cookie",
                   "consent.google")


def inline_text(node) -> str:
    """Visible text of a node where inline highlight tags do not split words.

    Engines wrap matched query terms in ``<strong>``/``<b>`` inside titles and
    snippets (``<strong>宁德</strong>时代``). ``get_text(" ")`` would turn that
    into ``宁德 时代`` and defeat identity matching. Adjacent text nodes are
    therefore joined without a separator when both sides are CJK characters and
    with a space otherwise; whitespace present in the source is kept as-is.
    """
    if node is None:
        return ""
    out = ""
    for s in node.strings:
        if not s:
            continue
        if out and not out[-1].isspace() and not s[0].isspace():
            if not (_is_cjk(out[-1]) and _is_cjk(s[0])):
                out += " "
        out += s
    return normalize_ws(out)


def _is_cjk(ch: str) -> bool:
    return "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff"


@dataclass
class ParsedPage:
    status: str
    results: list[RawResult] = field(default_factory=list)
    next_page: NextPage | None = None
    query_observed: str | None = None
    error_code: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def normalize_query(text: str | None) -> str:
    """Documented normalisation for query echo comparison: NFKC + whitespace."""
    if text is None:
        return ""
    return normalize_ws(unicodedata.normalize("NFKC", text)).casefold()


def query_match_status(requested: str, observed: str | None) -> str:
    if observed is None:
        return "unknown"
    a, b = normalize_query(requested), normalize_query(observed)
    if a == b:
        return "exact"
    if a and b and (a in b or b in a):
        return "corrected"
    return "mismatch"


def is_http_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


class EngineAdapter(ABC):
    name: str = "base"
    adapter_version: str = "base-v0"
    host: str = ""
    verified: bool = False  # True only after real on-machine acceptance

    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg or {}

    # --- navigation helpers ------------------------------------------------

    @abstractmethod
    def search_url(self, query: str) -> str: ...

    @abstractmethod
    def ready_selectors(self) -> list[str]:
        """DOM selectors any of which means the page reached a decidable state."""

    def is_engine_url(self, url: str | None) -> bool:
        if not url:
            return False
        host = (urlparse(url).hostname or "").lower()
        return host == self.host or host.endswith("." + self.host)

    # --- parsing -----------------------------------------------------------

    @abstractmethod
    def parse(self, html: str, final_url: str | None, query: str, cap: int) -> ParsedPage: ...

    def detect_blocked(self, html: str, final_url: str | None, soup: BeautifulSoup | None = None,
                       text_limit: int = 3000) -> str | None:
        """Shared challenge / login / consent recognition on short pages."""
        soup = soup or BeautifulSoup(html, "lxml")
        title = normalize_ws(soup.title.get_text(" ", strip=True)) if soup.title else ""
        text = normalize_ws(soup.get_text(" ", strip=True))[:text_limit]
        blob = f"{title} {text}"
        url = final_url or ""
        if any(m in blob for m in CHALLENGE_MARKERS):
            return "captcha"
        if any(m in blob for m in LOGIN_MARKERS):
            return "login_required"
        if "consent.google" in url or (len(text) < 1500 and
                                       sum(m in blob for m in CONSENT_MARKERS) >= 2):
            return "consent_required"
        return None

    def validate_next_page(self, next_page: NextPage | None, query: str,
                           current_page_index: int, visited_fingerprints: set[str]) -> NextPage | None:
        """Keep only a next-page link on this engine's host that advances the page."""
        if next_page is None:
            return None
        if next_page.kind == "link":
            if not is_http_url(next_page.href) or not self.is_engine_url(next_page.href):
                return None
            if next_page.expected_page_index is not None and \
                    next_page.expected_page_index <= current_page_index:
                return None
            q = self.query_in_url(next_page.href)
            if q is not None and normalize_query(q) != normalize_query(query):
                return None
        return next_page

    def query_in_url(self, url: str) -> str | None:
        try:
            qs = parse_qs(urlparse(url).query)
        except ValueError:
            return None
        for key in ("q", "wd", "query"):
            if key in qs and qs[key]:
                return qs[key][0]
        return None

    # --- shared utilities --------------------------------------------------

    @staticmethod
    def _text(node) -> str:
        return inline_text(node)

    @staticmethod
    def _abs(base: str, href: str | None) -> str | None:
        if not href:
            return None
        href = href.strip()
        if href.lower().startswith(("javascript:", "file:", "data:", "mailto:", "#")):
            return None
        return urljoin(base, href)

    @staticmethod
    def _quote(query: str) -> str:
        return quote_plus(query)

    @staticmethod
    def _has_class_like(node, pattern: str) -> bool:
        classes = " ".join(node.get("class", []) or [])
        return re.search(pattern, classes, re.I) is not None


def build_engine(name: str, cfg: dict[str, Any] | None = None) -> EngineAdapter:
    if name == "bing":
        from mic.browser.engines.bing import BingAdapter
        return BingAdapter(cfg)
    if name == "google":
        from mic.browser.engines.google import GoogleAdapter
        return GoogleAdapter(cfg)
    if name == "baidu":
        from mic.browser.engines.baidu import BaiduAdapter
        return BaiduAdapter(cfg)
    raise ValueError(f"unknown engine {name!r}")
