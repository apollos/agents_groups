"""Article publication evidence and a fail-closed collection time window.

Publication dates come from explicit publisher metadata/header fields, never
from body prose, a URL, dateModified, or a search engine's date guess. Naive
timestamps/date-only values use Asia/Shanghai (MIC's Chinese source workflow).
This checks source publication recency, not the date of every historical fact.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

from mic.utils import parse_time_window_days

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
META_NAMES = {"article:published_time", "og:published_time", "publishdate",
              "pubdate", "publication_date", "parsely-pub-date", "publishtime", "publish_time"}
ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "Report", "ScholarlyArticle"}
EXCLUDED = re.compile(r"related|recommend|sidebar|comment|footer|navigation", re.I)


def utcnow():
    return datetime.now(timezone.utc)


def parse_published(value):
    if not isinstance(value, str) or len(value) > 160:
        return None
    text = value.strip().replace("年", "-").replace("月", "-").replace("日", " ")
    text = text.replace("/", "-")
    # Publisher headers can render "2026 年 09 月 15 日 14:41" (including NBSP).
    # Normalize separators only; the full-field match below still rejects prose.
    text = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "-", text).strip()
    match = re.fullmatch(
        r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T\s]+(\d{1,2}):(\d{2})"
        r"(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?", text)
    if not match:
        return None
    year, month, day, hour, minute, second, offset = match.groups()
    try:
        stamp = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        if hour is not None:
            stamp += f"T{int(hour):02d}:{minute}:{second or '00'}{offset or ''}"
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed, "day" if hour is None else "second"
    except ValueError:
        return None


def _page_key(url):
    try:
        p = urlsplit(url)
        return (p.hostname or "").lower(), p.path.rstrip("/"), p.query
    except ValueError:
        return None


def _visible_article_field(node):
    for parent in [node, *node.parents]:
        if getattr(parent, "attrs", None) is None:
            continue
        signature = " ".join([str(parent.get("id", "")), *parent.get("class", [])])
        if (parent.name in {"aside", "nav", "footer"} or EXCLUDED.search(signature)
                or parent.has_attr("hidden") or parent.get("aria-hidden") == "true"):
            return False
    return True


def sina_bulletin_fields(soup, page_url):
    """Bind Sina's legacy bulletin table to this page's announcement id.

    Observed on 2026-10-04: the header's download link carries the same id as
    the URL; the labelled date is outside #content. A generic #content selector
    would also accept navigation/other sites, so share this binding with the
    body reader. Never derive publication time from the download URL.
    """
    page = urlsplit(page_url)
    if ((page.hostname or "").lower() not in {"vip.stock.finance.sina.com.cn", "money.finance.sina.com.cn"}
            or page.path != "/corp/view/vCB_AllBulletinDetail.php"):
        return None
    ids = parse_qs(page.query).get("id", [])
    if len(ids) != 1 or not re.fullmatch(r"\d+", ids[0]):
        return None
    tables = soup.select("table#allbulletin")
    if len(tables) != 1:
        return None
    table = tables[0]

    def visible(node):
        return _visible_article_field(node) and not any(
            re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
                      str(p.get("style", "")), re.I)
            for p in [node, *node.parents] if getattr(p, "attrs", None) is not None)

    heads = table.select(":scope > thead > tr > th")
    bodies = table.select("#content")
    if (len(heads) != 1 or len(bodies) != 1 or not visible(heads[0])
            or not visible(bodies[0]) or bodies[0].find_parent("table") is not table):
        return None
    bound = False
    for link in heads[0].select("a[href]"):
        target = urlsplit(urljoin(page_url, link.get("href")))
        if (visible(link) and target.scheme in {"http", "https"}
                and (target.hostname or "").lower() == "file.finance.sina.com.cn"
                and target.path.rsplit("/", 1)[-1].lower() == ids[0] + ".pdf"):
            bound = True
    if not bound:
        return None
    title = " ".join(str(c).strip() for c in heads[0].children
                     if getattr(c, "name", None) is None and str(c).strip())
    if len(title) < 6:
        return None
    dates = []
    for field in table.select(":scope > tbody > tr > td.graybgH2"):
        if visible(field) and field.find_parent("table") is table:
            match = re.fullmatch(r"公告日期\s*[:：]\s*(.+)", field.get_text(" ", strip=True))
            if match:
                dates.append(match.group(1))
    return {"body": bodies[0], "title": title, "dates": dates}


def bjx_headline_fields(soup, page_url):
    """Bind the Polaris (bjx.com.cn) external headline bar to this page's body.

    Observed on 2026-10-04 at news.bjx.com.cn/html/20260915/1512829.shtml:
    the article body is `#article_cont .cc-article`, but the title and the
    publication time live in a separate top-level `div.cc-headline` block:
    `.cc-headline > .box > h1` followed by a sibling `p` whose first `span` is
    "2026-09-15 11:47" and whose next span is "来源：...". The body itself opens
    with an event date ("2026年9月15日") and the right rail carries dozens of
    recommendation dates in `li > a > small`. Only the headline span bound to
    the single h1 + single body container counts. The URL date segment is used
    purely to recognise an article URL, never as the publication time. The page
    gives no timezone: naive values are interpreted as Asia/Shanghai downstream.
    """
    page = urlsplit(page_url)
    host = (page.hostname or "").lower()
    if not (host == "bjx.com.cn" or host.endswith(".bjx.com.cn")):
        return None
    if not re.fullmatch(r"/html/\d{8}/\d+\.shtml", page.path or ""):
        return None
    headlines = [h for h in soup.select("div.cc-headline")
                 if _visible_article_field(h)
                 and not any(p.name in {"article", "aside", "li", "a"} for p in h.parents)]
    bodies = [b for b in soup.select("#article_cont .cc-article") if _visible_article_field(b)]
    if len(headlines) != 1 or len(bodies) != 1:
        return None
    headline, body = headlines[0], bodies[0]
    if headline in body.parents or body in headline.parents:
        return None
    titles = headline.select("h1")
    if len(titles) != 1 or not _visible_article_field(titles[0]):
        return None
    title = " ".join(titles[0].get_text(" ", strip=True).split())
    if len(title) < 6:
        return None
    if soup.title:
        page_title = " ".join(soup.title.get_text(" ", strip=True).split())
        if page_title and title not in page_title and page_title not in title:
            return None
    dates = []
    for meta in titles[0].find_next_siblings("p"):
        if not _visible_article_field(meta):
            continue
        for span in meta.find_all("span", recursive=False):
            text = span.get_text(" ", strip=True)
            if _visible_article_field(span) and parse_published(text):
                dates.append(text)
    if not dates:
        return None
    return {"body": body, "title": title, "dates": dates}


def extract_publication(soup, page_url=""):
    candidates = []

    def add(raw, source):
        parsed = parse_published(raw)
        if parsed and len(candidates) < 30:
            date, precision = parsed
            candidates.append({"published_at": date.isoformat(), "precision": precision,
                               "source": source})

    # Restrict document metadata to <head>; card metadata in body is not primary.
    if soup.head:
        for tag in soup.head.find_all("meta"):
            name = str(tag.get("property") or tag.get("name") or "").lower()
            if name in META_NAMES:
                add(tag.get("content"), "meta:" + name)

    # Only top-level/@graph article nodes: never descend into related ItemLists.
    articles = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if len(raw) > 1_000_000:
            continue
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError):
            continue
        nodes = doc if isinstance(doc, list) else [doc]
        nodes = [child for node in nodes for child in (
            [node, *(node.get("@graph", []) if isinstance(node.get("@graph"), list) else [])]
            if isinstance(node, dict) else [])]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and any(t in ARTICLE_TYPES for t in types if isinstance(t, str)):
                articles.append(node)
    for node in articles:
        identity = node.get("mainEntityOfPage") or node.get("url") or node.get("@id")
        if isinstance(identity, dict):
            identity = identity.get("@id") or identity.get("url")
        # An identified article must match this fetched page. Anonymous article
        # metadata is accepted only if it is the sole article node on the page.
        if identity:
            if not isinstance(identity, str) or not page_url:
                continue
            try:
                identity = urljoin(page_url, identity)
            except ValueError:
                continue
            if _page_key(identity) is None or _page_key(identity) != _page_key(page_url):
                continue
        elif len(articles) != 1:
            continue
        add(node.get("datePublished"), "jsonld:datePublished")

    roots = soup.select("article")
    outer = [r for r in roots if r.find_parent("article") is None and _visible_article_field(r)]
    if len(outer) == 1:
        for tag in outer[0].select('[itemprop="datePublished"], time[pubdate], header time[datetime]'):
            if _visible_article_field(tag):
                add(tag.get("datetime") or tag.get("content") or tag.get_text(" ", strip=True),
                    "article:publication_field")
    # Sohu's primary publication header is outside <article> and must be read
    # before body scoping removes surrounding chrome.
    if (urlsplit(page_url).hostname or "").lower() in {"www.sohu.com", "sohu.com", "m.sohu.com"}:
        for tag in soup.select("#news-time"):
            if _visible_article_field(tag):
                add(tag.get_text(" ", strip=True), "sohu:#news-time")

    bulletin = sina_bulletin_fields(soup, page_url)
    if bulletin:
        for raw in bulletin["dates"]:
            add(raw, "sina:allbulletin.announcement-date")

    headline = bjx_headline_fields(soup, page_url)
    if headline:
        for raw in headline["dates"]:
            add(raw, "bjx:cc-headline.publication-span")

    # EnergyTrend puts the primary date in a non-semantic span inside its
    # WordPress article header. Bind the article id to the fetched URL; never
    # accept newsdate spans from recommendations, archive cards, or the body.
    page = urlsplit(page_url)
    if (page.hostname or "").lower() in {"energytrend.cn", "www.energytrend.cn"}:
        match = re.fullmatch(r"/news/\d{8}-(\d+)\.html", page.path)
        if match:
            primary = soup.find_all("article", id="post-" + match.group(1))
            if (len(primary) == 1 and primary[0].find_parent("article") is None
                    and _visible_article_field(primary[0])):
                headers = primary[0].select(":scope > div.content > header.entry-header")
                if len(headers) == 1 and len(headers[0].select("h1.entry-title")) == 1:
                    fields = [tag for tag in headers[0].select("span.newsdate")
                              if _visible_article_field(tag)]
                    if len(fields) == 1:
                        add(fields[0].get_text(" ", strip=True), "energytrend:entry-header.newsdate")

    if not candidates:
        return {"status": "unknown", "published_at": None,
                "reason": "no_explicit_publication_date"}
    dates = {datetime.fromisoformat(c["published_at"]).astimezone(LOCAL_TZ).date() for c in candidates}
    if len(dates) > 1:
        return {"status": "conflict", "published_at": None,
                "reason": "conflicting_publication_dates", "candidates": candidates}
    # Prefer a precise time over a date-only value, with the earliest precise
    # time if the publisher emitted several same-day timestamps.
    candidates.sort(key=lambda c: (c["precision"] != "second",
                                   datetime.fromisoformat(c["published_at"])))
    return {"status": "known", **candidates[0], "candidates": candidates}


@dataclass(frozen=True)
class PublicationWindow:
    days: int | None
    reference: datetime

    @classmethod
    def from_value(cls, value):
        days = parse_time_window_days(value) if isinstance(value, str) else None
        if value not in (None, "") and (days is None or days <= 0):
            raise ValueError("time_window must be positive, e.g. 7d, 30d, 12w; omit for historical collection")
        reference = utcnow()
        if days:
            try:
                reference - timedelta(days=days)
            except OverflowError as exc:
                raise ValueError("time_window exceeds supported date range") from exc
        return cls(days, reference)

    def describe(self):
        return {"enabled": self.days is not None, "days": self.days,
                "reference_time": self.reference.isoformat(),
                "cutoff": (self.reference - timedelta(days=self.days)).isoformat() if self.days else None,
                "basis": "verified_article_publication", "naive_date_timezone": "Asia/Shanghai"}

    def assess(self, publication):
        if self.days is None:
            return {"status": "disabled", "allowed": True}
        if publication.get("status") != "known":
            return {"status": "publication_time_unverified", "allowed": False,
                    "reason": publication.get("reason", "no_explicit_publication_date")}
        parsed = parse_published(publication.get("published_at"))
        if not parsed:
            return {"status": "publication_time_unverified", "allowed": False,
                    "reason": "invalid_publication_date"}
        date, _ = parsed
        cutoff = self.reference - timedelta(days=self.days)
        if publication.get("precision") == "day":
            old = date.astimezone(LOCAL_TZ).date() < cutoff.astimezone(LOCAL_TZ).date()
            future = date.astimezone(LOCAL_TZ).date() > self.reference.astimezone(LOCAL_TZ).date()
        else:
            old, future = date < cutoff, date > self.reference
        status = "outside_time_window" if old else "future_publication_time" if future else "in_window"
        return {"status": status, "allowed": status == "in_window",
                "published_at": date.isoformat(), "source": publication.get("source")}
