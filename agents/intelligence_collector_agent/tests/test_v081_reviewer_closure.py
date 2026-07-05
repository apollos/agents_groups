"""Tests for the V0.8.1 reviewer round.

Covers: HK snapshot field-completeness (adapter quality + persistence + eval), the
market_context_collector chain (adapter / planner / persistence / eval / request batch),
deterministic research cards (builder + export demand filter), and the v6 column migration
for databases created before the completeness columns existed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.adapters.hk_connect_adapter import HK_REQUIRED_FIELDS, HKConnectAdapter
from agent_trade_intel.adapters.market_context_adapter import MarketContextAdapter
from agent_trade_intel.config import load_config
from agent_trade_intel.db import SQLiteStore, loads_json
from agent_trade_intel.demand import DemandRegistry
from agent_trade_intel.evaluation import CoverageEvaluator
from agent_trade_intel.persistence import ResultPersister
from agent_trade_intel.planner import TaskGraphPlanner
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.request_center import RequestCenter
from agent_trade_intel.research_cards import ResearchCardBuilder

# ---------------------------------------------------------------------------
# fixtures (mirrors test_v08_research_loop)
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
            "hk_connect_collector": {"enabled": True, "provider": "akshare"},
            "market_context_collector": {"enabled": True, "provider": "akshare"},
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
# v6 migration: completeness columns + new tables
# ---------------------------------------------------------------------------


def test_v6_new_tables_and_columns_on_fresh_database(tmp_path: Path):
    store = _store(tmp_path)
    with store.session() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        hk_cols = {r["name"] for r in con.execute("PRAGMA table_info(hk_connect_snapshots)")}
        versions = {r["version"] for r in con.execute("SELECT version FROM schema_migrations")}
    assert {"market_context_snapshots", "research_cards"} <= tables
    assert {"field_completeness_json", "missing_fields_json", "provider_status_json"} <= hk_cols
    assert 6 in versions


def test_v6_adds_completeness_columns_to_old_database(tmp_path: Path):
    """A database whose hk_connect_snapshots predates V0.8.1 gains the columns in place."""
    store = SQLiteStore(tmp_path / "old.db")
    with store.session() as con:
        con.execute(
            """
            CREATE TABLE hk_connect_snapshots (
              snapshot_id TEXT PRIMARY KEY,
              target_id TEXT, ticker TEXT NOT NULL, company_name TEXT, as_of TEXT NOT NULL,
              hk_connect_eligible INTEGER, last_price_hkd REAL, turnover_hkd REAL,
              southbound_holding_shares REAL, southbound_holding_market_value_hkd REAL,
              southbound_holding_pct REAL, southbound_mv_change_1d REAL,
              southbound_mv_change_5d REAL, southbound_mv_change_10d REAL,
              buyback_amount_hkd REAL, ah_premium_pct REAL, hk_liquidity_score REAL,
              source_url TEXT, payload_json TEXT NOT NULL DEFAULT '{}',
              idempotency_key TEXT UNIQUE,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        con.execute(
            "INSERT INTO hk_connect_snapshots(snapshot_id, ticker, as_of, idempotency_key) "
            "VALUES ('hkc_old', '00700.HK', '2026-07-01', 'k_old')"
        )
    store.init_schema()
    with store.session() as con:
        row = con.execute("SELECT * FROM hk_connect_snapshots WHERE snapshot_id='hkc_old'").fetchone()
    assert row["field_completeness_json"] == "{}"
    assert row["missing_fields_json"] == "[]"
    assert row["provider_status_json"] == "{}"


# ---------------------------------------------------------------------------
# HK snapshot completeness (adapter quality -> persistence -> eval)
# ---------------------------------------------------------------------------


class _FakeDF:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return self._rows


class _FakeAkshareHK:
    """Component row exists but the southbound-statistics endpoint returns nothing."""

    @staticmethod
    def stock_hk_ggt_components_em():
        return _FakeDF([{"代码": "00700", "名称": "腾讯控股", "最新价": 512.0, "成交额": 8.1e9}])

    @staticmethod
    def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
        return _FakeDF([])


def test_hk_adapter_reports_field_completeness(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkshareHK())
    result = HKConnectAdapter().collect_snapshot(
        target_id="company_hk_00700", ticker="00700.HK", as_of="2026-07-06T16:30:00+08:00"
    )
    assert result.status == "success"
    quality = result.quality
    completeness = quality["field_completeness"]
    assert completeness["required_count"] == len(HK_REQUIRED_FIELDS)
    # price + turnover filled, all 6 southbound fields missing
    assert completeness["filled_count"] == 2
    assert completeness["ratio"] == round(2 / len(HK_REQUIRED_FIELDS), 4)
    assert "southbound_holding_pct" in quality["missing_fields"]
    # unwired fields are reported separately, not as collection failures
    assert set(quality["unsourced_fields"]) == {"buyback_amount_hkd", "ah_premium_pct", "hk_liquidity_score"}


def test_hk_persistence_stores_completeness_and_eval_flags_low_ratio(tmp_path: Path):
    store = _store(tmp_path)
    registry = DemandRegistry(store, SQLiteMessageQueue(store))
    registry.register(
        {
            "schema_version": "demand.v1",
            "demand_id": "d_hk",
            "demand_type": "daily_collection",
            "source_type": "research_pool_request",
            "status": "active",
            "targets": [
                {"target_type": "company", "target_id": "company_hk_00700", "ticker": "00700.HK",
                 "collect_hk_connect": True}
            ],
        }
    )
    result = ToolResult(tool_name="hk_connect_collector", operation="hk_connect_daily_snapshot", request={})
    result.status = "success"
    result.result = {"ticker": "00700.HK", "as_of": "2026-07-06", "hk_connect_eligible": True,
                     "last_price_hkd": 512.0, "turnover_hkd": 8.1e9}
    result.quality = {
        "usable": True,
        "source": "eastmoney_via_akshare",
        "missing_fields": ["southbound_holding_pct"],
        "unsourced_fields": ["ah_premium_pct"],
        "field_completeness": {"required_count": 8, "filled_count": 2, "ratio": 0.25},
    }
    task = {"task_id": "t1", "target": {"target_id": "company_hk_00700", "ticker": "00700.HK"},
            "as_of": "2026-07-06T16:30:00+08:00"}
    ResultPersister(store).save_hk_connect_snapshot(task=task, result=result)
    with store.session() as con:
        row = con.execute("SELECT * FROM hk_connect_snapshots WHERE ticker='00700.HK'").fetchone()
    assert loads_json(row["field_completeness_json"], {})["ratio"] == 0.25
    assert loads_json(row["missing_fields_json"], []) == ["southbound_holding_pct"]
    assert loads_json(row["provider_status_json"], {})["unsourced_fields"] == ["ah_premium_pct"]

    out = CoverageEvaluator(store).hk_connect_coverage(trade_date="2026-07-06")
    assert out["avg_field_completeness"] == 0.25
    assert out["low_completeness"] == [
        {"ticker": "00700.HK", "ratio": 0.25, "missing_fields": ["southbound_holding_pct"]}
    ]


# ---------------------------------------------------------------------------
# market_context_collector: adapter
# ---------------------------------------------------------------------------


class _FakeAkshareContext:
    @staticmethod
    def stock_zh_index_daily_em(symbol: str):
        assert symbol == "000300"
        rows = [{"日期": f"2026-06-{d:02d}", "收盘": 4000.0 + d} for d in range(1, 27)]
        return _FakeDF(rows)

    @staticmethod
    def futures_zh_spot(symbol: str):
        # Realtime endpoint: one row, no date column -> change_* stay null.
        return _FakeDF([{"symbol": symbol, "最新价": 78120.0}])


def test_market_context_adapter_time_series(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkshareContext())
    result = MarketContextAdapter().collect_snapshot(
        context={
            "context_id": "index_csi_300",
            "context_type": "equity_index",
            "name": "沪深300",
            "symbol": "000300",
            "akshare_func": "stock_zh_index_daily_em",
            "date_column": "日期",
            "value_column": "收盘",
            "unit": "index_points",
        },
        as_of="2026-07-06T09:00:00+08:00",
    )
    assert result.status == "success"
    data = result.result
    assert data["value"] == 4026.0
    assert data["change_1d"] == round((4026.0 / 4025.0 - 1) * 100, 4)
    assert data["change_5d"] == round((4026.0 / 4021.0 - 1) * 100, 4)
    assert data["change_20d"] == round((4026.0 / 4006.0 - 1) * 100, 4)
    assert data["as_of"] == "2026-07-06"
    assert result.quality["usable"] is True
    assert result.quality["field_completeness"] == 1.0


def test_market_context_adapter_realtime_single_row(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkshareContext())
    result = MarketContextAdapter().collect_snapshot(
        context={
            "context_id": "commodity_copper",
            "context_type": "commodity",
            "akshare_func": "futures_zh_spot",
            "akshare_args": {"symbol": "铜"},
            "value_column": "最新价",
        }
    )
    assert result.status == "success"
    assert result.result["value"] == 78120.0
    assert result.result["change_1d"] is None  # single realtime row: no history
    assert result.quality["usable"] is True
    assert set(result.quality["missing_fields"]) == {"change_1d", "change_5d", "change_20d"}


def test_market_context_adapter_fails_cleanly_without_akshare(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", None)  # import akshare -> ImportError
    result = MarketContextAdapter().collect_snapshot(context={"context_id": "index_csi_300"})
    assert result.status == "failed"
    assert result.errors[0]["error_code"] == "AKSHARE_NOT_INSTALLED"
    assert result.errors[0]["retryable"] is False


# ---------------------------------------------------------------------------
# market_context_collector: planner + persistence + eval
# ---------------------------------------------------------------------------


def _context_target(context_id: str) -> dict:
    return {
        "target_type": "market_context",
        "target_id": context_id,
        "context_id": context_id,
        "context_type": "equity_index",
        "name": context_id,
        "collect_mic": False,
        "collect_stock": False,
    }


def test_planner_market_context_daily_generates_snapshot_tasks():
    demand = {
        "demand_id": "demand_market_context_daily",
        "demand_type": "market_context_daily",
        "targets": [_context_target("index_csi_300"), _context_target("index_hang_seng_tech")],
    }
    tasks = TaskGraphPlanner({}).plan(
        demand, request_ticket_id="t1", as_of="2026-07-06T09:00:00+08:00", market_phase="pre_market"
    )
    assert [t["task_type"] for t in tasks] == ["market_context_snapshot"] * 2
    assert all(t["tool_name"] == "market_context_collector" for t in tasks)
    disabled = TaskGraphPlanner({"tools": {"market_context_collector": {"enabled": False}}}).plan(
        demand, request_ticket_id="t1", as_of="2026-07-06T09:00:00+08:00", market_phase="pre_market"
    )
    assert disabled == []


def test_save_market_context_snapshot_idempotent_by_context_date(tmp_path: Path):
    store = _store(tmp_path)
    persister = ResultPersister(store)
    task = {"task_id": "t1", "target": _context_target("index_csi_300"), "as_of": "2026-07-06T09:00:00+08:00"}
    result = ToolResult(tool_name="market_context_collector", operation="market_context_snapshot", request={})
    result.status = "success"
    result.result = {"context_id": "index_csi_300", "context_type": "equity_index", "as_of": "2026-07-06",
                     "value": 4026.0, "unit": "index_points", "change_1d": 0.02}
    persister.save_market_context_snapshot(task=task, result=result)
    result.result = dict(result.result, value=4030.0)  # later refresh same day
    persister.save_market_context_snapshot(task=task, result=result)
    with store.session() as con:
        rows = con.execute("SELECT value FROM market_context_snapshots WHERE context_id='index_csi_300'").fetchall()
    assert len(rows) == 1
    assert rows[0]["value"] == 4030.0


def test_market_context_coverage_reports_missing_contexts(tmp_path: Path):
    store = _store(tmp_path)
    registry = DemandRegistry(store, SQLiteMessageQueue(store))
    registry.register(
        {
            "schema_version": "demand.v1",
            "demand_id": "demand_market_context_daily",
            "demand_type": "market_context_daily",
            "source_type": "research_pool_request",
            "status": "active",
            "targets": [_context_target("index_csi_300"), _context_target("fx_cny_hkd")],
        }
    )
    task = {"task_id": "t1", "target": _context_target("index_csi_300"), "as_of": "2026-07-06T09:00:00+08:00"}
    result = ToolResult(tool_name="market_context_collector", operation="market_context_snapshot", request={})
    result.status = "success"
    result.result = {"context_id": "index_csi_300", "context_type": "equity_index", "as_of": "2026-07-06", "value": 4026.0}
    ResultPersister(store).save_market_context_snapshot(task=task, result=result)
    out = CoverageEvaluator(store).market_context_coverage(trade_date="2026-07-06")
    assert out["expected_contexts"] == 2
    assert out["contexts_with_snapshot"] == 1
    assert out["missing_snapshot"] == ["fx_cny_hkd"]
    assert out["missing_value"] == []


# ---------------------------------------------------------------------------
# market_context_collector: request batch registration
# ---------------------------------------------------------------------------


def test_request_batch_registers_market_contexts(tmp_path: Path):
    center, _ = _center(tmp_path)
    spec = {
        "market_contexts": [
            {
                "context_id": "index_csi_300",
                "context_type": "equity_index",
                "name": "沪深300",
                "symbol": "000300",
                "akshare_func": "stock_zh_index_daily_em",
                "akshare_args": {"symbol": "000300"},
                "date_column": "日期",
                "value_column": "收盘",
                "unit": "index_points",
            }
        ],
    }
    out = center.request_batch(spec)
    assert out["registered"]["market_contexts"] == 1
    demand = center.registry.get("demand_market_context_daily")
    assert demand["demand_type"] == "market_context_daily"
    assert demand["task_profile"]["mic"]["enabled"] is False
    assert demand["task_profile"]["market_context"]["enabled"] is True
    target = demand["targets"][0]
    assert target["target_type"] == "market_context"
    assert target["context_id"] == "index_csi_300"
    assert target["akshare_func"] == "stock_zh_index_daily_em"
    assert target["collect_mic"] is False
    # No MIC profile is written for context rows.
    mic_profiles = yaml.safe_load((tmp_path / "mic_config" / "target_profiles.yaml").read_text(encoding="utf-8"))
    assert not mic_profiles["target_profiles"]


def test_research_pool_full_yaml_market_contexts_section():
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "examples" / "research_pool_full.yaml").read_text(encoding="utf-8")
    )
    contexts = spec["market_contexts"]
    assert {c["context_id"] for c in contexts} >= {"index_csi_300", "fx_cny_hkd", "commodity_copper"}
    assert all(c.get("akshare_func") for c in contexts)
    assert spec["demands"]["demand_market_context_daily"]["kind"] == "market_context"


