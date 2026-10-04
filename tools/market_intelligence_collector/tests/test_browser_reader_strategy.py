"""T10 / T11 / T20 / T22: fetch strategy, browser rescue, strict scope kept, auth retry contract.

HTTP and browser transports are test doubles (synthetic DOMs). Nothing here is
live-site evidence.
"""

from __future__ import annotations

import pytest

from mic.browser.contracts import FetchResult
from mic.config import MICConfig
from mic.profile import TargetProfile
from mic.reader import LinkReader
from tests.browser_doubles import FakeBrowserSession, make_context

PARAGRAPHS = [
    "北极星储能网讯：2026年9月15日，某独立储能试点项目储能系统设备采购中标结果公示。",
    "一标段为磷酸铁锂电池储能系统，远景能源中标价为19461.6万元，合单价0.518元/Wh；",
    "二标段为钠电池储能系统，宁德时代中标价为4141.622万元，合单价1.035元/Wh。",
]
BODY = "".join(f"<p>{p}</p>" for p in PARAGRAPHS)
RELATED = '<div class="related"><h3>相关阅读</h3><p>海辰储能4MWh钠电池系统，开启储能新阶段。</p></div>'
GOOD_HTML = ('<html><head><title>宁德时代中标新闻</title><meta property="article:published_time" content="2026-09-15"></head><body><div class="news-content">'
             '<div id="article_cont"><div class="cc-article">' + BODY + '</div></div>' + RELATED +
             '</div></body></html>')
UNSCOPED_HTML = '<html><head><title>宁德时代中标新闻</title></head><body><div id="article_cont">' + BODY + RELATED + '</div></body></html>'
CAPTCHA_HTML = '<html><head><title>安全验证</title></head><body><p>请输入验证码</p></body></html>'
URL = "https://news.example.com/2026/09/15/catl-award.html"
PROFILE = TargetProfile(target_id="test", type="company", canonical_name="宁德时代")


def _reader(http_pages: dict[str, FetchResult | None], site_rules: dict | None = None,
            default_mode: str = "http_then_browser") -> LinkReader:
    cfg = MICConfig(raw={
        "output_schema": {"limits": {"strict_evidence_review": True}},
        "access_profiles": {"default": {"timeout_seconds": 5},
                            "browser_fetch": {"default_mode": default_mode,
                                              "fallback_reasons": ["anti_bot_page", "rendering_required"],
                                              "site_rules": site_rules or {}}},
    })

    class R(LinkReader):
        http_calls: list[str] = []

        def _fetch_http_result(self, url):
            R.http_calls.append(url)
            res = http_pages.get(url)
            if res is None:
                return FetchResult(transport="http", requested_url=url, blocked_reason="network_error",
                                   counted_as="http_read_attempts")
            return res

    R.http_calls = []
    return R(cfg)


def _http(html: str, url: str = URL, status: int = 200) -> FetchResult:
    if status != 200:
        return FetchResult(transport="http", requested_url=url, final_url=url, http_status=status,
                           blocked_reason="http_status", counted_as="http_read_attempts")
    return FetchResult(transport="http", requested_url=url, final_url=url, http_status=200,
                       content_type="text/html", html=html, counted_as="http_read_attempts")


def _ctx(session: FakeBrowserSession, **kw):
    ctx = make_context(**kw)
    ctx.set_browser_factory(lambda c: session)
    return ctx


# --- T10: http -> browser same URL, bounded attempts ----------------------------------------

def test_http_success_never_opens_browser():
    session = FakeBrowserSession(pages={URL: GOOD_HTML})
    reader = _reader({URL: _http(GOOD_HTML)})
    ctx = _ctx(session)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "read" and res.transport == "http"
    assert session.started is False
    used = ctx.budget.used_summary()
    assert used == {**used, "http_read_attempts": 1, "browser_read_attempts": 0}
    assert res.fetch_diagnostics["strategy"] == "http_then_browser"
    assert [a["transport"] for a in res.fetch_diagnostics["attempts"]] == ["http"]
    # strict scope preserved: three body paragraphs, no "相关阅读"
    texts = [p.text for p in res.passages if p.passage_id != "title"]
    assert texts == PARAGRAPHS
    assert res.body_scope["status"] == "scoped"


