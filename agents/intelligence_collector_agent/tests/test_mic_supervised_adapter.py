"""Supervised MIC collection in the Agent (browser search design 8.2/8.3/15, cases T14/T16).

All tests are offline. A stub worker module written under tmp_path stands in for
``mic.browser.worker``; it honours the same request/result-file contract (atomic private
result.json carrying the attempt id) and is driven by ``task_profile["stub_mode"]``.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

import pytest

from agent_trade_intel import agent as agent_module
from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.adapters.mic_adapter import MICAdapter, map_mic_error_code
from agent_trade_intel.agent import (
    MIC_FAULT_ERROR_CODES,
    NON_RETRYABLE_ERROR_CODES,
    RETRYABLE_ERROR_CODES,
    IntelligenceCollectorAgent,
    _tool_message_action,
)
from agent_trade_intel.attempts import CollectionAttemptRepository
from agent_trade_intel.config import AgentModelConfig, CollectorConfig, RuntimeConfig, ToolConfig
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.quality import QualityGate

WORKER_STUB = textwrap.dedent('''
    """Stand-in MIC worker for agent tests (NOT mic.browser.worker)."""
    import json, os, sys, time
    from pathlib import Path

    def write_result(run_dir, payload):
        tmp = run_dir / "result.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload))
        os.replace(tmp, run_dir / "result.json")

    def main(argv):
        req = json.loads(Path(argv[1]).read_text())
        run_dir = Path(req["run_dir"])
        attempt_id = req["attempt_id"]
        mode = (req.get("task_profile") or {}).get("stub_mode", "ok")
        if mode == "hang":
            while True:
                time.sleep(0.1)
        if mode == "crash":
            sys.exit(7)
        diag = {"execution_status": "completed", "search_status": "ok", "read_status": "ok",
                "output_status": "ok", "usable": True, "browser_run": True, "attempt_id": attempt_id,
                "budget_used": {"queries_attempted": 1, "search_page_attempts": 2, "gateway_requests_sent": 1},
                "cleanup": {"cleanup": "complete", "problems": []}, "auth_context": {"auth_mode": "anonymous"}}
        report = {"search_run_id": "run_stub", "summary": {"queries_executed": 1, "links_read": 1, "model_calls": 1},
                  "structured_outputs": {"events": 1, "facts": 0, "metrics": 0},
                  "all_events": [{"summary": "中标", "source": {"url": "https://example.test/n", "source_type": "official",
                                  "published_at": "2026-09-30"}, "confidence": 0.8}],
                  "collection_diagnostics": diag}
        if mode == "gui_fault":
            diag.update(execution_status="failed", stop_reason="gui_unavailable", search_status="failed",
                        usable=False, output_status="no_model_call")
            report["structured_outputs"] = {"events": 0}; report["all_events"] = []
        if mode == "empty":
            diag.update(search_status="empty", search_reason="no_candidates", read_status="not_run",
                        output_status="no_model_call", usable=False)
            report["structured_outputs"] = {"events": 0}; report["all_events"] = []
            report["summary"] = {"queries_executed": 1, "links_read": 0, "model_calls": 0}
        if mode == "profile_busy":
            write_result(run_dir, {"attempt_id": attempt_id, "status": "failed", "error_code": "profile_busy",
                                   "error_message": "profile held by pid 4242"})
            sys.exit(2)
        if mode == "config_error":
            write_result(run_dir, {"attempt_id": attempt_id, "status": "failed", "error_code": "ConfigError",
                                   "error_message": "browser_runtime: unknown field foo"})
            sys.exit(2)
        write_result(run_dir, {"attempt_id": attempt_id, "status": "completed", "report": report,
                               "budget_used": diag["budget_used"],
                               "gateway_requests_sent": diag["budget_used"]["gateway_requests_sent"]})
        sys.exit(0)

    if __name__ == "__main__":
        main(sys.argv)
''')


@pytest.fixture
def stub_module(tmp_path, monkeypatch):
    pkg = tmp_path / "agentstub"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "worker_stub.py").write_text(WORKER_STUB)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return "agentstub.worker_stub"


def _adapter(tmp_path, stub_module, **kw):
    kw.setdefault("timeout_seconds", 20)
    return MICAdapter(None, runs_root=tmp_path / "runs", python_executable=sys.executable,
                      worker_module=stub_module, grace_seconds=0.5, **kw)


def _reaped(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"


# --- adapter ---------------------------------------------------------------------------------------

def test_adapter_default_mode_is_subprocess_and_rejects_unknown_modes():
    assert MICAdapter().execution_mode == "subprocess"
    with pytest.raises(ValueError):
        MICAdapter(execution_mode="threads")


def test_supervised_completed_report_carries_diagnostics(tmp_path, stub_module):
    adapter = _adapter(tmp_path, stub_module)
    result = adapter.collect(target_id="company_300750", task_profile={"stub_mode": "ok"},
                             task_key="k1", attempt_id="att-ok")
    assert result.status == "success", result.errors
    assert result.quality["usable"] is True
    assert result.quality["collection_diagnostics"]["attempt_id"] == "att-ok"
    assert result.result_ref == "mic://search_runs/run_stub"
    assert adapter.last_outcome["status"] == "completed"
    assert adapter.last_outcome["cleanup"] == "complete"
    assert (Path(adapter.last_outcome["run_dir"]) / "result.json").exists()


def test_supervised_hard_timeout_reaps_worker_and_reports_sent_requests(tmp_path, stub_module):
    adapter = _adapter(tmp_path, stub_module, timeout_seconds=1)
    t0 = time.monotonic()
    result = adapter.collect(target_id="company_300750", task_profile={"stub_mode": "hang"}, attempt_id="att-hang")
    assert time.monotonic() - t0 < 10
    assert result.status == "failed"
    err = result.errors[0]
    assert err["error_code"] == "MIC_TIMEOUT" and err["retryable"] is True
    assert err["cleanup"] == "complete"
    assert "responses unknown" in err["error_message"]
    assert result.quality == {"usable": False}
    assert _reaped(adapter.last_outcome["worker_pid"])


def test_supervised_cancel_event_stops_worker(tmp_path, stub_module):
    adapter = _adapter(tmp_path, stub_module)
    cancel = Event()
    import threading
    threading.Timer(0.5, cancel.set).start()
    result = adapter.collect(target_id="company_300750", task_profile={"stub_mode": "hang"}, cancel_event=cancel)
    assert result.errors[0]["error_code"] == "MIC_CANCELLED"
    assert result.errors[0]["retryable"] is True
    assert _reaped(adapter.last_outcome["worker_pid"])


def test_worker_crash_is_retryable_but_never_a_success(tmp_path, stub_module):
    result = _adapter(tmp_path, stub_module).collect(target_id="x", task_profile={"stub_mode": "crash"})
    assert result.status == "failed"
    assert result.errors[0]["error_code"] == "MIC_WORKER_CRASHED"
    assert result.errors[0]["exit_code"] == 7


@pytest.mark.parametrize("mode, code, retryable", [
    ("gui_fault", "MIC_GUI_UNAVAILABLE", False),
    ("profile_busy", "MIC_PROFILE_BUSY", True),
    ("config_error", "MIC_CONFIG_ERROR", False),
])
def test_error_taxonomy_from_worker_and_diagnostics(tmp_path, stub_module, mode, code, retryable):
    result = _adapter(tmp_path, stub_module).collect(target_id="x", task_profile={"stub_mode": mode})
    assert result.status == "failed"
    assert result.errors[0]["error_code"] == code
    assert result.errors[0]["retryable"] is retryable
    q = QualityGate({}).evaluate(result)
    assert q["usable"] is False
    action = _tool_message_action(q, result.errors)[0]
    assert action == ("retry" if retryable else "ack")
    if mode == "gui_fault":
        # Report with execution_status=failed is kept for inspection, never treated as success.
        assert result.result["collection_diagnostics"]["stop_reason"] == "gui_unavailable"
        assert result.quality["collection_diagnostics"]["execution_status"] == "failed"


def test_code_map_and_error_sets_are_consistent():
    assert map_mic_error_code("gui_unavailable") == "MIC_GUI_UNAVAILABLE"
    assert map_mic_error_code("something_else") == "MIC_TOOL_FAILED"
    assert MIC_FAULT_ERROR_CODES <= NON_RETRYABLE_ERROR_CODES
    assert not (MIC_FAULT_ERROR_CODES & RETRYABLE_ERROR_CODES)
    assert {"MIC_TIMEOUT", "MIC_PROFILE_BUSY", "MIC_ATTEMPT_ACTIVE"} <= RETRYABLE_ERROR_CODES


def test_in_process_mode_is_explicit_opt_out():
    class API:
        def collect_intelligence(self, *, target_id, task_profile):
            return {"summary": {}, "structured_outputs": {"facts": 1}}

    adapter = MICAdapter(execution_mode="in_process")
    adapter._api = lambda: API()
    assert adapter.collect(target_id="t", task_profile={}).quality["usable"] is True


# --- quality gate with diagnostics -----------------------------------------------------------------

def _success(report):
    r = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
                   status="success", result=report)
    r.quality = {"collection_diagnostics": report.get("collection_diagnostics", {})}
    return r.finish()


def test_gate_rejects_report_whose_diagnostics_say_not_completed():
    report = {"summary": {"links_read": 1, "model_calls": 1}, "structured_outputs": {"facts": 2},
              "collection_diagnostics": {"execution_status": "timed_out", "stop_reason": "run_deadline",
                                         "search_status": "ok", "read_status": "partial", "output_status": "ok"}}
    q = QualityGate({}).evaluate(_success(report))
    assert q["usable"] is False and q["decision"] == "reject"
    assert q["execution_status"] == "timed_out"
    assert any(i["issue_type"] == "execution_timed_out" and i["error_code"] == "MIC_TIMEOUT" for i in q["issues"])


def test_gate_flags_search_empty_and_read_failed_separately():
    report = {"summary": {"links_read": 0, "model_calls": 0}, "structured_outputs": {},
              "collection_diagnostics": {"execution_status": "completed", "search_status": "empty",
                                         "read_status": "not_run", "output_status": "no_model_call",
                                         "budget_used": {"queries_attempted": 2}}}
    q = QualityGate({}).evaluate(_success(report))
    types = {i["issue_type"] for i in q["issues"]}
    assert "search_no_candidates" in types and q["usable"] is False
    assert q["collection_diagnostics"]["budget_used"] == {"queries_attempted": 2}
    report["collection_diagnostics"].update(search_status="ok", read_status="failed", read_failures={"anti_bot": 2})
    q = QualityGate({}).evaluate(_success(report))
    assert "read_failed" in {i["issue_type"] for i in q["issues"]}
    assert _tool_message_action(q, [])[0] == "ack"  # completed-but-empty is not retried


def test_gate_without_diagnostics_is_unchanged():
    report = {"summary": {"links_read": 1, "model_calls": 1}, "structured_outputs": {"facts": 2}}
    q = QualityGate({}).evaluate(_success({**report}))
    assert q["usable"] is True and "collection_diagnostics" not in q


# --- agent integration: attempts, lease loss, duplicates, blocking ---------------------------------

def _agent(root: Path, stub_module: str, *, lease_seconds: int = 30, timeout_seconds: int = 20) -> IntelligenceCollectorAgent:
    root.mkdir(parents=True, exist_ok=True)
    raw = {"capability_verification": {"run_on_startup": False}, "quality": {},
           "queue": {"lease_seconds": lease_seconds, "retry_delay_seconds": 1},
           "tools": {"market_intelligence_collector": {"timeout_seconds": timeout_seconds}}}
    config = CollectorConfig(
        raw=raw, path=root / "cfg.yaml",
        runtime=RuntimeConfig(agent_id="sup_test", agent_group="intelligence_collector",
                              state_sqlite_path=root / "state.db", bus_sqlite_path=root / "bus.db",
                              data_sqlite_path=root / "data.db", workspace_root=root,
                              log_dir=root / "logs", reports_dir=root / "reports"),
        model=AgentModelConfig(primary="offline/no_model_call", fallbacks=[], require_registered=False),
        tools=ToolConfig(mic_enabled=True, stock_enabled=False, mic_config_dir=None, stock_config_dir=None,
                         python_executable="python", stock_working_dir=None,
                         mic_execution_mode="subprocess", mic_runs_dir=str(root / "mic_runs"),
                         mic_python_executable=sys.executable))
    agent = IntelligenceCollectorAgent(config)
    agent.mic.worker_module = stub_module
    agent.mic.grace_seconds = 0.5
    return agent


def _dispatch(agent: IntelligenceCollectorAgent, *, stub_mode: str, suffix: str, idem: str = "same-task"):
    task = {"task_id": "task-1", "task_type": "mic_deep_collect", "idempotency_key": idem,
            "target": {"target_id": "company_300750", "company_name": "宁德时代", "ticker": "300750.SZ"},
            "budget_profile": {"max_queries": 1, "stub_mode": stub_mode}}
    ticket_id = agent.tickets.create_ticket(ticket_type="COLLECTION_TASK_TICKET", source_agent="t",
                                            target_agent_id=agent.config.runtime.agent_id, priority="normal", payload=task)
    message_id = agent.queue.publish("intelligence.collection", {"ticket_id": ticket_id},
                                     target_agent_id=agent.config.runtime.agent_id, idempotency_key="msg-" + suffix)
    return ticket_id, message_id, agent.run_once(topics=["intelligence.collection"])


def _attempts(agent):
    with agent.data_store.session() as con:
        return [dict(r) for r in con.execute("SELECT * FROM collection_attempt ORDER BY created_at")]


def _mic_task_profile_passthrough(monkeypatch):
    # Route the stub mode through the task profile MIC receives.
    original = agent_module._mic_task_profile

    def patched(task, config, default_focus=None):
        profile = original(task, config, default_focus=default_focus)
        mode = (task.get("budget_profile") or {}).get("stub_mode")
        if mode:
            profile["stub_mode"] = mode
        return profile

    monkeypatch.setattr(agent_module, "_mic_task_profile", patched)


def test_agent_records_attempt_and_persists_supervised_result(tmp_path, stub_module, monkeypatch):
    _mic_task_profile_passthrough(monkeypatch)
    agent = _agent(tmp_path / "a", stub_module)
    ticket_id, _message_id, out = _dispatch(agent, stub_mode="ok", suffix="1")
    assert out["status"] == "processed", out
    assert out["result"]["usable"] is True
    rows = _attempts(agent)
    assert len(rows) == 1 and rows[0]["state"] == "completed"
    assert rows[0]["task_key"] == "mic:same-task" and rows[0]["ticket_id"] == ticket_id
    assert rows[0]["result_path"] and Path(rows[0]["result_path"]).exists()
    assert json.loads(rows[0]["budget_used_json"])["gateway_requests_sent"] == 1
    assert agent.tickets.get(ticket_id)["status"] == "done"
    with agent.data_store.session() as con:
        q = json.loads(con.execute("SELECT quality_json FROM collection_runs").fetchone()[0])
    assert q["collection_diagnostics"]["browser_run"] is True


def test_agent_env_fault_is_not_retried_and_raises_fault_ticket(tmp_path, stub_module, monkeypatch):
    _mic_task_profile_passthrough(monkeypatch)
    agent = _agent(tmp_path / "a", stub_module)
    ticket_id, message_id, out = _dispatch(agent, stub_mode="gui_fault", suffix="1")
    assert out["status"] == "processed"  # acked: no blind retry for environment faults
    assert out["result"]["usable"] is False
    assert agent.tickets.get(ticket_id)["status"] == "failed"
    with agent.bus_store.session() as con:
        faults = [dict(r) for r in con.execute("SELECT payload_json FROM tickets WHERE ticket_type='FAULT_TICKET'")]
        delivery = con.execute("SELECT status FROM messages WHERE message_id=?", (message_id,)).fetchone()[0]
    assert faults and json.loads(faults[0]["payload_json"])["error_code"] == "MIC_GUI_UNAVAILABLE"
    assert delivery == "done"
    assert _attempts(agent)[0]["state"] == "failed"
    assert _attempts(agent)[0]["error_code"] == "MIC_GUI_UNAVAILABLE"


def test_agent_profile_busy_is_deferred_via_retry(tmp_path, stub_module, monkeypatch):
    _mic_task_profile_passthrough(monkeypatch)
    agent = _agent(tmp_path / "a", stub_module)
    ticket_id, message_id, out = _dispatch(agent, stub_mode="profile_busy", suffix="1")
    assert out["status"] == "retry_scheduled"
    with agent.bus_store.session() as con:
        row = con.execute("SELECT status, attempts FROM messages WHERE message_id=?", (message_id,)).fetchone()
    assert row["status"] == "open" and row["attempts"] == 1
    assert agent.tickets.get(ticket_id)["status"] == "open"


def test_duplicate_delivery_while_attempt_active_does_not_start_second_worker(tmp_path, stub_module, monkeypatch):
    _mic_task_profile_passthrough(monkeypatch)
    agent = _agent(tmp_path / "a", stub_module)
    # Another worker's attempt is alive (fresh heartbeat) for the same task key.
    attempt_id, _ = agent.attempts.start(task_key="mic:same-task", owner_token="other", deadline_seconds=600)
    agent.attempts.mark_running(attempt_id, worker_pid=os.getpid())
    with patch.object(agent.mic, "_collect_with_timeout") as collect:
        _ticket_id, message_id, out = _dispatch(agent, stub_mode="ok", suffix="dup")
    collect.assert_not_called()
    assert out["status"] == "retry_scheduled"
    with agent.bus_store.session() as con:
        row = con.execute("SELECT status, error_json FROM messages WHERE message_id=?", (message_id,)).fetchone()
    assert row["status"] == "open" and json.loads(row["error_json"])["error_code"] == "MIC_ATTEMPT_ACTIVE"
    rows = _attempts(agent)
    assert [r["state"] for r in rows] == ["running"]  # no second attempt row


def test_stale_active_attempt_is_closed_as_interrupted_and_task_proceeds(tmp_path, stub_module, monkeypatch):
    _mic_task_profile_passthrough(monkeypatch)
    agent = _agent(tmp_path / "a", stub_module)
    stale_id, _ = agent.attempts.start(task_key="mic:same-task", owner_token="dead-parent", deadline_seconds=1)
    with agent.data_store.session() as con:
        con.execute("UPDATE collection_attempt SET heartbeat_at=?, deadline_at=? WHERE attempt_id=?",
                    ("2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00", stale_id))
    _ticket_id, _message_id, out = _dispatch(agent, stub_mode="ok", suffix="1")
    assert out["status"] == "processed"
    states = {r["attempt_id"]: r["state"] for r in _attempts(agent)}
    assert states[stale_id] == "interrupted"
    assert "completed" in states.values()


def test_lease_loss_cancels_worker_and_leaves_message_to_new_owner(tmp_path, stub_module, monkeypatch):
    _mic_task_profile_passthrough(monkeypatch)
    original = agent_module.lease_heartbeat
    monkeypatch.setattr(agent_module, "lease_heartbeat", lambda keepalive, interval_seconds: original(keepalive, 0.2))
    agent = _agent(tmp_path / "a", stub_module, lease_seconds=30)

    real_extend = agent.queue.extend_lease
    calls = {"n": 0}

    def steal_then_extend(message_id, worker_id, lease_seconds):
        calls["n"] += 1
        if calls["n"] == 3:  # another consumer re-leased the message
            with agent.bus_store.session() as con:
                con.execute("UPDATE messages SET lease_owner='other-worker' WHERE message_id=?", (message_id,))
        return real_extend(message_id, worker_id, lease_seconds)

    monkeypatch.setattr(agent.queue, "extend_lease", steal_then_extend)
    t0 = time.monotonic()
    _ticket_id, message_id, out = _dispatch(agent, stub_mode="hang", suffix="1")
    assert time.monotonic() - t0 < 15
    assert out["status"] == "lease_lost", out
    assert agent.mic.last_outcome["status"] == "cancelled"
    assert _reaped(agent.mic.last_outcome["worker_pid"])
    rows = _attempts(agent)
    assert rows[0]["state"] == "cancelled"
    with agent.bus_store.session() as con:
        msg = con.execute("SELECT status, lease_owner FROM messages WHERE message_id=?", (message_id,)).fetchone()
    assert msg["status"] == "in_progress" and msg["lease_owner"] == "other-worker"  # untouched by us
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0  # revoked attempt exports nothing


def test_verified_live_cleanup_incomplete_blocks_new_runs_until_resolved(tmp_path, stub_module, monkeypatch):
    _mic_task_profile_passthrough(monkeypatch)
    agent = _agent(tmp_path / "a", stub_module)
    leftover_id, _ = agent.attempts.start(task_key="mic:old-task", owner_token="old", deadline_seconds=600)
    # A live process whose command line carries the attempt id = verified ownership.
    proc = subprocess.Popen([sys.executable, "-c", "import time,sys; time.sleep(60)", leftover_id])
    try:
        agent.attempts.finish(leftover_id, state="cleanup_incomplete", cleanup="incomplete", worker_pid=proc.pid)
        with patch.object(agent.mic, "_collect_with_timeout") as collect:
            ticket_id, _message_id, out = _dispatch(agent, stub_mode="ok", suffix="blocked")
        collect.assert_not_called()
        assert out["status"] == "processed" and out["result"]["usable"] is False
        assert agent.tickets.get(ticket_id)["status"] == "failed"
        with agent.bus_store.session() as con:
            faults = [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM tickets WHERE ticket_type='FAULT_TICKET'")]
        assert faults and faults[0]["error_code"] == "MIC_CLEANUP_INCOMPLETE"
        assert faults[0]["blocking_attempt_id"] == leftover_id
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    # Once the process is gone the block self-heals (row closed as interrupted) and runs proceed.
    _ticket_id, _message_id, out = _dispatch(agent, stub_mode="ok", suffix="after", idem="new-task")
    assert out["status"] == "processed" and out["result"]["usable"] is True
    assert agent.attempts.get(leftover_id)["state"] == "interrupted"


# --- repository / schema -------------------------------------------------------------------------------

def test_collection_attempt_schema_added_to_old_database(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT);"
                      "INSERT INTO schema_migrations(version) VALUES (7);")
    con.close()
    store = SQLiteStore(db)
    store.init_schema()
    store.init_schema()  # idempotent
    with store.session() as con:
        versions = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
        cols = {r["name"] for r in con.execute("PRAGMA table_info(collection_attempt)")}
    assert 8 in versions
    assert {"attempt_id", "task_key", "owner_token", "worker_pid", "state", "heartbeat_at", "deadline_at",
            "result_path"} <= cols


def test_unique_active_attempt_per_task_key(tmp_path):
    store = SQLiteStore(tmp_path / "d.db")
    store.init_schema()
    repo = CollectionAttemptRepository(store, stale_after_seconds=60)
    a, blocker = repo.start(task_key="k", owner_token="o1", deadline_seconds=100)
    assert a and blocker is None
    b, blocker = repo.start(task_key="k", owner_token="o2", deadline_seconds=100)
    assert b is None and blocker["attempt_id"] == a
    repo.finish(a, state="completed")
    c, blocker = repo.start(task_key="k", owner_token="o3", deadline_seconds=100)
    assert c and blocker is None
    assert repo.heartbeat(a) is False  # finished attempts cannot be revived
    assert repo.heartbeat(c) is True


# --- skill preflight is provider aware -------------------------------------------------------------

def test_skill_preflight_mentions_browser_doctor_and_searxng_branches(tmp_path):
    import types

    from agent_trade_intel.openclaw import OpenClawArtifactRenderer

    config = CollectorConfig(
        raw={}, path=tmp_path / "c.yaml",
        runtime=RuntimeConfig(agent_id="a", agent_group="g", state_sqlite_path=tmp_path / "s.db",
                              bus_sqlite_path=tmp_path / "b.db", data_sqlite_path=tmp_path / "d.db",
                              workspace_root=tmp_path, log_dir=tmp_path, reports_dir=tmp_path),
        model=AgentModelConfig(primary="x/y", fallbacks=[], require_registered=False),
        tools=ToolConfig(mic_enabled=True, stock_enabled=False, mic_config_dir="/mic/config", stock_config_dir=None,
                         python_executable="python", stock_working_dir=None))
    fake_cfg = types.SimpleNamespace(
        search_providers={"active": "browser_local", "providers": {"browser_local": {"type": "browser"}}},
        browser_enabled=True)
    cfg_mod = types.ModuleType("mic.config")
    cfg_mod.load_config = Mock(return_value=fake_cfg)
    with patch.dict("sys.modules", {"mic": types.ModuleType("mic"), "mic.config": cfg_mod}):
        text = OpenClawArtifactRenderer(config).skill_md()
    cfg_mod.load_config.assert_called_once_with("/mic/config")
    assert "search_providers.active = browser_local" in text
    assert "mic browser doctor" in text and "ensure-search" in text
    assert "gui_unavailable" in text and "profile_busy" in text
    assert "不得杀其他进程" in text
