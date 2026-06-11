from __future__ import annotations

from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .ids import new_id


class AgentMemory:
    """Agent-scoped durable memory.

    This class deliberately requires agent_id on construction so that this OpenClaw agent does
    not read or write another agent's memory by accident.
    """

    def __init__(self, store: SQLiteStore, agent_id: str):
        self.store = store
        self.agent_id = agent_id

    def add(
        self,
        *,
        memory_type: str,
        content_cn: str,
        source_ticket_ids: list[str] | None = None,
        validity_condition: str | None = None,
        confidence: float | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        memory_id = new_id("mem")
        with self.store.session() as con:
            con.execute(
                """
                INSERT INTO agent_memories(
                  memory_id, agent_id, memory_type, content_cn, source_ticket_ids_json,
                  validity_condition, confidence, tags_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    self.agent_id,
                    memory_type,
                    content_cn,
                    dumps_json(source_ticket_ids or []),
                    validity_condition,
                    confidence,
                    dumps_json(tags or []),
                    dumps_json(metadata or {}),
                ),
            )
        return memory_id

    def search(self, query: str = "", memory_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agent_memories WHERE agent_id=?"
        params: list[Any] = [self.agent_id]
        if memory_type:
            sql += " AND memory_type=?"
            params.append(memory_type)
        if query:
            sql += " AND content_cn LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.store.session() as con:
            rows = con.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["source_ticket_ids"] = loads_json(d.pop("source_ticket_ids_json"), [])
            d["tags"] = loads_json(d.pop("tags_json"), [])
            d["metadata"] = loads_json(d.pop("metadata_json"), {})
            out.append(d)
        return out