# ---------------------------------------------------------------------------
# research cards
# ---------------------------------------------------------------------------


def _seed_research_card_data(store: SQLiteStore) -> None:
    registry = DemandRegistry(store, SQLiteMessageQueue(store))
    registry.register(
        {
            "schema_version": "demand.v1",
            "demand_id": "demand_company_research_daily",
            "demand_type": "daily_collection",
            "source_type": "research_pool_request",
            "status": "active",
            "targets": [
                {
                    "target_type": "company",
                    "target_id": "company_002371",
                    "ticker": "002371.SZ",
                    "company_name": "北方华创",
                    "industry_id": "industry_ai_semi",
                    "theme_ids": ["industry_export_manufacturing"],
                    "tracking_variables": ["orders", "gross_margin"],
                }
            ],
        }
    )
    with store.session() as con:
        con.execute(
            "INSERT INTO structured_events(event_id, target_id, ticker, event_type, event_date, summary_cn, "
            "impact_json, source_type, confidence, idempotency_key, created_at) "
            "VALUES ('evt_1', 'company_002371', '002371.SZ', 'major_order', '2026-07-03', '中标特高压订单', "
            "'{\"direction\": \"positive\"}', 'exchange', 0.9, 'k_evt_1', '2026-07-03 06:00:00')"
        )
        con.execute(
            "INSERT INTO event_variable_links(event_id, target_id, ticker, tracking_variable, mapping_method, "
            "mapping_confidence, review_status, created_at) "
            "VALUES ('evt_1', 'company_002371', '002371.SZ', 'orders', 'mic_model', 0.9, 'accepted', "
            "'2026-07-03 06:00:00')"
        )
        con.execute(
            "INSERT INTO coverage_gaps(gap_id, target_id, ticker, priority, status, description) "
            "VALUES ('gap_1', 'company_002371', '002371.SZ', 'high', 'open', '缺少毛利率披露')"
        )


