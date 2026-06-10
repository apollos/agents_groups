"""Unit tests for the search provider layer: searxng, composite, fallback."""

from __future__ import annotations

import pytest

import mic.search as search_mod
from mic.config import load_config
from mic.schemas import SearchHit
from mic.search import (
    CompositeSearchProvider,
    FallbackSearchProvider,
    MockSearchProvider,
    SearxngProvider,
    TavilyProvider,
    build_search_provider,
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def test_searxng_provider_parses_json(monkeypatch):
    payload = {"results": [
        {"url": "https://www.cninfo.com.cn/a.html", "title": "公告A",
         "content": "摘要A", "engine": "baidu", "publishedDate": "2026-06-01"},
        {"url": "https://finance.example.com/b.html", "title": "新闻B",
         "content": "摘要B", "engine": "google"},
    ]}
    captured: dict = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(payload)

    monkeypatch.setattr(search_mod.httpx, "get", fake_get)
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://localhost:8888")
    provider = SearxngProvider({"engines": ["baidu", "google"]})
    hits = provider.search("宁德时代 公告", query_family="operating_update", limit=5)

    assert captured["url"] == "http://localhost:8888/search"
    assert captured["params"]["format"] == "json"
    assert captured["params"]["engines"] == "baidu,google"
    assert len(hits) == 2
    assert hits[0].provider == "searxng:baidu"
    assert hits[1].provider == "searxng:google"
    assert hits[0].domain == "cninfo.com.cn"
    assert hits[0].query_family == "operating_update"


def test_tavily_provider_parses_json(monkeypatch):
    payload = {"results": [
        {"url": "https://www.cls.cn/detail/1.html", "title": "宁德时代中标",
         "content": "宁德时代获得百亿订单……", "score": 0.91,
         "published_date": "2026-06-01"},
        {"url": "https://finance.example.com/2.html", "title": "电解液涨价",
         "content": "碳酸锂价格上行……", "score": 0.74},
    ]}
    captured: dict = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return _FakeResponse(payload)

    monkeypatch.setattr(search_mod.httpx, "post", fake_post)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    provider = TavilyProvider({"hits_per_query": 10})
    hits = provider.search("宁德时代 中标", query_family="orders_tender", limit=5)

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["headers"]["Authorization"] == "Bearer tvly-test-key"
    assert captured["body"]["max_results"] == 5
    assert captured["body"]["search_depth"] == "basic"
    assert len(hits) == 2
    assert hits[0].provider == "tavily"
    assert hits[0].domain == "cls.cn"
    assert hits[0].publish_time_guess == "2026-06-01"
    assert hits[0].query_family == "orders_tender"


def test_tavily_provider_requires_key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    provider = TavilyProvider({})
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY"):
        provider.search("q")


def _hit(url: str = "https://example.com/x") -> SearchHit:
    return SearchHit(query="q", title="t", snippet="s", url=url,
                     domain="example.com", rank=1, provider="stub")


class _StubProvider(search_mod.SearchProvider):
    name = "stub"

    def __init__(self, hits=None, error: Exception | None = None):
        self._hits = hits or []
        self._error = error
        self.calls = 0

    def search(self, query, query_family=None, limit=10):
        self.calls += 1
        if self._error:
            raise self._error
        return self._hits


def test_fallback_engages_on_primary_error():
    primary = _StubProvider(error=RuntimeError("boom"))
    fallback = _StubProvider(hits=[_hit()])
    provider = FallbackSearchProvider(primary, fallback)
    hits = provider.search("q")
    assert len(hits) == 1
    assert fallback.calls == 1


def test_fallback_engages_on_zero_hits():
    primary = _StubProvider(hits=[])
    fallback = _StubProvider(hits=[_hit()])
    provider = FallbackSearchProvider(primary, fallback)
    assert len(provider.search("q")) == 1
    assert fallback.calls == 1


def test_fallback_not_engaged_when_primary_succeeds():
    primary = _StubProvider(hits=[_hit()])
    fallback = _StubProvider(hits=[_hit("https://example.com/y")])
    provider = FallbackSearchProvider(primary, fallback)
    assert len(provider.search("q")) == 1
    assert fallback.calls == 0


def test_fallback_chain_second_rescue_engages():
    primary = _StubProvider(error=RuntimeError("primary down"))
    fb1 = _StubProvider(error=RuntimeError("tavily down"))
    fb2 = _StubProvider(hits=[_hit()])
    provider = FallbackSearchProvider(primary, FallbackSearchProvider(fb1, fb2))
    hits = provider.search("q")
    assert len(hits) == 1
    assert fb1.calls == 1 and fb2.calls == 1


def test_factory_builds_fallback_chain(monkeypatch):
    cfg = load_config()
    cfg.raw["search_providers"]["active"] = "serpapi_baidu"
    cfg.raw["search_providers"]["fallback"] = ["tavily", "searxng"]
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setenv("TAVILY_API_KEY", "dummy")
    monkeypatch.setattr(SearxngProvider, "is_available", lambda self: True)
    provider = build_search_provider(cfg)
    assert isinstance(provider, FallbackSearchProvider)
    assert isinstance(provider.fallback, FallbackSearchProvider)
    assert isinstance(provider.fallback.primary, TavilyProvider)
    assert isinstance(provider.fallback.fallback, SearxngProvider)


def test_factory_fallback_chain_skips_unusable(monkeypatch):
    cfg = load_config()
    cfg.raw["search_providers"]["active"] = "serpapi_baidu"
    cfg.raw["search_providers"]["fallback"] = ["tavily", "searxng"]
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setenv("TAVILY_API_KEY", "")            # tavily unusable
    monkeypatch.setattr(SearxngProvider, "is_available", lambda self: True)
    provider = build_search_provider(cfg)
    assert isinstance(provider, FallbackSearchProvider)
    assert isinstance(provider.fallback, SearxngProvider)  # chain collapses


def test_composite_merges_and_isolates_engine_failures():
    good = MockSearchProvider(2)
    bad = _StubProvider(error=RuntimeError("engine down"))
    provider = CompositeSearchProvider([bad, good])
    hits = provider.search("宁德时代", limit=5)
    assert len(hits) == 2  # bad engine skipped, good engine still answers


def test_factory_skips_down_searxng_and_falls_back_to_mock(monkeypatch):
    cfg = load_config()
    cfg.raw["search_providers"]["active"] = "searxng"
    cfg.raw["search_providers"].pop("fallback", None)
    monkeypatch.setenv("MIC_ALLOW_MOCK", "true")
    monkeypatch.setattr(SearxngProvider, "is_available", lambda self: False)
    provider = build_search_provider(cfg)
    assert isinstance(provider, MockSearchProvider)


def test_factory_wraps_real_primary_with_searxng_fallback(monkeypatch):
    cfg = load_config()
    cfg.raw["search_providers"]["active"] = "serpapi_baidu"
    cfg.raw["search_providers"]["fallback"] = "searxng"
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setattr(SearxngProvider, "is_available", lambda self: True)
    provider = build_search_provider(cfg)
    assert isinstance(provider, FallbackSearchProvider)
    assert provider.primary.name == "serpapi:baidu"
    assert isinstance(provider.fallback, SearxngProvider)


def test_factory_uses_fallback_alone_when_primary_unusable(monkeypatch):
    cfg = load_config()
    cfg.raw["search_providers"]["active"] = "serpapi_baidu"
    cfg.raw["search_providers"]["fallback"] = "searxng"
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("MIC_ALLOW_MOCK", "false")
    monkeypatch.setattr(SearxngProvider, "is_available", lambda self: True)
    provider = build_search_provider(cfg)
    assert isinstance(provider, SearxngProvider)


def test_factory_fails_fast_when_nothing_usable(monkeypatch):
    cfg = load_config()
    cfg.raw["search_providers"]["active"] = "serpapi_baidu"
    cfg.raw["search_providers"]["fallback"] = "searxng"
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("MIC_ALLOW_MOCK", "false")
    monkeypatch.setattr(SearxngProvider, "is_available", lambda self: False)
    with pytest.raises(RuntimeError):
        build_search_provider(cfg)
