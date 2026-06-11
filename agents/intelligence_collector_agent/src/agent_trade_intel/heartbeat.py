from __future__ import annotations

from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .ids import new_id


class HeartbeatRecorder:
    """Durable heartbeat trail so Runtime Controller can detect dead workers."""

    def __init__(self, store: SQLiteStore, agent_id: str, *, retention_days: int = 7):
        self.store = store
        self.agent_id = agent_id
        self.retention_days = retention_days

    def beat(
        self,
        *,
        state: str,
        worker_id: str | None = None,
        session_id: str | None = None,
        message_id: str | None = None,
        ticket_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        heartbeat_id = new_id("hb")
        with self.store.session() as con:
            con.execute(
                """
                INSERT INTO runtime_heartbeats(
                  heartbeat_id, agent_id, worker_id, session_id, state, message_id, ticket_id, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (heartbeat_id, self.agent_id, worker_id, session_id, state, message_id, ticket_id, dumps_json(details or {})),
            )
            con.execute(
                "DELETE FROM runtime_heartbeats WHERE agent_id=? AND created_at < datetime('now', ?)",
                (self.agent_id, f"-{self.retention_days} days"),
            )
        return heartbeat_id

    def latest(self, limit: int = 1) -> list[dict[str, Any]]:
        with self.store.session() as con:
            rows = con.execute(
                "SELECT * FROM runtime_heartbeats WHERE agent_id=? ORDER BY created_at DESC LIMIT ?",
                (self.agent_id, limit),
            ).fetchall()
        return [dict(r) | {"details": loads_json(r["details_json"], {})} for r in rows]
