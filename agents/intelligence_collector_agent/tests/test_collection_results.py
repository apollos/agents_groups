from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_trade_intel.collection_results import publish_ready_results
from agent_trade_intel.db import SQLiteStore, loads_json
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.tickets import TicketRepository
from agent_trade_intel.work_guard import WorkRevoked


def _bus(tmp_path: Path) -> tuple[SQLiteStore, SQLiteMessageQueue, TicketRepository]:
    store = SQLiteStore(tmp_path / "bus.db")
    store.init_schema()
    return store, SQLiteMessageQueue(store), TicketRepository(store)


def _task(tickets: TicketRepository) -> str:
    return tickets.create_ticket(
        ticket_type="COLLECTION_TASK_TICKET", source_agent="test", correlation_id="test-trace",
        payload={"task_type": "mic_deep_collect"},
    )


def _payload(ticket_id: str, *, status: str = "success", run_id: str = "run") -> dict:
    return {
        "ticket_id": ticket_id, "ticket_type": "COLLECTION_TASK_TICKET", "task_type": "mic_deep_collect",
        "status": status, "usable": status == "success", "run_ids": [run_id],
        "output_status": "structured_output" if status == "success" else "tool_failure",
    }


def test_failed_publication_replays_exact_receipt_once_without_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    store, queue, tickets = _bus(tmp_path)
    ticket_id = _task(tickets)
    payload = _payload(ticket_id)
    tickets.update_status(ticket_id, "done", result_payload=payload)
    original_publish = queue.publish

    def unavailable(*args, **kwargs):
        original_publish(*args, **kwargs)
        raise OSError("publication temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(queue, "publish", unavailable)
        with pytest.raises(OSError, match="publication temporarily unavailable"):
            publish_ready_results(store, queue, ticket_id)
    assert tickets.get(ticket_id)["status"] == "done"
    assert queue.list_messages(topic="collection.result") == []
    first = publish_ready_results(store, queue, ticket_id)
    assert publish_ready_results(store, queue, ticket_id) == first
    messages = queue.list_messages(topic="collection.result")
    assert len(messages) == 1
    assert messages[0]["payload"] == payload
    assert messages[0]["correlation_id"] == "test-trace"
    # Recovery only publishes receipts; it neither creates task deliveries nor tool runs.
    assert queue.list_messages(topic="intelligence.collection") == []
    with store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM ticket_events WHERE event_type='collection_result_ready'",
        ).fetchone()[0] == 1


def test_receipt_failure_rolls_back_terminal_ticket_transition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, queue, tickets = _bus(tmp_path)
    ticket_id = _task(tickets)
    original_append = tickets._append_event

    def fail_receipt(con, ticket_id, event_type, *args):
        if event_type == "collection_result_ready":
            raise OSError("receipt storage unavailable")
        return original_append(con, ticket_id, event_type, *args)

    monkeypatch.setattr(tickets, "_append_event", fail_receipt)
    with pytest.raises(OSError, match="receipt storage unavailable"):
        tickets.update_status(ticket_id, "done", result_payload=_payload(ticket_id))
    assert tickets.get(ticket_id)["status"] == "open"
    assert publish_ready_results(store, queue) == []
    with store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM ticket_events WHERE event_type='status_changed'").fetchone()[0] == 0


def test_old_terminal_ticket_without_receipt_does_not_fabricate_result(tmp_path: Path):
    store, queue, tickets = _bus(tmp_path)
    for status in ("done", "failed", "cancelled", "expired"):
        ticket_id = _task(tickets)
        tickets.update_status(ticket_id, status)
    assert publish_ready_results(store, queue) == []
    assert queue.list_messages(topic="collection.result") == []


def test_operator_retry_publishes_new_receipt_without_duplicating_old_one(tmp_path: Path):
    store, queue, tickets = _bus(tmp_path)
    ticket_id = _task(tickets)
    failed = _payload(ticket_id, status="failed", run_id="failed-run")
    tickets.update_status(ticket_id, "failed", result_payload=failed)
    first = publish_ready_results(store, queue, ticket_id)
    tickets.update_status(ticket_id, "open")
    assert publish_ready_results(store, queue, ticket_id) == []
    successful = _payload(ticket_id, run_id="successful-run")
    tickets.update_status(ticket_id, "done", result_payload=successful)
    second = publish_ready_results(store, queue, ticket_id)
    assert first != second
    assert publish_ready_results(store, queue, ticket_id) == second
    messages = queue.list_messages(topic="collection.result")
    assert len(messages) == 2
    assert {message["payload"]["run_ids"][0] for message in messages} == {"failed-run", "successful-run"}


