"""Supervised MIC collection subprocess (design section 8.2 - 8.3).

One collection task = one worker process in its own process group. The parent
(``RunSupervisor``) passes only serialisable parameters through a request
file, maintains a parent heartbeat, honours a cancel event and a wall-clock
deadline, and on timeout/cancel asks for cooperative stop (cancel file +
SIGTERM) before killing the owned process group after a short grace period.
The final report is read from a separately written result file - never
reconstructed from partial stdout.

The worker side lives in ``mic.browser.worker`` and is started with the same
interpreter (``sys.executable -m mic.browser.worker <request.json>``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mic.browser.profile_lock import ensure_private_dir, write_private_file
from mic.utils import new_id

WORKER_MODULE = "mic.browser.worker"
DEFAULT_GRACE_SECONDS = 5.0
PARENT_HEARTBEAT_SECONDS = 5.0

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_CANCELLED = "cancelled"
STATUS_WORKER_CRASHED = "worker_crashed"
STATUS_RESULT_MISSING = "result_missing"
STATUS_CLEANUP_INCOMPLETE = "cleanup_incomplete"


@dataclass
class SupervisedOutcome:
    status: str
    attempt_id: str
    run_dir: str
    report: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    exit_code: int | None = None
    stderr_tail: str = ""
    elapsed_seconds: float = 0.0
    budget_used: dict[str, Any] = field(default_factory=dict)
    cleanup: str = "complete"
    worker_pid: int | None = None
    gateway_requests_sent: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED and self.report is not None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_private_file(path, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def read_result_file(path: Path, expected_attempt_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load and validate the worker result; returns (payload, error_code)."""
    if not path.exists():
        return None, STATUS_RESULT_MISSING
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "result_corrupt"
    if not isinstance(payload, dict) or payload.get("attempt_id") != expected_attempt_id:
        return None, "result_attempt_mismatch"
    if payload.get("status") not in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED):
        return None, "result_status_invalid"
    return payload, None


def _tail(text: str, n: int = 2000) -> str:
    return text[-n:] if text else ""


