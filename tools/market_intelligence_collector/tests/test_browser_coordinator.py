"""T05/T07/T08/T20: SearchCoordinator decision table with fixture pages.

The loader is a test double; no browser or network is involved.
"""

from __future__ import annotations

from mic.browser.coordinator import LoadedPage, SearchCoordinator
from mic.browser.engines import build_engine
from tests.browser_doubles import FakeClock, FixtureLoader, fixture, make_context

Q = "宁德时代 中标"
P1 = fixture("bing_ok_page1.html")
P2 = fixture("bing_ok_page2.html")


def _coord(engines=("bing",), desired=2, sleeps=None, interval=3.0):
    sleeps = sleeps if sleeps is not None else []
    return SearchCoordinator([build_engine(e) for e in engines], desired_relevant_articles=desired,
                             results_per_page_cap=10, min_engine_interval_seconds=interval,
                             sleep=lambda s: sleeps.append(s))


def test_stops_when_desired_relevant_articles_reached_on_first_page():
    ctx = make_context()
    loader = FixtureLoader(default=P1)
    batch = _coord().run_query(Q, "orders_tender", "q1", 20, ctx, loader)
    assert batch.outcome == "completed"
    assert batch.stop_reason == "desired_relevant_reached"
    assert len(batch.page_attempts) == 1
    assert batch.quality["relevant_article_count"] == 2  # cninfo + finance.example (CATL)
    assert ctx.budget.used_summary()["search_page_attempts"] == 1
    hits = batch.hits
    assert all(h.discovery["engine"] == "bing" and h.discovery["page_index"] == 1 for h in hits)
    assert all(h.provider == "browser:bing" for h in hits)  # per-hit tag names the engine
    assert batch.provider == "browser"
    assert all(h.discovery["page_attempt_id"] for h in hits)
    assert [h.discovery["rank_in_page"] for h in hits] == [1, 3, 4, 5]


# --- T05: pagination, duplicate page, no new links -----------------------------------

def test_paginates_within_same_engine_until_desired_reached():
    ctx = make_context()
    loader = FixtureLoader(routes={"first=11": P2}, default=P1)
    batch = _coord(desired=3).run_query(Q, None, "q1", 20, ctx, loader)
    assert [p.page_index for p in batch.page_attempts] == [1, 2]
    assert loader.opened[1].startswith("https://www.bing.com/search?") and "first=11" in loader.opened[1]
    assert batch.stop_reason == "desired_relevant_reached"
    # duplicate cninfo link with utm_source is NOT a new hit
    urls = [h.url for h in batch.hits]
    assert len(urls) == len(set(urls))
    assert not any("utm_source" in u for u in urls)
    assert batch.page_attempts[1].quality["unique_new_count"] == 1


def test_duplicate_page_fingerprint_stops_engine_without_loop():
    ctx = make_context()
    loader = FixtureLoader(default=P1)  # page 2 returns identical page 1 content
    batch = _coord(desired=5).run_query(Q, None, "q1", 20, ctx, loader)
    assert [p.status for p in batch.page_attempts] == ["ok", "duplicate_page"]
    assert batch.stop_reason in ("duplicate_page", "engines_exhausted")
    assert len(loader.opened) == 2


def test_no_new_links_stops_pagination():
    ctx = make_context()
    page2_same_links = P2.replace("sse.example.cn/disclosure/2026/catl-energy-storage-award.html",
                                   "cninfo.com.cn/new/disclosure/detail?stockCode=300750&announcementId=123")
    loader = FixtureLoader(routes={"first=11": page2_same_links}, default=P1)
    batch = _coord(desired=5).run_query(Q, None, "q1", 20, ctx, loader)
    assert batch.page_attempts[1].quality["unique_new_count"] == 0
    assert len(loader.opened) == 2


def test_no_next_page_stops_engine():
    ctx = make_context()
    no_nav = P1.replace('class="sb_pagN" title="下一页"', 'class="disabled"')
    loader = FixtureLoader(default=no_nav)
    batch = _coord(desired=5).run_query(Q, None, "q1", 20, ctx, loader)
    assert len(batch.page_attempts) == 1
    assert batch.stop_reason in ("no_next_page", "engines_exhausted")


# --- T07: switching engines ---------------------------------------------------------------

