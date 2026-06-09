"""Model Call Planner (spec section 11 + 15).

Decides whether to call a model, which configured task/call_mode to use, and
executes it. Implements all spec call modes: no_model / single_model /
priority_fallback / parallel_ensemble / cascade / arbitration / batch_triage.
Enforces per-run and per-link budgets, early-stop, and one-shot bundle
extraction with optional splitting when the selected text is too long. It does
NOT decide cheap-vs-strong; all model selection comes from config.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mic.config import MICConfig
from mic.modeling.adapter import ModelCallResult, ModelRegistry
from mic.modeling.prompts import (
    build_arbitration_messages,
    build_batch_triage_messages,
    build_bundle_messages,
)
from mic.profile import TargetProfile
from mic.reader import ReadResult
from mic.schemas import Passage, TriageResult


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
        self.max_input_chars = self.budgets_cfg.get("max_input_chars_per_model_call", 8000)
        self.output_limits = (config.output_schema or {}).get("limits", {})

    # --- decision ----------------------------------------------------------

    def should_call_model(self, triage: TriageResult, source_type: str,
                          duplicate: bool) -> tuple[bool, str]:
        gov = (self.config.call_governance or {}).get("triggers", {})
        skip = gov.get("skip_model_when", {})
        if duplicate and skip.get("duplicate_canonical_url_analyzed"):
            return False, "duplicate_canonical_url"
        if not triage.need_model:
            return False, "triage_no_model"
        if not self.budget.can_call(1):
            return False, "run_budget_exhausted"
        return True, "ok"

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

        chunks = self._passage_chunks(read.passages)
        if len(chunks) > 1 and self._split_enabled():
            outputs = self._run_split(profile, source_metadata, chunks, policy)
            return LinkModelResult(read.source_link_id, task, call_mode,
                                   outputs=outputs, was_split=True)

        messages = build_bundle_messages(profile, source_metadata, read.passages,
                                         self.output_limits)
        outputs = self._dispatch(call_mode, policy, messages)
        return LinkModelResult(read.source_link_id, task, call_mode, outputs=outputs)

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
        if not self.budget.can_call(1):
            return []
        for spec in models:
            adapter = self.registry.get(spec["model_id"])
            if adapter is None or not adapter.usable:
                continue
            res = adapter.complete(messages)
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
            res = adapter.complete(messages)
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
            res = adapter.complete(messages)
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
            res = adapter.complete(messages)
            self.budget.record(1)
            results.append(res)
            # Unlike fallback, cascade keeps prior outputs; it only stops calling
            # further models once corroboration is strong enough.
            if (res.status == "success" and res.parsed
                    and res.parsed.get("confidence", 0.0) >= threshold):
                break
        return results

    # --- batch SERP triage (spec 11.1 batch_triage + 15.2 D) --------------

    def batch_triage(self, items: list[dict]) -> dict[str, dict]:
        """Triage a batch of search-hit summaries in a single model call.

        ``items`` = [{"id", "title", "snippet"}]. Returns id -> decision dict.
        Respects a dedicated batch-triage sub-budget so it never starves
        extraction calls.
        """
        policy = self.policies.get("serp_batch_triage", {})
        if not items or not policy:
            return {}
        if not self.budget.can_call_batch_triage():
            return {}
        models = sorted(policy.get("models", []), key=lambda x: x.get("priority", 99))
        messages = build_batch_triage_messages(items)
        for spec in models:
            adapter = self.registry.get(spec["model_id"])
            if adapter is None or not adapter.usable:
                continue
            res = adapter.complete(messages)
            self.budget.record(1)
            self.budget.batch_triage_calls_used += 1
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
            profile, source_metadata, read.passages, self.output_limits, conflicts)
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
        # Bound chunk count by the per-link call budget.
        return chunks[: self.budget.max_model_calls_per_source_link]

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
            if not self.budget.can_call(1):
                break
            messages = build_bundle_messages(profile, source_metadata, chunk,
                                             self.output_limits)
            res = adapter.complete(messages)
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
        conf_ok = res.parsed.get("confidence", 0.0) >= cond.get("confidence_gte", 0.75)
        schema_ok = bool(res.parsed.get("schema_version"))
        return schema_ok and conf_ok
