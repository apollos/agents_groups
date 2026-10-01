"""TEST DOUBLES for the browser route (design 16.1).

Everything here is a stand-in used by the offline suite: a fake clock, an
HTML-fixture page loader, a fake Playwright backend and a fake browser
session. None of it talks to a real browser or network, and nothing here is
evidence of real online acceptance.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mic.browser.coordinator import LoadedPage
from mic.budget import DEFAULT_LIMITS, RunBudget
from mic.run_context import RunContext, TargetIdentity

FIXTURES = Path(__file__).parent / "fixtures" / "search_pages"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def browser_runtime_block(**overrides) -> dict:
    """A valid, enabled browser_runtime block for tests (no real paths needed)."""
    block = {
        "version": 1, "enabled": True, "channel": "msedge", "headless": False,
        "profile_dir_env": "MIC_BROWSER_PROFILE_DIR", "credential_dir_env": "MIC_BROWSER_CREDENTIAL_DIR",
        "interaction_mode": "unattended", "max_open_pages": 2,
        "session_fallback": {"enabled": False, "allowed_origins": [], "cookie_sources": {},
                             "default_max_age_seconds": 86400, "apply_to_http_client": False},
        "limits": {"max_queries": 2, "max_search_pages_per_query": 3, "max_search_pages_per_run": 6,
                   "max_engines_per_query": 2, "max_links_to_read": 2, "max_http_read_attempts": 2,
                   "max_browser_read_attempts": 2, "max_model_calls": 3, "max_gateway_requests": 3,
                   "max_run_seconds": 300, "max_page_seconds": 25},
        "cache": {"reuse_analysis": False},
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and isinstance(block.get(key), dict):
            block[key] = {**block[key], **val}
        else:
            block[key] = val
    return block


def browser_config(active: str = "browser_local", provider: dict | None = None, runtime: dict | None = None,
                   fallback=None):
    """Load the repo config and switch it to the browser route (in-memory only)."""
    import copy

    from mic.config import load_config, validate_search_provider_config

    cfg = load_config()
    cfg.raw = copy.deepcopy(cfg.raw)
    cfg.set_browser_runtime(runtime or browser_runtime_block())
    sp = cfg.raw["search_providers"]
    sp["active"] = active
    sp["fallback"] = fallback if fallback is not None else []
    if provider is not None:
        sp["providers"]["browser_local"] = provider
    validate_search_provider_config(cfg)
    return cfg


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def catl_identity() -> TargetIdentity:
    return TargetIdentity(target_id="company_300750", canonical_name="宁德时代新能源科技股份有限公司",
                          aliases=["宁德时代", "CATL"], tickers=["300750"],
                          official_domains=["catl.com"],
                          association_terms=["特斯拉", "比亚迪", "动力电池", "碳酸锂"])


def make_context(limits: dict | None = None, clock: FakeClock | None = None,
                 identity: TargetIdentity | None = None, deadline_seconds: float | None = None,
                 session_store: Any = None, browser_enabled: bool = True) -> RunContext:
    clock = clock or FakeClock()
    merged = {**DEFAULT_LIMITS, **(limits or {})}
    deadline = clock() + (deadline_seconds if deadline_seconds is not None else merged["max_run_seconds"])
    budget = RunBudget(limits=merged, clock=clock, deadline_at=deadline)
    ctx = RunContext(run_id="run_test", attempt_id="attempt_test", identity=identity or catl_identity(),
                     budget=budget, browser_runtime={"enabled": browser_enabled, "max_open_pages": 2,
                                                     "cache": {"reuse_analysis": False}},
                     session_store=session_store, clock=clock)
    return ctx


class FixtureLoader:
    """Maps requested URLs to LoadedPage outcomes. Records every URL opened."""

    def __init__(self, routes: dict[str, Any] | None = None, default: Any = None,
                 clock: FakeClock | None = None, per_load_seconds: float = 0.0):
        self.routes = routes or {}
        self.default = default
        self.opened: list[str] = []
        self.clock = clock
        self.per_load_seconds = per_load_seconds

    def load(self, url: str, engine, timeout_seconds: float) -> LoadedPage:
        self.opened.append(url)
        if self.clock is not None and self.per_load_seconds:
            self.clock.advance(self.per_load_seconds)
        outcome = None
        for key, val in self.routes.items():
            if callable(key):
                if key(url):
                    outcome = val
                    break
            elif key == url or (key.startswith("first=") and key in url) or \
                    (key.startswith("host:") and (urlparse(url).hostname or "") == key[5:]):
                outcome = val
                break
        if outcome is None:
            outcome = self.default
        if outcome is None:
            return LoadedPage(status="network_error", error="no route")
        if callable(outcome):
            outcome = outcome(url)
        if isinstance(outcome, LoadedPage):
            return outcome
        if isinstance(outcome, str):
            return LoadedPage(status="navigated", final_url=url, html=outcome, http_status=200)
        raise TypeError(f"bad route value for {url}: {type(outcome)}")


def page_index_of(url: str) -> int:
    qs = parse_qs(urlparse(url).query)
    first = qs.get("first", ["1"])[0]
    return (int(first) - 1) // 10 + 1


class FakePage:
    def __init__(self, session: FakeBrowserSession):
        self.session = session
        self.url = "about:blank"
        self._closed = False
        self._html = ""
        self._title = ""

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    def content(self) -> str:
        return self._html

    def title(self) -> str:
        return self._title

    def wait_for_selector(self, *_a, **_k) -> None:
        return None

    def wait_for_load_state(self, *_a, **_k) -> None:
        return None

    def evaluate(self, *_a, **_k) -> dict:
        return {"body_text_length": len(self._html), "ready_state": "complete"}


class FakeBrowserSession:
    """Stands in for mic.browser.session.BrowserSession (no Playwright)."""

    def __init__(self, pages: dict[str, Any] | None = None, fail_start: Exception | None = None,
                 clock: Callable[[], float] | None = None):
        self.pages = pages or {}
        self.fail_start = fail_start
        self.started = False
        self.closed = False
        self.navigations: list[str] = []
        self.cookies_added: list[tuple[str, str, int]] = []
        self.cleared_domains: list[str] = []
        self.credential_versions: dict[str, str] = {}
        self.clock = clock or (lambda: 0.0)
        self.close_result = {"browser_started": True, "cleanup": "complete"}
        self.profile_dir = Path("/nonexistent/mic-edge")

    def start(self):
        if self.fail_start is not None:
            raise self.fail_start
        self.started = True
        return self

    def close(self) -> dict[str, Any]:
        self.closed = True
        self.started = False
        return dict(self.close_result)

    @contextmanager
    def page(self):
        page = FakePage(self)
        try:
            yield page
        finally:
            page.close()

    def navigate(self, page: FakePage, url: str, timeout_seconds: float) -> dict[str, Any]:
        self.navigations.append(url)
        spec = self.pages.get(url)
        if spec is None:
            return {"status": "network_error", "error": "no fake route", "final_url": None, "elapsed_ms": 1}
        if isinstance(spec, dict) and spec.get("status") not in (None, "navigated"):
            return {"status": spec["status"], "error": spec.get("error"), "final_url": spec.get("final_url"),
                    "elapsed_ms": 1}
        if isinstance(spec, str):
            spec = {"html": spec}
        page.url = spec.get("final_url", url)
        page._html = spec.get("html", "")
        page._title = spec.get("title", "")
        return {"status": "navigated", "http_status": spec.get("http_status", 200), "final_url": page.url,
                "elapsed_ms": 1, "error": None}

    def add_cookies(self, cookies, credential_id: str, version: str) -> None:
        self.cookies_added.append((credential_id, version, len(cookies)))
        self.credential_versions[credential_id] = version

    def clear_cookies_for_domains(self, domains) -> int:
        self.cleared_domains.extend(domains)
        return len(domains)

    def auth_context(self) -> dict[str, Any]:
        mode = "imported_cookie" if self.credential_versions else "profile"
        return {"auth_mode": mode, "auth_context_id": "ctx-" + "-".join(sorted(self.credential_versions.values()))
                or "ctx-profile", "credential_versions": dict(self.credential_versions)}

    def describe(self) -> dict[str, Any]:
        return {"profile_id": "fake", "started": self.started, **self.auth_context()}
