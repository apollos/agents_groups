from __future__ import annotations

from typing import Any

from .common import ToolResult
from agent_trade_intel.errors import ToolUnavailable
from agent_trade_intel.logging_setup import get_logger

logger = get_logger("adapters.mic")


class MICAdapter:
    """Adapter for market_intelligence_collector.

    This adapter does not mock MIC. It imports mic.api.AnalystAPI from the runtime environment.
    If MIC is not installed or not configured, the task fails with a tool-unavailable error.
    """

    tool_name = "market_intelligence_collector"

    def __init__(self, config_dir: str | None = None):
        self.config_dir = config_dir

    def _api(self):
        try:
            from mic.api import AnalystAPI  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on external tool install
            raise ToolUnavailable("market_intelligence_collector package 'mic' is not importable") from exc
        return AnalystAPI()

    def collect(self, *, target_id: str, task_profile: dict[str, Any]) -> ToolResult:
        request = {"target_id": target_id, "task_profile": task_profile}
        result = ToolResult(tool_name=self.tool_name, operation="collect_intelligence", request=request)
        logger.info("MIC collect: target=%s budget=%s", target_id, task_profile.get("budget_profile"))
        try:
            report = self._api().collect_intelligence(target_id=target_id, task_profile=task_profile)
            result.status = "success"
            result.result = report
            result.result_ref = f"mic://search_runs/{report.get('search_run_id')}" if report.get("search_run_id") else None
            result.quality = {
                "usable": True,
                "queries_executed": report.get("summary", {}).get("queries_executed"),
                "links_read": report.get("summary", {}).get("links_read"),
                "model_calls": report.get("summary", {}).get("model_calls"),
            }
        except Exception as exc:
            result.status = "failed"
            result.errors.append({"error_code": "MIC_TOOL_FAILED", "error_message": str(exc), "retryable": True})
            result.quality = {"usable": False}
            logger.warning("MIC collect failed for %s: %s", target_id, exc)
        return result.finish()

    def get_recent_events(self, target_id: str, since: str = "30d") -> ToolResult:
        request = {"target_id": target_id, "since": since}
        result = ToolResult(tool_name=self.tool_name, operation="get_recent_events", request=request)
        try:
            rows = self._api().get_recent_events(target_id, since=since)
            result.status = "success"
            result.result = {"events": rows}
            result.quality = {"usable": True}
        except Exception as exc:
            result.status = "failed"
            result.errors.append({"error_code": "MIC_READ_FAILED", "error_message": str(exc), "retryable": True})
            result.quality = {"usable": False}
        return result.finish()