def test_research_card_refresh_rolls_up_structured_data(tmp_path: Path):
    store = _store(tmp_path)
    _seed_research_card_data(store)
    card = ResearchCardBuilder(store).refresh(target_id="company_002371", as_of="2026-07-06")
    assert card["company_name"] == "北方华创"
    assert card["theme_ids"] == ["industry_export_manufacturing"]
    assert card["covered_variables"] == ["orders"]
    assert card["missing_variables"] == ["gross_margin"]
    assert card["coverage_ratio"] == 0.5
    assert card["latest_positive_evidence"][0]["summary_cn"] == "中标特高压订单"
    assert card["latest_negative_evidence"] == []
    assert card["open_questions"] == [{"description": "缺少毛利率披露", "priority": "high"}]
    assert card["hk_connect_snapshot"] is None  # A-share target
    assert card["pool_layer_suggestion"] == "keep_current_layer"
    # The card is persisted and refresh is an upsert.
    with store.session() as con:
        rows = con.execute("SELECT target_id FROM research_cards").fetchall()
    assert [r["target_id"] for r in rows] == ["company_002371"]
    ResearchCardBuilder(store).refresh(target_id="company_002371", as_of="2026-07-07")
    with store.session() as con:
        rows = con.execute("SELECT card_json FROM research_cards").fetchall()
    assert len(rows) == 1
    assert loads_json(rows[0]["card_json"], {})["as_of"] == "2026-07-07"


def test_research_card_export_filters_by_demand(tmp_path: Path):
    store = _store(tmp_path)
    _seed_research_card_data(store)
    builder = ResearchCardBuilder(store)
    builder.refresh(target_id="company_002371", as_of="2026-07-06")
    builder.refresh(target_id="company_unrelated", as_of="2026-07-06")
    all_cards = builder.export()
    assert {c["target_id"] for c in all_cards} == {"company_002371", "company_unrelated"}
    # Demand filter resolves the demand's target list; no demand_id column needed on the table.
    filtered = builder.export(demand_id="demand_company_research_daily")
    assert [c["target_id"] for c in filtered] == ["company_002371"]
    by_target = builder.export(target_id="company_unrelated")
    assert [c["target_id"] for c in by_target] == ["company_unrelated"]
