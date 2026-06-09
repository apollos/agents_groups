"""Search Provider Layer (spec section 4, item 4 + 2.4).

Discovers links via a search engine / API. Ships with:
  - MockSearchProvider: deterministic synthetic hits so the whole pipeline runs
    offline with no keys (the demo default).
  - SerpApiProvider / BingProvider: real OpenAI-style HTTP search APIs.

The mock provider also exposes synthetic page bodies so the Link Reader can run
end-to-end offline (see ``MockSearchProvider.page_body``).
"""

from __future__ import annotations

import hashlib
import os
import random
from abc import ABC, abstractmethod
from typing import Any

import httpx

from mic.schemas import SearchHit
from mic.utils import domain_of


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

    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        if not self.api_key:
            raise RuntimeError("SERPAPI_API_KEY not set")
        params = {"engine": self.engine, "q": query, "api_key": self.api_key,
                  "num": min(limit, self.hits_per_query)}
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


class BingProvider(SearchProvider):
    name = "bing"

    def __init__(self, cfg: dict[str, Any]):
        self.endpoint = cfg.get("endpoint", "https://api.bing.microsoft.com/v7.0/search")
        self.api_key = os.environ.get(cfg.get("api_key_env", "BING_SEARCH_API_KEY"), "")
        self.hits_per_query = cfg.get("hits_per_query", 10)

    def search(self, query: str, query_family: str | None = None,
               limit: int = 10) -> list[SearchHit]:
        if not self.api_key:
            raise RuntimeError("BING_SEARCH_API_KEY not set")
        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        params = {"q": query, "count": min(limit, self.hits_per_query), "mkt": "zh-CN"}
        resp = httpx.get(self.endpoint, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("webPages", {}).get("value", [])
        hits = []
        for rank, r in enumerate(results[:limit], start=1):
            url = r.get("url", "")
            hits.append(SearchHit(
                query=query, title=r.get("name", ""), snippet=r.get("snippet", ""),
                url=url, domain=domain_of(url), rank=rank, provider=self.name,
                publish_time_guess=r.get("dateLastCrawled"), query_family=query_family,
            ))
        return hits


def _mock_from_config(pcfg: dict[str, Any]) -> MockSearchProvider:
    return MockSearchProvider(pcfg.get("hits_per_query", 6))


def build_search_provider(config) -> SearchProvider:
    """Factory based on search_providers.yaml + MIC_ALLOW_MOCK.

    Real providers are validated at construction time. If a real provider is
    selected but its key is missing, ``MIC_ALLOW_MOCK=true`` falls back to the
    deterministic mock provider; ``MIC_ALLOW_MOCK=false`` fails fast. This keeps
    the README contract honest and avoids silent zero-hit runs where every query
    raises inside ``search()`` and gets swallowed by the pipeline's per-query
    error isolation.
    """
    sp_cfg = config.search_providers
    active = sp_cfg.get("active", "mock")
    providers = sp_cfg.get("providers", {})
    pcfg = providers.get(active, {})
    ptype = pcfg.get("type", active)

    if ptype == "mock":
        return _mock_from_config(pcfg)

    if ptype == "serpapi":
        provider = SerpApiProvider(pcfg)
        if provider.api_key:
            return provider
        if config.allow_mock:
            return _mock_from_config(pcfg)
        raise RuntimeError("SERPAPI_API_KEY not set and MIC_ALLOW_MOCK=false")

    if ptype == "bing":
        provider = BingProvider(pcfg)
        if provider.api_key:
            return provider
        if config.allow_mock:
            return _mock_from_config(pcfg)
        raise RuntimeError("BING_SEARCH_API_KEY not set and MIC_ALLOW_MOCK=false")

    # Unknown type -> mock fallback if allowed.
    if config.allow_mock:
        return _mock_from_config(pcfg)
    raise ValueError(f"Unknown search provider type: {ptype}")
