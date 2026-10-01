"""T06/T07: explainable relevance rules (identity vs association, site:, content form)."""

from __future__ import annotations

import pytest

from mic.browser.contracts import RawResult
from mic.browser.relevance import host_matches, identity_match, judge, site_constraint
from tests.browser_doubles import catl_identity


def _raw(title: str, url: str = "https://news.example.com/2026/09/catl-order.html", snippet: str = "") -> RawResult:
    return RawResult(title=title, raw_href=url, url=url, snippet=snippet, rank_in_page=1)


# --- T06 -----------------------------------------------------------------------------

def test_short_form_ningde_is_not_ningde_shidai():
    assert identity_match("宁德市某园区项目中标公告", catl_identity()) == []
    assert "宁德时代" in identity_match("宁德时代中标储能项目", catl_identity())


@pytest.mark.parametrize("text", ["特斯拉发布订单公告", "比亚迪 中标 储能项目", "动力电池 价格 上涨", "碳酸锂 招标"])
def test_association_terms_do_not_count_as_target(text):
    d = judge(_raw(text), "宁德时代 中标", catl_identity())
    assert d.target_match is False
    assert "target_identity_missing" in d.reasons


def test_ticker_needs_context():
    assert identity_match("订单号 300750 已发货", catl_identity()) == []
    assert "300750" in identity_match("宁德时代（300750）中标公告", catl_identity())
    assert "300750" in identity_match("300750.SZ 中标 储能项目", catl_identity())


def test_latin_alias_requires_word_boundary():
    assert identity_match("catalog of batteries", catl_identity()) == []
    assert "CATL" in identity_match("CATL wins storage order", catl_identity())


def test_relevant_article_requires_identity_task_and_form():
    d = judge(_raw("宁德时代：关于中标储能项目的公告", snippet="合同金额 12 亿元"), "宁德时代 中标", catl_identity())
    assert d.relevant is True and d.reasons == []
    assert "宁德时代" in d.matched_terms and "中标" in d.matched_terms


# --- T07 -----------------------------------------------------------------------------

def test_official_homepage_with_target_name_is_not_an_order_article():
    d = judge(_raw("宁德时代官网首页", url="https://www.catl.com/"), "宁德时代 中标", catl_identity())
    assert d.target_match is True
    assert d.relevant is False
    assert "homepage" in d.reasons and "task_keywords_missing" in d.reasons


def test_official_domain_counts_as_target_but_not_as_task_match():
    # Seen live on Bing: "企业简介" on catl.com carries no identity term in its title.
    d = judge(_raw("企业简介", url="https://www.catl.com/about/profile/"), "宁德时代 中标", catl_identity())
    assert d.target_match is True and "domain:catl.com" in d.matched_terms
    assert d.relevant is False and "task_keywords_missing" in d.reasons and "short_title" in d.reasons
    # look-alike hosts never count
    d2 = judge(_raw("企业简介", url="https://catl.com.evil.example/about/"), "宁德时代 中标", catl_identity())
    assert d2.target_match is False


@pytest.mark.parametrize("url", [
    "https://news.example.com/tag/catl",
    "https://news.example.com/search?q=catl",
    "https://news.example.com/list_1.html",
    "https://news.example.com/login",
    "https://www.baidu.com/s?wd=%E5%AE%81%E5%BE%B7&ie=utf-8",
    "https://news.example.com/s?keyword=catl&page=2",
])
def test_listing_tag_search_login_pages_are_not_article_candidates(url):
    d = judge(_raw("宁德时代 中标 新闻", url=url), "宁德时代 中标", catl_identity())
    assert d.content_form_ok is False


@pytest.mark.parametrize("url", [
    # Observed on the live Baidu SERP: Chinese article hosts use /s?<id> for articles.
    "https://baijiahao.baidu.com/s?id=1823388259107987743&wfr=spider&for=pc",
    "https://mp.weixin.qq.com/s?__biz=MjM5&mid=2650029438&idx=4&sn=abc",
])
def test_article_hosts_using_s_path_are_not_listings(url):
    d = judge(_raw("宁德时代 中标 江苏储能项目", url=url), "宁德时代 中标", catl_identity())
    assert d.content_form_ok is True and "listing_path" not in d.reasons


def test_site_constraint_strict_host_compare():
    assert site_constraint("site:cninfo.com.cn 宁德时代 中标") == "cninfo.com.cn"
    assert host_matches("https://www.cninfo.com.cn/x", "cninfo.com.cn") is True
    assert host_matches("https://cninfo.com.cn/x", "cninfo.com.cn") is True
    assert host_matches("https://cninfo.com.cn.evil.example/x", "cninfo.com.cn") is False
    assert host_matches("https://notcninfo.com.cn/x", "cninfo.com.cn") is False
    assert host_matches("https://catl.com.evil.example/x", "catl.com") is False


def test_site_mismatch_marks_result_not_relevant():
    d = judge(_raw("宁德时代 中标 公告", url="https://cninfo.com.cn.evil.example/a/1.html"),
              "site:cninfo.com.cn 宁德时代 中标", catl_identity())
    assert d.site_ok is False and d.relevant is False
    assert "site_host_mismatch" in d.reasons


def test_pending_redirect_is_not_judged_by_display_domain():
    raw = RawResult(title="宁德时代 中标 公告", raw_href="https://www.baidu.com/link?url=abc", url=None,
                    display_url="www.cninfo.com.cn", url_resolution="pending_redirect")
    d = judge(raw, "site:cninfo.com.cn 宁德时代 中标", catl_identity())
    assert d.site_ok is False  # unknown final host is never treated as confirmed
