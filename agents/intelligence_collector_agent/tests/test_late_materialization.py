"""Cancellation after local materialization must fence later writes and notifications.

Adapter responses and branch counts are offline doubles. These tests exercise
ownership and transaction boundaries, not the semantic accuracy of intelligence.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.db import dumps_json, loads_json
from test_task_lifecycle_execution import (
    AS_OF,
    COLLECTION_TOPIC,
    DEMAND_ID,
    _assert_task_status,
    _message,
    _plan,
    _runtime,
    _tasks,
    agent,
)


INTRADAY_AS_OF = "2026-10-19T10:30:00+08:00"


def _suspend(worker) -> None:
    worker.registry.apply_lifecycle(DEMAND_ID, "suspend")
    _runtime(worker).tick(now=AS_OF, phase="post_market")


@pytest.mark.parametrize("agent", ["split", "shared"], indirect=True)
def test_cancel_after_mic_structures_blocks_late_quality_and_notifications(agent, monkeypatch):
    task, = _plan(agent)
    # A P3 result avoids an earlier data-quality write masking the late ledger
    # guard under test. No business facts or events are fabricated into storage.
    collected = ToolResult(
        tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
        status="success", result={
            "summary": {"queries_executed": 1, "links_read": 1, "model_calls": 1},
            "structured_outputs": {"facts": 1},
        },
    ).finish()
    tool = Mock(return_value=collected)
    monkeypatch.setattr(agent.mic, "collect", tool)
    original_materialize = agent.persister.save_mic_structures
    persisted_before_cancel = []

    def materialize_then_suspend(**kwargs):
        saved = original_materialize(**kwargs)
        with agent.data_store.session() as con:
            run = con.execute("SELECT run_id, quality_json FROM collection_runs").fetchone()
        assert run is not None
        assert loads_json(run["quality_json"], {})["severity"] == "P3"
        persisted_before_cancel.append(dict(run))
        _suspend(agent)
        # Exercise both notification branches without inserting invented event
        # content; this test controls their count-only persister return value.
        return {**saved, "events": 1, "coverage_gaps": 1}

    materialize = Mock(side_effect=materialize_then_suspend)
    monkeypatch.setattr(agent.persister, "save_mic_structures", materialize)
    result = agent.run_once(topics=COLLECTION_TOPIC)

    assert result["status"] == "lease_lost", result
    _assert_task_status(agent, task, "cancelled")
    with agent.data_store.session() as con:
        runs = [dict(row) for row in con.execute("SELECT run_id, quality_json FROM collection_runs")]
    assert runs == persisted_before_cancel
    assert "event_ledger" not in loads_json(runs[0]["quality_json"], {})
    assert agent.tickets.list(ticket_type="EVENT_TICKET") == []
    assert agent.queue.list_messages(topic="intelligence.events") == []
    assert agent.queue.list_messages(topic="coverage_gap.created") == []
    assert agent.queue.list_messages(topic="collection.result") == []
    materialize.assert_called_once()
    tool.assert_called_once()


def _plan_intraday(worker) -> dict:
    worker.config.raw["intraday"] = {"use_query_bars_after_fetch": False}
    worker.config.raw["capability_verification"]["stock_data_collector"] = {
        "fallback_order": ["5m", "none"],
    }
    # Seed an explicitly labelled offline capability record. The normal reader
    # and frequency selector must find 5m before the status-only fallback.
    with worker.state_store.session() as con:
        con.execute(
            "INSERT INTO tool_capabilities(capability_id, tool_name, status, capabilities_json, notes) "
            "VALUES (?, 'stock_data_collector', 'available', ?, ?)",
            ("offline-5m-capability", dumps_json({"frequencies": {"5m": {"usable": True}}}),
             "offline test fixture; no live capability verification"),
        )
    worker.config.raw["cadence_profiles"] = {
        "offline_intraday": {
            "market_snapshot": {"enabled": True, "bucket_size": "10m"},
            "mic_black_swan_scan": {"enabled": False},
        },
    }
    worker.config.raw["market_features"] = {"thresholds": {"abnormality_score_gte": 0.0}}
    worker.registry.register({
        "schema_version": "demand.v1", "demand_id": DEMAND_ID,
        "demand_type": "intraday_monitoring", "source_type": "operator",
        "status": "active", "priority": "normal", "idempotency_key": DEMAND_ID,
        "cadence_profile": "offline_intraday",
        "targets": [{
            "target_id": "company_offline_0", "company_name": "离线样例",
            "ticker": "600000.SH", "pool_layer": "current_holding", "sellability": "sellable",
        }],
    })
    _runtime(worker).tick(now=INTRADAY_AS_OF, phase="intraday")
    planned = worker.run_once(topics=COLLECTION_TOPIC)
    assert planned["status"] == "processed", planned
    assert planned["result"]["status"] == "planned"
    task, = _tasks(worker)
    assert task["task_type"] == "intraday_snapshot_10m"
    assert _message(worker, task["task_ticket_id"])["attempts"] == 0
    return task


def _bars(**kwargs) -> ToolResult:
    assert kwargs["frequency"] == "5m"
    return ToolResult(
        tool_name="stock_data_collector", operation="fetch_historical_bars", request=kwargs,
        status="success", quality={"usable": True, "data_quality": 0.9},
        result={"stdout": {"data": {"bars": [{
            "datetime": INTRADAY_AS_OF, "open": 10.0, "close": 10.0,
            "high": 10.0, "low": 10.0, "volume": 100, "amount": 1000,
        }]}}},
    ).finish()


@pytest.mark.parametrize("agent", ["split", "shared"], indirect=True)
def test_cancel_after_intraday_enrichment_never_calls_feature_builder(agent, monkeypatch):
    task = _plan_intraday(agent)
    tool = Mock(side_effect=_bars)
    monkeypatch.setattr(agent.stock, "fetch_historical_bars", tool)
    original_enrichment = agent._feature_enrichment_queries

    def enrich_then_suspend(**kwargs):
        enriched = original_enrichment(**kwargs)
        _suspend(agent)
        return enriched

    enrichment = Mock(side_effect=enrich_then_suspend)
    feature_builder = Mock(wraps=agent.feature_builder.build_and_save)
    monkeypatch.setattr(agent, "_feature_enrichment_queries", enrichment)
    monkeypatch.setattr(agent.feature_builder, "build_and_save", feature_builder)
    result = agent.run_once(topics=COLLECTION_TOPIC)

    assert result["status"] == "lease_lost", result
    _assert_task_status(agent, task, "cancelled")
    feature_builder.assert_not_called()
    enrichment.assert_called_once()
    tool.assert_called_once()
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM market_features").fetchone()[0] == 0
        # The fetch completed before suspension; its execution evidence remains.
        assert con.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 1
    assert agent.tickets.list(ticket_type="MARKET_FEATURE_TICKET") == []
    assert agent.queue.list_messages(topic="intelligence.events") == []


@pytest.mark.parametrize("agent", ["split", "shared"], indirect=True)
def test_owned_intraday_feature_and_notification_commit_with_shared_database(agent, monkeypatch):
    task = _plan_intraday(agent)
    tool = Mock(side_effect=_bars)
    monkeypatch.setattr(agent.stock, "fetch_historical_bars", tool)

    result = agent.run_once(topics=COLLECTION_TOPIC)

    assert result["status"] == "processed", result
    _assert_task_status(agent, task, "done")
    with agent.data_store.session() as con:
        feature = con.execute("SELECT feature_id FROM market_features").fetchone()
        assert con.execute("SELECT COUNT(*) FROM market_features").fetchone()[0] == 1
    assert result["result"]["feature"]["feature_id"] == feature["feature_id"]
    tickets = agent.tickets.list(ticket_type="MARKET_FEATURE_TICKET")
    assert len(tickets) == 1
    assert tickets[0]["payload"]["feature_id"] == feature["feature_id"]
    messages = agent.queue.list_messages(topic="intelligence.events")
    assert len(messages) == 1
    assert messages[0]["payload"]["ticket_id"] == tickets[0]["ticket_id"]
    tool.assert_called_once()
