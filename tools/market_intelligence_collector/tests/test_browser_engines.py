"""T02-T05 (parsing half): engine adapters on fixtures.

Bing, Baidu and Google selectors were observed on real windowed sessions on
this machine (2026-10); ``*_ok_page1.html`` are sanitized skeletons of those
DOMs. The ``*_unverified.html`` fixtures are the older synthetic layouts kept
as legacy-shape regression checks.
"""

from __future__ import annotations

import pytest

from mic.browser.contracts import NextPage
from mic.browser.engines import build_engine
from mic.browser.engines.base import query_match_status
from tests.browser_doubles import fixture

Q = "宁德时代 中标"
BING_URL = "https://www.bing.com/search?q=%E5%AE%81%E5%BE%B7%E6%97%B6%E4%BB%A3+%E4%B8%AD%E6%A0%87"


# --- T02: only organic results, traceable rank/source ---------------------------

def test_bing_parses_only_organic_cards_and_drops_bad_links():
    page = build_engine("bing").parse(fixture("bing_ok_page1.html"), BING_URL, Q, cap=10)
    assert page.status == "ok"
    urls = [r.url for r in page.results]
    assert "https://ads.example.com/promo" not in urls          # li.b_ad excluded
    assert not any("wikipedia" in u for u in urls)              # knowledge card excluded
    assert not any(u.startswith("javascript") for u in urls)    # javascript: dropped
    assert len(page.results) == 4
    # rank_in_page is the organic-card position (javascript card at 2 keeps its slot)
    assert [r.rank_in_page for r in page.results] == [1, 3, 4, 5]
    first = page.results[0]
    assert first.raw_href.startswith("https://www.cninfo.com.cn/")
    assert first.display_url == "www.cninfo.com.cn"
    assert first.result_kind == "organic" and first.url_resolution == "direct"
    assert page.results[-1].date_text == "2026-09-30"
    assert page.query_observed == Q
    assert page.diagnostics["skipped"]["non_http"] == 1


def test_highlight_tags_do_not_split_cjk_words_but_keep_latin_spaces():
    # Live Bing/Baidu titles wrap matched terms: <strong>宁德</strong>时代.
    html = ('<div id="b_results"><li class="b_algo"><h2><a href="https://news.example.com/2026/a.html">'
            '<strong>宁德</strong>时代 <strong>中标</strong> CATL <b>wins</b> order</a></h2>'
            '<div class="b_caption"><p><strong>宁德</strong>时代（300750）公告</p></div></li></div>'
            '<input id="sb_form_q" value="宁德时代 中标">')
    page = build_engine("bing").parse(html, BING_URL, Q, cap=10)
    assert page.results[0].title == "宁德时代 中标 CATL wins order"
    assert page.results[0].snippet == "宁德时代（300750）公告"
    assert page.query_observed == Q  # query echo comes from the input value, untouched


@pytest.mark.parametrize("first,expected", [(10, 2), (11, 2), (20, 3), (21, 3), (1, 1)])
def test_bing_next_page_index_from_first_param(first, expected):
    # cn.bing.com uses first=10 for page 2, www.bing.com uses first=11 (observed live 2026-10)
    html = ('<div id="b_results"><li class="b_algo"><h2><a href="https://news.example.com/2026/a.html">'
            '宁德时代 中标 公告</a></h2></li></div>'
            f'<nav aria-label="分页"><a class="sb_pagN" title="下一页" href="/search?q=x&first={first}">下一页</a></nav>')
    page = build_engine("bing").parse(html, BING_URL, Q, cap=10)
    assert page.next_page is not None and page.next_page.expected_page_index == expected


def test_bing_results_per_page_cap_is_honoured():
    page = build_engine("bing").parse(fixture("bing_ok_page1.html"), BING_URL, Q, cap=2)
    assert len(page.results) == 2


def test_bing_rejects_engine_internal_redirect_wrappers():
    html = fixture("bing_ok_page1.html").replace(
        "https://www.catl.com/", "https://www.bing.com/ck/a?!&&p=abc&u=a1aHR0cHM6Ly9leGFtcGxl")
    page = build_engine("bing").parse(html, BING_URL, Q, cap=10)
    assert page.status == "ok"
    assert all("bing.com" not in (r.url or "") for r in page.results)
    assert page.diagnostics["skipped"]["engine_internal"] == 1


# --- T03: no_results vs parse_error -----------------------------------------------

def test_bing_no_results_is_not_parse_error():
    page = build_engine("bing").parse(fixture("bing_no_results.html"),
                                      "https://www.bing.com/search?q=xq9zk27", "xq9zk27 不存在公司", cap=10)
    assert page.status == "no_results"
    assert page.error_code is None
    assert page.results == []


