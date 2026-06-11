from __future__ import annotations

from typing import Any

from .adapters.common import ToolResult


class QualityGate:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.minimum_quality = float(config.get("quality", {}).get("minimum_quality_for_public_pool", 0.8))
        self.minimum_quality_for_trading = float(
            config.get("quality", {}).get("minimum_quality_for_trading_ready", self.minimum_quality)
        )

    def evaluate(self, result: ToolResult) -> dict[str, Any]:
        if result.tool_name == "stock_data_collector":
            return self._stock_quality(result)
        if result.tool_name == "market_intelligence_collector":
            return self._mic_quality(result)
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

    def _mic_quality(self, result: ToolResult) -> dict[str, Any]:
        if result.status != "success":
            return {"decision": "reject", "severity": "P1", "usable": False, "issues": result.errors}
        summary = result.result.get("summary", {}) if isinstance(result.result, dict) else {}
        links_read = int(summary.get("links_read") or 0)
        model_calls = int(summary.get("model_calls") or 0)
        if links_read == 0 and model_calls == 0:
            return {
                "decision": "accept_degraded",
                "severity": "P2",
                "usable": True,
                "issues": [{"issue_type": "no_links_or_model_calls", "severity": "medium"}],
            }
        if summary.get("queries_skipped_by_hit_budget", 0):
            return {
                "decision": "accept_degraded",
                "severity": "P2",
                "usable": True,
                "issues": [{"issue_type": "budget_tight", "severity": "medium"}],
            }
        return {"decision": "accept", "severity": "P3", "usable": True, "issues": []}
