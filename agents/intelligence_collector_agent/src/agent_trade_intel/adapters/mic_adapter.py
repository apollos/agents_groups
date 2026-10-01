from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

from agent_trade_intel.errors import ToolUnavailable
from agent_trade_intel.logging_setup import get_logger
from agent_trade_intel.quality import mic_output_summary

from .common import ToolResult

logger = get_logger("adapters.mic")

DEFAULT_TIMEOUT_SECONDS = 900
EXECUTION_MODES = ("subprocess", "in_process")

# Design 15 error table. ``retryable`` means "the queue may schedule a bounded retry with a
# new attempt"; it never means "retry the same attempt blindly".
NON_RETRYABLE_MIC_CODES = frozenset({
    "MIC_CONFIG_ERROR", "MIC_GUI_UNAVAILABLE", "MIC_DEPENDENCY_MISSING",
    "MIC_BROWSER_LAUNCH_FAILED", "MIC_CLEANUP_INCOMPLETE",
})
RETRYABLE_MIC_CODES = frozenset({
    "MIC_PROFILE_BUSY", "MIC_TIMEOUT", "MIC_CANCELLED", "MIC_WORKER_CRASHED", "MIC_TOOL_FAILED",
})

# MIC / worker error codes -> agent error codes.
_MIC_CODE_MAP = {
    "gui_unavailable": "MIC_GUI_UNAVAILABLE",
    "dependency_missing": "MIC_DEPENDENCY_MISSING",
    "browser_missing": "MIC_DEPENDENCY_MISSING",
    "browser_launch_failed": "MIC_BROWSER_LAUNCH_FAILED",
    "profile_busy": "MIC_PROFILE_BUSY",
    "config_error": "MIC_CONFIG_ERROR",
    "ConfigError": "MIC_CONFIG_ERROR",
    "cleanup_incomplete": "MIC_CLEANUP_INCOMPLETE",
    "mic_timeout": "MIC_TIMEOUT",
    "mic_timed_out": "MIC_TIMEOUT",
    "deadline_before_start": "MIC_TIMEOUT",
    "mic_cancelled": "MIC_CANCELLED",
    "worker_crashed": "MIC_WORKER_CRASHED",
    "result_missing": "MIC_WORKER_CRASHED",
    "worker_spawn_failed": "MIC_DEPENDENCY_MISSING",
}


