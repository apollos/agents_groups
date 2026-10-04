"""T13 / T14 / T15 / T18: profile lock, BrowserSession with a fake backend, supervised worker.

The Playwright backend is replaced by an in-memory double; the worker is a
real subprocess running a tiny stand-in script. No real browser is launched.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from mic.browser.profile_lock import (
    ProfileBusy,
    ProfileLock,
    ensure_private_dir,
    probe_lock,
    write_private_file,
)
from mic.browser.runner import RunSupervisor, read_result_file
from mic.browser.session import BrowserClosed, BrowserSession, BrowserUnavailable, gui_available
from tests.browser_doubles import browser_runtime_block

# --- profile lock --------------------------------------------------------------------------------

def test_profile_dir_and_lock_permissions_and_mutual_exclusion(tmp_path):
    profile = tmp_path / "profiles" / "mic-edge"
    ensure_private_dir(profile)
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    lock1 = ProfileLock(profile, "run1", "att1")
    lock1.acquire()
    assert stat.S_IMODE(lock1.lock_path.stat().st_mode) == 0o600
    info = json.loads(lock1.lock_path.read_text())
    assert info["run_id"] == "run1" and info["pid"] == os.getpid() and "created_at" in info
    assert probe_lock(profile)["locked"] is True
    lock2 = ProfileLock(profile, "run2", "att2")
    with pytest.raises(ProfileBusy) as exc:
        lock2.acquire()
    assert exc.value.code == "profile_busy"
    lock1.release()
    assert probe_lock(profile)["locked"] is False
    lock2.acquire()  # lock is about the OS lock, not file existence
    lock2.release()


def test_stale_lock_file_without_holder_is_not_busy(tmp_path):
    profile = tmp_path / "mic-edge"
    ensure_private_dir(profile)
    lock = ProfileLock(profile, "r", "a")
    lock.lock_path.write_text(json.dumps({"run_id": "dead", "pid": 999999}))
    lock.acquire()  # file exists but nobody holds the flock
    lock.release()


def test_probe_lock_reports_unopenable_lock_file_instead_of_raising(tmp_path, monkeypatch):
    profile = tmp_path / "mic-edge"
    ensure_private_dir(profile)
    lock = ProfileLock(profile, "r", "a")
    lock.lock_path.write_text("{}")
    real_open = os.open

    def deny(path, *args, **kwargs):
        if str(path) == str(lock.lock_path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny)
    result = probe_lock(profile)
    assert result["locked"] is None
    assert "PermissionError" in result["error"]


def test_private_file_is_0600(tmp_path):
    p = tmp_path / "d" / "x.json"
    write_private_file(p, b"{}")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


# --- BrowserSession with fake backend -------------------------------------------------------------

class FakeCtx:
    def __init__(self):
        self.pages = []
        self.closed = False
        self.handlers = {}
        self._cookies = []

    def cookies(self):
        return [dict(c) for c in self._cookies]

    def on(self, event, handler):
        self.handlers[event] = handler

    def new_page(self):
        page = FakePwPage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True

    def add_cookies(self, cookies):
        self._cookies.extend(cookies)

    def clear_cookies(self, name=None, domain=None, path=None):
        # Playwright semantics: string filters are exact matches.
        self._cookies = [c for c in self._cookies
                         if not ((name is None or c.get("name") == name) and (domain is None or c.get("domain") == domain)
                                 and (path is None or c.get("path") == path))]


class FakePwPage:
    def __init__(self, ctx):
        self.ctx = ctx
        self.url = "about:blank"
        self._closed = False

    def goto(self, url, timeout=None, wait_until=None):
        if "timeout" in url:
            raise TimeoutError("Timeout 1000ms exceeded")
        if "closed" in url:
            raise RuntimeError("Target page, context or browser has been closed")
        self.url = url
        return type("Resp", (), {"status": 200})()

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True


class FakeBackend:
    def __init__(self, fail=None):
        self.fail = fail
        self.ctx = None
        self.stopped = False

    @staticmethod
    def available():
        return True, "fake"

    def launch(self, **kwargs):
        if self.fail:
            raise self.fail
        self.kwargs = kwargs
        self.ctx = FakeCtx()
        return self.ctx

    def stop(self):
        self.stopped = True


def _session(tmp_path, backend=None, **rt):
    runtime = browser_runtime_block(**rt)
    return BrowserSession(runtime=runtime, run_id="r", attempt_id="a", profile_dir=tmp_path / "mic-edge",
                          backend=backend or FakeBackend())


def test_gui_unavailable_is_not_silently_headless(tmp_path, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert gui_available() is False
    session = _session(tmp_path)
    with pytest.raises(BrowserUnavailable) as exc:
        session.start()
    assert exc.value.code == "gui_unavailable"
    assert session.started is False


def test_session_lifecycle_lock_pages_and_clean_close(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    backend = FakeBackend()
    session = _session(tmp_path, backend)
    session.start()
    assert backend.kwargs["headless"] is False and backend.kwargs["channel"] == "msedge"
    assert str(backend.kwargs["user_data_dir"]).endswith("mic-edge")
    assert probe_lock(tmp_path / "mic-edge")["locked"] is True
    with session.page() as page:
        nav = session.navigate(page, "https://example.com/a", 5)
        assert nav["status"] == "navigated" and nav["final_url"] == "https://example.com/a"
        assert session.navigate(page, "javascript:alert(1)", 5)["status"] == "rejected"
        assert session.navigate(page, "https://example.com/timeout", 1)["status"] == "timeout"
    diag = session.close()
    assert diag["cleanup"] == "complete" and backend.stopped and backend.ctx.closed
    assert probe_lock(tmp_path / "mic-edge")["locked"] is False
    assert session.started is False


def test_second_session_on_same_profile_is_profile_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    s1 = _session(tmp_path)
    s1.start()
    s2 = _session(tmp_path)
    with pytest.raises(ProfileBusy):
        s2.start()
    s1.close()


def test_launch_failure_releases_lock_and_reports_code(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    session = _session(tmp_path, FakeBackend(fail=RuntimeError("Executable doesn't exist at /x/msedge")))
    with pytest.raises(BrowserUnavailable) as exc:
        session.start()
    assert exc.value.code == "browser_missing"
    assert probe_lock(tmp_path / "mic-edge")["locked"] is False


def test_user_closed_window_is_detected_and_close_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    backend = FakeBackend()
    session = _session(tmp_path, backend)
    session.start()
    with session.page() as page:
        assert session.navigate(page, "https://example.com/closed", 5)["status"] == "browser_closed"
    with pytest.raises(BrowserClosed):
        with session.page():
            pass
    diag = session.close()
    assert diag["closed_by_user"] is True
    assert diag["cleanup"] == "complete"


def test_cleanup_incomplete_is_reported_not_hidden(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")

    class BadStop(FakeBackend):
        def stop(self):
            raise RuntimeError("driver hung")

    session = _session(tmp_path, BadStop())
    session.start()
    diag = session.close()
    assert diag["cleanup"] == "cleanup_incomplete"
    assert any("backend_stop" in p for p in diag["problems"])
    assert probe_lock(tmp_path / "mic-edge")["locked"] is False  # lock still released


def test_auth_context_changes_with_imported_cookies_and_never_exposes_values(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    session = _session(tmp_path)
    session.start()
    anon = session.auth_context()
    assert anon["auth_mode"] == "profile"
    session.add_cookies([{"name": "sid", "value": "SECRET-VALUE", "domain": ".example.com", "path": "/"}], "c1", "v1")
    auth = session.auth_context()
    assert auth["auth_mode"] == "imported_cookie" and auth["auth_context_id"] != anon["auth_context_id"]
    assert "SECRET-VALUE" not in json.dumps(auth) and "SECRET-VALUE" not in json.dumps(session.describe())
    assert session.clear_cookies_for_domains(["example.com"]) == 1
    session.close()


def test_max_open_pages_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    session = _session(tmp_path, max_open_pages=1)
    session.start()
    with session.page():
        with pytest.raises(RuntimeError):
            with session.page():
                pass
    session.close()


# --- supervised worker subprocess (T14 / T15) ------------------------------------------------------

WORKER_STUB = textwrap.dedent('''
    """Stand-in worker used by the lifecycle tests (NOT the real mic.browser.worker)."""
    import json, os, signal, sys, time
    from pathlib import Path

    def main(argv):
        req = json.loads(Path(argv[1]).read_text())
        run_dir = Path(req["run_dir"])
        mode = req.get("mode", "ok")
        attempt_id = req["attempt_id"]
        if mode == "crash":
            sys.exit(7)
        if mode == "hang_ignore_term":
            signal.signal(signal.SIGTERM, lambda *a: None)
            while True:
                time.sleep(0.1)
        if mode == "hang":
            while True:
                time.sleep(0.1)
        if mode == "stdout_only":
            print(json.dumps({"status": "completed", "report": {"fake": True}}))
            sys.exit(0)
        if mode == "corrupt":
            (run_dir / "result.json").write_text("{not json")
            sys.exit(0)
        if mode == "wrong_attempt":
            (run_dir / "result.json").write_text(json.dumps({"attempt_id": "other", "status": "completed",
                                                             "report": {}}))
            sys.exit(0)
        if mode == "echo_deadline":
            (run_dir / "seen.json").write_text(json.dumps({
                "deadline_epoch": req["deadline_epoch"], "hard_deadline_epoch": req["hard_deadline_epoch"],
                "started_epoch": req["started_epoch"], "deadline_sources": req["deadline_sources"]}))
        payload = {"attempt_id": attempt_id, "status": "completed",
                   "report": {"collection_diagnostics": {"execution_status": "completed"}},
                   "budget_used": {"gateway_requests_sent": 0}}
        tmp = run_dir / "result.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload))
        os.replace(tmp, run_dir / "result.json")
        if mode == "completed_then_exit_7":
            sys.exit(7)
        if mode in ("orphan_child", "orphan_child_ignore_term"):
            # Leave a descendant behind in the same process group (like a browser helper).
            code = "import signal, time\\n"
            if mode == "orphan_child_ignore_term":
                code += "signal.signal(signal.SIGTERM, lambda *a: None)\\n"
            code += "time.sleep(60)\\n"
            pid = os.fork()
            if pid == 0:
                os.execv(sys.executable, [sys.executable, "-c", code])
            (run_dir / "child.pid").write_text(str(pid))
            sys.exit(0)
        if mode.startswith("detached_browser_"):
            # Like a real Edge under Playwright on Linux: own session / process group (detached),
            # ignores TERM, and - as Chromium wipes its environ - carries NO attempt marker.
            #   _hang       : worker keeps running -> only the live parent chain identifies it
            #   _registered : worker registers the browser (as BrowserSession.start does) and
            #                 exits at once -> chain broken; only the registration identifies it
            #   _foreign    : chain broken, nothing registered -> not ours, must be left alone
            import subprocess
            code = "import signal, time\\nsignal.signal(signal.SIGTERM, lambda *a: None)\\ntime.sleep(60)\\n"
            env = {k: v for k, v in os.environ.items() if k != "MIC_WORKER_ATTEMPT_ID"}
            argv = [sys.executable, "-c", code, "--user-data-dir=" + os.environ["MIC_BROWSER_PROFILE_DIR"]]
            if mode == "detached_browser_hang":
                child = subprocess.Popen(argv, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                (run_dir / "child.pid").write_text(str(child.pid))
                while True:
                    time.sleep(0.1)
            if mode == "detached_browser_registered":
                child = subprocess.Popen(argv, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                (run_dir / "child.pid").write_text(str(child.pid))
                from mic.browser.runner import register_browser_processes
                assert any(p["pid"] == child.pid for p in register_browser_processes())
                sys.exit(0)  # chain breaks here; the registration must carry ownership
            launcher = ("import subprocess, sys\\n"
                        f"p = subprocess.Popen({argv!r}, start_new_session=True, stdin=subprocess.DEVNULL,"
                        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\\n"
                        "print(p.pid)\\n")
            out = subprocess.run([sys.executable, "-c", launcher], env=env, capture_output=True, text=True)
            (run_dir / "child.pid").write_text(out.stdout.strip())
            time.sleep(0.3)  # intermediate is gone -> grandchild reparented, chain broken
            sys.exit(0)
        if mode == "launch_crash_marked_browser":
            # Review (4th round): the driver crashes during the start-up handshake. The browser
            # process already exists (detached, ignores TERM, NO environ marker, NOT registered -
            # launch never returned) and the worker dies at once, before the supervisor's next
            # scan: the only thing identifying the browser is the Chromium switch the worker put
            # on its command line (--mic-attempt-id=<attempt>, like BrowserSession.start does).
            import subprocess
            code = "import signal, time\\nsignal.signal(signal.SIGTERM, lambda *a: None)\\ntime.sleep(60)\\n"
            env = {k: v for k, v in os.environ.items() if k != "MIC_WORKER_ATTEMPT_ID"}
            argv = [sys.executable, "-c", code, "--mic-attempt-id=" + attempt_id,
                    "--user-data-dir=" + os.environ["MIC_BROWSER_PROFILE_DIR"]]
            child = subprocess.Popen(argv, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (run_dir / "child.pid").write_text(str(child.pid))
            payload = {"attempt_id": attempt_id, "status": "failed", "error_code": "browser_launch_failed",
                       "error_message": "driver crashed during handshake"}
            (run_dir / "result.json").write_text(json.dumps(payload))
            sys.exit(3)
        if mode == "profile_busy":
            # Real lock contention: another attempt holds the profile lock -> this worker fails
            # fast with profile_busy (exit 3) exactly like mic.browser.worker does.
            from mic.browser.profile_lock import ProfileBusy, ProfileLock
            time.sleep(0.5)  # give the lock holder time to "start its browser"
            try:
                ProfileLock(Path(os.environ["MIC_BROWSER_PROFILE_DIR"]), "r", attempt_id).acquire()
            except ProfileBusy:
                payload = {"attempt_id": attempt_id, "status": "failed", "error_code": "profile_busy",
                           "error_message": "profile busy"}
                (run_dir / "result.json").write_text(json.dumps(payload))
                sys.exit(3)
            raise SystemExit("lock unexpectedly acquired")
        sys.exit(0)

    if __name__ == "__main__":
        main(sys.argv)
''')


@pytest.fixture
def stub_module(tmp_path, monkeypatch):
    pkg = tmp_path / "stubpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "worker_stub.py").write_text(WORKER_STUB)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return "stubpkg.worker_stub"


def _supervisor(tmp_path, stub_module, grace=1.0):
    return RunSupervisor(runs_root=tmp_path / "runs", python_executable=sys.executable,
                         grace_seconds=grace, heartbeat_seconds=0.2, worker_module=stub_module,
                         extra_env={"PYTHONPATH": os.environ["PYTHONPATH"],
                                    "MIC_BROWSER_PROFILE_DIR": str(tmp_path / "profiles" / "mic-edge")})


def _run(sup, mode, **kw):
    args = {"target_id": "company_300750", "task_profile": {"budget_profile": {"max_queries": 1}},
            "deadline_seconds": 20, "config_dir": None, "task_key": "t", "attempt_id": "a",
            "poll_seconds": 0.05, "extra_request": {"mode": mode}}
    args.update(kw)
    return sup.run(**args)


def _proc_state(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    except FileNotFoundError:
        return None


def _assert_reaped(pid: int) -> None:
    """The supervisor's *own child* must be waited on: gone, or a reused pid that is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    assert _proc_state(pid) != "Z", "worker left as zombie"


