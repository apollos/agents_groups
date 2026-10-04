"""Publication provenance must not be confused with business/event dates."""
import json
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from mic.publication_time import PublicationWindow, extract_publication, parse_published

NOW = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)
URL = "https://www.sohu.com/a/921322979_115863"


def extract(html, url=URL):
    return extract_publication(BeautifulSoup(html, "lxml"), url)


def test_sohu_header_outside_article_beats_business_dates():
    result = extract('<div class="text-title"><span id="news-time">2025-08-06 19:18</span></div>'
                     '<article><p>2026年9月30日交付。2025Q2销量150GWh。</p></article>')
    assert result["published_at"] == "2025-08-06T19:18:00+08:00"
    assert result["source"] == "sohu:#news-time"
    assert PublicationWindow(30, NOW).assess(result)["status"] == "outside_time_window"


def test_body_dates_searchish_dates_and_modified_dates_are_not_publication():
    result = extract('<head><meta property="article:modified_time" content="2026-10-03"></head>'
                     '<article><p>2026年10月3日，公司宣布2025Q2业绩。</p>'
                     '<time datetime="2026-10-03">交付日期</time></article>'
                     '<aside><time itemprop="datePublished">2026-10-03</time></aside>')
    assert result["status"] == "unknown"


def test_related_publication_field_inside_article_is_ignored():
    result = extract('<article><p>正文。</p><div class="related-news">'
                     '<time itemprop="datePublished" datetime="2026-10-03"></time></div></article>')
    assert result["status"] == "unknown"


def test_conflicting_explicit_dates_are_held_for_review():
    result = extract('<head><meta property="article:published_time" content="2025-08-06"></head>'
                     '<span id="news-time">2026-10-03</span><article>正文</article>')
    assert result["status"] == "conflict"
    assert not PublicationWindow(30, NOW).assess(result)["allowed"]


@pytest.mark.parametrize("identity,accepted", [(URL, True), ("https://other.test/article", False)])
def test_jsonld_article_must_match_page(identity, accepted):
    doc = {"@graph": [{"@type": "NewsArticle", "url": identity, "datePublished": "2026-10-03",
                       "dateModified": "2026-10-04"}]}
    result = extract('<script type="application/ld+json">' + json.dumps(doc) + '</script><article>正文</article>')
    assert (result["status"] == "known") is accepted


def test_jsonld_related_list_not_traversed():
    doc = {"@type": "ItemList", "itemListElement": [{"@type": "NewsArticle", "datePublished": "2026-10-03"}]}
    assert extract('<script type="application/ld+json">' + json.dumps(doc) + '</script>')["status"] == "unknown"


def test_generic_primary_article_header_and_hidden_fields():
    result = extract('<article><header><time datetime="2026-10-03T10:00:00+08:00"></time>'
                     '<time hidden datetime="2025-01-01"></time></header></article>')
    assert result["published_at"] == "2026-10-03T10:00:00+08:00"


@pytest.mark.parametrize("raw", ["2026Q2", "2026-09", "2026-02-30", "2天前", "昨天", "业务时间2026-10-03"])
def test_partial_or_ambiguous_dates_not_guessed(raw):
    assert parse_published(raw) is None


@pytest.mark.parametrize("raw,status", [
    ("2026-09-04T01:00:00Z", "in_window"),
    ("2026-09-04T00:59:59Z", "outside_time_window"),
    ("2026-09-04", "in_window"),
    ("2026-10-04T09:00:01+08:00", "future_publication_time"),
    ("2026-10-05", "future_publication_time"),
])
def test_window_boundaries_and_timezone(raw, status):
    date, precision = parse_published(raw)
    got = PublicationWindow(30, NOW).assess({"status": "known", "published_at": date.isoformat(), "precision": precision})
    assert got["status"] == status


@pytest.mark.parametrize("value", ["garbage", "0d", "-1d", 30, "30days"])
def test_bad_window_does_not_silently_disable_filter(value):
    with pytest.raises(ValueError):
        PublicationWindow.from_value(value)


def test_omitted_window_allows_historical_collection():
    assert PublicationWindow.from_value(None).assess({"status": "unknown"})["allowed"]
