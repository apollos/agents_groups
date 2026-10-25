"""Offline lifecycle regressions through registration, planning and execution.

Only adapter calls are substituted. Separate state/bus/data databases catch the
projection failures hidden by single-database fixtures. Empty MIC output here
proves execution bookkeeping only, never successful business acceptance.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.agent import IntelligenceCollectorAgent
from agent_trade_intel.config import AgentModelConfig, CollectorConfig, RuntimeConfig, ToolConfig
from agent_trade_intel.db import loads_json
from agent_trade_intel.reader import IntelligenceReader
from agent_trade_intel.recovery import RecoveryManager
from agent_trade_intel.runtime import RuntimeController


AS_OF = "2026-10-19T16:00:00+08:00"
DEMAND_ID = "lifecycle-offline-demand"
COLLECTION_TOPIC = ["intelligence.collection"]


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> IntelligenceCollectorAgent:
    shared_database = getattr(request, "param", "split") == "shared"

    def database(name: str) -> Path:
        return tmp_path / ("shared.db" if shared_database else f"{name}.db")

    config = CollectorConfig(
        raw={
            "capability_verification": {"run_on_startup": False, "run_pre_market": False},
            "queue": {"lease_seconds": 60, "retry_delay_seconds": 0},
        },
        path=tmp_path / "config.yaml",
        runtime=RuntimeConfig(
            agent_id="lifecycle-test", agent_group="intelligence_collector",
            state_sqlite_path=database("state"), bus_sqlite_path=database("bus"),
            data_sqlite_path=database("data"), workspace_root=tmp_path,
            log_dir=tmp_path / "logs", reports_dir=tmp_path / "reports",
        ),
        model=AgentModelConfig(primary="offline/no-model", fallbacks=[], require_registered=False),
        tools=ToolConfig(
            mic_enabled=True, stock_enabled=True, mic_config_dir=None,
            stock_config_dir=None, python_executable="python", stock_working_dir=None,
            mic_runs_dir=str(tmp_path / "mic_runs"),
        ),
    )
    worker = IntelligenceCollectorAgent(config)

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected real adapter call in offline lifecycle regression")

    monkeypatch.setattr(worker.mic, "collect", forbidden)
    monkeypatch.setattr(worker.stock, "_run", forbidden)
    monkeypatch.setattr(worker.stock, "cli_available", forbidden)
    monkeypatch.setattr(worker.hk_connect, "collect_snapshot", forbidden)
    monkeypatch.setattr(worker.market_context, "collect_snapshot", forbidden)
    return worker


def _runtime(agent: IntelligenceCollectorAgent) -> RuntimeController:
    return RuntimeController(
        agent.config, state_store=agent.state_store,
        bus_store=agent.bus_store, data_store=agent.data_store,
    )


def _reader(agent: IntelligenceCollectorAgent) -> IntelligenceReader:
    return IntelligenceReader(agent.data_store, agent.bus_store, agent.state_store)


def _tasks(agent: IntelligenceCollectorAgent) -> list[dict]:
    with agent.data_store.session() as con:
        return [dict(row) for row in con.execute(
            "SELECT * FROM collection_tasks WHERE demand_id=? ORDER BY task_id", (DEMAND_ID,)
        )]


def _message(agent: IntelligenceCollectorAgent, ticket_id: str) -> dict:
    with agent.bus_store.session() as con:
        rows = con.execute("SELECT * FROM messages WHERE topic='intelligence.collection'").fetchall()
    matches = [dict(row) for row in rows if loads_json(row["payload_json"], {}).get("ticket_id") == ticket_id]
    assert len(matches) == 1
    return matches[0]


def _plan(agent: IntelligenceCollectorAgent, *, target_count: int = 1, stock_only: bool = False) -> list[dict]:
    registered = agent.registry.register({
        "schema_version": "demand.v1", "demand_id": DEMAND_ID,
        "demand_type": "daily_collection" if stock_only else "on_demand_research",
        "source_type": "operator", "status": "active", "priority": "normal",
        "idempotency_key": DEMAND_ID,
        "targets": [{
            "target_id": f"company_offline_{i}", "company_name": f"离线样例 {i}",
            "ticker": f"60000{i}.SH", "collect_mic": not stock_only,
            "collect_stock": stock_only,
        } for i in range(target_count)],
        "task_profile": {"mic": {"time_window": "", "questions": [{"question": "公开资料有哪些缺口？"}]}},
    })
    assert registered["status"] == "registered"
    tick = _runtime(agent).tick(now=AS_OF, phase="post_market")
    assert tick["capability_check"] is None
    assert len(tick["created"]) == 2  # request Ticket + its delivery
    planned = agent.run_once(topics=COLLECTION_TOPIC)
    assert planned["status"] == "processed", planned
    assert planned["result"]["status"] == "planned"
    tasks = _tasks(agent)
    assert len(tasks) == target_count
    assert {task["status"] for task in tasks} == {"open"}
    assert all(_message(agent, task["task_ticket_id"])["attempts"] == 0 for task in tasks)
    return tasks


def _empty_mic_result(**kwargs) -> ToolResult:
    return ToolResult(
        tool_name="market_intelligence_collector", operation="collect_intelligence",
        request={"target_id": kwargs["target_id"], "task_profile": kwargs["task_profile"]},
        status="success", result={
            "summary": {"queries_executed": 0, "links_read": 0, "model_calls": 0},
            "structured_outputs": {},
        },
    ).finish()


def _assert_task_status(agent: IntelligenceCollectorAgent, task: dict, expected: str) -> None:
    assert agent.tickets.get(task["task_ticket_id"])["status"] == expected
    current = {row["task_id"]: row for row in _tasks(agent)}
    assert current[task["task_id"]]["status"] == expected
    reported = _reader(agent).read_collection_status(demand_id=DEMAND_ID)["tasks"]
    assert {row["task_id"]: row["status"] for row in reported}[task["task_id"]] == expected


@pytest.mark.parametrize("agent", ["split", "shared"], indirect=True)
def test_planned_task_tracks_running_and_done_without_claiming_usable_output(agent, monkeypatch):
    task, = _plan(agent)

    def collect(**kwargs):
        _assert_task_status(agent, task, "in_progress")
        assert kwargs["task_profile"]["time_window"] == ""
        assert kwargs["task_profile"]["questions"] == [{"question": "公开资料有哪些缺口？"}]
        return _empty_mic_result(**kwargs)

    tool = Mock(side_effect=collect)
    monkeypatch.setattr(agent.mic, "collect", tool)
    result = agent.run_once(topics=COLLECTION_TOPIC)

    assert result["status"] == "processed", result
    assert result["result"]["usable"] is False
    assert result["result"]["output_status"] == "no_structured_output"
    _assert_task_status(agent, task, "done")
    assert _message(agent, task["task_ticket_id"])["status"] == "done"
    tool.assert_called_once()


@pytest.mark.parametrize("failure_kind", ["retryable_result", "exception"])
def test_retry_exhaustion_closes_ticket_task_and_delivery(agent, monkeypatch, failure_kind):
    task, = _plan(agent, stock_only=failure_kind == "exception")
    ticket_id = task["task_ticket_id"]
    message_id = _message(agent, ticket_id)["message_id"]
    with agent.bus_store.session() as con:
        con.execute("UPDATE messages SET max_attempts=2 WHERE message_id=?", (message_id,))

    if failure_kind == "exception":
        tool = Mock(side_effect=RuntimeError("offline transport failed"))
        monkeypatch.setattr(agent.stock, "fetch_trading_status", tool)
    else:
        def retry(**kwargs):
            return ToolResult(
                tool_name="market_intelligence_collector", operation="collect_intelligence",
                request={"target_id": kwargs["target_id"]},
                status="failed", errors=[{
                    "error_code": "MIC_PROFILE_BUSY", "error_message": "offline contention", "retryable": True,
                }],
            ).finish()

        tool = Mock(side_effect=retry)
        monkeypatch.setattr(agent.mic, "collect", tool)

    first = agent.run_once(topics=COLLECTION_TOPIC)
    assert first["message_id"] == message_id
    assert _message(agent, ticket_id)["status"] == "open"
    _assert_task_status(agent, task, "open")

    last = agent.run_once(topics=COLLECTION_TOPIC)
    assert last["message_id"] == message_id
    assert last["status"] != "retry_scheduled", last
    assert _message(agent, ticket_id)["status"] == "dead"
    _assert_task_status(agent, task, "failed")
    assert agent.run_once(topics=COLLECTION_TOPIC)["status"] == "idle"
    assert tool.call_count == 2


def test_suspend_cancels_pending_work_and_preserves_completed_task(agent, monkeypatch):
    tasks = _plan(agent, target_count=2)
    tool = Mock(side_effect=_empty_mic_result)
    monkeypatch.setattr(agent.mic, "collect", tool)
    completed = agent.run_once(topics=COLLECTION_TOPIC)
    done = next(task for task in tasks if _message(agent, task["task_ticket_id"])["message_id"] == completed["message_id"])
    pending = next(task for task in tasks if task["task_id"] != done["task_id"])

    agent.registry.apply_lifecycle(DEMAND_ID, "suspend")
    _runtime(agent).tick(now=AS_OF, phase="post_market")

    _assert_task_status(agent, done, "done")
    _assert_task_status(agent, pending, "cancelled")
    assert _message(agent, pending["task_ticket_id"])["status"] == "done"
    assert agent.run_once(topics=COLLECTION_TOPIC)["status"] == "idle"
    tool.assert_called_once()


@pytest.mark.parametrize("repair_via", ["reader", "recovery"])
def test_projection_failure_cannot_retry_completed_tool_and_later_repair_succeeds(agent, monkeypatch, repair_via):
    from agent_trade_intel import task_lifecycle

    task, = _plan(agent)
    tool = Mock(side_effect=_empty_mic_result)
    monkeypatch.setattr(agent.mic, "collect", tool)
    with monkeypatch.context() as failing_projection:
        failing_projection.setattr(
            task_lifecycle, "sync_task_statuses", Mock(side_effect=sqlite3.OperationalError("offline projection unavailable"))
        )
        result = agent.run_once(topics=COLLECTION_TOPIC)
        assert result["status"] == "processed", result
        assert agent.tickets.get(task["task_ticket_id"])["status"] == "done"
        assert _message(agent, task["task_ticket_id"])["status"] == "done"
        assert agent.run_once(topics=COLLECTION_TOPIC)["status"] == "idle"

    if repair_via == "recovery":
        RecoveryManager(
            agent.bus_store, agent.queue, agent.tickets,
            agent.config.runtime.agent_id, agent.config.runtime.agent_group,
            data_store=agent.data_store,
        ).recover()
        assert _tasks(agent)[0]["status"] == "done"
    reported = _reader(agent).read_collection_status(demand_id=DEMAND_ID)
    assert reported["tasks"][0]["status"] == "done"
    assert _tasks(agent)[0]["status"] == "done"
    tool.assert_called_once()


@pytest.mark.parametrize("tool_kind", ["mic", "stock"])
def test_late_result_after_suspend_cannot_revive_cancelled_task(agent, monkeypatch, tool_kind):
    task, = _plan(agent, stock_only=tool_kind == "stock")

    def suspend_then_return(**kwargs):
        agent.registry.apply_lifecycle(DEMAND_ID, "suspend")
        _runtime(agent).tick(now=AS_OF, phase="post_market")
        _assert_task_status(agent, task, "cancelled")
        if tool_kind == "mic":
            return _empty_mic_result(**kwargs)
        return ToolResult(
            tool_name="stock_data_collector", operation="fetch_trading_status",
            request=kwargs, status="success",
        ).finish()

    tool = Mock(side_effect=suspend_then_return)
    if tool_kind == "mic":
        monkeypatch.setattr(agent.mic, "collect", tool)
    else:
        monkeypatch.setattr(agent.stock, "fetch_trading_status", tool)
    result = agent.run_once(worker_id="cancelled-worker", topics=COLLECTION_TOPIC)

    _assert_task_status(agent, task, "cancelled")
    delivery = _message(agent, task["task_ticket_id"])
    assert delivery["status"] == "done" and delivery["lease_owner"] is None
    assert result["status"] != "retry_scheduled"
    assert _reader(agent).read_collection_status(demand_id=DEMAND_ID)["runs"] == []
    tool.assert_called_once()


@pytest.mark.parametrize("agent", ["split", "shared"], indirect=True)
@pytest.mark.parametrize("tool_kind", ["mic", "stock"])
def test_suspend_at_result_write_boundary_rejects_already_returned_tool_output(agent, monkeypatch, tool_kind):
    task, = _plan(agent, stock_only=tool_kind == "stock")
    if tool_kind == "mic":
        tool = Mock(side_effect=_empty_mic_result)
        monkeypatch.setattr(agent.mic, "collect", tool)
    else:
        tool = Mock(return_value=ToolResult(
            tool_name="stock_data_collector", operation="fetch_trading_status",
            request={}, status="success",
        ).finish())
        monkeypatch.setattr(agent.stock, "fetch_trading_status", tool)
    original_save_run = agent.persister.save_run

    def suspend_before_guarded_write(**kwargs):
        # The tool and its post-call lease check have both returned; cancellation
        # lands at the persistence boundary, before the guarded write starts.
        tool.assert_called_once()
        agent.registry.apply_lifecycle(DEMAND_ID, "suspend")
        _runtime(agent).tick(now=AS_OF, phase="post_market")
        return original_save_run(**kwargs)

    write = Mock(side_effect=suspend_before_guarded_write)
    monkeypatch.setattr(agent.persister, "save_run", write)
    result = agent.run_once(topics=COLLECTION_TOPIC)

    assert result["status"] == "lease_lost", result
    _assert_task_status(agent, task, "cancelled")
    assert _message(agent, task["task_ticket_id"])["status"] == "done"
    assert _reader(agent).read_collection_status(demand_id=DEMAND_ID)["runs"] == []
    write.assert_called_once()
    tool.assert_called_once()


@pytest.mark.parametrize("agent", ["split", "shared"], indirect=True)
def test_recovery_republishes_committed_completion_receipt_without_recalling_tool(agent, monkeypatch):
    task, = _plan(agent)
    ticket_id = task["task_ticket_id"]
    tool = Mock(side_effect=_empty_mic_result)
    monkeypatch.setattr(agent.mic, "collect", tool)
    original_publish = agent.queue.publish

    def fail_completion_publication(topic, payload, **kwargs):
        if topic == "collection.result":
            raise sqlite3.OperationalError("offline result publication unavailable")
        return original_publish(topic, payload, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(agent.queue, "publish", fail_completion_publication)
        result = agent.run_once(topics=COLLECTION_TOPIC)
        assert result["status"] == "finalization_error", result
        _assert_task_status(agent, task, "done")
        assert _message(agent, ticket_id)["status"] == "done"
        assert agent.queue.list_messages(topic="collection.result") == []
        with agent.bus_store.session() as con:
            receipts = con.execute(
                "SELECT payload_json FROM ticket_events WHERE ticket_id=? AND event_type='collection_result_ready'",
                (ticket_id,),
            ).fetchall()
        assert len(receipts) == 1
        receipt = loads_json(receipts[0]["payload_json"], {})

    recovery = RecoveryManager(
        agent.bus_store, agent.queue, agent.tickets,
        agent.config.runtime.agent_id, agent.config.runtime.agent_group,
        data_store=agent.data_store,
    )
    recovered = recovery.recover()
    published = agent.queue.list_messages(topic="collection.result")
    assert len(published) == 1
    assert published[0]["payload"] == receipt
    assert published[0]["message_id"] in recovered["collection_results"]
    recovery.recover()
    assert len(agent.queue.list_messages(topic="collection.result")) == 1
    assert agent.run_once(topics=COLLECTION_TOPIC)["status"] == "idle"
    assert len(_reader(agent).read_collection_status(demand_id=DEMAND_ID)["runs"]) == 1
    tool.assert_called_once()


@pytest.mark.parametrize("new_worker", ["other-worker", "same-worker"])
@pytest.mark.parametrize("tool_kind", ["mic", "stock"])
def test_late_result_cannot_overwrite_new_lease_even_with_reused_worker_id(agent, monkeypatch, new_worker, tool_kind):
    task, = _plan(agent, stock_only=tool_kind == "stock")
    ticket_id = task["task_ticket_id"]

    def steal_then_return(**kwargs):
        message_id = _message(agent, ticket_id)["message_id"]
        # Emulate expiry followed by another delivery; attempts is the lease generation.
        with agent.bus_store.session() as con:
            con.execute("UPDATE messages SET lease_until='2000-01-01T00:00:00+00:00' WHERE message_id=?", (message_id,))
        assert agent.queue.requeue_expired_leases() == 1
        acquired = agent.queue.lease(topics=COLLECTION_TOPIC, worker_id=new_worker, lease_seconds=60)
        assert acquired.message_id == message_id and acquired.attempts == 2
        if tool_kind == "mic":
            return _empty_mic_result(**kwargs)
        return ToolResult(
            tool_name="stock_data_collector", operation="fetch_trading_status",
            request=kwargs, status="success",
        ).finish()

    tool = Mock(side_effect=steal_then_return)
    if tool_kind == "mic":
        monkeypatch.setattr(agent.mic, "collect", tool)
    else:
        monkeypatch.setattr(agent.stock, "fetch_trading_status", tool)
    result = agent.run_once(worker_id="same-worker", topics=COLLECTION_TOPIC)

    assert result["status"] == "lease_lost", result
    delivery = _message(agent, ticket_id)
    assert delivery["status"] == "in_progress"
    assert delivery["lease_owner"] == new_worker and delivery["attempts"] == 2
    assert agent.tickets.get(ticket_id)["status"] != "done"
    assert _reader(agent).read_collection_status(demand_id=DEMAND_ID)["runs"] == []
    tool.assert_called_once()


def test_redelivery_of_done_ticket_does_not_call_adapter_again(agent, monkeypatch):
    task, = _plan(agent)
    tool = Mock(side_effect=_empty_mic_result)
    monkeypatch.setattr(agent.mic, "collect", tool)
    assert agent.run_once(topics=COLLECTION_TOPIC)["status"] == "processed"
    message_id = _message(agent, task["task_ticket_id"])["message_id"]
    with agent.bus_store.session() as con:
        # Crash after the Ticket commit but before the delivery acknowledgement.
        con.execute(
            "UPDATE messages SET status='open', lease_owner=NULL, lease_until=NULL WHERE message_id=?", (message_id,)
        )

    agent.run_once(topics=COLLECTION_TOPIC)

    _assert_task_status(agent, task, "done")
    assert _message(agent, task["task_ticket_id"])["status"] == "done"
    tool.assert_called_once()


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "expired", "dead"])
def test_recovery_closes_terminal_deliveries_and_repairs_stale_task(agent, terminal_status):
    task, = _plan(agent)
    ticket_id = task["task_ticket_id"]
    leased = agent.queue.lease(topics=COLLECTION_TOPIC, worker_id="crashed-worker", lease_seconds=60)
    agent.tickets.update_status(ticket_id, terminal_status, "offline terminal transition before crash")

    recovery = RecoveryManager(
        agent.bus_store, agent.queue, agent.tickets,
        agent.config.runtime.agent_id, agent.config.runtime.agent_group,
        data_store=agent.data_store,
    ).recover()

    assert leased.message_id in recovery["acked_completed"]
    assert _message(agent, ticket_id)["status"] == "done"
    expected_task_status = "failed" if terminal_status == "dead" else terminal_status
    assert _tasks(agent)[0]["status"] == expected_task_status
    assert agent.tickets.get(ticket_id)["status"] == terminal_status
    assert agent.run_once(topics=COLLECTION_TOPIC)["status"] == "idle"


@pytest.mark.parametrize("new_worker", ["other-worker", "same-worker"])
def test_stale_lease_cannot_ack_nack_or_extend_new_delivery(agent, new_worker):
    task, = _plan(agent)
    original = agent.queue.lease(topics=COLLECTION_TOPIC, worker_id="same-worker", lease_seconds=60)
    with agent.bus_store.session() as con:
        con.execute(
            "UPDATE messages SET lease_until='2000-01-01T00:00:00+00:00' WHERE message_id=?",
            (original.message_id,),
        )
    assert agent.queue.requeue_expired_leases() == 1
    replacement = agent.queue.lease(topics=COLLECTION_TOPIC, worker_id=new_worker, lease_seconds=60)
    before = _message(agent, task["task_ticket_id"])

    assert agent.queue.ack(
        original.message_id, worker_id="same-worker", lease_attempt=original.attempts,
    ) is False
    assert agent.queue.nack(
        original.message_id, {"error_code": "STALE_WORKER"}, retryable=False,
        worker_id="same-worker", lease_attempt=original.attempts,
    ) is None
    assert agent.queue.extend_lease(
        original.message_id, "same-worker", 600, lease_attempt=original.attempts,
    ) is False

    after = _message(agent, task["task_ticket_id"])
    for field in ("status", "lease_owner", "lease_until", "attempts", "error_json"):
        assert after[field] == before[field]
    assert replacement.attempts == original.attempts + 1
    assert agent.tickets.get(task["task_ticket_id"])["status"] == "open"


def test_operator_retry_generation_rejects_old_lease_when_worker_and_attempt_repeat(agent):
    task, = _plan(agent)
    original = agent.queue.lease(topics=COLLECTION_TOPIC, worker_id="same-worker", lease_seconds=60)
    assert agent.queue.retry_message(original.message_id) is True
    queued = _message(agent, task["task_ticket_id"])
    assert queued["attempts"] == 0
    assert queued["lease_generation"] == original.lease_generation
    replacement = agent.queue.lease(topics=COLLECTION_TOPIC, worker_id="same-worker", lease_seconds=60)
    assert replacement.attempts == original.attempts
    assert replacement.lease_generation == original.lease_generation + 1
    before = _message(agent, task["task_ticket_id"])

    assert agent.queue.ack(
        original.message_id, worker_id="same-worker", lease_attempt=original.attempts,
        lease_generation=original.lease_generation,
    ) is False
    assert agent.queue.nack(
        original.message_id, {"error_code": "STALE_WORKER"}, retryable=False,
        worker_id="same-worker", lease_attempt=original.attempts,
        lease_generation=original.lease_generation,
    ) is None
    assert agent.queue.extend_lease(
        original.message_id, "same-worker", 600, lease_attempt=original.attempts,
        lease_generation=original.lease_generation,
    ) is False

    after = _message(agent, task["task_ticket_id"])
    for field in ("status", "lease_owner", "lease_until", "attempts", "lease_generation", "error_json"):
        assert after[field] == before[field]
    assert agent.queue.ack(
        replacement.message_id, worker_id="same-worker", lease_attempt=replacement.attempts,
        lease_generation=replacement.lease_generation,
    ) is True


def test_operator_retry_keeps_failed_and_successful_execution_evidence_separate(agent, monkeypatch):
    task, = _plan(agent)
    ticket_id = task["task_ticket_id"]
    message_id = _message(agent, ticket_id)["message_id"]
    with agent.bus_store.session() as con:
        con.execute("UPDATE messages SET max_attempts=1 WHERE message_id=?", (message_id,))
    failure = ToolResult(
        tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
        status="failed", errors=[{
            "error_code": "MIC_PROFILE_BUSY", "error_message": "offline contention", "retryable": True,
        }],
    ).finish()
    tool = Mock(side_effect=[failure, _empty_mic_result(target_id="company_offline_0", task_profile={})])
    monkeypatch.setattr(agent.mic, "collect", tool)

    first = agent.run_once(worker_id="same-worker", topics=COLLECTION_TOPIC)
    assert first["status"] == "dead_lettered"
    original = _message(agent, ticket_id)
    _assert_task_status(agent, task, "failed")
    assert agent.queue.retry_message(message_id) is True
    second = agent.run_once(worker_id="same-worker", topics=COLLECTION_TOPIC)

    assert second["status"] == "processed", second
    replacement = _message(agent, ticket_id)
    assert replacement["attempts"] == original["attempts"] == 1
    assert replacement["lease_generation"] == original["lease_generation"] + 1
    _assert_task_status(agent, task, "done")
    runs = _reader(agent).read_collection_status(demand_id=DEMAND_ID)["runs"]
    assert len(runs) == 2
    assert {row["status"] for row in runs} == {"failed", "success"}
    assert len({row["run_id"] for row in runs}) == 2
    assert tool.call_count == 2


def test_cancellation_cannot_be_revived_by_attempt_heartbeat_or_late_finish(agent):
    attempt_id, blocker = agent.attempts.start(
        task_key="cancelled-offline-attempt", owner_token="original-owner", deadline_seconds=60,
    )
    assert attempt_id and blocker is None
    agent.attempts.mark_running(attempt_id)
    agent.attempts.request_cancel(attempt_id)

    assert agent.attempts.heartbeat(attempt_id, state="running") is False
    agent.attempts.mark_running(attempt_id)
    assert agent.attempts.get(attempt_id)["state"] == "cancelling"
    agent.attempts.finish(attempt_id, state="cancelled", error_code="operator_cancel", cleanup="complete")
    agent.attempts.finish(attempt_id, state="completed")

    assert agent.attempts.heartbeat(attempt_id, state="running") is False
    final = agent.attempts.get(attempt_id)
    assert final["state"] == "cancelled"
    assert final["error_code"] == "operator_cancel"


@pytest.mark.parametrize("agent", ["shared"], indirect=True)
def test_shared_store_semantic_mic_preserves_outer_result_guard(agent, monkeypatch):
    from agent_trade_intel import semantic_event_store
    from mic.event_resolution import PROTOCOL

    task, = _plan(agent)
    original_save = semantic_event_store.save

    def assert_outer_transaction_survives(*args, **kwargs):
        saved = original_save(*args, **kwargs)
        with agent.data_store.session() as con:
            assert con.in_transaction, "semantic save committed the outer cancellation guard"
        return saved

    def collect(**kwargs):
        result = _empty_mic_result(**kwargs)
        result.result.update({
            "event_resolution_protocol": PROTOCOL,
            "coverage_gaps": [{"gap_id": "offline-semantic-gap", "description": "离线样例缺口"}],
        })
        return result

    monkeypatch.setattr(semantic_event_store, "save", assert_outer_transaction_survives)
    tool = Mock(side_effect=collect)
    monkeypatch.setattr(agent.mic, "collect", tool)
    outcome = agent.run_once(topics=COLLECTION_TOPIC)

    assert outcome["status"] == "processed", outcome
    assert outcome["result"]["output_status"] == "no_structured_output"
    _assert_task_status(agent, task, "done")
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM coverage_gaps WHERE gap_id='offline-semantic-gap'").fetchone()[0] == 1
    assert len(agent.queue.list_messages(topic="collection.result")) == 1
    tool.assert_called_once()


@pytest.mark.parametrize("agent", ["shared"], indirect=True)
def test_semantic_savepoint_does_not_commit_outer_transaction(agent):
    from mic.event_resolution import PROTOCOL

    result = _empty_mic_result(target_id="offline-target", task_profile={})
    result.result.update({
        "event_resolution_protocol": PROTOCOL,
        "coverage_gaps": [{"gap_id": "uncommitted-gap", "description": "事务回滚样例"}],
    })
    with agent.data_store.session() as con:
        con.execute("BEGIN IMMEDIATE")
        with agent.data_store.reuse_connection(con):
            agent.persister.save_mic_structures(task={"target": {"target_id": "offline-target"}}, result=result)
        assert con.in_transaction
        assert con.execute("SELECT COUNT(*) FROM coverage_gaps").fetchone()[0] == 1
        con.execute("ROLLBACK")
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM coverage_gaps").fetchone()[0] == 0


def test_semantic_replay_keeps_gap_without_supplied_id_idempotent(agent):
    from mic.event_resolution import PROTOCOL

    task = {"task_id": "task", "target": {"target_id": "offline-target"}}
    result = _empty_mic_result(target_id="offline-target", task_profile={})
    result.result.update({
        "event_resolution_protocol": PROTOCOL, "search_run_id": "offline-report",
        "coverage_gaps": [{"description": "可重复保存的同一缺口", "suggested_next_queries": ["离线样例"]}],
    })
    agent.persister.save_mic_structures(task=task, result=result)
    agent.persister.save_mic_structures(task=task, result=result)
    with agent.data_store.session() as con:
        rows = con.execute("SELECT gap_id, description FROM coverage_gaps").fetchall()
    assert len(rows) == 1
    assert rows[0]["description"] == "可重复保存的同一缺口"
    agent.persister.save_mic_structures(task={**task, "task_id": "different-task"}, result=result)
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM coverage_gaps").fetchone()[0] == 2
