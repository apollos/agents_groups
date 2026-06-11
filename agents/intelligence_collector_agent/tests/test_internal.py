from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_trade_intel.circuit_breaker import CircuitBreaker
from agent_trade_intel.config import load_config
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.demand import DemandCompiler, DemandRegistry
from agent_trade_intel.errors import ValidationError
from agent_trade_intel.market_features import MarketFeatureBuilder
from agent_trade_intel.planner import TaskGraphPlanner
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.recovery import RecoveryManager
from agent_trade_intel.reports import DailyReportBuilder
from agent_trade_intel.tickets import TicketRepository


def _config(tmp_path: Path) -> Path:
    cfg = {
        "agent": {"agent_id": "test_intel", "agent_group": "intelligence_collector"},
        "openclaw": {"model": {"primary": "openai/gpt-5.5", "fallbacks": [], "require_registered": False, "allow_openclaw_default": False}},
        "runtime": {"sqlite_path": str(tmp_path / "intel.db"), "workspace_root": str(tmp_path), "log_dir": str(tmp_path / "logs"), "timezone": "Asia/Shanghai"},
        "queue": {"consume_topics": ["intelligence.collection"], "lease_seconds": 30},
        "cadence": {"intraday_bucket_minutes": 10},
        "tools": {"python_executable": "python", "market_intelligence_collector": {"enabled": True}, "stock_data_collector": {"enabled": True, "config_dir": "config", "working_dir": None}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def _demand() -> dict:
    return {
        "schema_version": "demand.v1",
        "demand_id": "d1",
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
        "idempotency_key": "demand:d1",
    }


def _store(tmp_path: Path, name: str = "t.db") -> SQLiteStore:
    store = SQLiteStore(tmp_path / name)
    store.init_schema()
    return store


def test_demand_compile_creates_ticket_and_message(tmp_path: Path):
    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path, "intel.db")
    reg = DemandRegistry(store)
    q = SQLiteMessageQueue(store)
    tickets = TicketRepository(store)
    compiler = DemandCompiler(store, q, tickets, cfg.runtime.agent_id, cfg.runtime.agent_group)
    reg.register(_demand(), activate=True)
    created = compiler.compile_active_demands(reg, as_of="2026-06-11T10:30:00+08:00", market_phase="intraday")
    assert created
    msgs = q.list_messages(status="open")
    assert len(msgs) == 1
    assert msgs[0]["payload"]["ticket_type"] == "COLLECTION_REQUEST_TICKET"


def test_queue_lease_ack(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    msg_id = q.publish("t", {"x": 1}, idempotency_key="k1")
    assert q.publish("t", {"x": 1}, idempotency_key="k1") == msg_id
    msg = q.lease(topics=["t"], worker_id="w")
    assert msg.payload == {"x": 1}
    q.ack(msg.message_id)
    assert q.list_messages(status="done")[0]["message_id"] == msg_id


def test_queue_nack_retry_then_dead(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    q.publish("t", {"x": 1}, max_attempts=2)
    msg = q.lease(topics=["t"], worker_id="w")
    q.nack(msg.message_id, {"error_code": "E1"}, retryable=True, retry_delay_seconds=0)
    assert q.inspect(msg.message_id)["status"] == "open"
    msg = q.lease(topics=["t"], worker_id="w")
    q.nack(msg.message_id, {"error_code": "E2"}, retryable=True, retry_delay_seconds=0)
    assert q.inspect(msg.message_id)["status"] == "dead"
    # operator retry requeues a dead letter
    assert q.retry_message(msg.message_id)
    assert q.inspect(msg.message_id)["status"] == "open"


def test_queue_nack_non_retryable_goes_dead(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    q.publish("t", {"x": 1})
    msg = q.lease(topics=["t"], worker_id="w")
    q.nack(msg.message_id, {"error_code": "FATAL"}, retryable=False)
    assert q.inspect(msg.message_id)["status"] == "dead"


def test_queue_extend_lease(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    q.publish("t", {"x": 1})
    msg = q.lease(topics=["t"], worker_id="w", lease_seconds=10)
    before = q.inspect(msg.message_id)["lease_until"]
    q.extend_lease(msg.message_id, "w", 3600)
    after = q.inspect(msg.message_id)["lease_until"]
    assert after > before
    # another worker cannot extend
    q.extend_lease(msg.message_id, "other", 7200)
    assert q.inspect(msg.message_id)["lease_until"] == after


def test_queue_target_filtering(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    q.publish("t", {"to": "other"}, target_agent_id="other_agent")
    q.publish("t", {"to": "group"}, target_agent_group="g1")
    msg = q.lease(topics=["t"], worker_id="w", target_agent_id="me", target_agent_group="g1")
    assert msg.payload == {"to": "group"}
    # the message addressed to other_agent must not be leased by us
    from agent_trade_intel.errors import QueueEmpty

    with pytest.raises(QueueEmpty):
        q.lease(topics=["t"], worker_id="w", target_agent_id="me", target_agent_group="g1")


def test_demand_lifecycle(tmp_path: Path):
    store = _store(tmp_path)
    reg = DemandRegistry(store)
    reg.register(_demand(), activate=True)
    res = reg.apply_lifecycle("d1", "suspend")
    assert res["new_status"] == "suspended"
    assert reg.get("d1")["status"] == "suspended"
    assert reg.active(as_of="2026-06-11") == []
    reg.apply_lifecycle("d1", "resume")
    assert reg.get("d1")["status"] == "active"
    reg.apply_lifecycle("d1", "cancel")
    assert reg.get("d1")["status"] == "cancelled"
    with pytest.raises(ValidationError):
        reg.apply_lifecycle("d1", "suspend")


def test_planner_cadence_throttles_black_swan(tmp_path: Path):
    config = {
        "runtime": {"timezone": "Asia/Shanghai"},
        "cadence": {
            "intraday_bucket_minutes": 10,
            "held_sellable_intraday_minutes": 10,
            "black_swan_held_sellable_minutes": 60,
        },
    }
    planner = TaskGraphPlanner(config)
    demand = _demand()

    def keys(as_of: str) -> dict[str, str]:
        tasks = planner.plan(demand, request_ticket_id="rt1", as_of=as_of, market_phase="intraday")
        return {t["task_type"]: t["idempotency_key"] for t in tasks}

    k1 = keys("2026-06-11T10:32:00+08:00")
    k2 = keys("2026-06-11T10:48:00+08:00")
    # black swan dedupes within its 60m bucket; snapshot moves to the next 10m bucket
    assert k1["black_swan_scan"] == k2["black_swan_scan"]
    assert k1["intraday_snapshot_10m"] != k2["intraday_snapshot_10m"]
    k3 = keys("2026-06-11T11:05:00+08:00")
    assert k3["black_swan_scan"] != k1["black_swan_scan"]


def test_recovery_acks_completed_and_faults_dead_letters(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    tickets = TicketRepository(store)
    recovery = RecoveryManager(store, q, tickets, "test_intel", "intelligence_collector")

    # message whose ticket already finished -> should be acked
    done_ticket = tickets.create_ticket(ticket_type="COLLECTION_TASK_TICKET", source_agent="a", summary_cn="t")
    tickets.update_status(done_ticket, "done")
    done_msg = q.publish("intelligence.collection", {"ticket_id": done_ticket})

    # dead letter -> should get a FAULT_TICKET
    q.publish("t", {"ticket_id": "none"})
    dead = q.lease(topics=["t"], worker_id="w")
    q.nack(dead.message_id, {"error_code": "E"}, retryable=False)

    summary = recovery.recover()
    assert done_msg in summary["acked_completed"]
    assert len(summary["fault_tickets_for_dead_letters"]) == 1
    # recovery is idempotent: same fault ticket on second run
    assert recovery.recover()["fault_tickets_for_dead_letters"] == summary["fault_tickets_for_dead_letters"]


def test_recovery_republishes_orphan_task_ticket(tmp_path: Path):
    store = _store(tmp_path)
    q = SQLiteMessageQueue(store)
    tickets = TicketRepository(store)
    recovery = RecoveryManager(store, q, tickets, "test_intel", "intelligence_collector")
    orphan = tickets.create_ticket(ticket_type="COLLECTION_TASK_TICKET", source_agent="a", summary_cn="orphan")
    assert q.list_messages() == []
    recovery.recover()
    msgs = q.list_messages(status="open")
    assert len(msgs) == 1
    assert msgs[0]["payload"]["ticket_id"] == orphan


def test_circuit_breaker_opens_and_recovers(tmp_path: Path):
    store = _store(tmp_path)
    breaker = CircuitBreaker(store, {"circuit_breaker": {"failure_threshold": 2, "cooldown_seconds": 0}})
    assert breaker.allow("toolx")
    assert not breaker.record_failure("toolx")
    assert breaker.record_failure("toolx")  # threshold reached -> open
    # cooldown 0 -> immediately half_open probe allowed
    assert breaker.allow("toolx")
    breaker.record_success("toolx")
    state = breaker.state("toolx")
    assert state["status"] == "closed"
    assert state["consecutive_failures"] == 0


def test_config_resolves_paths_against_workspace_root(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    cfg_file = config_dir / "intel.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "agent": {"agent_id": "a", "agent_group": "g"},
                "openclaw": {"model": {"primary": "openai/gpt-5.5"}},
                "runtime": {"workspace_root": ".."},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.runtime.state_sqlite_path == (tmp_path / "data" / "intelligence_collector_state.db").resolve()
    assert cfg.runtime.bus_sqlite_path == (tmp_path / "data" / "ticket_bus.db").resolve()
    assert cfg.runtime.data_sqlite_path == (tmp_path / "data" / "intelligence_collector_data.db").resolve()
    assert cfg.runtime.log_dir == (tmp_path / "logs").resolve()
    assert cfg.runtime.reports_dir == (tmp_path / "reports").resolve()


def test_market_feature_bucket_filter_and_degraded(tmp_path: Path):
    store = _store(tmp_path)
    builder = MarketFeatureBuilder(store, {"cadence": {"intraday_bucket_minutes": 10}})
    task = {
        "target": {"ticker": "300750.SZ", "target_id": "company_300750"},
        "bucket_start": "2026-06-11T10:30:00+08:00",
        "bucket_end": "2026-06-11T10:40:00+08:00",
        "bucket_size": "10m",
        "as_of": "2026-06-11T10:32:00+08:00",
    }
    stock_result = {
        "stdout": {
            "request_id": "req_1",
            "data": {
                "bars": [
                    {"datetime": "2026-06-11T10:30:00+08:00", "open": 100, "close": 101, "amount": 1000, "volume": 10},
                    {"datetime": "2026-06-11T10:35:00+08:00", "open": 101, "close": 102, "amount": 1200, "volume": 11},
                    {"datetime": "2026-06-11T11:00:00+08:00", "open": 102, "close": 110, "amount": 9999, "volume": 99},
                ]
            },
        }
    }
    feature = builder.build_and_save(task=task, stock_result=stock_result, quality={"data_quality": 0.9}, source_frequency="15m")
    payload = feature["payload"]
    # the 11:00 bar is outside the bucket and must be excluded
    assert payload["features"]["price_features"]["close"] == 102
    assert payload["degraded_from"] == "10m_to_15m"
    # idempotent: same bucket returns the same feature_id
    again = builder.build_and_save(task=task, stock_result=stock_result, quality={"data_quality": 0.9}, source_frequency="15m")
    assert again["feature_id"] == feature["feature_id"]


def test_daily_report_contains_new_sections(tmp_path: Path):
    store = _store(tmp_path)
    out = tmp_path / "reports"
    builder = DailyReportBuilder(data_store=store, bus_store=store, state_store=store, output_dir=out, agent_id="test_intel")
    result = builder.build(trade_date="2026-06-11")
    assert Path(result["html_path"]).exists()
    assert Path(result["json_path"]).exists()
    for key in ("top_events", "top_market_features", "fault_tickets", "tool_capability_checks", "circuit_breakers"):
        assert key in result["summary"]


def test_logging_writes_to_log_dir(tmp_path: Path):
    import agent_trade_intel.logging_setup as ls

    # reset module state so this test controls the handler target
    ls._configured = False
    logger = ls.setup_logging(tmp_path / "logs", level="INFO")
    for h in list(logger.handlers):
        h.flush()
    ls.get_logger("test").info("hello logs")
    log_files = list((tmp_path / "logs").glob("intelligence_collector.log*"))
    assert log_files
    assert "hello logs" in log_files[0].read_text(encoding="utf-8")



def test_config_rejects_openclaw_placeholder(tmp_path: Path):
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "agent": {"agent_id": "a", "agent_group": "g"},
                "openclaw": {"model": {"primary": "REPLACE_WITH_REGISTERED_OPENCLAW_MODEL"}},
                "runtime": {"workspace_root": str(tmp_path)},
            }
        ),
        encoding="utf-8",
    )
    from agent_trade_intel.errors import ConfigError

    with pytest.raises(ConfigError):
        load_config(cfg_file)


def test_tool_paths_resolve_against_workspace_root(tmp_path: Path):
    cfg_file = tmp_path / "intel.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "agent": {"agent_id": "a", "agent_group": "g"},
                "openclaw": {"model": {"primary": "openai/gpt-5.5"}},
                "runtime": {"workspace_root": str(tmp_path)},
                "tools": {
                    "market_intelligence_collector": {"config_dir": "mic_config"},
                    "stock_data_collector": {"config_dir": "stock_config", "working_dir": "stock_root"},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_file)
    assert cfg.tools.mic_config_dir == str((tmp_path / "mic_config").resolve())
    assert cfg.tools.stock_config_dir == str((tmp_path / "stock_config").resolve())
    assert cfg.tools.stock_working_dir == str((tmp_path / "stock_root").resolve())


def test_planner_skips_intraday_snapshot_outside_intraday_phase():
    planner = TaskGraphPlanner({"runtime": {"timezone": "Asia/Shanghai"}, "schedule": {"allow_lunch_break_black_swan": True}})
    tasks = planner.plan(_demand(), request_ticket_id="rt1", as_of="2026-06-11T12:10:00+08:00", market_phase="lunch_break")
    assert {t["task_type"] for t in tasks} == {"black_swan_scan"}
    weekend = planner.plan(_demand(), request_ticket_id="rt1", as_of="2026-06-13T10:30:00+08:00", market_phase="non_trading_day")
    assert weekend == []


def test_quality_gate_enforces_minimum_quality():
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.quality import QualityGate

    result = ToolResult(tool_name="stock_data_collector", operation="fetch", request={})
    result.status = "success"
    result.quality = {"usable": True, "data_quality": 0.5, "conflicts": [], "status": "success"}
    q = QualityGate({"quality": {"minimum_quality_for_public_pool": 0.8}}).evaluate(result)
    assert q["decision"] == "accept_degraded"
    assert q["usable"] is False
