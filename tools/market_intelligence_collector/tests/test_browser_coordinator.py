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


# --- Review R9 / design 10.1: interactive mode holds the page and re-observes -----------------

def _interactive(ctx, clock: FakeClock, wait_seconds: float = 60):
    ctx.interaction_mode = "interactive"
    ctx.browser_runtime["human_wait_seconds"] = wait_seconds
    sleeps: list[float] = []

    def sleep(s):
        sleeps.append(s)
        clock.advance(s)

    coord = SearchCoordinator([build_engine("bing")], desired_relevant_articles=2, results_per_page_cap=10,
                              min_engine_interval_seconds=0, sleep=sleep)
    return coord, sleeps


def test_interactive_captcha_recovers_after_operator_action_without_new_page_attempt():
    clock = FakeClock()
    ctx = make_context(clock=clock)
    # Operator solves the challenge: the 3rd re-observation of the same tab shows results.
    loader = FixtureLoader(default=fixture("bing_captcha.html"),
                           reobserve=lambda url, n: P1 if n >= 3 else None, clock=clock)
    coord, sleeps = _interactive(ctx, clock)
    batch = coord.run_query(Q, None, "q1", 20, ctx, loader)
    page = batch.page_attempts[0]
    assert page.status == "ok" and batch.hits and batch.outcome == "completed"
    hw = page.diagnostics["human_wait"]
    assert hw["blocked_status"] == "captcha" and hw["outcome"] == "recovered" and hw["polls"] == 3
    assert hw["observed_statuses"][-1] == "ok" and hw["observed_statuses"][0] == "captcha"
    # Re-observation never navigates: one page attempt, one URL opened, tab held then released.
    assert ctx.budget.used_summary()["search_page_attempts"] == 1
    assert loader.opened == [loader.holds[0]] and loader.released == loader.holds
    assert len(loader.reobserved) == 3
    # Waiting counted in run time (clock advanced by the sleeps).
    assert abs(hw["waited_seconds"] - sum(sleeps)) < 1e-6 and hw["waited_seconds"] > 0
    assert ctx.budget.elapsed_seconds() >= hw["waited_seconds"]


def test_interactive_wait_times_out_and_is_bounded_by_human_wait_seconds():
    clock = FakeClock()
    ctx = make_context(clock=clock)
    loader = FixtureLoader(default=fixture("bing_captcha.html"), clock=clock)  # never recovers
    coord, sleeps = _interactive(ctx, clock, wait_seconds=10)
    batch = coord.run_query(Q, None, "q1", 20, ctx, loader)
    page = batch.page_attempts[0]
    assert page.status == "captcha" and batch.outcome == "blocked"
    hw = page.diagnostics["human_wait"]
    assert hw["outcome"] == "timeout" and abs(hw["allowance_seconds"] - 10) < 1e-6
    assert abs(sum(sleeps) - 10) < 1e-6 and hw["polls"] == 5
    assert loader.released == loader.holds and len(loader.holds) == 1
    assert ctx.budget.used_summary()["search_page_attempts"] == 1


def test_interactive_wait_is_clamped_to_remaining_run_time_and_honours_cancel():
    clock = FakeClock()
    ctx = make_context(clock=clock, deadline_seconds=7)
    loader = FixtureLoader(default=fixture("bing_captcha.html"), clock=clock)
    coord, sleeps = _interactive(ctx, clock, wait_seconds=60)
    batch = coord.run_query(Q, None, "q1", 20, ctx, loader)
    hw = batch.page_attempts[0].diagnostics["human_wait"]
    assert abs(hw["allowance_seconds"] - 7) < 1e-6 and sum(sleeps) <= 7 + 1e-6
    # Cancel during the wait stops polling promptly.
    clock2 = FakeClock()
    ctx2 = make_context(clock=clock2)
    flag = {"cancel": False}

    def operator_cancels(url, n):
        if n >= 2:
            flag["cancel"] = True  # external cancel signal arrives during the wait
        return None

    loader2 = FixtureLoader(default=fixture("bing_captcha.html"), clock=clock2, reobserve=operator_cancels)
    coord2, sleeps2 = _interactive(ctx2, clock2, wait_seconds=60)
    ctx2.budget.cancel_check = lambda: flag["cancel"]
    batch2 = coord2.run_query(Q, None, "q1", 20, ctx2, loader2)
    hw2 = batch2.page_attempts[0].diagnostics["human_wait"]
    assert hw2["outcome"] == "cancelled" and hw2["polls"] == 2 and sum(sleeps2) <= 4 + 1e-6
    assert batch2.outcome in ("blocked", "cancelled") and loader2.released == loader2.holds


