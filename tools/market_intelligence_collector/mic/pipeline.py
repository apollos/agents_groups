"""Pipeline runner (spec section 18) + Batch Report (section 19).

Orchestrates the full run:
  target/task -> query plan -> search -> dedup -> triage -> read ->
  passage selection -> model call planning -> validation -> merge ->
  persist structured results -> batch report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mic.config import MICConfig, load_config
from mic.logging_utils import get_logger, setup_logging
from mic.merge import ModelContribution, MultiModelMerger
from mic.modeling.adapter import ModelRegistry
from mic.modeling.call_planner import CallBudget, LinkModelResult, ModelCallPlanner
from mic.modeling.vision import VisionExtractor
from mic.planner import QueryPlanner
from mic.profile import TargetProfile
from mic.reader import LinkReader
from mic.schemas import CoverageGap, SearchHit
from mic.search import build_search_provider
from mic.store import Repository, get_database
from mic.triage import SearchHitTriage
from mic.utils import canonicalize_url, domain_of
from mic.validate import BundleValidator

logger = get_logger("pipeline")


@dataclass
class RunStats:
    queries_generated: int = 0
    queries_executed: int = 0
    queries_skipped_by_hit_budget: int = 0
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

    # --- public ------------------------------------------------------------

    def collect_intelligence(self, target_id: str, task_profile: dict[str, Any],
                             model_policy_version: str | None = None,
                             query_plan_version: str | None = None) -> dict:
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
        call_budget = CallBudget(
            max_model_calls_per_run=run_calls,
            max_model_calls_per_source_link=gov.get("max_model_calls_per_source_link", 3),
            max_parallel_model_groups_per_run=gov.get("max_parallel_model_groups_per_run", 5),
            max_batch_triage_calls=gov.get("max_batch_triage_calls", max(1, run_calls // 6)),
        )
        call_planner = ModelCallPlanner(self.config, self.registry, call_budget)

        run_id = self.repo.create_search_run(
            target_id, task_profile, budget_profile,
            self.query_plan_version, self.policy_version)
        _, log_path = setup_logging(run_id, console=False)
        stats = RunStats(log_file=str(log_path) if log_path else None)

        logger.info("collect_start run_id=%s target_id=%s", run_id, target_id)
        try:
            self._execute(run_id, profile, task_profile, call_planner, stats)
            summary = self._summary(run_id, target_id, task_profile, stats)
            self.repo.finish_search_run(run_id, "completed", summary)
            logger.info("collect_completed run_id=%s summary=%s",
                        run_id, summary.get("summary", {}))
            return summary
        except Exception as exc:  # noqa: BLE001
            logger.exception("collect_failed run_id=%s target_id=%s", run_id, target_id)
            self.repo.finish_search_run(
                run_id, "failed", {"error": str(exc), "log_file": stats.log_file})
            raise

    # --- core --------------------------------------------------------------

    def _execute(self, run_id: str, profile: TargetProfile, task_profile: dict,
                 call_planner: ModelCallPlanner, stats: RunStats) -> None:
        self.vision.reset_run()
        budget_profile = task_profile.get("budget_profile", {})
        max_queries = budget_profile.get("max_queries", 80)
        max_hits = budget_profile.get("max_search_hits")
        if max_hits is None:
            # Derive a coherent default from the rest of the budget so multi-
            # engine setups don't silently starve the query plan: every planned
            # query gets room for a full SERP from every active engine.
            max_hits = self._default_max_hits(max_queries)
            logger.info("max_search_hits_derived run_id=%s value=%s", run_id, max_hits)
        max_links_to_read = budget_profile.get("max_links_to_read", 100)

        planned = self.planner.plan(profile, task_profile,
                                    family_feedback=self._family_feedback)
        stats.queries_generated = len(planned)

        seen_canonical: set[str] = set()
        seen_content_hash: set[str] = set()
        triaged: list[tuple[str, SearchHit, Any]] = []  # (link_id, hit, triage)

        for qi, pq in enumerate(planned):
            if stats.search_hits >= max_hits:
                # Queries are priority-ordered, so the budget drops the lowest-
                # value tail - but never silently.
                stats.queries_skipped_by_hit_budget = len(planned) - qi
                logger.warning(
                    "hit_budget_truncated_queries run_id=%s max_search_hits=%s "
                    "executed=%s skipped=%s", run_id, max_hits,
                    stats.queries_executed, stats.queries_skipped_by_hit_budget)
                break
            query_id = self.repo.save_query(run_id, {**pq.to_record(), "executed": True})
            try:
                hits = self.search.search(pq.query_text, pq.query_family,
                                          limit=self._hits_per_query)
            except Exception as exc:  # noqa: BLE001 - one bad query shouldn't kill the run
                logger.warning("search_query_failed run_id=%s query=%r error=%s",
                               run_id, pq.query_text, exc)
                continue
            stats.queries_executed += 1
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
                if is_dup:
                    self.repo.update_link_triage(
                        link_id, tri.read_priority, tri.triage_decision,
                        reason=tri.reason, signals=tri.matched_signals,
                        need_model=tri.need_model)
                    continue
                # Cross-run reuse (spec 15.1): this canonical URL was already
                # analyzed for the same target -> clone the structured result
                # instead of re-reading and re-calling models.
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
                    self._tally_counts(stats, self.repo.clone_latest_analysis(
                        prior.id, link_id, profile.target_id))
                    continue
                triaged.append((link_id, hit, tri))

        # Model-based SERP batch triage for borderline-score hits (spec 11.1 /
        # 15.2 D): one model call decides many hits, instead of one call per hit.
        self._batch_triage(call_planner, triaged, stats)

        for link_id, _hit, tri in triaged:
            self.repo.update_link_triage(
                link_id, tri.read_priority, tri.triage_decision,
                reason=tri.reason, signals=tri.matched_signals,
                need_model=tri.need_model)

        # Build read queue from final decisions, sort by priority, cap by budget.
        read_queue = [(lid, h, t) for lid, h, t in triaged
                      if t.triage_decision == "read"]
        read_queue.sort(key=lambda x: x[2].read_priority, reverse=True)
        read_queue = read_queue[:max_links_to_read]

        for link_id, hit, tri in read_queue:
            read = self.reader.read(link_id, hit.url, profile)
            self.repo.save_read_attempt({
                "source_link_id": link_id, "access_profile_id": self.reader.access_profile_id,
                "read_status": read.read_status, "http_status": read.http_status,
                "content_type": read.content_type, "content_length": read.content_length,
                "extracted_title": read.title, "extracted_publish_time": read.publish_time,
                "content_hash": read.content_hash,
                "selected_passage_count": len(read.passages),
                "failure_reason": read.failure_reason,
            })
            if read.read_status != "read":
                self.repo.update_link_read(
                    link_id, "failed", None, None,
                    document_type=read.document_type,
                    access_profile_id=self.reader.access_profile_id)
                continue
            stats.links_read += 1

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
                self._tally_counts(stats, self.repo.clone_latest_analysis(
                    prior_body.id, link_id, profile.target_id))
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
                "publish_time": read.publish_time or hit.publish_time_guess,
            }
            materiality = tri.read_priority
            link_result = call_planner.run_for_link(
                profile, read, source_metadata, tri, source_type, materiality)

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
                arb_outputs = call_planner.arbitrate(
                    profile, read, source_metadata, merge_result.field_conflicts)
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

            if bundle.decision in ("save_structured", "link_only"):
                self.repo.save_merged_analysis(
                    profile.target_id, link_id, bundle, {
                        "disagreement_level": merge_result.disagreement_level,
                        "merge_method": merge_result.merge_method,
                        "model_outputs": merge_result.model_outputs,
                        "field_conflicts": merge_result.field_conflicts,
                    }, search_run_id=run_id)
                self._tally(stats, bundle, source_metadata=source_metadata)

        # Persist run-level coverage gaps that weren't tied to a saved link.
        self.repo.save_coverage_gaps(run_id, profile.target_id, self._run_gaps(stats))
        stats.model_calls = call_planner.budget.calls_used
        stats.vision_calls = self.vision.calls_used
        stats.estimated_model_cost = self._cost_from_runs(stats) + \
            round(self.vision.estimated_cost, 6)

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
            decisions = call_planner.batch_triage(items)
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
            entry = {
                "summary": e.summary, "event_type": e.event_type,
                "event_date": e.event_date, "impact_channels": e.impact.channels,
                "confidence": e.confidence, "source_link_id": bundle.source_link_id}
            # Evidence fields for downstream consumers (agent structured_events).
            if source_metadata:
                entry["source"] = {
                    "url": source_metadata.get("url"),
                    "domain": source_metadata.get("source_name"),
                    "source_type": source_metadata.get("source_type"),
                    "published_at": source_metadata.get("publish_time"),
                    "title": source_metadata.get("title"),
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

    def _run_gaps(self, stats: RunStats) -> list[CoverageGap]:
        gaps = []
        if stats.structured["events"] > 0 and stats.structured["facts"] == 0:
            gaps.append(CoverageGap(
                gap_type="missing_amount",
                description="发现事件线索，但缺少可量化事实/金额。", priority="medium"))
        return gaps

    def _cost_from_runs(self, stats: RunStats) -> float:
        return round(stats.estimated_model_cost, 6)

    def _summary(self, run_id: str, target_id: str, task_profile: dict,
                 stats: RunStats) -> dict:
        stats.top_events.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        stats.top_relations.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        profile = self.config.get_target_profile(target_id) or {}
        return {
            "search_run_id": run_id,
            "target": profile.get("canonical_name", target_id),
            "time_window": task_profile.get("time_window", ""),
            "log_file": stats.log_file,
            "summary": {
                "queries_generated": stats.queries_generated,
                "queries_executed": stats.queries_executed,
                "queries_skipped_by_hit_budget": stats.queries_skipped_by_hit_budget,
                "search_hits": stats.search_hits,
                "unique_source_links": stats.unique_source_links,
                "links_read": stats.links_read,
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
