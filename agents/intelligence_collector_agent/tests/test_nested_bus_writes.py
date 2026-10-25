"""Nested notification writes must retain the surrounding lease-guard transaction."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.tickets import TicketRepository
from agent_trade_intel.work_guard import WorkRevoked, result_write_guard


@pytest.fixture
def bus_work(tmp_path: Path):
    bus = SQLiteStore(tmp_path / "bus.db")
    bus.init_schema()
    tickets, queue = TicketRepository(bus), SQLiteMessageQueue(bus)
    parent = tickets.create_ticket(ticket_type="COLLECTION_REQUEST_TICKET", source_agent="test", status="in_progress")
    message = queue.publish("intelligence.collection", {"ticket_id": parent})
    delivery = queue.lease(topics=["intelligence.collection"], worker_id="worker", lease_seconds=60)
    lease = (message, "worker", delivery.attempts, delivery.lease_generation)
    tickets.active_lease = queue.active_lease = lease
    return SimpleNamespace(bus=bus, tickets=tickets, queue=queue, parent=parent, lease=lease)


def notification(work, *, key="notice"):
    ticket = work.tickets.create_ticket(
        ticket_type="EVENT_TICKET", source_agent="test", parent_ticket_id=work.parent,
        payload={"run_id": "offline-run"}, idempotency_key="ticket:" + key,
    )
    message = work.queue.publish(
        "intelligence.events", {"ticket_id": ticket}, idempotency_key="message:" + key,
    )
    return ticket, message


def assert_outer_lock_is_held(work):
    # connect() intentionally bypasses reuse_connection, like another process.
    con = work.bus.connect()
    try:
        con.execute("PRAGMA busy_timeout=0")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            con.execute("BEGIN IMMEDIATE")
    finally:
        con.close()


def test_notification_writes_and_idempotent_reuse_preserve_outer_transaction(bus_work):
    work = bus_work
    with result_write_guard(work.bus, work.lease):
        ticket, message = notification(work)
        assert notification(work) == (ticket, message)
        with work.bus.session() as outer:
            assert outer.in_transaction
            assert outer.execute("SELECT COUNT(*) FROM ticket_events WHERE ticket_id=?", (ticket,)).fetchone()[0] == 1
        observer = work.bus.connect()
        try:
            assert observer.execute("SELECT ticket_id FROM tickets WHERE ticket_id=?", (ticket,)).fetchone() is None
            assert observer.execute("SELECT message_id FROM messages WHERE message_id=?", (message,)).fetchone() is None
        finally:
            observer.close()
        assert_outer_lock_is_held(work)
    assert work.tickets.get(ticket) is not None
    assert work.queue.inspect(message)["status"] == "open"
    with work.bus.session() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")


def test_outer_failure_rolls_back_notification_ticket_event_and_message(bus_work):
    work = bus_work
    with pytest.raises(OSError, match="outer publication failed"):
        with result_write_guard(work.bus, work.lease):
            ticket, message = notification(work)
            assert notification(work) == (ticket, message)
            raise OSError("outer publication failed")
    assert work.tickets.get(ticket) is None
    assert work.queue.inspect(message) is None
    with work.bus.session() as con:
        assert con.execute("SELECT COUNT(*) FROM ticket_events WHERE ticket_id=?", (ticket,)).fetchone()[0] == 0
    assert work.queue.inspect(work.lease[0])["status"] == "in_progress"


def test_caught_nested_failure_rolls_back_only_its_savepoint(bus_work, monkeypatch):
    work = bus_work

    def fail_event(*args, **kwargs):
        raise OSError("nested ticket event failed")

    with result_write_guard(work.bus, work.lease):
        before = notification(work, key="before")
        with monkeypatch.context() as patch:
            patch.setattr(work.tickets, "_append_event", fail_event)
            with pytest.raises(OSError, match="nested ticket event failed"):
                work.tickets.create_ticket(ticket_type="EVENT_TICKET", source_agent="test", idempotency_key="broken")
        with work.bus.session() as outer:
            assert outer.in_transaction
            assert outer.execute("SELECT ticket_id FROM tickets WHERE idempotency_key='broken'").fetchone() is None
        with pytest.raises(TypeError):
            work.queue.publish("intelligence.events", {"not_json": object()}, idempotency_key="broken-message")
        after = notification(work, key="after")
        assert_outer_lock_is_held(work)
    assert all(work.tickets.get(ticket) and work.queue.inspect(message) for ticket, message in (before, after))
    with work.bus.session() as con:
        assert con.execute("SELECT message_id FROM messages WHERE idempotency_key='broken-message'").fetchone() is None


def test_nested_collection_writes_keep_active_lease_fencing(bus_work):
    work = bus_work
    stale = (*work.lease[:3], work.lease[3] + 1)
    with result_write_guard(work.bus, work.lease):
        work.tickets.active_lease = work.queue.active_lease = stale
        with pytest.raises(WorkRevoked):
            work.tickets.create_ticket(ticket_type="COLLECTION_TASK_TICKET", source_agent="test", parent_ticket_id=work.parent)
        with pytest.raises(WorkRevoked):
            work.queue.publish("intelligence.collection", {"ticket_id": work.parent})
        with work.bus.session() as outer:
            assert outer.in_transaction
        assert_outer_lock_is_held(work)


def test_explicit_publish_connection_also_keeps_callers_transaction(bus_work):
    work = bus_work
    with work.bus.session() as outer:
        outer.execute("BEGIN IMMEDIATE")
        message = work.queue.publish("collection.result", {"ticket_id": work.parent},
                                     idempotency_key="explicit", _connection=outer)
        assert work.queue.publish("collection.result", {"ticket_id": work.parent},
                                  idempotency_key="explicit", _connection=outer) == message
        assert outer.in_transaction
        assert_outer_lock_is_held(work)
        outer.execute("ROLLBACK")
    assert work.queue.inspect(message) is None


@pytest.mark.parametrize("status", ["open", "in_progress", "failed", "done"])
def test_result_notification_retry_does_not_change_source_task(bus_work, status):
    work = bus_work
    ticket = work.tickets.create_ticket(
        ticket_type="COLLECTION_TASK_TICKET", source_agent="test",
        parent_ticket_id=work.parent, status=status,
    )
    message = work.queue.publish("collection.result", {"ticket_id": ticket})
    work.queue.move_to_dead_letter(message, "notification consumer unavailable")
    assert work.tickets.get(ticket)["status"] == status
    assert work.queue.retry_message(message)
    assert work.queue.inspect(message)["status"] == "open"
    assert work.tickets.get(ticket)["status"] == status


def test_explicit_retry_reopens_dead_collection_ticket(bus_work):
    work = bus_work
    ticket = work.tickets.create_ticket(
        ticket_type="COLLECTION_TASK_TICKET", source_agent="test",
        parent_ticket_id=work.parent, status="dead",
    )
    message = work.queue.publish("intelligence.collection", {"ticket_id": ticket})
    work.queue.move_to_dead_letter(message, "prior execution exhausted")
    assert work.tickets.get(ticket)["status"] == "dead"
    assert work.queue.retry_message(message)
    assert work.tickets.get(ticket)["status"] == "open"
    assert work.queue.inspect(message)["status"] == "open"


def test_noncollection_ticket_does_not_block_message_retry(bus_work):
    work = bus_work
    ticket = work.tickets.create_ticket(ticket_type="FAULT_TICKET", source_agent="test", status="done")
    message = work.queue.publish("intelligence.collection", {"ticket_id": ticket})
    work.queue.move_to_dead_letter(message, "delivery failed")
    assert work.queue.retry_message(message)
    assert work.tickets.get(ticket)["status"] == "done"
