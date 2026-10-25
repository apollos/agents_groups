from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .errors import QueueEmpty
from .ids import new_id, utc_now_iso
from .logging_setup import get_logger
from .work_guard import TERMINAL_TICKET_STATES, owns_lease, require_work

logger = get_logger("queue")


@dataclass
class QueueMessage:
    message_id: str
    topic: str
    payload: dict[str, Any]
    priority: int
    status: str
    correlation_id: str | None = None
    target_agent_id: str | None = None
    target_agent_group: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    lease_generation: int = 0


class MessageQueue(ABC):
    """Abstract durable queue interface.

    Business code must depend on this interface only, so the SQLite implementation can later be
    swapped for Redis Streams / RabbitMQ / Kafka without touching agent logic.
    """

    @abstractmethod
    def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        priority: int = 0,
        available_at: str | None = None,
        expires_at: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        target_agent_id: str | None = None,
        target_agent_group: str | None = None,
        max_attempts: int = 3,
    ) -> str: ...

    @abstractmethod
    def lease(
        self,
        *,
        topics: list[str],
        worker_id: str,
        lease_seconds: int = 300,
        target_agent_id: str | None = None,
        target_agent_group: str | None = None,
    ) -> QueueMessage: ...

    @abstractmethod
    def ack(self, message_id: str, *, worker_id: str | None = None, lease_attempt: int | None = None, lease_generation: int | None = None) -> bool: ...

    @abstractmethod
    def nack(self, message_id: str, error: dict[str, Any], *, retryable: bool = True, retry_delay_seconds: int = 60,
             worker_id: str | None = None, lease_attempt: int | None = None, lease_generation: int | None = None) -> str | None: ...

    @abstractmethod
    def extend_lease(self, message_id: str, worker_id: str, lease_seconds: int, *, lease_attempt: int | None = None, lease_generation: int | None = None) -> bool:
        """Extend the lease; return False when this worker no longer owns the message."""
        ...

    @abstractmethod
    def move_to_dead_letter(self, message_id: str, reason: str) -> None: ...

    @abstractmethod
    def requeue_expired_leases(self) -> int: ...


