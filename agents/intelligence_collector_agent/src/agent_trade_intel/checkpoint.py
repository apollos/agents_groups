from __future__ import annotations

from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .ids import new_id, stable_hash


class CheckpointManager:
    def __init__(self, store: SQLiteStore, agent_id: str):
        self.store = store
        self.agent_id = agent_id

    def save(
        self,
        *,
        session_id: str | None,
        state: str,
        checkpoint: dict[str, Any],
        checkpoint_type: str = "runtime",
        trade_date: str | None = None,
        market_phase: str | None = None,
        current_ticket_id: str | None = None,
        current_task_id: str | None = None,
        open_ticket_ids: list[str] | None = None,
        next_due_tasks: list[str] | None = None,
    ) -> str:
        checkpoint_id = new_id("ckpt")
        checksum = stable_hash(checkpoint, 32)
        with self.store.session() as con:
            con.execute(
                """
                INSERT INTO agent_checkpoints(
                  checkpoint_id, agent_id, session_id, checkpoint_type, state, trade_date,
                  market_phase, current_ticket_id, current_task_id, open_ticket_ids_json,
                  next_due_tasks_json, checkpoint_json, state_checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    self.agent_id,
                    session_id,
                    checkpoint_type,
                    state,
                    trade_date,
                    market_phase,
                    current_ticket_id,
                    current_task_id,
                    dumps_json(open_ticket_ids or []),
                    dumps_json(next_due_tasks or []),
                    dumps_json(checkpoint),
                    checksum,
                ),
            )
        return checkpoint_id

    def latest(self) -> dict[str, Any] | None:
        with self.store.session() as con:
            row = con.execute(
                "SELECT * FROM agent_checkpoints WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
                (self.agent_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["open_ticket_ids"] = loads_json(d.pop("open_ticket_ids_json"), [])
        d["next_due_tasks"] = loads_json(d.pop("next_due_tasks_json"), [])
        d["checkpoint"] = loads_json(d.pop("checkpoint_json"), {})
        return d