class MICCollectError(Exception):
    """Structured MIC failure carried from the supervised run to the ToolResult."""

    def __init__(self, code: str, message: str, *, retryable: bool, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


def map_mic_error_code(code: str | None) -> str:
    if not code:
        return "MIC_TOOL_FAILED"
    return _MIC_CODE_MAP.get(str(code), "MIC_TOOL_FAILED")


def is_retryable_code(code: str) -> bool:
    return code not in NON_RETRYABLE_MIC_CODES


class MICAdapter:
    """Adapter for market_intelligence_collector.

    This adapter does not mock MIC. ``collect`` runs one MIC collection in a supervised child
    process (``mic.browser.runner.RunSupervisor``), so the wall-clock timeout, cancellation on
    lease loss and resource cleanup are real: the worker's process group is TERMed and, after a
    grace period, KILLed. ``execution_mode="in_process"`` keeps the historical thread-based path
    for environments that explicitly opt out; its timeout only stops waiting.
    """

    tool_name = "market_intelligence_collector"

    def __init__(self, config_dir: str | None = None, *, timeout_seconds: int | None = None,
                 execution_mode: str = "subprocess", runs_root: str | Path | None = None,
                 python_executable: str | None = None, worker_module: str | None = None,
                 grace_seconds: float = 5.0):
        if execution_mode not in EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of {EXECUTION_MODES}, got {execution_mode!r}")
        self.config_dir = config_dir
        self.timeout_seconds = int(timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
        self.execution_mode = execution_mode
        self.runs_root = Path(runs_root) if runs_root else None
        self.python_executable = python_executable or sys.executable
        self.worker_module = worker_module
        self.grace_seconds = float(grace_seconds)
        self.last_outcome: dict[str, Any] | None = None

    # --- MIC entry points ------------------------------------------------------------------

    def _api(self):
        try:
            from mic.api import AnalystAPI  # type: ignore
            from mic.config import load_config
        except Exception as exc:  # pragma: no cover - depends on external tool install
            raise ToolUnavailable("market_intelligence_collector package 'mic' is not importable") from exc
        return AnalystAPI(config=load_config(self.config_dir))

    def _supervisor(self):
        try:
            from mic.browser.runner import RunSupervisor  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on external tool install
            raise ToolUnavailable("market_intelligence_collector worker runner is not importable") from exc
        kwargs: dict[str, Any] = {
            "runs_root": self.runs_root or (Path.cwd() / "mic_runs"),
            "python_executable": self.python_executable,
            "grace_seconds": self.grace_seconds,
        }
        if self.worker_module:
            kwargs["worker_module"] = self.worker_module
        return RunSupervisor(**kwargs)

    # --- collect ---------------------------------------------------------------------------

    def collect(self, *, target_id: str, task_profile: dict[str, Any], task_key: str | None = None,
                attempt_id: str | None = None, cancel_event: threading.Event | None = None,
                on_heartbeat: Callable[[], None] | None = None,
                deadline_seconds: float | None = None) -> ToolResult:
        request = {"target_id": target_id, "task_profile": task_profile,
                   "task_key": task_key, "attempt_id": attempt_id, "execution_mode": self.execution_mode}
        result = ToolResult(tool_name=self.tool_name, operation="collect_intelligence", request=request)
        logger.info(
            "MIC collect: target=%s budget=%s timeout=%ss mode=%s attempt=%s",
            target_id, task_profile.get("budget_profile"), self.timeout_seconds, self.execution_mode, attempt_id,
        )
        self.last_outcome = None
        try:
            report = self._collect_with_timeout(
                target_id, task_profile, task_key=task_key, attempt_id=attempt_id,
                cancel_event=cancel_event, on_heartbeat=on_heartbeat, deadline_seconds=deadline_seconds)
            self._finish_success(result, report)
        except FutureTimeoutError:
            self._finish_error(result, "MIC_TIMEOUT",
                               f"MIC collect exceeded {self.timeout_seconds}s hard timeout", retryable=True)
            logger.warning("MIC collect timed out for %s after %ss", target_id, self.timeout_seconds)
        except MICCollectError as exc:
            self._finish_error(result, exc.code, str(exc), retryable=exc.retryable, details=exc.details)
            logger.warning("MIC collect failed for %s: %s %s", target_id, exc.code, exc)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a structured ToolResult
            self._finish_error(result, "MIC_TOOL_FAILED", str(exc), retryable=True)
            logger.warning("MIC collect failed for %s: %s", target_id, exc)
        return result.finish()

    def _finish_success(self, result: ToolResult, report: dict[str, Any]) -> None:
        diag = report.get("collection_diagnostics") if isinstance(report, dict) else None
        diag = diag if isinstance(diag, dict) else {}
        execution_status = str(diag.get("execution_status") or "completed")
        if execution_status != "completed":
            # The worker returned a report, but MIC itself says the run did not complete
            # (environment fault, cancel, deadline). Surface that as the error, keep the report.
            code = map_mic_error_code(diag.get("stop_reason") or diag.get("error_code")) \
                if execution_status == "failed" else ("MIC_TIMEOUT" if execution_status == "timed_out" else "MIC_CANCELLED")
            result.result = report
            result.result_ref = _run_ref(report)
            self._finish_error(result, code, f"MIC run {execution_status}: {diag.get('stop_reason')}",
                               retryable=is_retryable_code(code), details={"collection_diagnostics": diag})
            return
        result.status = "success"
        result.result = report
        result.result_ref = _run_ref(report)
        output = mic_output_summary(report)
        summary = report.get("summary", {}) if isinstance(report, dict) else {}
        result.quality = {
            **output,
            "usable": output["structured_output_count"] > 0,
            "queries_executed": summary.get("queries_executed"),
            "links_read": summary.get("links_read"),
            "model_calls": summary.get("model_calls"),
        }
        if diag:
            result.quality["collection_diagnostics"] = diag

    def _finish_error(self, result: ToolResult, code: str, message: str, *, retryable: bool,
                      details: dict[str, Any] | None = None) -> None:
        result.status = "failed"
        error: dict[str, Any] = {"error_code": code, "error_message": message, "retryable": retryable}
        if details:
            error.update({k: v for k, v in details.items() if v is not None})
        result.errors.append(error)
        result.quality = {"usable": False}
        if details and details.get("collection_diagnostics"):
            result.quality["collection_diagnostics"] = details["collection_diagnostics"]

    def _collect_with_timeout(self, target_id: str, task_profile: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Run one MIC collection under the hard timeout; test seam for the gate tests."""
        if self.execution_mode == "subprocess":
            return self._collect_supervised(target_id, task_profile, **kwargs)
        return self._collect_in_process(target_id, task_profile)

    def _collect_in_process(self, target_id: str, task_profile: dict[str, Any]) -> dict[str, Any]:
        """Legacy path: MIC runs in this process and the timeout only stops waiting.

        Kept for explicit opt-out only. The orphaned thread finishes in the background, so this
        is not a hard timeout; the supervised subprocess mode is the default.
        """
        api = self._api()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mic-collect")
        try:
            future = executor.submit(api.collect_intelligence, target_id=target_id, task_profile=task_profile)
            return future.result(timeout=self.timeout_seconds)
        finally:
            executor.shutdown(wait=False)

    def _collect_supervised(self, target_id: str, task_profile: dict[str, Any], *,
                            task_key: str | None = None, attempt_id: str | None = None,
                            cancel_event: threading.Event | None = None,
                            on_heartbeat: Callable[[], None] | None = None,
                            deadline_seconds: float | None = None) -> dict[str, Any]:
        supervisor = self._supervisor()
        deadline = float(self.timeout_seconds)
        if deadline_seconds is not None:
            deadline = min(deadline, float(deadline_seconds))
        outcome = supervisor.run(
            target_id=target_id, task_profile=task_profile, deadline_seconds=deadline,
            config_dir=self.config_dir, task_key=task_key, attempt_id=attempt_id,
            cancel_event=cancel_event, on_heartbeat=on_heartbeat)
        self.last_outcome = {
            "status": outcome.status, "attempt_id": outcome.attempt_id, "run_dir": outcome.run_dir,
            "exit_code": outcome.exit_code, "elapsed_seconds": outcome.elapsed_seconds,
            "cleanup": outcome.cleanup, "worker_pid": outcome.worker_pid,
            "budget_used": outcome.budget_used, "gateway_requests_sent": outcome.gateway_requests_sent,
            "error_code": outcome.error_code,
        }
        details = {
            "attempt_id": outcome.attempt_id, "run_dir": outcome.run_dir, "cleanup": outcome.cleanup,
            "exit_code": outcome.exit_code, "elapsed_seconds": outcome.elapsed_seconds,
            "budget_used": outcome.budget_used or None,
            "gateway_requests_sent": outcome.gateway_requests_sent,
            "stderr_tail": outcome.stderr_tail[-800:] if outcome.stderr_tail else None,
        }
        if outcome.status == "completed" and isinstance(outcome.report, dict):
            return outcome.report
        if outcome.status == "cleanup_incomplete":
            raise MICCollectError(
                "MIC_CLEANUP_INCOMPLETE",
                outcome.error_message or "worker process group not reaped; profile may still be in use",
                retryable=False, details=details)
        if outcome.status == "timed_out":
            raise MICCollectError(
                "MIC_TIMEOUT",
                f"MIC collect exceeded {deadline:.0f}s hard timeout; worker reaped "
                f"(gateway requests already sent: {outcome.gateway_requests_sent}, responses unknown)",
                retryable=True, details=details)
        if outcome.status == "cancelled":
            raise MICCollectError(
                "MIC_CANCELLED", outcome.error_message or "collection cancelled by the agent",
                retryable=True, details=details)
        if outcome.status in ("worker_crashed", "result_missing"):
            raise MICCollectError(
                "MIC_WORKER_CRASHED", outcome.error_message or "worker exited without a result",
                retryable=True, details=details)
        code = map_mic_error_code(outcome.error_code)
        if outcome.report is not None:
            details["collection_diagnostics"] = (outcome.report or {}).get("collection_diagnostics")
        raise MICCollectError(code, outcome.error_message or f"MIC worker failed ({outcome.error_code})",
                              retryable=is_retryable_code(code), details=details)

    # --- read-only -------------------------------------------------------------------------

    def get_recent_events(self, target_id: str, since: str = "30d") -> ToolResult:
        request = {"target_id": target_id, "since": since}
        result = ToolResult(tool_name=self.tool_name, operation="get_recent_events", request=request)
        try:
            rows = self._api().get_recent_events(target_id, since=since)
            result.status = "success"
            result.result = {"events": rows}
            output = mic_output_summary(result.result)
            result.quality = {**output, "usable": output["structured_output_count"] > 0}
        except Exception as exc:  # noqa: BLE001 - read-only path reports, never raises
            result.status = "failed"
            result.errors.append({"error_code": "MIC_READ_FAILED", "error_message": str(exc), "retryable": True})
            result.quality = {"usable": False}
        return result.finish()


def _run_ref(report: Any) -> str | None:
    if isinstance(report, dict) and report.get("search_run_id"):
        return f"mic://search_runs/{report.get('search_run_id')}"
    return None
