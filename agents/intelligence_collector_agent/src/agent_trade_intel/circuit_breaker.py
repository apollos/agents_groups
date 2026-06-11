from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .logging_setup import get_logger

logger = get_logger("circuit_breaker")


class CircuitBreaker:
    """Per-tool circuit breaker backed by the circuit_breakers table.

    closed -> (consecutive failures >= threshold) -> open -> (cooldown elapsed) -> half_open
    half_open allows one probe call; success closes, failure reopens.
    """

    def __init__(self, store: SQLiteStore, config: dict[str, Any]):
        self.store = store
        breaker_cfg = config.get("circuit_breaker", {}) or {}
        self.failure_threshold = int(breaker_cfg.get("failure_threshold", 5))
        self.cooldown_seconds = int(breaker_cfg.get("cooldown_seconds", 600))

    def allow(self, tool_name: str) -> bool:
        row = self._get(tool_name)
        if row is None or row["status"] == "closed":
            return True
        if row["status"] == "half_open":
            return True
        cooldown_until = row["cooldown_until"]
        if cooldown_until and cooldown_until <= _now_iso():
            self._set(tool_name, status="half_open")
            logger.info("circuit for %s entering half_open probe", tool_name)
            return True
        return False

    def record_success(self, tool_name: str) -> None:
        row = self._get(tool_name)
        if row is None:
            return
        if row["status"] != "closed" or row["consecutive_failures"]:
            self._set(tool_name, status="closed", consecutive_failures=0, opened_at=None, cooldown_until=None)
            logger.info("circuit for %s closed", tool_name)

    def record_failure(self, tool_name: str, error: dict[str, Any] | None = None) -> bool:
        """Record a failure. Returns True if the circuit is now open."""
        row = self._get(tool_name)
        failures = (int(row["consecutive_failures"]) if row else 0) + 1
        was_half_open = bool(row and row["status"] == "half_open")
        if failures >= self.failure_threshold or was_half_open:
            cooldown_until = (datetime.now(timezone.utc) + timedelta(seconds=self.cooldown_seconds)).isoformat(
                timespec="seconds"
            )
            self._set(
                tool_name,
                status="open",
                consecutive_failures=failures,
                opened_at=_now_iso(),
                cooldown_until=cooldown_until,
                last_error=error,
            )
            logger.warning("circuit for %s OPEN after %s consecutive failures", tool_name, failures)
            return True
        self._set(tool_name, status="closed", consecutive_failures=failures, last_error=error)
        return False

    def state(self, tool_name: str) -> dict[str, Any] | None:
        row = self._get(tool_name)
        if row is None:
            return None
        return dict(row) | {"last_error": loads_json(row["last_error_json"], None)}

    def states(self) -> list[dict[str, Any]]:
        with self.store.session() as con:
            rows = con.execute("SELECT * FROM circuit_breakers ORDER BY tool_name").fetchall()
        return [dict(r) | {"last_error": loads_json(r["last_error_json"], None)} for r in rows]

    def _get(self, tool_name: str):
        with self.store.session() as con:
            return con.execute("SELECT * FROM circuit_breakers WHERE tool_name=?", (tool_name,)).fetchone()

    def _set(
        self,
        tool_name: str,
        *,
        status: str | None = None,
        consecutive_failures: int | None = None,
        opened_at: str | None = "__keep__",
        cooldown_until: str | None = "__keep__",
        last_error: dict[str, Any] | None = "__keep__",  # type: ignore[assignment]
    ) -> None:
        with self.store.session() as con:
            row = con.execute("SELECT * FROM circuit_breakers WHERE tool_name=?", (tool_name,)).fetchone()
            current = dict(row) if row else {
                "status": "closed",
                "consecutive_failures": 0,
                "opened_at": None,
                "cooldown_until": None,
                "last_error_json": None,
            }
            new_status = status if status is not None else current["status"]
            new_failures = consecutive_failures if consecutive_failures is not None else current["consecutive_failures"]
            new_opened = current["opened_at"] if opened_at == "__keep__" else opened_at
            new_cooldown = current["cooldown_until"] if cooldown_until == "__keep__" else cooldown_until
            new_error_json = current["last_error_json"] if last_error == "__keep__" else (dumps_json(last_error) if last_error else None)
            con.execute(
                """
                INSERT INTO circuit_breakers(tool_name, status, consecutive_failures, opened_at, cooldown_until, last_error_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(tool_name) DO UPDATE SET
                  status=excluded.status,
                  consecutive_failures=excluded.consecutive_failures,
                  opened_at=excluded.opened_at,
                  cooldown_until=excluded.cooldown_until,
                  last_error_json=excluded.last_error_json,
                  updated_at=datetime('now')
                """,
                (tool_name, new_status, new_failures, new_opened, new_cooldown, new_error_json),
            )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
