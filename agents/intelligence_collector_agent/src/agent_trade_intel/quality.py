from __future__ import annotations

from typing import Any

from .adapters.common import ToolResult


MIC_STRUCTURED_FIELDS = (
    "facts", "metrics", "events", "relations", "risks", "catalysts",
    "customer_supplier_signals", "price_cost_margin_signals", "policy_signals",
)


def mic_output_summary(report: dict[str, Any], ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe actual structured output, independent of tool execution success.

    Briefs, questions, search hits and coverage gaps alone are not usable structured
    intelligence. Facts/metrics-only and cache-only runs can still produce output.
    This is an output-presence check, not semantic verification of the claims.
    """
    declared = report.get("structured_outputs") or {}
    declared = declared if isinstance(declared, dict) else {}
    counts = {}
    for key in MIC_STRUCTURED_FIELDS:
        value = declared.get(key, 0)
        try:
            counts[key] = max(0, int(value)) if not isinstance(value, bool) else 0
        except (TypeError, ValueError, OverflowError):
            counts[key] = 0
    events = report.get("all_events") or report.get("top_events") or report.get("events") or []
    relations = report.get("top_relations") or []
    counts["events"] = max(counts["events"], len(events) if isinstance(events, list) else 0)
    if report.get("event_resolution_protocol"):
        # Pending identity decisions remain available for review but are not
        # counted as confirmed business events / usable event-only output.
        counts["events"] = sum(1 for e in events if (e.get("event_resolution") or {}).get("status") == "resolved")
        if ledger is not None:
            counts["events"] = sum(int(ledger.get(k, 0)) for k in ("events", "events_linked", "events_replayed"))
    counts["relations"] = max(counts["relations"], len(relations) if isinstance(relations, list) else 0)
    total = sum(counts.values())
    return {"output_status": "structured_output" if total else "no_structured_output",
            "structured_output_count": total, "structured_counts": counts}


_DIAG_KEYS = ("execution_status", "search_status", "search_reason", "read_status", "output_status", "usable",
              "stop_reason", "budget_used", "elapsed_seconds", "provider", "browser_run", "attempt_id",
              "search_outcomes", "read_failures", "page_attempts", "auth_context", "cleanup")

_DIAG_FAULT_CODES = {
    "gui_unavailable": "MIC_GUI_UNAVAILABLE", "dependency_missing": "MIC_DEPENDENCY_MISSING",
    "browser_missing": "MIC_DEPENDENCY_MISSING", "browser_launch_failed": "MIC_BROWSER_LAUNCH_FAILED",
    "profile_busy": "MIC_PROFILE_BUSY", "run_deadline": "MIC_TIMEOUT", "cancelled": "MIC_CANCELLED",
}


def _diag_summary(diag: dict[str, Any]) -> dict[str, Any]:
    """Compact, log-safe copy of MIC collection_diagnostics (no cookie values ever appear here)."""
    return {k: diag.get(k) for k in _DIAG_KEYS if k in diag}


def _diag_error_code(diag: dict[str, Any]) -> str | None:
    reason = diag.get("stop_reason") or diag.get("error_code")
    return _DIAG_FAULT_CODES.get(str(reason), None) if reason else None


class QualityGate:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.minimum_quality = float(config.get("quality", {}).get("minimum_quality_for_public_pool", 0.8))
        self.minimum_quality_for_trading = float(
            config.get("quality", {}).get("minimum_quality_for_trading_ready", self.minimum_quality)
        )
        self.mic_rules = dict(config.get("quality", {}).get("mic", {}) or {})

    def evaluate(self, result: ToolResult, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if result.tool_name == "stock_data_collector":
            return self._stock_quality(result)
        if result.tool_name == "market_intelligence_collector":
            return self._mic_quality(result, context or {})
        if result.status == "success":
            return {"decision": "accept", "severity": "P3", "usable": True, "issues": []}
        return {"decision": "reject", "severity": "P1", "usable": False, "issues": result.errors}

    def _stock_quality(self, result: ToolResult) -> dict[str, Any]:
        q = result.quality or {}
        issues: list[dict[str, Any]] = []
        status = q.get("status") or result.status
        errors = result.errors or q.get("errors") or []
        if result.status == "failed" or status == "failed":
            issues.extend(errors)
        if q.get("persistence_saved") is False:
            issues.append({"issue_type": "persistence_failed", "severity": "critical", "error_code": "STORAGE_FAILED"})
        conflicts = q.get("conflicts") or []
        for c in conflicts:
            if c.get("severity") in {"high", "critical"}:
                issues.append({"issue_type": "provider_conflict", **c})
        quality_score = q.get("data_quality")
        quality_below_public = False
        if quality_score is not None:
            try:
                quality_below_public = float(quality_score) < self.minimum_quality
            except (TypeError, ValueError):
                quality_below_public = True
            if quality_below_public:
                issues.append(
                    {
                        "issue_type": "data_quality_below_threshold",
                        "severity": "medium",
                        "data_quality": quality_score,
                        "minimum_quality": self.minimum_quality,
                    }
                )
        critical = any(i.get("severity") == "critical" for i in issues)
        high = any(i.get("severity") == "high" for i in issues)
        auth_errors = [e for e in errors if e.get("error_code") in {"TOKEN_MISSING", "AUTH_FAILED", "PERMISSION_DENIED"}]
        storage_errors = [e for e in errors if e.get("error_code") in {"STORAGE_FAILED", "RAW_SAVE_FAILED"}]
        if critical or auth_errors or storage_errors:
            return {"decision": "quarantine", "severity": "P0", "usable": False, "issues": issues + auth_errors + storage_errors, "data_quality": quality_score}
        if high:
            return {"decision": "accept_with_review", "severity": "P1", "usable": True, "issues": issues, "data_quality": quality_score}
        if quality_below_public:
            return {
                "decision": "accept_degraded",
                "severity": "P2",
                "usable": False,
                "issues": issues,
                "data_quality": quality_score,
            }
        if status == "partial_success":
            return {"decision": "accept_degraded", "severity": "P2", "usable": bool(q.get("usable", True)), "issues": errors, "data_quality": quality_score}
        if result.status == "success" and q.get("usable", True):
            return {"decision": "accept", "severity": "P3", "usable": True, "issues": [], "data_quality": quality_score}
        return {"decision": "reject", "severity": "P1", "usable": False, "issues": issues or errors, "data_quality": quality_score}

    def _mic_quality(self, result: ToolResult, context: dict[str, Any]) -> dict[str, Any]:
        diag = (result.quality or {}).get("collection_diagnostics")
        if not isinstance(diag, dict):
            diag = (result.result or {}).get("collection_diagnostics") if isinstance(result.result, dict) else None
        diag = diag if isinstance(diag, dict) else {}
        if result.status != "success":
            out = {"decision": "reject", "severity": "P1", "usable": False,
                   "execution_status": str(diag.get("execution_status") or "failed"),
                   "output_status": str(diag.get("output_status") or "unknown"), "issues": list(result.errors)}
            if diag:
                out["collection_diagnostics"] = _diag_summary(diag)
            return out
        report = result.result if isinstance(result.result, dict) else {}
        summary = report.get("summary", {}) or {}
        ledger = (result.quality or {}).get("event_ledger")
        output = mic_output_summary(report, ledger=ledger)
        usable = output["structured_output_count"] > 0
        links_read = int(summary.get("links_read") or 0)
        model_calls = int(summary.get("model_calls") or 0)
        cached = int(summary.get("cached_or_reused_results") or 0)
        issues: list[dict[str, Any]] = []
        if ledger and ledger.get("events_pending"):
            issues.append({"issue_type": "event_resolution_pending", "severity": "medium",
                           "detail": f"{ledger['events_pending']} source events await semantic resolution"})
        if not usable:
            issues.append({"issue_type": "no_structured_output", "severity": "medium",
                           "detail": "tool completed but produced no structured facts, metrics, events or signals"})
        if result.operation == "collect_intelligence" and links_read == 0 and model_calls == 0 and not (cached and usable):
            issues.append({"issue_type": "no_links_or_model_calls", "severity": "medium"})
        if summary.get("queries_skipped_by_hit_budget", 0):
            issues.append({"issue_type": "budget_tight", "severity": "medium"})
        # Design 15: each acceptance question is answered separately by MIC's diagnostics.
        execution_status = "completed"
        if diag:
            execution_status = str(diag.get("execution_status") or "completed")
            if execution_status != "completed":
                usable = False
                issues.append({"issue_type": f"execution_{execution_status}", "severity": "high",
                               "error_code": _diag_error_code(diag),
                               "detail": f"MIC run did not complete: {diag.get('stop_reason')}"})
            search_status = diag.get("search_status")
            if search_status in ("blocked", "failed"):
                issues.append({"issue_type": f"search_{search_status}", "severity": "medium",
                               "detail": str(diag.get("search_reason") or "")})
            elif search_status == "empty":
                issues.append({"issue_type": "search_no_candidates", "severity": "medium",
                               "detail": "search completed but produced no relevant candidates"})
            if diag.get("read_status") == "failed":
                issues.append({"issue_type": "read_failed", "severity": "medium",
                               "detail": f"no selected link passed fetch + scope check: {diag.get('read_failures')}"})
            if diag.get("output_status") == "no_model_call" and usable and not cached:
                issues.append({"issue_type": "output_without_model_call", "severity": "medium"})
            if diag.get("cleanup") and (diag["cleanup"].get("cleanup") not in (None, "complete")):
                issues.append({"issue_type": "cleanup_incomplete", "severity": "high",
                               "detail": str(diag["cleanup"].get("problems"))})
        issues.extend(self._mic_research_issues(report, context))
        significant = [i for i in issues if i.get("severity") != "low"]
        if execution_status != "completed":
            decision, severity = "reject", "P1"
        else:
            decision = "accept_degraded" if significant else "accept"
            severity = "P2" if significant else "P3"
        out = {"decision": decision, "severity": severity, "usable": usable,
               "execution_status": execution_status, **output, "issues": issues}
        if ledger is not None:
            out["event_ledger"] = ledger
        if diag:
            out["collection_diagnostics"] = _diag_summary(diag)
        return out

    def _mic_research_issues(self, report: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
        """Research-quality checks beyond "did the tool run": event coverage and evidence.

        These never fail the run (data is still persisted); they degrade the decision so the
        gaps show up as P2 data-quality issues instead of being silently accepted.
        """
        issues: list[dict[str, Any]] = []
        events = report.get("all_events") or report.get("top_events") or []
        priority = str(context.get("priority") or "normal")
        if not events and priority in {"high", "urgent"} and bool(self.mic_rules.get("flag_high_priority_zero_events", True)):
            issues.append(
                {
                    "issue_type": "high_priority_zero_events",
                    "severity": "medium",
                    "detail": f"high-priority target produced no top_events (priority={priority})",
                }
            )
        if events and bool(self.mic_rules.get("require_source_url", True)):
            missing = sum(1 for ev in events if not ((ev.get("source") or {}).get("url") or ev.get("source_url")))
            if missing == len(events):
                issues.append(
                    {
                        "issue_type": "events_missing_source_url",
                        "severity": "medium",
                        "detail": f"all {len(events)} events lack a source URL; evidence cannot be verified",
                    }
                )
        if events and bool(self.mic_rules.get("require_published_at_for_high_confidence", True)):
            threshold = float(self.mic_rules.get("high_confidence_threshold", 0.75))
            stale = [
                ev
                for ev in events
                if (ev.get("confidence") or 0) >= threshold
                and not ((ev.get("source") or {}).get("published_at") or ev.get("published_at"))
            ]
            if stale:
                issues.append(
                    {
                        "issue_type": "high_confidence_missing_published_at",
                        "severity": "medium",
                        "detail": f"{len(stale)} high-confidence event(s) lack published_at; freshness cannot be judged",
                    }
                )
        if events and bool(self.mic_rules.get("flag_low_authority_sources", True)):
            weak = {"media", "social", "unknown", None, ""}
            all_weak = all(((ev.get("source") or {}).get("source_type") or ev.get("source_type")) in weak for ev in events)
            if all_weak:
                issues.append(
                    {
                        "issue_type": "low_authority_sources_only",
                        "severity": "medium",
                        "detail": "no exchange/regulator/official corroboration among event sources",
                    }
                )
        issues.extend(self._variable_coverage_issues(events, context))
        return issues

    def _variable_coverage_issues(self, events: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
        """Flag runs whose events cover none / few of the target's tracking variables (V0.8)."""
        if not bool(self.mic_rules.get("flag_tracking_variable_coverage", True)):
            return []
        target = context.get("target") or {}
        expected = {str(v) for v in (target.get("tracking_variables") or []) if v}
        if not expected or not events:
            return []
        covered: set[str] = set()
        for ev in events:
            for tv in ev.get("tracking_variables") or []:
                variable = tv if isinstance(tv, str) else (tv or {}).get("variable")
                if variable:
                    covered.add(str(variable))
        missing = sorted(expected - covered)
        if len(missing) == len(expected):
            return [
                {
                    "issue_type": "zero_tracking_variable_coverage",
                    "severity": "medium",
                    "detail": "target declares tracking_variables but this run covered none of them",
                    "missing_variables": missing,
                }
            ]
        if len(missing) / len(expected) >= float(self.mic_rules.get("low_variable_coverage_ratio", 0.7)):
            return [
                {
                    "issue_type": "low_tracking_variable_coverage",
                    "severity": "low",
                    "detail": f"only {len(expected) - len(missing)}/{len(expected)} tracking variables covered",
                    "missing_variables": missing,
                }
            ]
        return []
