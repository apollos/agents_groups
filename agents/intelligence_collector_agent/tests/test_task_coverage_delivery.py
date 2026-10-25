"""Offline transport regression for model-authored task coverage and gap details.

The reports below are hand-written contract examples, not successful model output.
These tests exercise real Agent persistence, SQLite and the formal Reader; they
do not test whether a model can discover an omitted research direction.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.agent import IntelligenceCollectorAgent
from agent_trade_intel.config import AgentModelConfig, CollectorConfig, RuntimeConfig, ToolConfig
from agent_trade_intel.db import SQLiteStore, loads_json
from agent_trade_intel.persistence import ResultPersister
from agent_trade_intel.quality import QualityGate
from agent_trade_intel.reader import IntelligenceReader


DEMAND_ID = "offline-coverage-demand"
MIC_RUN_ID = "offline-mic-coverage-run"
QUESTION = "本期产品交付、经营进展与资本运作有哪些公开资料及覆盖缺口？"


def _task() -> dict:
    return {
        "task_id": "offline-coverage-task", "task_type": "mic_deep_collect",
        "demand_id": DEMAND_ID, "idempotency_key": "offline-coverage-task-key",
        "target": {"target_id": "company_offline", "company_name": "离线样例公司",
                   "ticker": "600000.SH"},
        "task_profile": {"questions": [{"question": QUESTION}], "time_window": "90d"},
    }


def _report(*, semantic: bool) -> dict:
    gaps = [
        {
            "gap_id": "offline-gap-source-a", "search_run_id": MIC_RUN_ID,
            "source_link_id": "offline-source-a", "gap_type": "task_question_coverage",
            "description": "本来源未覆盖问题中的资本运作方向，不能据此认定本期没有相关事项。",
            "priority": "high", "suggested_next_queries": ["离线样例公司 本期 资本运作 公告"],
            "status": "open",
        },
        {
            "gap_id": "offline-gap-source-b", "search_run_id": MIC_RUN_ID,
            "source_link_id": "offline-source-b", "gap_type": "missing_date",
            "description": "本来源仅有产品计划，未披露实际交付日期。",
            "priority": "medium", "suggested_next_queries": ["离线样例公司 交付日期 公告"],
            "status": "open",
        },
        {
            "gap_id": "offline-gap-run", "search_run_id": MIC_RUN_ID,
            "source_link_id": None, "gap_type": "publication_time_unverified",
            "description": "一条候选来源披露日期无法验证，未进入近期事项抽取。",
            "priority": "medium", "suggested_next_queries": [], "status": "open",
        },
    ]
    report = {
        "search_run_id": MIC_RUN_ID, "target": "离线样例公司", "time_window": "90d",
        "task_questions": [{"question": QUESTION, "period": None}],
        "summary": {"queries_executed": 0, "links_read": 0, "model_calls": 0},
        "structured_outputs": {"events": 0, "facts": 0, "metrics": 0, "coverage_gaps": len(gaps)},
        "all_events": [], "top_events": [], "coverage_gaps": gaps,
        "task_coverage": [
            {"protocol": "task_coverage_v1", "scope": "source_material",
             "question_id": "q1", "question": QUESTION, "source_link_id": "offline-source-a",
             "status": "unanswered", "uncovered_aspects": ["资本运作"], "answer_claim_ids": [],
             "reason": "model_unanswered", "answer_claim_refs": []},
            {"protocol": "task_coverage_v1", "scope": "source_material",
             "question_id": "q1", "question": QUESTION, "source_link_id": "offline-source-b",
             "status": "unanswered", "uncovered_aspects": ["产品交付"], "answer_claim_ids": [],
             "reason": "model_unanswered", "answer_claim_refs": []},
        ],
        "replay_context": {"mode": "human_authored_contract", "new_model_calls": 0},
    }
    if semantic:
        from mic.event_resolution import PROTOCOL
        report["event_resolution_protocol"] = PROTOCOL
    return report


def _result(report: dict) -> ToolResult:
    result = ToolResult(
        tool_name="market_intelligence_collector", operation="collect_intelligence",
        request={"target_id": _task()["target"]["target_id"], "task_profile": _task()["task_profile"]},
        status="success", result=deepcopy(report), result_ref=f"mic://search_runs/{report['search_run_id']}",
    ).finish()
    result.quality.update(QualityGate({}).evaluate(result))
    return result


def _store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.init_schema()
    return store


@pytest.mark.parametrize("semantic", [False, True], ids=["legacy", "semantic"])
def test_formal_reader_keeps_full_gap_details_coverage_and_run_references(tmp_path: Path, semantic: bool):
    data, bus = _store(tmp_path / "data.db"), _store(tmp_path / "bus.db")
    persister = ResultPersister(data)
    task, report = _task(), _report(semantic=semantic)
    original = deepcopy(report)
    result = _result(report)
    run_id = persister.save_run(task=task, ticket_id="offline-ticket", result=result)
    persister.save_mic_structures(task=task, result=result)
    # Repeated persistence is the same delivery, not permission for another tool call.
    assert persister.save_run(task=task, ticket_id="offline-ticket", result=result) == run_id
    persister.save_mic_structures(task=task, result=result)

    reader = IntelligenceReader(data, bus)
    rows = reader.read_collection_status(demand_id=DEMAND_ID)["runs"]
    assert len(rows) == 1
    row = rows[0]
    assert (row["run_id"], row["task_id"], row["ticket_id"], row["demand_id"]) == (
        run_id, task["task_id"], "offline-ticket", DEMAND_ID,
    )
    assert row["result_ref"] == f"mic://search_runs/{MIC_RUN_ID}"
    delivered = loads_json(row["result_json"], {})
    assert delivered == original
    assert [gap["source_link_id"] for gap in delivered["coverage_gaps"]] == [
        "offline-source-a", "offline-source-b", None,
    ]
    assert {gap["search_run_id"] for gap in delivered["coverage_gaps"]} == {MIC_RUN_ID}
    assert row["quality"]["usable"] is False
    assert row["quality"]["output_status"] == "no_structured_output"
    assert reader.read_collection_status(demand_id="another-demand")["runs"] == []
    with data.session() as con:
        gaps = {row["gap_id"]: dict(row) for row in con.execute("SELECT * FROM coverage_gaps")}
        assert con.execute("SELECT COUNT(*) FROM structured_events").fetchone()[0] == 0
    assert set(gaps) == {gap["gap_id"] for gap in original["coverage_gaps"]}
    for expected in original["coverage_gaps"]:
        stored = gaps[expected["gap_id"]]
        assert stored["target_id"] == task["target"]["target_id"]
        assert stored["source_run_id"] == MIC_RUN_ID
        assert stored["description"] == expected["description"]
        assert stored["priority"] == expected["priority"]
        assert stored["status"] == expected["status"]
        assert loads_json(stored["suggested_next_queries_json"], []) == expected["suggested_next_queries"]
    assert report == original and result.result == original


@pytest.mark.parametrize("semantic", [False, True], ids=["legacy", "semantic"])
def test_gap_replay_without_external_ids_deduplicates_without_merging_sources(tmp_path: Path, semantic: bool):
    persister = ResultPersister(_store(tmp_path / "data.db"))
    report = _report(semantic=semantic)
    # Same wording at two different sources remains two scoped observations.
    gaps = [deepcopy(report["coverage_gaps"][0]) for _ in range(2)]
    gaps[1]["source_link_id"] = "offline-source-b"
    for gap in gaps:
        gap.pop("gap_id")
        gap["description"] = "本来源未覆盖需求的全部方向。"
    report["coverage_gaps"] = gaps
    report["structured_outputs"]["coverage_gaps"] = len(gaps)
    result = _result(report)
    for _ in range(2):
        persister.save_mic_structures(task=_task(), result=result)
    with persister.store.session() as con:
        rows = con.execute("SELECT gap_id,source_run_id FROM coverage_gaps").fetchall()
    assert len(rows) == 2
    assert len({row["gap_id"] for row in rows}) == 2
    assert {row["source_run_id"] for row in rows} == {MIC_RUN_ID}


@pytest.mark.parametrize("semantic", [False, True], ids=["legacy", "semantic"])
def test_real_agent_delivers_gap_only_report_once_and_keeps_it_unusable(tmp_path: Path, monkeypatch, semantic: bool):
    config = CollectorConfig(
        raw={"capability_verification": {"run_on_startup": False}, "quality": {}},
        path=tmp_path / "offline.yaml",
        runtime=RuntimeConfig(
            agent_id="offline-coverage-agent", agent_group="intelligence_collector",
            state_sqlite_path=tmp_path / "state.db", bus_sqlite_path=tmp_path / "bus.db",
            data_sqlite_path=tmp_path / "data.db", workspace_root=tmp_path,
            log_dir=tmp_path / "logs", reports_dir=tmp_path / "reports",
        ),
        model=AgentModelConfig(primary="offline/no-model-call", fallbacks=[], require_registered=False),
        tools=ToolConfig(
            mic_enabled=True, stock_enabled=False, mic_config_dir=None, stock_config_dir=None,
            python_executable="python", stock_working_dir=None,
        ),
    )
    agent = IntelligenceCollectorAgent(config)
    report = _report(semantic=semantic)
    replay = Mock(return_value=deepcopy(report))
    monkeypatch.setattr(agent.mic, "_collect_with_timeout", replay)

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected external tool call in offline coverage transport regression")

    monkeypatch.setattr(agent.stock, "_run", forbidden)
    monkeypatch.setattr(agent.stock, "cli_available", forbidden)
    monkeypatch.setattr(agent.hk_connect, "collect_snapshot", forbidden)
    monkeypatch.setattr(agent.market_context, "collect_snapshot", forbidden)
    for number in range(2):
        ticket_id = agent.tickets.create_ticket(
            ticket_type="COLLECTION_TASK_TICKET", source_agent="offline-test",
            target_agent_id=config.runtime.agent_id, priority="normal", payload=_task(),
        )
        agent.queue.publish(
            "intelligence.collection", {"ticket_id": ticket_id}, target_agent_id=config.runtime.agent_id,
            idempotency_key=f"offline-coverage-delivery-{number}",
        )
        outcome = agent.run_once(topics=["intelligence.collection"])
        assert outcome["status"] == "processed", outcome
        assert outcome["result"]["output_status"] == "no_structured_output"
        assert outcome["result"]["usable"] is False
        assert agent.tickets.get(ticket_id)["status"] == "done"
    replay.assert_called_once()
    reader = IntelligenceReader(agent.data_store, agent.bus_store, agent.state_store)
    rows = reader.read_collection_status(demand_id=DEMAND_ID)["runs"]
    assert len(rows) == 1
    assert loads_json(rows[0]["result_json"], {}) == report
    assert rows[0]["result_ref"] == f"mic://search_runs/{MIC_RUN_ID}"
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM coverage_gaps").fetchone()[0] == len(report["coverage_gaps"])
        assert con.execute("SELECT COUNT(*) FROM structured_events").fetchone()[0] == 0
