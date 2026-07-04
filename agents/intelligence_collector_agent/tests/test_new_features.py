from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from agent_trade_intel.config import load_config
from agent_trade_intel.db import SQLiteStore, dumps_json
from agent_trade_intel.demand import DemandRegistry
from agent_trade_intel.errors import QueueEmpty
from agent_trade_intel.market_features import MarketFeatureBuilder, should_emit_feature_ticket
from agent_trade_intel.planner import TaskGraphPlanner
from agent_trade_intel.pools import PoolRepository, resolve_demand_targets
from agent_trade_intel.query_service import IntelligenceQueryService
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.reader import IntelligenceReader
from agent_trade_intel.reports import DailyReportBuilder
from agent_trade_intel.runtime import RuntimeController
from agent_trade_intel.tickets import TicketRepository
from agent_trade_intel.time_utils import is_trading_day, market_phase


def _config(tmp_path: Path) -> Path:
    cfg = {
        "agent": {"agent_id": "test_intel", "agent_group": "intelligence_collector"},
        "openclaw": {"model": {"primary": "openai/gpt-5.5", "fallbacks": [], "require_registered": False, "allow_openclaw_default": False}},
        "runtime": {"sqlite_path": str(tmp_path / "intel.db"), "workspace_root": str(tmp_path), "log_dir": str(tmp_path / "logs"), "timezone": "Asia/Shanghai"},
        "queue": {"consume_topics": ["intelligence.collection"], "lease_seconds": 30},
        "cadence": {"intraday_bucket_minutes": 10},
        "capability_verification": {"run_pre_market": False},
        "tools": {"python_executable": "python", "market_intelligence_collector": {"enabled": True}, "stock_data_collector": {"enabled": True, "config_dir": "config", "working_dir": None}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def _store(tmp_path: Path, name: str = "t.db") -> SQLiteStore:
    store = SQLiteStore(tmp_path / name)
    store.init_schema()
    return store


def _demand(demand_id: str = "d1") -> dict:
    return {
        "schema_version": "demand.v1",
        "demand_id": demand_id,
        "demand_type": "intraday_monitoring",
        "source_type": "test_harness",
        "status": "active",
        "priority": "high",
        "active_from": "2026-06-11",
        "active_to": "2026-06-11",
        "target_scope": {"scope_type": "explicit_targets", "include_tickers": ["300750.SZ"], "pool_layers": ["current_holding"]},
        "targets": [
            {
                "target_type": "ticker",
                "ticker": "300750.SZ",
                "target_id": "company_300750",
                "company_name": "宁德时代",
                "pool_layer": "current_holding",
                "sellability": "sellable",
            }
        ],
        "task_profile": {"mic": {"enabled": True}, "stock_data": {"enabled": True}},
        "test_mode": True,
        "idempotency_key": f"demand:{demand_id}",
    }


# ---------------------------------------------------------------------------
# demand.registered / demand.changed messages + RuntimeController
# ---------------------------------------------------------------------------


def test_demand_register_and_lifecycle_publish_messages(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    reg = DemandRegistry(store, q)
    res = reg.register(_demand(), activate=True)
    assert res["message_id"]
    registered = q.list_messages(topic="demand.registered")
    assert len(registered) == 1
    assert registered[0]["payload"]["demand_id"] == "d1"
    res2 = reg.apply_lifecycle("d1", "suspend")
    assert res2["message_id"]
    changed = q.list_messages(topic="demand.changed")
    assert changed[0]["payload"]["new_status"] == "suspended"


def test_runtime_tick_consumes_demand_messages_and_compiles(tmp_path: Path):
    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path, "intel.db")
    q = SQLiteMessageQueue(store)
    DemandRegistry(store, q).register(_demand(), activate=True)
    controller = RuntimeController(cfg, state_store=store, bus_store=store, data_store=store)
    summary = controller.tick(now="2026-06-11T10:30:00+08:00", phase="intraday")
    assert any(e["topic"] == "demand.registered" for e in summary["demand_events_consumed"])
    assert summary["created"]
    # the demand.registered message must be done, and a collection request must be open
    assert q.list_messages(status="done", topic="demand.registered")
    assert q.list_messages(status="open", topic="intelligence.collection")


def test_runtime_tick_cancels_open_work_for_cancelled_demand(tmp_path: Path):
    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path, "intel.db")
    q = SQLiteMessageQueue(store)
    tickets = TicketRepository(store)
    reg = DemandRegistry(store, q)
    reg.register(_demand(), activate=True)
    controller = RuntimeController(cfg, state_store=store, bus_store=store, data_store=store)
    controller.tick(now="2026-06-11T10:30:00+08:00", phase="intraday")
    open_before = q.list_messages(status="open", topic="intelligence.collection")
    assert open_before
    reg.apply_lifecycle("d1", "cancel")
    summary = controller.tick(now="2026-06-11T10:40:00+08:00", phase="intraday")
    cancel_events = [e for e in summary["demand_events_consumed"] if e["topic"] == "demand.changed"]
    assert cancel_events and cancel_events[0]["cancelled"]["tickets"]
    cancelled_ticket = cancel_events[0]["cancelled"]["tickets"][0]
    assert tickets.get(cancelled_ticket)["status"] == "cancelled"
    assert not q.list_messages(status="open", topic="intelligence.collection")


def test_runtime_tick_schedules_capability_check_when_forced(tmp_path: Path):
    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path, "intel.db")
    controller = RuntimeController(cfg, state_store=store, bus_store=store, data_store=store)
    summary = controller.tick(now="2026-06-11T08:30:00+08:00", phase="pre_market", run_capability_validation=True)
    assert summary["capability_check"]
    ticket = TicketRepository(store).get(summary["capability_check"]["ticket_id"])
    assert ticket["payload"]["task_type"] == "tool_capability_check"
    # idempotent within the same day
    again = controller.tick(now="2026-06-11T08:40:00+08:00", phase="pre_market", run_capability_validation=True)
    assert again["capability_check"]["ticket_id"] == summary["capability_check"]["ticket_id"]


