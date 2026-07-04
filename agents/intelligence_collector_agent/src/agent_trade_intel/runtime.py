from __future__ import annotations

from typing import Any

from .config import CollectorConfig
from .db import SQLiteStore
from .demand import DemandCompiler, DemandRegistry
from .errors import QueueEmpty
from .ids import make_idempotency_key, new_id
from .logging_setup import get_logger
from .queue import SQLiteMessageQueue
from .tickets import TicketRepository, priority_to_int
from .time_utils import market_phase, parse_dt

logger = get_logger("runtime")

DEMAND_TOPICS = ["demand.registered", "demand.changed"]


class RuntimeController:
    """First-edition Runtime Controller.

    tick() consumes demand.registered / demand.changed messages, compiles active Demands into
    COLLECTION_REQUEST_TICKETs + messages, cancels open work for suspended/cancelled Demands and
    schedules pre-market capability validation.
    """

    def __init__(
        self,
        config: CollectorConfig,
        *,
        state_store: SQLiteStore,
        bus_store: SQLiteStore,
        data_store: SQLiteStore,
    ):
        self.config = config
        self.state_store = state_store
        self.bus_store = bus_store
        self.data_store = data_store
        self.queue = SQLiteMessageQueue(bus_store)
        self.tickets = TicketRepository(bus_store)
        self.registry = DemandRegistry(data_store, self.queue)
        self.compiler = DemandCompiler(
            data_store, self.queue, self.tickets, config.runtime.agent_id, config.runtime.agent_group
        )

    def tick(
        self,
        *,
        now: str,
        phase: str | None = None,
        run_capability_validation: bool = False,
    ) -> dict[str, Any]:
        resolved_phase = phase or market_phase(parse_dt(now, self.config.runtime.timezone), self.config.raw)
        expired = self.queue.expire_messages()
        requeued = self.queue.requeue_expired_leases()
        demand_events = self._consume_demand_messages()
        created = self.compiler.compile_active_demands(self.registry, as_of=now, market_phase=resolved_phase)
        capability = self._maybe_schedule_capability_check(now=now, phase=resolved_phase, force=run_capability_validation)
        return {
            "status": "ok",
            "market_phase": resolved_phase,
            "created": created,
            "demand_events_consumed": demand_events,
            "capability_check": capability,
            "expired_messages": expired,
            "requeued_expired_leases": requeued,
        }

    def _consume_demand_messages(self, *, max_messages: int = 100) -> list[dict[str, Any]]:
        consumed: list[dict[str, Any]] = []
        worker_id = f"runtime_controller:{new_id('worker')}"
        for _ in range(max_messages):
            try:
                msg = self.queue.lease(topics=DEMAND_TOPICS, worker_id=worker_id, lease_seconds=60)
            except QueueEmpty:
                break
            try:
                if msg.topic == "demand.changed" and msg.payload.get("new_status") in {"suspended", "cancelled"}:
                    cancelled = self._cancel_open_work(str(msg.payload.get("demand_id")))
                    consumed.append({"topic": msg.topic, "payload": msg.payload, "cancelled": cancelled})
                else:
                    consumed.append({"topic": msg.topic, "payload": msg.payload})
                self.queue.ack(msg.message_id)
            except Exception as exc:  # noqa: BLE001 - runtime must not crash on one bad message
                logger.exception("failed to process demand message %s", msg.message_id)
                self.queue.nack(msg.message_id, {"error_code": "RUNTIME_DEMAND_MESSAGE_FAILED", "error_message": str(exc)})
        return consumed

    def _cancel_open_work(self, demand_id: str) -> dict[str, list[str]]:
        """Cancel open request/task Tickets of a suspended/cancelled Demand and ack their messages."""
        needle = f'"demand_id":"{demand_id}"'
        with self.bus_store.session() as con:
            rows = con.execute(
                """
                SELECT ticket_id FROM tickets
                WHERE ticket_type IN ('COLLECTION_REQUEST_TICKET', 'COLLECTION_TASK_TICKET')
                  AND status IN ('open', 'in_progress')
                  AND payload_json LIKE ?
                """,
                (f"%{needle}%",),
            ).fetchall()
        cancelled_tickets = [str(r["ticket_id"]) for r in rows]
        for ticket_id in cancelled_tickets:
            self.tickets.update_status(ticket_id, "cancelled", f"demand {demand_id} suspended/cancelled")
        acked_messages: list[str] = []
        if cancelled_tickets:
            with self.bus_store.session() as con:
                msg_rows = con.execute("SELECT message_id, payload_json FROM messages WHERE status='open'").fetchall()
            for row in msg_rows:
                if any(ticket_id in str(row["payload_json"]) for ticket_id in cancelled_tickets):
                    self.queue.ack(str(row["message_id"]))
                    acked_messages.append(str(row["message_id"]))
        logger.info(
            "cancelled %s tickets / acked %s messages for demand %s",
            len(cancelled_tickets),
            len(acked_messages),
            demand_id,
        )
        return {"tickets": cancelled_tickets, "messages": acked_messages}

    def _maybe_schedule_capability_check(self, *, now: str, phase: str, force: bool) -> dict[str, Any] | None:
        cap_cfg = self.config.get("capability_verification", {}) or {}
        should_run = force or (bool(cap_cfg.get("run_pre_market", False)) and phase == "pre_market" and not self._has_capability_check_today(now))
        if not should_run:
            return None
        date_part = now[:10]
        task = {
            "task_id": new_id("task"),
            "task_type": "tool_capability_check",
            "tool_name": "stock_data_collector",
            "as_of": now,
            "idempotency_key": make_idempotency_key("collection_task", "capability_check", date_part),
        }
        ticket_id = self.tickets.create_ticket(
            ticket_type="COLLECTION_TASK_TICKET",
            source_agent="runtime_controller",
            target_agent_group=self.config.runtime.agent_group,
            target_agent_id=self.config.runtime.agent_id,
            priority="high",
            summary_cn=f"{date_part} 盘前工具能力验证任务。",
            payload=task,
            idempotency_key=task["idempotency_key"],
        )
        message_id = self.queue.publish(
            "intelligence.collection",
            {"ticket_id": ticket_id, "ticket_type": "COLLECTION_TASK_TICKET"},
            priority=priority_to_int("high"),
            idempotency_key=make_idempotency_key("message", "capability_check", ticket_id),
            target_agent_id=self.config.runtime.agent_id,
            target_agent_group=self.config.runtime.agent_group,
        )
        return {"ticket_id": ticket_id, "message_id": message_id}

    def _has_capability_check_today(self, now: str) -> bool:
        with self.state_store.session() as con:
            row = con.execute(
                "SELECT capability_id FROM tool_capabilities WHERE tool_name='stock_data_collector' AND date(checked_at)=date(?) LIMIT 1",
                (now[:10],),
            ).fetchone()
        return row is not None