def _assert_not_running(pid: int) -> None:
    """A *descendant* the supervisor cannot wait on (grandchild): gone, or at most a zombie
    awaiting init / a subreaper - never running. (Whether init has reaped it yet depends on
    the environment, so both outcomes are accepted.)"""
    state = _proc_state(pid)
    assert state in (None, "Z"), f"descendant {pid} still running (state={state})"


def test_supervisor_happy_path_reads_validated_result(tmp_path, stub_module):
    sup = _supervisor(tmp_path, stub_module)
    out = _run(sup, "ok", attempt_id="att-1")
    assert out.status == "completed" and out.ok
    assert out.report["collection_diagnostics"]["execution_status"] == "completed"
    assert out.exit_code == 0
    assert (Path(out.run_dir) / "result.json").exists()
    assert out.attempt_id == "att-1"


@pytest.mark.parametrize("mode, expected, code", [
    ("crash", "worker_crashed", "result_missing"),
    ("stdout_only", "result_missing", "result_missing"),
    ("corrupt", "result_missing", "result_corrupt"),
    ("wrong_attempt", "result_missing", "result_attempt_mismatch"),
])
def test_supervisor_never_fabricates_success_from_stdout_or_bad_files(tmp_path, stub_module, mode, expected, code):
    sup = _supervisor(tmp_path, stub_module)
    out = _run(sup, mode)
    assert out.status == expected
    assert out.error_code == code
    assert out.report is None
    assert out.ok is False


