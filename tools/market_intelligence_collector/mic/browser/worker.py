"""MIC collection worker (design 8.2, child side).

Started by ``RunSupervisor`` as ``python -m mic.browser.worker <request.json>``.
Creates the API, database connection, HTTP client and browser session in its
own process, runs one collection with the remaining deadline, and writes the
result file atomically. Signal handlers only set the cancel flag; the normal
execution path performs the shutdown. Parent loss (pid gone or stale parent
heartbeat) also cancels the run.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from mic.browser.runner import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED, write_json_atomic

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_CANCELLED = 3
EXIT_BAD_REQUEST = 4

HEARTBEAT_SECONDS = 3.0
PARENT_STALE_SECONDS = 30.0
START_MARGIN_SECONDS = 1.0


class CancelFlag:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: str | None = None

    def set(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


def _parent_alive(parent_pid: int, hb_path: Path) -> bool:
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if hb_path.exists():
        try:
            data = json.loads(hb_path.read_text(encoding="utf-8"))
            if time.time() - float(data.get("at_epoch", 0)) > PARENT_STALE_SECONDS:
                return False
        except (OSError, ValueError):
            return True
    return True


def _heartbeat_loop(request: dict[str, Any], cancel: CancelFlag, stop: threading.Event,
                    run_dir: Path) -> None:
    hb_path = run_dir / "heartbeat.json"
    cancel_path = Path(request["cancel_path"])
    parent_hb = Path(request["parent_heartbeat_path"])
    parent_pid = int(request["parent_pid"])
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            write_json_atomic(hb_path, {"pid": os.getpid(), "attempt_id": request["attempt_id"],
                                        "state": "cancelling" if cancel.is_set() else "running",
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "at_epoch": time.time()})
        except OSError:
            pass
        if cancel_path.exists():
            cancel.set("cancel_requested")
        if not _parent_alive(parent_pid, parent_hb):
            cancel.set("parent_lost")
        if time.time() >= float(request["deadline_epoch"]):
            cancel.set("deadline")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m mic.browser.worker <request.json>", file=sys.stderr)
        return EXIT_BAD_REQUEST
    request_path = Path(argv[0])
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"bad request file: {exc}", file=sys.stderr)
        return EXIT_BAD_REQUEST
    attempt_id = request.get("attempt_id")
    result_path = Path(request["result_path"])
    run_dir = Path(request["run_dir"])
    cancel = CancelFlag()

    def _on_signal(signum, _frame):  # signal handlers only set the flag
        cancel.set(f"signal_{signum}")

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    remaining = float(request["deadline_epoch"]) - time.time() - START_MARGIN_SECONDS
    if remaining <= 0:
        write_json_atomic(result_path, {"attempt_id": attempt_id, "status": STATUS_FAILED,
                                        "error_code": "deadline_before_start",
                                        "error_message": "no time left after worker startup"})
        return EXIT_FAILED

    stop = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat_loop, args=(request, cancel, stop, run_dir),
                                 daemon=True, name="mic-worker-heartbeat")
    hb_thread.start()

    exit_code = EXIT_FAILED
    payload: dict[str, Any] = {"attempt_id": attempt_id, "status": STATUS_FAILED}
    try:
        from mic.api import AnalystAPI
        from mic.config import load_config

        config = load_config(request.get("config_dir"))
        api = AnalystAPI(config=config)
        run_options = {
            "deadline_seconds": remaining,
            "cancel_check": cancel.is_set,
            "attempt_id": attempt_id,
            "task_key": request.get("task_key"),
            "artifact_dir": str(run_dir),
        }
        report = api.collect_intelligence(
            request["target_id"], request["task_profile"], run_options=run_options)
        diag = (report or {}).get("collection_diagnostics") or {}
        if cancel.is_set() and diag.get("execution_status") in ("cancelled", "timed_out"):
            payload.update(status=STATUS_CANCELLED, error_code=f"mic_{diag.get('execution_status')}",
                           error_message=cancel.reason, report=report,
                           budget_used=diag.get("budget_used"),
                           gateway_requests_sent=(diag.get("budget_used") or {}).get(
                               "gateway_requests_sent"))
            exit_code = EXIT_CANCELLED
        else:
            payload.update(status=STATUS_COMPLETED, report=report,
                           budget_used=diag.get("budget_used"),
                           gateway_requests_sent=(diag.get("budget_used") or {}).get(
                               "gateway_requests_sent"))
            exit_code = EXIT_OK
    except Exception as exc:  # noqa: BLE001 - everything becomes a structured failure
        code = getattr(exc, "code", None) or type(exc).__name__
        payload.update(status=STATUS_FAILED, error_code=str(code),
                       error_message=str(exc)[:1000])
        traceback.print_exc(file=sys.stderr)
        exit_code = EXIT_FAILED
    finally:
        stop.set()
        try:
            write_json_atomic(result_path, payload)
        except OSError as exc:
            print(f"cannot write result: {exc}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
