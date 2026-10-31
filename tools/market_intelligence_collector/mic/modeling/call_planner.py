"""Model Call Planner (spec section 11 + 15).

Decides whether to call a model, which configured task/call_mode to use, and
executes it. Implements all spec call modes: no_model / single_model /
priority_fallback / parallel_ensemble / cascade / arbitration / batch_triage.
Enforces per-run and per-link budgets, early-stop, and one-shot bundle
extraction with optional splitting when the selected text is too long. It does
NOT decide cheap-vs-strong; all model selection comes from config.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING

from mic.config import MICConfig
from mic.modeling.adapter import ModelCallResult, ModelRegistry
from mic.modeling.prompts import (
    build_arbitration_messages,
    build_batch_triage_messages,
    build_bundle_messages,
    build_json_recovery_messages,
)
from mic.profile import TargetProfile
from mic.schemas import Passage, TriageResult
from mic.task_triage import (PROTOCOL as TASK_TRIAGE_PROTOCOL, INDEX_BASIS,
                             SEARCH_BASIS, validate_task_triage)
from mic.validate import bundle_protocol_errors

if TYPE_CHECKING:  # type-hint only; a runtime import would be circular
    # (reader -> modeling.vision -> modeling/__init__ -> call_planner -> reader)
    from mic.reader import ReadResult


@dataclass
class CallBudget:
    max_model_calls_per_run: int = 30
    max_model_calls_per_source_link: int = 3
    max_parallel_model_groups_per_run: int = 5
    max_batch_triage_calls: int = 5
    min_extraction_calls_reserved: int = 1
    calls_used: int = 0
    parallel_groups_used: int = 0
    batch_triage_calls_used: int = 0

    def can_call(self, n: int = 1) -> bool:
        return self.calls_used + n <= self.max_model_calls_per_run

    def can_call_batch_triage(self) -> bool:
        if self.batch_triage_calls_used >= self.max_batch_triage_calls:
            return False
        # Reserve at least one extraction call when the run has any model-call
        # budget. Batch SERP triage is supposed to reduce waste, not consume the
        # only available model call and leave no article extractable.
        reserved = min(self.min_extraction_calls_reserved, self.max_model_calls_per_run)
        return self.calls_used + 1 <= self.max_model_calls_per_run - reserved

    def record(self, n: int = 1) -> None:
        self.calls_used += n


@dataclass
class LinkModelResult:
    source_link_id: str
    task_name: str
    call_mode: str
    outputs: list[ModelCallResult] = field(default_factory=list)
    skipped_reason: str | None = None
    was_split: bool = False
    arbitrated: bool = False


class ModelCallPlanner:
    def __init__(self, config: MICConfig, registry: ModelRegistry, budget: CallBudget):
        self.config = config
        self.registry = registry
        self.budget = budget
        self.policies = config.model_policies.get("tasks", {})
        self.policy_version = config.model_policies.get("version", "model_policy_v0.3")
        gov = config.call_governance or {}
        self.early_stop = gov.get("early_stop", {})
        self.budgets_cfg = gov.get("budgets", {})
        self.batching_cfg = gov.get("batching", {})
        self.extraction_cfg = gov.get("extraction", {})
        self.max_input_chars = self.budgets_cfg.get("max_input_chars_per_model_call", 32000)
        self.output_limits = (config.output_schema or {}).get("limits", {})
        self.triage_results: list[ModelCallResult] = []
        self.task_triage_reviews: list[dict] = []
        # Run-level research questions (mic.task_questions.task_context); set by the
        # pipeline per run and attached to every extraction / arbitration prompt.
        self.task_context: dict | None = None
        # Snapshot of effective run limits/remaining before each existing triage call.
        self.triage_budget_context: dict | None = None
        self.acceptance_trace = None
        self._source_calls: dict[str, int] = {}
        self._json_recovery_used = False

    def _complete(self, adapter, messages, *, json_recovery=None):
        if self.acceptance_trace is None:
            result = adapter.complete(messages)
            if json_recovery is not None:
                result.json_recovery = dict(json_recovery)
            return result
        if json_recovery is not None:
            return self.acceptance_trace.complete(adapter, messages, json_recovery=json_recovery)
        return self.acceptance_trace.complete(adapter, messages)

    @contextmanager
    def _source_budget(self, source_link_id):
        # Extraction, chunks, recovery and arbitration share one source ceiling.
        # Dispatch is synchronous; restore the configured ceiling on every exit.
        ceiling = self.budget.max_model_calls_per_source_link
        self.budget.max_model_calls_per_source_link = max(
            0, ceiling - self._source_calls.get(source_link_id, 0))
        before = self.budget.calls_used
        try:
            yield
        finally:
            self._source_calls[source_link_id] = self._source_calls.get(source_link_id, 0) + (
                self.budget.calls_used - before)
            self.budget.max_model_calls_per_source_link = ceiling

    # --- decision ----------------------------------------------------------

    def should_call_model(self, triage: TriageResult, source_type: str,
                          duplicate: bool) -> tuple[bool, str]:
        gov = (self.config.call_governance or {}).get("triggers", {})
        skip = gov.get("skip_model_when", {})
        if duplicate and skip.get("duplicate_canonical_url_analyzed"):
            return False, "duplicate_canonical_url"
        # Note: same_content_hash_analyzed is enforced upstream in the pipeline
        # (content-hash reuse paths run before any model call), and
        # title_snippet_low_relevance is enforced by triage (skip_for_now links
        # never reach this planner).
        reason = self._call_trigger(triage, source_type, gov.get("call_model_when", {}))
        if reason is None:
            return False, "triage_no_model"
        if not self.budget.can_call(1):
            return False, "run_budget_exhausted"
        return True, reason

    @staticmethod
    def _call_trigger(triage: TriageResult, source_type: str,
                      call: dict) -> str | None:
        """First matched call_model_when condition (spec 15.2), or None.

        ``rule_score_between`` is what triage encoded into ``need_model``; the
        other conditions can promote a read-decision link that fell outside the
        score band.
        """
        if triage.need_model:
            return "rule_score_between"
        signals = set(triage.matched_signals)
        if call.get("contains_metric_or_amount") and "amount_mentioned" in signals:
            return "contains_metric_or_amount"
        if call.get("high_value_keyword_matched") and "strong_fact_keyword" in signals:
            return "high_value_keyword_matched"
        if call.get("contains_relation_signal") and signals & {
                "customer_keyword", "supplier_keyword",
                "order_keyword", "tender_keyword"}:
            return "contains_relation_signal"
        if source_type in call.get("source_type_in", []):
            return "source_type_in"
        return None

    def select_task(self, triage: TriageResult, source_type: str,
                    materiality_score: float) -> str:
        """Choose a configured task/policy for this link."""
        hv = self.policies.get("high_value_parallel_analysis", {})
        trig = hv.get("trigger", {})
        st_in = trig.get("source_type_in", [])
        mat_gte = trig.get("materiality_score_gte", 999)
        if source_type in st_in and materiality_score >= mat_gte:
            return "high_value_parallel_analysis"
        return "bundle_extraction"

    # --- per-link execution ------------------------------------------------

    def run_for_link(self, profile: TargetProfile, read: ReadResult,
                     source_metadata: dict, triage: TriageResult,
                     source_type: str, materiality_score: float) -> LinkModelResult:
        do_call, reason = self.should_call_model(triage, source_type, duplicate=False)
        task = self.select_task(triage, source_type, materiality_score)
        if not do_call:
            return LinkModelResult(read.source_link_id, task, "no_model",
                                   skipped_reason=reason)

        policy = self.policies.get(task, {})
        call_mode = policy.get("call_mode", "single_model")

        with self._source_budget(read.source_link_id):
            if self.budget.max_model_calls_per_source_link <= 0:
                return LinkModelResult(read.source_link_id, task, "no_model",
                                       skipped_reason="source_model_budget_exhausted")
            chunks = self._passage_chunks(read.passages)
            if len(chunks) > 1 and self._split_enabled():
                outputs = self._run_split(profile, source_metadata, chunks, policy)
                return LinkModelResult(read.source_link_id, task, call_mode,
                                       outputs=outputs, was_split=True)

            messages = build_bundle_messages(profile, source_metadata, read.passages,
                                             self.output_limits, task_context=self.task_context)
            outputs = self._dispatch(call_mode, policy, messages)
            self._recover_json(outputs, messages)
            return LinkModelResult(read.source_link_id, task, call_mode, outputs=outputs)

    def _recover_json(self, outputs, messages):
        """One explicit source re-extraction, inside all existing budgets.

        Ordinary fallback runs first. Triggers are unparseable JSON or a parsed
        object that violates the bundle protocol; both share the single recovery
        quota. No protocol-valid output is retried for weak evidence, low scores,
        an explicit skip or failed review; a bad response is never repaired locally
        or reused as source material. Split/triage/arbitration do not use this
        path. A failed recovery never starts another fallback chain.
        """
        if (not self.extraction_cfg.get("json_recovery", {}).get("enabled", True)
                or self._json_recovery_used or not outputs
                or any(r.status == "success" and r.parsed is not None
                       and not bundle_protocol_errors(r.parsed) for r in outputs)
                or len(outputs) >= self.budget.max_model_calls_per_source_link
                or not self.budget.can_call()):
            return
        parent = outputs[-1]
        if parent.status == "json_invalid":
            trigger, protocol_errors = "json_invalid", None
        elif parent.status == "success" and parent.parsed is not None:
            trigger, protocol_errors = "response_protocol_invalid", bundle_protocol_errors(parent.parsed)
        else:
            return
        adapter = self.registry.get(parent.model_config_id)
        if adapter is None or not adapter.usable:
            return
        runtime_budget = adapter.budget
        if runtime_budget is not None and not runtime_budget.can("gateway_requests_sent"):
            return  # Includes cancellation/deadline; do not create a phantom send.
        recovery = {
            "strategy": "reextract_original_input", "attempt": 1, "trigger": trigger,
            "parent_output_index": len(outputs) - 1,
            "parent_response_sha256": sha256(parent.raw_text.encode("utf-8")).hexdigest(),
        }
        if protocol_errors:
            recovery["protocol_errors"] = list(protocol_errors)
        if parent.provider_request_id:
            recovery["parent_provider_request_id"] = parent.provider_request_id
        if parent.acceptance_trace and parent.acceptance_trace.get("trace_id"):
            recovery["parent_trace_id"] = parent.acceptance_trace["trace_id"]
        self._json_recovery_used = True
        result = self._complete(adapter, build_json_recovery_messages(messages, protocol_errors=protocol_errors),
                                json_recovery=recovery)
        self.budget.record()
        outputs.append(result)

    def _dispatch(self, call_mode: str, policy: dict,
                  messages: list[dict]) -> list[ModelCallResult]:
        if call_mode == "parallel_ensemble":
            return self._parallel_ensemble(policy, messages)
        if call_mode == "cascade":
            return self._cascade(policy, messages)
        if call_mode == "single_model":
            return self._single_model(policy, messages)
        return self._priority_fallback(policy, messages)

    # --- call modes --------------------------------------------------------

    def _single_model(self, policy: dict, messages: list[dict]) -> list[ModelCallResult]:
        """Call exactly one configured model (spec 11.1 single_model)."""
        models = sorted(policy.get("models", []), key=lambda x: x.get("priority", 99))
        if self.budget.max_model_calls_per_source_link <= 0 or not self.budget.can_call(1):
            return []
        for spec in models:
            adapter = self.registry.get(spec["model_id"])
            if adapter is None or not adapter.usable:
                continue
            res = self._complete(adapter, messages)
            self.budget.record(1)
            return [res]
        return []

    def _priority_fallback(self, policy: dict, messages: list[dict]) -> list[ModelCallResult]:
        models = sorted(policy.get("models", []), key=lambda x: x.get("priority", 99))
        conf_threshold = policy.get("confidence_threshold", 0.4)
        results: list[ModelCallResult] = []
        per_link = self.budget.max_model_calls_per_source_link
        for spec in models:
            if len(results) >= per_link or not self.budget.can_call(1):
                break
            adapter = self.registry.get(spec["model_id"])
            if adapter is None or not adapter.usable:
                continue
            res = self._complete(adapter, messages)
            self.budget.record(1)
            results.append(res)
            if self._early_stop(res, conf_threshold):
                break
        return results

    def _parallel_ensemble(self, policy: dict, messages: list[dict]) -> list[ModelCallResult]:
        # Executed sequentially here (no threads) but treated as one parallel group.
        results: list[ModelCallResult] = []
        if self.budget.parallel_groups_used >= self.budget.max_parallel_model_groups_per_run:
            # Degrade to priority_fallback when the parallel-group budget is spent.
            return self._priority_fallback(policy, messages)
        self.budget.parallel_groups_used += 1
        per_link = self.budget.max_model_calls_per_source_link
        for spec in policy.get("models", []):
            if len(results) >= per_link or not self.budget.can_call(1):
                break
            adapter = self.registry.get(spec["model_id"])
            if adapter is None or not adapter.usable:
                continue
            res = self._complete(adapter, messages)
            self.budget.record(1)
            results.append(res)
        return results

    def _cascade(self, policy: dict, messages: list[dict]) -> list[ModelCallResult]:
        """Call models in priority order, accumulating outputs, stopping once a
        result clears the cascade confidence/schema bar (spec 11.1 cascade)."""
        models = sorted(policy.get("models", []), key=lambda x: x.get("priority", 99))
        threshold = policy.get("cascade_confidence_threshold", 0.7)
        results: list[ModelCallResult] = []
        per_link = self.budget.max_model_calls_per_source_link
        for spec in models:
            if len(results) >= per_link or not self.budget.can_call(1):
                break
            adapter = self.registry.get(spec["model_id"])
            if adapter is None or not adapter.usable:
                continue
            res = self._complete(adapter, messages)
            self.budget.record(1)
            results.append(res)
            # Unlike fallback, cascade keeps prior outputs; it only stops calling
            # further models once corroboration is strong enough.
            if (res.status == "success" and res.parsed
                    and res.parsed.get("confidence", 0.0) >= threshold):
                break
        return results

    # --- batch SERP triage (spec 11.1 batch_triage + 15.2 D) --------------

    def batch_triage(self, items: list[dict], *,
                     navigation_context: dict | None = None) -> dict[str, dict]:
        """Triage a batch of search-hit summaries in a single model call.

        ``items`` includes id/title/snippet and optional URL/domain/rule priority.
        Returns id -> decision dict. With research questions, only a complete
        validated model ranking may alter task-directed reading order.
        Respects a dedicated batch-triage sub-budget so it never starves
        extraction calls.
        """
        policy = self.policies.get("serp_batch_triage", {})
        if not items or not policy:
            return {}
        if not self.budget.can_call_batch_triage():
            return {}
        models = sorted(policy.get("models", []), key=lambda x: x.get("priority", 99))
        task_aware = bool(self.task_context and self.task_context.get("questions"))
        if navigation_context is not None and not task_aware:
            return {}  # Directory following requires a complete task-bound ranking.
        messages = build_batch_triage_messages(
            items, task_context=self.task_context, reading_budget=self.triage_budget_context,
            navigation_context=navigation_context)
        if navigation_context is not None and len(messages[-1]["content"]) > self.max_input_chars:
            return {}  # Never silently truncate the evidence-to-URL binding.
        basis = INDEX_BASIS if navigation_context is not None else SEARCH_BASIS
        for spec in models:
            adapter = self.registry.get(spec["model_id"])
            if adapter is None or not adapter.usable:
                continue
            res = self._complete(adapter, messages)
            self.budget.record(1)
            self.budget.batch_triage_calls_used += 1
            # Triage requests have no model_run row; keep them reportable so
            # every gateway send stays traceable (A4).
            self.triage_results.append(res)
            if task_aware:
                review = {
                    "protocol": TASK_TRIAGE_PROTOCOL,
                    "model_config_id": res.model_config_id,
                    "provider_request_id": res.provider_request_id,
                    "request_status": res.status,
                    "raw_text": res.raw_text,
                    "reading_budget": self.triage_budget_context,
                    "basis": basis,
                }
                if navigation_context is not None:
                    review["navigation_context"] = navigation_context
                if res.status == "success":
                    out = validate_task_triage(items, res.parsed, review=review, basis=basis)
                else:
                    out = {}
                    review.update({"status": "not_assessed", "raw": res.parsed,
                                   "input_ids": [item.get("id") for item in items],
                                   "errors": ["model_request_unsuccessful"]})
                self.task_triage_reviews.append(review)
                return out
            if res.status == "success" and res.parsed:
                out = {}
                for r in res.parsed.get("results", []):
                    if r.get("id") is not None:
                        out[str(r["id"])] = r
                return out
            break  # one model attempt per batch; don't fan out triage
        return {}

    def batch_size(self) -> int:
        return self.batching_cfg.get("serp_batch_size", 20)

    # --- arbitration (spec 11.1 arbitration + 14.4/14.5) ------------------

    def arbitrate(self, profile: TargetProfile, read: ReadResult, source_metadata: dict,
                  conflicts: list[dict]) -> list[ModelCallResult]:
        policy = self.policies.get("arbitration", {})
        if not policy or not self.budget.can_call(1):
            return []
        messages = build_arbitration_messages(
            profile, source_metadata, read.passages, self.output_limits, conflicts,
            task_context=self.task_context)
        with self._source_budget(read.source_link_id):
            return self._priority_fallback(policy, messages)

    @staticmethod
    def arbitration_triggered(conflicts: list[dict], config: MICConfig) -> bool:
        if not conflicts:
            return False
        trig = (config.model_policies.get("tasks", {})
                .get("arbitration", {}).get("trigger", {}))
        fields = set(trig.get("field_conflict_in", [])) | {"relation_direction"}
        for c in conflicts:
            field_name = c.get("field", "")
            if field_name in fields or field_name.replace("impact_", "") in fields:
                return True
        return False

    # --- task splitting (spec 10.3 + 15.2 extraction.split_tasks_only_when) -

    def _split_enabled(self) -> bool:
        when = self.extraction_cfg.get("split_tasks_only_when", [])
        return "selected_text_too_long" in when

    def _passage_chunks(self, passages: list[Passage]) -> list[list[Passage]]:
        total = sum(len(p.text) for p in passages)
        if total <= self.max_input_chars or len(passages) <= 1:
            return [passages]
        chunks: list[list[Passage]] = []
        current: list[Passage] = []
        size = 0
        for p in passages:
            if current and size + len(p.text) > self.max_input_chars:
                chunks.append(current)
                current, size = [], 0
            current.append(p)
            size += len(p.text)
        if current:
            chunks.append(current)
        # Preserve the split decision even if only one source slot remains.
        # _run_split bounds actual calls; truncating here could turn multiple
        # chunks into a "single" unsplit request with all original passages.
        return chunks

    def _run_split(self, profile: TargetProfile, source_metadata: dict,
                   chunks: list[list[Passage]], policy: dict) -> list[ModelCallResult]:
        """One model call per chunk using the top-priority usable model, so a long
        article still gets fully covered without exceeding the input budget."""
        models = sorted(policy.get("models", []), key=lambda x: x.get("priority", 99))
        adapter = next((self.registry.get(s["model_id"]) for s in models
                        if self.registry.get(s["model_id"])
                        and self.registry.get(s["model_id"]).usable), None)
        if adapter is None:
            return []
        results: list[ModelCallResult] = []
        for chunk in chunks:
            if (len(results) >= self.budget.max_model_calls_per_source_link
                    or not self.budget.can_call(1)):
                break
            messages = build_bundle_messages(profile, source_metadata, chunk,
                                             self.output_limits, task_context=self.task_context)
            res = self._complete(adapter, messages)
            self.budget.record(1)
            results.append(res)
        return results

    # --- early stop --------------------------------------------------------

    def _early_stop(self, res: ModelCallResult, conf_threshold: float) -> bool:
        if not self.early_stop.get("enabled", True):
            return False
        if res.status != "success" or res.parsed is None:
            return False
        cond = self.early_stop.get("stop_fallback_when", {})
        confidence = res.parsed.get("confidence", 0.0)
        conf_ok = (confidence >= cond.get("confidence_gte", 0.75)
                   and confidence >= conf_threshold)
        schema_ok = bool(res.parsed.get("schema_version"))
        objects_ok = True
        if cond.get("required_objects_extracted"):
            objects_ok = any(
                res.parsed.get(k) for k in ("facts", "metrics", "events", "relations"))
        return schema_ok and conf_ok and objects_ok