def test_supervisor_timeout_terminates_process_group(tmp_path, stub_module):
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    t0 = time.monotonic()
    out = _run(sup, "hang", deadline_seconds=1.0)
    assert out.status == "timed_out" and out.error_code == "mic_timeout"
    assert out.cleanup == "complete"
    assert out.worker_pid is not None
    _assert_reaped(out.worker_pid)
    assert time.monotonic() - t0 < 10
    assert (Path(out.run_dir) / "cancel.json").exists()  # cooperative stop was requested first


def test_supervisor_kills_after_grace_when_term_ignored(tmp_path, stub_module):
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    t0 = time.monotonic()
    out = _run(sup, "hang_ignore_term", deadline_seconds=1.0)
    assert out.status == "timed_out"
    assert out.cleanup == "complete"
    assert 1.5 <= time.monotonic() - t0 < 10  # waited the grace period before KILL
    _assert_reaped(out.worker_pid)


def test_supervisor_cancel_event_stops_worker(tmp_path, stub_module):
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    out = _run(sup, "hang", deadline_seconds=30, cancel_event=cancel)
    assert out.status == "cancelled" and out.error_code == "mic_cancelled"
    _assert_reaped(out.worker_pid)


def test_supervisor_rejects_completed_result_with_nonzero_exit(tmp_path, stub_module):
    """Review R10: result file says completed but the worker exited 7 -> not a success."""
    sup = _supervisor(tmp_path, stub_module)
    out = _run(sup, "completed_then_exit_7")
    assert out.status == "failed" and out.error_code == "worker_exit_nonzero"
    assert out.exit_code == 7 and out.ok is False
    assert out.report is not None  # kept for diagnostics, never treated as success


