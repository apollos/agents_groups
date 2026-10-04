"""Offline E2E: real parser/model mock/store; no live fetch or model requests."""
from datetime import datetime, timezone

import pytest

from mic.pipeline import Pipeline
from mic.planner import PlannedQuery
from mic.schemas import SearchHit, TriageResult
from mic.search import SearchProvider
from mic.store import models as m

NOW = datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)


class Sources(SearchProvider):
    name = "mock"

    def __init__(self, cases):
        self.cases = cases

    def search(self, query, query_family=None, limit=10):
        return [SearchHit(query=query, query_family=query_family, title="宁德时代中标12亿元订单" + name,
                          url=f"https://example.test/{name}", domain="example.test", rank=i,
                          provider="mock", publish_time_guess="2026-10-03")
                for i, name in enumerate(self.cases, 1)]

    def page_body(self, url):
        name = url.rsplit("/", 1)[-1]
        published = self.cases[name]
        meta = f'<meta property="article:published_time" content="{published}">' if published else ''
        return (f'<html><head>{meta}<title>宁德时代订单{name}</title></head><body><article>'
                f'<p>宁德时代于2026年10月3日与特斯拉签订12亿元订单，{name}项目。</p>'
                '</article></body></html>')


def pipeline(config, monkeypatch, cases):
    monkeypatch.setattr("mic.publication_time.utcnow", lambda: NOW)
    config.raw["search_providers"]["active"] = "mock"
    config.raw["call_governance"]["batching"]["serp_batch_triage"] = False
    config.raw["output_schema"]["limits"]["strict_evidence_review"] = True
    pipe = Pipeline(config)
    pipe.search = Sources(cases)
    pipe.reader.search_provider = pipe.search
    monkeypatch.setattr(pipe.planner, "plan", lambda *a, **k: [PlannedQuery("宁德时代 中标", "orders_tender", 80)])
    monkeypatch.setattr(pipe.triage, "triage", lambda hit, link_id, **k: TriageResult(
        source_link_id=link_id, triage_decision="read", read_priority=90, need_model=True))
    return pipe


def collect(pipe, window="30d"):
    return pipe.collect_intelligence("company_300750", {
        "time_window": window, "focus": ["operating_update"],
        "budget_profile": {"max_queries": 1, "max_search_hits": 10, "max_links_to_read": 10, "max_model_calls": 10}})


def test_only_verified_recent_source_reaches_extraction(config, monkeypatch):
    pipe = pipeline(config, monkeypatch, {"old": "2025-08-06", "unknown": None,
                                         "future": "2026-10-05", "recent": "2026-10-03"})
    report = collect(pipe)
    diag = report["collection_diagnostics"]["time_window_filter"]
    assert diag["passed"] == 1
    assert diag["filtered_by_reason"] == {"outside_time_window": 1,
        "publication_time_unverified": 1, "future_publication_time": 1}
    assert report["summary"]["links_model_analyzed"] == 1
    assert report["structured_outputs"]["briefs"] == 1
    with pipe.repo.db.session() as db:
        analyzed = db.query(m.SourceLink).join(m.ModelRun, m.ModelRun.source_link_id == m.SourceLink.id).all()
        assert analyzed and all(link.url.endswith("/recent") for link in analyzed)
        assert db.query(m.CoverageGapRow).count() >= 3
        rejected = db.query(m.SourceLink).filter(m.SourceLink.url.endswith("/unknown")).one()
        assert rejected.triage_decision == "link_record_only"
        attempt = db.query(m.LinkReadAttempt).filter_by(source_link_id=rejected.id).one()
        assert attempt.diagnostics["publication_time"]["status"] == "unknown"
        assert attempt.diagnostics["fetch"]["time_window"]["allowed"] is False


def test_all_stale_or_unknown_report_filtered_not_model_failure(config, monkeypatch):
    pipe = pipeline(config, monkeypatch, {"old": "2025-08-06", "unknown": None})
    report = collect(pipe)
    assert report["summary"]["model_calls"] == 0
    assert report["collection_diagnostics"]["output_status"] == "time_window_filtered"
    assert not report["collection_diagnostics"]["usable"]
    assert report["structured_outputs"]["coverage_gaps"] == 2


@pytest.mark.parametrize("date", ["2025-08-06", None])
def test_historical_cache_cannot_bypass_current_window(config, monkeypatch, date):
    pipe = pipeline(config, monkeypatch, {"cached": date})
    historical = collect(pipe, None)
    assert historical["structured_outputs"]["briefs"] > 0
    current = collect(pipe)
    assert current["summary"]["links_read"] == 1
    assert current["summary"]["cached_or_reused_results"] == 0
    assert current["summary"]["links_model_analyzed"] == 0
    assert current["structured_outputs"]["briefs"] == 0


def test_fresh_content_cache_reused_only_after_publication_check(config, monkeypatch):
    pipe = pipeline(config, monkeypatch, {"recent": "2026-10-03"})
    assert collect(pipe)["structured_outputs"]["briefs"] > 0
    second = collect(pipe)
    assert second["summary"]["links_read"] == 1
    assert second["summary"]["cached_or_reused_results"] == 1
    assert second["structured_outputs"]["briefs"] > 0
    pipe.search.cases["recent"] = "2025-08-06"  # same body hash, now stale metadata
    third = collect(pipe)
    assert third["summary"]["cached_or_reused_results"] == 0
    assert third["structured_outputs"]["briefs"] == 0