def test_anti_bot_http_page_falls_back_to_browser_once():
    session = FakeBrowserSession(pages={URL: GOOD_HTML})
    reader = _reader({URL: _http(CAPTCHA_HTML)})
    ctx = _ctx(session)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "read" and res.transport == "browser"
    assert session.navigations == [URL]
    used = ctx.budget.used_summary()
    assert (used["http_read_attempts"], used["browser_read_attempts"]) == (1, 1)
    attempts = res.fetch_diagnostics["attempts"]
    assert [a["transport"] for a in attempts] == ["http", "browser"]
    assert "html" not in attempts[1] and "content" not in attempts[1]  # diagnostics never carry page content


def test_browser_read_budget_refuses_before_navigation():
    session = FakeBrowserSession(pages={URL: GOOD_HTML})
    reader = _reader({URL: _http(CAPTCHA_HTML)})
    ctx = _ctx(session, limits={"max_browser_read_attempts": 0})
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "failed" and res.failure_reason == "anti_bot_page"
    assert session.navigations == []
    assert res.fetch_diagnostics["attempts"][-1]["skipped"] == "browser_read_attempts"


@pytest.mark.parametrize("status", [404, 410])
def test_http_404_410_do_not_trigger_browser(status):
    session = FakeBrowserSession(pages={URL: GOOD_HTML})
    reader = _reader({URL: _http("", status=status)})
    ctx = _ctx(session)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "failed"
    assert res.http_status == status
    assert session.navigations == []
    assert ctx.budget.used_summary()["browser_read_attempts"] == 0


def test_browser_first_site_rule_then_http_only_on_transport_failure():
    host = "news.example.com"
    session = FakeBrowserSession(pages={URL: {"status": "timeout", "error": "TimeoutError"}})
    reader = _reader({URL: _http(GOOD_HTML)}, site_rules={host: {"mode": "browser_first"}})
    ctx = _ctx(session)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "read" and res.transport == "http"
    assert [a["transport"] for a in res.fetch_diagnostics["attempts"]] == ["browser", "http"]
    assert res.fetch_diagnostics["strategy"] == "browser_first"


def test_browser_unavailable_does_not_mask_as_success():
    from mic.browser.session import BrowserUnavailable

    session = FakeBrowserSession(fail_start=BrowserUnavailable("gui_unavailable", "no DISPLAY"))
    reader = _reader({URL: _http(CAPTCHA_HTML)})
    ctx = _ctx(session)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "failed"
    attempts = res.fetch_diagnostics["attempts"]
    assert attempts[1]["transport"] == "browser" and attempts[1]["blocked_reason"] == "gui_unavailable"


def test_pending_redirect_final_url_is_reported():
    pending = "https://www.baidu.com/link?url=abc"
    session = FakeBrowserSession(pages={pending: {"html": GOOD_HTML, "final_url": URL}})
    reader = _reader({pending: _http(CAPTCHA_HTML, url=pending)})
    ctx = _ctx(session)
    res = reader.read("l1", pending, PROFILE, context=ctx)
    assert res.read_status == "read"
    assert res.final_url == URL  # real host learned from the budgeted navigation only


def test_http_redirect_reports_real_final_url(monkeypatch):
    """Review R6: the plain HTTP transport followed redirects but recorded the requested URL,
    so a Google ``/goto`` wrapper stayed the source of an article living on the news site."""
    import mic.reader as reader_mod

    wrapper = "https://www.google.com/goto?url=OPAQUE"

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}
        url = URL  # httpx reports the post-redirect URL here
        text = GOOD_HTML
        content = GOOD_HTML.encode("utf-8")

    monkeypatch.setattr(reader_mod.httpx, "get", lambda *a, **kw: _Resp())
    cfg = MICConfig(raw={"output_schema": {"limits": {"strict_evidence_review": True}},
                         "access_profiles": {"default": {"timeout_seconds": 5},
                                             "browser_fetch": {"default_mode": "http_only"}}})
    reader = LinkReader(cfg)
    ctx = _ctx(FakeBrowserSession())
    res = reader.read("l1", wrapper, PROFILE, context=ctx)
    assert res.read_status == "read" and res.transport == "http"
    assert res.final_url == URL  # real host learned from the redirect, not the wrapper
    assert res.fetch_diagnostics["attempts"][0]["final_url"] == URL
    assert reader._fetch(wrapper)[3] == URL


# --- T11: scope rules kept; no full-page fallback ----------------------------------------------

