from __future__ import annotations

from pathlib import Path

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.db import SQLiteStore, loads_json
from agent_trade_intel.ids import make_idempotency_key
from agent_trade_intel.persistence import ResultPersister


def _persister(tmp_path: Path) -> ResultPersister:
    store = SQLiteStore(tmp_path / "data.db")
    store.init_schema()
    return ResultPersister(store)


def _result(status: str, *, tool: str = "market_intelligence_collector", operation: str = "collect_intelligence") -> ToolResult:
    return ToolResult(
        tool_name=tool, operation=operation, request={"target_id": "test_target"}, status=status,
        result={"status_evidence": status},
        errors=[{"error_code": "TEMPORARY_FAILURE", "retryable": True}] if status == "failed" else [],
    ).finish()


def test_failed_delivery_and_successful_retry_keep_distinct_evidence(tmp_path: Path):
    persister = _persister(tmp_path)
    task = {"task_id": "task", "idempotency_key": "stable-task-key"}
    failed = persister.save_run(
        task={**task, "_execution_key": "message:1"}, ticket_id="ticket", result=_result("failed"),
    )
    assert persister.successful_run(task, "market_intelligence_collector", "collect_intelligence") is None
    successful = persister.save_run(
        task={**task, "_execution_key": "message:2"}, ticket_id="ticket", result=_result("success"),
    )
    assert failed != successful
    with persister.store.session() as con:
        rows = con.execute("SELECT * FROM collection_runs ORDER BY rowid").fetchall()
    assert [row["status"] for row in rows] == ["failed", "success"]
    assert loads_json(rows[0]["result_json"])["status_evidence"] == "failed"
    assert loads_json(rows[1]["result_json"])["status_evidence"] == "success"
    assert loads_json(rows[0]["errors_json"])[0]["error_code"] == "TEMPORARY_FAILURE"
    assert loads_json(rows[1]["errors_json"]) == []
    assert persister.successful_run(task, "market_intelligence_collector", "collect_intelligence") == successful


def test_same_delivery_and_operation_deduplicates_without_overwriting_evidence(tmp_path: Path):
    persister = _persister(tmp_path)
    task = {"task_id": "task", "idempotency_key": "stable-task-key", "_execution_key": "message:1"}
    first = persister.save_run(task=task, ticket_id="ticket", result=_result("failed"))
    repeated = persister.save_run(task=task, ticket_id="ticket", result=_result("failed"))
    assert repeated == first
    with persister.store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 1
    other_operation = persister.save_run(
        task=task, ticket_id="ticket", result=_result("success", operation="other_operation"),
    )
    assert other_operation != first
    assert persister.successful_run(task, "market_intelligence_collector", "collect_intelligence") is None


def test_legacy_base_run_does_not_shadow_a_new_retry(tmp_path: Path):
    persister = _persister(tmp_path)
    task = {"task_id": "task", "idempotency_key": "stable-task-key"}
    legacy = persister.save_run(task=task, ticket_id="ticket", result=_result("failed"))
    retry = persister.save_run(
        task={**task, "_execution_key": "message:2"}, ticket_id="ticket", result=_result("success"),
    )
    with persister.store.session() as con:
        old = con.execute("SELECT status, idempotency_key FROM collection_runs WHERE run_id=?", (legacy,)).fetchone()
    assert old["status"] == "failed"
    assert old["idempotency_key"] == make_idempotency_key(
        "run", task["idempotency_key"], "market_intelligence_collector", "collect_intelligence",
    )
    assert retry != legacy
    assert persister.successful_run(task, "market_intelligence_collector", "collect_intelligence") == retry


def test_success_lookup_selects_latest_success_and_keeps_task_tool_operation_boundaries(tmp_path: Path):
    persister = _persister(tmp_path)
    task = {"task_id": "task", "idempotency_key": "stable-task-key"}
    first = persister.save_run(
        task={**task, "_execution_key": "message:1"}, ticket_id="ticket", result=_result("success"),
    )
    latest = persister.save_run(
        task={**task, "_execution_key": "message:2"}, ticket_id="ticket", result=_result("success"),
    )
    persister.save_run(
        task={**task, "_execution_key": "message:3"}, ticket_id="ticket", result=_result("failed"),
    )
    persister.save_run(
        task={**task, "task_id": "other-task", "_execution_key": "other:1"},
        ticket_id="other-ticket", result=_result("success"),
    )
    assert first != latest
    assert persister.successful_run(task, "market_intelligence_collector", "collect_intelligence") == latest
    assert persister.successful_run(task, "other-tool", "collect_intelligence") is None
    assert persister.successful_run(task, "market_intelligence_collector", "other-operation") is None


def test_legacy_success_without_task_id_is_reusable_by_base_key(tmp_path: Path):
    persister = _persister(tmp_path)
    task = {"idempotency_key": "legacy-task-key"}
    saved = persister.save_run(task=task, ticket_id="legacy-ticket", result=_result("success"))
    assert persister.save_run(task=task, ticket_id="legacy-ticket", result=_result("success")) == saved
    assert persister.successful_run(task, "market_intelligence_collector", "collect_intelligence") == saved
    assert persister.successful_run({}, "market_intelligence_collector", "collect_intelligence") is None
    assert persister.successful_run(
        {"idempotency_key": "different-key"}, "market_intelligence_collector", "collect_intelligence",
    ) is None


def test_retry_cache_requires_the_exact_task_contract_key(tmp_path: Path):
    persister = _persister(tmp_path)
    task = {"task_id": "task", "idempotency_key": "demand:%_one"}
    saved = persister.save_run(
        task={**task, "_execution_key": "message:2"}, ticket_id="ticket", result=_result("success"),
    )
    assert persister.successful_run(task, "market_intelligence_collector", "collect_intelligence") == saved
    assert persister.successful_run(
        {**task, "idempotency_key": "demand:%"}, "market_intelligence_collector", "collect_intelligence",
    ) is None
