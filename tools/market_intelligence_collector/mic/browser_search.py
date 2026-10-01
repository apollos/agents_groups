"""Browser-backed search provider (design 5.2 / 6 / 7).

``BrowserSearchProvider`` is the ``type: browser`` entry in
``search_providers.yaml``. Construction only validates its configuration;
the browser is started lazily by the ``RunContext`` on the first page load.

Two entry points:

* ``search_with_context(request, context)`` - the budgeted path used by the
  pipeline: pages, engines, hits and authenticated retries all come from the
  shared ``RunBudget`` in ``context``.
* ``search(query, ...)`` - legacy compatibility: one engine, one page, in a
  temporary context with default limits. Only intended for ``mic search
  probe`` and ad-hoc use; the pipeline always calls ``search_with_context``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from mic.browser.config import ConfigError
from mic.browser.contracts import SearchBatch, SearchRequest
from mic.browser.coordinator import (
    BrowserPageLoader,
    PageLoader,
    SearchCoordinator,
    build_coordinator,
)
from mic.budget import RunBudget
from mic.run_context import RunContext
from mic.schemas import SearchHit
from mic.search import SearchProvider
from mic.utils import new_id

logger = logging.getLogger(__name__)

KNOWN_ENGINES = ("bing", "google", "baidu")
ALLOWED_KEYS = {"type", "engine_order", "enabled_engines", "results_per_page_cap",
                "desired_relevant_articles", "max_retries_per_page", "min_engine_interval_seconds",
                "query_rewrite", "hits_per_query", "family_focus"}


def validate_browser_provider_config(pcfg: dict[str, Any]) -> dict[str, Any]:
    unknown = set(pcfg) - ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"browser search provider: unknown keys {sorted(unknown)}")
    order = list(pcfg.get("engine_order") or ["bing"])
    enabled = list(pcfg.get("enabled_engines") or order)
    for name in order + enabled:
        if name not in KNOWN_ENGINES:
            raise ConfigError(f"browser search provider: unknown engine {name!r}")
    if not [e for e in order if e in enabled]:
        raise ConfigError("browser search provider: no enabled engine in engine_order")
    if pcfg.get("query_rewrite", False):
        raise ConfigError("browser search provider: query_rewrite must be false in v1")
    for key, lo, hi in (("results_per_page_cap", 1, 50), ("desired_relevant_articles", 1, 20),
                        ("max_retries_per_page", 0, 1), ("min_engine_interval_seconds", 0, 60)):
        if key in pcfg:
            val = pcfg[key]
            if not isinstance(val, (int, float)) or isinstance(val, bool) or not (lo <= val <= hi):
                raise ConfigError(f"browser search provider: {key} must be between {lo} and {hi}")
    return {"type": "browser", "engine_order": order, "enabled_engines": [e for e in order if e in enabled],
            "results_per_page_cap": int(pcfg.get("results_per_page_cap", 10)),
            "desired_relevant_articles": int(pcfg.get("desired_relevant_articles", 2)),
            "max_retries_per_page": int(pcfg.get("max_retries_per_page", 0)),
            "min_engine_interval_seconds": float(pcfg.get("min_engine_interval_seconds", 3)),
            "query_rewrite": False, "family_focus": dict(pcfg.get("family_focus") or {})}


class BrowserSearchProvider(SearchProvider):
    name = "browser"
    browser_backed = True

    def __init__(self, pcfg: dict[str, Any] | None = None, *, provider_name: str = "browser_local",
                 sleep: Callable[[float], None] = time.sleep,
                 loader_factory: Callable[[RunContext], PageLoader] | None = None):
        self.cfg = validate_browser_provider_config(pcfg or {})
        self.name = f"browser({'+'.join(self.cfg['enabled_engines'])})"
        self.provider_name = provider_name
        self._sleep = sleep
        self._loader_factory = loader_factory or self._default_loader
        self.coordinator: SearchCoordinator = build_coordinator(self.cfg, sleep=sleep,
                                                                provider_name=provider_name)
        self.last_batch: SearchBatch | None = None

    @property
    def engines(self) -> list[str]:
        return [e.name for e in self.coordinator.engines]

    @staticmethod
    def _default_loader(context: RunContext) -> PageLoader:
        return BrowserPageLoader(context.browser(), clock=context.clock)

    def search_with_context(self, request: SearchRequest, context: RunContext) -> SearchBatch:
        loader = self._loader_factory(context)
        focus = self.cfg["family_focus"].get(request.query_family or "", None)
        batch = self.coordinator.run_query(request.query, request.query_family, request.query_id,
                                           request.limit, context, loader, family_focus=focus)
        self.last_batch = batch
        return batch

    def search(self, query: str, query_family: str | None = None, limit: int = 10) -> list[SearchHit]:
        """Compatibility path: one engine, one page, temporary default-limit context."""
        from mic.config import load_config

        cfg = load_config()
        runtime = cfg.browser_runtime
        if not cfg.browser_enabled:
            raise RuntimeError("browser_runtime.enabled is false; use `mic browser doctor` first")
        limits = dict(runtime.get("limits") or {})
        limits["max_search_pages_per_query"] = 1
        limits["max_engines_per_query"] = 1
        budget = RunBudget(limits=limits)
        context = RunContext(run_id=new_id("probe"), budget=budget, browser_runtime=runtime,
                             interaction_mode=runtime.get("interaction_mode", "unattended"))
        try:
            batch = self.search_with_context(
                SearchRequest(query=query, query_family=query_family, limit=limit), context)
        finally:
            context.close()
        return batch.hits
