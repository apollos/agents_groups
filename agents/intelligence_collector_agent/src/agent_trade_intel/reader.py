from __future__ import annotations

from typing import Any

from .db import SQLiteStore, loads_json
from .tickets import TicketRepository


class IntelligenceReader:
    """Internal CLI/test reader.

    Multi-agent service mode should prefer message-based queries. This reader is intended for CLI,
    debugging and tests. It reads the split stores explicitly: data for collection outputs, bus for
    Ticket chains and state for private runtime/capability state.
    """

    def __init__(self, data_store: SQLiteStore, bus_store: SQLiteStore | None = None, state_store: SQLiteStore | None = None):
        self.data_store = data_store
        self.bus_store = bus_store or data_store
        self.state_store = state_store or data_store
        self.tickets = TicketRepository(self.bus_store)

    def read_recent_events(self, *, target_id: str | None = None, ticker: str | None = None, limit: int = 50) -> dict[str, Any]:
        sql = "SELECT * FROM structured_events WHERE 1=1"
        params: list[Any] = []
        if target_id:
            sql += " AND target_id=?"
            params.append(target_id)
        if ticker:
            sql += " AND ticker=?"
            params.append(ticker)
        sql += " ORDER BY event_date DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self.data_store.session() as con:
            rows = con.execute(sql, params).fetchall()
        return {"status": "success", "items": [_event_row(r) for r in rows]}

    def read_market_features(self, *, ticker: str, window: str | None = None, limit: int = 100) -> dict[str, Any]:
        sql = "SELECT * FROM market_features WHERE ticker=?"
        params: list[Any] = [ticker]
        if window:
            sql += " AND feature_window=?"
            params.append(window)
        sql += " ORDER BY bucket_start DESC LIMIT ?"
        params.append(limit)
        with self.data_store.session() as con:
            rows = con.execute(sql, params).fetchall()
        return {"status": "success", "items": [_feature_row(r) for r in rows]}

    def read_collection_status(self, *, demand_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        with self.data_store.session() as con:
            if demand_id:
                demand = con.execute("SELECT * FROM collection_demands WHERE demand_id=?", (demand_id,)).fetchone()
                tasks = con.execute("SELECT * FROM collection_tasks WHERE demand_id=? ORDER BY created_at DESC LIMIT ?", (demand_id, limit)).fetchall()
                runs = con.execute("SELECT * FROM collection_runs WHERE demand_id=? ORDER BY created_at DESC LIMIT ?", (demand_id, limit)).fetchall()
            else:
                demand = None
                tasks = con.execute("SELECT * FROM collection_tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
                runs = con.execute("SELECT * FROM collection_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return {
            "status": "success",
            "demand": loads_json(demand["payload_json"], {}) if demand else None,
            "tasks": [dict(r) | {"payload": loads_json(r["payload_json"], {})} for r in tasks],
            "runs": [dict(r) | {"request": loads_json(r["request_json"], {}), "quality": loads_json(r["quality_json"], {})} for r in runs],
        }

    def read_ticket_chain(self, *, correlation_id: str) -> dict[str, Any]:
        return {"status": "success", "items": self.tickets.by_correlation(correlation_id)}

    def read_tool_capabilities(self, *, tool_name: str | None = None, limit: int = 10) -> dict[str, Any]:
        sql = "SELECT * FROM tool_capabilities WHERE 1=1"
        params: list[Any] = []
        if tool_name:
            sql += " AND tool_name=?"
            params.append(tool_name)
        sql += " ORDER BY checked_at DESC LIMIT ?"
        params.append(limit)
        with self.state_store.session() as con:
            rows = con.execute(sql, params).fetchall()
        return {
            "status": "success",
            "items": [
                dict(r) | {"capabilities": loads_json(r["capabilities_json"], {}), "errors": loads_json(r["errors_json"], [])}
                for r in rows
            ],
        }

    def read_data_quality_issues(self, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        sql = "SELECT * FROM data_quality_issues WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.data_store.session() as con:
            rows = con.execute(sql, params).fetchall()
        return {"status": "success", "items": [dict(r) | {"payload": loads_json(r["payload_json"], {})} for r in rows]}


def _event_row(r) -> dict[str, Any]:
    d = dict(r)
    d["impact"] = loads_json(d.pop("impact_json"), {})
    d["source_refs"] = loads_json(d.pop("source_refs_json"), [])
    d["payload"] = loads_json(d.pop("payload_json"), {})
    return d


def _feature_row(r) -> dict[str, Any]:
    d = dict(r)
    d["feature"] = loads_json(d.pop("feature_json"), {})
    d["source_refs"] = loads_json(d.pop("source_refs_json"), [])
    return d
