"""Publish durable collection result receipts without repeating collection work."""

from __future__ import annotations

from .db import SQLiteStore, loads_json
from .ids import make_idempotency_key
from .queue import SQLiteMessageQueue
from .tickets import priority_to_int


def publish_ready_results(
    bus_store: SQLiteStore, queue: SQLiteMessageQueue, ticket_id: str | None = None,
) -> list[str]:
    """Deliver the latest explicit receipt of each terminal collection task, idempotently.

    Ticket completion and its ``collection_result_ready`` event are committed together. A bus
    publication failure can then be repaired using the exact recorded payload. Older terminal
    tickets without a receipt do not acquire an inferred business outcome. Each receipt owns
    its delivery key, so an operator's later retry may publish a new outcome for the same task.
    Receipt selection and message insertion share the bus transaction so a concurrent operator
    cannot reopen the task between validation and publication.
    """
    query = """
        SELECT t.ticket_id, t.correlation_id, e.event_id, e.payload_json
        FROM tickets t
        JOIN ticket_events e ON e.ticket_id=t.ticket_id
        WHERE t.ticket_type='COLLECTION_TASK_TICKET'
          AND t.status IN ('done', 'failed', 'cancelled', 'expired', 'dead')
          AND e.event_type='collection_result_ready'
          AND e.new_status=t.status
          AND e.rowid=(
              SELECT MAX(latest.rowid) FROM ticket_events latest
              WHERE latest.ticket_id=t.ticket_id AND latest.event_type='collection_result_ready'
          )
          AND e.rowid>COALESCE((
              SELECT MAX(transition.rowid) FROM ticket_events transition
              WHERE transition.ticket_id=t.ticket_id AND transition.event_type='status_changed'
          ), 0)
    """
    params = ()
    if ticket_id is not None:
        query += " AND t.ticket_id=?"
        params = (ticket_id,)
    query += " ORDER BY e.rowid"
    published: list[str] = []
    with bus_store.session() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            receipts = con.execute(query, params).fetchall()
            for receipt in receipts:
                payload = loads_json(receipt["payload_json"], {})
                if not isinstance(payload, dict) or payload.get("ticket_id") != receipt["ticket_id"]:
                    raise ValueError(f"invalid collection result receipt {receipt['event_id']}")
                published.append(queue.publish(
                    "collection.result", payload,
                    priority=priority_to_int("normal"), correlation_id=receipt["correlation_id"],
                    idempotency_key=make_idempotency_key("message", "collection_result", receipt["event_id"]),
                    _connection=con,
                ))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return published
