"""Explainable relevance rules for search result cards (design section 6.3).

Rules only, no model calls. A result is *relevant* when all three hold:

1. target identity - title or snippet matches the target's canonical name,
   an alias or a ticker *with context*. Shortened forms ("宁德" for
   "宁德时代") never count; association terms (products, customers,
   suppliers, regions) alone never count. Identity of a *different* company
   that merely shares a prefix does not count either.
2. task match - the hit touches the query family's subject (keywords
   derived from the query itself plus family focus words).
3. content form - looks like an article / announcement / research page
   rather than a listing, tag, search or login page.

``site:`` queries additionally require strict host equality (or subdomain
of) the requested host - "cninfo.com.cn" does not match
"cninfo.com.cn.example.net".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from mic.browser.contracts import RawResult
from mic.run_context import TargetIdentity

LISTING_PATH_RE = re.compile(
    r"(/tag/|/tags/|/search|/list[/_.]|/category/|/channel/|/topic/|/index\.html?$|/page/\d+|"
    r"/login|/signin|/register|/author/|/zt/|/special/|/column/|/node/\d+/?$)", re.I)
# ``/s?`` is a site-search page only when it carries a search-term key. Article hosts such as
# ``baijiahao.baidu.com/s?id=`` and ``mp.weixin.qq.com/s?__biz=`` use the same path for articles
# (observed on the live Baidu SERP, 2026-10) and must not be dropped as listings.
SITE_SEARCH_RE = re.compile(r"/s\?(?:.*&)?(?:wd|q|query|keyword|keywords|kw|word)=", re.I)
LISTING_TITLE_RE = re.compile(
    r"(搜索结果|最新资讯|资讯列表|新闻列表|_第\d+页|第\d+页|专题|频道|首页|登录|注册|标签|列表|"
    r"search results|login|sign in|tag archives|category)", re.I)
TASK_STOPWORDS = {"公告", "新闻", "资讯", "最新", "相关", "公司", "股份", "有限公司", "the", "and", "of", "a"}
SITE_RE = re.compile(r"(?:^|\s)site:([A-Za-z0-9.\-]+)")
TICKER_CONTEXT = ("股", "证券", "代码", "SZ", "SH", "HK", "stock", "ticker", "沪", "深", "创业板", "科创板",
                  "港股", "A股")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class RelevanceDecision:
    relevant: bool
    target_match: bool
    task_match: bool
    content_form_ok: bool
    site_ok: bool
    matched_terms: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "relevant": self.relevant,
            "target_match": self.target_match,
            "task_match": self.task_match,
            "content_form_ok": self.content_form_ok,
            "site_ok": self.site_ok,
            "matched_terms": list(self.matched_terms),
            "reasons": list(self.reasons),
        }


def site_constraint(query: str) -> str | None:
    m = SITE_RE.search(query)
    return m.group(1).lower().rstrip(".") if m else None


def host_matches(url: str | None, site_host: str) -> bool:
    """Strict host comparison: equality or dotted subdomain."""
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    site_host = site_host.lower().rstrip(".")
    return host == site_host or host.endswith("." + site_host)


def query_terms(query: str) -> list[str]:
    """Task keywords: the query minus the ``site:`` clause, split by whitespace."""
    q = SITE_RE.sub(" ", query)
    terms = []
    for tok in q.split():
        tok = tok.strip("\"'()[]")
        if len(tok) < 2 and not CJK_RE.search(tok):
            continue
        terms.append(tok)
    return terms


def _contains_term(text: str, term: str) -> bool:
    if not term:
        return False
    t = term.casefold()
    if CJK_RE.search(term):
        return t in text
    # Latin terms must match on word boundaries so "CATL" doesn't hit "catalog".
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])", text) is not None


def identity_match(text: str, identity: TargetIdentity) -> list[str]:
    """Return the identity terms that genuinely match, excluding shortened forms."""
    lowered = text.casefold()
    matched: list[str] = []
    names = [identity.canonical_name, *identity.aliases]
    for name in names:
        if name and len(name.strip()) >= 2 and _contains_term(lowered, name.strip()):
            matched.append(name)
    for ticker in identity.tickers:
        tk = ticker.strip()
        if not tk or not _contains_term(lowered, tk):
            continue
        # Ticker only counts with context (code column, exchange suffix, "股" etc.).
        idx = lowered.find(tk.casefold())
        window = lowered[max(0, idx - 12): idx + len(tk) + 12]
        if any(c.casefold() in window for c in TICKER_CONTEXT) or \
                re.search(rf"[（(]\s*{re.escape(tk.casefold())}\s*[)）]", lowered):
            matched.append(ticker)
    return matched


def _task_terms_for(query: str, identity: TargetIdentity, family_focus: list[str] | None) -> list[str]:
    names = {identity.canonical_name.casefold(), *(a.casefold() for a in identity.aliases),
             *(t.casefold() for t in identity.tickers)}
    out = []
    for term in query_terms(query):
        if term.casefold() in names or term in TASK_STOPWORDS:
            continue
        out.append(term)
    for focus in family_focus or []:
        if focus and focus not in out:
            out.append(focus)
    return out


def task_match(text: str, query: str, identity: TargetIdentity, family_focus: list[str] | None) -> list[str]:
    terms = _task_terms_for(query, identity, family_focus)
    if not terms:
        return ["<identity-only-query>"]
    lowered = text.casefold()
    return [t for t in terms if _contains_term(lowered, t)]


def content_form_ok(url: str | None, title: str) -> tuple[bool, str | None]:
    if url:
        path = urlparse(url).path or "/"
        full = path + ("?" + urlparse(url).query if urlparse(url).query else "")
        if path in ("", "/"):
            return False, "homepage"
        if LISTING_PATH_RE.search(full) or SITE_SEARCH_RE.search(full):
            return False, "listing_path"
    if title and LISTING_TITLE_RE.search(title) and not re.search(r"\d{4}", title):
        return False, "listing_title"
    if title and len(title.strip()) < 6:
        return False, "short_title"
    return True, None


def judge(result: RawResult, query: str, identity: TargetIdentity,
          family_focus: list[str] | None = None, url: str | None = None) -> RelevanceDecision:
    text = f"{result.title} {result.snippet} {result.display_url or ''}"
    target_url = url or result.url
    matched = identity_match(text, identity)
    # Design 6.3: an official domain raises source trust (counts as target identity),
    # but a page there still needs task keywords and article form to be relevant.
    for dom in identity.official_domains:
        if target_url and host_matches(target_url, dom):
            matched.append(f"domain:{dom}")
            break
    tmatch = task_match(text, query, identity, family_focus)
    form_ok, form_reason = content_form_ok(target_url, result.title)
    site = site_constraint(query)
    site_ok = True if site is None else host_matches(target_url, site)
    reasons: list[str] = []
    if not matched:
        reasons.append("target_identity_missing")
    if not tmatch:
        reasons.append("task_keywords_missing")
    if not form_ok:
        reasons.append(form_reason or "content_form")
    if not site_ok:
        reasons.append("site_host_mismatch")
    return RelevanceDecision(
        relevant=bool(matched) and bool(tmatch) and form_ok and site_ok,
        target_match=bool(matched), task_match=bool(tmatch), content_form_ok=form_ok, site_ok=site_ok,
        matched_terms=matched + [t for t in tmatch if not t.startswith("<")], reasons=reasons,
    )


def page_relevance(decisions: list[RelevanceDecision]) -> dict[str, Any]:
    return {
        "relevant_count": sum(1 for d in decisions if d.relevant),
        "target_match_count": sum(1 for d in decisions if d.target_match),
        "task_match_count": sum(1 for d in decisions if d.task_match),
        "content_form_ok_count": sum(1 for d in decisions if d.content_form_ok),
        "site_ok_count": sum(1 for d in decisions if d.site_ok),
        "total": len(decisions),
    }
