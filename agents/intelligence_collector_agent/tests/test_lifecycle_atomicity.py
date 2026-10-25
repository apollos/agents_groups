"""Bus lifecycle transitions must preserve receipts and serialize with workers."""
from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.recovery import RecoveryManager
from agent_trade_intel.runtime import RuntimeController
from agent_trade_intel.tickets import TicketRepository
from agent_trade_intel.work_guard import WorkRevoked


@pytest.fixture
def lifecycle(tmp_path: Path):
    bus = SQLiteStore(tmp_path / "bus.db")
    data = SQLiteStore(tmp_path / "data.db")
    bus.init_schema()
    data.init_schema()
    queue, tickets = SQLiteMessageQueue(bus), TicketRepository(bus)
    config = SimpleNamespace(runtime=SimpleNamespace(agent_id="test_agent", agent_group="test_group"))
    runtime = RuntimeController(config, state_store=data, bus_store=bus, data_store=data)
    recovery = RecoveryManager(bus, queue, tickets, "test_agent", "test_group", data_store=data)
    return SimpleNamespace(bus=bus, data=data, queue=queue, tickets=tickets, runtime=runtime, recovery=recovery)


def work(lifecycle, *, status="open", kind="COLLECTION_TASK_TICKET", payload=None):
    ticket_id = lifecycle.tickets.create_ticket(
        ticket_type=kind, source_agent="test", status=status,
        payload=payload or {"demand_id": "demand"},
    )
    message_id = lifecycle.queue.publish("intelligence.collection", {"ticket_id": ticket_id, "ticket_type": kind})
    return ticket_id, message_id


def test_recovery_keeps_result_and_quality_notifications_available(lifecycle):
    task_id, task_message = work(lifecycle, status="failed")
    request_id, request_message = work(lifecycle, status="cancelled", kind="COLLECTION_REQUEST_TICKET")
    other_id, other_message = work(lifecycle, status="done", kind="FAULT_TICKET")
    notifications = [lifecycle.queue.publish(topic, {"ticket_id": task_id})
                     for topic in ("collection.result", "intelligence.events", "intelligence.quality")]
    assert set(lifecycle.recovery._ack_completed()) == {task_message, request_message}
    assert all(lifecycle.queue.inspect(mid)["status"] == "open" for mid in notifications + [other_message])
    assert lifecycle.tickets.get(task_id)["status"] == "failed"
    assert lifecycle.tickets.get(request_id)["status"] == "cancelled"
    assert lifecycle.tickets.get(other_id)["status"] == "done"


def test_cancellation_matches_top_level_demand_and_preserves_completed_work(lifecycle):
    wanted, wanted_message = work(lifecycle)
    request, request_message = work(lifecycle, kind="COLLECTION_REQUEST_TICKET")
    done, done_message = work(lifecycle, status="done")
    unrelated, unrelated_message = work(lifecycle, payload={"demand_id": "other", "nested": {"demand_id": "demand"}})
    noncollection, noncollection_message = work(lifecycle, kind="FAULT_TICKET")
    notification = lifecycle.queue.publish("collection.result", {"ticket_id": wanted})
    # Whitespace is valid JSON and must not affect exact demand ownership.
    with lifecycle.bus.session() as con:
        con.execute("UPDATE tickets SET payload_json=? WHERE ticket_id=?", (json.dumps({"demand_id": "demand"}, indent=2), wanted))
    result = lifecycle.runtime._cancel_open_work("demand")
    assert set(result["tickets"]) == {wanted, request}
    assert set(result["messages"]) == {wanted_message, request_message}
    for tid in (wanted, request):
        assert lifecycle.tickets.get(tid)["status"] == "cancelled"
    assert lifecycle.tickets.get(done)["status"] == "done"
    assert lifecycle.tickets.get(unrelated)["status"] == "open"
    assert lifecycle.tickets.get(noncollection)["status"] == "open"
    assert all(lifecycle.queue.inspect(mid)["status"] == "open"
               for mid in (done_message, unrelated_message, noncollection_message, notification))


