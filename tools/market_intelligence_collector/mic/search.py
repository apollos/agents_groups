"""Search Provider Layer (spec section 4, item 4 + 2.4).

Discovers links via a search engine / API. Ships with:
  - MockSearchProvider: deterministic synthetic hits so the whole pipeline runs
    offline with no keys (the demo default).
  - SerpApiProvider: real SERP results via SerpApi (engine=baidu/google/bing).

The mock provider also exposes synthetic page bodies so the Link Reader can run
end-to-end offline (see ``MockSearchProvider.page_body``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
from abc import ABC, abstractmethod
from typing import Any

import httpx

from mic.schemas import SearchHit
from mic.utils import domain_of

logger = logging.getLogger(__name__)


class SearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        ...

    def page_body(self, url: str) -> str | None:
        """Optional: providers backed by synthetic data can serve page bodies."""
        return None


# --- Mock provider ----------------------------------------------------------

_DOMAINS = [
    ("cninfo.com.cn", "exchange"),
    ("sse.com.cn", "exchange"),
    ("gov.cn", "regulator"),
    ("cs.com.cn", "media"),
    ("yicai.com", "media"),
    ("eastmoney.com", "media"),
    ("xueqiu.com", "forum"),
]

# Snippet/body templates seeded with concrete facts so triage + extraction have
# something to work with. {q} is the query, {co} the leading token of the query.
_TEMPLATES = [
    ("{co}发布公告：与{cust}签订重大供货协议，合同金额约{amt}亿元，预计{period}交付。",
     "{co}今日发布公告，公司与{cust}签订动力电池供货框架协议，协议金额约{amt}亿元，"
     "供货周期覆盖{period}。公司表示该订单有望提升储能与动力电池系统出货量，"
     "对当期收入形成正向拉动。原材料碳酸锂价格近期同比下降12%，单位成本承压缓解，"
     "毛利率环比改善2.3个百分点。"),
    ("{co}{period}经营数据：出货量同比增长28%，开工率回升至{rate}%。",
     "据{co}投资者关系活动记录，{period}动力电池出货量同比增长28%，产能利用率回升至{rate}%，"
     "公司称下游新能源汽车需求旺盛，渠道库存处于低位。管理层提示海外关税与汇率波动风险。"),
    ("{co}涉及环保处罚，被监管部门罚款{amt}万元。",
     "监管部门公告显示，{co}下属子公司因环保问题被处以{amt}万元罚款，并要求限期整改。"
     "公司回应称影响有限，不影响正常生产经营，但分析师关注潜在停产与声誉风险。"),
    ("{q}：行业价格持续走高，新增产能投放节奏放缓。",
     "行业研究显示，{q}相关产品价格本月环比上涨5%，库存去化明显，开工率维持高位。"
     "受能耗与环保政策约束，新增产能投放节奏放缓，供需缺口短期难以缓解。"),
]


class MockSearchProvider(SearchProvider):
    name = "mock"

    def __init__(self, hits_per_query: int = 6):
        self.hits_per_query = hits_per_query
        self._bodies: dict[str, str] = {}

    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        n = min(limit, self.hits_per_query)
        rng = random.Random(int(hashlib.md5(query.encode("utf-8")).hexdigest(), 16))
        co = query.split()[0] if query.split() else query
        cust = "特斯拉"
        hits: list[SearchHit] = []
        for rank in range(1, n + 1):
            title_tpl, body_tpl = _TEMPLATES[(rank - 1) % len(_TEMPLATES)]
            fill = {
                "q": query, "co": co, "cust": cust,
                "amt": rng.choice([12, 23, 35, 48, 86]),
                "period": rng.choice(["2026Q1", "2026Q2", "近30天", "上半年"]),
                "rate": rng.choice([72, 78, 83, 88]),
            }
            title = title_tpl.format(**fill)
            body = body_tpl.format(**fill)
            domain, _stype = _DOMAINS[(rank - 1) % len(_DOMAINS)]
            slug = hashlib.md5(f"{query}{rank}".encode()).hexdigest()[:10]
            url = f"https://www.{domain}/news/{slug}.html"
            self._bodies[url] = f"<html><head><title>{title}</title></head><body>" \
                                f"<article><h1>{title}</h1><p>{body}</p></article></body></html>"
            hits.append(SearchHit(
                query=query, title=title, snippet=body[:80], url=url,
                domain=domain, rank=rank, provider=self.name,
                publish_time_guess="2026-06", query_family=query_family,
            ))
        return hits

    def page_body(self, url: str) -> str | None:
        return self._bodies.get(url)


# --- Real providers ---------------------------------------------------------


class SerpApiProvider(SearchProvider):
    name = "serpapi"

    def __init__(self, cfg: dict[str, Any]):
        self.endpoint = cfg.get("endpoint", "https://serpapi.com/search")
        self.engine = cfg.get("engine", "baidu")
        self.api_key = os.environ.get(cfg.get("api_key_env", "SERPAPI_API_KEY"), "")
        self.hits_per_query = cfg.get("hits_per_query", 10)
        # Instance-level name keeps engines distinguishable when several SerpApi
        # engines (e.g. baidu + google) run together in a CompositeSearchProvider.
        self.name = f"serpapi:{self.engine}"

    # SerpApi uses a different "number of results" param per backing engine:
    # google -> num, baidu -> rn, bing -> count. Unknown engines fall back to
    # "num" (the most common convention among SerpApi engines).
    _COUNT_PARAM = {"google": "num", "baidu": "rn", "bing": "count"}

    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        if not self.api_key:
            raise RuntimeError("SERPAPI_API_KEY not set")
        count_param = self._COUNT_PARAM.get(self.engine, "num")
        params = {"engine": self.engine, "q": query, "api_key": self.api_key,
                  count_param: min(limit, self.hits_per_query)}
        resp = httpx.get(self.endpoint, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("organic_results", [])
        hits = []
        for rank, r in enumerate(results[:limit], start=1):
            url = r.get("link", "")
            hits.append(SearchHit(
                query=query, title=r.get("title", ""), snippet=r.get("snippet", ""),
                url=url, domain=domain_of(url), rank=rank, provider=self.name,
                publish_time_guess=r.get("date"), query_family=query_family,
            ))
        return hits


# Note: the native Bing Search API v7 was retired by Microsoft on 2025-08-11
# (no new keys, existing ones disabled). Bing results are available via
# SerpApiProvider with engine=bing, or through a SearXNG instance.


class SearxngProvider(SearchProvider):
    """Self-hosted SearXNG metasearch instance (free, no per-query cost).

    One query fans out to the engines configured on the instance (e.g.
    baidu + google + bing) and comes back as structured JSON. The instance
    must enable the JSON output format (``search.formats: [html, json]`` in
    its settings.yml); see ``searxng/`` in this repo for a ready-to-run
    docker-compose setup.
    """

    name = "searxng"

    def __init__(self, cfg: dict[str, Any]):
        env_name = cfg.get("base_url_env", "SEARXNG_BASE_URL")
        self.base_url = (os.environ.get(env_name, "")
                         or cfg.get("base_url", "http://localhost:8888")).rstrip("/")
        self.engines = cfg.get("engines", [])
        self.language = cfg.get("language", "zh-CN")
        self.hits_per_query = cfg.get("hits_per_query", 10)
        self.name = "searxng"

    def is_available(self) -> bool:
        """Cheap liveness probe so the factory can skip a down instance."""
        try:
            resp = httpx.get(f"{self.base_url}/healthz", timeout=3)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        params: dict[str, Any] = {
            "q": query, "format": "json", "language": self.language,
            "categories": "general",
        }
        if self.engines:
            params["engines"] = ",".join(self.engines)
        resp = httpx.get(f"{self.base_url}/search", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        n = min(limit, self.hits_per_query)
        hits = []
        for rank, r in enumerate(results[:n], start=1):
            url = r.get("url", "")
            engine = r.get("engine", "")
            hits.append(SearchHit(
                query=query, title=r.get("title", ""), snippet=r.get("content", ""),
                url=url, domain=domain_of(url), rank=rank,
                provider=f"searxng:{engine}" if engine else self.name,
                publish_time_guess=r.get("publishedDate"), query_family=query_family,
            ))
        return hits


class TavilyProvider(SearchProvider):
    """Tavily search API (https://tavily.com) - LLM-oriented web search.

    Bills per API call like SerpApi (basic search = 1 credit; the free tier is
    1000 credits/month). Returns extracted-content snippets that are longer and
    cleaner than classic SERP snippets, which helps triage scoring. Coverage of
    the Chinese financial web (baijiahao, cls.cn, ...) is weaker than Baidu, so
    it fits best as a zero-ops rescue ``fallback`` or a supplementary engine
    rather than the sole primary for A-share targets.
    """

    name = "tavily"

    def __init__(self, cfg: dict[str, Any]):
        self.endpoint = cfg.get("endpoint", "https://api.tavily.com/search")
        self.api_key = os.environ.get(cfg.get("api_key_env", "TAVILY_API_KEY"), "")
        self.hits_per_query = cfg.get("hits_per_query", 10)
        self.topic = cfg.get("topic", "general")
        # "basic" costs 1 credit per call, "advanced" costs 2.
        self.search_depth = cfg.get("search_depth", "basic")

    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        if not self.api_key:
            raise RuntimeError("TAVILY_API_KEY not set")
        body = {
            "query": query,
            "topic": self.topic,
            "search_depth": self.search_depth,
            "max_results": min(limit, self.hits_per_query, 20),
        }
        resp = httpx.post(self.endpoint, json=body, timeout=20,
                          headers={"Authorization": f"Bearer {self.api_key}"})
        resp.raise_for_status()
        data = resp.json()
        hits = []
        for rank, r in enumerate(data.get("results", []), start=1):
            url = r.get("url", "")
            hits.append(SearchHit(
                query=query, title=r.get("title", ""), snippet=r.get("content", ""),
                url=url, domain=domain_of(url), rank=rank, provider=self.name,
                publish_time_guess=r.get("published_date"), query_family=query_family,
            ))
        return hits


# --- Composite (fan-out to several engines) ---------------------------------


class CompositeSearchProvider(SearchProvider):
    """Runs several engines per query and merges their hits.

    Each query is sent to every child provider; results are concatenated and
    keep their own ``provider`` tag (e.g. ``serpapi:baidu`` / ``bing``).
    Cross-engine overlap is deduplicated downstream by canonical URL in the
    pipeline, so here we simply collect everything. A single engine failing
    (network error, etc.) is logged and skipped rather than aborting the query.
    """

    name = "composite"

    def __init__(self, providers: list[SearchProvider]):
        if not providers:
            raise ValueError("CompositeSearchProvider requires at least one provider")
        self.providers = providers
        self.name = "composite(" + "+".join(p.name for p in providers) + ")"

    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for provider in self.providers:
            try:
                hits.extend(provider.search(query, query_family, limit=limit))
            except Exception as exc:  # noqa: BLE001 - one engine shouldn't kill the query
                logger.warning("search_engine_failed engine=%s query=%r error=%s",
                               provider.name, query, exc)
        return hits

    def page_body(self, url: str) -> str | None:
        for provider in self.providers:
            body = provider.page_body(url)
            if body is not None:
                return body
        return None


# --- Fallback (primary engines -> rescue engine) ----------------------------


class FallbackSearchProvider(SearchProvider):
    """Wraps a primary provider with a rescue provider.

    The fallback fires when the primary raises *or* returns zero hits (a
    composite primary swallows per-engine errors, so "all engines failed"
    surfaces here as an empty list). If the fallback itself fails, the
    exception propagates to the pipeline's per-query error isolation.
    """

    name = "fallback"

    def __init__(self, primary: SearchProvider, fallback: SearchProvider):
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name} -> fallback({fallback.name})"

    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        hits: list[SearchHit] = []
        try:
            hits = self.primary.search(query, query_family, limit=limit)
        except Exception as exc:  # noqa: BLE001 - rescue instead of failing the query
            logger.warning("primary_search_failed provider=%s query=%r error=%s",
                           self.primary.name, query, exc)
        if hits:
            return hits
        logger.info("search_fallback_engaged fallback=%s query=%r",
                    self.fallback.name, query)
        return self.fallback.search(query, query_family, limit=limit)

    def page_body(self, url: str) -> str | None:
        body = self.primary.page_body(url)
        if body is not None:
            return body
        return self.fallback.page_body(url)


def _mock_from_config(pcfg: dict[str, Any]) -> MockSearchProvider:
    return MockSearchProvider(pcfg.get("hits_per_query", 6))


def _build_one(name: str, providers: dict[str, Any]) -> SearchProvider | None:
    """Build a single configured provider.

    Returns the provider, or ``None`` when it should be skipped because a real
    engine is selected but its API key is missing. Mock providers are always
    built. Key/fallback policy across the whole selection is decided by the
    caller so it can honour ``MIC_ALLOW_MOCK`` consistently.
    """
    pcfg = providers.get(name, {})
    ptype = pcfg.get("type", name)

    if ptype == "mock":
        return _mock_from_config(pcfg)
    if ptype == "serpapi":
        provider = SerpApiProvider(pcfg)
        return provider if provider.api_key else None
    if ptype == "searxng":
        searx = SearxngProvider(pcfg)
        return searx if searx.is_available() else None
    if ptype == "tavily":
        tavily = TavilyProvider(pcfg)
        return tavily if tavily.api_key else None

    logger.warning("unknown_search_provider name=%s type=%s", name, ptype)
    return None


def build_search_provider(config) -> SearchProvider:
    """Factory based on search_providers.yaml + MIC_ALLOW_MOCK.

    ``active`` may be a single provider name (string) or a list of names to run
    together. Each name refers to an entry under ``providers`` (so the same
    SerpApi backend can appear several times with different ``engine`` values,
    e.g. ``serpapi_baidu`` + ``serpapi_google``).

    An optional ``fallback`` key names one rescue provider or an ordered list
    of them (e.g. ``[tavily, searxng]``): when every active engine fails or a
    query yields zero hits, the same query is retried against each fallback in
    turn until one returns results.

    Real providers are validated at construction time. Engines whose API key is
    missing (or whose SearXNG instance is down) are skipped (logged). If at
    least one real engine remains, the run proceeds with those; multiple
    engines are wrapped in a ``CompositeSearchProvider``. If nothing usable is
    built, ``MIC_ALLOW_MOCK=true`` falls back to the deterministic mock provider
    while ``MIC_ALLOW_MOCK=false`` fails fast. This keeps the README contract
    honest and avoids silent zero-hit runs where every query raises inside
    ``search()`` and gets swallowed by the pipeline's per-query error isolation.
    """
    sp_cfg = config.search_providers
    active = sp_cfg.get("active", "mock")
    providers = sp_cfg.get("providers", {})
    names = [active] if isinstance(active, str) else list(active)

    built: list[SearchProvider] = []
    for name in names:
        provider = _build_one(name, providers)
        if provider is not None:
            built.append(provider)
        else:
            logger.warning("search_provider_skipped name=%s (missing key or unknown)", name)

    primary: SearchProvider | None = None
    if len(built) == 1:
        primary = built[0]
    elif built:
        primary = CompositeSearchProvider(built)

    if primary is None:
        # Nothing usable (missing keys / down instances / unknown types).
        # Try the fallback provider alone before resorting to mock.
        fallback = _build_fallback(sp_cfg, providers, names)
        if fallback is not None:
            logger.warning("search_using_fallback_only fallback=%s", fallback.name)
            return fallback
        if config.allow_mock:
            first_cfg = providers.get(names[0], {}) if names else {}
            return _mock_from_config(first_cfg)
        raise RuntimeError(
            f"No usable search provider for active={active!r} and MIC_ALLOW_MOCK=false "
            "(check API keys / SearXNG instance)")

    # Mock never fails, so wrapping it in a fallback only adds noise.
    if isinstance(primary, MockSearchProvider):
        return primary

    fallback = _build_fallback(sp_cfg, providers, names)
    if fallback is not None:
        return FallbackSearchProvider(primary, fallback)
    return primary


def _build_fallback(sp_cfg: dict[str, Any], providers: dict[str, Any],
                    active_names: list[str]) -> SearchProvider | None:
    """Build the configured ``fallback`` provider(s), if any and usable.

    ``fallback`` accepts a single name or an ordered list (e.g.
    ``[tavily, searxng]``): the first usable provider is tried first and each
    later one rescues the previous, by nesting ``FallbackSearchProvider``.
    Names already running as active engines are skipped (they would only
    repeat the same query), as are unusable ones (missing key / down instance).
    """
    fallback_cfg = sp_cfg.get("fallback")
    if not fallback_cfg:
        return None
    names = [fallback_cfg] if isinstance(fallback_cfg, str) else list(fallback_cfg)

    built: list[SearchProvider] = []
    for name in names:
        if name in active_names:
            continue
        provider = _build_one(name, providers)
        if provider is None:
            logger.warning("search_fallback_unavailable name=%s", name)
            continue
        built.append(provider)
    if not built:
        return None

    chained = built[-1]
    for provider in reversed(built[:-1]):
        chained = FallbackSearchProvider(provider, chained)
    return chained
