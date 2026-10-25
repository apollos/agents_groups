from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agent_trade_intel import task_lifecycle
from agent_trade_intel.cli import _agent_status
from agent_trade_intel.dashboard import DashboardService
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.reader import IntelligenceReader
from agent_trade_intel.reports import DailyReportBuilder
from agent_trade_intel.tickets import TicketRepository


def _stores(tmp_path: Path, split: bool) -> tuple[SQLiteStore, SQLiteStore]:
    data = SQLiteStore(tmp_path / "data.db")
    data.init_schema()
    bus = SQLiteStore(tmp_path / "bus.db") if split else data
    if split:
        bus.init_schema()
    return data, bus


def _task(data: SQLiteStore, bus: SQLiteStore, *, status: str = "open") -> tuple[str, str]:
    ticket_id = TicketRepository(bus).create_ticket(
        ticket_type="COLLECTION_TASK_TICKET", source_agent="test", status=status,
    )
    with data.session() as con:
        con.execute(
            "INSERT INTO collection_tasks(task_id, task_ticket_id, task_type, demand_id) "
            "VALUES (?, ?, 'candidate_full_stock_snapshot', 'demand')",
            (ticket_id, ticket_id),
        )
    message_id = SQLiteMessageQueue(bus).publish("intelligence.collection", {"ticket_id": ticket_id})
    return ticket_id, message_id


def _status(data: SQLiteStore, task_id: str) -> str:
    with data.session() as con:
        return str(con.execute(
            "SELECT status FROM collection_tasks WHERE task_id=?", (task_id,),
        ).fetchone()["status"])


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize(
    "ticket_status,delivery_status,expected",
    [
        ("open", "in_progress", "in_progress"),
        ("in_progress", "open", "open"),
        ("open", "dead", "failed"),
        ("in_progress", "expired", "expired"),
        ("done", "open", "done"),
        ("failed", "in_progress", "failed"),
        ("cancelled", "open", "cancelled"),
        ("expired", "in_progress", "expired"),
        ("open", "done", "open"),
    ],
)
def test_projection_uses_ticket_outcome_and_actual_delivery(
    tmp_path: Path, split: bool, ticket_status: str, delivery_status: str, expected: str,
):
    data, bus = _stores(tmp_path, split)
    ticket_id, message_id = _task(data, bus, status=ticket_status)
    with bus.session() as con:
        con.execute("UPDATE messages SET status=? WHERE message_id=?", (delivery_status, message_id))
    task_lifecycle.sync_task_statuses(data, bus)
    assert _status(data, ticket_id) == expected
    assert task_lifecycle.sync_task_statuses(data, bus)["updated_tasks"] == 0
    assert TicketRepository(bus).get(ticket_id)["status"] == ticket_status
    assert SQLiteMessageQueue(bus).inspect(message_id)["status"] == delivery_status


def test_result_notifications_do_not_reopen_a_dead_delivery(tmp_path: Path):
    data, bus = _stores(tmp_path, True)
    ticket_id, message_id = _task(data, bus)
    with bus.session() as con:
        con.execute("UPDATE messages SET status='dead' WHERE message_id=?", (message_id,))
    SQLiteMessageQueue(bus).publish("collection.result", {"ticket_id": ticket_id})
    task_lifecycle.sync_task_statuses(data, bus)
    assert _status(data, ticket_id) == "failed"


def test_legacy_rows_and_completed_history_are_preserved(tmp_path: Path):
    data, bus = _stores(tmp_path, True)
    done_id, _ = _task(data, bus, status="done")
    with data.session() as con:
        con.execute(
            "INSERT INTO collection_demands(demand_id, demand_type, source_type, status, payload_json) "
            "VALUES ('demand', 'test', 'test', 'cancelled', '{}')"
        )
        con.execute(
            "INSERT INTO collection_tasks(task_id, task_ticket_id, task_type, status) "
            "VALUES ('legacy', 'missing-ticket', 'test', 'in_progress')"
        )
        con.execute(
            "INSERT INTO collection_tasks(task_id, task_type, status) VALUES ('unbound', 'test', 'done')"
        )
    task_lifecycle.sync_task_statuses(data, bus)
    assert _status(data, done_id) == "done"
    assert _status(data, "legacy") == "in_progress"
    assert _status(data, "unbound") == "done"


