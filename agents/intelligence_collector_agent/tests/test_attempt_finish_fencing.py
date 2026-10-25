"""Deterministic races at MIC attempt completion; no external collection or processes."""

from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from agent_trade_intel import agent as agent_module
from agent_trade_intel import attempts as attempts_module
from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.attempts import CollectionAttemptRepository
from test_task_lifecycle_execution import COLLECTION_TOPIC, _empty_mic_result, _plan
from test_task_lifecycle_execution import agent as agent


@pytest.mark.parametrize("agent", ["split", "shared"], indirect=True)
@pytest.mark.parametrize("revocation", ["request_cancel", "terminal_cancel"])
def test_attempt_revoked_between_read_and_finish_cannot_publish_output(agent, monkeypatch, revocation):
    _plan(agent)
    external = CollectionAttemptRepository(agent.data_store)
    original_finish = agent.attempts.finish
    revoked = []

    def revoke_before_finish(attempt_id, **kwargs):
        if not revoked:
            revoked.append(attempt_id)
            if revocation == "request_cancel":
                external.request_cancel(attempt_id)
            else:
                assert external.finish(attempt_id, state="cancelled", error_code="external_cancel")
        return original_finish(attempt_id, **kwargs)

    tool = Mock(side_effect=_empty_mic_result)
    monkeypatch.setattr(agent.mic, "collect", tool)
    monkeypatch.setattr(agent.attempts, "finish", revoke_before_finish)
    outcome = agent.run_once(topics=COLLECTION_TOPIC)

    assert outcome["status"] == "lease_lost", outcome
    assert len(revoked) == 1
    saved_attempt = agent.attempts.get(revoked[0])
    assert saved_attempt["state"] == "cancelled"
    if revocation == "terminal_cancel":
        assert saved_attempt["error_code"] == "external_cancel"
    assert agent.queue.list_messages(topic="collection.result") == []
    assert agent.queue.list_messages(topic="intelligence.events") == []
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM structured_events").fetchone()[0] == 0
    tool.assert_called_once()


@pytest.mark.parametrize("agent", ["split", "shared"], indirect=True)
@pytest.mark.parametrize("revocation", ["lease_loss", "terminal_cancel"])
def test_revocation_retains_live_cleanup_blocker_for_next_task(agent, monkeypatch, revocation):
    _plan(agent, target_count=2)
    leftover_pid = 987654
    external = CollectionAttemptRepository(agent.data_store)

    def lose_lease_with_incomplete_cleanup(**kwargs):
        agent.mic.last_outcome = {
            "status": "cleanup_incomplete", "cleanup": "incomplete", "worker_pid": leftover_pid,
        }
        if revocation == "terminal_cancel":
            assert external.finish(kwargs["attempt_id"], state="cancelled", error_code="external_cancel")
            assert external.get(kwargs["attempt_id"])["state"] == "cancelled"
        else:
            with agent.bus_store.session() as con:
                con.execute(
                    "UPDATE messages SET lease_owner='replacement-worker' WHERE message_id=?",
                    (agent._current_lease[0],),
                )
        return ToolResult(
            tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
            status="failed", errors=[{
                "error_code": "MIC_CLEANUP_INCOMPLETE", "error_message": "offline leftover evidence",
                "retryable": False,
            }],
        ).finish()

    tool = Mock(side_effect=lose_lease_with_incomplete_cleanup)
    monkeypatch.setattr(agent.mic, "collect", tool)
    monkeypatch.setattr(attempts_module, "pid_owns_attempt", lambda pid, attempt_id: pid == leftover_pid)
    first = agent.run_once(topics=COLLECTION_TOPIC)
    assert first["status"] == "lease_lost", first
    with agent.data_store.session() as con:
        attempts = con.execute("SELECT * FROM collection_attempt").fetchall()
        assert con.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0
    assert len(attempts) == 1
    assert attempts[0]["state"] == "cleanup_incomplete"
    assert attempts[0]["cleanup"] == "incomplete"
    assert attempts[0]["worker_pid"] == leftover_pid
    assert agent.attempts.blocking_cleanup_incomplete()["attempt_id"] == attempts[0]["attempt_id"]

    blocked = agent.run_once(topics=COLLECTION_TOPIC)
    assert blocked["status"] == "processed", blocked
    assert blocked["result"]["usable"] is False
    assert agent.attempts.get(attempts[0]["attempt_id"])["state"] == "cleanup_incomplete"
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM collection_attempt").fetchone()[0] == 1
    tool.assert_called_once()


def test_short_lease_schedules_heartbeat_before_expiry(agent, monkeypatch):
    _plan(agent)
    agent.lease_seconds = 30
    intervals = []

    @contextmanager
    def record_heartbeat(keepalive, interval_seconds):
        intervals.append(interval_seconds)
        keepalive()
        yield

    monkeypatch.setattr(agent_module, "lease_heartbeat", record_heartbeat)
    monkeypatch.setattr(agent.mic, "collect", Mock(side_effect=_empty_mic_result))
    outcome = agent.run_once(topics=COLLECTION_TOPIC)

    assert outcome["status"] == "processed", outcome
    assert len(intervals) == 1
    assert 0 < intervals[0] < agent.lease_seconds
