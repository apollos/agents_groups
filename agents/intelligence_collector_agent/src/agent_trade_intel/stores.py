from __future__ import annotations

from .config import CollectorConfig
from .db import SQLiteStore


def create_stores(config: CollectorConfig) -> dict[str, SQLiteStore]:
    """Create the three logical stores used by the agent.

    state: private memory/checkpoint/session/heartbeat/circuit/capability state for this Agent.
    bus: shared message queue + Ticket Bus for multi-Agent communication.
    data: collection demands, tasks, runs, structured events, features, reports and quality issues.

    The three paths may point to the same SQLite file in a small local deployment, but the code
    treats them as separate boundaries so OpenClaw multi-Agent deployments can share bus/data while
    keeping per-Agent state isolated.
    """
    return {
        "state": SQLiteStore(config.runtime.state_sqlite_path),
        "bus": SQLiteStore(config.runtime.bus_sqlite_path),
        "data": SQLiteStore(config.runtime.data_sqlite_path),
    }


def init_unique_stores(stores: dict[str, SQLiteStore]) -> None:
    seen: set[str] = set()
    for store in stores.values():
        key = str(store.sqlite_path)
        if key in seen:
            continue
        store.init_schema()
        seen.add(key)