# ---------------------------------------------------------------------------
# dynamic pool resolution
# ---------------------------------------------------------------------------


def test_dynamic_pool_target_resolution(tmp_path: Path):
    store = _store(tmp_path)
    pool = PoolRepository(store)
    pool.upsert_member(pool_layer="current_holding", ticker="300750.SZ", company_name="宁德时代", sellability="sellable")
    pool.upsert_member(pool_layer="current_holding", ticker="600000.SH", sellability="t1_locked")
    pool.upsert_member(pool_layer="current_holding", ticker="600519.SH", sellability="sellable", is_st=True)
    demand = _demand()
    demand["targets"] = []
    demand["target_scope"] = {
        "scope_type": "dynamic_pool",
        "pool_layers": ["current_holding"],
        "filters": {"sellability": "sellable", "exclude_st": True, "exclude_suspended": True},
    }
    targets = resolve_demand_targets(demand, pool)
    assert [t["ticker"] for t in targets] == ["300750.SZ"]
    assert targets[0]["pool_layer"] == "current_holding"
    # explicit targets always win
    explicit = _demand()
    assert resolve_demand_targets(explicit, pool)[0]["target_id"] == "company_300750"
    # removal is reflected
    assert pool.remove_member(pool_layer="current_holding", ticker="300750.SZ")
    assert resolve_demand_targets(demand, pool) == []


# ---------------------------------------------------------------------------
# message-based query service
# ---------------------------------------------------------------------------


def _seed_event(store: SQLiteStore, event_id: str, ticker: str, event_type: str, confidence: float):
    with store.session() as con:
        con.execute(
            """
            INSERT INTO structured_events(
              event_id, target_id, ticker, event_type, event_date, summary_cn,
              confidence, data_quality, payload_json, idempotency_key
            ) VALUES (?, ?, ?, ?, '2026-06-11', 's', ?, 0.9, '{}', ?)
            """,
            (event_id, f"ticker_{ticker}", ticker, event_type, confidence, f"evt:{event_id}"),
        )