def test_bing_unknown_dom_is_explicit_parse_error_without_anchor_scan():
    page = build_engine("bing").parse(fixture("bing_unknown_dom.html"), BING_URL, Q, cap=10)
    assert page.status == "parse_error"
    assert page.error_code == "parse_error"
    assert page.results == []  # the stray <a> tags are never harvested


# --- T04: challenge / login / consent pages ---------------------------------------

def test_bing_captcha_page_detected():
    page = build_engine("bing").parse(fixture("bing_captcha.html"), BING_URL, Q, cap=10)
    assert page.status == "captcha"
    assert page.results == []


def test_bing_login_page_detected():
    page = build_engine("bing").parse(fixture("bing_login.html"), BING_URL, Q, cap=10)
    assert page.status == "login_required"


def test_google_consent_page_detected():
    page = build_engine("google").parse(fixture("google_consent.html"),
                                        "https://consent.google.com/m?continue=https://www.google.com/search",
                                        Q, cap=10)
    assert page.status == "consent_required"


def test_baidu_captcha_page_detected():
    page = build_engine("baidu").parse(fixture("baidu_captcha.html"),
                                       "https://wappass.baidu.com/static/captcha/tuxing.html", Q, cap=10)
    assert page.status == "captcha"


def test_title_ok_but_body_is_challenge_is_still_blocked():
    html = fixture("bing_captcha.html").replace("<title>安全验证</title>", "<title>宁德时代 中标 - 搜索</title>")
    page = build_engine("bing").parse(html, BING_URL, Q, cap=10)
    assert page.status == "captcha"


# --- T05: pagination controls --------------------------------------------------------

def test_bing_next_page_from_on_page_control():
    eng = build_engine("bing")
    page = eng.parse(fixture("bing_ok_page1.html"), BING_URL, Q, cap=10)
    assert page.next_page is not None
    assert page.next_page.kind == "link"
    assert page.next_page.href.startswith("https://www.bing.com/search?")
    assert page.next_page.expected_page_index == 2
    assert eng.validate_next_page(page.next_page, Q, 1, set()) is not None


@pytest.mark.parametrize("href,reason", [
    ("https://evil.example/search?q=%E5%AE%81%E5%BE%B7%E6%97%B6%E4%BB%A3+%E4%B8%AD%E6%A0%87&first=11", "off-host"),
    ("https://www.bing.com/search?q=other+query&first=11", "query identity changed"),
    ("javascript:next()", "javascript"),
])
def test_bing_next_page_validation_rejects_bad_controls(href, reason):
    eng = build_engine("bing")
    nxt = NextPage(kind="link", href=href, label="下一页", expected_page_index=2)
    assert eng.validate_next_page(nxt, Q, 1, set()) is None, reason


def test_bing_next_page_must_advance_page_index():
    eng = build_engine("bing")
    nxt = NextPage(kind="link", href=BING_URL + "&first=1", label="1", expected_page_index=1)
    assert eng.validate_next_page(nxt, Q, 1, set()) is None


def test_no_next_page_when_control_absent():
    page = build_engine("bing").parse(fixture("bing_no_results.html"), BING_URL, "x", cap=10)
    assert page.next_page is None


# --- query echo ---------------------------------------------------------------------

def test_query_match_status_normalises_width_and_case():
    assert query_match_status("CATL 中标", "catl　中标") == "exact"
    assert query_match_status("宁德时代 中标", "宁德时代 中标 公告") == "corrected"
    assert query_match_status("宁德时代 中标", "比亚迪 订单") == "mismatch"
    assert query_match_status("x", None) == "unknown"


# --- Google: legacy synthetic fixture (div.g cards, direct hrefs) ---------------------

def test_google_legacy_layout_excludes_modules_and_keeps_direct_urls():
    eng = build_engine("google")
    page = eng.parse(fixture("google_ok_unverified.html"), "https://www.google.com/search?q=x", Q, cap=10)
    assert page.status == "ok"
    assert page.diagnostics["skipped"]["excluded_module"] == 3  # ad, knowledge panel, people-also-ask
    urls = [(r.url, r.url_resolution) for r in page.results]
    assert urls == [("https://www.cninfo.com.cn/new/disclosure/detail?stockCode=300750&announcementId=123", "direct"),
                    ("https://finance.example.com/a/20260930/catl-order.html", "direct")]
    assert page.next_page is not None and page.next_page.expected_page_index == 2


# --- Google: real DOM skeleton (verified 2026-10-02) ----------------------------------

