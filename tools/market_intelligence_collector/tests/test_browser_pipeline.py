"""T10 / T12 / T13 / T14 / T16 / T17: Pipeline on the browser route with test doubles.

Search pages come from HTML fixtures, body reads from a fake browser session,
models from MIC mock mode. No real browser, network or model is involved.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from mic.browser.contracts import FetchResult
from mic.pipeline import Pipeline
from mic.reader import LinkReader
from tests.browser_doubles import (
    FakeBrowserSession,
    FakeClock,
    FixtureLoader,
    browser_config,
    fixture,
)
from tests.test_browser_reader_strategy import CAPTCHA_HTML, GOOD_HTML

P1 = fixture("bing_ok_page1.html")
TASK = {"task_type": "company_update", "time_window": "30d", "focus": ["operating_update"],
        "budget_profile": {"max_queries": 2, "max_links_to_read": 2, "max_model_calls": 3}}


def _echo(html: str):
    """Make the fixture echo whatever query the URL carries (so query_match is exact)."""
    def render(url: str) -> str:
        q = parse_qs(urlparse(url).query).get("q", [""])[0]
        return html.replace('value="宁德时代 中标"', f'value="{q}"')
    return render


@pytest.fixture
def strict_cfg():
    cfg = browser_config()
    cfg.raw.setdefault("output_schema", {}).setdefault("limits", {})["strict_evidence_review"] = True
    return cfg


def _pipeline(cfg, loader: FixtureLoader, monkeypatch, http_html: str = CAPTCHA_HTML):
    pipe = Pipeline(cfg)
    assert pipe.search.browser_backed is True
    pipe.search._loader_factory = lambda ctx: loader

    def fake_http(self, url):  # HTTP transport double: returns a challenge page -> browser fallback
        return FetchResult(transport="http", requested_url=url, final_url=url, http_status=200,
                           content_type="text/html", html=http_html, counted_as="http_read_attempts")

    monkeypatch.setattr(LinkReader, "_fetch_http_result", fake_http)
    return pipe


def _variant(n: int) -> str:
    """Distinct article bodies so within-run content-hash dedup does not kick in."""
    return GOOD_HTML.replace("中标结果公示", f"中标结果公示（第{n}号）")


def _session_for_hits() -> FakeBrowserSession:
    pages = {
        "https://www.cninfo.com.cn/new/disclosure/detail?stockCode=300750&announcementId=123": _variant(1),
        "https://finance.example.com/a/20260930/catl-order.html": _variant(2),
        "https://www.catl.com/": _variant(3),
        "https://news.example.com/2026/ningde-city-tender": _variant(4),
    }
    return FakeBrowserSession(pages=pages)


def test_browser_run_records_attempts_discovery_and_diagnostics(strict_cfg, monkeypatch):
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    session = _session_for_hits()
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "deadline_seconds": 300, "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    assert diag["browser_run"] is True and diag["execution_status"] == "completed"
    assert diag["search_status"] == "ok"
    assert diag["budget_used"]["queries_attempted"] == 2
    assert 1 <= diag["budget_used"]["search_page_attempts"] <= 6
    assert diag["budget_used"]["links_selected_for_read"] <= 2
    assert diag["reuse_analysis"] is False
    assert diag["cleanup"]["cleanup"] == "complete"
    assert session.closed is True
    # search_page_attempt rows: all finished, none left attempting
    rows = pipe.repo.search_page_attempts_for_run(report["search_run_id"])
    assert rows and all(r["state"] == "finished" for r in rows)
    assert all(r["engine"] == "bing" and r["adapter_version"] == "bing-dom-20261001" for r in rows)
    assert diag["page_attempts"]["search_page_attempts"] == len(rows) == diag["budget_used"]["search_page_attempts"]
    assert diag["page_attempts"]["interrupted"] == 0
    # source_link.metadata.discovery is persisted
    links = pipe.repo.source_links_for_run(report["search_run_id"], limit=50)
    assert links
    disc = links[0]["metadata"]["discovery"]
    assert disc["engine"] == "bing" and disc["page_attempt_id"] in {r["id"] for r in rows}
    assert "relevance" in disc
    # legacy summary keys preserved + new counters
    s = report["summary"]
    assert "queries_executed" in s and s["queries_executed"] == s["queries_completed"] == 2
    assert s["search_page_attempts"] == diag["budget_used"]["search_page_attempts"]
    # one budget view: planner-counted model calls are mirrored into budget_used
    assert diag["budget_used"]["model_calls"] == s["model_calls"]
    assert "structured_outputs" in report and "all_events" in report
    with pipe.repo.db.session() as sess:
        from mic.store import models as m
        providers = {row.provider for row in sess.query(m.SourceLink).filter_by(search_run_id=report["search_run_id"])}
    assert providers == {"browser:bing"}


def test_browser_read_diagnostics_persisted_with_body_scope(strict_cfg, monkeypatch):
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    session = _session_for_hits()
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    if diag["read_status"] == "not_run":
        pytest.skip("triage selected no links to read under mock scoring")
    assert diag["budget_used"]["browser_read_attempts"] >= 1
    links = pipe.repo.source_links_for_run(report["search_run_id"], limit=50)
    read_links = [link for link in links if link["read_status"] == "read"]
    assert read_links
    attempts = pipe.repo.read_attempts_for_link(read_links[0]["source_link_id"])
    d = attempts[-1]["diagnostics"]
    assert d["transport"] == "browser"
    assert d["body_scope"]["status"] == "scoped"
    assert d["parser_version"] == "article_scope_v1"
    assert [a["transport"] for a in d["fetch"]["attempts"]] == ["http", "browser"]
    assert d["selected_passage_ids"]


# --- T12: no cross-run reuse on browser runs ------------------------------------------------------

def test_browser_runs_never_reuse_prior_analysis(strict_cfg, monkeypatch):
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    r1 = pipe.collect_intelligence("company_300750", TASK, run_options={
        "browser_factory": lambda ctx: _session_for_hits()})
    r2 = pipe.collect_intelligence("company_300750", TASK, run_options={
        "browser_factory": lambda ctx: _session_for_hits()})
    assert r1["summary"]["links_read"] >= 1
    assert r1["summary"]["cached_or_reused_results"] == 0
    assert r2["summary"]["cached_or_reused_results"] == 0  # same URLs, same bodies: still re-read, no clone
    assert r2["summary"]["links_read"] == r1["summary"]["links_read"]
    assert r2["collection_diagnostics"]["budget_used"]["browser_read_attempts"] == \
        r1["collection_diagnostics"]["budget_used"]["browser_read_attempts"]


# --- T14: cancel / deadline ----------------------------------------------------------------------

def test_cancel_check_stops_run_without_further_requests(strict_cfg, monkeypatch):
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    session = _session_for_hits()
    flag = {"cancel": False}

    class CancelAfterFirstLoad(FixtureLoader):
        def load(self, url, engine, timeout_seconds):
            flag["cancel"] = True
            return super().load(url, engine, timeout_seconds)

    loader2 = CancelAfterFirstLoad(default=_echo(P1))
    pipe.search._loader_factory = lambda ctx: loader2
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "cancel_check": lambda: flag["cancel"], "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    assert diag["execution_status"] == "cancelled"
    assert diag["usable"] is False
    assert len(loader2.opened) == 1           # no second query page
    assert session.navigations == []          # no body reads after cancel
    assert session.started is False           # never started, nothing left open
    assert diag["cleanup"]["cleanup"] in ("not_needed", "complete")
    run = pipe.repo.get_search_run(report["search_run_id"]) if hasattr(pipe.repo, "get_search_run") else None
    if run is not None:
        assert run["status"] == "cancelled"


def test_deadline_expiry_reports_timed_out(strict_cfg, monkeypatch):
    clock = FakeClock()
    loader = FixtureLoader(default=_echo(P1), clock=clock, per_load_seconds=400)  # one page blows the deadline
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    session = _session_for_hits()
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "deadline_seconds": 300, "clock": clock, "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    assert diag["execution_status"] == "timed_out"
    assert diag["usable"] is False
    assert len(loader.opened) == 1
    assert session.navigations == []


def test_environment_fault_in_search_fails_fast(strict_cfg, monkeypatch):
    from mic.browser.session import BrowserUnavailable

    class Raising:
        opened: list = []

        def load(self, url, engine, timeout_seconds):
            raise BrowserUnavailable("gui_unavailable", "no DISPLAY")

    pipe = _pipeline(strict_cfg, Raising(), monkeypatch)

    def factory(ctx):
        raise BrowserUnavailable("gui_unavailable", "no DISPLAY")

    pipe.search._loader_factory = factory
    report = pipe.collect_intelligence("company_300750", TASK, run_options={})
    diag = report["collection_diagnostics"]
    assert diag["execution_status"] == "failed"
    assert diag["search_status"] == "failed"
    assert diag["stop_reason"] == "gui_unavailable"
    assert diag["usable"] is False
    assert diag["budget_used"]["queries_attempted"] == 1  # stopped after the first environment fault


# --- T17: interrupted attempts are marked, never completed ---------------------------------------

def test_interrupted_page_attempts_marked(strict_cfg):
    pipe = Pipeline(strict_cfg)
    run_id = pipe.repo.create_search_run("company_300750", TASK, TASK["budget_profile"], "v", "v")
    aid = pipe.repo.start_search_page_attempt(run_id, {
        "page_attempt_id": "spa_x", "query_id": None, "engine": "bing", "adapter_version": "bing-dom-20261001",
        "page_index": 1, "query_requested": "q", "requested_url": "https://www.bing.com/search?q=q"})
    assert pipe.repo.mark_interrupted_page_attempts(run_id) == 1
    rows = pipe.repo.search_page_attempts_for_run(run_id)
    assert rows[0]["id"] == aid and rows[0]["state"] == "interrupted"
    assert rows[0]["result_count"] in (None, 0)


# --- T13 / gateway: counting at actual send with retries disabled -----------------------------------

def test_gateway_requests_counted_at_send_including_failures():
    from mic.budget import DEFAULT_LIMITS, RunBudget
    from mic.modeling.adapter import ModelAdapter

    class Boom(Exception):
        pass

    class FakeCompletions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            raise Boom("gateway 502")

    class FakeClient:
        def __init__(self):
            self.chat = type("Chat", (), {})()
            self.chat.completions = FakeCompletions()

    adapter = ModelAdapter(model_config_id="m", provider="p", provider_type="openclaw_gateway",
                           endpoint="http://127.0.0.1:1/v1", model="x", api_key="k", allow_mock=False)
    client = FakeClient()
    adapter._client = client
    budget = RunBudget(limits={**DEFAULT_LIMITS, "max_gateway_requests": 1})
    adapter.set_budget(budget)
    r1 = adapter._api_complete([{"role": "user", "content": "hi"}], 10, 2)
    assert r1.status == "request_failed" and r1.error_type == "Boom"
    r2 = adapter._api_complete([{"role": "user", "content": "hi"}], 10, 2)
    assert r2.status == "request_failed" and r2.error_type == "budget_exhausted"
    assert client.chat.completions.calls == 1         # refused before sending
    assert budget.used_summary()["gateway_requests_sent"] == 1


def test_openai_client_created_with_sdk_retries_disabled(monkeypatch):
    import mic.modeling.adapter as adapter_mod

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = type("Chat", (), {})()
            self.chat.completions = type("C", (), {"create": lambda self, **kw: (_ for _ in ()).throw(RuntimeError("x"))})()

    monkeypatch.setattr(adapter_mod, "OpenAI", FakeOpenAI)
    adapter = adapter_mod.ModelAdapter(model_config_id="m", provider="p", provider_type="t",
                                       endpoint="http://127.0.0.1:1/v1", model="x", api_key="k", allow_mock=False)
    adapter._api_complete([{"role": "user", "content": "hi"}], 10, 2)
    assert captured["max_retries"] == 0
