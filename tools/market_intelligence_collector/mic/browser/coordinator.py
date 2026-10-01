"""Search coordinator: first page to at most three pages per query (design 6).

The coordinator owns the decision table in section 6.2. It never generates
new queries, never raises a budget and never retries the same page. Page
loading is abstracted behind ``PageLoader`` so the whole decision logic is
testable offline with HTML fixtures; ``BrowserPageLoader`` is the real
Playwright-backed implementation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from mic.browser.contracts import (
    NextPage,
    RawResult,
    SearchBatch,
    SearchPageResult,
    page_fingerprint,
)
from mic.browser.engines.base import EngineAdapter, ParsedPage, build_engine, query_match_status
from mic.browser.relevance import RelevanceDecision, judge, page_relevance
from mic.run_context import RunContext, TargetIdentity
from mic.schemas import SearchHit
from mic.utils import canonicalize_url, domain_of, new_id, now

logger = logging.getLogger(__name__)

RELEVANCE_RULES_VERSION = "relevance_rules_v1"
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "spm",
                   "from", "fr", "ref", "share_token", "msclkid", "gclid", "fbclid"}


@dataclass
class LoadedPage:
    status: str  # navigated | timeout | network_error | browser_closed | rejected
    final_url: str | None = None
    html: str | None = None
    http_status: int | None = None
    elapsed_ms: int = 0
    error: str | None = None
    ready: bool = True


class PageLoader(Protocol):
    def load(self, url: str, engine: EngineAdapter, timeout_seconds: float) -> LoadedPage: ...


class BrowserPageLoader:
    """Loads a search page in the dedicated browser session with a bounded wait."""

    def __init__(self, session, clock: Callable[[], float] = time.monotonic):
        self.session = session
        self.clock = clock

    def load(self, url: str, engine: EngineAdapter, timeout_seconds: float) -> LoadedPage:
        start = self.clock()
        with self.session.page() as page:
            nav = self.session.navigate(page, url, timeout_seconds)
            if nav["status"] != "navigated":
                return LoadedPage(status=nav["status"], final_url=nav.get("final_url"),
                                  error=nav.get("error"), elapsed_ms=nav.get("elapsed_ms", 0))
            # Bounded readiness wait on engine-specific features; never a fixed
            # sleep and never networkidle (search pages keep streaming requests).
            remaining = max(0.5, timeout_seconds - (self.clock() - start))
            ready = True
            try:
                page.wait_for_selector(", ".join(engine.ready_selectors()), state="attached",
                                       timeout=int(remaining * 1000))
            except Exception:  # noqa: BLE001 - we still parse whatever is present
                ready = False
            try:
                html = page.content()
                final_url = page.url
            except Exception as exc:  # noqa: BLE001
                return LoadedPage(status="browser_closed", error=f"{type(exc).__name__}: {exc}"[:200],
                                  elapsed_ms=int((self.clock() - start) * 1000))
            return LoadedPage(status="navigated", final_url=final_url, html=html,
                              http_status=nav.get("http_status"), ready=ready,
                              elapsed_ms=int((self.clock() - start) * 1000))


def strip_tracking(url: str) -> str:
    """Remove only known tracking params; keep article-identifying query params."""
    from urllib.parse import parse_qsl, urlencode, urlunparse
    p = urlparse(url)
    if not p.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunparse(p._replace(query=urlencode(kept, doseq=True)))


@dataclass
class QueryState:
    query: str
    query_id: str
    query_family: str | None
    seen_keys: set[str] = field(default_factory=set)
    relevant_keys: set[str] = field(default_factory=set)
    hits: list[SearchHit] = field(default_factory=list)
    page_attempts: list[SearchPageResult] = field(default_factory=list)
    visited_fingerprints: set[str] = field(default_factory=set)
    off_topic_examples: list[str] = field(default_factory=list)
    auth_retry_done_for: set[str] = field(default_factory=set)
    last_engine_load: dict[str, float] = field(default_factory=dict)


class SearchCoordinator:
    def __init__(self, engines: list[EngineAdapter], *, desired_relevant_articles: int = 2,
                 results_per_page_cap: int = 10, min_engine_interval_seconds: float = 3.0,
                 max_retries_per_page: int = 0, sleep: Callable[[float], None] = time.sleep,
                 provider_name: str = "browser"):
        if not engines:
            raise ValueError("at least one enabled engine is required")
        self.engines = engines
        self.desired = int(desired_relevant_articles)
        self.cap = int(results_per_page_cap)
        self.min_interval = float(min_engine_interval_seconds)
        self.max_retries_per_page = int(max_retries_per_page)  # kept 0 in v1; documented knob
        self.sleep = sleep
        self.provider_name = provider_name

    # --- public ------------------------------------------------------------

    def run_query(self, query: str, query_family: str | None, query_id: str | None, limit: int,
                  context: RunContext, loader: PageLoader,
                  family_focus: list[str] | None = None) -> SearchBatch:
        qid = query_id or new_id("q")
        state = QueryState(query=query, query_id=qid, query_family=query_family)
        identity = context.identity or TargetIdentity(target_id="", canonical_name="")
        budget = context.budget
        engine_idx = 0
        next_page: NextPage | None = None
        page_index = 1
        stop_reason: str | None = None
        outcome = "completed"

        while True:
            if engine_idx >= len(self.engines):
                stop_reason = stop_reason or "engines_exhausted"
                break
            engine = self.engines[engine_idx]
            ok, reason = budget.can_open_search_page(qid, engine.name)
            if not ok:
                stop_reason = reason
                outcome = "budget_exhausted" if reason not in ("cancelled",) else "cancelled"
                break
            url = next_page.href if (next_page and next_page.href) else engine.search_url(query)
            self._respect_interval(engine.name, state, context)
            page = self._open_page(engine, url, page_index, state, context, loader, identity, family_focus)
            state.page_attempts.append(page)

            # --- decision table (6.2) -------------------------------------
            status = page.status
            if status in ("captcha", "login_required"):
                recovered = self._try_authenticated_recovery(engine, url, page_index, state, context,
                                                             loader, identity, family_focus, page)
                if recovered is not None:
                    state.page_attempts.append(recovered)
                    page, status = recovered, recovered.status
                if status in ("captcha", "login_required"):
                    outcome = "blocked"
                    engine_idx, next_page, page_index = engine_idx + 1, None, 1
                    stop_reason = status
                    continue
            if status == "consent_required":
                # Never fabricate consent via cookies; interactive mode would hand
                # this to the operator, unattended mode moves to the next engine.
                outcome = "blocked"
                stop_reason = "consent_required"
                engine_idx, next_page, page_index = engine_idx + 1, None, 1
                continue
            if status in ("no_results",):
                stop_reason = "no_results"
                engine_idx, next_page, page_index = engine_idx + 1, None, 1
                continue
            if status in ("parse_error", "query_mismatch", "network_error", "timeout", "rejected",
                          "not_ready"):
                outcome = "partial" if state.hits else "failed"
                stop_reason = status
                engine_idx, next_page, page_index = engine_idx + 1, None, 1
                continue
            if status == "browser_closed":
                outcome = "failed"
                stop_reason = "browser_closed"
                break
            if status == "duplicate_page":
                stop_reason = "duplicate_page"
                engine_idx, next_page, page_index = engine_idx + 1, None, 1
                continue

            # status == ok
            outcome = "completed"
            if len(state.relevant_keys) >= self.desired:
                stop_reason = "desired_relevant_reached"
                break
            new_unique = int(page.quality.get("unique_new_count", 0))
            has_target = int(page.quality.get("target_match_count", 0)) > 0
            if new_unique == 0:
                stop_reason = "no_new_links"
                engine_idx, next_page, page_index = engine_idx + 1, None, 1
                continue
            if not has_target:
                stop_reason = "no_target_match"
                engine_idx, next_page, page_index = engine_idx + 1, None, 1
                continue
            candidate = engine.validate_next_page(page.next_page, query, page_index, state.visited_fingerprints)
            if candidate is None:
                stop_reason = "no_next_page"
                engine_idx, next_page, page_index = engine_idx + 1, None, 1
                continue
            next_page = candidate
            page_index = candidate.expected_page_index or (page_index + 1)
            stop_reason = None

        if not state.hits and outcome == "completed":
            outcome = "empty"
        if state.hits and outcome in ("failed", "blocked"):
            outcome = "partial"
        hits = state.hits[:limit] if limit else state.hits
        quality = {
            "relevance_rules_version": RELEVANCE_RULES_VERSION,
            "relevant_article_count": len(state.relevant_keys),
            "unique_count": len(state.seen_keys),
            "pages_opened": len(state.page_attempts),
            "engines_used": sorted({p.engine for p in state.page_attempts}),
            "off_topic_examples": state.off_topic_examples[:5],
            "desired_relevant_articles": self.desired,
        }
        return SearchBatch(query=query, query_family=query_family, provider=self.provider_name, hits=hits,
                           page_attempts=state.page_attempts, outcome=outcome, stop_reason=stop_reason,
                           quality=quality)

    # --- internals -----------------------------------------------------------

    def _respect_interval(self, engine_name: str, state: QueryState, context: RunContext) -> None:
        last = state.last_engine_load.get(engine_name)
        if last is None or self.min_interval <= 0:
            return
        wait = self.min_interval - (context.clock() - last)
        if wait > 0:
            wait = min(wait, context.budget.remaining_seconds())
            if wait > 0:
                self.sleep(wait)

    def _open_page(self, engine: EngineAdapter, url: str, page_index: int, state: QueryState,
                   context: RunContext, loader: PageLoader, identity: TargetIdentity,
                   family_focus: list[str] | None, *, authenticated_retry: bool = False) -> SearchPageResult:
        budget = context.budget
        budget.reserve_search_page(state.query_id, engine.name)
        attempt_id = new_id("spa")
        auth = context.auth_context()
        page = SearchPageResult(
            engine=engine.name, adapter_version=engine.adapter_version, query_requested=state.query,
            page_index=page_index, page_attempt_id=attempt_id, requested_url=url,
            started_at=now().isoformat(), auth_mode=auth.get("auth_mode", "anonymous"),
            auth_context_id=auth.get("auth_context_id"), authenticated_retry=authenticated_retry,
        )
        handle = context.recorder.page_started({**page.to_record(), "query_id": state.query_id})
        state.last_engine_load[engine.name] = context.clock()
        try:
            loaded = loader.load(url, engine, budget.page_timeout_seconds())
        except Exception as exc:  # noqa: BLE001 - loader failures are page failures
            loaded = LoadedPage(status="network_error", error=f"{type(exc).__name__}: {exc}"[:200])
        page.final_url = loaded.final_url
        page.diagnostics.update({"load_status": loaded.status, "http_status": loaded.http_status,
                                 "elapsed_ms": loaded.elapsed_ms, "load_error": loaded.error,
                                 "ready": loaded.ready})
        if loaded.status != "navigated" or loaded.html is None:
            page.status = loaded.status if loaded.status != "navigated" else "network_error"
            page.error_code = page.status
        else:
            self._parse_into(engine, loaded, page, state, context, identity, family_focus)
        page.finished_at = now().isoformat()
        context.recorder.page_finished(handle, {**page.to_record(), "query_id": state.query_id})
        return page

    def _parse_into(self, engine: EngineAdapter, loaded: LoadedPage, page: SearchPageResult,
                    state: QueryState, context: RunContext, identity: TargetIdentity,
                    family_focus: list[str] | None) -> None:
        if loaded.final_url and not engine.is_engine_url(loaded.final_url):
            page.status = "network_error"
            page.error_code = "off_engine_redirect"
            page.diagnostics["final_host"] = urlparse(loaded.final_url).hostname
            return
        try:
            parsed: ParsedPage = engine.parse(loaded.html or "", loaded.final_url, state.query, self.cap)
        except Exception as exc:  # noqa: BLE001 - adapter bug is an explicit parse_error
            page.status, page.error_code = "parse_error", "parse_error"
            page.diagnostics["parse_exception"] = f"{type(exc).__name__}: {exc}"[:200]
            return
        page.query_observed = parsed.query_observed
        page.query_match_status = query_match_status(state.query, parsed.query_observed)
        page.diagnostics.update(parsed.diagnostics)
        if parsed.status != "ok":
            page.status = parsed.status
            page.error_code = parsed.error_code or (parsed.status if parsed.status not in ("no_results",) else None)
            return
        if page.query_match_status == "mismatch":
            page.status, page.error_code = "query_mismatch", "query_mismatch"
            page.diagnostics["result_count_discarded"] = len(parsed.results)
            return

        locators = [r.url or r.raw_href for r in parsed.results]
        page.page_fingerprint = page_fingerprint(locators)
        if page.page_fingerprint in state.visited_fingerprints:
            page.status, page.error_code = "duplicate_page", "duplicate_page"
            page.quality = {"result_count": len(parsed.results), "unique_new_count": 0}
            return
        state.visited_fingerprints.add(page.page_fingerprint)

        decisions: list[RelevanceDecision] = []
        new_unique = 0
        for raw in parsed.results:
            decision = judge(raw, state.query, identity, family_focus)
            decisions.append(decision)
            key = self._discovery_key(raw)
            is_new = key not in state.seen_keys
            if is_new:
                if not context.budget.can_accept_hit(state.query_id):
                    page.diagnostics["hits_truncated_by_budget"] = True
                    break
                state.seen_keys.add(key)
                new_unique += 1
                context.budget.accept_hit(state.query_id)
                hit = self._to_hit(raw, page, state, decision)
                state.hits.append(hit)
                if decision.relevant:
                    state.relevant_keys.add(key)
                elif len(state.off_topic_examples) < 5 and raw.title:
                    state.off_topic_examples.append(raw.title[:80])
        rel = page_relevance(decisions)
        page.quality = {**rel, "result_count": len(parsed.results), "unique_new_count": new_unique,
                        "relevant_article_count_total": len(state.relevant_keys),
                        "relevance_rules_version": RELEVANCE_RULES_VERSION}
        page.relevance = "ok" if rel["target_match_count"] > 0 else "low"
        page.next_page = parsed.next_page
        page.status = "ok"

    @staticmethod
    def _discovery_key(raw: RawResult) -> str:
        if raw.url:
            return "url:" + canonicalize_url(strip_tracking(raw.url))
        return "pending:" + raw.raw_href

    def _to_hit(self, raw: RawResult, page: SearchPageResult, state: QueryState,
                decision: RelevanceDecision) -> SearchHit:
        url = strip_tracking(raw.url) if raw.url else raw.raw_href
        resolution = raw.url_resolution if raw.url else "pending"
        return SearchHit(
            query=state.query, title=raw.title, snippet=raw.snippet, url=url,
            domain=domain_of(url) if raw.url else "", rank=len(state.hits) + 1,
            # Per-hit provider tag names the engine (design: browser:bing / browser:baidu);
            # the batch keeps the configured provider name.
            provider=f"browser:{page.engine}", query_family=state.query_family,
            publish_time_guess=raw.date_text,
            discovery={
                "engine": page.engine, "page_index": page.page_index, "rank_in_page": raw.rank_in_page,
                "page_attempt_id": page.page_attempt_id, "adapter_version": page.adapter_version,
                "retrieved_at": page.started_at, "raw_href": raw.raw_href, "url_resolution": resolution,
                "display_url": raw.display_url, "date_text": raw.date_text, "result_kind": raw.result_kind,
                "relevance": decision.as_dict(), "relevance_rules_version": RELEVANCE_RULES_VERSION,
                "query_id": state.query_id,
            },
        )

    def _try_authenticated_recovery(self, engine: EngineAdapter, url: str, page_index: int,
                                    state: QueryState, context: RunContext, loader: PageLoader,
                                    identity: TargetIdentity, family_focus: list[str] | None,
                                    blocked_page: SearchPageResult) -> SearchPageResult | None:
        """One budgeted retry with a user-authorised session for this origin (design 10.3)."""
        store = context.session_store
        if store is None:
            return None
        origin = store.origin_of(url)
        if origin in state.auth_retry_done_for:
            return None
        ok, reason = context.budget.can_authenticated_retry(origin)
        if not ok:
            blocked_page.diagnostics["auth_retry_skipped"] = reason
            return None
        ok_page, page_reason = context.budget.can_open_search_page(state.query_id, engine.name)
        if not ok_page:
            blocked_page.diagnostics["auth_retry_skipped"] = page_reason
            return None
        exclude = {blocked_page.auth_context_id} if blocked_page.auth_mode == "imported_cookie" else set()
        cred = store.credential_for(origin, exclude_versions=exclude)
        if cred is None:
            blocked_page.diagnostics["auth_retry_skipped"] = "no_credential"
            return None
        state.auth_retry_done_for.add(origin)
        try:
            context.budget.reserve_authenticated_retry(origin)
            context.browser().add_cookies(cred["cookies"], cred["credential_id"], cred["version"])
        except Exception as exc:  # noqa: BLE001
            blocked_page.diagnostics["auth_retry_error"] = f"{type(exc).__name__}: {exc}"[:200]
            return None
        finally:
            cred["cookies"] = None  # drop the values from this frame as early as possible
        return self._open_page(engine, url, page_index, state, context, loader, identity, family_focus,
                               authenticated_retry=True)


def build_coordinator(provider_cfg: dict[str, Any], *, sleep: Callable[[float], None] = time.sleep,
                      provider_name: str = "browser") -> SearchCoordinator:
    order = list(provider_cfg.get("engine_order") or ["bing"])
    enabled = set(provider_cfg.get("enabled_engines") or order)
    engines = [build_engine(name, provider_cfg) for name in order if name in enabled]
    return SearchCoordinator(
        engines,
        desired_relevant_articles=int(provider_cfg.get("desired_relevant_articles", 2)),
        results_per_page_cap=int(provider_cfg.get("results_per_page_cap", 10)),
        min_engine_interval_seconds=float(provider_cfg.get("min_engine_interval_seconds", 3)),
        max_retries_per_page=int(provider_cfg.get("max_retries_per_page", 0)),
        sleep=sleep, provider_name=provider_name,
    )