@pytest.mark.parametrize("mode", ["orphan_child", "orphan_child_ignore_term"])
def test_supervisor_reaps_descendants_after_main_process_exit(tmp_path, stub_module, mode):
    """Review R2: the main process exiting is not cleanup; the owned group must be empty."""
    from mic.browser.runner import process_group_alive
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    t0 = time.monotonic()
    out = _run(sup, mode)
    child_pid = int((Path(out.run_dir) / "child.pid").read_text())
    assert out.status == "completed" and out.cleanup == "complete"
    assert time.monotonic() - t0 < 10
    _assert_reaped(out.worker_pid)
    _assert_not_running(child_pid)  # grandchild: gone or zombie, never running
    assert process_group_alive(out.worker_pid) is False


def test_supervisor_timeout_leaves_no_descendants(tmp_path, stub_module):
    """A hanging worker with a TERM-ignoring descendant: cleanup only completes when both are gone."""
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    out = _run(sup, "hang_ignore_term", deadline_seconds=1.0)
    assert out.status == "timed_out" and out.cleanup == "complete"
    from mic.browser.runner import process_group_alive
    assert process_group_alive(out.worker_pid) is False


@pytest.mark.parametrize("mode", ["detached_browser_hang", "detached_browser_registered"])
def test_supervisor_reaps_detached_browser_group(tmp_path, stub_module, mode):
    """Review: Playwright launches the browser detached (own process group) and Chromium wipes
    its environ. A TERM-ignoring 'browser' without any marker must still be found - via the
    live parent chain while the worker runs (hang -> timeout), or via the worker's
    registration once the chain is gone (registered) - and killed."""
    from mic.browser.runner import OwnershipSpec, load_registered_processes, owned_processes
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    t0 = time.monotonic()
    out = _run(sup, mode, deadline_seconds=1.5 if mode == "detached_browser_hang" else 20)
    child_pid = int((Path(out.run_dir) / "child.pid").read_text())
    assert out.cleanup == "complete", (out.status, out.error_message)
    assert out.status == ("timed_out" if mode == "detached_browser_hang" else "completed")
    assert time.monotonic() - t0 < 10
    _assert_not_running(child_pid)
    assert out.owned_process_groups >= 2  # worker group + the detached browser group
    assert out.leftover_processes == 0 and out.foreign_profile_processes == 0
    if mode == "detached_browser_registered":
        reg_path = Path(out.run_dir) / "browser_processes.json"
        reg = load_registered_processes(reg_path, out.attempt_id)
        assert [p["pid"] for p in reg] == [child_pid]
        assert stat.S_IMODE(reg_path.stat().st_mode) == 0o600
        assert owned_processes(OwnershipSpec(registered=reg), set()) == []  # gone


