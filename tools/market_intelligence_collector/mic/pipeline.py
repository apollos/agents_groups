"""Pipeline runner (spec section 18) + Batch Report (section 19).

Orchestrates the full run:
  target/task -> query plan -> search -> dedup -> triage -> read ->
  passage selection -> model call planning -> validation -> merge ->
  persist structured results -> batch report.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mic.browser.contracts import SearchBatch, SearchRequest
from mic.budget import DEFAULT_LIMITS, BudgetExceeded, RunBudget, merge_limits
from mic.config import MICConfig, load_config
from mic.logging_utils import get_logger, setup_logging
from mic.merge import ModelContribution, MultiModelMerger
from mic.modeling.adapter import ModelRegistry
from mic.modeling.call_planner import CallBudget, LinkModelResult, ModelCallPlanner
from mic.modeling.vision import VisionExtractor
from mic.planner import QueryPlanner
from mic.profile import TargetProfile
from mic.publication_time import PublicationWindow
from mic.reader import LinkReader
from mic.run_context import RunContext, TargetIdentity, config_fingerprint
from mic.schemas import CoverageGap, SearchHit, TriageResult
from mic.search import build_search_provider
from mic.store import Repository, get_database
from mic.triage import SearchHitTriage
from mic.utils import canonicalize_url, domain_of, new_id
from mic.validate import BundleValidator

logger = get_logger("pipeline")

# Browser route read gate signals (see ``CollectionPipeline._apply_read_gate``).
READ_GATE_SIGNAL_PREFIX = "read_gate:"
RELATED_ONLY_SIGNAL = "related_entity_only"


class RunCancelled(RuntimeError):
    """Raised inside the run loop when the caller cancelled or the deadline passed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class RunStats:
    queries_generated: int = 0
    queries_executed: int = 0
    queries_skipped_by_hit_budget: int = 0
    # Design 9/15: explicit attempt counters (legacy ``queries_executed`` kept).
    queries_attempted: int = 0
    queries_completed: int = 0
    search_page_attempts: int = 0
    search_api_requests: int = 0
    http_read_attempts: int = 0
    browser_read_attempts: int = 0
    authenticated_retries: int = 0
    gateway_requests_sent: int = 0
    links_selected_for_read: int = 0
    # Browser route read gate (design 6.3): read candidates demoted by the page relevance
    # rules, keyed by reason, plus candidates kept only as related-company information.
    read_gate_demoted: dict[str, int] = field(default_factory=dict)
    read_gate_related_only: int = 0
    search_outcomes: dict[str, int] = field(default_factory=dict)
    search_errors: list[dict] = field(default_factory=list)
    read_failures: dict[str, int] = field(default_factory=dict)
    time_window_filter: dict[str, Any] = field(default_factory=dict)
    output_decisions: list[dict[str, Any]] = field(default_factory=list)
    # One entry per model request actually attempted (A4 traceability).
    model_requests: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    execution_status: str = "completed"
    search_hits: int = 0
    unique_source_links: int = 0
    deduplicated_links: int = 0
    links_read: int = 0
    links_model_analyzed: int = 0
    model_calls: int = 0
    parallel_ensemble_calls: int = 0
    fallback_calls: int = 0
    cascade_calls: int = 0
    arbitration_calls: int = 0
    split_extractions: int = 0
    batch_triaged_hits: int = 0
    batch_triage_calls: int = 0
    vision_calls: int = 0
    cached_or_reused_results: int = 0
    estimated_model_cost: float = 0.0
    passage_selection_saved_chars: int = 0
    log_file: str | None = None
    structured: dict[str, int] = field(default_factory=lambda: {
        "briefs": 0, "facts": 0, "metrics": 0, "events": 0, "relations": 0,
        "risks": 0, "catalysts": 0, "customer_supplier_signals": 0,
        "price_cost_margin_signals": 0, "policy_signals": 0,
        "analyst_questions": 0, "coverage_gaps": 0})
    top_events: list[dict] = field(default_factory=list)
    top_relations: list[dict] = field(default_factory=list)


