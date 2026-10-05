"""T10 / T12 / T13 / T14 / T16 / T17: Pipeline on the browser route with test doubles.

Search pages come from HTML fixtures, body reads from a fake browser session,
models from MIC mock mode. No real browser, network or model is involved.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone

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


@pytest.fixture(autouse=True)
def publication_clock(monkeypatch):
    monkeypatch.setattr("mic.publication_time.utcnow",
                        lambda: datetime(2026, 10, 4, tzinfo=timezone.utc))


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
    # The diversified second query asks about major-contract announcements.
    # Supply cards relevant to both fixture queries instead of only echoing the
    # new search-box text over a tender-only result page.
    pages = P1.replace("合同金额", "重大合同金额").replace("供货周期两年", "重大合同公告，供货周期两年")
    loader = FixtureLoader(default=_echo(pages))
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
    # Target identity for consumers: profile canonical name + aliases (business-event subject resolution).
    assert report["target"] == "宁德时代新能源科技股份有限公司"
    assert "宁德时代" in report["target_aliases"] and "CATL" in report["target_aliases"]
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


def _count_model_completions(monkeypatch) -> dict:
    """Count every ModelAdapter.complete call (mock or real) made during a run."""
    from mic.modeling.adapter import ModelAdapter
    counter = {"n": 0}
    orig = ModelAdapter.complete

    def counting(self, messages, max_tokens=None, json_mode=None):
        counter["n"] += 1
        return orig(self, messages, max_tokens=max_tokens, json_mode=json_mode)

    monkeypatch.setattr(ModelAdapter, "complete", counting)
    return counter


def test_cancel_during_body_read_sends_no_model_request(strict_cfg, monkeypatch):
    """Review R3: a cancel arriving while a body is being read used to be noticed only at the
    next loop head, after the model request for that body had already gone out."""
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    calls = _count_model_completions(monkeypatch)
    flag = {"cancel": False}

    class CancelDuringRead(FakeBrowserSession):
        def navigate(self, page, url, timeout_seconds):
            out = super().navigate(page, url, timeout_seconds)
            flag["cancel"] = True  # the Agent cancels while the page body is loading
            return out

    session = CancelDuringRead(pages=_session_for_hits().pages)
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "cancel_check": lambda: flag["cancel"], "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    if not session.navigations:
        pytest.skip("triage selected no links to read under mock scoring")
    assert diag["execution_status"] == "cancelled"
    assert calls["n"] == 0, "model request sent after the cancel signal"
    assert diag["budget_used"]["gateway_requests_sent"] == 0
    assert report["summary"]["links_model_analyzed"] == 0


def test_cancel_during_last_failing_read_is_not_completed(strict_cfg, monkeypatch):
    """Review (2nd round): a cancel arriving during the *last* read, which then fails and
    ``continue``s, used to slip past every check and the run was reported completed."""
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)  # HTTP = challenge -> browser read
    flag = {"cancel": False}

    class CancelAndFail(FakeBrowserSession):
        def navigate(self, page, url, timeout_seconds):
            flag["cancel"] = True
            return super().navigate(page, url, timeout_seconds)

    # Every body is a challenge page -> each read fails.
    session = CancelAndFail(pages={u: CAPTCHA_HTML for u in _session_for_hits().pages})
    report = pipe.collect_intelligence("company_300750", {**TASK, "budget_profile": {
        **TASK["budget_profile"], "max_links_to_read": 1}}, run_options={
        "cancel_check": lambda: flag["cancel"], "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    if not session.navigations:
        pytest.skip("triage selected no links to read under mock scoring")
    assert len(session.navigations) == 1
    assert diag["execution_status"] == "cancelled" and diag["usable"] is False
    assert report["summary"]["links_read"] == 0


def test_deadline_crossed_during_wrapup_is_timed_out_not_usable(strict_cfg, monkeypatch):
    """Review (2nd round): work finished inside the budget but closing the browser pushed the
    run past the hard limit (300 s budget, 301 s elapsed) -> not completed, not usable."""
    clock = FakeClock()
    loader = FixtureLoader(default=_echo(P1), clock=clock)
    pipe = _pipeline(strict_cfg, loader, monkeypatch)

    class SlowClose(FakeBrowserSession):
        def close(self):
            clock.advance(301)  # teardown takes longer than the whole budget
            return super().close()

    session = SlowClose(pages=_session_for_hits().pages)
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "deadline_seconds": 300, "clock": clock, "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    assert diag["execution_status"] == "timed_out" and diag["stop_reason"] == "run_deadline"
    assert diag["usable"] is False
    assert diag["cleanup"]["cleanup"] == "complete"
    run = pipe.repo.get_search_run(report["search_run_id"]) if hasattr(pipe.repo, "get_search_run") else None
    if run is not None:
        assert run["status"] == "timed_out"


def test_cancel_during_wrapup_is_reported_cancelled(strict_cfg, monkeypatch):
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    flag = {"cancel": False}

    class CancelOnClose(FakeBrowserSession):
        def close(self):
            flag["cancel"] = True
            return super().close()

    session = CancelOnClose(pages=_session_for_hits().pages)
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "cancel_check": lambda: flag["cancel"], "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    assert diag["execution_status"] == "cancelled" and diag["usable"] is False


def test_batch_triage_cannot_repromote_gated_hit(strict_cfg, monkeypatch):
    """Review (2nd round): the model batch triage ran after the rule gate and re-promoted a
    ``content_form_ok=false`` hit (``/tag/catl``) to read. The gate is final now."""
    import mic.pipeline as pipeline_mod
    from mic.pipeline import READ_GATE_SIGNAL_PREFIX
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    real_gate = pipeline_mod.Pipeline._apply_read_gate
    gated: list[str] = []

    def gate_all_as_tag_pages(hit, tri, stats):
        rel = (hit.discovery or {}).setdefault("relevance", {})
        rel["content_form_ok"] = False  # every SERP card is a tag / listing page
        out = real_gate(hit, tri, stats)
        if out.triage_decision == "link_record_only" and f"{READ_GATE_SIGNAL_PREFIX}content_form" in out.matched_signals:
            gated.append(hit.url)
        return out

    def repromote_everything(self, call_planner, triaged, stats):
        for _lid, _h, t in triaged:
            t.triage_decision, t.need_model, t.read_priority = "read", True, 200.0
        stats.batch_triage_calls += 1

    monkeypatch.setattr(pipeline_mod.Pipeline, "_apply_read_gate", staticmethod(gate_all_as_tag_pages))
    monkeypatch.setattr(pipeline_mod.Pipeline, "_batch_triage", repromote_everything)
    session = _session_for_hits()
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "deadline_seconds": 300, "browser_factory": lambda ctx: session})
    assert gated, "gate never demoted anything - test setup broken"
    assert session.navigations == []  # nothing read despite the model saying 'read'
    assert report["summary"]["links_selected_for_read"] == 0
    assert report["summary"]["read_gate_demoted"].get("content_form", 0) == len(set(gated))
    links = pipe.repo.source_links_for_run(report["search_run_id"], limit=50)
    assert links and all(link["triage_decision"] != "read" for link in links)


def test_model_response_after_deadline_is_timed_out_and_not_persisted(strict_cfg, monkeypatch):
    """Review R3: the last model call returning past the deadline must not yield 'completed'."""
    from mic.modeling.adapter import ModelAdapter
    clock = FakeClock()
    loader = FixtureLoader(default=_echo(P1), clock=clock)
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    orig_complete, orig_mock = ModelAdapter.complete, ModelAdapter._mock_complete
    statuses: list[str] = []

    def slow_mock(self, messages, input_chars):  # runs after the pre-send gate = "in flight"
        clock.advance(400)  # the response lands after the 300 s run deadline
        return orig_mock(self, messages, input_chars)

    def recording_complete(self, messages, max_tokens=None, json_mode=None):
        res = orig_complete(self, messages, max_tokens=max_tokens, json_mode=json_mode)
        statuses.append(res.status)
        return res

    monkeypatch.setattr(ModelAdapter, "_mock_complete", slow_mock)
    monkeypatch.setattr(ModelAdapter, "complete", recording_complete)
    session = _session_for_hits()
    report = pipe.collect_intelligence("company_300750", TASK, run_options={
        "deadline_seconds": 300, "clock": clock, "browser_factory": lambda ctx: session})
    diag = report["collection_diagnostics"]
    if not statuses:
        pytest.skip("no model call under mock scoring")
    # The late response is the only one that went out; every further adapter in the same plan
    # was refused by the deadline gate (mock path included) and the run ended on the next check.
    assert statuses[0] == "success" and all(s == "request_failed" for s in statuses[1:])
    assert diag["execution_status"] == "timed_out"
    assert diag["usable"] is False
    assert report["summary"]["links_model_analyzed"] == 0
    assert all(v == 0 for v in report["structured_outputs"].values())  # nothing persisted late
    assert report["all_events"] == []


def test_call_planner_uses_effective_model_call_limit(monkeypatch):
    """Review R4: deployment max_model_calls=1, task asks 3 -> planner budget is 1."""
    import mic.pipeline as pipeline_mod
    from tests.browser_doubles import browser_runtime_block
    cfg = browser_config(runtime=browser_runtime_block(limits={"max_model_calls": 1, "max_gateway_requests": 5}))
    captured = {}
    real_planner = pipeline_mod.ModelCallPlanner

    class CapturingPlanner(real_planner):
        def __init__(self, config, registry, budget):
            captured["budget"] = budget
            super().__init__(config, registry, budget)

    monkeypatch.setattr(pipeline_mod, "ModelCallPlanner", CapturingPlanner)
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(cfg, loader, monkeypatch)
    calls = _count_model_completions(monkeypatch)
    report = pipe.collect_intelligence("company_300750", {**TASK, "budget_profile": {
        **TASK["budget_profile"], "max_model_calls": 3}}, run_options={
        "browser_factory": lambda ctx: _session_for_hits()})
    assert captured["budget"].max_model_calls_per_run == 1
    assert report["summary"]["model_calls"] <= 1
    assert calls["n"] <= 1


def _hit_with_relevance(title: str, **rel):
    from mic.schemas import SearchHit
    base = {"relevant": True, "target_match": True, "task_match": True,
            "content_form_ok": True, "site_ok": True, "matched_terms": [], "reasons": []}
    return SearchHit(query="q", title=title, snippet="", url="https://news.example.com/a",
                     domain="news.example.com", rank=1, provider="browser:bing",
                     discovery={"relevance": {**base, **rel}})


def _read_triage(signals: list[str]):
    from mic.schemas import TriageResult
    return TriageResult(source_link_id="l1", triage_decision="read", read_priority=112.0,
                        matched_signals=signals, need_model=True, reason="rule score 112")


def test_read_gate_demotes_non_target_hits_and_flags_related_companies():
    """Review R7 / design 6.3: relevance rules gate read candidates; never promote."""
    from mic.pipeline import READ_GATE_SIGNAL_PREFIX, RELATED_ONLY_SIGNAL, Pipeline, RunStats
    stats = RunStats()
    # 比亚迪 tender, target CATL not named, 比亚迪 not in the target profile -> record only.
    out = Pipeline._apply_read_gate(_hit_with_relevance("比亚迪中标200亿元", target_match=False),
                                    _read_triage(["amount_mentioned", "tender_keyword"]), stats)
    assert out.triage_decision == "link_record_only" and out.need_model is False
    assert f"{READ_GATE_SIGNAL_PREFIX}no_target_or_related_entity" in out.matched_signals
    assert out.read_priority == 112.0  # score untouched: gate is transparent
    # Same hit but 比亚迪 is a competitor in the profile (legacy entity match) -> still readable,
    # explicitly tagged so the queue orders it behind target-identity candidates.
    out = Pipeline._apply_read_gate(_hit_with_relevance("比亚迪中标200亿元", target_match=False),
                                    _read_triage(["target_entity_match", "amount_mentioned"]), stats)
    assert out.triage_decision == "read" and RELATED_ONLY_SIGNAL in out.matched_signals
    # Wrong site / wrong content form demote regardless of entity match.
    out = Pipeline._apply_read_gate(_hit_with_relevance("宁德时代 中标", site_ok=False),
                                    _read_triage(["target_entity_match"]), stats)
    assert out.triage_decision == "link_record_only"
    assert f"{READ_GATE_SIGNAL_PREFIX}site_rule" in out.matched_signals
    out = Pipeline._apply_read_gate(_hit_with_relevance("宁德时代 中标", content_form_ok=False),
                                    _read_triage(["target_entity_match"]), stats)
    assert f"{READ_GATE_SIGNAL_PREFIX}content_form" in out.matched_signals
    # Target-identity hit passes through unchanged; non-read decisions are never promoted.
    tri = _read_triage(["target_entity_match"])
    assert Pipeline._apply_read_gate(_hit_with_relevance("宁德时代 中标"), tri, stats) is tri
    rec = tri.model_copy(update={"triage_decision": "link_record_only"})
    assert Pipeline._apply_read_gate(_hit_with_relevance("宁德时代 中标"), rec, stats) is rec
    # Hits without relevance metadata (legacy providers) are untouched.
    from mic.schemas import SearchHit
    plain = SearchHit(query="q", title="t", snippet="", url="https://x/a", domain="x", rank=1, provider="p")
    assert Pipeline._apply_read_gate(plain, tri, stats) is tri
    assert stats.read_gate_demoted == {"no_target_or_related_entity": 1, "site_rule": 1, "content_form": 1}
    assert stats.read_gate_related_only == 1


def test_read_gate_counters_in_report_and_queue_order(strict_cfg, monkeypatch):
    """End to end: gate counters surface in the report summary; related-only hits sort last."""
    import mic.pipeline as pipeline_mod
    from mic.pipeline import RELATED_ONLY_SIGNAL
    loader = FixtureLoader(default=_echo(P1))
    pipe = _pipeline(strict_cfg, loader, monkeypatch)
    real_gate = pipeline_mod.Pipeline._apply_read_gate
    order: list[str] = []

    def tagging_gate(hit, tri, stats):
        out = real_gate(hit, tri, stats)
        # Mark the first read candidate as related-only to exercise queue ordering.
        if out.triage_decision == "read" and not order:
            order.append(hit.url)
            return out.model_copy(update={"matched_signals": [*out.matched_signals, RELATED_ONLY_SIGNAL],
                                          "read_priority": 999.0})
        return out

    monkeypatch.setattr(pipeline_mod.Pipeline, "_apply_read_gate", staticmethod(tagging_gate))
    read_urls: list[str] = []
    real_read = LinkReader.read

    def recording_read(self, source_link_id, url, *a, **kw):
        read_urls.append(url)
        return real_read(self, source_link_id, url, *a, **kw)

    monkeypatch.setattr(LinkReader, "read", recording_read)
    report = pipe.collect_intelligence("company_300750", {**TASK, "budget_profile": {
        **TASK["budget_profile"], "max_links_to_read": 1}}, run_options={
        "deadline_seconds": 300, "browser_factory": lambda ctx: _session_for_hits()})
    s = report["summary"]
    assert "read_gate_demoted" in s and "read_gate_related_only" in s
    # Despite priority 999 the related-only candidate must not take the single read slot.
    assert read_urls and read_urls[0] != order[0]


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
