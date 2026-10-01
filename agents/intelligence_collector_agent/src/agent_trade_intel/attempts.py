"""Control records for supervised MIC collection attempts (browser search design 8.3).

One row per child-process run. The unique partial index on ``task_key`` guarantees a single
*active* attempt per task; a second delivery of the same task sees the active row and defers
instead of starting a second worker on the same browser profile. Ownership checks for leftover
``cleanup_incomplete`` rows are evidence based (pid alive *and* its command line carries the
attempt id); nothing here kills processes by name.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .db import SQLiteStore, dumps_json
from .ids import new_id

ACTIVE_STATES = ("starting", "running", "cancelling")
TERMINAL_STATES = ("completed", "failed", "cancelled", "interrupted", "cleanup_incomplete")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def pid_owns_attempt(pid: int | None, attempt_id: str) -> bool:
    """True only when ``pid`` is alive and its command line references ``attempt_id``."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # alive but not ours to inspect: stay conservative
    try:
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        environ_hint = ""
        try:
            environ_hint = Path(f"/proc/{int(pid)}/environ").read_bytes().decode("utf-8", "replace")
        except OSError:
            pass
    except OSError:
        return True  # cannot verify; assume it may still own the profile
    return attempt_id in cmdline or f"MIC_WORKER_ATTEMPT_ID={attempt_id}" in environ_hint


class CollectionAttemptRepository:
    def __init__(self, store: SQLiteStore, *, stale_after_seconds: int = 180):
        self.store = store
        self.stale_after_seconds = int(stale_after_seconds)

    # --- lifecycle ----------------------------------------------------------------------

    def start(self, *, task_key: str, owner_token: str, deadline_seconds: float,
              task_id: str | None = None, ticket_id: str | None = None,
              message_id: str | None = None) -> tuple[str | None, dict[str, Any] | None]:
        """Register a new attempt in state ``starting``.

        Returns ``(attempt_id, None)`` or ``(None, active_row)`` when another attempt for the
        same task is still active with a fresh heartbeat. Stale active rows (heartbeat older
        than ``stale_after_seconds`` and deadline passed or no heartbeat at all) are closed as
        ``interrupted`` first, so a crashed parent does not block the task forever.
        """
        attempt_id = new_id("attempt")
        now = _now()
        deadline_at = _iso(now + timedelta(seconds=float(deadline_seconds)))
        for _ in range(2):
            try:
                with self.store.session() as con:
                    con.execute(
                        """
                        INSERT INTO collection_attempt(attempt_id, task_key, task_id, ticket_id, message_id,
                          owner_token, state, heartbeat_at, deadline_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'starting', ?, ?)
                        """,
                        (attempt_id, task_key, task_id, ticket_id, message_id, owner_token, _iso(now), deadline_at),
                    )
                return attempt_id, None
            except sqlite3.IntegrityError:
                active = self.active_for(task_key)
                if active is None:
                    continue
                if self._is_stale(active):
                    self.finish(active["attempt_id"], state="interrupted", error_code="stale_heartbeat")
                    continue
                return None, active
        return None, self.active_for(task_key)

    def mark_running(self, attempt_id: str, *, worker_pid: int | None = None) -> None:
        with self.store.session() as con:
            con.execute(
                """
                UPDATE collection_attempt
                SET state='running', worker_pid=COALESCE(?, worker_pid),
                    worker_started_at=COALESCE(worker_started_at, ?), heartbeat_at=?, updated_at=datetime('now')
                WHERE attempt_id=? AND state IN ('starting', 'running')
                """,
                (worker_pid, _iso(_now()), _iso(_now()), attempt_id),
            )

    def heartbeat(self, attempt_id: str, *, state: str | None = None, worker_pid: int | None = None) -> bool:
        """Refresh heartbeat; returns False if the attempt is no longer active (revoked)."""
        with self.store.session() as con:
            cur = con.execute(
                """
                UPDATE collection_attempt
                SET heartbeat_at=?, state=COALESCE(?, state), worker_pid=COALESCE(?, worker_pid),
                    updated_at=datetime('now')
                WHERE attempt_id=? AND state IN ('starting', 'running', 'cancelling')
                """,
                (_iso(_now()), state, worker_pid, attempt_id),
            )
        return bool(cur.rowcount)

    def request_cancel(self, attempt_id: str) -> None:
        self.heartbeat(attempt_id, state="cancelling")

    def finish(self, attempt_id: str, *, state: str, error_code: str | None = None,
               cleanup: str | None = None, result_path: str | None = None,
               budget_used: dict[str, Any] | None = None, worker_pid: int | None = None) -> None:
        if state not in TERMINAL_STATES:
            raise ValueError(f"not a terminal state: {state}")
        with self.store.session() as con:
            con.execute(
                """
                UPDATE collection_attempt
                SET state=?, error_code=?, cleanup=COALESCE(?, cleanup), result_path=COALESCE(?, result_path),
                    budget_used_json=?, worker_pid=COALESCE(?, worker_pid), updated_at=datetime('now')
                WHERE attempt_id=?
                """,
                (state, error_code, cleanup, result_path, dumps_json(budget_used or {}), worker_pid, attempt_id),
            )

    # --- queries ------------------------------------------------------------------------

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        with self.store.session() as con:
            row = con.execute("SELECT * FROM collection_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
        return dict(row) if row else None

    def is_active(self, attempt_id: str) -> bool:
        row = self.get(attempt_id)
        return bool(row and row["state"] in ACTIVE_STATES)

    def active_for(self, task_key: str) -> dict[str, Any] | None:
        with self.store.session() as con:
            row = con.execute(
                "SELECT * FROM collection_attempt WHERE task_key=? AND state IN ('starting','running','cancelling')",
                (task_key,),
            ).fetchone()
        return dict(row) if row else None

    def blocking_cleanup_incomplete(self) -> dict[str, Any] | None:
        """A previous attempt whose worker could not be reaped and is verifiably still alive.

        Rows whose worker pid is gone (or no longer references the attempt) are closed as
        ``interrupted`` so they stop blocking; a verified live worker blocks new runs on the
        shared browser profile until an operator resolves it.
        """
        with self.store.session() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM collection_attempt WHERE state='cleanup_incomplete' ORDER BY updated_at DESC")]
        for row in rows:
            if pid_owns_attempt(row.get("worker_pid"), row["attempt_id"]):
                return row
            self.finish(row["attempt_id"], state="interrupted", error_code="cleanup_incomplete_worker_gone",
                        cleanup=row.get("cleanup"))
        return None

    def _is_stale(self, row: dict[str, Any]) -> bool:
        hb = _parse(row.get("heartbeat_at")) or _parse(row.get("created_at"))
        if hb is None:
            return True
        age = (_now() - hb).total_seconds()
        if age <= self.stale_after_seconds:
            return False
        deadline = _parse(row.get("deadline_at"))
        # Past deadline, or no heartbeat for far longer than the stale window: the parent died.
        return deadline is None or _now() > deadline or age > 2 * self.stale_after_seconds