def test_unattended_mode_never_holds_or_waits():
    clock = FakeClock()
    ctx = make_context(clock=clock)
    loader = FixtureLoader(default=fixture("bing_captcha.html"), clock=clock)
    sleeps: list[float] = []
    batch = _coord(engines=("bing",), sleeps=sleeps).run_query(Q, None, "q1", 20, ctx, loader)
    assert batch.page_attempts[0].status == "captcha"
    assert "human_wait" not in batch.page_attempts[0].diagnostics
    assert loader.holds == [] and loader.reobserved == [] and sleeps == []


def test_interactive_ok_page_is_released_without_waiting():
    clock = FakeClock()
    ctx = make_context(clock=clock)
    loader = FixtureLoader(default=P1, clock=clock)
    coord, sleeps = _interactive(ctx, clock)
    batch = coord.run_query(Q, None, "q1", 20, ctx, loader)
    assert batch.hits and sleeps == [] and loader.released == loader.holds and loader.holds


def test_interactive_consent_page_waits_then_moves_on_without_fabricating_consent():
    clock = FakeClock()
    ctx = make_context(clock=clock)
    loader = FixtureLoader(default=LoadedPage(status="navigated", final_url="https://www.bing.com/search?q=x",
                                              html="<html><body><div id='bnp_container'>consent</div></body></html>",
                                              http_status=200), clock=clock)
    coord, sleeps = _interactive(ctx, clock, wait_seconds=4)
    batch = coord.run_query(Q, None, "q1", 20, ctx, loader)
    page = batch.page_attempts[0]
    if page.status == "consent_required":  # depends on the bing adapter's consent detection
        assert page.diagnostics["human_wait"]["outcome"] == "timeout" and sum(sleeps) <= 4 + 1e-6
    assert loader.released == loader.holds


def test_browser_page_loader_hold_reobserves_same_tab_and_releases_once():
    """The Playwright-backed loader keeps the tab open only when asked; reobserve never navigates."""
    from contextlib import contextmanager

    from mic.browser.coordinator import BrowserPageLoader

    class _Page:
        def __init__(self):
            self.url = "https://www.bing.com/search?q=x"
            self.contents = ["<html>captcha</html>", "<html>results</html>"]
            self.closed = False
            self.waits = 0

        def wait_for_selector(self, *a, **k):
            self.waits += 1

        def content(self):
            return self.contents.pop(0) if len(self.contents) > 1 else self.contents[0]

        def is_closed(self):
            return self.closed

    class _Session:
        def __init__(self):
            self.pages: list[_Page] = []
            self.closed: list[_Page] = []
            self.navigations = 0

        @contextmanager
        def page(self):
            p = _Page()
            self.pages.append(p)
            try:
                yield p
            finally:
                p.closed = True
                self.closed.append(p)

        def navigate(self, page, url, timeout):
            self.navigations += 1
            return {"status": "navigated", "http_status": 200, "final_url": url, "elapsed_ms": 1}

    engine = build_engine("bing")
    # Default: tab closed before returning, nothing held.
    s = _Session()
    loaded = BrowserPageLoader(s).load("https://www.bing.com/search?q=x", engine, 5.0)
    assert loaded.status == "navigated" and loaded.held is None and s.closed == s.pages
    # hold=True: tab stays open, reobserve reads the current DOM without navigating.
    s = _Session()
    loaded = BrowserPageLoader(s).load("https://www.bing.com/search?q=x", engine, 5.0, hold=True)
    assert loaded.held is not None and s.closed == [] and loaded.html == "<html>captcha</html>"
    again = loaded.held.reobserve(engine, 2.0)
    assert again.status == "navigated" and again.html == "<html>results</html>" and s.navigations == 1
    loaded.held.release()
    loaded.held.release()  # idempotent
    assert s.closed == s.pages and len(s.closed) == 1
    assert loaded.held.reobserve(engine, 1.0).status == "browser_closed"
    # Navigation failure with hold=True still closes the tab and holds nothing.
    s = _Session()
    s.navigate = lambda page, url, timeout: {"status": "timeout", "error": "t", "elapsed_ms": 5}
    loaded = BrowserPageLoader(s).load("https://www.bing.com/search?q=x", engine, 5.0, hold=True)
    assert loaded.status == "timeout" and loaded.held is None and s.closed == s.pages