class SQLiteMessageQueue(MessageQueue):
    """Small reliable message queue implemented on SQLite.

    This is intentionally separate from Ticket storage. A Ticket is the business record; a Message
    is the delivery envelope used by OpenClaw/runtime workers.

    Status machine: open -> in_progress -> done | open (retry) | dead.
    """

    def __init__(self, store: SQLiteStore):
        self.store = store
        self.active_lease: tuple[str, str, int, int] | None = None

    def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        priority: int = 0,
        available_at: str | None = None,
        expires_at: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
        target_agent_id: str | None = None,
        target_agent_group: str | None = None,
        max_attempts: int = 3,
        _connection: sqlite3.Connection | None = None,
    ) -> str:
        message_id = new_id("msg")
        with (self.store.session() if _connection is None else nullcontext(_connection)) as con:
            owns_transaction = not con.in_transaction
            savepoint = new_id("sp_publish") if not owns_transaction else None
            con.execute("BEGIN IMMEDIATE" if owns_transaction else f"SAVEPOINT {savepoint}")
            finish = "COMMIT" if owns_transaction else f"RELEASE SAVEPOINT {savepoint}"
            try:
                if self.active_lease is not None and topic == "intelligence.collection":
                    require_work(con, self.active_lease)
                if idempotency_key:
                    row = con.execute(
                        "SELECT message_id FROM messages WHERE idempotency_key=?",
                        (idempotency_key,),
                    ).fetchone()
                    if row:
                        con.execute(finish)
                        return str(row["message_id"])
                con.execute(
                    """
                    INSERT INTO messages(
                      message_id, topic, status, priority, payload_json, correlation_id,
                      idempotency_key, target_agent_id, target_agent_group, available_at,
                      expires_at, max_attempts
                    ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        topic,
                        priority,
                        dumps_json(payload),
                        correlation_id,
                        idempotency_key,
                        target_agent_id,
                        target_agent_group,
                        available_at or utc_now_iso(),
                        expires_at,
                        max_attempts,
                    ),
                )
                con.execute(finish)
            except BaseException:
                if con.in_transaction:
                    if owns_transaction:
                        con.execute("ROLLBACK")
                    else:
                        con.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        con.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
        logger.info("published message %s topic=%s priority=%s", message_id, topic, priority)
        return message_id

    def lease(
        self,
        *,
        topics: list[str],
        worker_id: str,
        lease_seconds: int = 300,
        target_agent_id: str | None = None,
        target_agent_group: str | None = None,
    ) -> QueueMessage:
        if not topics:
            raise ValueError("topics cannot be empty")
        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        placeholders = ",".join("?" for _ in topics)
        params: list[Any] = list(topics)
        target_clause = ""
        if target_agent_id or target_agent_group:
            # A message addressed to a specific agent_id may only be consumed by that agent.
            # A message addressed to a group only (agent_id NULL) may be consumed by any group member.
            # An unaddressed message may be consumed by anyone.
            target_clause = """
              AND (
                target_agent_id=?
                OR (target_agent_id IS NULL AND target_agent_group=?)
                OR (target_agent_id IS NULL AND target_agent_group IS NULL)
              )
            """
            params.extend([target_agent_id, target_agent_group])

        with self.store.session() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute(
                    f"""
                    SELECT * FROM messages
                    WHERE topic IN ({placeholders})
                      AND status='open'
                      AND datetime(available_at) <= datetime('now')
                      AND (expires_at IS NULL OR datetime(expires_at) > datetime('now'))
                      {target_clause}
                    ORDER BY priority DESC, datetime(created_at) ASC
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
                if row is None:
                    con.execute("COMMIT")
                    raise QueueEmpty("no open message available")
                con.execute(
                    """
                    UPDATE messages
                    SET status='in_progress', lease_owner=?, lease_until=?, attempts=attempts+1,
                        lease_generation=lease_generation+1,
                        updated_at=datetime('now')
                    WHERE message_id=?
                    """,
                    (worker_id, lease_until, row["message_id"]),
                )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
        logger.info("leased message %s by %s until %s", row["message_id"], worker_id, lease_until)
        return QueueMessage(
            message_id=str(row["message_id"]),
            topic=str(row["topic"]),
            payload=loads_json(row["payload_json"], {}),
            priority=int(row["priority"]),
            status="in_progress",
            correlation_id=row["correlation_id"],
            target_agent_id=row["target_agent_id"],
            target_agent_group=row["target_agent_group"],
            attempts=int(row["attempts"]) + 1,
            max_attempts=int(row["max_attempts"]),
            lease_generation=int(row["lease_generation"]) + 1,
        )

    def ack(self, message_id: str, *, worker_id: str | None = None, lease_attempt: int | None = None, lease_generation: int | None = None) -> bool:
        with self.store.session() as con:
            con.execute("BEGIN IMMEDIATE")
            if worker_id is not None and not owns_lease(con, message_id, worker_id, lease_attempt, lease_generation):
                con.execute("COMMIT")
                return False
            cur = con.execute(
                """
                UPDATE messages
                SET status='done', lease_owner=NULL, lease_until=NULL, updated_at=datetime('now')
                WHERE message_id=?
                """,
                (message_id,),
            )
            con.execute("COMMIT")
        logger.info("acked message %s", message_id)
        return bool(cur.rowcount)

    def nack(self, message_id: str, error: dict[str, Any], *, retryable: bool = True, retry_delay_seconds: int = 60,
             worker_id: str | None = None, lease_attempt: int | None = None, lease_generation: int | None = None) -> str | None:
        with self.store.session() as con:
            con.execute("BEGIN IMMEDIATE")
            if worker_id is not None and not owns_lease(con, message_id, worker_id, lease_attempt, lease_generation):
                con.execute("COMMIT")
                return None
            row = con.execute(
                "SELECT attempts, max_attempts FROM messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if not retryable or attempts >= max_attempts:
                new_status = "dead"
                available_at = utc_now_iso()
            else:
                new_status = "open"
                available_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds)).isoformat(
                    timespec="seconds"
                )
            con.execute(
                """
                UPDATE messages
                SET status=?, lease_owner=NULL, lease_until=NULL, available_at=?,
                    error_json=?, updated_at=datetime('now')
                WHERE message_id=?
                """,
                (new_status, available_at, dumps_json(error), message_id),
            )
            self._sync_delivery_ticket(con, message_id, new_status)
            con.execute("COMMIT")
        logger.warning("nacked message %s -> %s (attempts=%s/%s)", message_id, new_status, attempts, max_attempts)
        return new_status

    @staticmethod
    def _sync_delivery_ticket(con, message_id: str, delivery_status: str, *, operator_retry: bool = False) -> None:
        """Delivery exhaustion is terminal even when the handler requested a retry."""
        row = con.execute("SELECT topic, payload_json FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if not row or row["topic"] != "intelligence.collection":
            return
        payload = loads_json(row["payload_json"], {})
        ticket_id = payload.get("ticket_id") if isinstance(payload, dict) else None
        ticket = con.execute("SELECT status, ticket_type FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        if not ticket or ticket["ticket_type"] not in {"COLLECTION_TASK_TICKET", "COLLECTION_REQUEST_TICKET"}:
            return
        if ticket["status"] in TERMINAL_TICKET_STATES and not (operator_retry and ticket["status"] in {"failed", "dead"}):
            return
        status = {"dead": "failed", "expired": "expired", "open": "open"}.get(delivery_status)
        if status is None or status == ticket["status"]:
            return
        con.execute("UPDATE tickets SET status=?, updated_at=datetime('now') WHERE ticket_id=?", (status, ticket_id))
        con.execute(
            """INSERT INTO ticket_events(event_id,ticket_id,event_type,old_status,new_status,message,payload_json)
               VALUES (?,?,'status_changed',?,?,?,?)""",
            (new_id("tevt"), ticket_id, ticket["status"], status, "delivery " + delivery_status,
             dumps_json({"message_id": message_id})),
        )

    # Kept for backwards compatibility with earlier code paths.
    def fail(self, message_id: str, error: dict[str, Any], *, retry_delay_seconds: int = 60) -> None:
        self.nack(message_id, error, retryable=True, retry_delay_seconds=retry_delay_seconds)

    def extend_lease(self, message_id: str, worker_id: str, lease_seconds: int, *, lease_attempt: int | None = None, lease_generation: int | None = None) -> bool:
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self.store.session() as con:
            cur = con.execute(
                """
                UPDATE messages
                SET lease_until=?, updated_at=datetime('now')
                WHERE message_id=? AND status='in_progress' AND lease_owner=?
                  AND datetime(lease_until)>datetime('now')
                  AND (? IS NULL OR attempts=?)
                  AND (? IS NULL OR lease_generation=?)
                """,
                (lease_until, message_id, worker_id, lease_attempt, lease_attempt, lease_generation, lease_generation),
            )
        if cur.rowcount:
            logger.debug("extended lease of %s to %s", message_id, lease_until)
            return True
        logger.warning("lease of %s is no longer owned by %s (expired or re-leased)", message_id, worker_id)
        return False

    def move_to_dead_letter(self, message_id: str, reason: str) -> None:
        with self.store.session() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """
                UPDATE messages
                SET status='dead', lease_owner=NULL, lease_until=NULL,
                    error_json=?, updated_at=datetime('now')
                WHERE message_id=?
                """,
                (dumps_json({"error_code": "DEAD_LETTER", "error_message": reason}), message_id),
            )
            self._sync_delivery_ticket(con, message_id, "dead")
            con.execute("COMMIT")
        logger.warning("moved message %s to dead letter: %s", message_id, reason)

    def retry_message(self, message_id: str) -> bool:
        """Requeue a dead/failed message (CLI operator action)."""
        with self.store.session() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT topic, payload_json FROM messages WHERE message_id=?", (message_id,)).fetchone()
            payload = loads_json(row["payload_json"], {}) if row and row["topic"] == "intelligence.collection" else {}
            ticket_id = payload.get("ticket_id") if isinstance(payload, dict) else None
            ticket = con.execute("SELECT status, ticket_type FROM tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
            if (ticket and ticket["ticket_type"] in {"COLLECTION_REQUEST_TICKET", "COLLECTION_TASK_TICKET"}
                    and ticket["status"] in TERMINAL_TICKET_STATES - {"failed", "dead"}):
                con.execute("COMMIT")
                return False
            cur = con.execute(
                """
                UPDATE messages
                SET status='open', attempts=0, lease_owner=NULL, lease_until=NULL,
                    available_at=datetime('now'), updated_at=datetime('now')
                WHERE message_id=? AND status IN ('dead', 'open', 'in_progress')
                """,
                (message_id,),
            )
            if cur.rowcount:
                self._sync_delivery_ticket(con, message_id, "open", operator_retry=True)
            con.execute("COMMIT")
        retried = bool(cur.rowcount)
        if retried:
            logger.info("operator retried message %s", message_id)
        return retried

    def expire_messages(self) -> int:
        """Mark open messages whose expires_at has passed as expired (design §5.4)."""
        with self.store.session() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("""SELECT message_id FROM messages WHERE status='open'
                AND expires_at IS NOT NULL AND datetime(expires_at)<=datetime('now')""").fetchall()
            cur = con.execute(
                """
                UPDATE messages
                SET status='expired', updated_at=datetime('now')
                WHERE status='open'
                  AND expires_at IS NOT NULL
                  AND datetime(expires_at) <= datetime('now')
                """
            )
            count = int(cur.rowcount or 0)
            for row in rows:
                self._sync_delivery_ticket(con, row["message_id"], "expired")
            con.execute("COMMIT")
        if count:
            logger.info("expired %s stale open messages", count)
        return count

    def requeue_expired_leases(self) -> int:
        with self.store.session() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("""SELECT message_id, attempts, max_attempts FROM messages
                WHERE status='in_progress' AND lease_until IS NOT NULL
                AND datetime(lease_until)<=datetime('now')""").fetchall()
            dead_cur = con.execute(
                """
                UPDATE messages
                SET status='dead', lease_owner=NULL, lease_until=NULL,
                    error_json='{"error_code":"LEASE_EXPIRED_MAX_ATTEMPTS","error_message":"lease expired after max attempts"}',
                    updated_at=datetime('now')
                WHERE status='in_progress'
                  AND lease_until IS NOT NULL
                  AND datetime(lease_until) <= datetime('now')
                  AND attempts >= max_attempts
                """
            )
            open_cur = con.execute(
                """
                UPDATE messages
                SET status='open', lease_owner=NULL, lease_until=NULL, updated_at=datetime('now')
                WHERE status='in_progress'
                  AND lease_until IS NOT NULL
                  AND datetime(lease_until) <= datetime('now')
                  AND attempts < max_attempts
                """
            )
            count = int((dead_cur.rowcount or 0) + (open_cur.rowcount or 0))
            for row in rows:
                status = "dead" if row["attempts"] >= row["max_attempts"] else "open"
                self._sync_delivery_ticket(con, row["message_id"], status)
            con.execute("COMMIT")
        if count:
            logger.warning("requeued/dead-lettered %s expired leases", count)
        return count

    def inspect(self, message_id: str) -> dict[str, Any] | None:
        with self.store.session() as con:
            row = con.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if row is None:
            return None
        return dict(row) | {"payload": loads_json(row["payload_json"], {}), "error": loads_json(row["error_json"], None)}

    def list_messages(self, status: str | None = None, limit: int = 100, topic: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM messages WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status=?"
            params.append(status)
        if topic:
            query += " AND topic=?"
            params.append(topic)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store.session() as con:
            rows = con.execute(query, params).fetchall()
        return [dict(row) | {"payload": loads_json(row["payload_json"], {})} for row in rows]

    def depth_by_status(self) -> dict[str, int]:
        with self.store.session() as con:
            rows = con.execute("SELECT status, COUNT(*) c FROM messages GROUP BY status").fetchall()
        return {str(r["status"]): int(r["c"]) for r in rows}
