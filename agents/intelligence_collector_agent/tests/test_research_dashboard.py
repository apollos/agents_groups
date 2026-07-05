"""Tests for the research effectiveness dashboard (reviewer round 6).

Covers: ResearchDashboardService summary/coverage/target aggregation consistency with
the CLI evaluators, candidate-only cell counting, industry/theme grouping, HK-connect
completeness / market-context / research-card visibility, golden-run persistence, the
real HTTP routes (/api/research/*) and the no-data / bad-input behaviour.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from agent_trade_intel.config import load_config
from agent_trade_intel.dashboard import DASHBOARD_HTML, DashboardService, build_dashboard_handler
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.demand import DemandRegistry
from agent_trade_intel.evaluation import CoverageEvaluator
from agent_trade_intel.golden_eval import GoldenSetEvaluator
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.research_cards import ResearchCardBuilder
from agent_trade_intel.research_dashboard import ResearchDashboardService

TRADE_DATE = "2026-07-03"

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "intel.db")
    store.init_schema()
    return store


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


def _seed(store: SQLiteStore) -> None:
    registry = DemandRegistry(store, SQLiteMessageQueue(store))
    _register_demand(
        registry,
        "demand_company_research_daily",
        [
            {
                "target_type": "company",
                "target_id": "company_002371",
                "ticker": "002371.SZ",
                "company_name": "北方华创",
                "industry_id": "industry_ai_semi",
                "theme_ids": ["theme_ai"],
                "pool_layer": "core",
                "tracking_variables": ["orders", "gross_margin"],
            },
            {
                "target_type": "company",
                "target_id": "company_hk_00700",
                "ticker": "00700.HK",
                "company_name": "腾讯控股",
                "industry_id": "industry_internet",
                "theme_ids": ["theme_platform"],
                "pool_layer": "core",
                "tracking_variables": ["buyback", "southbound_holding"],
                "collect_hk_connect": True,
            },
        ],
    )
    _register_demand(
        registry,
        "demand_market_context_daily",
        [
            {
                "target_type": "market_context",
                "target_id": "ctx_hs300",
                "context_id": "ctx_hs300",
                "context_type": "index",
                "name": "沪深300",
            }
        ],
        demand_type="market_context_daily",
    )
    with store.session() as con:
        con.execute(
            "INSERT INTO structured_events(event_id, target_id, ticker, event_type, event_date, summary_cn, "
            "source_type, confidence, idempotency_key, created_at) "
            "VALUES ('evt_1', 'company_002371', '002371.SZ', 'major_order', ?, '中标特高压订单', "
            "'exchange', 0.9, 'k_evt_1', ? || ' 06:00:00')",
            (TRADE_DATE, TRADE_DATE),
        )
        con.execute(
            "INSERT INTO event_variable_links(event_id, target_id, ticker, tracking_variable, mapping_method, "
            "mapping_confidence, review_status, created_at) "
            "VALUES ('evt_1', 'company_002371', '002371.SZ', 'orders', 'mic_model', 0.9, 'accepted', "
            "? || ' 06:00:00')",
            (TRADE_DATE,),
        )
        con.execute(
            "INSERT INTO event_variable_links(event_id, target_id, ticker, tracking_variable, mapping_method, "
            "mapping_confidence, review_status, created_at) "
            "VALUES ('evt_1', 'company_002371', '002371.SZ', 'gross_margin', 'keyword_candidate', 0.4, "
            "'pending', ? || ' 06:00:00')",
            (TRADE_DATE,),
        )
        con.execute(
            "INSERT INTO structured_events(event_id, target_id, ticker, event_type, event_date, summary_cn, "
            "source_type, confidence, idempotency_key, created_at) "
            "VALUES ('evt_2', 'company_hk_00700', '00700.HK', 'buyback', ?, '继续回购', "
            "'media', 0.6, 'k_evt_2', ? || ' 07:00:00')",
            (TRADE_DATE, TRADE_DATE),
        )
        con.execute(
            "INSERT INTO event_variable_links(event_id, target_id, ticker, tracking_variable, mapping_method, "
            "mapping_confidence, review_status, created_at) "
            "VALUES ('evt_2', 'company_hk_00700', '00700.HK', 'buyback', 'keyword_candidate', 0.5, 'pending', "
            "? || ' 07:00:00')",
            (TRADE_DATE,),
        )
        con.execute(
            "INSERT INTO hk_connect_snapshots(snapshot_id, target_id, ticker, as_of, hk_connect_eligible, "
            "southbound_holding_pct, field_completeness_json, missing_fields_json, idempotency_key) "
            "VALUES ('hkc_1', 'company_hk_00700', '00700.HK', ?, 1, 8.9, "
            "'{\"required_count\":6,\"filled_count\":3,\"ratio\":0.5}', "
            "'[\"turnover_hkd\",\"last_price_hkd\",\"southbound_holding_shares\"]', 'k_hkc_1')",
            (TRADE_DATE,),
        )
        con.execute(
            "INSERT INTO market_context_snapshots(snapshot_id, context_id, context_type, name, as_of, value, "
            "unit, change_1d, idempotency_key) "
            "VALUES ('mcs_1', 'ctx_hs300', 'index', '沪深300', ?, 4023.5, 'point', -0.4, 'k_mcs_1')",
            (TRADE_DATE,),
        )
        con.execute(
            "INSERT INTO coverage_gaps(gap_id, target_id, ticker, priority, description) "
            "VALUES ('gap_1', 'company_002371', '002371.SZ', 'high', '缺少毛利率权威证据')"
        )
        con.execute(
            "INSERT INTO data_quality_issues(issue_id, severity, issue_type, ticker, summary_cn) "
            "VALUES ('dq_1', 'P1', 'coverage', '00700.HK', '南向持股字段缺失')"
        )
    builder = ResearchCardBuilder(store)
    builder.refresh(target_id="company_002371", as_of=TRADE_DATE)
    builder.refresh(target_id="company_hk_00700", as_of=TRADE_DATE)


def _service(store: SQLiteStore) -> ResearchDashboardService:
    return ResearchDashboardService(data_store=store, timezone="Asia/Shanghai")


# ---------------------------------------------------------------------------
# summary aggregation
# ---------------------------------------------------------------------------


def test_research_dashboard_summary_matches_coverage_evaluator(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    svc = _service(store)
    evaluator = CoverageEvaluator(store, timezone="Asia/Shanghai")

    expected = evaluator.target_variable_coverage(
        trade_date=TRADE_DATE, demand_id="demand_company_research_daily", confirmed_only=True
    )
    actual = svc.summary(trade_date=TRADE_DATE, demand_id="demand_company_research_daily")

    assert actual["coverage"]["confirmed"]["expected_cells"] == expected["expected_cells"]
    assert actual["coverage"]["confirmed"]["covered_cells"] == expected["covered_cells"]
    assert actual["coverage"]["confirmed"]["coverage_ratio"] == expected["coverage_ratio"]
    # authoritative cells: the accepted `orders` link comes from an exchange source
    assert actual["coverage"]["authoritative_covered_cells"] == 1
    score = actual["research_health_score"]
    assert score is not None and 0.0 < score < 1.0
    # JSON-serialisable (what the HTTP API returns)
    assert json.loads(json.dumps(actual, default=str))["trade_date"] == TRADE_DATE


def test_research_dashboard_summary_counts_candidate_only_cells(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    out = _service(store).summary(trade_date=TRADE_DATE)

    # gross_margin@002371 and buyback@00700 have only pending keyword candidates
    assert out["coverage"]["candidate_only_cells"] == 2
    examples = {(x["target_id"], x["tracking_variable"]) for x in out["coverage"]["candidate_only_examples"]}
    assert examples == {("company_002371", "gross_margin"), ("company_hk_00700", "buyback")}
    # zero accepted coverage: the HK target declared 2 variables, none accepted
    zero = out["coverage"]["zero_covered_targets"]
    assert [x["target_id"] for x in zero] == ["company_hk_00700"]
    assert zero[0]["expected_variables"] == 2 and zero[0]["ticker"] == "00700.HK"


def test_research_dashboard_groups_by_industry_theme_and_pool_layer(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    groups = _service(store).summary(trade_date=TRADE_DATE)["groups"]

    by_industry = {g["group"]: g for g in groups["by_industry"]}
    assert by_industry["industry_ai_semi"]["expected_cells"] == 2
    assert by_industry["industry_ai_semi"]["covered_cells"] == 1
    assert by_industry["industry_ai_semi"]["authoritative_cells"] == 1
    assert by_industry["industry_internet"]["covered_cells"] == 0

    by_theme = {g["group"]: g for g in groups["by_theme"]}
    assert by_theme["theme_ai"]["coverage_ratio"] == 0.5
    assert by_theme["theme_platform"]["covered_cells"] == 0

    by_pool = {g["group"]: g for g in groups["by_pool_layer"]}
    assert by_pool["core"]["expected_cells"] == 4


def test_research_dashboard_hk_completeness_visible(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    hk = _service(store).summary(trade_date=TRADE_DATE)["hk_connect"]

    assert hk["expected_hk_targets"] == 1 and hk["hk_targets_with_snapshot"] == 1
    assert hk["avg_field_completeness"] == 0.5
    assert hk["low_completeness"][0]["ticker"] == "00700.HK"
    # rows must expose field_completeness AND missing_fields, not just row existence
    row = hk["rows"][0]
    assert row["field_completeness"]["ratio"] == 0.5
    assert "turnover_hkd" in row["missing_fields"]


def test_research_dashboard_market_context_visible(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    mc = _service(store).summary(trade_date=TRADE_DATE)["market_context"]

    assert mc["expected_contexts"] == 1 and mc["contexts_with_snapshot"] == 1
    assert mc["missing_snapshot"] == []
    assert mc["rows"][0]["context_id"] == "ctx_hs300" and mc["rows"][0]["value"] == 4023.5
    # a day without snapshots reports the gap instead of failing
    empty = _service(store).summary(trade_date="2026-07-04")["market_context"]
    assert empty["missing_snapshot"] == ["ctx_hs300"]


def test_research_dashboard_research_card_summary_visible(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    cards = _service(store).summary(trade_date=TRADE_DATE)["research_cards"]

    assert cards["cards_total"] == 2 and cards["fresh_cards"] == 2
    low = {c["target_id"] for c in cards["low_coverage_cards"]}
    assert "company_hk_00700" in low  # 0/2 accepted variables
    assert set(cards["pool_layer_suggestions"]) >= {"keep_current_layer"} or cards["pool_layer_suggestions"]
    # cards refreshed on an earlier as_of become stale for a later trade_date
    stale_view = _service(store).summary(trade_date="2026-07-06")["research_cards"]
    assert stale_view["fresh_cards"] == 0 and len(stale_view["stale_cards"]) == 2


def test_research_dashboard_quality_and_gaps_in_summary(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    quality = _service(store).summary(trade_date=TRADE_DATE)["quality"]

    assert [x["issue_id"] for x in quality["open_p0_p1_issues"]] == ["dq_1"]
    assert [x["gap_id"] for x in quality["open_high_priority_gaps"]] == ["gap_1"]


# ---------------------------------------------------------------------------
# coverage matrix / target detail
# ---------------------------------------------------------------------------


def test_research_dashboard_coverage_matrix_enriched_with_target_meta(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    svc = _service(store)

    confirmed = svc.coverage_matrix(trade_date=TRADE_DATE)
    assert confirmed["covered_cells"] == 1
    cell = next(x for x in confirmed["matrix"] if x["tracking_variable"] == "orders")
    assert cell["industry_id"] == "industry_ai_semi" and cell["pool_layer"] == "core"

    inclusive = svc.coverage_matrix(trade_date=TRADE_DATE, include_candidates=True)
    assert inclusive["covered_cells"] == 3  # orders + gross_margin + buyback


def test_research_dashboard_target_detail_returns_card_events_links_gaps(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
    detail = _service(store).target_detail(target_id="company_002371")

    assert detail["research_card"]["coverage_ratio"] == 0.5
    assert detail["research_card"]["missing_variables"] == ["gross_margin"]
    assert [e["event_id"] for e in detail["events"]] == ["evt_1"]
    assert {link["tracking_variable"] for link in detail["variable_links"]} == {"orders", "gross_margin"}
    assert detail["open_gaps"][0]["gap_id"] == "gap_1"

    unknown = _service(store).target_detail(target_id="does_not_exist")
    assert unknown["research_card"] is None
    assert unknown["events"] == [] and unknown["variable_links"] == [] and unknown["open_gaps"] == []


def test_research_dashboard_summary_no_data_returns_empty_not_error(tmp_path: Path):
    store = _store(tmp_path)  # schema only, nothing registered
    out = _service(store).summary(trade_date=TRADE_DATE)

    assert out["coverage"]["confirmed"]["expected_cells"] == 0
    assert out["coverage"]["confirmed"]["coverage_ratio"] is None
    assert out["coverage"]["zero_covered_targets"] == []
    assert out["hk_connect"]["expected_hk_targets"] == 0
    assert out["research_cards"]["cards_total"] == 0
    assert out["golden"] is None


# ---------------------------------------------------------------------------
# golden run persistence
# ---------------------------------------------------------------------------


def test_golden_eval_record_persists_and_summary_shows_latest(tmp_path: Path):
    store = _store(tmp_path)
    _seed(store)
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
                    },
                    {
                        "expected_event_id": "golden_miss",
                        "target_id": "company_hk_00700",
                        "date_range": ["2026-07-01", "2026-07-06"],
                        "keywords": ["分红"],
                    },
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    out = GoldenSetEvaluator(store).evaluate(golden, record=True)
    assert out["run_id"].startswith("golden_run_")

    latest = _service(store).summary(trade_date=TRADE_DATE)["golden"]
    assert latest["run_id"] == out["run_id"]
    assert latest["recall"] == 0.5
    assert latest["missed"] == ["golden_miss"]
    # evaluate without record does not add a run
    GoldenSetEvaluator(store).evaluate(golden)
    with store.session() as con:
        assert con.execute("SELECT COUNT(*) c FROM golden_eval_runs").fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# HTTP routes (real handler from build_dashboard_handler)
# ---------------------------------------------------------------------------


def _dashboard_config(tmp_path: Path) -> Path:
    cfg = {
        "agent": {"agent_id": "test_intel", "agent_group": "intelligence_collector"},
        "openclaw": {"model": {"primary": "openai/gpt-5.5", "fallbacks": [], "require_registered": False, "allow_openclaw_default": False}},
        "runtime": {"sqlite_path": str(tmp_path / "intel.db"), "workspace_root": str(tmp_path), "log_dir": str(tmp_path / "logs"), "timezone": "Asia/Shanghai"},
        "tools": {"python_executable": "python", "market_intelligence_collector": {"enabled": True}, "stock_data_collector": {"enabled": True}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def test_dashboard_research_http_endpoints(tmp_path: Path):
    from http.server import ThreadingHTTPServer

    cfg = load_config(_dashboard_config(tmp_path))
    store = _store(tmp_path)
    _seed(store)
    service = DashboardService(cfg, state_store=store, bus_store=store, data_store=store)
    research = ResearchDashboardService(data_store=store, timezone="Asia/Shanghai")
    handler = build_dashboard_handler(DASHBOARD_HTML.replace("__REFRESH_SECONDS__", "5"), service, research)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/", timeout=5) as res:
            html = res.read().decode("utf-8")
        assert "研究效果" in html and "/api/research/summary" in html

        with urllib.request.urlopen(f"{base}/api/research/summary?date={TRADE_DATE}", timeout=5) as res:
            data = json.loads(res.read().decode("utf-8"))
        assert data["trade_date"] == TRADE_DATE
        assert data["coverage"]["confirmed"]["covered_cells"] == 1

        url = f"{base}/api/research/coverage?date={TRADE_DATE}&include_candidates=1"
        with urllib.request.urlopen(url, timeout=5) as res:
            cov = json.loads(res.read().decode("utf-8"))
        assert cov["covered_cells"] == 3

        with urllib.request.urlopen(f"{base}/api/research/target?target_id=company_002371", timeout=5) as res:
            detail = json.loads(res.read().decode("utf-8"))
        assert detail["research_card"]["coverage_ratio"] == 0.5

        # missing target_id -> 400, unknown target -> 200 with empty payload (not 500)
        try:
            urllib.request.urlopen(f"{base}/api/research/target", timeout=5)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as err:
            assert err.code == 400
        with urllib.request.urlopen(f"{base}/api/research/target?target_id=nope", timeout=5) as res:
            unknown = json.loads(res.read().decode("utf-8"))
        assert unknown["research_card"] is None and unknown["events"] == []
    finally:
        server.shutdown()
        server.server_close()
