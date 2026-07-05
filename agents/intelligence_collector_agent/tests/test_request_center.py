from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_trade_intel.config import load_config
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.demand import DemandRegistry
from agent_trade_intel.planner import TaskGraphPlanner
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.request_center import (
    COMPANY_DEMAND_ID,
    INDUSTRY_DEMAND_ID,
    STOCK_DEMAND_ID,
    RequestCenter,
    list_mic_targets,
    load_mic_profiles,
    resolve_mic_config_dir,
)


def _mic_config_dir(tmp_path: Path) -> Path:
    mic_dir = tmp_path / "mic_config"
    mic_dir.mkdir()
    (mic_dir / "target_profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "target_profiles": {
                    "industry_pv_glass": {
                        "target_id": "industry_pv_glass",
                        "type": "industry",
                        "canonical_name": "光伏玻璃",
                    }
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
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
# request industry / company / stock
# ---------------------------------------------------------------------------


def test_request_industry_writes_mic_profile_and_demand(tmp_path: Path):
    center, store = _center(tmp_path)
    result = center.request_industry(
        name="AI算力",
        target_id="industry_ai_semi",
        products=["AI服务器", "光模块"],
        companies=["工业富联", "中际旭创"],
        metrics=["AI服务器订单"],
    )
    assert result["status"] == "requested"
    assert result["target_id"] == "industry_ai_semi"
    # MIC profile written and mergeable
    profiles = load_mic_profiles(resolve_mic_config_dir(center.cfg))
    assert profiles["industry_ai_semi"]["type"] == "industry"
    assert profiles["industry_ai_semi"]["canonical_name"] == "AI算力"
    assert "工业富联" in profiles["industry_ai_semi"]["representative_companies"]
    # legacy entry preserved
    assert "industry_pv_glass" in profiles
    # demand created with the target, active, and demand.registered published
    demand = DemandRegistry(store).get(INDUSTRY_DEMAND_ID)
    assert demand and demand["_registry"]["status"] == "active"
    assert demand["targets"][0]["target_id"] == "industry_ai_semi"
    q = SQLiteMessageQueue(store)
    assert q.list_messages(topic="demand.registered")


def test_request_industry_twice_updates_instead_of_duplicating(tmp_path: Path):
    center, store = _center(tmp_path)
    center.request_industry(name="AI算力", target_id="industry_ai_semi")
    second = center.request_industry(name="AI算力", target_id="industry_ai_semi", products=["光模块"])
    assert second["demand"]["targets_updated"] == 1
    assert second["demand"]["targets_added"] == 0
    demand = DemandRegistry(store).get(INDUSTRY_DEMAND_ID)
    assert len(demand["targets"]) == 1
    assert demand["current_version"] == 2


def test_request_company_a_share_adds_pool_member(tmp_path: Path):
    center, store = _center(tmp_path)
    result = center.request_company(name="北方华创", ticker="002371.SZ", competitors=["中微公司"])
    assert result["target_id"] == "company_002371"
    assert result["pool"]["status"] == "ok"
    demand = DemandRegistry(store).get(COMPANY_DEMAND_ID)
    target = demand["targets"][0]
    assert target["ticker"] == "002371.SZ"
    assert target["collect_stock"] is True
    members = center.pool_repo.list_members(pool_layer="watchlist")
    assert members and members[0]["ticker"] == "002371.SZ"
    profiles = load_mic_profiles(resolve_mic_config_dir(center.cfg))
    assert "北方华创" in profiles["company_002371"]["aliases"]


def test_request_company_hk_skips_stock_and_pool(tmp_path: Path):
    center, store = _center(tmp_path)
    result = center.request_company(name="腾讯控股", ticker="0700.HK")
    # HK codes are zero-padded so 0700.HK / 00700.HK share one target_id
    assert result["target_id"] == "company_hk_00700"
    assert result["pool"] is None
    demand = DemandRegistry(store).get(COMPANY_DEMAND_ID)
    assert demand["targets"][0]["collect_stock"] is False


def test_request_stock_skips_mic_and_requires_a_share(tmp_path: Path):
    center, store = _center(tmp_path)
    result = center.request_stock(ticker="600519.SH", company_name="贵州茅台")
    demand = DemandRegistry(store).get(STOCK_DEMAND_ID)
    assert demand["targets"][0]["collect_mic"] is False
    assert result["pool"]["status"] == "ok"
    # MIC profile file untouched by stock requests
    profiles = load_mic_profiles(resolve_mic_config_dir(center.cfg))
    assert not any(k.startswith("ticker_") for k in profiles)
    with pytest.raises(ValueError):
        center.request_stock(ticker="0700.HK")


def test_remove_target(tmp_path: Path):
    center, store = _center(tmp_path)
    center.request_stock(ticker="600519.SH")
    center.request_stock(ticker="601899.SH")
    result = center.remove_target(demand_id=STOCK_DEMAND_ID, ticker="600519.SH")
    assert result["status"] == "removed"
    demand = DemandRegistry(store).get(STOCK_DEMAND_ID)
    assert [t["ticker"] for t in demand["targets"]] == ["601899.SH"]
    assert center.remove_target(demand_id=STOCK_DEMAND_ID, ticker="600519.SH")["status"] == "not_found"


def test_status_lists_mic_targets_and_demands(tmp_path: Path):
    center, _ = _center(tmp_path)
    center.request_industry(name="AI算力", target_id="industry_ai_semi")
    center.request_stock(ticker="600519.SH")
    status = center.status()
    ids = {t["target_id"] for t in status["mic_registered_targets"]}
    assert {"industry_pv_glass", "industry_ai_semi"} <= ids
    demand_ids = {d["demand_id"] for d in status["managed_demands"]}
    assert {INDUSTRY_DEMAND_ID, STOCK_DEMAND_ID} <= demand_ids
    assert status["pool_members"]


def test_unregistered_mic_targets_warning(tmp_path: Path):
    center, _ = _center(tmp_path)
    demand = {
        "targets": [
            {"target_id": "industry_pv_glass"},
            {"target_id": "industry_unknown_line"},
            {"target_id": "ticker_600519.SH", "ticker": "600519.SH"},
            {"target_id": "company_x", "collect_mic": False},
        ]
    }
    assert center.unregistered_mic_targets(demand) == ["industry_unknown_line"]


# ---------------------------------------------------------------------------
# request batch
# ---------------------------------------------------------------------------


def _batch_spec() -> dict:
    return {
        "defaults": {"priority": "normal", "test_mode": False, "pool_layer": "watchlist"},
        "demands": {
            "demand_company_watch_daily": {
                "kind": "company",
                "priority": "low",
                "task_profile": {"mic": {"enabled": True, "budget_profile": {"max_queries": 4}}},
            }
        },
        "industries": [
            {"name": "AI算力", "target_id": "industry_ai_semi", "products": "AI服务器,光模块"},
            {"name": "高端装备", "target_id": "industry_high_end_equipment"},
        ],
        "companies": [
            {"name": "北方华创", "ticker": "002371.SZ", "products": "刻蚀设备"},
            {"name": "腾讯控股", "ticker": "0700.HK"},
            {"name": "拓荆科技", "ticker": "688072.SH", "demand_id": "demand_company_watch_daily"},
        ],
        "stocks": [{"ticker": "600519.SH", "company_name": "贵州茅台"}],
    }


def test_request_batch_registers_everything_with_one_version_per_demand(tmp_path: Path):
    center, store = _center(tmp_path)
    result = center.request_batch(_batch_spec())
    assert result["registered"] == {"industries": 2, "companies": 3, "stocks": 1, "market_contexts": 0}
    assert result["mic_profiles_written"] == 5  # 2 industries + 3 companies (stocks write no profile)

    # MIC profiles: industries + all companies (including watch tier) are written
    profiles = load_mic_profiles(resolve_mic_config_dir(center.cfg))
    for tid in ("industry_ai_semi", "industry_high_end_equipment", "company_002371", "company_hk_00700", "company_688072"):
        assert tid in profiles, tid

    registry = DemandRegistry(store)
    # one version bump per demand regardless of target count
    for demand_id, expected_targets in [
        (INDUSTRY_DEMAND_ID, 2),
        (COMPANY_DEMAND_ID, 2),
        ("demand_company_watch_daily", 1),
        (STOCK_DEMAND_ID, 1),
    ]:
        demand = registry.get(demand_id)
        assert demand, demand_id
        assert len(demand["targets"]) == expected_targets
        assert demand["current_version"] == 1

    # scaffold override applied to the watch demand
    watch = registry.get("demand_company_watch_daily")
    assert watch["priority"] == "low"
    assert watch["task_profile"]["mic"]["budget_profile"]["max_queries"] == 4
    # pool: two A-share companies + one stock (HK skipped)
    members = {m["ticker"] for m in center.pool_repo.list_members(pool_layer="watchlist")}
    assert members == {"002371.SZ", "688072.SH", "600519.SH"}


def test_request_batch_is_idempotent(tmp_path: Path):
    center, store = _center(tmp_path)
    center.request_batch(_batch_spec())
    second = center.request_batch(_batch_spec())
    registry = DemandRegistry(store)
    demand = registry.get(COMPANY_DEMAND_ID)
    assert len(demand["targets"]) == 2
    assert demand["current_version"] == 2
    for entry in second["demands"]:
        assert entry["targets_added"] == 0


def test_request_batch_test_mode_default(tmp_path: Path):
    center, store = _center(tmp_path)
    spec = _batch_spec()
    spec["defaults"]["test_mode"] = True
    center.request_batch(spec)
    demand = DemandRegistry(store).get(INDUSTRY_DEMAND_ID)
    assert demand["test_mode"] is True


# ---------------------------------------------------------------------------
# planner behaviour for the new target flags
# ---------------------------------------------------------------------------


def _planner_config() -> dict:
    return {"runtime": {"timezone": "Asia/Shanghai"}, "cadence": {}, "schedule": {}}


def test_planner_daily_collection_skips_mic_for_stock_only_targets():
    demand = {
        "demand_id": "d_stock",
        "demand_type": "daily_collection",
        "targets": [
            {"target_type": "ticker", "target_id": "ticker_600519.SH", "ticker": "600519.SH", "collect_mic": False, "collect_stock": True}
        ],
    }
    tasks = TaskGraphPlanner(_planner_config()).plan(
        demand, request_ticket_id="t1", as_of="2026-07-03T15:30:00+08:00", market_phase="post_market"
    )
    assert [t["task_type"] for t in tasks] == ["post_close_stock_refresh"]


def test_planner_daily_collection_skips_stock_for_industry_and_hk_targets():
    demand = {
        "demand_id": "d_mix",
        "demand_type": "daily_collection",
        "targets": [
            {"target_type": "industry", "target_id": "industry_ai_semi", "collect_mic": True},
            {"target_type": "company", "target_id": "company_hk_0700", "ticker": "0700.HK", "collect_mic": True, "collect_stock": False},
            {"target_type": "company", "target_id": "company_002371", "ticker": "002371.SZ", "collect_mic": True, "collect_stock": True},
        ],
    }
    tasks = TaskGraphPlanner(_planner_config()).plan(
        demand, request_ticket_id="t1", as_of="2026-07-03T15:30:00+08:00", market_phase="post_market"
    )
    mic_targets = [t["target"]["target_id"] for t in tasks if t["task_type"] == "mic_deep_collect"]
    stock_targets = [t["target"]["target_id"] for t in tasks if t["task_type"] == "post_close_stock_refresh"]
    assert mic_targets == ["industry_ai_semi", "company_hk_0700", "company_002371"]
    assert stock_targets == ["company_002371"]
