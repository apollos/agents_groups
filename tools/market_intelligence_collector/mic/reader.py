"""Link Reader + Content Preprocessor (spec section 10).

Reads a search-result URL, extracts a transient analysis text, selects the most
relevant passages, and then *discards* the raw body. Only metadata, content
hash and selected passages flow downstream; raw content is never persisted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from mic.config import MICConfig
from mic.profile import TargetProfile
from mic.schemas import Passage
from mic.search import SearchProvider
from mic.utils import content_hash, normalize_ws, simhash

_AMOUNT_RE = re.compile(r"\d+(\.\d+)?\s*(亿元|万元|亿|万吨|吨|GWh|MWh|%|个百分点|元)")
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?|20\d{2}Q[1-4]|近\d+天|上半年|下半年")


@dataclass
class ReadResult:
    source_link_id: str
    read_status: str  # read | failed
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    content_length: Optional[int] = None
    title: Optional[str] = None
    publish_time: Optional[str] = None
    content_hash: Optional[str] = None
    simhash: Optional[str] = None
    passages: list[Passage] = field(default_factory=list)
    failure_reason: Optional[str] = None


class LinkReader:
    def __init__(self, config: MICConfig, search_provider: SearchProvider | None = None):
        self.config = config
        self.search_provider = search_provider
        ap = config.access_profiles.get("default", {})
        self.timeout = ap.get("timeout_seconds", 15)
        self.user_agent = ap.get("user_agent", "MIC/0.3")
        self.access_profile_id = ap.get("profile_id", "default")
        gov = (config.call_governance or {}).get("budgets", {})
        self.max_passages = gov.get("max_selected_passages_per_link", 8)
        self.max_chars = gov.get("max_input_chars_per_model_call", 8000)

    def read(self, source_link_id: str, url: str, profile: TargetProfile) -> ReadResult:
        html, http_status, ctype = self._fetch(url)
        if html is None:
            return ReadResult(source_link_id=source_link_id, read_status="failed",
                              http_status=http_status, failure_reason="fetch_failed")
        title, publish_time, body = self._extract(html)
        chash = content_hash(body)
        shash = simhash(body)
        passages = self._select_passages(title, body, profile)
        return ReadResult(
            source_link_id=source_link_id, read_status="read", http_status=http_status,
            content_type=ctype, content_length=len(body), title=title,
            publish_time=publish_time, content_hash=chash, simhash=shash,
            passages=passages,
        )

    # --- fetch -------------------------------------------------------------

    def _fetch(self, url: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
        # Offline/mock support: synthetic providers can serve bodies directly.
        if self.search_provider is not None:
            body = self.search_provider.page_body(url)
            if body is not None:
                return body, 200, "text/html"
        try:
            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True,
                             headers={"User-Agent": self.user_agent})
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200:
                return None, resp.status_code, ctype
            return resp.text, resp.status_code, ctype
        except (httpx.HTTPError, OSError):
            return None, None, None

    # --- extraction --------------------------------------------------------

    def _extract(self, html: str) -> tuple[str, Optional[str], str]:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        title = ""
        if soup.title and soup.title.string:
            title = normalize_ws(soup.title.string)
        elif soup.h1:
            title = normalize_ws(soup.h1.get_text())

        publish_time = self._guess_publish_time(soup)

        blocks = []
        for el in soup.find_all(["p", "li", "td", "h1", "h2", "h3"]):
            txt = normalize_ws(el.get_text())
            if len(txt) >= 8:
                blocks.append(txt)
        if not blocks:
            blocks = [normalize_ws(soup.get_text())]
        return title, publish_time, "\n".join(blocks)

    @staticmethod
    def _guess_publish_time(soup: BeautifulSoup) -> Optional[str]:
        for meta_name in ("article:published_time", "publishdate", "pubdate", "date"):
            tag = soup.find("meta", attrs={"property": meta_name}) or \
                  soup.find("meta", attrs={"name": meta_name})
            if tag and tag.get("content"):
                return tag["content"]
        text = soup.get_text()[:2000]
        m = _DATE_RE.search(text)
        return m.group(0) if m else None

    # --- passage selection (spec 10.2) ------------------------------------

    def _select_passages(self, title: str, body: str,
                        profile: TargetProfile) -> list[Passage]:
        entity_terms = profile.all_entity_terms()
        keyword_terms = (
            profile.products + profile.customers + profile.suppliers
            + ["客户", "供应商", "政策", "订单", "中标", "处罚", "涨价", "降价",
               "毛利率", "产能", "库存", "开工率", "风险"]
        )
        paragraphs = [p for p in body.split("\n") if p.strip()]
        scored: list[tuple[float, int, str]] = []
        for idx, para in enumerate(paragraphs):
            score = 0.0
            if idx == 0:
                score += 5  # first paragraph
            if any(t and t in para for t in entity_terms):
                score += 8
            if any(t in para for t in keyword_terms):
                score += 4
            if _AMOUNT_RE.search(para):
                score += 6
            if _DATE_RE.search(para):
                score += 3
            if any(w in para for w in ("综上", "总体", "预计", "影响", "因此")):
                score += 2
            if score > 0:
                scored.append((score, idx, para))

        scored.sort(key=lambda x: (-x[0], x[1]))
        selected = scored[: self.max_passages - 1]  # leave room for title passage
        selected.sort(key=lambda x: x[1])  # restore document order

        passages: list[Passage] = []
        if title:
            passages.append(Passage(passage_id="title", section="标题", text=title))
        budget = self.max_chars
        for _score, idx, para in selected:
            text = para[: max(0, budget)]
            if not text:
                break
            passages.append(Passage(
                passage_id=f"p{idx}", section=f"正文第{idx + 1}段", text=text))
            budget -= len(text)
            if budget <= 0:
                break
        return passages