def test_supervisor_leaves_foreign_detached_process_alone(tmp_path, stub_module):
    """Control: a detached process on the MIC profile path but with no marker, no live chain
    and no registration is not ours -> untouched (only counted as foreign_profile_processes)."""
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    out = _run(sup, "detached_browser_foreign")
    child_pid = int((Path(out.run_dir) / "child.pid").read_text())
    try:
        assert out.status == "completed" and out.cleanup == "complete"
        assert _proc_state(child_pid) not in (None, "Z")  # still running
        assert out.foreign_profile_processes == 1
    finally:
        os.kill(child_pid, 9)


def test_driver_crash_before_registration_still_reaps_marked_browser(tmp_path, stub_module):
    """Review (4th round): browser created, driver crashed in the handshake, worker gone before
    the next scan - nothing registered, parent chain broken. The command-line marker alone
    must identify the browser; it is reaped and NOT counted as foreign."""
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    out = _run(sup, "launch_crash_marked_browser", attempt_id="att-crash")
    child_pid = int((Path(out.run_dir) / "child.pid").read_text())
    try:
        assert not (Path(out.run_dir) / "browser_processes.json").exists()  # launch never returned
        assert out.status == "failed" and out.error_code == "browser_launch_failed" and out.exit_code == 3
        assert out.cleanup == "complete" and out.leftover_processes == 0
        assert out.owned_process_groups >= 2  # worker group + the detached browser's group
        assert out.foreign_profile_processes == 0
        _assert_not_running(child_pid)
    finally:
        try:
            os.kill(child_pid, 9)
        except ProcessLookupError:
            pass


def test_marked_browser_survives_real_driver_crash_and_is_owned(tmp_path, monkeypatch):
    """Same failure with the real Playwright launcher: a stand-in browser executable that never
    completes the CDP handshake, the node driver SIGKILLed mid-launch. BrowserSession reports
    browser_launch_failed; the surviving browser carries the marker and is owned by the
    supervisor's rule - on nothing else (no registration, chain broken)."""
    pytest.importorskip("playwright")
    import stat
    import subprocess
    import threading

    from mic.browser.runner import OwnershipSpec, owned_processes
    from mic.browser.session import BrowserSession, BrowserUnavailable, PlaywrightBackend
    attempt = f"att-real-{os.getpid()}"
    monkeypatch.setenv("MIC_WORKER_ATTEMPT_ID", attempt)
    monkeypatch.delenv("MIC_BROWSER_PROCESS_FILE", raising=False)
    fake = tmp_path / "fake-browser.sh"
    fake.write_text("#!/bin/bash\ntrap '' TERM\nsleep 60\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    profile = tmp_path / "profiles" / "mic-edge"
    runtime = {"headless": True, "executable_path": str(fake), "browser_start_timeout_seconds": 15}
    backend = PlaywrightBackend()

    def kill_driver():
        # The node driver is our child (sync_playwright().start()); kill it mid-handshake.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for p in subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True,
                                    text=True).stdout.split():
                try:
                    cmd = Path(f"/proc/{p}/cmdline").read_bytes()
                except OSError:
                    continue
                if b"node" in cmd and b"playwright" in cmd:
                    children = subprocess.run(["pgrep", "-P", p], capture_output=True, text=True).stdout
                    if children.strip():  # the browser has been spawned -> crash the driver now
                        os.kill(int(p), 9)
                        return
            time.sleep(0.02)

    killer = threading.Thread(target=kill_driver, daemon=True)
    session = BrowserSession(runtime=runtime, run_id="r", attempt_id=attempt, profile_dir=profile,
                             backend=backend)
    killer.start()
    with pytest.raises(BrowserUnavailable) as info:
        session.start()
    killer.join(timeout=5)
    try:
        assert info.value.code == "browser_launch_failed"
        deadline = time.monotonic() + 3
        owned = []
        while time.monotonic() < deadline:
            owned = owned_processes(OwnershipSpec(attempt_id=attempt), set())
            if owned:
                break
            time.sleep(0.05)
        pids = {pid for pid, _g, _s in owned}
        assert pids, "surviving browser not identified by the command-line marker"
        cmds = [Path(f"/proc/{pid}/cmdline").read_bytes() for pid in pids]
        # The stand-in browser carries the marker; its ``sleep`` child is owned via the group.
        assert any(f"--mic-attempt-id={attempt}".encode() in c and str(fake).encode() in c for c in cmds)
        assert all(_proc_state(pid) not in (None, "Z") for pid in pids)  # really survived the crash
        # The lock was released on failure; a foreign attempt id owns nothing of ours.
        assert not owned_processes(OwnershipSpec(attempt_id=attempt + "-other"), set())
    finally:
        for pid, _g, _s in owned_processes(OwnershipSpec(attempt_id=attempt), set()):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        try:
            backend.stop()
        except Exception:  # noqa: BLE001
            pass