def test_query_service_round_trip(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    tickets = TicketRepository(store)
    reader = IntelligenceReader(data_store=store, bus_store=store, state_store=store)
    service = IntelligenceQueryService(reader, tickets, q, "test_intel")
    _seed_event(store, "e1", "300750.SZ", "risk", 0.9)
    _seed_event(store, "e2", "300750.SZ", "policy", 0.4)
    _seed_event(store, "e3", "600519.SH", "risk", 0.95)

    req = service.publish_request(
        query_type="recent_events",
        target={"ticker": "300750.SZ"},
        filters={"event_types": ["risk", "policy"], "min_confidence": 0.7},
        limit=10,
        source_agent="analysis_agent_x",
    )
    # request message is addressed to the collector group
    msg = q.lease(topics=["query.intelligence.request"], worker_id="w", target_agent_id="test_intel", target_agent_group="intelligence_collector")
    assert msg.payload["ticket_id"] == req["ticket_id"]

    result = service.handle_request_ticket(tickets.get(req["ticket_id"]))
    q.ack(msg.message_id)
    assert result["status"] == "answered"
    assert result["count"] == 1  # e2 filtered by confidence, e3 by ticker

    response_ticket = tickets.get(result["response_ticket_id"])
    assert response_ticket["ticket_type"] == "INTELLIGENCE_QUERY_RESPONSE_TICKET"
    assert response_ticket["payload"]["items"][0]["event_id"] == "e1"
    assert response_ticket["evidence_refs"] == ["e1"]
    # response message is targeted back at the requester
    resp_msg = q.lease(topics=["query.intelligence.response"], worker_id="w2", target_agent_id="analysis_agent_x")
    assert resp_msg.payload["ticket_id"] == result["response_ticket_id"]
    # request ticket closed
    assert tickets.get(req["ticket_id"])["status"] == "done"


def test_query_service_unsupported_type_still_answers(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    tickets = TicketRepository(store)
    service = IntelligenceQueryService(IntelligenceReader(data_store=store, bus_store=store, state_store=store), tickets, q, "test_intel")
    req = service.publish_request(query_type="nonexistent", source_agent="x")
    result = service.handle_request_ticket(tickets.get(req["ticket_id"]))
    assert result["count"] == 0
    response = tickets.get(result["response_ticket_id"])
    assert response["payload"]["status"] == "unsupported_query_type"


# ---------------------------------------------------------------------------
# enriched market features + multi-condition thresholds
# ---------------------------------------------------------------------------


def _feature_config() -> dict:
    return {
        "cadence": {"intraday_bucket_minutes": 10},
        "market_features": {
            "thresholds": {
                "abnormality_score_gte": 0.99,
                "return_abs_gte": 0.02,
                "amount_ratio_vs_20d_same_bucket_gte": 3.0,
                "distance_to_limit_up_lte": 0.015,
                "distance_to_limit_down_lte": 0.015,
            },
            "risk_review": {"negative_return_lte": -0.025, "hit_limit_down": True},
            "limit_rules": {"default_pct": 0.10, "st_pct": 0.05, "growth_board_pct": 0.20},
        },
    }


def _bucket_task() -> dict:
    return {
        "target": {"ticker": "300750.SZ", "target_id": "company_300750"},
        "bucket_start": "2026-06-11T10:30:00+08:00",
        "bucket_end": "2026-06-11T10:40:00+08:00",
        "bucket_size": "10m",
        "as_of": "2026-06-11T10:32:00+08:00",
    }


def test_market_feature_enrichment_and_thresholds(tmp_path: Path):
    store = _store(tmp_path)
    builder = MarketFeatureBuilder(store, _feature_config())
    stock_result = {
        "stdout": {
            "request_id": "req_1",
            "data": {
                "bars": [
                    {"datetime": "2026-06-11T10:30:00+08:00", "open": 100, "close": 101, "high": 101.5, "low": 99.5, "amount": 3000, "volume": 30},
                    {"datetime": "2026-06-11T10:35:00+08:00", "open": 101, "close": 103, "high": 103.5, "low": 100.5, "amount": 3500, "volume": 34},
                ]
            },
        }
    }
    daily_result = {"stdout": [{"datetime": "2026-06-09", "close": 99}, {"datetime": "2026-06-10", "close": 100}]}
    history_result = {
        "stdout": [
            {"datetime": f"2026-06-0{d}T10:3{m}:00+08:00", "amount": 1000, "close": 100}
            for d in (2, 3, 4, 5)
            for m in (0, 5)
        ]
    }
    feature = builder.build_and_save(
        task=_bucket_task(),
        stock_result=stock_result,
        quality={"data_quality": 0.9},
        source_frequency="5m",
        daily_result=daily_result,
        history_result=history_result,
    )
    features = feature["payload"]["features"]
    assert features["price_features"]["prev_close"] == 100
    assert features["price_features"]["day_return"] == pytest.approx(0.03)
    assert "position_in_intraday_range" in features["price_features"]
    # bucket amount 6500 vs 2000/day average -> ratio 3.25
    assert features["volume_features"]["amount_ratio_vs_20d_same_bucket"] == pytest.approx(3.25)
    # 300xxx -> 20% limit from prev_close 100
    assert features["tradability_features"]["limit_up_price"] == pytest.approx(120.0)
    assert features["tradability_features"]["limit_down_price"] == pytest.approx(80.0)
    assert features["tradability_features"]["hit_limit_up"] is False

    emit, risk_review, reasons = should_emit_feature_ticket(feature, _feature_config())
    assert emit  # return 3% >= 2%, ratio 3.25 >= 3.0
    assert not risk_review
    assert any("abs(return)" in r for r in reasons)
    assert any("amount_ratio" in r for r in reasons)


def test_market_feature_limit_down_triggers_risk_review(tmp_path: Path):
    store = _store(tmp_path)
    builder = MarketFeatureBuilder(store, _feature_config())
    stock_result = {
        "stdout": {
            "data": {
                "bars": [
                    {"datetime": "2026-06-11T10:30:00+08:00", "open": 85, "close": 80, "high": 85, "low": 80, "amount": 5000, "volume": 60},
                ]
            }
        }
    }
    daily_result = {"stdout": [{"datetime": "2026-06-10", "close": 100}]}
    feature = builder.build_and_save(
        task=_bucket_task(),
        stock_result=stock_result,
        quality={"data_quality": 0.9},
        source_frequency="5m",
        daily_result=daily_result,
    )
    tradability = feature["payload"]["features"]["tradability_features"]
    assert tradability["hit_limit_down"] is True
    emit, risk_review, reasons = should_emit_feature_ticket(feature, _feature_config())
    assert emit and risk_review
    assert "hit_limit_down(risk_review)" in reasons


# ---------------------------------------------------------------------------
# trading calendar
# ---------------------------------------------------------------------------


def test_market_calendar_holidays_and_extra_days():
    cfg = {
        "market_calendar": {"holidays": ["2026-06-11"], "extra_trading_days": ["2026-06-13"]},
        "schedule": {"market_windows": {"morning": ["09:30", "11:30"], "afternoon": ["13:00", "15:00"]}},
    }
    holiday = datetime.fromisoformat("2026-06-11T10:30:00+08:00")  # Thursday but holiday
    weekend_makeup = datetime.fromisoformat("2026-06-13T10:30:00+08:00")  # Saturday but trading
    normal_weekend = datetime.fromisoformat("2026-06-14T10:30:00+08:00")  # Sunday
    assert not is_trading_day(holiday, cfg)
    assert is_trading_day(weekend_makeup, cfg)
    assert not is_trading_day(normal_weekend, cfg)
    assert market_phase(holiday, cfg) == "non_trading_day"
    assert market_phase(weekend_makeup, cfg) == "intraday"


# ---------------------------------------------------------------------------
# message expiry
# ---------------------------------------------------------------------------


def test_expired_messages_are_not_leased_and_get_marked(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    q.publish("t", {"x": 1}, expires_at="2000-01-01T00:00:00+00:00")
    live_id = q.publish("t", {"x": 2}, expires_at="2100-01-01T00:00:00+00:00")
    msg = q.lease(topics=["t"], worker_id="w")
    assert msg.message_id == live_id
    q.ack(msg.message_id)
    with pytest.raises(QueueEmpty):
        q.lease(topics=["t"], worker_id="w")
    assert q.expire_messages() == 1
    assert q.list_messages(status="expired")[0]["payload"] == {"x": 1}


# ---------------------------------------------------------------------------
# cadence profiles
# ---------------------------------------------------------------------------


def test_planner_uses_named_cadence_profile():
    config = {
        "runtime": {"timezone": "Asia/Shanghai"},
        "cadence": {"intraday_bucket_minutes": 10, "held_sellable_intraday_minutes": 10},
        "cadence_profiles": {
            "watchlist_default": {
                "market_snapshot": {"enabled": True, "bucket_size": "60m"},
                "mic_black_swan_scan": {"enabled": False},
            }
        },
    }
    planner = TaskGraphPlanner(config)
    demand = _demand()
    demand["cadence_profile"] = "watchlist_default"
    tasks = planner.plan(demand, request_ticket_id="rt1", as_of="2026-06-11T10:32:00+08:00", market_phase="intraday")
    assert {t["task_type"] for t in tasks} == {"intraday_snapshot_10m"}
    assert tasks[0]["bucket_size"] == "60m"
    # without the profile the black swan scan comes back
    plain = planner.plan(_demand(), request_ticket_id="rt1", as_of="2026-06-11T10:32:00+08:00", market_phase="intraday")
    assert {t["task_type"] for t in plain} == {"intraday_snapshot_10m", "black_swan_scan"}


def test_planner_accepts_injected_targets():
    planner = TaskGraphPlanner({"runtime": {"timezone": "Asia/Shanghai"}, "cadence": {"intraday_bucket_minutes": 10}})
    demand = _demand()
    demand["targets"] = []
    injected = [{"target_type": "ticker", "ticker": "600519.SH", "target_id": "t600519", "pool_layer": "current_holding", "sellability": "sellable"}]
    tasks = planner.plan(demand, request_ticket_id="rt1", as_of="2026-06-11T10:32:00+08:00", market_phase="intraday", targets=injected)
    assert all(t["target"]["ticker"] == "600519.SH" for t in tasks)


# ---------------------------------------------------------------------------
# report: new sections + report message
# ---------------------------------------------------------------------------


def test_daily_report_new_sections_and_message(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    with store.session() as con:
        con.execute(
            "INSERT INTO collection_demands(demand_id, demand_type, source_type, status, priority, payload_json, idempotency_key) "
            "VALUES ('d1', 'intraday_monitoring', 'test', 'active', 'high', '{}', 'k1')"
        )
        con.execute(
            "INSERT INTO collection_tasks(task_id, demand_id, task_type, tool_name, ticker, status, payload_json, idempotency_key, created_at) "
            "VALUES ('t1', 'd1', 'intraday_snapshot_10m', 'stock_data_collector', '300750.SZ', 'failed', '{}', 'tk1', '2026-06-11 02:00:00')"
        )
        con.execute(
            "INSERT INTO collection_runs(run_id, task_id, tool_name, operation, status, request_json, result_ref, quality_json, created_at) "
            "VALUES ('r1', 't1', 'market_intelligence_collector', 'collect_intelligence', 'success', '{}', NULL, ?, '2026-06-11 02:00:00')",
            (dumps_json({"queries_executed": 5, "links_read": 3, "model_calls": 2}),),
        )
        con.execute(
            "INSERT INTO coverage_gaps(gap_id, target_id, ticker, description, priority, status, created_at) "
            "VALUES ('g1', 'company_300750', '300750.SZ', '缺少公告', 'high', 'open', '2026-06-11 03:00:00')"
        )
    q.publish("intelligence.collection", {"ticket_id": "x"}, available_at="2026-06-11 01:00:00")
    with store.session() as con:
        con.execute("UPDATE messages SET created_at='2026-06-11 01:00:00'")

    builder = DailyReportBuilder(data_store=store, bus_store=store, state_store=store, output_dir=tmp_path / "reports", agent_id="test_intel", queue=q)
    result = builder.build(trade_date="2026-06-11")
    summary = result["summary"]
    assert result["message_id"]
    assert q.list_messages(topic="report.collection_daily")

    coverage = {d["demand_id"]: d for d in summary["demand_coverage"]}
    assert coverage["d1"]["tasks_failed"] == 1
    assert summary["message_stats"]
    assert summary["cost_usage"]["mic_budget_usage"] == {"queries_executed": 5, "links_read": 3, "model_calls": 2}
    assert summary["cost_usage"]["total_tool_calls"] == 1
    suggestions = summary["followup_suggestions"]
    assert any("补采覆盖缺口" in s for s in suggestions)
    assert any("重跑失败任务" in s for s in suggestions)

    html = Path(result["html_path"]).read_text(encoding="utf-8")
    for section in ("Demand 覆盖情况", "Message 处理统计", "成本与调用次数", "次日补采建议"):
        assert section in html

    # json-only format skips html
    json_only = builder.build(trade_date="2026-06-12", output_format="json")
    assert json_only["html_path"] is None
    assert json_only["json_path"] is not None