GOOGLE_Q = "宁德时代 中标 储能"
GOOGLE_URL = "https://www.google.com/search?q=%E5%AE%81%E5%BE%B7%E6%97%B6%E4%BB%A3+%E4%B8%AD%E6%A0%87+%E5%82%A8%E8%83%BD&hl=zh-CN"


def test_google_adapter_is_verified_and_marks_goto_wrappers_pending():
    eng = build_engine("google")
    assert eng.verified is True and "unverified" not in eng.adapter_version
    page = eng.parse(fixture("google_ok_page1.html"), GOOGLE_URL, GOOGLE_Q, cap=10)
    assert page.status == "ok"
    assert page.query_observed == GOOGLE_Q
    assert page.diagnostics["cards"] == 5  # 4 organic + the people-also-ask inner card
    assert page.diagnostics["skipped"]["excluded_module"] == 1  # ...which is excluded; #tads sits outside #rso
    assert [(r.url, r.url_resolution) for r in page.results] == [
        (None, "pending_redirect"),
        (None, "pending_redirect"),
        (None, "pending_redirect"),
        ("https://www.cninfo.com.cn/new/disclosure/detail?stockCode=300750&announcementId=123", "direct"),
    ]
    for r in page.results[:3]:
        # The wrapper is kept absolute so the reader can follow it; the token is opaque.
        assert r.raw_href.startswith("https://www.google.com/goto?url=CAES"), r.raw_href
    assert [r.rank_in_page for r in page.results] == [1, 2, 3, 4]
    titles = [r.title for r in page.results]
    assert "宁德时代储能业务占比多少？" not in titles and "广告" not in " ".join(titles)


def test_google_real_dom_splits_date_prefix_display_and_snippet():
    page = build_engine("google").parse(fixture("google_ok_page1.html"), GOOGLE_URL, GOOGLE_Q, cap=10)
    first, second, third, legacy = page.results
    assert first.title == "储能系统"
    assert first.date_text is None and first.display_url == "https://www.catl.com › ess"
    assert first.snippet.startswith("宁德时代凭借电芯良好的一致性")  # <em> boundary: CJK joined, no space
    assert second.date_text == "2025年2月8日" and second.display_url == "https://cn.solarbe.com › news"
    assert second.snippet.startswith("项目配置包括1套储能容量为100MW/200MWh") and "—" not in second.snippet[:3]
    assert third.date_text == "2026年9月11日" and third.display_url.startswith("https://finance.sina.com.cn")
    assert legacy.date_text == "3 days ago" and legacy.snippet.startswith("宁德时代新能源科技股份有限公司")
    assert legacy.display_url == "www.cninfo.com.cn › new › disclosure"


def test_google_real_dom_next_page_control_validates():
    eng = build_engine("google")
    page = eng.parse(fixture("google_ok_page1.html"), GOOGLE_URL, GOOGLE_Q, cap=10)
    assert page.next_page is not None
    assert page.next_page.href.startswith("https://www.google.com/search?q=")
    assert "start=10" in page.next_page.href and page.next_page.expected_page_index == 2
    assert page.next_page.label == "下一页"
    assert eng.validate_next_page(page.next_page, GOOGLE_Q, 1, set()) is not None
    assert eng.validate_next_page(page.next_page, GOOGLE_Q, 2, set()) is None  # does not advance


def test_google_cap_limits_results_but_keeps_rank():
    page = build_engine("google").parse(fixture("google_ok_page1.html"), GOOGLE_URL, GOOGLE_Q, cap=2)
    assert [r.title for r in page.results] == ["储能系统", "宁德时代再次中标储能项目"]
    assert [r.rank_in_page for r in page.results] == [1, 2]


def test_google_pending_results_feed_hits_with_absolute_wrapper_url():
    """Coordinator contract: a pending hit's url is the absolute wrapper the reader will follow."""
    from mic.browser.coordinator import SearchCoordinator
    page = build_engine("google").parse(fixture("google_ok_page1.html"), GOOGLE_URL, GOOGLE_Q, cap=10)
    assert SearchCoordinator._discovery_key(page.results[0]) == "pending:https://www.google.com/goto?url=CAESUQH_TOKEN_1"


def test_baidu_legacy_layout_marks_redirects_pending_without_mu():
    """Legacy ``.c-abstract`` layout without ``mu``: the redirect wrapper stays pending."""
    eng = build_engine("baidu")
    page = eng.parse(fixture("baidu_ok_unverified.html"), "https://www.baidu.com/s?wd=x", Q, cap=10)
    assert page.status == "ok"
    assert len(page.results) == 2
    pending = page.results[0]
    assert pending.url is None and pending.url_resolution == "pending_redirect"
    assert pending.raw_href.startswith("https://www.baidu.com/link?url=")
    assert pending.display_url == "www.cninfo.com.cn"  # displayed host is NOT promoted to the real URL
    assert page.results[1].url == "https://finance.example.com/a/20260930/catl-order.html"
    assert page.results[1].url_resolution == "direct"
    assert page.next_page is not None and page.next_page.expected_page_index == 2