def test_cleanup_is_unverified_when_proc_cannot_be_read(tmp_path, stub_module, monkeypatch):
    """Review (4th round): when the supervisor cannot confirm the tree is gone it must say so
    (cleanup_incomplete), never report "complete" because the scan came back empty."""
    import mic.browser.runner as runner

    def broken(*_a, **_k):
        raise runner.ProcTableUnavailable("/proc unreadable")
    monkeypatch.setattr(runner, "_proc_table", broken)
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    out = _run(sup, "ok", attempt_id="att-noproc")
    assert out.exit_code == 0 and (Path(out.run_dir) / "result.json").exists()
    assert out.status == "cleanup_incomplete" and out.cleanup == "unverified"
    assert "/proc" in out.error_message and not out.ok


def test_transient_scan_failure_then_recovery_with_leftover_is_not_complete(tmp_path, stub_module, monkeypatch):
    """Review (5th round): /proc scans fail while the worker runs (cache stays empty) and once
    more at the start of the reap, then recover. The recovered scan finds the worker's
    detached browser alive - that must be reaped (or reported), never "cleanup=complete
    with leftover_processes=1"."""
    import mic.browser.runner as runner

    real = runner._proc_table
    runs_root = tmp_path / "runs"
    state = {"post_exit_failures": 0, "calls": 0}

    def flaky(*a, **k):
        state["calls"] += 1
        result_written = any(p.exists() for p in runs_root.glob("*/result.json"))
        if not result_written:
            raise runner.ProcTableUnavailable("scan failed while worker runs")  # cache stays empty
        if state["post_exit_failures"] < 1:
            state["post_exit_failures"] += 1
            raise runner.ProcTableUnavailable("one more failure at reap time")
        return real(*a, **k)
    monkeypatch.setattr(runner, "_proc_table", flaky)
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    out = _run(sup, "launch_crash_marked_browser", attempt_id="att-flaky")
    child_pid = int((Path(out.run_dir) / "child.pid").read_text())
    try:
        assert state["post_exit_failures"] == 1 and state["calls"] > 2  # scans did recover
        # A failed scan is "cannot tell", not "gone": the reap waits for a real scan, finds the
        # browser, escalates TERM -> KILL and only then is cleanup complete.
        assert out.status == "failed" and out.error_code == "browser_launch_failed"
        assert out.cleanup == "complete" and out.leftover_processes == 0
        assert out.owned_process_groups >= 2
        _assert_not_running(child_pid)
    finally:
        try:
            os.kill(child_pid, 9)
        except ProcessLookupError:
            pass


def test_residue_found_only_by_final_scan_is_reaped_and_reverified(tmp_path, stub_module, monkeypatch):
    """Review (5th round), harder variant: every scan during the first reap phase is blind, so
    the browser's group is never discovered there; the final verification scan is the first
    to see it. The supervisor must reap again and re-verify instead of passing the earlier
    verdict through."""
    import mic.browser.runner as runner

    real = runner._proc_table
    state = {"blind": False, "reaps": 0}

    def flaky(*a, **k):
        if state["blind"]:
            raise runner.ProcTableUnavailable("blind during first reap")
        return real(*a, **k)
    monkeypatch.setattr(runner, "_proc_table", flaky)
    sup = _supervisor(tmp_path, stub_module, grace=0.3)
    orig_reap = sup._reap_tree

    def reap(tree):
        state["reaps"] += 1
        state["blind"] = state["reaps"] == 1
        try:
            return orig_reap(tree)
        finally:
            state["blind"] = False
    monkeypatch.setattr(sup, "_reap_tree", reap)
    out = _run(sup, "launch_crash_marked_browser", attempt_id="att-blind")
    child_pid = int((Path(out.run_dir) / "child.pid").read_text())
    try:
        assert state["reaps"] == 2  # first pass blind -> final scan found residue -> reaped again
        assert out.status == "failed" and out.error_code == "browser_launch_failed"
        assert out.cleanup == "complete" and out.leftover_processes == 0
        _assert_not_running(child_pid)
    finally:
        try:
            os.kill(child_pid, 9)
        except ProcessLookupError:
            pass


