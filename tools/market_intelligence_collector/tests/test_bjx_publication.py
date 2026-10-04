"""Polaris (bjx.com.cn) external headline bar → publication time binding.

Fixture provenance: synthetic HTML rebuilt from the DOM paths, classes and
visible text recorded in the local diagnostic
``collector-pilot/bjx-publication-diagnostic-20261004-203639-456436`` for
https://news.bjx.com.cn/html/20260915/1512829.shtml (HTTP 200, 2026-10-04).
It is not a saved copy of the live page. Expected values come from the
diagnostic's recorded headline span ("2026-09-15 11:47"), not from any model.
"""
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

from mic.publication_time import PublicationWindow, extract_publication

URL = "https://news.bjx.com.cn/html/20260915/1512829.shtml"
TITLE = "远景能源、宁德时代中标！河北100MW/400MWh储能系统采购中标公示"
HEADLINE = (
    '<div class="cc-headline"><div class="box">'
    f'<h1>{TITLE}</h1>'
    '<p><span>2026-09-15 11:47</span><span>来源：河北省招标投标公共服务平台</span>'
    '<span id="key_word">关键词：储能招标 储能系统</span></p>'
    '</div></div>')
BODY = (
    '<div class="cc-layout-3"><div class="box"><div class="center js-detail-center">'
    '<div id="article_cont"><div class="cc-article">'
    '<p>北极星储能网讯：2026年9月15日，河北任丘智弘100MW/400MWh新型技术路线磷酸铁锂电池+钠电池'
    '独立储能试点项目储能系统设备采购中标结果公示。</p>'
    '<p>第一中标候选人：远景能源有限公司，投标报价 0.6 元/Wh。</p>'
    '<p>第二中标候选人：宁德时代新能源科技股份有限公司。</p>'
    '</div></div>'
    # right rail: recommendation list with newer dates (li > a > div > small)
    '<div class="right"><div class="body"><ul class="active">'
    '<li><a href="/html/20260930/1515000.shtml"><div><small>2026-09-30</small></div></a></li>'
    '<li><a href="/html/20261001/1515100.shtml"><div><small>2026-10-01</small></div></a></li>'
    '<li><a href="/html/20261002/1515200.shtml"><p>2026-10-02</p></a></li>'
    '</ul></div></div>'
    '</div></div></div>')
PAGE = f'<html><head><title>{TITLE}-北极星储能网</title></head><body>{HEADLINE}{BODY}</body></html>'


def extract(html, url=URL):
    return extract_publication(BeautifulSoup(html, "lxml"), url)


def test_headline_span_bound_to_single_body_is_publication_time():
    result = extract(PAGE)
    assert result["status"] == "known"
    assert result["published_at"] == "2026-09-15T11:47:00+08:00"
    assert result["precision"] == "second"
    assert result["source"] == "bjx:cc-headline.publication-span"
    assert len(result["candidates"]) == 1
    window = PublicationWindow(30, datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc))
    assessment = window.assess(result)
    assert assessment["allowed"] is True and assessment["status"] == "in_window"


def test_naive_headline_time_is_interpreted_as_asia_shanghai():
    # 2026-09-15 11:47 +08:00 == 2026-09-15 03:47 UTC. A cutoff one minute
    # after that instant must reject it; one minute before must accept it.
    result = extract(PAGE)
    late = PublicationWindow(30, datetime(2026, 10, 15, 3, 48, tzinfo=timezone.utc))
    early = PublicationWindow(30, datetime(2026, 10, 15, 3, 46, tzinfo=timezone.utc))
    assert late.assess(result)["status"] == "outside_time_window"
    assert early.assess(result)["status"] == "in_window"


def test_body_event_date_and_recommendation_dates_are_never_used():
    # Remove the headline span: the body "2026年9月15日" and the rail's
    # 2026-09-30/10-01/10-02 must not become the publication time.
    html = PAGE.replace("<span>2026-09-15 11:47</span>", "")
    result = extract(html)
    assert result["status"] == "unknown"
    assert result["reason"] == "no_explicit_publication_date"


def test_url_date_segment_is_not_a_publication_time():
    # Same URL shape, but no headline at all: the /html/20260915/ segment
    # must not be promoted into a publication date.
    html = PAGE.replace(HEADLINE, "")
    assert extract(html)["status"] == "unknown"


@pytest.mark.parametrize("html,url", [
    # wrong host: template lookalike on another site
    (PAGE, "https://news.example.com/html/20260915/1512829.shtml"),
    # not an article URL on bjx (list page)
    (PAGE, "https://news.bjx.com.cn/zt/"),
    # two headline bars: ambiguous, fail closed
    (PAGE.replace(HEADLINE, HEADLINE + HEADLINE), URL),
    # headline present but no single body container (directory-like page)
    (PAGE.replace('<div class="cc-article">', '<div class="cc-list">'), URL),
    # two body containers
    (PAGE.replace('<div class="cc-article">', '<div class="cc-article"></div><div class="cc-article">'), URL),
    # hidden headline bar
    (PAGE.replace('<div class="cc-headline">', '<div class="cc-headline" hidden>'), URL),
    # headline inside a recommendation list item
    (PAGE.replace(HEADLINE, '<ul><li>' + HEADLINE + '</li></ul>'), URL),
    # headline title does not belong to this page (document title differs)
    (PAGE.replace(f'<title>{TITLE}-北极星储能网</title>', '<title>其他文章标题-北极星储能网</title>'), URL),
    # date is in the h1, not in the metadata paragraph
    (PAGE.replace('<span>2026-09-15 11:47</span>', '').replace(f'<h1>{TITLE}</h1>', f'<h1>{TITLE} 2026-09-15 11:47</h1>'), URL),
    # date sits in a paragraph *before* the h1 (not the metadata line under it)
    (PAGE.replace('<span>2026-09-15 11:47</span>', '').replace(f'<h1>{TITLE}</h1>', f'<p><span>2026-09-15 11:47</span></p><h1>{TITLE}</h1>'), URL),
])
def test_unbound_ambiguous_or_hidden_headlines_are_not_accepted(html, url):
    assert extract(html, url)["status"] == "unknown"


def test_conflicting_headline_spans_report_conflict_not_latest():
    html = PAGE.replace('<span>2026-09-15 11:47</span>',
                        '<span>2026-09-15 11:47</span><span>2026-09-30 09:00</span>')
    result = extract(html)
    assert result["status"] == "conflict"
    assert result["published_at"] is None


def test_same_day_duplicate_span_keeps_single_date():
    html = PAGE.replace('<span>2026-09-15 11:47</span>',
                        '<span>2026-09-15 11:47</span><span>2026-09-15</span>')
    result = extract(html)
    assert result["status"] == "known"
    assert result["published_at"] == "2026-09-15T11:47:00+08:00"


def test_article_scope_uses_headline_time_and_drops_rail_dates():
    from mic.article_scope import extract_article

    class _Reader:
        def _collect_image_urls(self, root):
            return []

    article = extract_article(PAGE, _Reader(), URL)
    assert article.report["status"] == "scoped"
    assert article.publish_time == "2026-09-15T11:47:00+08:00"
    assert article.report["selector"] == "#article_cont .cc-article"
    assert "中标候选人" in article.body
    assert "2026-09-30" not in article.body and "2026-10-02" not in article.body
