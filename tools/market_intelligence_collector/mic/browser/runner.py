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
# The worker plans to finish this long before the hard deadline (close browser, write
# result). Bounded to a fraction of short deadlines so tests / tiny budgets still start.
DEFAULT_WRAPUP_SECONDS = 10.0
# How often the supervisor rescans /proc for processes carrying this attempt's marker
# (discovers the detached browser group while the worker is still alive).
TREE_TRACK_SECONDS = 2.0

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
    # Process-tree diagnostics: groups owned by this attempt (worker + detached browser
    # group(s)) and processes still alive when cleanup gave up (0 when complete).
    owned_process_groups: int = 0
    leftover_processes: int = 0
    # Browsers on the MIC profile that are *not* ours (lock holder of another attempt or a
    # stale browser). Diagnostics only - never signalled by this attempt.
    foreign_profile_processes: int = 0

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


def effective_run_seconds(deadline_seconds: float, task_profile: dict[str, Any] | None,
                          config_dir: str | None) -> tuple[float, dict[str, Any]]:
    """One hard limit for supervisor and worker (review: the Agent timeout alone ignored the
    browser deployment's ``max_run_seconds`` and the task budget).

    = min(caller deadline, task ``budget_profile.max_run_seconds``, deployment
    ``browser_runtime.limits.max_run_seconds`` when the browser route is enabled).
    Config problems are left for the worker to report; they never extend the deadline.
    """
    sources: dict[str, Any] = {"caller": float(deadline_seconds)}
    effective = float(deadline_seconds)
    budget = ((task_profile or {}).get("budget_profile") or {})
    task_limit = budget.get("max_run_seconds")
    if isinstance(task_limit, (int, float)) and not isinstance(task_limit, bool) and task_limit > 0:
        sources["task"] = float(task_limit)
        effective = min(effective, float(task_limit))
    try:
        from mic.config import load_config
        cfg = load_config(config_dir)
        runtime = cfg.browser_runtime
        if runtime.get("enabled"):
            dep = (runtime.get("limits") or {}).get("max_run_seconds")
            if isinstance(dep, (int, float)) and dep > 0:
                sources["deployment"] = float(dep)
                effective = min(effective, float(dep))
    except Exception as exc:  # noqa: BLE001 - the worker will surface the config error itself
        sources["deployment_error"] = f"{type(exc).__name__}"
    return effective, sources


def _proc_stat(pid: int | str) -> tuple[str, int, int, int] | None:
    """(state, ppid, pgrp, starttime_ticks) from ``/proc/<pid>/stat``; None when gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat = fh.read()
    except OSError:
        return None
    # "pid (comm) state ppid pgrp ..." - comm may contain spaces/parens. After the comm the
    # fields are 1-based 3.. so starttime (field 22) is rest[19].
    rest = stat.rsplit(")", 1)[-1].split()
    if len(rest) < 20:
        return None
    try:
        return rest[0], int(rest[1]), int(rest[2]), int(rest[19])
    except ValueError:
        return None


def _environ_has(pid: int | str, entry: bytes) -> bool:
    """True when ``/proc/<pid>/environ`` contains exactly ``entry`` (``KEY=value``)."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            return entry in fh.read().split(b"\0")
    except OSError:
        return False


