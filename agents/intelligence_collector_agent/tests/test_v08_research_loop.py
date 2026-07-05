"""Tests for the V0.8 research-loop reviewer round.

Covers: planner HK-connect tasks, event -> tracking_variable links (model labels +
keyword candidates), migration v5 tables, HK-connect adapter/persistence,
derived_from_demands runtime references, theme_ids, quality variable-coverage rules,
and the coverage / golden-set evaluators.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.adapters.hk_connect_adapter import HKConnectAdapter, calc_ah_premium_pct
from agent_trade_intel.config import load_config
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.demand import DemandRegistry
from agent_trade_intel.evaluation import CoverageEvaluator
from agent_trade_intel.golden_eval import GoldenSetEvaluator
from agent_trade_intel.persistence import ResultPersister
from agent_trade_intel.planner import TaskGraphPlanner
from agent_trade_intel.pools import resolve_demand_targets
from agent_trade_intel.quality import QualityGate
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.request_center import RequestCenter

# ---------------------------------------------------------------------------
# fixtures (mirrors test_reviewer_round2)
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


def _mic_result(events: list[dict], links_read: int = 3) -> ToolResult:
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={})
    result.status = "success"
    result.result = {
        "search_run_id": "run_x",
        "summary": {"links_read": links_read, "model_calls": 2},
        "all_events": events,
        "top_events": events[:5],
    }
    return result.finish()


# ---------------------------------------------------------------------------
# Planner: HK-connect tasks (reviewer §1)
# ---------------------------------------------------------------------------


def test_daily_collection_hk_target_gets_mic_and_hk_snapshot_but_no_a_share_stock():
    demand = {
        "demand_id": "d1",
        "demand_type": "daily_collection",
        "targets": [
            {
                "target_type": "company",
                "target_id": "company_hk_00700",
                "ticker": "00700.HK",
                "collect_mic": True,
                "collect_stock": False,
                "collect_hk_connect": True,
            }
        ],
    }
    tasks = TaskGraphPlanner({"runtime": {"timezone": "Asia/Shanghai"}}).plan(
        demand, request_ticket_id="ticket_1", as_of="2026-07-06T16:30:00+08:00", market_phase="post_market"
    )
    assert [t["task_type"] for t in tasks] == ["mic_deep_collect", "hk_connect_daily_snapshot"]
    assert tasks[1]["tool_name"] == "hk_connect_collector"


def test_planner_hk_connect_skips_a_share_optout_and_disabled_config():
    targets = [
        {"target_id": "company_600519", "ticker": "600519.SH", "collect_mic": True, "collect_stock": True},
        {"target_id": "company_hk_00700", "ticker": "00700.HK", "collect_mic": True, "collect_hk_connect": False},
        {"target_id": "company_hk_00981", "ticker": "00981.HK", "collect_mic": True},
    ]
    demand = {"demand_id": "d1", "demand_type": "daily_collection", "targets": targets}
    tasks = TaskGraphPlanner({}).plan(
        demand, request_ticket_id="t1", as_of="2026-07-06T16:30:00+08:00", market_phase="post_market"
    )
    hk = [t["target"]["target_id"] for t in tasks if t["task_type"] == "hk_connect_daily_snapshot"]
    assert hk == ["company_hk_00981"]  # A-share skipped, explicit opt-out respected, default HK opt-in
    disabled = TaskGraphPlanner({"tools": {"hk_connect_collector": {"enabled": False}}}).plan(
        demand, request_ticket_id="t1", as_of="2026-07-06T16:30:00+08:00", market_phase="post_market"
    )
    assert not [t for t in disabled if t["task_type"] == "hk_connect_daily_snapshot"]


def test_periodic_review_only_generates_mic_tasks_even_post_market():
    demand = {
        "demand_id": "review_1",
        "demand_type": "periodic_review",
        "targets": [
            {"target_id": "company_hk_00700", "ticker": "00700.HK", "collect_mic": True, "collect_hk_connect": True},
        ],
    }
    tasks = TaskGraphPlanner({}).plan(
        demand, request_ticket_id="ticket_1", as_of="2026-07-06T16:30:00+08:00", market_phase="post_market"
    )
    assert [t["task_type"] for t in tasks] == ["mic_deep_collect"]


# ---------------------------------------------------------------------------
# DB migration v5 (reviewer §3 / §6)
# ---------------------------------------------------------------------------


def test_v5_tables_created_on_existing_database(tmp_path: Path):
    store = SQLiteStore(tmp_path / "intel.db")
    store.init_schema()
    with store.session() as con:
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = {r["version"] for r in con.execute("SELECT version FROM schema_migrations")}
    assert {"event_variable_links", "hk_connect_snapshots"} <= tables
    assert 5 in versions


# ---------------------------------------------------------------------------
# Event -> tracking_variable links (reviewer §2 / §4 / §5)
# ---------------------------------------------------------------------------


def test_persister_saves_event_variable_links_from_mic_model(tmp_path: Path):
    store = _store(tmp_path)
    events = [
        {
            "summary": "签订重大AI服务器订单",
            "event_type": "major_order",
            "event_date": "2026-07-03",
            "confidence": 0.9,
            "tracking_variables": [
                {"variable": "orders", "direction": "positive", "strength": 0.8, "confidence": 0.9, "reasoning": "中标公告"},
                {"variable": "gross_margin", "direction": "unclear", "strength": 0.2, "confidence": 0.4, "reasoning": "未披露毛利"},
                {"variable": "hallucinated_var", "direction": "positive", "confidence": 0.9},
            ],
        }
    ]
    task = {
        "task_id": "t1",
        "target": {"target_id": "company_002371", "ticker": "002371.SZ", "tracking_variables": ["orders", "gross_margin", "inventory"]},
        "idempotency_key": "k1",
    }
    saved = ResultPersister(store).save_mic_structures(task=task, result=_mic_result(events))
    assert saved["events"] == 1
    assert saved["event_variable_links"] >= 2
    with store.session() as con:
        rows = con.execute(
            "SELECT tracking_variable, mapping_method, review_status FROM event_variable_links ORDER BY tracking_variable"
        ).fetchall()
    by_var = {(r["tracking_variable"], r["mapping_method"]): r["review_status"] for r in rows}
    assert by_var[("orders", "mic_model")] == "accepted"  # confidence 0.9 >= 0.65
    assert by_var[("gross_margin", "mic_model")] == "pending"  # confidence 0.4 stays pending
    assert not any(r["tracking_variable"] == "hallucinated_var" for r in rows)  # not in target list


def test_keyword_candidate_mapping_is_pending_not_accepted(tmp_path: Path):
    store = _store(tmp_path)
    events = [
        {"summary": "公司公告回购并注销股份，库存下降", "event_type": "buyback", "event_date": "2026-07-03", "confidence": 0.8}
    ]
    task = {
        "task_id": "t1",
        "target": {"target_id": "company_hk_00700", "ticker": "00700.HK", "tracking_variables": ["buyback", "inventory", "ah_premium"]},
        "idempotency_key": "k1",
    }
    ResultPersister(store).save_mic_structures(task=task, result=_mic_result(events))
    with store.session() as con:
        rows = con.execute("SELECT tracking_variable, mapping_method, review_status FROM event_variable_links").fetchall()
    assert rows, "keyword candidates should be generated for matching variables"
    assert all(r["mapping_method"] == "keyword_candidate" for r in rows)
    assert all(r["review_status"] == "pending" for r in rows)  # candidates never enter confirmed coverage
    assert {r["tracking_variable"] for r in rows} <= {"buyback", "inventory"}


def test_variable_links_saved_even_for_duplicate_events(tmp_path: Path):
    """An event seen before the target declared variables gains links on re-collection."""
    store = _store(tmp_path)
    persister = ResultPersister(store)
    events = [{"summary": "签订重大合同中标", "event_type": "major_order", "event_date": "2026-07-03", "confidence": 0.9}]
    bare_task = {"task_id": "t1", "target": {"target_id": "company_002371", "ticker": "002371.SZ"}, "idempotency_key": "k1"}
    assert persister.save_mic_structures(task=bare_task, result=_mic_result(events))["event_variable_links"] == 0
    tagged_task = {
        "task_id": "t2",
        "target": {"target_id": "company_002371", "ticker": "002371.SZ", "tracking_variables": ["orders"]},
        "idempotency_key": "k2",
    }
    second = persister.save_mic_structures(task=tagged_task, result=_mic_result(events))
    assert second["events"] == 0  # duplicate event not re-inserted
    assert second["event_variable_links"] == 1  # but the link is added now
    with store.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM structured_events").fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# HK-connect adapter + persistence (reviewer §6)
# ---------------------------------------------------------------------------


class _FakeDF:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return self._rows


class _FakeAkshare:
    @staticmethod
    def stock_hk_ggt_components_em():
        return _FakeDF([{"代码": "00700", "名称": "腾讯控股", "最新价": 512.0, "成交额": 8.1e9}])

    @staticmethod
    def stock_hsgt_stock_statistics_em(symbol: str, start_date: str, end_date: str):
        assert symbol == "南向持股"
        return _FakeDF(
            [
                {
                    "股票代码": "00700",
                    "股票简称": "腾讯控股",
                    "持股数量": 8.2e8,
                    "持股市值": 4.2e11,
                    "持股数量占发行股百分比": 8.9,
                    "持股市值变化-1日": 1.2e9,
                    "持股市值变化-5日": -3.0e9,
                    "持股市值变化-10日": 5.5e9,
                }
            ]
        )


def test_hk_connect_adapter_maps_akshare_component_and_southbound_rows(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAkshare())
    result = HKConnectAdapter().collect_snapshot(
        target_id="company_hk_00700", ticker="0700.HK", as_of="2026-07-06T16:30:00+08:00"
    )
    assert result.status == "success"
    data = result.result
    assert data["ticker"] == "00700.HK"
    assert data["hk_connect_eligible"] is True
    assert data["southbound_holding_pct"] == 8.9
    assert data["southbound_mv_change_5d"] == -3.0e9
    assert data["as_of"] == "2026-07-06"
    assert result.quality["has_holding"] is True


def test_hk_connect_adapter_fails_cleanly_without_akshare(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", None)  # import akshare -> ImportError
    result = HKConnectAdapter().collect_snapshot(target_id=None, ticker="00700.HK")
    assert result.status == "failed"
    assert result.errors[0]["error_code"] == "AKSHARE_NOT_INSTALLED"
    assert result.errors[0]["retryable"] is False


def test_save_hk_connect_snapshot_idempotent_by_ticker_date(tmp_path: Path):
    store = _store(tmp_path)
    persister = ResultPersister(store)
    task = {"task_id": "t1", "target": {"target_id": "company_hk_00700", "ticker": "00700.HK"}, "as_of": "2026-07-06T16:30:00+08:00"}
    result = ToolResult(tool_name="hk_connect_collector", operation="hk_connect_daily_snapshot", request={})
    result.status = "success"
    result.result = {"ticker": "00700.HK", "as_of": "2026-07-06", "hk_connect_eligible": True, "southbound_holding_pct": 8.9}
    persister.save_hk_connect_snapshot(task=task, result=result)
    result.result = dict(result.result, southbound_holding_pct=9.1)  # later refresh same day
    persister.save_hk_connect_snapshot(task=task, result=result)
    with store.session() as con:
        rows = con.execute("SELECT southbound_holding_pct FROM hk_connect_snapshots WHERE ticker='00700.HK'").fetchall()
    assert len(rows) == 1  # one snapshot per (ticker, as_of)
    assert rows[0]["southbound_holding_pct"] == 9.1


def test_calc_ah_premium_pct():
    assert calc_ah_premium_pct(a_price_cny=10.0, h_price_hkd=9.0, cny_hkd=1.08) == 20.0
    assert calc_ah_premium_pct(a_price_cny=None, h_price_hkd=9.0, cny_hkd=1.08) is None


# ---------------------------------------------------------------------------
# derived_from_demands runtime reference (reviewer §9)
# ---------------------------------------------------------------------------


def _register_demand(registry: DemandRegistry, demand_id: str, targets: list[dict], **extra) -> None:
    registry.register(
        {
            "schema_version": "demand.v1",
            "demand_id": demand_id,
            "demand_type": extra.pop("demand_type", "daily_collection"),
            "source_type": "research_pool_request",
            "status": "active",
            "targets": targets,
            **extra,
        }
    )


def test_derived_periodic_review_tracks_source_demand_changes(tmp_path: Path):
    store = _store(tmp_path)
    registry = DemandRegistry(store, SQLiteMessageQueue(store))
    t1 = {"target_type": "company", "target_id": "company_002371", "ticker": "002371.SZ", "collect_mic": True}
    t2 = {"target_type": "company", "target_id": "company_300308", "ticker": "300308.SZ", "collect_mic": True}
    _register_demand(registry, "demand_company_research_daily", [t1])
    _register_demand(
        registry,
        "demand_company_monthly_review",
        [],
        demand_type="periodic_review",
        cadence="monthly",
        derived_from_demands=["demand_company_research_daily"],
        target_scope={"scope_type": "explicit_targets"},
    )
    review = registry.get("demand_company_monthly_review")
    ids = [t["target_id"] for t in resolve_demand_targets(review, None, registry=registry)]
    assert ids == ["company_002371"]
    # Source demand gains a target; the review follows without being re-registered.
    source = registry.get("demand_company_research_daily")
    source.pop("_registry", None)
    source.pop("current_version", None)
    source["targets"] = [t1, t2]
    registry.register(source)
    ids = [t["target_id"] for t in resolve_demand_targets(review, None, registry=registry)]
    assert sorted(ids) == ["company_002371", "company_300308"]


def test_batch_derived_from_demands_registers_review_without_copy(tmp_path: Path):
    center, _store_ = _center(tmp_path)
    spec = {
        "companies": [{"name": "北方华创", "ticker": "002371.SZ", "industry_id": "industry_ai_semi"}],
        "demands": {
            "demand_company_monthly_review": {
                "kind": "company",
                "demand_type": "periodic_review",
                "cadence": "monthly",
                "cadence_anchor": 1,
                "derived_from_demands": ["demand_company_research_daily"],
                "task_profile": {"mic": {"enabled": True, "time_window": "30d"}},
            }
        },
    }
    out = center.request_batch(spec)
    review = center.registry.get("demand_company_monthly_review")
    assert review["demand_type"] == "periodic_review"
    assert review["derived_from_demands"] == ["demand_company_research_daily"]
    assert not review.get("targets")  # runtime reference: no frozen copy in the payload
    resolved = resolve_demand_targets(review, None, registry=center.registry)
    assert [t["target_id"] for t in resolved] == ["company_002371"]
    assert not [w for w in out["warnings"] if "derived_from_demands" in w]


# ---------------------------------------------------------------------------
# theme_ids + HK company defaults (reviewer §8 / §14.3)
# ---------------------------------------------------------------------------


def test_company_entry_theme_ids_and_hk_defaults(tmp_path: Path):
    center, _store_ = _center(tmp_path)
    spec = {
        "defaults": {
            "hk_company": {
                "collect_hk_connect": True,
                "tracking_variables": ["southbound_holding", "buyback", "ah_premium"],
            }
        },
        "tracking_variables_by_industry": {"industry_internet_consumer": ["gmv_quality", "buyback"]},
        "companies": [
            {
                "name": "腾讯控股",
                "ticker": "0700.HK",
                "industry_id": "industry_internet_consumer",
                "theme_ids": ["theme_globalization"],
            },
            {"name": "三一重工", "ticker": "600031.SH", "industry_id": "industry_high_end_equipment",
             "theme_ids": "industry_export_manufacturing,theme_globalization"},
        ],
    }
    center.request_batch(spec)
    targets = {t["target_id"]: t for t in center.registry.get("demand_company_research_daily")["targets"]}
    hk = targets["company_hk_00700"]
    assert hk["theme_ids"] == ["theme_globalization"]
    assert hk["collect_hk_connect"] is True
    # industry defaults + HK variable set, deduped (buyback appears once)
    assert hk["tracking_variables"] == ["gmv_quality", "buyback", "southbound_holding", "ah_premium"]
    a_share = targets["company_600031"]
    assert a_share["theme_ids"] == ["industry_export_manufacturing", "theme_globalization"]
    assert "collect_hk_connect" not in a_share
    # theme_ids reach the MIC profile so prompts/aggregation can use them
    mic_profiles = yaml.safe_load((tmp_path / "mic_config" / "target_profiles.yaml").read_text(encoding="utf-8"))
    assert mic_profiles["target_profiles"]["company_600031"]["theme_ids"] == [
        "industry_export_manufacturing",
        "theme_globalization",
    ]


def test_research_pool_full_yaml_v08_sections():
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "examples" / "research_pool_full.yaml").read_text(encoding="utf-8")
    )
    assert spec["defaults"]["hk_company"]["collect_hk_connect"] is True
    reviews = [d for d in spec["demands"].values() if d.get("demand_type") == "periodic_review"]
    assert reviews and all(d.get("derived_from_demands") for d in reviews)
    themed = [c for c in spec["companies"] if c.get("theme_ids")]
    assert themed and all("industry_export_manufacturing" in c["theme_ids"] for c in themed)


# ---------------------------------------------------------------------------
# QualityGate variable coverage (reviewer §15)
# ---------------------------------------------------------------------------


def test_quality_zero_variable_coverage_degrades():
    gate = QualityGate({})
    events = [
        {
            "summary": "行业新闻",
            "confidence": 0.8,
            "source": {"url": "https://e.com/1", "source_type": "exchange", "published_at": "2026-07-03"},
            "tracking_variables": [],
        }
    ]
    target = {"target_id": "company_002371", "tracking_variables": ["orders", "gross_margin"]}
    verdict = gate.evaluate(_mic_result(events), context={"priority": "normal", "target": target})
    assert verdict["decision"] == "accept_degraded"
    assert "zero_tracking_variable_coverage" in {i["issue_type"] for i in verdict["issues"]}


def test_quality_partial_variable_coverage_is_low_severity_note():
    gate = QualityGate({})
    events = [
        {
            "summary": "订单公告",
            "confidence": 0.8,
            "source": {"url": "https://e.com/1", "source_type": "exchange", "published_at": "2026-07-03"},
            "tracking_variables": [{"variable": "orders", "confidence": 0.9}],
        }
    ]
    target = {"target_id": "company_002371", "tracking_variables": ["orders", "gross_margin", "inventory", "capex"]}
    verdict = gate.evaluate(_mic_result(events), context={"priority": "normal", "target": target})
    issue_types = {i["issue_type"] for i in verdict["issues"]}
    assert "low_tracking_variable_coverage" in issue_types
    assert verdict["decision"] == "accept"  # low-severity note does not degrade the run


# ---------------------------------------------------------------------------
# Coverage / golden-set evaluators (reviewer §12 / §13)
# ---------------------------------------------------------------------------


def _seed_coverage_data(store: SQLiteStore) -> None:
    registry = DemandRegistry(store, SQLiteMessageQueue(store))
    _register_demand(
        registry,
        "demand_company_research_daily",
        [
            {"target_type": "company", "target_id": "company_002371", "ticker": "002371.SZ",
             "tracking_variables": ["orders", "gross_margin"]},
            {"target_type": "company", "target_id": "company_hk_00700", "ticker": "00700.HK",
             "tracking_variables": ["buyback"], "collect_hk_connect": True},
        ],
    )
    with store.session() as con:
        con.execute(
            "INSERT INTO structured_events(event_id, target_id, ticker, event_type, event_date, summary_cn, "
            "source_type, confidence, idempotency_key, created_at) "
            "VALUES ('evt_1', 'company_002371', '002371.SZ', 'major_order', '2026-07-03', '中标特高压订单', "
            "'exchange', 0.9, 'k_evt_1', '2026-07-03 06:00:00')"
        )
        con.execute(
            "INSERT INTO event_variable_links(event_id, target_id, ticker, tracking_variable, mapping_method, "
            "mapping_confidence, review_status, created_at) "
            "VALUES ('evt_1', 'company_002371', '002371.SZ', 'orders', 'mic_model', 0.9, 'accepted', '2026-07-03 06:00:00')"
        )
        con.execute(
            "INSERT INTO event_variable_links(event_id, target_id, ticker, tracking_variable, mapping_method, "
            "mapping_confidence, review_status, created_at) "
            "VALUES ('evt_1', 'company_002371', '002371.SZ', 'gross_margin', 'keyword_candidate', 0.4, 'pending', "
            "'2026-07-03 06:00:00')"
        )


def test_coverage_eval_outputs_target_variable_matrix(tmp_path: Path):
    store = _store(tmp_path)
    _seed_coverage_data(store)
    evaluator = CoverageEvaluator(store, timezone="Asia/Shanghai")
    out = evaluator.target_variable_coverage(trade_date="2026-07-03")
    assert out["expected_cells"] == 3  # orders + gross_margin + buyback
    assert out["covered_cells"] == 1  # only the accepted model link counts
    cell = next(x for x in out["matrix"] if x["tracking_variable"] == "orders")
    assert cell["covered"] and cell["has_authoritative_source"]
    with_candidates = evaluator.target_variable_coverage(trade_date="2026-07-03", confirmed_only=False)
    assert with_candidates["covered_cells"] == 2  # pending keyword candidate now counts
    empty_day = evaluator.target_variable_coverage(trade_date="2026-07-04")
    assert empty_day["covered_cells"] == 0 and empty_day["coverage_ratio"] == 0.0


def test_hk_connect_coverage_reports_missing_snapshots(tmp_path: Path):
    store = _store(tmp_path)
    _seed_coverage_data(store)
    evaluator = CoverageEvaluator(store)
    out = evaluator.hk_connect_coverage(trade_date="2026-07-03")
    assert out["expected_hk_targets"] == 1
    assert out["hk_targets_with_snapshot"] == 0
    assert out["missing_snapshot"] == ["00700.HK"]
    with store.session() as con:
        con.execute(
            "INSERT INTO hk_connect_snapshots(snapshot_id, target_id, ticker, as_of, hk_connect_eligible, "
            "southbound_holding_pct, idempotency_key) VALUES ('hkc_1', 'company_hk_00700', '00700.HK', "
            "'2026-07-03', 1, 8.9, 'k_hkc_1')"
        )
    out = evaluator.hk_connect_coverage(trade_date="2026-07-03")
    assert out["hk_targets_with_snapshot"] == 1 and not out["missing_snapshot"]


def test_golden_eval_calculates_recall(tmp_path: Path):
    store = _store(tmp_path)
    _seed_coverage_data(store)
    golden = tmp_path / "golden.yaml"
    golden.write_text(
        yaml.safe_dump(
            {
                "golden_events": [
                    {
                        "expected_event_id": "golden_hit",
                        "target_id": "company_002371",
                        "date_range": ["2026-07-01", "2026-07-06"],
                        "keywords": ["中标"],
                        "expected_variables": ["orders"],
                        "must_have_source_type": ["exchange", "official"],
                    },
                    {
                        "expected_event_id": "golden_miss",
                        "target_id": "company_hk_00700",
                        "date_range": ["2026-07-01", "2026-07-06"],
                        "keywords": ["回购"],
                    },
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    out = GoldenSetEvaluator(store).evaluate(golden)
    assert out["expected_count"] == 2
    assert out["matched_count"] == 1
    assert out["recall"] == 0.5
    assert out["missed"] == ["golden_miss"]
