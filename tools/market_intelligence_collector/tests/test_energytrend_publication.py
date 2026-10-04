"""Primary header shape observed at /news/20260915-148614.html on 2026-10-04."""
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from mic.publication_time import PublicationWindow, extract_publication, parse_published

URL = "https://www.energytrend.cn/news/20260915-148614.html"
HEADER = ('<header class="entry-header"><table><tr><td class="maintitle">'
          '<h1 class="entry-title">项目中标公示</h1></td></tr><tr><td>'
          '<span class="body newsdate">2026 年 09 月 15 日 14:41 </span>'
          '</td></tr></table></header>')
ARTICLE = ('<article id="post-148614"><div class="content">' + HEADER
           + '<div class="entry-content">2025年8月4日开工。</div></div></article>')


def extract(html, url=URL):
    return extract_publication(BeautifulSoup(html, "lxml"), url)


@pytest.mark.parametrize("raw", ["2026 年 09 月 15 日 14:41", "2026\u00a0年 09 月 15 日 14:41",
                                "2026年9月15日14:41", "2026年9月15日"])
def test_spaced_chinese_date(raw):
    parsed, precision = parse_published(raw)
    assert (parsed.year, parsed.month, parsed.day) == (2026, 9, 15)
    assert precision == ("second" if "14:41" in raw else "day")


def test_primary_header_recognized_and_passes_window():
    result = extract(ARTICLE)
    assert result["published_at"] == "2026-09-15T14:41:00+08:00"
    assert result["source"] == "energytrend:entry-header.newsdate"
    assert PublicationWindow(30, datetime(2026, 10, 4, tzinfo=timezone.utc)).assess(result)["allowed"]


@pytest.mark.parametrize("html,url", [
    (ARTICLE, "https://unrelated.example/news/20260915-148614.html"),
    (ARTICLE, "https://www.energytrend.cn/news/20260915-999.html"),
    (ARTICLE, "https://www.energytrend.cn/news/"),
    (ARTICLE.replace('id="post-148614"', 'id="post-999"'), URL),
    (ARTICLE + ARTICLE, URL),
    ('<article id="post-148614"><div class="entry-content"><span class="newsdate">2026-09-15</span></div></article>', URL),
    ('<aside class="related">' + ARTICLE + '</aside>', URL),
    (ARTICLE.replace('<header ', '<header hidden '), URL),
    (ARTICLE.replace('class="body newsdate"', 'class="body newsdate related"'), URL),
    (ARTICLE.replace('class="entry-title"', 'class="card-title"'), URL),
])
def test_unbound_related_or_hidden_dates_not_accepted(html, url):
    assert extract(html, url)["status"] == "unknown"


def test_conflicting_metadata_still_blocks():
    result = extract('<head><meta property="article:published_time" content="2025-08-06"></head>' + ARTICLE)
    assert result["status"] == "conflict"


def test_body_prose_with_date_still_not_a_timestamp():
    assert parse_published("2026 年 09 月 15 日交付项目") is None
