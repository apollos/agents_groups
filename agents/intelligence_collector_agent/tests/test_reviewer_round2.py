"""Tests for the second reviewer round (V0.7.2).

Covers: populated 2026 A-share trading calendar, HK ticker normalization, research-pool
target metadata (industry_id / tracking_variables), periodic-review demand cadence,
query_family propagation into structured_events and the published_at quality rule.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.config import load_config
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.demand import DemandCompiler, DemandRegistry, cadence_due
from agent_trade_intel.persistence import ResultPersister
from agent_trade_intel.quality import QualityGate
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.request_center import RequestCenter, normalize_ticker
from agent_trade_intel.tickets import TicketRepository
from agent_trade_intel.time_utils import is_trading_day, parse_dt, validate_market_calendar

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "intelligence_collector.yaml"


# ---------------------------------------------------------------------------
# fixtures (mirrors test_review_hardening)
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> Path:
    mic_dir = tmp_path / "mic_config"
    mic_dir.mkdir()
    (mic_dir / "target_profiles.yaml").write_text(
        yaml.safe_dump({"target_profiles": {}}, allow_unicode=True), encoding="utf-8"
    )
    cfg = {
        "agent": {"agent_id": "test_intel", "agent_group": "intelligence_collector"},
        "openclaw": {"model": {"primary": "openai/gpt-5.5", "fallbacks": [], "require_registered": False, "allow_openclaw_default": False}},
        "runtime": {"sqlite_path": str(tmp_path / "intel.db"), "workspace_root": str(tmp_path), "log_dir": str(tmp_path / "logs"), "timezone": "Asia/Shanghai"},
        "queue": {"consume_topics": ["intelligence.collection"], "lease_seconds": 30},
        "tools": {
            "python_executable": "python",
            "market_intelligence_collector": {"enabled": True, "config_dir": str(mic_dir)},
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


def _center(tmp_path: Path) -> tuple[RequestCenter, SQLiteStore]:
    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path)
    return RequestCenter(cfg, data_store=store, bus_store=store), store


# ---------------------------------------------------------------------------
# P0 2026 trading calendar populated (reviewer 2.4)
# ---------------------------------------------------------------------------


def test_repo_config_has_2026_a_share_calendar():
    raw = yaml.safe_load(REPO_CONFIG.read_text(encoding="utf-8"))
    check = validate_market_calendar(raw, 2026)
    assert check["status"] == "ok"
    assert check["holidays_in_year"] >= 15
    # Spring Festival Tuesday and National Day Thursday are weekday holidays, not trading days.
    assert not is_trading_day(parse_dt("2026-02-17T10:00:00+08:00"), raw)
    assert not is_trading_day(parse_dt("2026-10-01T10:00:00+08:00"), raw)
    # Trading resumes 10/8; make-up working Saturday 10/10 stays a non-trading day for A-shares.
    assert is_trading_day(parse_dt("2026-10-08T10:00:00+08:00"), raw)
    assert not is_trading_day(parse_dt("2026-10-10T10:00:00+08:00"), raw)


# ---------------------------------------------------------------------------
# P1 HK ticker normalization + research metadata on targets (reviewer 3.1 / 4.5)
# ---------------------------------------------------------------------------


def test_normalize_ticker_pads_hk_codes():
    assert normalize_ticker("0700.HK") == "00700.HK"
    assert normalize_ticker("9988.hk") == "09988.HK"
    assert normalize_ticker("00700.HK") == "00700.HK"
    assert normalize_ticker("600519.SH") == "600519.SH"
    assert normalize_ticker(None) is None


def test_company_target_carries_research_metadata(tmp_path: Path):
    center, _store_ = _center(tmp_path)
    center.request_company(
        name="腾讯控股",
        ticker="0700.HK",
        industry_id="industry_internet_consumer",
        tracking_variables="southbound_holding,buyback,margin",
    )
    demand = center.registry.get("demand_company_research_daily")
    target = demand["targets"][0]
    assert target["ticker"] == "00700.HK"  # normalized to 5-digit HK form
    assert target["target_id"] == "company_hk_00700"
    assert target["industry_id"] == "industry_internet_consumer"
    assert target["tracking_variables"] == ["southbound_holding", "buyback", "margin"]
    assert target["collect_stock"] is False


def test_research_pool_full_yaml_has_industry_ids_and_padded_hk_tickers():
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "examples" / "research_pool_full.yaml").read_text(encoding="utf-8")
    )
    companies = spec["companies"]
    assert companies, "example spec should list companies"
    assert all(c.get("industry_id") for c in companies)
    hk = [str(c["ticker"]) for c in companies if str(c.get("ticker", "")).endswith(".HK")]
    assert hk and all(len(t.split(".")[0]) == 5 for t in hk)


# ---------------------------------------------------------------------------
# P1 periodic-review cadence (reviewer 4.3)
# ---------------------------------------------------------------------------


def test_cadence_due_daily_weekly_monthly_quarterly():
    assert cadence_due({}, "2026-07-01T09:00:00+08:00")  # no cadence == daily
    weekly = {"cadence": "weekly", "timezone": "Asia/Shanghai"}
    assert cadence_due(weekly, "2026-07-03T09:00:00+08:00")  # Friday (default anchor)
    assert not cadence_due(weekly, "2026-07-02T09:00:00+08:00")  # Thursday
    assert cadence_due({"cadence": "weekly", "cadence_anchor": "mon"}, "2026-07-06T09:00:00+08:00")
    monthly = {"cadence": "monthly"}
    assert cadence_due(monthly, "2026-07-01T09:00:00+08:00")
    assert not cadence_due(monthly, "2026-07-02T09:00:00+08:00")
    quarterly = {"cadence": "quarterly", "cadence_anchor": 15}
    assert cadence_due(quarterly, "2026-07-15T09:00:00+08:00")
    assert not cadence_due(quarterly, "2026-08-15T09:00:00+08:00")  # not a quarter month


def test_compiler_skips_weekly_demand_until_due(tmp_path: Path):
    store = _store(tmp_path)
    queue = SQLiteMessageQueue(store)
    tickets = TicketRepository(store)
    registry = DemandRegistry(store, queue)
    registry.register(
        {
            "schema_version": "demand.v1",
            "demand_id": "demand_industry_weekly_review",
            "demand_type": "periodic_review",
            "source_type": "research_pool_request",
            "status": "active",
            "cadence": "weekly",
            "cadence_anchor": "fri",
            "timezone": "Asia/Shanghai",
            "targets": [{"target_type": "industry", "target_id": "industry_ai_semi", "collect_mic": True}],
        }
    )
    compiler = DemandCompiler(store, queue, tickets, "test_intel", "intelligence_collector")
    demand = registry.get("demand_industry_weekly_review")
    assert compiler.compile_demand(demand, as_of="2026-07-02T16:30:00+08:00", market_phase="post_market") == []
    created = compiler.compile_demand(demand, as_of="2026-07-03T16:30:00+08:00", market_phase="post_market")
    assert len(created) == 2  # ticket + message on the due Friday


def test_batch_registers_periodic_review_demand(tmp_path: Path):
    center, _store_ = _center(tmp_path)
    spec = {
        "demands": {
            "demand_industry_weekly_review": {
                "kind": "industry",
                "demand_type": "periodic_review",
                "cadence": "weekly",
                "cadence_anchor": "fri",
                "task_profile": {"mic": {"enabled": True, "time_window": "7d"}},
            }
        },
        "industries": [{"name": "AI算力", "target_id": "industry_ai_semi", "demand_id": "demand_industry_weekly_review"}],
    }
    out = center.request_batch(spec)
    assert out["demands"][0]["demand_config_updated"] is True
    demand = center.registry.get("demand_industry_weekly_review")
    assert demand["demand_type"] == "periodic_review"
    assert demand["cadence"] == "weekly"
    assert demand["task_profile"]["mic"]["time_window"] == "7d"


# ---------------------------------------------------------------------------
# P1 query_family propagation + published_at quality rule (reviewer 3.2 / 3.3)
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


def test_persister_saves_query_family(tmp_path: Path):
    store = _store(tmp_path)
    result = _mic_result(
        [
            {
                "summary": "中标特高压项目",
                "event_type": "order_win",
                "event_date": "2026-07-03",
                "confidence": 0.9,
                "source": {
                    "url": "https://www.example.com/n/1.html",
                    "domain": "example.com",
                    "source_type": "exchange",
                    "published_at": "2026-07-03T10:00:00+08:00",
                    "query_family": "early_signal",
                },
            }
        ]
    )
    task = {"task_id": "t1", "target": {"target_id": "company_600406", "ticker": "600406.SH"}, "idempotency_key": "k1"}
    assert ResultPersister(store).save_mic_structures(task=task, result=result)["events"] == 1
    with store.session() as con:
        row = con.execute("SELECT query_family, source_type FROM structured_events").fetchone()
    assert row["query_family"] == "early_signal"
    assert row["source_type"] == "exchange"


def test_migration_v4_adds_query_family_to_v3_database(tmp_path: Path):
    """A database created at V0.7.1 (v3 schema, no query_family) upgrades in place."""
    import sqlite3

    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE structured_events (
          event_id TEXT PRIMARY KEY,
          schema_version TEXT NOT NULL DEFAULT 'structured_event.v1',
          target_id TEXT, ticker TEXT, company_name TEXT,
          event_type TEXT NOT NULL, event_subtype TEXT, event_date TEXT,
          summary_cn TEXT NOT NULL,
          impact_json TEXT NOT NULL DEFAULT '{}',
          source_refs_json TEXT NOT NULL DEFAULT '[]',
          source_level TEXT, source_url TEXT, source_domain TEXT, source_type TEXT,
          published_at TEXT, retrieved_at TEXT,
          confidence REAL, data_quality REAL,
          source_corroboration_status TEXT, source_run_id TEXT,
          payload_json TEXT NOT NULL DEFAULT '{}',
          idempotency_key TEXT UNIQUE,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    con.close()
    store = SQLiteStore(db_path)
    store.init_schema()
    with store.session() as con:
        cols = {row["name"] for row in con.execute("PRAGMA table_info(structured_events)")}
        versions = {row["version"] for row in con.execute("SELECT version FROM schema_migrations")}
    assert "query_family" in cols
    assert 4 in versions


def test_quality_flags_high_confidence_event_missing_published_at():
    gate = QualityGate({})
    event = {
        "summary": "重大合同",
        "confidence": 0.9,
        "source": {"url": "https://example.com/1", "source_type": "exchange"},
    }
    verdict = gate.evaluate(_mic_result([event]), context={"priority": "normal"})
    issue_types = {i["issue_type"] for i in verdict["issues"]}
    assert verdict["decision"] == "accept_degraded"
    assert "high_confidence_missing_published_at" in issue_types

    dated = dict(event, source=dict(event["source"], published_at="2026-07-03T10:00:00+08:00"))
    verdict_ok = gate.evaluate(_mic_result([dated]), context={"priority": "normal"})
    assert "high_confidence_missing_published_at" not in {i["issue_type"] for i in verdict_ok["issues"]}
    # low-confidence events are exempt from the published_at requirement
    low = dict(event, confidence=0.3)
    verdict_low = gate.evaluate(_mic_result([low]), context={"priority": "normal"})
    assert "high_confidence_missing_published_at" not in {i["issue_type"] for i in verdict_low["issues"]}