@pytest.mark.parametrize("split", [False, True])
def test_failed_projection_rolls_back_and_public_reader_repairs_it(
    tmp_path: Path, split: bool, monkeypatch: pytest.MonkeyPatch,
):
    data, bus = _stores(tmp_path, split)
    ticket_id, _ = _task(data, bus, status="cancelled")
    original_sync = task_lifecycle._sync

    def fail_after_update(data_con, bus_con):
        original_sync(data_con, bus_con)
        raise OSError("injected projection storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(task_lifecycle, "_sync", fail_after_update)
        assert task_lifecycle.best_effort_sync(data, bus)["status"] == "deferred"
        assert _status(data, ticket_id) == "open"
        with pytest.raises(OSError, match="projection storage failure"):
            IntelligenceReader(data, bus).read_collection_status()
    rows = IntelligenceReader(data, bus).read_collection_status()["tasks"]
    assert rows[0]["status"] == "cancelled"
    assert TicketRepository(bus).get(ticket_id)["status"] == "cancelled"


@pytest.mark.parametrize("split", [False, True])
def test_projection_holds_bus_snapshot_until_data_commit(
    tmp_path: Path, split: bool, monkeypatch: pytest.MonkeyPatch,
):
    data, bus = _stores(tmp_path, split)
    ticket_id, _ = _task(data, bus)
    entered, release = Event(), Event()
    failures = []
    original_sync = task_lifecycle._sync

    def pause_before_projection(data_con, bus_con):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test did not release projection")
        return original_sync(data_con, bus_con)

    def synchronize():
        try:
            task_lifecycle.sync_task_statuses(data, bus)
        except Exception as exc:
            failures.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(task_lifecycle, "_sync", pause_before_projection)
        worker = Thread(target=synchronize)
        worker.start()
        try:
            assert entered.wait(5)
            with bus.session() as con:
                con.execute("PRAGMA busy_timeout=1")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    con.execute("UPDATE tickets SET status='cancelled' WHERE ticket_id=?", (ticket_id,))
        finally:
            release.set()
            worker.join(timeout=5)
    assert not worker.is_alive()
    assert not failures
    TicketRepository(bus).update_status(ticket_id, "cancelled")
    task_lifecycle.sync_task_statuses(data, bus)
    assert _status(data, ticket_id) == "cancelled"


@pytest.mark.parametrize("outlet", ["reader", "cli", "report", "dashboard"])
def test_public_status_outlets_repair_existing_task_rows(tmp_path: Path, outlet: str):
    data, bus = _stores(tmp_path, True)
    ticket_id, _ = _task(data, bus, status="cancelled")
    with data.session() as con:
        con.execute(
            "INSERT INTO collection_demands(demand_id, demand_type, source_type, status, payload_json) "
            "VALUES ('demand', 'test', 'test', 'suspended', '{}')"
        )
    config = SimpleNamespace(
        runtime=SimpleNamespace(
            agent_id="test", agent_group="intelligence_collector", timezone="Asia/Shanghai",
            state_sqlite_path=data.sqlite_path, bus_sqlite_path=bus.sqlite_path,
            data_sqlite_path=data.sqlite_path, workspace_root=tmp_path, log_dir=tmp_path / "logs",
        ),
        model=SimpleNamespace(primary="test-model"), raw={},
    )
    if outlet == "reader":
        result = IntelligenceReader(data, bus).read_collection_status()
        assert result["tasks"][0]["status"] == "cancelled"
    elif outlet == "cli":
        result = _agent_status(config, data, bus, data)
        assert result["open_tasks_by_type"] == []
    elif outlet == "dashboard":
        result = DashboardService(config, state_store=data, bus_store=bus, data_store=data).overview()
        assert result["recent_tasks"][0]["status"] == "cancelled"
    else:
        trade_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        result = DailyReportBuilder(data, tmp_path / "reports", "test", bus_store=bus)._summary(trade_date)
        assert result["demand_coverage"][0]["tasks_open"] == 0
    assert _status(data, ticket_id) == "cancelled"