def test_recovery_uses_only_latest_receipt_for_current_terminal_status(tmp_path: Path):
    store, queue, tickets = _bus(tmp_path)
    ticket_id = _task(tickets)
    tickets.update_status(ticket_id, "failed", result_payload=_payload(ticket_id, status="failed"))
    tickets.update_status(ticket_id, "open")
    latest = _payload(ticket_id, run_id="latest-run")
    tickets.update_status(ticket_id, "done", result_payload=latest)
    assert len(publish_ready_results(store, queue)) == 1
    assert queue.list_messages(topic="collection.result")[0]["payload"] == latest
    with store.session() as con:
        receipts = con.execute("SELECT payload_json FROM ticket_events WHERE event_type='collection_result_ready'").fetchall()
    assert [loads_json(row["payload_json"])["status"] for row in receipts] == ["failed", "success"]


@pytest.mark.parametrize("new_status", ["done", "failed"])
def test_terminal_transition_without_new_receipt_cannot_republish_stale_result(tmp_path: Path, new_status: str):
    store, queue, tickets = _bus(tmp_path)
    ticket_id = _task(tickets)
    tickets.update_status(ticket_id, "failed", result_payload=_payload(ticket_id, status="failed"))
    tickets.update_status(ticket_id, "open")
    tickets.update_status(ticket_id, new_status)
    assert publish_ready_results(store, queue) == []


def test_nonterminal_or_failed_compare_and_swap_cannot_create_receipt(tmp_path: Path):
    store, queue, tickets = _bus(tmp_path)
    ticket_id = _task(tickets)
    with pytest.raises(ValueError, match="terminal"):
        tickets.update_status(ticket_id, "in_progress", result_payload=_payload(ticket_id))
    with pytest.raises(ValueError, match="identify its ticket"):
        tickets.update_status(ticket_id, "done", result_payload=_payload("another-ticket"))
    assert not tickets.update_status(
        ticket_id, "done", expected_statuses={"in_progress"}, result_payload=_payload(ticket_id),
    )
    assert publish_ready_results(store, queue) == []


def test_cancelled_work_cannot_commit_a_late_result_receipt(tmp_path: Path):
    store, queue, tickets = _bus(tmp_path)
    ticket_id = _task(tickets)
    message_id = queue.publish("intelligence.collection", {"ticket_id": ticket_id})
    delivery = queue.lease(topics=["intelligence.collection"], worker_id="worker")
    tickets.active_lease = (message_id, "worker", delivery.attempts, delivery.lease_generation)
    TicketRepository(store).update_status(ticket_id, "cancelled")
    with pytest.raises(WorkRevoked):
        tickets.update_status(ticket_id, "done", result_payload=_payload(ticket_id))
    assert tickets.get(ticket_id)["status"] == "cancelled"
    assert publish_ready_results(store, queue) == []


def test_receipt_validation_and_publication_hold_one_bus_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    store, queue, tickets = _bus(tmp_path)
    ticket_id = _task(tickets)
    tickets.update_status(ticket_id, "failed", result_payload=_payload(ticket_id, status="failed"))
    original_publish = queue.publish

    def publish_while_operator_tries_to_reopen(*args, **kwargs):
        assert kwargs["_connection"].in_transaction
        with store.session() as competing:
            competing.execute("PRAGMA busy_timeout=1")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("UPDATE tickets SET status='open' WHERE ticket_id=?", (ticket_id,))
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(queue, "publish", publish_while_operator_tries_to_reopen)
    assert len(publish_ready_results(store, queue, ticket_id)) == 1
    assert tickets.get(ticket_id)["status"] == "failed"
    tickets.update_status(ticket_id, "open")
    assert publish_ready_results(store, queue, ticket_id) == []
