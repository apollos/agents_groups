"""Fence a worker's writes against the current durable delivery lease."""
from __future__ import annotations

from contextlib import contextmanager

from .db import loads_json
from .errors import AgentTradeIntelError


TERMINAL_TICKET_STATES = frozenset({"done", "failed", "cancelled", "expired", "dead"})


class WorkRevoked(AgentTradeIntelError):
    """The delivery lease or its business work is no longer owned by this worker."""


def owns_lease(con, message_id: str, worker_id: str, attempt: int | None = None,
               generation: int | None = None) -> bool:
    row = con.execute(
        """SELECT attempts, lease_generation FROM messages WHERE message_id=? AND status='in_progress'
           AND lease_owner=? AND datetime(lease_until)>datetime('now')""",
        (message_id, worker_id),
    ).fetchone()
    return (row is not None and (attempt is None or int(row["attempts"]) == attempt)
            and (generation is None or int(row["lease_generation"]) == generation))


def require_work(con, lease: tuple[str, str, int, int], ticket_id: str | None = None) -> None:
    message_id, worker_id, attempt, generation = lease
    if not owns_lease(con, message_id, worker_id, attempt, generation):
        raise WorkRevoked("delivery lease is no longer owned")
    row = con.execute("SELECT payload_json FROM messages WHERE message_id=?", (message_id,)).fetchone()
    actual_ticket_id = loads_json(row["payload_json"], {}).get("ticket_id")
    if ticket_id is not None and actual_ticket_id != ticket_id:
        raise WorkRevoked("delivery does not own this ticket")
    ticket = con.execute("SELECT status FROM tickets WHERE ticket_id=?", (actual_ticket_id,)).fetchone()
    if ticket is None or ticket["status"] in TERMINAL_TICKET_STATES:
        raise WorkRevoked("ticket is already terminal or missing")


@contextmanager
def result_write_guard(bus_store, lease):
    """Serialize local result writes with cancellation/lease changes; never hold across I/O."""
    with bus_store.session() as con:
        outer = not con.in_transaction
        if outer:
            con.execute("BEGIN IMMEDIATE")
        try:
            require_work(con, lease)
            with bus_store.reuse_connection(con):
                yield
            if outer:
                con.execute("COMMIT")
        except BaseException:
            if outer:
                con.execute("ROLLBACK")
            raise