def test_no_target_match_switches_to_next_engine_first_page():
    ctx = make_context()
    off_topic = P1.replace("宁德时代", "宁德市").replace("CATL", "XYZ").replace(
        "https://www.catl.com/", "https://www.other-corp.example/").replace(  # official domain is a target signal
        'value="宁德市 中标"', 'value="宁德时代 中标"')  # engine echoed our query; results are off-topic
    loader = FixtureLoader(routes={"host:www.bing.com": off_topic,
                                   "host:www.google.com": fixture("google_ok_unverified.html")})
    batch = _coord(engines=("bing", "google")).run_query(Q, None, "q1", 20, ctx, loader)
    assert [(p.engine, p.page_index) for p in batch.page_attempts] == [("bing", 1), ("google", 1)]
    assert batch.page_attempts[0].relevance == "low"
    assert batch.page_attempts[0].quality["target_match_count"] == 0
    assert batch.quality["off_topic_examples"]


def test_no_results_switches_engine_and_ends_empty_without_fallback():
    ctx = make_context()
    loader = FixtureLoader(default=fixture("bing_no_results.html"))
    batch = _coord(engines=("bing",)).run_query("xq9zk27 不存在公司", None, "q1", 20, ctx, loader)
    assert batch.outcome == "empty"
    assert batch.hits == []
    assert batch.page_attempts[0].status == "no_results"
    assert len(loader.opened) == 1


def test_parse_error_is_recorded_not_retried_same_page():
    ctx = make_context()
    loader = FixtureLoader(default=fixture("bing_unknown_dom.html"))
    batch = _coord(engines=("bing", "google")).run_query(Q, None, "q1", 20, ctx, loader)
    statuses = [(p.engine, p.status) for p in batch.page_attempts]
    assert statuses[0] == ("bing", "parse_error")
    assert statuses[1][0] == "google"  # moved on, did not reload bing page 1
    assert batch.outcome == "failed"


def test_query_mismatch_discards_hits_and_switches():
    ctx = make_context()
    mismatched = P1.replace('value="宁德时代 中标"', 'value="比亚迪 订单"')
    loader = FixtureLoader(default=mismatched)
    batch = _coord(engines=("bing",)).run_query(Q, None, "q1", 20, ctx, loader)
    assert batch.page_attempts[0].status == "query_mismatch"
    assert batch.hits == []


def test_network_failure_counts_a_page_and_moves_on():
    ctx = make_context()
    loader = FixtureLoader(routes={"host:www.bing.com": LoadedPage(status="timeout", error="TimeoutError"),
                                   "host:www.google.com": fixture("google_ok_unverified.html")})
    batch = _coord(engines=("bing", "google")).run_query(Q, None, "q1", 20, ctx, loader)
    assert batch.page_attempts[0].status == "timeout"
    assert ctx.budget.used_summary()["search_page_attempts"] == 2


# --- T08: three pages across engines; budget refuses before opening ------------------------

def test_three_page_budget_exhausted_across_engines_no_fourth_page():
    ctx = make_context(limits={"max_search_pages_per_query": 3, "max_engines_per_query": 2})
    # bing page1 has target hits + next page; page2 same links -> no new -> switch to google page1
    page2_same = P2.replace("sse.example.cn/disclosure/2026/catl-energy-storage-award.html",
                            "cninfo.com.cn/new/disclosure/detail?stockCode=300750&announcementId=123")
    loader = FixtureLoader(routes={"first=11": page2_same, "host:www.google.com": fixture("google_ok_unverified.html")},
                           default=P1)
    batch = _coord(engines=("bing", "google"), desired=10).run_query(Q, None, "q1", 50, ctx, loader)
    assert [(p.engine, p.page_index) for p in batch.page_attempts] == [("bing", 1), ("bing", 2), ("google", 1)]
    assert len(loader.opened) == 3
    # google page 1 repeats links already found -> no_new_links -> no engines left; never a 4th page
    assert batch.stop_reason in ("max_search_pages_per_query", "no_next_page", "engines_exhausted",
                                 "max_engines_per_query", "no_new_links")
    assert ctx.budget.used_summary()["search_page_attempts"] == 3
    assert ctx.budget.can_open_search_page("q1", "bing") == (False, "max_search_pages_per_query")