def _cmdline_has_profile(pid: int | str, profile_arg: bytes) -> bool:
    """True when the process command line carries ``--user-data-dir=<profile>`` as a whole
    argument. Chromium rewrites its argv area for the process title (arguments become
    space separated), so match on the raw bytes with a NUL / space / end terminator."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    start = raw.find(profile_arg)
    if start < 0:
        return False
    end = start + len(profile_arg)
    return end == len(raw) or raw[end:end + 1] in (b"\0", b" ")


BROWSER_PROCESS_FILE_ENV = "MIC_BROWSER_PROCESS_FILE"


def _proc_table(exclude_own_group: bool = True) -> dict[int, tuple[str, int, int, int]]:
    """pid -> (state, ppid, pgrp, starttime) for every live non-zombie process except ours.

    The supervisor also drops its own process group (never a kill target); the worker must
    keep it, because the Playwright node driver - the hop between worker and browser in the
    parent chain - lives in the worker's group.
    """
    me, my_pgid = os.getpid(), os.getpgid(0)
    table: dict[int, tuple[str, int, int, int]] = {}
    try:
        entries = [int(e.name) for e in os.scandir("/proc") if e.name.isdigit()]
    except OSError:
        return table
    for pid in entries:
        if pid == me:
            continue
        st = _proc_stat(pid)
        if st is None or st[0] == "Z" or (exclude_own_group and st[2] == my_pgid):
            continue
        table[pid] = st
    return table


def descendant_processes(root_pid: int) -> list[dict[str, int]]:
    """Live descendants of ``root_pid`` (parent chain), each as ``{pid, pgid, starttime}``.

    Called by the worker right after the browser launched, while the chain
    worker -> node driver -> browser is intact, to register the browser's identity.
    """
    table = _proc_table(exclude_own_group=False)
    table.pop(root_pid, None)
    owned = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (_s, ppid, _pg, _st) in table.items():
            if pid not in owned and ppid in owned:
                owned.add(pid)
                changed = True
    owned.discard(root_pid)
    return [{"pid": pid, "pgid": table[pid][2], "starttime": table[pid][3]} for pid in sorted(owned)]


def register_browser_processes(path: Path | str | None = None, root_pid: int | None = None) -> list[dict[str, int]]:
    """Worker side: record the browser processes this attempt owns (pid / pgid / starttime).

    ``path`` defaults to ``$MIC_BROWSER_PROCESS_FILE`` (set by the supervisor to a file in the
    0700 attempt run dir). Returns what was recorded; silently records nothing when no path
    is configured (e.g. ``mic browser setup`` / probes run without a supervisor).
    """
    target = path or os.environ.get(BROWSER_PROCESS_FILE_ENV)
    procs = descendant_processes(root_pid or os.getpid())
    procs = [p for p in procs if p["pgid"] != os.getpgid(0)]  # own group is tracked anyway
    if target:
        payload = {"attempt_id": os.environ.get("MIC_WORKER_ATTEMPT_ID"), "registered_at": time.time(),
                   "processes": procs}
        try:
            write_json_atomic(Path(target), payload)
        except OSError:  # pragma: no cover - diagnostics only, never fail the run for it
            pass
    return procs


def load_registered_processes(path: Path | str | None, attempt_id: str | None) -> list[dict[str, int]]:
    """Supervisor side: the worker's registration for *this* attempt (others are ignored)."""
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or (attempt_id and data.get("attempt_id") not in (None, attempt_id)):
        return []
    out = []
    for p in data.get("processes") or []:
        try:
            out.append({"pid": int(p["pid"]), "pgid": int(p["pgid"]), "starttime": int(p["starttime"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


@dataclass
class OwnershipSpec:
    """What identifies a process as belonging to one supervised attempt."""

    attempt_id: str | None = None          # MIC_WORKER_ATTEMPT_ID marker (python / node helpers)
    root_pids: set[int] = field(default_factory=set)   # worker pid(s): descendants are owned
    registered: list[dict[str, int]] = field(default_factory=list)  # worker-registered browser
    profile_dir: str | None = None         # MIC profile: diagnostics only, never a kill criterion


def owned_processes(spec: OwnershipSpec | str | None, pgids: set[int]) -> list[tuple[int, int, str]]:
    """Every live (non-zombie) process that belongs to this attempt: ``(pid, pgid, state)``.

    Playwright spawns the browser ``detached`` (own session / process group) and Chromium
    wipes its own ``/proc/<pid>/environ`` (observed: every ``msedge`` process shows an empty
    environ), so neither the worker's group nor an environment marker can find Edge. A
    process is owned when **any** of these holds:

    1. it is in a process group already known to be owned (worker group, browser group);
    2. it descends from an owned process through the live parent chain
       (worker -> node driver -> browser main; renderers share the browser's group);
    3. the worker registered it at browser start (``register_browser_processes``) and its
       ``starttime`` still matches (pid reuse excluded) - then its group is owned too;
    4. its environment carries exactly ``MIC_WORKER_ATTEMPT_ID=<attempt>`` (Python / Node
       helpers).

    The MIC profile path is deliberately **not** an ownership rule: an attempt that lost the
    profile-lock race would otherwise "own" the lock holder's browser (review). The
    supervisor's own group is never owned. Zombies are ignored.
    """
    if not isinstance(spec, OwnershipSpec):
        spec = OwnershipSpec(attempt_id=spec)
    marker = f"MIC_WORKER_ATTEMPT_ID={spec.attempt_id}".encode() if spec.attempt_id else None
    uid = os.getuid()
    table = _proc_table()
    groups = set(pgids)
    for reg in spec.registered:
        st = table.get(reg["pid"])
        if st is not None and st[3] == reg["starttime"]:
            groups.add(st[2])
    owned: set[int] = set()
    for pid, (_state, _ppid, pgrp, _start) in table.items():
        if pgrp in groups or pid in spec.root_pids:
            owned.add(pid)
            continue
        if marker is None:
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        if _environ_has(pid, marker):
            owned.add(pid)
    # Rule 2: propagate down the live parent chain until nothing new is found.
    changed = True
    while changed:
        changed = False
        for pid, (_state, ppid, _pgrp, _st) in table.items():
            if pid not in owned and ppid in owned:
                owned.add(pid)
                changed = True
    return [(pid, table[pid][2], table[pid][0]) for pid in sorted(owned)]


def profile_processes(profile_dir: str | None, exclude: set[int]) -> int:
    """Diagnostics only: live processes on the MIC profile that are *not* ours (another
    attempt holding the lock, or a stale browser). Reported, never signalled."""
    if not profile_dir:
        return 0
    arg = f"--user-data-dir={profile_dir}".encode()
    return sum(1 for pid in _proc_table() if pid not in exclude and _cmdline_has_profile(pid, arg))


def process_group_alive(pgid: int | None) -> bool:
    """True while any process of the owned group still exists (zombies excluded)."""
    if pgid is None:
        return False
    try:
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            st = _proc_stat(entry.name)
            if st is not None and st[2] == pgid and st[0] != "Z":
                return True
        return False
    except OSError:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True


def profile_dir_for(config_dir: str | None, env: dict[str, str]) -> str | None:
    """The dedicated browser profile the worker will use (same resolution as the session),
    or None when the browser route is not configured - then rule 3 is simply unavailable."""
    try:
        from mic.browser.config import resolve_profile_dir
        from mic.config import load_config
        # Not gated on ``enabled``: only a browser started *after* this run on exactly this
        # profile can match, and that is a MIC browser whatever the flag says.
        return str(resolve_profile_dir(load_config(config_dir).browser_runtime, env))
    except Exception:  # noqa: BLE001 - the worker reports config problems itself
        return None


class _OwnedTree:
    """Process groups and processes that belong to one attempt (see ``owned_processes``)."""

    def __init__(self, spec: OwnershipSpec, worker_pgid: int | None,
                 registry_path: Path | None = None):
        self.spec = spec
        self.registry_path = registry_path
        self.pgids: set[int] = {worker_pgid} if worker_pgid is not None else set()
        self.leftover = 0

    def refresh(self) -> list[tuple[int, int, str]]:
        """Scan once; remember every group an owned process lives in (groups outlive the
        parent chain, so a browser seen once stays tracked after the worker is gone)."""
        if self.registry_path is not None:
            # The worker registers its browser (pid / pgid / starttime) right after launch;
            # pick it up whenever it appears (cheap: a tiny file in the run dir).
            self.spec.registered = load_registered_processes(self.registry_path, self.spec.attempt_id)
        procs = owned_processes(self.spec, self.pgids)
        my_pgid = os.getpgid(0)
        for _pid, pgrp, _state in procs:
            if pgrp != my_pgid and pgrp > 0:
                self.pgids.add(pgrp)
        self.leftover = len(procs)
        return procs

    def alive(self) -> bool:
        return bool(self.refresh())

    def wait_gone(self, seconds: float, poll: float = 0.1) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if not self.alive():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def signal(self, proc: subprocess.Popen | None, sig: int) -> None:
        """Signal every owned group plus any marked process outside the known groups."""
        procs = self.refresh()
        for pgid in list(self.pgids):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:  # pragma: no cover - foreign process reused the id
                self.pgids.discard(pgid)
        for pid, pgrp, _state in procs:
            if pgrp not in self.pgids:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
        if proc is not None and not self.pgids and proc.poll() is None:
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass


class RunSupervisor:
    """Starts, monitors and reaps one MIC worker process."""

    def __init__(self, *, runs_root: Path, python_executable: str | None = None,
                 grace_seconds: float = DEFAULT_GRACE_SECONDS,
                 heartbeat_seconds: float = PARENT_HEARTBEAT_SECONDS,
                 extra_env: dict[str, str] | None = None,
                 worker_module: str = WORKER_MODULE,
                 cwd: str | None = None,
                 wrapup_seconds: float = DEFAULT_WRAPUP_SECONDS):
        self.runs_root = Path(runs_root)
        self.python_executable = python_executable or sys.executable
        self.grace_seconds = float(grace_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.extra_env = dict(extra_env or {})
        self.worker_module = worker_module
        self.cwd = cwd
        self.wrapup_seconds = float(wrapup_seconds)

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
        # One effective hard limit (caller / task budget / browser deployment). The worker
        # gets the same limit minus a bounded wrap-up margin so it can close the browser and
        # write its result before the supervisor's TERM/KILL at the hard deadline.
        hard_seconds, deadline_sources = effective_run_seconds(deadline_seconds, task_profile, config_dir)
        wrapup = min(self.wrapup_seconds, hard_seconds * 0.1)
        worker_seconds = max(0.0, hard_seconds - wrapup)
        deadline_epoch = started_epoch + hard_seconds

        request = {
            "attempt_id": attempt_id, "task_key": task_key, "target_id": target_id,
            "task_profile": task_profile, "config_dir": config_dir,
            "run_dir": str(run_dir), "result_path": str(result_path),
            "cancel_path": str(cancel_path), "parent_heartbeat_path": str(parent_hb_path),
            "parent_pid": os.getpid(), "deadline_epoch": started_epoch + worker_seconds,
            "hard_deadline_epoch": deadline_epoch, "deadline_sources": deadline_sources,
            "started_epoch": started_epoch,
            **(extra_request or {}),
        }
        write_json_atomic(request_path, request)
        self._write_parent_heartbeat(parent_hb_path, "running")

        browser_registry_path = run_dir / "browser_processes.json"
        env = {**os.environ, **self.extra_env, "MIC_WORKER_ATTEMPT_ID": attempt_id,
               BROWSER_PROCESS_FILE_ENV: str(browser_registry_path)}
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
        # Owned process tree: the worker's group (start_new_session) plus every group found
        # carrying this attempt's marker while it runs (the detached browser and its helpers).
        tree = _OwnedTree(OwnershipSpec(attempt_id=attempt_id, root_pids={proc.pid},
                                        profile_dir=profile_dir_for(config_dir, env)),
                          self._owned_pgid(proc), registry_path=browser_registry_path)
        last_track = 0.0
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
                if now - last_track >= TREE_TRACK_SECONDS:
                    tree.refresh()
                    last_track = now
                time.sleep(poll_seconds)

            if stop_reason is not None:
                write_json_atomic(cancel_path, {"reason": stop_reason, "at": time.time()})
                cleanup = self._terminate(proc, tree)
            else:
                proc.wait()
                # The main process exiting is not proof the tree is gone (review: a child
                # ignoring TERM survived while the report said "complete"). Reap descendants
                # the worker left behind before declaring the profile free.
                cleanup = self._reap_tree(tree)
        finally:
            stderr_file.close()
            stdout_file.close()

        elapsed = time.monotonic() - started
        stderr_tail = _tail(self._read(run_dir / "worker.stderr.log"))
        payload, err = read_result_file(result_path, attempt_id)
        ours = {p for p, _g, _s in tree.refresh()}
        foreign = profile_processes(tree.spec.profile_dir, ours)
        outcome = SupervisedOutcome(status=STATUS_FAILED, attempt_id=attempt_id,
                                    run_dir=str(run_dir), exit_code=proc.returncode,
                                    stderr_tail=stderr_tail, elapsed_seconds=round(elapsed, 2),
                                    worker_pid=proc.pid, cleanup=cleanup,
                                    owned_process_groups=len(tree.pgids),
                                    leftover_processes=tree.leftover,
                                    foreign_profile_processes=foreign)
        if payload is not None:
            outcome.budget_used = payload.get("budget_used") or {}
            outcome.gateway_requests_sent = payload.get("gateway_requests_sent")
        if cleanup != "complete":
            outcome.status = STATUS_CLEANUP_INCOMPLETE
            outcome.error_code = STATUS_CLEANUP_INCOMPLETE
            outcome.error_message = (f"{tree.leftover} owned process(es) across {len(tree.pgids)} group(s) "
                                     "still alive after TERM/KILL (worker and/or detached browser); "
                                     "do not retry on the same profile automatically")
            outcome.report = payload.get("report") if payload and isinstance(payload.get("report"), dict) \
                else None
            return outcome
        if stop_reason is not None:
            outcome.status = stop_reason
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
            if proc.returncode != 0:
                # Review: a "completed" result file plus a non-zero exit means the worker
                # failed after writing it (teardown error, unhandled exception in cleanup).
                outcome.status = STATUS_FAILED
                outcome.error_code = "worker_exit_nonzero"
                outcome.error_message = (f"worker wrote a completed result but exited with "
                                         f"code {proc.returncode}; not accepted as success")
                outcome.report = payload["report"]
                return outcome
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

    @staticmethod
    def _owned_pgid(proc: subprocess.Popen) -> int | None:
        """Process group created for the worker (``start_new_session``); None when it is
        already gone or - defensively - equals our own group (never signal ourselves)."""
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            # Main process already reaped; the group id equals the worker pid by construction.
            pgid = proc.pid
        if pgid == os.getpgid(os.getpid()):
            return None
        return pgid

    def _terminate(self, proc: subprocess.Popen, tree: _OwnedTree) -> str:
        """TERM the whole owned tree, then KILL after grace. Returns cleanup state.

        "complete" requires every owned process to be gone - worker group *and* the
        browser's detached group(s) - not just the main process.
        """
        tree.signal(proc, signal.SIGTERM)
        if self._wait(proc, self.grace_seconds) and tree.wait_gone(self.grace_seconds):
            return "complete"
        tree.signal(proc, signal.SIGKILL)
        if self._wait(proc, self.grace_seconds) and tree.wait_gone(self.grace_seconds):
            return "complete"
        return STATUS_CLEANUP_INCOMPLETE

    def _reap_tree(self, tree: _OwnedTree) -> str:
        """After a normal worker exit: descendants (browser, helpers) must be gone too."""
        if not tree.alive():
            return "complete"
        # Give an orderly shutdown a short moment (the worker closed the browser; Chromium
        # helpers may still be exiting), then escalate exactly like a timeout.
        if tree.wait_gone(min(2.0, self.grace_seconds)):
            return "complete"
        tree.signal(None, signal.SIGTERM)
        if tree.wait_gone(self.grace_seconds):
            return "complete"
        tree.signal(None, signal.SIGKILL)
        if tree.wait_gone(self.grace_seconds):
            return "complete"
        return STATUS_CLEANUP_INCOMPLETE

    @staticmethod
    def _wait(proc: subprocess.Popen, seconds: float) -> bool:
        try:
            proc.wait(timeout=seconds)
            return True
        except subprocess.TimeoutExpired:
            return False