class Pipeline:
    def __init__(self, config: MICConfig | None = None):
        self.config = config or load_config()
        self.repo = Repository(get_database(self.config.database_url))
        self.planner = QueryPlanner(self.config)
        self.search = build_search_provider(self.config)
        self.triage = SearchHitTriage(self.config)
        self.registry = ModelRegistry(self.config)
        self.vision = VisionExtractor(self.config, self.registry)
        self.reader = LinkReader(self.config, search_provider=self.search,
                                 vision=self.vision)
        self.merger = MultiModelMerger(self.config)
        self.validator = BundleValidator((self.config.output_schema or {}).get("limits", {}))
        self.policy_version = self.config.model_policies.get("version", "model_policy_v0.3")
        self.query_plan_version = self.config.query_families.get("version", "query_plan_v0.3")
        # Per-query SERP request cap; providers additionally apply their own
        # hits_per_query limit.
        self._hits_per_query = (self.config.search_providers or {}).get(
            "max_hits_per_query", 12)
        self._cancel_check: Callable[[], bool] | None = None

    # --- public ------------------------------------------------------------

    def collect_intelligence(self, target_id: str, task_profile: dict[str, Any],
                             model_policy_version: str | None = None,
                             query_plan_version: str | None = None,
                             run_options: dict[str, Any] | None = None) -> dict:
        """Run one collection.

        ``run_options`` (all optional, design 8/9): ``deadline_seconds`` (shared
        deadline, combined with the configured ``max_run_seconds`` by min),
        ``cancel_check`` (callable returning True to stop), ``attempt_id``,
        ``task_key``, ``artifact_dir``, ``clock`` (monotonic clock for tests),
        ``browser_factory`` (test double for the browser session).
        """
        run_options = dict(run_options or {})
        window = PublicationWindow.from_value(task_profile.get("time_window"))
        profile_cfg = self.config.get_target_profile(target_id)
        if profile_cfg is None:
            raise ValueError(f"Unknown target_id: {target_id}")
        # Version pinning (spec 20.1): only one config version is loaded per
        # process, so a mismatching pin is an error rather than a silent ignore.
        if model_policy_version and model_policy_version != self.policy_version:
            raise ValueError(
                f"model_policy_version {model_policy_version!r} not loaded "
                f"(active: {self.policy_version!r})")
        if query_plan_version and query_plan_version != self.query_plan_version:
            raise ValueError(
                f"query_plan_version {query_plan_version!r} not loaded "
                f"(active: {self.query_plan_version!r})")
        profile = TargetProfile.from_config(profile_cfg)
        self.repo.upsert_target_profile(profile_cfg)

        # Feedback-driven weights (spec 22.2). Loaded once per run.
        self._model_feedback = self.repo.model_feedback_scores()
        self._family_feedback = self.repo.family_feedback_weights()
        self._source_feedback = self.repo.source_type_feedback_weights()

        self.triage.for_profile(profile).set_source_feedback(self._source_feedback)

        budget_profile = task_profile.get("budget_profile", {})
        gov = (self.config.call_governance or {}).get("budgets", {})
        run_calls = budget_profile.get("max_model_calls", gov.get("max_model_calls_per_run", 30))

        run_id = self.repo.create_search_run(
            target_id, task_profile, budget_profile,
            self.query_plan_version, self.policy_version)
        _, log_path = setup_logging(run_id, console=False)
        stats = RunStats(log_file=str(log_path) if log_path else None)
        stats.time_window_filter = {**window.describe(), "passed": 0,
                                    "filtered_by_reason": {}, "filtered_links": []}
        context = self._build_context(run_id, profile, budget_profile, run_calls, run_options)
        self.registry.set_budget(context.budget)
        # The call planner is created from the *effective* limits (deployment ceiling and
        # task request merged by min) - review: building it from the task value alone let a
        # task asking for 3 calls run 3 against a deployment max_model_calls of 1.
        run_calls = int(context.budget.limits.get("max_model_calls", run_calls))
        call_budget = CallBudget(
            max_model_calls_per_run=run_calls,
            max_model_calls_per_source_link=gov.get("max_model_calls_per_source_link", 3),
            max_parallel_model_groups_per_run=gov.get("max_parallel_model_groups_per_run", 5),
            max_batch_triage_calls=gov.get("max_batch_triage_calls", max(1, run_calls // 6)),
        )
        call_planner = ModelCallPlanner(self.config, self.registry, call_budget)

        logger.info("collect_start run_id=%s target_id=%s attempt_id=%s browser=%s",
                    run_id, target_id, context.attempt_id, self._browser_run)
        cleanup: dict[str, Any] = {}
        try:
            try:
                self._execute(run_id, profile, task_profile, call_planner, stats, context, window)
            except RunCancelled as exc:
                stats.execution_status = "timed_out" if exc.reason == "run_deadline" else "cancelled"
                stats.stop_reason = exc.reason
                logger.warning("collect_stopped run_id=%s reason=%s", run_id, exc.reason)
            finally:
                cleanup = self._close_context(context)
                self.registry.set_budget(None)
            # Final verdict after wrap-up (browser closed) and before the report is published:
            # a cancel that arrived during teardown, or wrap-up pushing the run past its hard
            # limit, must not be reported as completed / usable (review).
            self._final_status_check(stats, context)
            summary = self._summary(run_id, target_id, task_profile, stats, context, cleanup)
            status = "completed" if stats.execution_status == "completed" else stats.execution_status
            self.repo.finish_search_run(run_id, status, summary)
            logger.info("collect_%s run_id=%s summary=%s", status, run_id, summary.get("summary", {}))
            return summary
        except Exception as exc:  # noqa: BLE001
            logger.exception("collect_failed run_id=%s target_id=%s", run_id, target_id)
            if not cleanup:
                cleanup = self._close_context(context)
                self.registry.set_budget(None)
            self.repo.finish_search_run(
                run_id, "failed", {"error": str(exc), "log_file": stats.log_file,
                                   "collection_diagnostics": {
                                       "execution_status": "failed",
                                       "error_code": getattr(exc, "code", type(exc).__name__),
                                       "budget_used": context.budget.used_summary(),
                                       "cleanup": cleanup}})
            raise

    # --- run context -----------------------------------------------------------

    @property
    def _browser_run(self) -> bool:
        return bool(getattr(self.search, "browser_backed", False))

    def _build_context(self, run_id: str, profile: TargetProfile, budget_profile: dict,
                       run_calls: int, run_options: dict[str, Any]) -> RunContext:
        clock: Callable[[], float] = run_options.get("clock") or time.monotonic
        runtime = self.config.browser_runtime
        if self._browser_run:
            limits = merge_limits(runtime.get("limits") or {}, budget_profile)
        else:
            # Legacy (API) providers keep their historical budget semantics; the
            # RunBudget only tracks model/gateway counts and the deadline.
            big = 10 ** 9
            limits = {**{k: big for k in DEFAULT_LIMITS}, "max_search_pages_per_run": 0,
                      "max_browser_read_attempts": 0, "max_authenticated_retries_per_run": 0,
                      "max_model_calls": run_calls,
                      "max_gateway_requests": int(budget_profile.get("max_gateway_requests", big)),
                      "max_run_seconds": int(budget_profile.get("max_run_seconds",
                                                                DEFAULT_LIMITS["max_run_seconds"] * 24)),
                      "max_page_seconds": DEFAULT_LIMITS["max_page_seconds"]}
        deadline = run_options.get("deadline_seconds")
        run_seconds = float(limits["max_run_seconds"])
        if deadline is not None:
            run_seconds = min(run_seconds, float(deadline))
        budget = RunBudget(limits=limits, clock=clock, deadline_at=clock() + run_seconds)
        session_store = None
        if self._browser_run and (runtime.get("session_fallback") or {}).get("enabled"):
            try:
                from mic.browser.session_fallback import SessionStore
                session_store = SessionStore.from_runtime(runtime)
            except Exception as exc:  # noqa: BLE001 - fallback is optional
                logger.warning("session_store_unavailable error=%s", exc)
        context = RunContext(
            run_id=run_id, attempt_id=run_options.get("attempt_id") or new_id("attempt"),
            config_fingerprint=config_fingerprint(
                self.config.search_providers.get("active"), runtime.get("limits"),
                (self.config.output_schema or {}).get("limits", {}).get("strict_evidence_review")),
            artifact_dir=run_options.get("artifact_dir"),
            identity=TargetIdentity.from_profile(profile), budget=budget,
            browser_runtime=runtime, interaction_mode=runtime.get("interaction_mode", "unattended"),
            session_store=session_store, clock=clock,
        )
        if run_options.get("browser_factory") is not None:
            context.set_browser_factory(run_options["browser_factory"])
        cancel_check = run_options.get("cancel_check")
        self._cancel_check: Callable[[], bool] | None = cancel_check if callable(cancel_check) else None
        # Every budget gate (search page, read attempt, gateway send) polls the same signal.
        budget.cancel_check = self._cancel_check
        context.recorder.bind(
            lambda rec: self.repo.start_search_page_attempt(run_id, rec),
            lambda handle, rec: self.repo.finish_search_page_attempt(handle, rec))
        return context

    def _close_context(self, context: RunContext) -> dict[str, Any]:
        try:
            return context.close()
        except Exception as exc:  # noqa: BLE001 - report, never mask the run result
            logger.exception("browser_close_failed run_id=%s", context.run_id)
            return {"browser_started": True, "cleanup": "cleanup_incomplete", "error": str(exc)[:200]}
        finally:
            try:
                self.search.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.repo.mark_interrupted_page_attempts(context.run_id)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _apply_read_gate(hit: SearchHit, tri: TriageResult, stats: RunStats) -> TriageResult:
        """Browser route read gate (design 6.3 + review R7).

        The page relevance rules already classified the hit (``discovery.relevance``); here they
        only *demote* ``read`` candidates, never promote them, and never change the triage score:

        * ``site_ok`` / ``content_form_ok`` false -> ``link_record_only`` (wrong site or a
          non-article form such as a search listing / tag page);
        * ``target_match`` false and the legacy triage saw no entity at all -> ``link_record_only``;
        * ``target_match`` false but a related company (customer / supplier / competitor from the
          target profile) is named -> stays readable, tagged ``related_entity_only`` so it is
          ordered behind target-identity candidates in the read queue.
        """
        if tri.triage_decision != "read":
            return tri
        rel = (hit.discovery or {}).get("relevance")
        if not isinstance(rel, dict):
            return tri
        demote_reason: str | None = None
        if rel.get("site_ok") is False:
            demote_reason = "site_rule"
        elif rel.get("content_form_ok") is False:
            demote_reason = "content_form"
        elif rel.get("target_match") is False and "target_entity_match" not in tri.matched_signals:
            demote_reason = "no_target_or_related_entity"
        if demote_reason is not None:
            signal_name = f"{READ_GATE_SIGNAL_PREFIX}{demote_reason}"
            if signal_name in tri.matched_signals:
                # Already gated once and re-promoted afterwards (batch triage): demote again
                # without counting or tagging twice.
                return tri.model_copy(update={"triage_decision": "link_record_only", "need_model": False})
            stats.read_gate_demoted[demote_reason] = stats.read_gate_demoted.get(demote_reason, 0) + 1
            return tri.model_copy(update={
                "triage_decision": "link_record_only",
                "need_model": False,
                "matched_signals": [*tri.matched_signals, signal_name],
                "reason": f"{tri.reason}; read gate: {demote_reason}".strip("; "),
            })
        if rel.get("target_match") is False:
            if RELATED_ONLY_SIGNAL in tri.matched_signals:
                return tri
            stats.read_gate_related_only += 1
            return tri.model_copy(update={
                "matched_signals": [*tri.matched_signals, RELATED_ONLY_SIGNAL],
                "reason": f"{tri.reason}; related company only (target not named)".strip("; "),
            })
        return tri

    @staticmethod
    def _final_status_check(stats: RunStats, context: RunContext) -> None:
        if stats.execution_status != "completed":
            return
        budget = context.budget
        if budget.poll_cancel():
            stats.execution_status, stats.stop_reason = "cancelled", budget.cancel_reason or "cancelled"
        elif budget.deadline_at is not None and budget.expired():
            stats.execution_status, stats.stop_reason = "timed_out", "run_deadline"
        else:
            return
        logger.warning("collect_stopped_at_wrapup run_id=%s reason=%s elapsed=%.1fs",
                       context.run_id, stats.stop_reason, budget.elapsed_seconds())

    def _check_alive(self, context: RunContext) -> None:
        try:
            context.check_alive()  # polls the external cancel signal, then the deadline
        except BudgetExceeded as exc:
            raise RunCancelled("run_deadline" if exc.counter == "max_run_seconds" else exc.counter) from exc

    # --- core --------------------------------------------------------------

    def _execute(self, run_id: str, profile: TargetProfile, task_profile: dict,
                 call_planner: ModelCallPlanner, stats: RunStats, context: RunContext,
                 window: PublicationWindow) -> None:
        self.vision.reset_run()
        if getattr(self, "validator", None) is not None:
            # Statement review needs to tell the target's own award from another party's.
            self.validator.target_names = [profile.canonical_name, *profile.aliases]
        budget_profile = task_profile.get("budget_profile", {})
        browser_run = self._browser_run
        limits = context.budget.limits
        if browser_run:
            max_queries = limits["max_queries"]
            max_hits = limits["max_search_hits"]
            max_links_to_read = limits["max_links_to_read"]
            hits_per_query = min(self._hits_per_query, limits["max_hits_per_query"])
        else:
            max_queries = budget_profile.get("max_queries", 80)
            max_hits = budget_profile.get("max_search_hits")
            if max_hits is None:
                # Derive a coherent default from the rest of the budget so multi-
                # engine setups don't silently starve the query plan: every planned
                # query gets room for a full SERP from every active engine.
                max_hits = self._default_max_hits(max_queries)
                logger.info("max_search_hits_derived run_id=%s value=%s", run_id, max_hits)
            max_links_to_read = budget_profile.get("max_links_to_read", 100)
            hits_per_query = self._hits_per_query
        # Cross-run analysis reuse is disabled for browser runs until the cache
        # is isolated by auth context / scope version (design 12.3).
        reuse_enabled = (not browser_run) or bool(
            (context.browser_runtime.get("cache") or {}).get("reuse_analysis"))

        # Plan against the effective browser cap, not a possibly larger caller
        # budget; reserve coverage before selecting same-family query variants.
        planning_task = task_profile
        if browser_run:
            planning_task = {**task_profile,
                             "budget_profile": {**budget_profile, "max_queries": max_queries}}
        planned = self.planner.plan(profile, planning_task,
                                    family_feedback=self._family_feedback,
                                    coverage_first=browser_run)
        if browser_run and len(planned) > max_queries:
            planned = planned[:max_queries]
        stats.queries_generated = len(planned)

        seen_canonical: set[str] = set()
        seen_content_hash: set[str] = set()
        triaged: list[tuple[str, SearchHit, Any]] = []  # (link_id, hit, triage)

        for qi, pq in enumerate(planned):
            self._check_alive(context)
            if stats.search_hits >= max_hits:
                # The budget drops the remaining planned tail (coverage-first on
                # browser runs), with an explicit count rather than silently.
                stats.queries_skipped_by_hit_budget = len(planned) - qi
                logger.warning(
                    "hit_budget_truncated_queries run_id=%s max_search_hits=%s "
                    "executed=%s skipped=%s", run_id, max_hits,
                    stats.queries_executed, stats.queries_skipped_by_hit_budget)
                break
            query_id = self.repo.save_query(run_id, {**pq.to_record(), "executed": True})
            request = SearchRequest(query=pq.query_text, query_family=pq.query_family,
                                    query_id=query_id, limit=hits_per_query)
            stats.queries_attempted += 1
            context.budget.record("queries_attempted")
            try:
                batch: SearchBatch = self.search.search_with_context(request, context)
            except BudgetExceeded as exc:
                stats.search_errors.append({"query": pq.query_text, "error": "budget_exhausted",
                                            "counter": exc.counter})
                stats.stop_reason = stats.stop_reason or exc.counter
                if exc.counter in ("cancelled", "max_run_seconds"):
                    raise RunCancelled("run_deadline" if exc.counter == "max_run_seconds"
                                       else "cancelled") from exc
                logger.warning("search_budget_exhausted run_id=%s counter=%s", run_id, exc.counter)
                stats.queries_skipped_by_hit_budget = len(planned) - qi
                break
            except Exception as exc:  # noqa: BLE001 - one bad query shouldn't kill the run
                code = getattr(exc, "code", None)
                stats.search_errors.append({"query": pq.query_text, "error": type(exc).__name__,
                                            "code": code, "message": str(exc)[:200]})
                logger.warning("search_query_failed run_id=%s query=%r error=%s",
                               run_id, pq.query_text, exc)
                if code in ("gui_unavailable", "dependency_missing", "browser_missing",
                            "browser_launch_failed", "profile_busy"):
                    # Environment faults are not per-query noise: stop searching
                    # and surface the code (design 15 fail-fast table).
                    stats.stop_reason = code
                    stats.execution_status = "failed"
                    stats.search_outcomes[code] = stats.search_outcomes.get(code, 0) + 1
                    break
                continue
            stats.queries_executed += 1
            stats.queries_completed += 1
            context.budget.record("queries_completed")
            stats.search_page_attempts += batch.pages_opened
            stats.search_api_requests += batch.api_requests
            stats.search_outcomes[batch.outcome] = stats.search_outcomes.get(batch.outcome, 0) + 1
            if batch.stop_reason and batch.outcome in ("blocked", "failed", "budget_exhausted"):
                stats.search_errors.append({"query": pq.query_text, "outcome": batch.outcome,
                                            "stop_reason": batch.stop_reason})
            hits = batch.hits
            for hit in hits:
                if stats.search_hits >= max_hits:
                    break
                stats.search_hits += 1
                canonical = canonicalize_url(hit.url)
                if not hit.domain:
                    hit.domain = domain_of(hit.url)
                source_type = self.triage.source_type(hit.domain)
                link_id = self.repo.save_source_link(run_id, query_id, hit, canonical,
                                                    source_type)
                is_dup = canonical in seen_canonical
                if is_dup:
                    stats.deduplicated_links += 1
                else:
                    seen_canonical.add(canonical)
                    stats.unique_source_links += 1

                tri = self.triage.triage(hit, link_id, is_duplicate=is_dup)
                if browser_run and not is_dup:
                    tri = self._apply_read_gate(hit, tri, stats)
                if is_dup:
                    self.repo.update_link_triage(
                        link_id, tri.read_priority, tri.triage_decision,
                        reason=tri.reason, signals=tri.matched_signals,
                        need_model=tri.need_model)
                    continue
                # Cross-run reuse (spec 15.1): this canonical URL was already
                # analyzed for the same target -> clone the structured result
                # instead of re-reading and re-calling models.
                prior = None
                if reuse_enabled and window.days is None:
                    prior = self.repo.find_analyzed_link_by_canonical(
                        canonical, profile.target_id, exclude_run_id=run_id)
                if prior is not None:
                    stats.cached_or_reused_results += 1
                    self.repo.update_link_triage(
                        link_id, tri.read_priority, "link_record_only",
                        reason=f"cross-run canonical URL reuse: {prior.id}",
                        signals=[*tri.matched_signals, "cross_run_canonical_reuse"],
                        need_model=False)
                    self.repo.update_link_read(
                        link_id, "link_record_only", prior.content_hash, prior.simhash)
                    self._tally_cloned(stats, self.repo.clone_latest_analysis(
                        prior.id, link_id, profile.target_id), hit)
                    continue
                triaged.append((link_id, hit, tri))

        # Model-based SERP batch triage for borderline-score hits (spec 11.1 /
        # 15.2 D): one model call decides many hits, instead of one call per hit.
        self._check_alive(context)
        self._batch_triage(call_planner, triaged, stats)
        self._check_alive(context)
        # Final gate *after* every triage step (review: batch triage re-promoted a hit the
        # rule gate had demoted - ``/tag/catl``, content_form_ok=false - and it was read).
        if browser_run:
            triaged = [(lid, h, self._apply_read_gate(h, t, stats)) for lid, h, t in triaged]

        for link_id, _hit, tri in triaged:
            self.repo.update_link_triage(
                link_id, tri.read_priority, tri.triage_decision,
                reason=tri.reason, signals=tri.matched_signals,
                need_model=tri.need_model)

        # Build read queue from final decisions, sort by priority, cap by budget.
        # Browser route: candidates that match the target identity come first; candidates kept
        # only as related-company information (customer / supplier / competitor named, target
        # not) fill remaining slots and never displace a target-identity candidate.
        read_queue = [(lid, h, t) for lid, h, t in triaged
                      if t.triage_decision == "read"]
        read_queue.sort(key=lambda x: (RELATED_ONLY_SIGNAL in x[2].matched_signals, -x[2].read_priority))
        read_queue = read_queue[:max_links_to_read]
        stats.links_selected_for_read = len(read_queue)
        context.budget.record("links_selected_for_read", len(read_queue))
        seen_final_canonical: set[str] = set()

        for link_id, hit, tri in read_queue:
            self._check_alive(context)
            read = self.reader.read(link_id, hit.url, profile, context=context)
            # Right after the read returns, before any branch may ``continue`` (review: a
            # cancel / deadline during the *last* read, which then failed, was never seen).
            self._check_alive(context)
            # Pending redirects / browser navigations may land on another URL:
            # update canonical identity and never analyse the same body twice.
            final_canonical = canonicalize_url(read.final_url or hit.url)
            if read.final_url and read.final_url != hit.url:
                self.repo.update_link_final_url(link_id, read.final_url, final_canonical,
                                                domain_of(read.final_url))
                hit.url, hit.domain = read.final_url, domain_of(read.final_url)
            if read.read_status == "read" and final_canonical in seen_final_canonical:
                read.read_status, read.failure_reason = "failed", "duplicate_final_url"
            seen_final_canonical.add(final_canonical)
            freshness = window.assess(read.publication_time)
            read.fetch_diagnostics["time_window"] = freshness
            self.repo.save_read_attempt({
                "source_link_id": link_id, "access_profile_id": self.reader.access_profile_id,
                "read_status": read.read_status, "http_status": read.http_status,
                "content_type": read.content_type, "content_length": read.content_length,
                "extracted_title": read.title, "extracted_publish_time": read.publish_time,
                "content_hash": read.content_hash,
                "selected_passage_count": len(read.passages),
                "failure_reason": read.failure_reason,
                "diagnostics": self._read_diagnostics(read),
            })
            if read.read_status != "read":
                stats.read_failures[read.failure_reason or "unknown"] = \
                    stats.read_failures.get(read.failure_reason or "unknown", 0) + 1
                self.repo.update_link_read(
                    link_id, "failed", None, None,
                    document_type=read.document_type,
                    access_profile_id=self.reader.access_profile_id)
                continue
            stats.links_read += 1
            if window.days is not None:
                if not freshness["allowed"]:
                    reason = freshness["status"]
                    counts = stats.time_window_filter["filtered_by_reason"]
                    counts[reason] = counts.get(reason, 0) + 1
                    stats.time_window_filter["filtered_links"].append({
                        "source_link_id": link_id, "url": hit.url, "title": read.title,
                        **freshness,
                    })
                    self.repo.update_link_triage(
                        link_id, tri.read_priority, "link_record_only",
                        reason=f"时间窗过滤: {reason}",
                        signals=[*tri.matched_signals, f"time_window:{reason}"], need_model=False)
                    self.repo.update_link_read(
                        link_id, "link_record_only", read.content_hash, read.simhash,
                        document_type=read.document_type, access_profile_id=self.reader.access_profile_id)
                    continue
                stats.time_window_filter["passed"] += 1

            if read.publication_time.get("status") == "known":
                hit.publish_time_guess = read.publish_time

            # Content-hash reuse (spec 15.1 A): same body within this run.
            if read.content_hash in seen_content_hash:
                stats.cached_or_reused_results += 1
                self.repo.update_link_read(
                    link_id, "link_record_only", read.content_hash, read.simhash,
                    document_type=read.document_type,
                    access_profile_id=self.reader.access_profile_id)
                continue
            # Cross-run content-hash reuse: identical body already analyzed for
            # this target in a prior run -> clone instead of calling models.
            prior_body = None
            if reuse_enabled:
                prior_body = self.repo.find_analyzed_link_by_content_hash(
                    read.content_hash, profile.target_id, exclude_link_id=link_id)
            if prior_body is not None:
                stats.cached_or_reused_results += 1
                self.repo.update_link_triage(
                    link_id, tri.read_priority, "link_record_only",
                    reason=f"cross-run content_hash reuse: {prior_body.id}",
                    signals=[*tri.matched_signals, "cross_run_content_hash_reuse"],
                    need_model=False)
                self.repo.update_link_read(
                    link_id, "link_record_only", read.content_hash, read.simhash,
                    document_type=read.document_type,
                    access_profile_id=self.reader.access_profile_id)
                self._tally_cloned(stats, self.repo.clone_latest_analysis(
                    prior_body.id, link_id, profile.target_id), hit)
                continue
            seen_content_hash.add(read.content_hash)
            self.repo.update_link_read(
                link_id, "read", read.content_hash, read.simhash,
                document_type=read.document_type,
                access_profile_id=self.reader.access_profile_id)

            # Passage selection saving estimate (spec 19 call_efficiency): chars
            # of full body that were NOT sent to the model.
            selected_chars = sum(len(p.text) for p in read.passages)
            stats.passage_selection_saved_chars += max(
                0, (read.content_length or 0) - selected_chars)

            source_type = self.triage.source_type(hit.domain)
            source_metadata = {
                "source_link_id": link_id, "title": read.title or hit.title,
                "url": hit.url, "source_name": hit.domain, "source_type": source_type,
                "publish_time": read.publish_time,
                "query_family": hit.query_family,
            }
            materiality = tri.read_priority
            self._check_alive(context)  # before any model request for this link
            link_result = call_planner.run_for_link(
                profile, read, source_metadata, tri, source_type, materiality)
            # After the request(s) returned and before anything is persisted: a cancel that
            # arrived meanwhile, or a response that landed past the deadline, ends the run
            # as cancelled / timed_out instead of "completed" (review R3).
            self._check_alive(context)

            if link_result.call_mode == "no_model" or not link_result.outputs:
                continue

            stats.links_model_analyzed += 1
            if link_result.was_split:
                stats.split_extractions += 1
            if link_result.call_mode == "parallel_ensemble":
                stats.parallel_ensemble_calls += 1
            elif link_result.call_mode == "cascade":
                stats.cascade_calls += 1
            elif link_result.call_mode in ("priority_fallback", "single_model") and \
                    len(link_result.outputs) > 1:
                # Every output beyond the first is an actual fallback call.
                stats.fallback_calls += len(link_result.outputs) - 1

            contributions = self._persist_model_outputs(
                link_id, link_result, read, stats)
            if not contributions:
                continue

            merge_result = self.merger.merge(
                link_id, profile.target_id, contributions, self._model_feedback)

            # Arbitration on field/relation-direction conflict (spec 11.3 / 14).
            if ModelCallPlanner.arbitration_triggered(merge_result.field_conflicts,
                                                      self.config):
                self._check_alive(context)
                arb_outputs = call_planner.arbitrate(
                    profile, read, source_metadata, merge_result.field_conflicts)
                self._check_alive(context)
                if arb_outputs:
                    stats.arbitration_calls += 1
                    arb_result = LinkModelResult(
                        link_id, "arbitration", "arbitration", outputs=arb_outputs)
                    arb_contribs = self._persist_model_outputs(
                        link_id, arb_result, read, stats)
                    # Arbiter output carries extra weight as the tie-breaker.
                    for c in arb_contribs:
                        c.configured_weight *= 1.5
                    merge_result = self.merger.merge(
                        link_id, profile.target_id, contributions + arb_contribs,
                        self._model_feedback)

            bundle = merge_result.bundle
            stats.output_decisions.append({"source_link_id": link_id,
                                           **merge_result.decision_diagnostics})

            if bundle.decision in ("save_structured", "link_only"):
                self._check_alive(context)  # never persist after cancel / past the deadline
                self.repo.save_merged_analysis(
                    profile.target_id, link_id, bundle, {
                        "disagreement_level": merge_result.disagreement_level,
                        "merge_method": merge_result.merge_method,
                        "model_outputs": merge_result.model_outputs,
                        "field_conflicts": merge_result.field_conflicts,
                    }, search_run_id=run_id)
                self._tally(stats, bundle, source_metadata=source_metadata)

        # End of work: a run that is cancelled or past its deadline here is not "completed".
        self._check_alive(context)
        # Persist run-level coverage gaps that weren't tied to a saved link.
        run_gaps = self._run_gaps(stats)
        self.repo.save_coverage_gaps(run_id, profile.target_id, run_gaps)
        stats.structured["coverage_gaps"] += len(run_gaps)
        stats.model_calls = call_planner.budget.calls_used
        # Model calls are gated by the call planner's own budget (max_model_calls_per_run) and
        # every real HTTP send by ``gateway_requests_sent``; mirror the count into the run budget
        # so ``collection_diagnostics.budget_used`` is one consistent view (observed live: the
        # counter stayed 0 while two gateway requests had been sent).
        already = context.budget.used.get("model_calls", 0)
        if stats.model_calls > already:
            context.budget.record("model_calls", stats.model_calls - already)
        stats.vision_calls = self.vision.calls_used
        stats.estimated_model_cost = self._cost_from_runs(stats) + \
            round(self.vision.estimated_cost, 6)
        self._fold_budget(stats, context)

    @staticmethod
    def _fold_budget(stats: RunStats, context: RunContext) -> None:
        used = context.budget.used_summary()
        stats.http_read_attempts = used.get("http_read_attempts", 0)
        stats.browser_read_attempts = used.get("browser_read_attempts", 0)
        stats.authenticated_retries = used.get("authenticated_retries", 0)
        stats.gateway_requests_sent = used.get("gateway_requests_sent", 0)
        stats.search_page_attempts = max(stats.search_page_attempts, used.get("search_page_attempts", 0))

    @staticmethod
    def _read_diagnostics(read) -> dict[str, Any]:
        """Persisted per-attempt diagnostics (design 12.2); never page content."""
        return {
            "transport": read.transport, "final_url": read.final_url,
            "body_scope": dict(read.body_scope or {}),
            "parser_version": (read.fetch_diagnostics or {}).get("parser_version", "article_scope_v1"),
            "fetch": read.fetch_diagnostics or {},
            "selected_passage_ids": [p.passage_id for p in read.passages],
            "content_hash": read.content_hash, "document_type": read.document_type,
            "publication_time": read.publication_time,
        }

    def _default_max_hits(self, max_queries: int, cap: int = 800) -> int:
        """Budget-coherent default for max_search_hits.

        max_queries x active engines x per-query request cap, bounded by a hard
        run-level guardrail.
        """
        primary = getattr(self.search, "primary", self.search)
        engines = len(getattr(primary, "providers", [])) or 1
        return min(cap, max_queries * engines * self._hits_per_query)

    # --- batch triage ------------------------------------------------------

    def _batch_triage(self, call_planner: ModelCallPlanner,
                      triaged: list[tuple[str, Any, Any]], stats: RunStats) -> None:
        if not (self.config.call_governance or {}).get("batching", {}).get(
                "serp_batch_triage", False):
            return
        trig = (self.config.model_policies.get("tasks", {})
                .get("serp_batch_triage", {}).get("trigger", {}))
        band = trig.get("use_model_when_rule_score_between", [45, 75])
        lo, hi = band[0], band[1]
        candidates = [(lid, h, t) for lid, h, t in triaged if lo <= t.read_priority <= hi]
        size = call_planner.batch_size()
        for i in range(0, len(candidates), size):
            batch = candidates[i:i + size]
            items = [{"id": lid, "title": h.title, "snippet": h.snippet}
                     for lid, h, _ in batch]
            before = len(call_planner.triage_results)
            decisions = call_planner.batch_triage(items)
            for res in call_planner.triage_results[before:]:
                stats.model_requests.append({
                    "model_run_id": None, "source_link_id": None,
                    "task_name": "serp_batch_triage", "model_config_id": res.model_config_id,
                    "requested_model": res.model_name, "status": res.status,
                    "error_type": res.error_type, "output_tokens": res.output_tokens,
                    **(res.request_diagnostics() if hasattr(res, "request_diagnostics") else {}),
                })
            if not decisions:
                break  # budget spent or disabled
            stats.batch_triage_calls += 1
            for lid, _h, t in batch:
                d = decisions.get(lid)
                if not d:
                    continue
                stats.batch_triaged_hits += 1
                t.triage_decision = d.get("triage_decision", t.triage_decision)
                t.read_priority = max(t.read_priority, float(d.get("read_priority",
                                                                   t.read_priority)))
                t.need_model = bool(d.get("need_model", t.need_model))

    # --- helpers -----------------------------------------------------------

    def _persist_model_outputs(self, link_id, link_result, read,
                               stats: RunStats) -> list[ModelContribution]:
        contributions: list[ModelContribution] = []
        policy = self.config.model_policies.get("tasks", {}).get(link_result.task_name, {})
        weight_by_model = {m["model_id"]: m.get("weight", 1.0)
                           for m in policy.get("models", [])}
        for res in link_result.outputs:
            stats.estimated_model_cost += res.estimated_cost
            model_run_id = self.repo.save_model_run({
                "source_link_id": link_id, "task_name": link_result.task_name,
                "call_mode": link_result.call_mode, "provider_type": res.provider_type,
                "provider": res.provider, "model_name": res.model_name,
                "model_config_id": res.model_config_id,
                "model_policy_version": self.policy_version,
                "prompt_version": "bundle_v0.3", "schema_version": "bundle_extraction_v0.3",
                "input_chars": res.input_chars, "input_tokens": res.input_tokens,
                "output_tokens": res.output_tokens, "reasoning_tokens": res.reasoning_tokens,
                "cached_tokens": res.cached_tokens, "estimated_cost": res.estimated_cost,
                "latency_ms": res.latency_ms, "status": res.status,
                "error_type": res.error_type, "error_message": res.error_message,
                "provider_request_id": getattr(res, "provider_request_id", None),
                "request_diagnostics": (res.request_diagnostics()
                                        if hasattr(res, "request_diagnostics") else None),
            })
            stats.model_requests.append({
                "model_run_id": model_run_id, "source_link_id": link_id,
                "task_name": link_result.task_name, "model_config_id": res.model_config_id,
                "requested_model": res.model_name, "status": res.status,
                "error_type": res.error_type, "output_tokens": res.output_tokens,
                **(res.request_diagnostics() if hasattr(res, "request_diagnostics") else {}),
            })
            if res.status != "success" or res.parsed is None:
                self.repo.save_model_output({
                    "model_run_id": model_run_id, "source_link_id": link_id,
                    "output_json": res.parsed, "schema_valid": False,
                    "validation_errors": {"status": res.status}, "decision": None,
                })
                continue

            report = self.validator.validate(res.parsed, read.passages)
            self.repo.save_model_output({
                "model_run_id": model_run_id, "source_link_id": link_id,
                "output_json": res.parsed, "schema_valid": report.schema_valid,
                "validation_errors": {"errors": report.errors, "warnings": report.warnings},
                "decision": (report.bundle.decision if report.bundle else None),
                "overall_score": (report.bundle.overall_score if report.bundle else None),
                "confidence": (report.bundle.confidence if report.bundle else None),
            })
            if not report.schema_valid or report.bundle is None:
                continue
            contributions.append(ModelContribution(
                model_config_id=res.model_config_id, provider=res.provider,
                bundle=report.bundle,
                schema_validity_score=1.0,
                evidence_locator_score=1.0 - 0.05 * len(
                    [w for w in report.warnings if "evidence" in w]),
                configured_weight=weight_by_model.get(res.model_config_id, 1.0),
            ))
        return contributions

    def _tally(self, stats: RunStats, bundle,
               source_metadata: dict | None = None) -> None:
        s = stats.structured
        s["briefs"] += 1
        s["facts"] += len(bundle.facts)
        s["metrics"] += len(bundle.metrics)
        s["events"] += len(bundle.events)
        s["relations"] += len(bundle.relations)
        s["risks"] += len(bundle.risks)
        s["catalysts"] += len(bundle.catalysts)
        s["customer_supplier_signals"] += len(bundle.customer_supplier_signals)
        s["price_cost_margin_signals"] += len(bundle.price_cost_margin_signals)
        s["policy_signals"] += len(bundle.policy_signals)
        s["analyst_questions"] += len(bundle.analyst_questions)
        s["coverage_gaps"] += len(bundle.coverage_gaps)
        for e in bundle.events:
            # Export the reviewed event in full. Metrics and entities also hold
            # pending-review candidates; dropping them would remove qualifications
            # before the downstream Agent persists its event payload.
            entry = e.model_dump(mode="json")
            entry["source_link_id"] = bundle.source_link_id
            # Keep the flat field consumed by older report readers.
            entry["impact_channels"] = list(e.impact.channels)
            # Evidence fields for downstream consumers (agent structured_events).
            if source_metadata:
                entry["source"] = {
                    "url": source_metadata.get("url"),
                    "domain": source_metadata.get("source_name"),
                    "source_type": source_metadata.get("source_type"),
                    "published_at": source_metadata.get("publish_time"),
                    "title": source_metadata.get("title"),
                    "query_family": source_metadata.get("query_family"),
                }
            stats.top_events.append(entry)
        for r in bundle.relations:
            stats.top_relations.append({
                "relation_type": r.relation_type, "subject": r.subject_entity.name,
                "object": r.object_entity.name, "confidence": r.confidence})

    def _tally_counts(self, stats: RunStats, counts: dict[str, int]) -> None:
        """Fold cloned (cache-reused) structured row counts into run stats."""
        for key, n in counts.items():
            if key in stats.structured:
                stats.structured[key] += n

    def _tally_cloned(self, stats: RunStats, cloned: dict, hit) -> None:
        """Fold a cache-reuse clone result into run stats, including event details.

        Without this, all_events only contains fresh-analysis events and the
        "full event persistence" contract breaks whenever a link is reused.
        """
        self._tally_counts(stats, cloned)
        source = {
            "url": hit.url,
            "domain": hit.domain,
            "source_type": self.triage.source_type(hit.domain),
            "published_at": hit.publish_time_guess,
            "title": hit.title,
            "query_family": hit.query_family,
        }
        for entry in cloned.get("cloned_events") or []:
            stats.top_events.append({**entry, "source": source})

    def _run_gaps(self, stats: RunStats) -> list[CoverageGap]:
        gaps = []
        for reason, count in stats.time_window_filter.get("filtered_by_reason", {}).items():
            gaps.append(CoverageGap(
                gap_type=reason, priority="medium",
                description=f"时间窗检查: {count} 篇来源因 {reason} 保留链接但未提取近期情报。"))
        if stats.structured["events"] > 0 and stats.structured["facts"] == 0:
            gaps.append(CoverageGap(
                gap_type="missing_amount",
                description="发现事件线索，但缺少可量化事实/金额。", priority="medium"))
        return gaps

    def _cost_from_runs(self, stats: RunStats) -> float:
        return round(stats.estimated_model_cost, 6)

    def _collection_diagnostics(self, run_id: str, stats: RunStats, context: RunContext,
                                cleanup: dict[str, Any]) -> dict[str, Any]:
        """Design 15: answer each acceptance question separately."""
        self._fold_budget(stats, context)
        browser_run = self._browser_run
        if stats.execution_status != "completed":
            execution_status = stats.execution_status
        else:
            execution_status = "completed"
        # search
        if stats.queries_attempted == 0:
            search_status, search_reason = "not_run", stats.stop_reason
        elif stats.search_hits > 0:
            blocked = sum(v for k, v in stats.search_outcomes.items() if k in ("blocked", "failed"))
            search_status = "partial" if (blocked or stats.search_errors) else "ok"
            search_reason = stats.stop_reason or (stats.search_errors[0].get("stop_reason")
                                                  if stats.search_errors else None)
        else:
            outcomes = stats.search_outcomes
            if outcomes.get("blocked"):
                search_status, search_reason = "blocked", "engine_blocked"
            elif outcomes.get("failed") or stats.search_errors:
                search_status = "failed"
                search_reason = stats.stop_reason or (stats.search_errors[0].get("stop_reason")
                                                      or stats.search_errors[0].get("code")
                                                      or stats.search_errors[0].get("error"))
            else:
                search_status, search_reason = "empty", "no_candidates"
        # read
        if stats.links_selected_for_read == 0:
            read_status = "not_run"
        elif stats.links_read == stats.links_selected_for_read:
            read_status = "ok"
        elif stats.links_read > 0:
            read_status = "partial"
        else:
            read_status = "failed"
        # output
        structured_total = stats.structured.get("events", 0) + stats.structured.get("facts", 0) + \
            stats.structured.get("metrics", 0) + stats.structured.get("relations", 0)
        if stats.links_model_analyzed == 0:
            output_status = "no_model_call" if stats.links_read == 0 else "no_structured_output"
        elif structured_total == 0:
            output_status = "no_structured_output"
        else:
            output_status = "ok"
        if (stats.time_window_filter.get("enabled") and
                stats.time_window_filter.get("filtered_by_reason") and
                not stats.time_window_filter.get("passed") and structured_total == 0):
            output_status = "time_window_filtered"
        usable = (execution_status == "completed" and search_status in ("ok", "partial")
                  and read_status in ("ok", "partial") and output_status == "ok")
        page_stats: dict[str, Any] = {}
        if browser_run:
            try:
                page_stats = self.repo.search_page_stats_for_run(run_id)
            except Exception:  # noqa: BLE001
                page_stats = {}
        diag = {
            "execution_status": execution_status,
            "search_status": search_status,
            "search_reason": search_reason,
            "read_status": read_status,
            "read_failures": dict(stats.read_failures),
            "time_window_filter": stats.time_window_filter,
            "output_status": output_status,
            "output_decisions": list(stats.output_decisions),
            "model_requests": list(stats.model_requests),
            "usable": usable,
            "stop_reason": stats.stop_reason,
            "budget_used": context.budget.used_summary(),
            "budget_limits": {k: v for k, v in context.budget.limits.items() if v < 10 ** 9},
            "elapsed_seconds": round(context.budget.elapsed_seconds(), 2),
            "search_outcomes": dict(stats.search_outcomes),
            "search_errors": stats.search_errors[:10],
            "provider": getattr(self.search, "name", None),
            "browser_run": browser_run,
            "attempt_id": context.attempt_id,
            "config_fingerprint": context.config_fingerprint,
            "cleanup": cleanup,
        }
        if browser_run:
            diag["page_attempts"] = page_stats
            diag["auth_context"] = context.auth_context()
            diag["credential_sweep"] = context.credential_sweep
            diag["reuse_analysis"] = bool((context.browser_runtime.get("cache") or {}).get("reuse_analysis"))
        return diag

    def _summary(self, run_id: str, target_id: str, task_profile: dict,
                 stats: RunStats, context: RunContext | None = None,
                 cleanup: dict[str, Any] | None = None) -> dict:
        stats.top_events.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        stats.top_relations.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        profile = self.config.get_target_profile(target_id) or {}
        diagnostics = (self._collection_diagnostics(run_id, stats, context, cleanup or {})
                       if context is not None else None)
        return {
            "search_run_id": run_id,
            "target": profile.get("canonical_name", target_id),
            "time_window": task_profile.get("time_window", ""),
            "log_file": stats.log_file,
            "collection_diagnostics": diagnostics,
            "summary": {
                "queries_generated": stats.queries_generated,
                "queries_executed": stats.queries_executed,
                "queries_attempted": stats.queries_attempted,
                "queries_completed": stats.queries_completed,
                "queries_skipped_by_hit_budget": stats.queries_skipped_by_hit_budget,
                "search_page_attempts": stats.search_page_attempts,
                "search_api_requests": stats.search_api_requests,
                "http_read_attempts": stats.http_read_attempts,
                "browser_read_attempts": stats.browser_read_attempts,
                "authenticated_retries": stats.authenticated_retries,
                "gateway_requests_sent": stats.gateway_requests_sent,
                "links_selected_for_read": stats.links_selected_for_read,
                "read_gate_demoted": dict(stats.read_gate_demoted),
                "read_gate_related_only": stats.read_gate_related_only,
                "search_hits": stats.search_hits,
                "unique_source_links": stats.unique_source_links,
                "links_read": stats.links_read,
                "time_window_passed": stats.time_window_filter.get("passed", 0),
                "time_window_filtered": sum(stats.time_window_filter.get("filtered_by_reason", {}).values()),
                "links_model_analyzed": stats.links_model_analyzed,
                "model_calls": stats.model_calls,
                "parallel_ensemble_calls": stats.parallel_ensemble_calls,
                "fallback_calls": stats.fallback_calls,
                "cascade_calls": stats.cascade_calls,
                "arbitration_calls": stats.arbitration_calls,
                "split_extractions": stats.split_extractions,
                "batch_triage_calls": stats.batch_triage_calls,
                "vision_calls": stats.vision_calls,
                "cached_or_reused_results": stats.cached_or_reused_results,
                "estimated_model_cost": round(stats.estimated_model_cost, 4),
            },
            "structured_outputs": stats.structured,
            "top_events": stats.top_events[:5],
            # Full event list for downstream coverage accounting: minor events (small buyback,
            # inventory drift, tender shortlist) matter for variable coverage even when they
            # don't make the top-5 display cut.
            "all_events": stats.top_events,
            "top_relations": stats.top_relations[:5],
            "call_efficiency": {
                "deduplicated_links": stats.deduplicated_links,
                "reused_existing_analysis": stats.cached_or_reused_results,
                "batch_triage_saved_calls_estimate": max(
                    0, stats.batch_triaged_hits - stats.batch_triage_calls),
                "passage_selection_saved_tokens_estimate": round(
                    stats.passage_selection_saved_chars / 3),
            },
        }