def test_captcha_consumes_page_attempt_and_switches():
    ctx = make_context(limits={"max_search_pages_per_query": 3})
    loader = FixtureLoader(routes={"host:www.bing.com": fixture("bing_captcha.html"),
                                   "host:www.google.com": fixture("google_ok_unverified.html")})
    batch = _coord(engines=("bing", "google")).run_query(Q, None, "q1", 20, ctx, loader)
    assert batch.page_attempts[0].status == "captcha"
    assert ctx.budget.used_summary()["search_page_attempts"] == 2
    assert batch.hits  # google rescued the query


def test_limit_truncates_returned_hits_without_extra_pages():
    ctx = make_context()
    loader = FixtureLoader(default=P1)
    batch = _coord().run_query(Q, None, "q1", 2, ctx, loader)
    assert len(batch.hits) == 2
    assert len(loader.opened) == 1


def test_run_page_cap_refuses_before_opening():
    ctx = make_context(limits={"max_search_pages_per_run": 1})
    loader = FixtureLoader(default=P1)
    _coord(desired=5).run_query(Q, None, "q1", 20, ctx, loader)
    batch2 = _coord(desired=5).run_query(Q + " 公告", None, "q2", 20, ctx, loader)
    assert batch2.outcome == "budget_exhausted"
    assert batch2.stop_reason == "max_search_pages_per_run"
    assert len(loader.opened) == 1


def test_min_engine_interval_sleeps_between_same_engine_pages():
    clock = FakeClock()
    ctx = make_context(clock=clock)
    sleeps: list[float] = []
    loader = FixtureLoader(routes={"first=11": P2}, default=P1, clock=clock, per_load_seconds=1.0)
    _coord(desired=3, sleeps=sleeps, interval=3.0).run_query(Q, None, "q1", 20, ctx, loader)
    assert sleeps and abs(sleeps[0] - 2.0) < 1e-6


# --- T20: authenticated recovery is bounded and counted ----------------------------------------

class _Store:
    def __init__(self, cred):
        self.cred = cred
        self.calls = 0

    @staticmethod
    def origin_of(url):
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    def credential_for(self, origin, exclude_versions=None):
        self.calls += 1
        return dict(self.cred) if self.cred else None


def test_captcha_then_authenticated_retry_once_counted_in_budget():
    from tests.browser_doubles import FakeBrowserSession

    store = _Store({"credential_id": "bing_user", "version": "v1", "cookies": [{"name": "a", "value": "secret"}]})
    ctx = make_context(session_store=store)
    fake = FakeBrowserSession()
    ctx.set_browser_factory(lambda c: fake)
    state = {"n": 0}

    def bing(url):
        state["n"] += 1
        return fixture("bing_captcha.html") if state["n"] == 1 else P1

    loader = FixtureLoader(routes={"host:www.bing.com": bing})
    batch = _coord(engines=("bing",)).run_query(Q, None, "q1", 20, ctx, loader)
    statuses = [(p.status, p.authenticated_retry) for p in batch.page_attempts]
    assert statuses == [("captcha", False), ("ok", True)]
    used = ctx.budget.used_summary()
    assert used["authenticated_retries"] == 1
    assert used["search_page_attempts"] == 2  # retry consumed a normal page attempt
    assert fake.cookies_added == [("bing_user", "v1", 1)]
    assert batch.page_attempts[1].auth_mode == "imported_cookie"


def test_authenticated_retry_not_repeated_when_still_blocked():
    from tests.browser_doubles import FakeBrowserSession

    store = _Store({"credential_id": "bing_user", "version": "v1", "cookies": [{"name": "a", "value": "x"}]})
    ctx = make_context(session_store=store)
    ctx.set_browser_factory(lambda c: FakeBrowserSession())
    loader = FixtureLoader(default=fixture("bing_captcha.html"))
    batch = _coord(engines=("bing",)).run_query(Q, None, "q1", 20, ctx, loader)
    assert [p.status for p in batch.page_attempts] == ["captcha", "captcha"]
    assert batch.outcome == "blocked"
    assert ctx.budget.used_summary()["authenticated_retries"] == 1
    assert len(loader.opened) == 2


def test_no_credential_means_no_retry():
    store = _Store(None)
    ctx = make_context(session_store=store)
    loader = FixtureLoader(default=fixture("bing_captcha.html"))
    batch = _coord(engines=("bing",)).run_query(Q, None, "q1", 20, ctx, loader)
    assert len(batch.page_attempts) == 1
    assert batch.page_attempts[0].diagnostics["auth_retry_skipped"] == "no_credential"
    assert ctx.budget.used_summary()["authenticated_retries"] == 0