def test_losing_profile_lock_race_never_touches_lock_holders_browser(tmp_path, stub_module):
    """Review (3rd round): attempt A holds the real ProfileLock and is starting its browser;
    attempt B starts at the same time, gets profile_busy and cleans up. B must not signal A's
    browser even though it runs on the same profile and started after B began."""
    import subprocess
    import threading

    from mic.browser.profile_lock import ProfileLock
    profile = tmp_path / "profiles" / "mic-edge"
    lock = ProfileLock(profile, "run-A", "att-A").acquire()  # A holds the lock
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    result: dict = {}
    th = threading.Thread(target=lambda: result.update(out=_run(sup, "profile_busy", attempt_id="att-B")))
    th.start()
    time.sleep(0.15)  # B is already running (started_epoch set) when A's browser appears
    env = {k: v for k, v in os.environ.items() if k != "MIC_WORKER_ATTEMPT_ID"}
    a_browser = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"--user-data-dir={profile}"],
                                 env=env, start_new_session=True)
    try:
        th.join(timeout=30)
        out = result["out"]
        assert out.status == "failed" and out.error_code == "profile_busy" and out.exit_code == 3
        assert out.cleanup == "complete" and out.leftover_processes == 0
        assert a_browser.poll() is None, f"lock holder's browser was killed (rc={a_browser.returncode})"
        assert out.foreign_profile_processes == 1  # seen, reported, not touched
        assert lock.holder()["attempt_id"] == "att-A"  # A's lock untouched as well
    finally:
        a_browser.kill()
        a_browser.wait()
        lock.release()


def test_owned_processes_rules(tmp_path):
    """Ownership rules: exact marker, known group, live parent chain, worker registration with a
    matching starttime; the MIC profile path alone never owns; own group is never owned."""
    import subprocess

    from mic.browser.runner import (
        OwnershipSpec,
        browser_marker_arg,
        descendant_processes,
        owned_processes,
        profile_processes,
    )
    sleep = "import time; time.sleep(30)"
    env_a = {**os.environ, "MIC_WORKER_ATTEMPT_ID": "att-A"}
    env_ab = {**os.environ, "MIC_WORKER_ATTEMPT_ID": "att-AB"}
    clean = {k: v for k, v in os.environ.items() if k != "MIC_WORKER_ATTEMPT_ID"}
    profile = str(tmp_path / "prof" / "mic-edge")
    pa = subprocess.Popen([sys.executable, "-c", sleep], env=env_a, start_new_session=True)
    pab = subprocess.Popen([sys.executable, "-c", sleep], env=env_ab, start_new_session=True)
    same_group = subprocess.Popen([sys.executable, "-c", sleep], env=env_a)
    # "worker" (own session) whose child detaches again (own session, no marker) = browser.
    chain_code = (f"import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', {sleep!r}],"
                  " start_new_session=True); print(p.pid, flush=True); time.sleep(30)")
    worker = subprocess.Popen([sys.executable, "-c", chain_code], env=clean, start_new_session=True,
                              stdout=subprocess.PIPE, text=True)
    browser_pid = int(worker.stdout.readline())
    # A "browser" on the MIC profile that nobody registered (another attempt's), and a sibling.
    prof_proc = subprocess.Popen([sys.executable, "-c", sleep, f"--user-data-dir={profile}"], env=clean,
                                 start_new_session=True)
    sibling = subprocess.Popen([sys.executable, "-c", sleep, f"--user-data-dir={profile}2"], env=clean,
                               start_new_session=True)
    # Browsers identified by the Chromium switch alone (no environ marker, no chain, nothing
    # registered): ours, a look-alike attempt id (prefix) and another attempt's.
    marked = subprocess.Popen([sys.executable, "-c", sleep, browser_marker_arg("att-A")], env=clean,
                              start_new_session=True)
    marked_prefix = subprocess.Popen([sys.executable, "-c", sleep, browser_marker_arg("att-AB")], env=clean,
                                     start_new_session=True)
    marked_other = subprocess.Popen([sys.executable, "-c", sleep, browser_marker_arg("att-B")], env=clean,
                                    start_new_session=True)
    try:
        time.sleep(0.3)
        pids = lambda spec, pg=None: {pid for pid, _g, _s in owned_processes(spec, pg or set())}  # noqa: E731
        found = pids(OwnershipSpec(attempt_id="att-A"))
        assert pa.pid in found and pab.pid not in found
        assert same_group.pid not in found  # our own process group is excluded by design
        assert marked.pid in found and marked_prefix.pid not in found and marked_other.pid not in found
        assert os.getpgid(marked.pid) in {g for _p, g, _s in owned_processes(OwnershipSpec(attempt_id="att-A"), set())}
        assert pids(None, {os.getpgid(pab.pid)}) >= {pab.pid} and pa.pid not in pids(None, {os.getpgid(pab.pid)})
        chain = pids(OwnershipSpec(root_pids={worker.pid}))
        assert {worker.pid, browser_pid} <= chain and prof_proc.pid not in chain
        # Registration (what the worker records at browser start) owns the browser's group ...
        reg = [p for p in descendant_processes(worker.pid) if p["pid"] == browser_pid]
        assert reg and reg[0]["pgid"] == browser_pid
        assert browser_pid in pids(OwnershipSpec(registered=reg))
        assert worker.pid not in pids(OwnershipSpec(registered=reg))
        # ... but not with a stale starttime (pid reuse) ...
        stale = [{**reg[0], "starttime": reg[0]["starttime"] + 1}]
        assert browser_pid not in pids(OwnershipSpec(registered=stale))
        # ... and the profile path alone owns nothing; it is only a diagnostic count.
        assert pids(OwnershipSpec(profile_dir=profile)) == set()
        assert profile_processes(profile, set()) == 1 and profile_processes(profile, {prof_proc.pid}) == 0
    finally:
        for p in (pa, pab, same_group, worker, prof_proc, sibling, marked, marked_prefix, marked_other):
            p.kill()
            p.wait()
        os.kill(browser_pid, 9)