class RunSupervisor:
    """Starts, monitors and reaps one MIC worker process."""

    def __init__(self, *, runs_root: Path, python_executable: str | None = None,
                 grace_seconds: float = DEFAULT_GRACE_SECONDS,
                 heartbeat_seconds: float = PARENT_HEARTBEAT_SECONDS,
                 extra_env: dict[str, str] | None = None,
                 worker_module: str = WORKER_MODULE,
                 cwd: str | None = None):
        self.runs_root = Path(runs_root)
        self.python_executable = python_executable or sys.executable
        self.grace_seconds = float(grace_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.extra_env = dict(extra_env or {})
        self.worker_module = worker_module
        self.cwd = cwd

    def run(self, *, target_id: str, task_profile: dict[str, Any],
            deadline_seconds: float, config_dir: str | None = None,
            task_key: str | None = None, attempt_id: str | None = None,
            cancel_event: threading.Event | None = None,
            on_heartbeat: Callable[[], None] | None = None,
            poll_seconds: float = 0.25,
            extra_request: dict[str, Any] | None = None) -> SupervisedOutcome:
        attempt_id = attempt_id or new_id("attempt")
        run_dir = ensure_private_dir(self.runs_root / attempt_id)
        request_path = run_dir / "request.json"
        result_path = run_dir / "result.json"
        cancel_path = run_dir / "cancel.json"
        parent_hb_path = run_dir / "parent_heartbeat.json"
        started = time.monotonic()
        started_epoch = time.time()
        deadline_epoch = started_epoch + float(deadline_seconds)

        request = {
            "attempt_id": attempt_id, "task_key": task_key, "target_id": target_id,
            "task_profile": task_profile, "config_dir": config_dir,
            "run_dir": str(run_dir), "result_path": str(result_path),
            "cancel_path": str(cancel_path), "parent_heartbeat_path": str(parent_hb_path),
            "parent_pid": os.getpid(), "deadline_epoch": deadline_epoch,
            "started_epoch": started_epoch,
            **(extra_request or {}),
        }
        write_json_atomic(request_path, request)
        self._write_parent_heartbeat(parent_hb_path, "running")

        env = {**os.environ, **self.extra_env, "MIC_WORKER_ATTEMPT_ID": attempt_id}
        # Keep the MIC run log next to the attempt artefacts (0700 run dir) unless the
        # deployment pinned MIC_LOG_DIR explicitly; otherwise mic.logging_utils would write
        # into the tool's source tree ``logs/``.
        if not env.get("MIC_LOG_DIR"):
            log_dir = run_dir / "logs"
            log_dir.mkdir(mode=0o700, exist_ok=True)
            env["MIC_LOG_DIR"] = str(log_dir)
        cmd = [self.python_executable, "-m", self.worker_module, str(request_path)]
        stderr_file = (run_dir / "worker.stderr.log").open("ab")
        stdout_file = (run_dir / "worker.stdout.log").open("ab")
        try:
            proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
                cmd, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=stdout_file, stderr=stderr_file, start_new_session=True)
        except OSError as exc:
            stderr_file.close()
            stdout_file.close()
            return SupervisedOutcome(status=STATUS_FAILED, attempt_id=attempt_id,
                                     run_dir=str(run_dir), error_code="worker_spawn_failed",
                                     error_message=str(exc))

        stop_reason: str | None = None
        last_hb = time.monotonic()
        try:
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                now = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    stop_reason = STATUS_CANCELLED
                    break
                if time.time() >= deadline_epoch:
                    stop_reason = STATUS_TIMED_OUT
                    break
                if now - last_hb >= self.heartbeat_seconds:
                    self._write_parent_heartbeat(parent_hb_path, "running")
                    if on_heartbeat is not None:
                        try:
                            on_heartbeat()
                        except Exception:  # noqa: BLE001 - lease renewal must not kill supervision
                            pass
                    last_hb = now
                time.sleep(poll_seconds)

            cleanup = "complete"
            if stop_reason is not None:
                write_json_atomic(cancel_path, {"reason": stop_reason, "at": time.time()})
                cleanup = self._terminate(proc)
            else:
                proc.wait()
        finally:
            stderr_file.close()
            stdout_file.close()

        elapsed = time.monotonic() - started
        stderr_tail = _tail(self._read(run_dir / "worker.stderr.log"))
        payload, err = read_result_file(result_path, attempt_id)
        outcome = SupervisedOutcome(status=STATUS_FAILED, attempt_id=attempt_id,
                                    run_dir=str(run_dir), exit_code=proc.returncode,
                                    stderr_tail=stderr_tail, elapsed_seconds=round(elapsed, 2),
                                    worker_pid=proc.pid)
        if payload is not None:
            outcome.budget_used = payload.get("budget_used") or {}
            outcome.gateway_requests_sent = payload.get("gateway_requests_sent")
        if stop_reason is not None:
            outcome.status = stop_reason
            outcome.cleanup = cleanup
            if cleanup != "complete":
                outcome.status = STATUS_CLEANUP_INCOMPLETE
                outcome.error_code = STATUS_CLEANUP_INCOMPLETE
                outcome.error_message = ("worker process group did not exit after TERM/KILL; "
                                         "do not retry on the same profile automatically")
            else:
                outcome.error_code = "mic_timeout" if stop_reason == STATUS_TIMED_OUT else "mic_cancelled"
                outcome.error_message = (f"worker stopped: {stop_reason} after {elapsed:.0f}s; "
                                         "requests already sent to the gateway may still complete")
            # A worker that managed to write a cancelled result keeps its budget numbers.
            return outcome
        if payload is None:
            outcome.status = STATUS_WORKER_CRASHED if proc.returncode not in (0,) else STATUS_RESULT_MISSING
            outcome.error_code = err or STATUS_RESULT_MISSING
            outcome.error_message = (f"worker exit_code={proc.returncode} without a valid result file "
                                     f"({err}); stdout/stderr are logs only and are not used as a report")
            return outcome
        if payload["status"] == STATUS_COMPLETED and isinstance(payload.get("report"), dict):
            outcome.status = STATUS_COMPLETED
            outcome.report = payload["report"]
            return outcome
        outcome.status = STATUS_CANCELLED if payload["status"] == STATUS_CANCELLED else STATUS_FAILED
        outcome.error_code = payload.get("error_code") or "mic_worker_failed"
        outcome.error_message = payload.get("error_message")
        outcome.report = payload.get("report") if isinstance(payload.get("report"), dict) else None
        return outcome

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _write_parent_heartbeat(self, path: Path, state: str) -> None:
        try:
            write_json_atomic(path, {"pid": os.getpid(), "state": state, "at_epoch": time.time()})
        except OSError:
            pass

    def _terminate(self, proc: subprocess.Popen) -> str:
        """TERM the owned process group, then KILL after grace. Returns cleanup state."""
        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return "complete"
        if pgid == os.getpgid(os.getpid()):
            pgid = None  # never signal our own group
        self._signal(proc, pgid, signal.SIGTERM)
        if self._wait(proc, self.grace_seconds):
            return "complete"
        self._signal(proc, pgid, signal.SIGKILL)
        if self._wait(proc, self.grace_seconds):
            return "complete"
        return STATUS_CLEANUP_INCOMPLETE

    @staticmethod
    def _signal(proc: subprocess.Popen, pgid: int | None, sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _wait(proc: subprocess.Popen, seconds: float) -> bool:
        try:
            proc.wait(timeout=seconds)
            return True
        except subprocess.TimeoutExpired:
            return False
