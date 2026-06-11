from __future__ import annotations

from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .ids import new_id


class AgentSessionRepository:
    def __init__(self, store: SQLiteStore, agent_id: str):
        self.store = store
        self.agent_id = agent_id

    def start(self, *, model_ref: str | None, metadata: dict[str, Any] | None = None) -> str:
        session_id = new_id("session")
        with self.store.session() as con:
            con.execute(
                """
                INSERT INTO agent_sessions(session_id, agent_id, model_ref, status, metadata_json)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (session_id, self.agent_id, model_ref, dumps_json(metadata or {})),
            )
        return session_id

    def stop(self, session_id: str, status: str = "stopped") -> None:
        with self.store.session() as con:
            con.execute(
                "UPDATE agent_sessions SET status=?, stopped_at=datetime('now') WHERE session_id=?",
                (status, session_id),
            )

    def latest(self) -> dict[str, Any] | None:
        with self.store.session() as con:
            row = con.execute(
                "SELECT * FROM agent_sessions WHERE agent_id=? ORDER BY started_at DESC LIMIT 1",
                (self.agent_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["metadata"] = loads_json(d.pop("metadata_json"), {})
        return d
