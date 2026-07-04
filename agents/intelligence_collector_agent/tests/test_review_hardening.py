"""Tests for the reviewer-driven hardening changes (V0.7.1).

Covers: planner MIC-planning acceptance cases, MIC lease heartbeat + hard timeout + run dedup,
local-trading-day statistics, atomic MIC profile writes, batch demand-config update semantics,
calendar validation, structured-event evidence fields and the strengthened MIC quality gate.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import yaml

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.adapters.mic_adapter import MICAdapter
from agent_trade_intel.agent import lease_heartbeat
from agent_trade_intel.config import load_config
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.demand import DemandRegistry
from agent_trade_intel.persistence import ResultPersister
from agent_trade_intel.planner import TaskGraphPlanner
from agent_trade_intel.quality import QualityGate
from agent_trade_intel.request_center import RequestCenter, load_mic_profiles, mic_profiles_lock, save_mic_profiles
from agent_trade_intel.time_utils import local_day_utc_range, validate_market_calendar


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _mic_config_dir(tmp_path: Path) -> Path:
    mic_dir = tmp_path / "mic_config"
    mic_dir.mkdir()
    (mic_dir / "target_profiles.yaml").write_text(
        yaml.safe_dump({"target_profiles": {}}, allow_unicode=True), encoding="utf-8"
    )
    return mic_dir


def _config(tmp_path: Path) -> Path:
    mic_dir = _mic_config_dir(tmp_path)
    cfg = {
        "agent": {"agent_id": "test_intel", "agent_group": "intelligence_collector"},
        "openclaw": {"model": {"primary": "openai/gpt-5.5", "fallbacks": [], "require_registered": False, "allow_openclaw_default": False}},
        "runtime": {"sqlite_path": str(tmp_path / "intel.db"), "workspace_root": str(tmp_path), "log_dir": str(tmp_path / "logs"), "timezone": "Asia/Shanghai"},
        "queue": {"consume_topics": ["intelligence.collection"], "lease_seconds": 30},
        "tools": {
            "python_executable": "python",
            "market_intelligence_collector": {"enabled": True, "config_dir": str(mic_dir), "timeout_seconds": 1},
            "stock_data_collector": {"enabled": True, "config_dir": None, "working_dir": None},
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "intel.db")
    store.init_schema()
    return store


def _planner_config() -> dict:
    return {"runtime": {"timezone": "Asia/Shanghai"}, "cadence": {}, "schedule": {}}


def _daily_demand(targets: list[dict]) -> dict:
    return {"demand_id": "d_daily", "demand_type": "daily_collection", "targets": targets}


# ---------------------------------------------------------------------------
# P0-1 planner acceptance cases (reviewer section 2)
# ---------------------------------------------------------------------------


def test_daily_collection_pre_market_creates_one_mic_per_target():
    demand = _daily_demand(
        [
            {"target_type": "industry", "target_id": "industry_ai_semi", "collect_mic": True},
            {"target_type": "company", "target_id": "company_002371", "ticker": "002371.SZ", "collect_mic": True, "collect_stock": True},
        ]
    )
    tasks = TaskGraphPlanner(_planner_config()).plan(
        demand, request_ticket_id="t1", as_of="2026-07-03T09:00:00+08:00", market_phase="pre_market"
    )
    mic = [t for t in tasks if t["task_type"] == "mic_deep_collect"]
    assert len(mic) == 2  # exactly one per target, never duplicated
    assert not [t for t in tasks if t["task_type"] == "post_close_stock_refresh"]


def test_daily_collection_post_market_creates_one_mic_and_one_stock_for_a_share():
    demand = _daily_demand(
        [
            {"target_type": "company", "target_id": "company_002371", "ticker": "002371.SZ", "collect_mic": True, "collect_stock": True},
            {"target_type": "company", "target_id": "company_hk_00700", "ticker": "0700.HK", "collect_mic": True, "collect_stock": False},
            {"target_type": "industry", "target_id": "industry_ai_semi", "collect_mic": True},
        ]
    )
    tasks = TaskGraphPlanner(_planner_config()).plan(
        demand, request_ticket_id="t1", as_of="2026-07-03T16:30:00+08:00", market_phase="post_market"
    )
    mic = [t["target"]["target_id"] for t in tasks if t["task_type"] == "mic_deep_collect"]
    stock = [t["target"]["target_id"] for t in tasks if t["task_type"] == "post_close_stock_refresh"]
    assert sorted(mic) == ["company_002371", "company_hk_00700", "industry_ai_semi"]
    assert stock == ["company_002371"]  # HK and industry targets get no stock refresh


def test_on_demand_research_creates_one_mic_per_target_only():
    for demand_type in ("on_demand_research", "coverage_gap_followup"):
        demand = {
            "demand_id": f"d_{demand_type}",
            "demand_type": demand_type,
            "targets": [{"target_type": "company", "target_id": "company_002371", "ticker": "002371.SZ"}],
        }
        tasks = TaskGraphPlanner(_planner_config()).plan(
            demand, request_ticket_id="t1", as_of="2026-07-03T16:30:00+08:00", market_phase="post_market"
        )
        assert [t["task_type"] for t in tasks] == ["mic_deep_collect"], demand_type


# ---------------------------------------------------------------------------
# P0-2 lease heartbeat / MIC timeout / run dedup
# ---------------------------------------------------------------------------


def test_lease_heartbeat_extends_lease_during_long_call():
    calls: list[float] = []
    lock = threading.Lock()

    def keepalive() -> None:
        with lock:
            calls.append(time.monotonic())

    with lease_heartbeat(keepalive, interval_seconds=0.05):
        time.sleep(0.3)
    # one immediate call plus several from the background thread
    assert len(calls) >= 3
    # the thread stops after the context exits
    count = len(calls)
    time.sleep(0.15)
    assert len(calls) == count


def test_mic_adapter_hard_timeout_returns_retryable_mic_timeout():
    class SlowAPI:
        def collect_intelligence(self, *, target_id, task_profile):
            time.sleep(5)
            return {}

    adapter = MICAdapter(timeout_seconds=1)
    adapter._api = lambda: SlowAPI()  # noqa: SLF001 - test seam, avoids importing real mic
    result = adapter.collect(target_id="industry_ai_semi", task_profile={})
    assert result.status == "failed"
    assert result.errors[0]["error_code"] == "MIC_TIMEOUT"
    assert result.errors[0]["retryable"] is True
    assert result.quality == {"usable": False}


def test_agent_reuses_successful_mic_run_for_same_idempotency_key(tmp_path: Path):
    from agent_trade_intel.agent import IntelligenceCollectorAgent

    cfg = load_config(_config(tmp_path))
    _store(tmp_path)
    agent = IntelligenceCollectorAgent(cfg)
    task = {"task_id": "task_1", "task_type": "mic_deep_collect", "idempotency_key": "collection_task:d1:mic:industry_x:2026-07-03"}
    assert agent._successful_mic_run(task) is None
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={})
    result.status = "success"
    run_id = ResultPersister(agent.data_store).save_run(task=task, ticket_id="tk1", result=result.finish())
    assert agent._successful_mic_run(task) == run_id
    # failed runs do not short-circuit re-execution
    failed_task = dict(task, idempotency_key="collection_task:d1:mic:industry_y:2026-07-03")
    failed = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={})
    failed.status = "failed"
    ResultPersister(agent.data_store).save_run(task=failed_task, ticket_id="tk2", result=failed.finish())
    assert agent._successful_mic_run(failed_task) is None


# ---------------------------------------------------------------------------
# P1 local trading day
# ---------------------------------------------------------------------------


def test_local_day_utc_range_shanghai():
    start, end = local_day_utc_range("2026-07-04", "Asia/Shanghai")
    assert start == "2026-07-03 16:00:00"
    assert end == "2026-07-04 16:00:00"


def test_dashboard_counts_use_local_day(tmp_path: Path):
    from agent_trade_intel.dashboard import DashboardService

    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path)
    with store.session() as con:
        # 20:00 UTC yesterday-UTC == 04:00 today in Asia/Shanghai when polled before 16:00 UTC.
        # Simplest deterministic case: one event stamped "now" (always in today's local range).
        con.execute(
            "INSERT INTO structured_events(event_id, ticker, event_type, summary_cn, payload_json, idempotency_key) "
            "VALUES ('e1', '300750.SZ', 'risk', '事件', '{}', 'ek1')"
        )
    d = DashboardService(cfg, state_store=store, bus_store=store, data_store=store).overview()
    assert d["today_local"]
    assert d["today_utc"]
    assert d["events_created_today"] == 1


def test_report_counts_local_day_early_morning_rows(tmp_path: Path):
    from agent_trade_intel.reports import DailyReportBuilder

    store = _store(tmp_path)
    with store.session() as con:
        # 01:30 Asia/Shanghai on 2026-07-04 == 17:30 UTC on 2026-07-03: date(created_at) would
        # put it on the wrong day, the UTC-range filter must count it for 2026-07-04.
        con.execute(
            "INSERT INTO structured_events(event_id, ticker, event_type, summary_cn, payload_json, idempotency_key, created_at) "
            "VALUES ('e1', '300750.SZ', 'risk', '凌晨事件', '{}', 'ek1', '2026-07-03 17:30:00')"
        )
    builder = DailyReportBuilder(
        data_store=store, bus_store=store, state_store=store, output_dir=tmp_path / "reports",
        agent_id="test_intel", timezone="Asia/Shanghai",
    )
    summary = builder.build(trade_date="2026-07-04", output_format="json")["summary"]
    assert summary["events_created"] == 1
    previous = builder.build(trade_date="2026-07-03", output_format="json")["summary"]
    assert previous["events_created"] == 0


# ---------------------------------------------------------------------------
# P1 calendar validation
# ---------------------------------------------------------------------------


def test_validate_market_calendar_flags_missing_year():
    empty = validate_market_calendar({"market_calendar": {"holidays": [], "extra_trading_days": []}}, 2026)
    assert empty["status"] == "warning"
    assert empty["holidays_in_year"] == 0
    ok = validate_market_calendar(
        {"market_calendar": {"holidays": ["2026-01-01", "2026-02-17"], "extra_trading_days": ["2026-02-15"]}}, 2026
    )
    assert ok["status"] == "ok"
    assert ok["holidays_in_year"] == 2
    assert ok["extra_trading_days_in_year"] == 1
    bad = validate_market_calendar({"market_calendar": {"holidays": ["not-a-date", "2026-01-01"]}}, 2026)
    assert bad["status"] == "warning"
    assert any("invalid" in w for w in bad["warnings"])


# ---------------------------------------------------------------------------
# P1 atomic MIC profile writes + lock
# ---------------------------------------------------------------------------


def test_save_mic_profiles_is_atomic_and_leaves_no_temp_files(tmp_path: Path):
    mic_dir = _mic_config_dir(tmp_path)
    save_mic_profiles(mic_dir, {"industry_x": {"target_id": "industry_x", "type": "industry"}})
    profiles = load_mic_profiles(mic_dir)
    assert "industry_x" in profiles
    leftovers = [p for p in mic_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_concurrent_profile_upserts_keep_both_targets(tmp_path: Path):
    mic_dir = _mic_config_dir(tmp_path)

    def upsert(target_id: str) -> None:
        with mic_profiles_lock(mic_dir):
            profiles = load_mic_profiles(mic_dir)
            profiles[target_id] = {"target_id": target_id, "type": "industry"}
            time.sleep(0.05)  # widen the race window
            save_mic_profiles(mic_dir, profiles)

    threads = [threading.Thread(target=upsert, args=(f"industry_{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    profiles = load_mic_profiles(mic_dir)
    assert {f"industry_{i}" for i in range(4)} <= set(profiles)


# ---------------------------------------------------------------------------
# P1 batch --update-demand-config semantics
# ---------------------------------------------------------------------------


def _center(tmp_path: Path) -> tuple[RequestCenter, SQLiteStore]:
    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path)
    return RequestCenter(cfg, data_store=store, bus_store=store), store


def _spec(max_queries: int) -> dict:
    return {
        "demands": {
            "demand_industry_research_daily": {
                "kind": "industry",
                "priority": "high",
                "task_profile": {"mic": {"enabled": True, "budget_profile": {"max_queries": max_queries}}},
            }
        },
        "industries": [{"name": "AI算力", "target_id": "industry_ai_semi"}],
    }


def test_batch_rerun_skips_demand_overrides_without_flag(tmp_path: Path):
    center, store = _center(tmp_path)
    first = center.request_batch(_spec(max_queries=4))
    assert first["demands"][0]["demand_config_updated"] is True  # applied on creation
    assert first["warnings"] == []

    second = center.request_batch(_spec(max_queries=99))
    assert second["demands"][0]["demand_config_updated"] is False
    assert any("--update-demand-config" in w for w in second["warnings"])
    demand = DemandRegistry(store).get("demand_industry_research_daily")
    assert demand["task_profile"]["mic"]["budget_profile"]["max_queries"] == 4  # unchanged


def test_batch_rerun_applies_demand_overrides_with_flag(tmp_path: Path):
    center, store = _center(tmp_path)
    center.request_batch(_spec(max_queries=4))
    third = center.request_batch(_spec(max_queries=99), update_demand_config=True)
    assert third["demands"][0]["demand_config_updated"] is True
    assert third["warnings"] == []
    demand = DemandRegistry(store).get("demand_industry_research_daily")
    assert demand["task_profile"]["mic"]["budget_profile"]["max_queries"] == 99
    assert demand["priority"] == "high"
    assert demand["current_version"] == 2  # version bumped so runtime picks the change up


# ---------------------------------------------------------------------------
# P1 event evidence fields + strengthened MIC quality gate
# ---------------------------------------------------------------------------


def _mic_result(top_events: list[dict], links_read: int = 3) -> ToolResult:
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={})
    result.status = "success"
    result.result = {
        "search_run_id": "run_x",
        "summary": {"links_read": links_read, "model_calls": 2},
        "top_events": top_events,
    }
    return result.finish()


def test_persister_saves_event_evidence_fields(tmp_path: Path):
    store = _store(tmp_path)
    persister = ResultPersister(store)
    result = _mic_result(
        [
            {
                "summary": "中标重大合同",
                "event_type": "order_win",
                "event_date": "2026-07-03",
                "confidence": 0.9,
                "source": {
                    "url": "https://www.sse.com.cn/notice/1.html",
                    "domain": "sse.com.cn",
                    "source_type": "exchange",
                    "published_at": "2026-07-03T10:00:00+08:00",
                },
            }
        ]
    )
    task = {"task_id": "t1", "target": {"target_id": "company_002371", "ticker": "002371.SZ"}, "idempotency_key": "k1"}
    saved = persister.save_mic_structures(task=task, result=result)
    assert saved["events"] == 1
    with store.session() as con:
        row = con.execute("SELECT * FROM structured_events").fetchone()
    assert row["source_url"] == "https://www.sse.com.cn/notice/1.html"
    assert row["source_domain"] == "sse.com.cn"
    assert row["source_type"] == "exchange"
    assert row["published_at"] == "2026-07-03T10:00:00+08:00"
    assert row["retrieved_at"]


def test_quality_gate_flags_high_priority_zero_events():
    gate = QualityGate({"quality": {}})
    q = gate.evaluate(_mic_result([]), context={"priority": "high"})
    assert q["decision"] == "accept_degraded"
    assert q["severity"] == "P2"
    assert any(i["issue_type"] == "high_priority_zero_events" for i in q["issues"])
    # normal priority with zero events is not flagged
    q2 = gate.evaluate(_mic_result([]), context={"priority": "normal"})
    assert q2["decision"] == "accept"


def test_quality_gate_flags_missing_source_url_and_weak_sources():
    gate = QualityGate({"quality": {}})
    no_url = gate.evaluate(_mic_result([{"summary": "事件A", "event_type": "risk"}]), context={})
    assert any(i["issue_type"] == "events_missing_source_url" for i in no_url["issues"])
    weak = gate.evaluate(
        _mic_result([{"summary": "事件B", "source": {"url": "https://weibo.com/x", "source_type": "social"}}]),
        context={},
    )
    assert any(i["issue_type"] == "low_authority_sources_only" for i in weak["issues"])
    strong = gate.evaluate(
        _mic_result(
            [{"summary": "事件C", "source": {"url": "https://www.sse.com.cn/x", "source_type": "exchange"}}]
        ),
        context={},
    )
    assert strong["decision"] == "accept"
    assert strong["issues"] == []


def test_quality_gate_checks_configurable_off():
    gate = QualityGate(
        {"quality": {"mic": {"flag_high_priority_zero_events": False, "require_source_url": False, "flag_low_authority_sources": False}}}
    )
    q = gate.evaluate(_mic_result([{"summary": "无来源事件"}]), context={"priority": "high"})
    assert q["decision"] == "accept"
