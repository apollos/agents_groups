"""Rebuild collection task status from the authoritative Ticket and delivery records.

The data store's ``collection_tasks`` rows are a query/report projection. A failed projection
write must not turn a completed tool call into another delivery attempt. Writers therefore use
``best_effort_sync``; public readers use the strict synchronizer so stale state is not reported
as current after a storage failure.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from .db import SQLiteStore, loads_json
from .logging_setup import get_logger

logger = get_logger("task_lifecycle")

_TERMINAL_TICKETS = {"done", "failed", "cancelled", "expired", "dead"}


def sync_task_statuses(data_store: SQLiteStore, bus_store: SQLiteStore) -> dict[str, Any]:
    """Synchronize task statuses without dispatching work or changing Ticket/delivery state.

    Lock the bus before the data store and hold that snapshot until the projection commits.
    Concurrent synchronizers cannot apply an older bus snapshot after a newer one. The same
    connection is used for deployments whose logical stores share a SQLite file. No schema
    change or historical result inference is needed: legacy rows without a bound Ticket retain
    their status until there is authoritative evidence for a transition.
    """
    with bus_store.session() as bus:
        bus.execute("BEGIN IMMEDIATE")
        try:
            if data_store.sqlite_path == bus_store.sqlite_path:
                summary = _sync(bus, bus)
            else:
                with data_store.session() as data:
                    data.execute("BEGIN IMMEDIATE")
                    try:
                        summary = _sync(data, bus)
                        data.execute("COMMIT")
                    except BaseException:
                        data.execute("ROLLBACK")
                        raise
            bus.execute("COMMIT")
            return summary
        except BaseException:
            bus.execute("ROLLBACK")
            raise


def best_effort_sync(data_store: SQLiteStore, bus_store: SQLiteStore) -> dict[str, Any]:
    """Repair the projection after a transition, leaving durable delivery outcomes intact."""
    try:
        return sync_task_statuses(data_store, bus_store)
    except Exception as exc:
        logger.warning("collection task status synchronization deferred: %s", exc, exc_info=True)
        return {"status": "deferred", "error": str(exc)}


def _sync(data: sqlite3.Connection, bus: sqlite3.Connection) -> dict[str, Any]:
    tasks = data.execute(
        "SELECT task_id, task_ticket_id, status FROM collection_tasks WHERE task_ticket_id IS NOT NULL"
    ).fetchall()
    tickets = {
        str(row["ticket_id"]): str(row["status"])
        for row in bus.execute(
            "SELECT ticket_id, status FROM tickets WHERE ticket_type='COLLECTION_TASK_TICKET'"
        )
    }
    deliveries: dict[str, set[str]] = defaultdict(set)
    # Result/quality/event notifications can carry the same ticket_id, but are not task work.
    for row in bus.execute(
        "SELECT payload_json, status FROM messages WHERE topic='intelligence.collection'"
    ):
        payload = loads_json(row["payload_json"], {})
        if isinstance(payload, dict) and payload.get("ticket_id") in tickets:
            deliveries[str(payload["ticket_id"])].add(str(row["status"]))

    updated = 0
    matched = 0
    for task in tasks:
        ticket_id = str(task["task_ticket_id"])
        if ticket_id not in tickets:
            continue
        matched += 1
        status = _project_status(tickets[ticket_id], deliveries[ticket_id])
        if status != task["status"]:
            data.execute(
                "UPDATE collection_tasks SET status=?, updated_at=datetime('now') WHERE task_id=?",
                (status, task["task_id"]),
            )
            updated += 1
    return {"status": "ok", "matched_tasks": matched, "updated_tasks": updated}


def _project_status(ticket_status: str, delivery_statuses: set[str]) -> str:
    if ticket_status in _TERMINAL_TICKETS:
        return "failed" if ticket_status == "dead" else ticket_status
    # A live delivery takes precedence over an older envelope of the same task. In the normal
    # case there is one idempotent envelope, including after an operator retries it.
    if "in_progress" in delivery_statuses:
        return "in_progress"
    if "open" in delivery_statuses:
        return "open"
    if "dead" in delivery_statuses:
        return "failed"
    if "expired" in delivery_statuses:
        return "expired"
    # An ack only describes transport. It cannot prove usable output or business completion.
    return ticket_status