# --- Baidu: real DOM skeleton (verified 2026-10-02) -----------------------------------

BAIDU_Q = "宁德时代 中标 储能"
BAIDU_URL = "https://www.baidu.com/s?wd=%E5%AE%81%E5%BE%B7%E6%97%B6%E4%BB%A3%20%E4%B8%AD%E6%A0%87%20%E5%82%A8%E8%83%BD&ie=utf-8"


def test_baidu_adapter_is_verified_and_uses_declared_destination():
    eng = build_engine("baidu")
    assert eng.verified is True and "unverified" not in eng.adapter_version
    page = eng.parse(fixture("baidu_ok_page1.html"), BAIDU_URL, BAIDU_Q, cap=10)
    assert page.status == "ok"
    assert page.query_observed == BAIDU_Q
    assert page.diagnostics["skipped"]["excluded_module"] == 2  # 百科 + 寻标宝 result-op modules
    urls = [(r.url, r.url_resolution) for r in page.results]
    assert urls == [
        ("https://www.catl.com/ess/", "engine_declared"),
        ("https://baijiahao.example/s?id=1000000000000000001&wfr=spider&for=pc", "engine_declared"),
        ("https://m.bjx.example/mnews/20241205/1415043.shtml", "engine_declared"),
        (None, "pending_redirect"),  # no mu attribute
        (None, "pending_redirect"),  # mu is itself a baidu.com/link wrapper
    ]
    for r in page.results:
        assert r.raw_href.startswith("http://www.baidu.com/link?url="), r.raw_href
    assert [r.rank_in_page for r in page.results] == [1, 2, 3, 4, 5]


def test_baidu_real_dom_splits_date_prefix_source_and_snippet():
    page = build_engine("baidu").parse(fixture("baidu_ok_page1.html"), BAIDU_URL, BAIDU_Q, cap=10)
    first, second, third = page.results[:3]
    assert first.date_text is None and first.display_url == "宁德时代·CATL"
    assert first.snippet.startswith("储能系统为输配电侧")
    assert second.date_text == "2025年2月7日" and second.display_url == "新浪财经"
    assert second.snippet.startswith("宁德时代中标国信江苏常州100MW/200MWh 储能系统采购")
    assert third.date_text == "2024年12月5日" and third.display_url == "北极星电力网"
    assert third.title.startswith("19.36MWh! 宁德时代中标国宁新储2024年第二批储能设备")  # <em> split, CJK joined
    assert "2024年12月5日" not in third.snippet and third.snippet.startswith("12月4日,国宁新储")
    legacy = page.results[4]
    assert legacy.display_url == "example.org/notice" and legacy.snippet == "legacy abstract layout."


def test_baidu_real_dom_next_page_control_validates():
    eng = build_engine("baidu")
    page = eng.parse(fixture("baidu_ok_page1.html"), BAIDU_URL, BAIDU_Q, cap=10)
    assert page.next_page is not None
    assert page.next_page.href.startswith("https://www.baidu.com/s?wd=")
    assert "pn=10" in page.next_page.href and page.next_page.expected_page_index == 2
    assert "下一页" in page.next_page.label
    assert eng.validate_next_page(page.next_page, BAIDU_Q, 1, set()) is not None


def test_baidu_cap_limits_results_but_keeps_rank():
    page = build_engine("baidu").parse(fixture("baidu_ok_page1.html"), BAIDU_URL, BAIDU_Q, cap=2)
    assert [r.url for r in page.results] == ["https://www.catl.com/ess/",
                                             "https://baijiahao.example/s?id=1000000000000000001&wfr=spider&for=pc"]


def test_baidu_wappass_redirect_reports_no_observed_query():
    page = build_engine("baidu").parse("<html><body><input id='kw' value='token'/></body></html>",
                                       "https://wappass.baidu.com/static/captcha/tuxing_v2.html?x=1", BAIDU_Q, cap=10)
    assert page.status == "captcha" and page.query_observed is None
    assert page.diagnostics == {"challenge": "baidu_wappass"}


def test_google_sorry_page_reports_no_observed_query():
    html = "<html><body><form action='index'><input name='q' value='CHALLENGE_TOKEN'/></form></body></html>"
    page = build_engine("google").parse(html, "https://www.google.com/sorry/index?continue=x", Q, cap=10)
    assert page.status == "captcha" and page.query_observed is None
