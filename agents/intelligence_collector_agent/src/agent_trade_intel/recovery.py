from __future__ import annotations

from typing import Any

from .db import SQLiteStore, loads_json
from .ids import make_idempotency_key
from .logging_setup import get_logger
from .queue import SQLiteMessageQueue
from .tickets import TicketRepository, priority_to_int

logger = get_logger("recovery")


class RecoveryManager:
    """Crash recovery per design section 14.3.

    1. Requeue expired leases.
    2. Ack open/in_progress messages whose ticket is already done (work landed before crash).
    3. Ensure every dead-letter message has a FAULT_TICKET.
    4. Re-publish messages for open COLLECTION_TASK_TICKETs that lost their delivery message
       (crash between ticket creation and publish).
    """

    def __init__(self, store: SQLiteStore, queue: SQLiteMessageQueue, tickets: TicketRepository, agent_id: str, agent_group: str):
        self.store = store
        self.queue = queue
        self.tickets = tickets
        self.agent_id = agent_id
        self.agent_group = agent_group

    def recover(self) -> dict[str, Any]:
        requeued = self.queue.requeue_expired_leases()
        acked = self._ack_completed()
        faults = self._fault_dead_letters()
        republished = self._republish_orphan_task_tickets()
        summary = {
            "status": "ok",
            "requeued_expired_leases": requeued,
            "acked_completed": acked,
            "fault_tickets_for_dead_letters": faults,
            "republished_orphan_tickets": republished,
        }
        logger.info("recovery complete: %s", summary)
        return summary

    def _ack_completed(self) -> list[str]:
        acked: list[str] = []
        with self.store.session() as con:
            rows = con.execute(
                "SELECT message_id, payload_json FROM messages WHERE status IN ('open', 'in_progress')"
            ).fetchall()
        for row in rows:
            payload = loads_json(row["payload_json"], {})
            ticket_id = payload.get("ticket_id")
            if not ticket_id:
                continue
            ticket = self.tickets.get(ticket_id)
            if ticket and ticket.get("status") == "done":
                self.queue.ack(str(row["message_id"]))
                acked.append(str(row["message_id"]))
        return acked

    def _fault_dead_letters(self) -> list[str]:
        created: list[str] = []
        with self.store.session() as con:
            rows = con.execute("SELECT * FROM messages WHERE status='dead'").fetchall()
        for row in rows:
            message_id = str(row["message_id"])
            error = loads_json(row["error_json"], {}) or {"error_code": "DEAD_LETTER"}
            ticket_id = self.tickets.create_ticket(
                ticket_type="FAULT_TICKET",
                source_agent=self.agent_id,
                target_agent_group="runtime_controller",
                priority="urgent",
                correlation_id=row["correlation_id"],
                summary_cn=f"消息 {message_id} 进入 dead letter，需要人工或 Runtime Controller 处理。",
                payload={"message_id": message_id, "topic": row["topic"], "error": error},
                idempotency_key=make_idempotency_key("fault", "dead_letter", message_id),
            )
            created.append(ticket_id)
        return created

    def _republish_orphan_task_tickets(self) -> list[str]:
        republished: list[str] = []
        with self.store.session() as con:
            rows = con.execute(
                """
                SELECT t.ticket_id, t.ticket_type, t.priority, t.correlation_id
                FROM tickets t
                WHERE t.ticket_type IN ('COLLECTION_TASK_TICKET', 'COLLECTION_REQUEST_TICKET')
                  AND t.status IN ('open', 'in_progress')
                """
            ).fetchall()
        for row in rows:
            ticket_id = str(row["ticket_id"])
            kind = "collection_task" if row["ticket_type"] == "COLLECTION_TASK_TICKET" else "collection_request"
            # publish() dedupes on idempotency_key, so this is a no-op when the message exists.
            msg_id = self.queue.publish(
                "intelligence.collection",
                {"ticket_id": ticket_id, "ticket_type": row["ticket_type"]},
                priority=priority_to_int(str(row["priority"] or "normal")),
                correlation_id=row["correlation_id"],
                idempotency_key=make_idempotency_key("message", kind, ticket_id),
                target_agent_id=self.agent_id,
                target_agent_group=self.agent_group,
            )
            republished.append(msg_id)
        return republished