def test_cancellation_and_delivery_revocation_are_one_visible_transition(lifecycle, monkeypatch):
    ticket_id, message_id = work(lifecycle, status="in_progress")
    delivery = lifecycle.queue.lease(topics=["intelligence.collection"], worker_id="worker", lease_seconds=60)
    finishing_tickets = TicketRepository(lifecycle.bus)
    finishing_tickets.active_lease = (message_id, "worker", delivery.attempts, delivery.lease_generation)
    at_delivery_update, release = Event(), Event()
    finish_started, finish_done = Event(), Event()
    failures, cancelled = [], []
    original_connect = lifecycle.bus.connect

    def connect():
        con = original_connect()

        def trace(sql):
            normalized = " ".join(sql.upper().split())
            if current_thread().name == "cancel-work" and normalized.startswith("UPDATE MESSAGES SET STATUS='DONE'"):
                at_delivery_update.set()
                if not release.wait(5):
                    failures.append(TimeoutError("cancellation test barrier"))
            if current_thread().name == "finish-work" and normalized == "BEGIN IMMEDIATE":
                finish_started.set()
        con.set_trace_callback(trace)
        return con

    monkeypatch.setattr(lifecycle.bus, "connect", connect)

    def cancel():
        try:
            cancelled.append(lifecycle.runtime._cancel_open_work("demand"))
        except BaseException as exc:
            failures.append(exc)

    def finish():
        try:
            finishing_tickets.update_status(ticket_id, "done", "late completion")
            failures.append(AssertionError("cancelled work was finalized"))
        except WorkRevoked:
            pass
        except BaseException as exc:
            failures.append(exc)
        finally:
            finish_done.set()

    cancellation = Thread(target=cancel, name="cancel-work")
    finalization = Thread(target=finish, name="finish-work")
    cancellation.start()
    try:
        assert at_delivery_update.wait(5)
        # A reader sees the old committed pair until the cancellation commits both.
        assert lifecycle.tickets.get(ticket_id)["status"] == "in_progress"
        assert lifecycle.queue.inspect(message_id)["status"] == "in_progress"
        finalization.start()
        assert finish_started.wait(5)
        assert not finish_done.wait(0.05)
    finally:
        release.set()
        cancellation.join(5)
        if finalization.ident is not None:
            finalization.join(5)
    assert not cancellation.is_alive() and not finalization.is_alive()
    assert failures == []
    assert cancelled == [{"tickets": [ticket_id], "messages": [message_id]}]
    assert lifecycle.tickets.get(ticket_id)["status"] == "cancelled"
    assert lifecycle.queue.inspect(message_id)["status"] == "done"


def test_recovery_terminal_check_cannot_race_operator_retry(lifecycle, monkeypatch):
    ticket_id, message_id = work(lifecycle, status="failed")
    at_ticket_read, release, retry_started, retry_done = Event(), Event(), Event(), Event()
    failures, acked, retried = [], [], []
    original_connect = lifecycle.bus.connect

    def connect():
        con = original_connect()

        def trace(sql):
            normalized = " ".join(sql.upper().split())
            if current_thread().name == "recover-work" and normalized.startswith("SELECT") and "FROM TICKETS" in normalized:
                at_ticket_read.set()
                if not release.wait(5):
                    failures.append(TimeoutError("recovery test barrier"))
            if current_thread().name == "retry-work" and normalized == "BEGIN IMMEDIATE":
                retry_started.set()
        con.set_trace_callback(trace)
        return con

    monkeypatch.setattr(lifecycle.bus, "connect", connect)

    def recover():
        try:
            acked.extend(lifecycle.recovery._ack_completed())
        except BaseException as exc:
            failures.append(exc)

    def retry():
        try:
            retried.append(lifecycle.queue.retry_message(message_id))
        except BaseException as exc:
            failures.append(exc)
        finally:
            retry_done.set()

    recovery_thread = Thread(target=recover, name="recover-work")
    retry_thread = Thread(target=retry, name="retry-work")
    recovery_thread.start()
    try:
        assert at_ticket_read.wait(5)
        retry_thread.start()
        assert retry_started.wait(5)
        assert not retry_done.wait(0.05)
    finally:
        release.set()
        recovery_thread.join(5)
        if retry_thread.ident is not None:
            retry_thread.join(5)
    assert not recovery_thread.is_alive() and not retry_thread.is_alive()
    assert failures == []
    assert acked == [message_id]
    assert retried == [False]  # It was already retired before the operator acquired the lock.
    assert lifecycle.tickets.get(ticket_id)["status"] == "failed"
    assert lifecycle.queue.inspect(message_id)["status"] == "done"


def test_recovery_preserves_an_operator_retry_that_committed_first(lifecycle):
    ticket_id, message_id = work(lifecycle, status="failed")
    assert lifecycle.queue.retry_message(message_id)
    delivery = lifecycle.queue.lease(topics=["intelligence.collection"], worker_id="new-worker", lease_seconds=60)
    assert lifecycle.recovery._ack_completed() == []
    assert lifecycle.tickets.get(ticket_id)["status"] == "open"
    message = lifecycle.queue.inspect(message_id)
    assert message["status"] == "in_progress" and message["lease_owner"] == "new-worker"
    assert message["lease_generation"] == delivery.lease_generation


def test_failed_cancellation_event_rolls_back_ticket_and_message(lifecycle, monkeypatch):
    ticket_id, message_id = work(lifecycle)

    def fail_event(*args, **kwargs):
        raise OSError("event write failed")

    monkeypatch.setattr(lifecycle.runtime.tickets, "_append_event", fail_event)
    with pytest.raises(OSError, match="event write failed"):
        lifecycle.runtime._cancel_open_work("demand")
    assert lifecycle.tickets.get(ticket_id)["status"] == "open"
    assert lifecycle.queue.inspect(message_id)["status"] == "open"