def test_supervisor_merges_task_and_deployment_deadline(tmp_path, stub_module, monkeypatch):
    """Review R3c: the hard deadline = min(caller, task budget, browser deployment max_run_seconds),
    and the worker is told to finish a bounded wrap-up margin earlier."""
    class _Cfg:
        browser_runtime = {"enabled": True, "limits": {"max_run_seconds": 40}}

    import mic.config as cfg_mod  # the runner imports load_config lazily -> patch at source
    monkeypatch.setattr(cfg_mod, "load_config", lambda config_dir=None: _Cfg())
    sup = _supervisor(tmp_path, stub_module)
    out = _run(sup, "echo_deadline", deadline_seconds=900,
               task_profile={"budget_profile": {"max_run_seconds": 120}})
    assert out.status == "completed"
    seen = json.loads((Path(out.run_dir) / "seen.json").read_text())
    hard = seen["hard_deadline_epoch"] - seen["started_epoch"]
    soft = seen["deadline_epoch"] - seen["started_epoch"]
    assert hard == pytest.approx(40.0)  # deployment ceiling wins over 900 / 120
    assert soft == pytest.approx(40.0 - 4.0)  # wrap-up = min(10 s, 10 %)
    assert seen["deadline_sources"] == {"caller": 900.0, "task": 120.0, "deployment": 40.0}


def test_effective_run_seconds_without_browser_route_keeps_task_min(tmp_path, monkeypatch):
    from mic.browser.runner import effective_run_seconds

    class _Cfg:
        browser_runtime = {"enabled": False, "limits": {"max_run_seconds": 5}}

    import mic.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_config", lambda config_dir=None: _Cfg())
    eff, src = effective_run_seconds(900, {"budget_profile": {"max_run_seconds": 200}}, None)
    assert eff == 200 and "deployment" not in src
    eff, src = effective_run_seconds(60, {"budget_profile": {"max_run_seconds": True}}, None)
    assert eff == 60 and "task" not in src  # bool is not a limit


def test_result_file_validation(tmp_path):
    p = tmp_path / "result.json"
    assert read_result_file(p, "a")[0] is None
    p.write_text("garbage")
    assert read_result_file(p, "a")[0] is None
    p.write_text(json.dumps({"attempt_id": "b", "status": "completed"}))
    assert read_result_file(p, "a")[0] is None
    p.write_text(json.dumps({"attempt_id": "a", "status": "completed", "report": {}}))
    assert read_result_file(p, "a")[0] is not None


def test_real_worker_module_runs_mock_collection_end_to_end(tmp_path):
    """The real ``mic.browser.worker`` in a subprocess, legacy mock provider (env from conftest)."""
    sup = RunSupervisor(runs_root=tmp_path / "runs", python_executable=sys.executable,
                        grace_seconds=2.0, heartbeat_seconds=0.5,
                        cwd=str(Path(__file__).resolve().parent.parent))
    out = sup.run(target_id="company_300750",
                  task_profile={"task_type": "company_update", "time_window": "30d",
                                "budget_profile": {"max_queries": 1, "max_links_to_read": 1, "max_model_calls": 2}},
                  deadline_seconds=120, config_dir=None, task_key="t-e2e", attempt_id="e2e-1", poll_seconds=0.1)
    assert out.status == "completed", (out.error_code, out.error_message, out.stderr_tail)
    diag = out.report["collection_diagnostics"]
    assert diag["execution_status"] == "completed"
    assert diag["attempt_id"] == "e2e-1"
    assert diag["browser_run"] is False
    assert out.budget_used["queries_attempted"] == 1
    assert (Path(out.run_dir) / "heartbeat.json").exists() or out.elapsed_seconds < 3
    # MIC run log lives with the attempt artefacts, not in the tool's source tree logs/
    log_file = out.report.get("log_file")
    assert log_file and Path(log_file).parent == Path(out.run_dir) / "logs", log_file
    assert stat.S_IMODE((Path(out.run_dir) / "logs").stat().st_mode) == 0o700


# --- T18: no secrets in logs / diagnostics -----------------------------------------------------------

def test_request_file_is_private_and_contains_no_secrets(tmp_path, stub_module, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-SHOULD-NOT-LEAK")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "tok-SHOULD-NOT-LEAK")
    sup = _supervisor(tmp_path, stub_module)
    out = _run(sup, "ok")
    run_dir = Path(out.run_dir)
    for name in ("request.json", "result.json"):
        path = run_dir / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        text = path.read_text()
        assert "SHOULD-NOT-LEAK" not in text
    for log in run_dir.glob("*.log"):
        assert "SHOULD-NOT-LEAK" not in log.read_text()
