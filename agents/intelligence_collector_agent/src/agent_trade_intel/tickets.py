from __future__ import annotations

from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .ids import new_id, stable_hash, utc_now_iso


class TicketRepository:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def create_ticket(
        self,
        *,
        ticket_type: str,
        source_agent: str,
        target_agent_group: str | None = None,
        target_agent_id: str | None = None,
        priority: str = "normal",
        status: str = "open",
        parent_ticket_id: str | None = None,
        correlation_id: str | None = None,
        related_tickers: list[str] | None = None,
        summary_cn: str = "",
        payload: dict[str, Any] | None = None,
        payload_ref: str | None = None,
        evidence_refs: list[str] | None = None,
        expires_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        payload = payload or {}
        audit_hash = stable_hash(
            {
                "ticket_type": ticket_type,
                "parent_ticket_id": parent_ticket_id,
                "correlation_id": correlation_id,
                "summary_cn": summary_cn,
                "payload": payload,
                "evidence_refs": evidence_refs or [],
            },
            32,
        )
        with self.store.session() as con:
            if idempotency_key:
                row = con.execute(
                    "SELECT ticket_id FROM tickets WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return str(row["ticket_id"])
            ticket_id = new_id("ticket")
            con.execute(
                """
                INSERT INTO tickets(
                  ticket_id, ticket_type, parent_ticket_id, correlation_id, priority,
                  status, expires_at, source_agent, target_agent_group, target_agent_id,
                  related_tickers_json, summary_cn, payload_ref, payload_json,
                  evidence_refs_json, idempotency_key, audit_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    ticket_type,
                    parent_ticket_id,
                    correlation_id,
                    priority,
                    status,
                    expires_at,
                    source_agent,
                    target_agent_group,
                    target_agent_id,
                    dumps_json(related_tickers or []),
                    summary_cn,
                    payload_ref,
                    dumps_json(payload),
                    dumps_json(evidence_refs or []),
                    idempotency_key,
                    audit_hash,
                ),
            )
            self._append_event(con, ticket_id, "created", None, status, "ticket created", {})
        return ticket_id

    def update_status(self, ticket_id: str, status: str, message: str = "", payload: dict[str, Any] | None = None) -> None:
        with self.store.session() as con:
            row = con.execute("SELECT status FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            old_status = row["status"] if row else None
            con.execute(
                "UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=?",
                (status, ticket_id),
            )
            self._append_event(con, ticket_id, "status_changed", old_status, status, message, payload or {})

    def get(self, ticket_id: str) -> dict[str, Any] | None:
        with self.store.session() as con:
            row = con.execute("SELECT * FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list(self, *, ticket_type: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM tickets WHERE 1=1"
        params: list[Any] = []
        if ticket_type:
            query += " AND ticket_type=?"
            params.append(ticket_type)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store.session() as con:
            rows = con.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def by_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        with self.store.session() as con:
            rows = con.execute(
                "SELECT * FROM tickets WHERE correlation_id=? ORDER BY created_at ASC",
                (correlation_id,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def _append_event(
        self,
        con,
        ticket_id: str,
        event_type: str,
        old_status: str | None,
        new_status: str | None,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        con.execute(
            """
            INSERT INTO ticket_events(event_id, ticket_id, event_type, old_status, new_status, message, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("tevt"), ticket_id, event_type, old_status, new_status, message, dumps_json(payload)),
        )

    def _row_to_dict(self, row) -> dict[str, Any]:
        d = dict(row)
        d["related_tickers"] = loads_json(d.pop("related_tickers_json"), [])
        d["payload"] = loads_json(d.pop("payload_json"), {})
        d["evidence_refs"] = loads_json(d.pop("evidence_refs_json"), [])
        return d


def topic_for_ticket_type(ticket_type: str) -> str:
    if ticket_type in {"COLLECTION_REQUEST_TICKET", "COLLECTION_TASK_TICKET"}:
        return "intelligence.collection"
    if ticket_type in {"EVENT_TICKET", "MARKET_FEATURE_TICKET", "ANALYSIS_REQUEST_TICKET"}:
        return "intelligence.events"
    if ticket_type in {"DATA_QUALITY_TICKET", "FAULT_TICKET"}:
        return "intelligence.quality"
    if ticket_type in {"COLLECTION_REPORT_TICKET"}:
        return "intelligence.reports"
    return "tickets"


PRIORITY_INT = {"low": 0, "normal": 10, "high": 20, "urgent": 30}


def priority_to_int(priority: str) -> int:
    return PRIORITY_INT.get(priority, 10)