def test_scope_unresolved_is_final_without_site_rule():
    session = FakeBrowserSession(pages={URL: GOOD_HTML})
    reader = _reader({URL: _http(UNSCOPED_HTML)})
    ctx = _ctx(session)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "failed" and res.failure_reason == "article_scope_unresolved"
    assert session.navigations == []
    assert res.passages == []


def test_scope_unresolved_with_allow_scope_retry_tries_browser_once():
    session = FakeBrowserSession(pages={URL: UNSCOPED_HTML})  # browser still cannot scope -> fail
    reader = _reader({URL: _http(UNSCOPED_HTML)},
                     site_rules={"news.example.com": {"mode": "http_then_browser", "allow_scope_retry": True}})
    ctx = _ctx(session)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "failed" and res.failure_reason == "article_scope_unresolved"
    assert session.navigations == [URL]
    assert res.passages == []  # never falls back to whole-page text
    assert ctx.budget.used_summary()["browser_read_attempts"] == 1


def test_legacy_read_without_context_unchanged():
    reader = _reader({URL: _http(GOOD_HTML)})
    res = reader.read("l1", URL, PROFILE)
    assert res.read_status == "read" and res.transport == "http"
    assert res.body_scope["status"] == "scoped"


# --- T20 / T22: authenticated retry re-validates body; cookies never relax thresholds ------------

class _Store:
    def __init__(self, cred):
        self.cred = cred

    @staticmethod
    def origin_of(url):
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    def credential_for(self, origin, exclude_versions=None):
        return dict(self.cred) if self.cred else None


def test_browser_captcha_then_authenticated_retry_goes_through_full_body_checks():
    state = {"n": 0}

    class Session(FakeBrowserSession):
        def navigate(self, page, url, timeout_seconds):
            state["n"] += 1
            self.pages = {URL: CAPTCHA_HTML if state["n"] == 1 else UNSCOPED_HTML}
            return super().navigate(page, url, timeout_seconds)

    session = Session()
    store = _Store({"credential_id": "news_user", "version": "v7", "cookies": [{"name": "s", "value": "SECRET"}]})
    reader = _reader({URL: _http(CAPTCHA_HTML)})
    ctx = _ctx(session, session_store=store)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    # Authenticated page loaded but body scope still unresolved -> still a failure.
    assert res.read_status == "failed" and res.failure_reason == "article_scope_unresolved"
    assert session.cookies_added == [("news_user", "v7", 1)]
    used = ctx.budget.used_summary()
    assert used["authenticated_retries"] == 1 and used["browser_read_attempts"] == 2
    attempts = res.fetch_diagnostics["attempts"]
    assert attempts[-1]["authenticated_retry"] is True and attempts[-1]["auth_mode"] == "imported_cookie"
    assert "SECRET" not in repr(res.fetch_diagnostics)  # cookie values never in diagnostics


def test_authenticated_retry_success_is_counted_and_scoped():
    state = {"n": 0}

    class Session(FakeBrowserSession):
        def navigate(self, page, url, timeout_seconds):
            state["n"] += 1
            self.pages = {URL: CAPTCHA_HTML if state["n"] == 1 else GOOD_HTML}
            return super().navigate(page, url, timeout_seconds)

    store = _Store({"credential_id": "news_user", "version": "v7", "cookies": [{"name": "s", "value": "SECRET"}]})
    reader = _reader({URL: _http(CAPTCHA_HTML)})
    ctx = _ctx(Session(), session_store=store)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "read" and res.transport == "browser"
    assert [p.text for p in res.passages if p.passage_id != "title"] == PARAGRAPHS
    assert ctx.budget.used_summary()["authenticated_retries"] == 1


def test_no_second_authenticated_retry_per_origin():
    class Session(FakeBrowserSession):
        def navigate(self, page, url, timeout_seconds):
            self.pages = {URL: CAPTCHA_HTML}
            return super().navigate(page, url, timeout_seconds)

    store = _Store({"credential_id": "news_user", "version": "v7", "cookies": [{"name": "s", "value": "x"}]})
    reader = _reader({URL: _http(CAPTCHA_HTML)})
    session = Session()
    ctx = _ctx(session, session_store=store)
    res = reader.read("l1", URL, PROFILE, context=ctx)
    assert res.read_status == "failed"
    assert len(session.navigations) == 2  # anonymous + one authenticated
    assert ctx.budget.used_summary()["authenticated_retries"] == 1
