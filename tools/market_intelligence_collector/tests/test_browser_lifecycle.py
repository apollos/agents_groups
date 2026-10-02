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
        self.cookies = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def new_page(self):
        page = FakePwPage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def clear_cookies(self, domain=None):
        self.cookies = [c for c in self.cookies if domain is None or c.get("domain", "").lstrip(".") != domain]


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
                         extra_env={"PYTHONPATH": os.environ["PYTHONPATH"]})


def _run(sup, mode, **kw):
    args = {"target_id": "company_300750", "task_profile": {"budget_profile": {"max_queries": 1}},
            "deadline_seconds": 20, "config_dir": None, "task_key": "t", "attempt_id": "a",
            "poll_seconds": 0.05, "extra_request": {"mode": mode}}
    args.update(kw)
    return sup.run(**args)


def _assert_reaped(pid: int) -> None:
    # The child was waited on by the supervisor; a zombie would still answer kill(pid, 0),
    # so check via /proc state when the pid happens to be reused.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    assert state != "Z", "worker left as zombie"


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
    _assert_reaped(child_pid)
    assert process_group_alive(out.worker_pid) is False
    # the orphan was not left in the worker's group
    try:
        state = Path(f"/proc/{child_pid}/stat").read_text().split(")")[-1].split()[0]
        assert state == "Z"  # at most a zombie awaiting init, never running
    except FileNotFoundError:
        pass


def test_supervisor_timeout_leaves_no_descendants(tmp_path, stub_module):
    """A hanging worker with a TERM-ignoring descendant: cleanup only completes when both are gone."""
    sup = _supervisor(tmp_path, stub_module, grace=0.5)
    out = _run(sup, "hang_ignore_term", deadline_seconds=1.0)
    assert out.status == "timed_out" and out.cleanup == "complete"
    from mic.browser.runner import process_group_alive
    assert process_group_alive(out.worker_pid) is False


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
