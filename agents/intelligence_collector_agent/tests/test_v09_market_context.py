"""V0.9: market-context collection goes through the stock tool.

Covers the agent-side chain with a fake tool CLI: adapter mapping (fresh / stale / missing /
config errors / environment errors), the architecture boundary (no vendor imports in agent
production code), task handling in the agent (stale = saved but not today's coverage,
non-retryable = manual hint), persistence of data_date / provenance, the v10 column migration
on a legacy database, and coverage that distinguishes fresh / stale / unknown rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.adapters.market_context_adapter import MarketContextAdapter
from agent_trade_intel.agent import IntelligenceCollectorAgent
from agent_trade_intel.config import AgentModelConfig, CollectorConfig, RuntimeConfig, ToolConfig
from agent_trade_intel.db import SQLiteStore, loads_json
from agent_trade_intel.demand import DemandRegistry
from agent_trade_intel.evaluation import CoverageEvaluator
from agent_trade_intel.persistence import ResultPersister
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.request_center import RequestCenter

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "agent_trade_intel"
AS_OF = "2026-07-06"


# ---------------------------------------------------------------------------
# Fake tool CLI
# ---------------------------------------------------------------------------
def tool_payload(*, value, data_date, changes=None, status="success", quality=None, metric="close", unit="index_points",
                 context_id="index_csi_300", context_type="equity_index", symbol="000300", errors=None, warnings=None):
    q = {
        "usable": value is not None, "status": "fresh", "is_fresh": True, "staleness_days": 0, "max_staleness_days": 3,
        "data_date_matches_as_of": True, "observations": 26, "missing_fields": [], "single_source": True,
        "cross_validated": False, "conflicts": [], "anomalies": [], "warnings": [], "data_quality_score": 0.925,
    }
    q.update(quality or {})
    return {
        "request_id": "mctx_test", "status": status, "errors": errors or [], "warnings": warnings or [],
        "result": {
            "context_id": context_id, "context_type": context_type, "symbol": symbol, "name": "沪深300",
            "identity": {"index_code": symbol, "market": "A_share", "currency": "CNY"},
            "request_window": {"mode": "latest", "as_of": AS_OF, "frequency": "1d"},
            "data_date": data_date, "observed_at": None, "collected_at": f"{AS_OF}T09:00:05+08:00",
            "metric": metric, "value": value, "unit": unit, "values": {metric: value},
            "changes": changes or {}, "series": [],
            "source": {"provider": "akshare", "source_api": "stock_zh_index_daily", "source_url": "https://finance.sina.com.cn/stock/"},
            "quality": q,
            "provenance": {"stock_data_request_id": "req_1", "ingestion_run_ids": ["run_1"], "record_ids": ["rec_1"], "raw_payload_ids": ["raw_1"]},
        },
        "stock_data_response": {"request_id": "req_1", "status": status, "records_returned": {"index_bars": 26}},
    }


def change(value, periods, kind="percent", unit="trading_day", reason=None):
    return {"periods": periods, "period_unit": unit, "kind": kind, "value": value, "reason": reason}


class FakeCLI:
    def __init__(self, payload=None, rc=0, stdout=None, stderr="", exc=None):
        self.payload, self.rc, self.stdout, self.stderr, self.exc = payload, rc, stdout, stderr, exc
        self.cmds: list[list[str]] = []

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if self.exc:
            raise self.exc
        out = self.stdout if self.stdout is not None else json.dumps(self.payload, ensure_ascii=False)
        return subprocess.CompletedProcess(cmd, self.rc, stdout=out, stderr=self.stderr)


def adapter_with(monkeypatch, cli: FakeCLI, **kw) -> MarketContextAdapter:
    monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: cli(cmd))
    return MarketContextAdapter(**kw)


CTX = {"context_id": "index_csi_300", "context_type": "equity_index", "name": "沪深300", "symbol": "000300", "metrics": ["close"]}


# ---------------------------------------------------------------------------
# Architecture boundary
# ---------------------------------------------------------------------------
def test_agent_production_code_has_no_vendor_imports():
    offenders = []
    pattern = re.compile(r"^\s*(import\s+(akshare|tushare|baostock)\b|from\s+(akshare|tushare|baostock)\b)", re.M)
    for path in SRC_ROOT.rglob("*.py"):
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert offenders == [], f"agent production code must not import data vendors: {offenders}"
    # The adapter must not carry vendor-call / column-parsing / change-computation logic any more.
    adapter_src = (SRC_ROOT / "adapters" / "market_context_adapter.py").read_text(encoding="utf-8")
    for forbidden in ("getattr(ak", "to_dict(", "DEFAULT_VALUE_COLUMNS", "DEFAULT_DATE_COLUMNS", "_pct_change", "_sort_rows", "_call_spec", "stock_zh_index_daily_em"):
        assert forbidden not in adapter_src, forbidden
    # Vendor function names only appear as *legacy fields to ignore*, never as call targets.
    import ast

    tree = ast.parse(adapter_src)
    string_consts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and "akshare_func" in n.value]
    # Only the LEGACY_VENDOR_FIELDS tuple element and the module docstring may mention it.
    assert sorted(string_consts, key=len)[0] == "akshare_func" and len(string_consts) <= 2, string_consts
    assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) and "akshare" in ast.dump(n) for n in ast.walk(tree))


# ---------------------------------------------------------------------------
# Adapter mapping
# ---------------------------------------------------------------------------
def test_adapter_builds_business_request_and_maps_fresh_result(monkeypatch):
    cli = FakeCLI(tool_payload(value=4026.0, data_date=AS_OF, changes={
        "1p": change(0.0248, 1), "5p": change(0.1243, 5), "20p": change(0.4993, 20)}))
    adapter = adapter_with(monkeypatch, cli, python_executable="py", config_dir="/cfg", working_dir="/wd")
    result = adapter.collect_snapshot(context=CTX, as_of=f"{AS_OF}T09:00:00+08:00")
    assert result.status == "success"
    cmd = cli.cmds[0]
    assert cmd[:7] == ["py", "-m", "stock_data_ingestion.cli", "--config-dir", "/cfg", "fetch", "market-context"]
    assert cmd[cmd.index("--context-type") + 1] == "equity_index" and cmd[cmd.index("--symbol") + 1] == "000300"
    assert cmd[cmd.index("--as-of") + 1] == AS_OF and cmd[cmd.index("--metrics") + 1] == "close"
    assert "--compact" in cmd and cmd[cmd.index("--requested-by") + 1] == "intelligence_collector_agent"
    assert not any("akshare" in tok or "stock_zh" in tok for tok in cmd)
    data = result.result
    assert data["value"] == 4026.0 and data["data_date"] == AS_OF and data["as_of"] == AS_OF
    assert (data["change_1d"], data["change_5d"], data["change_20d"]) == (0.0248, 0.1243, 0.4993)
    assert data["change_kind"] == "percent" and data["change_period_unit"] == "trading_day"
    assert data["provider"] == "akshare" and data["source_api"] == "stock_zh_index_daily"
    assert data["provenance"]["record_ids"] == ["rec_1"] and data["tool_request_id"] == "mctx_test"
    assert result.quality["usable"] is True and result.quality["is_fresh"] is True and result.quality["field_completeness"] == 1.0


def test_adapter_forwards_category_params_and_realtime(monkeypatch):
    payload = tool_payload(value=78120.0, data_date=AS_OF, metric="latest", unit="CNY/ton", context_id="commodity_copper",
                           context_type="commodity", symbol="CU0", changes={
                               "snapshot_vs_pre_settle": change(0.35, 1, unit="snapshot_vs_pre_settle"),
                               "1p": change(None, 1, reason="realtime_only_snapshot"), "5p": change(None, 5, reason="realtime_only_snapshot"),
                               "20p": change(None, 20, reason="realtime_only_snapshot")})
    cli = FakeCLI(payload)
    result = adapter_with(monkeypatch, cli).collect_snapshot(context={
        "context_id": "commodity_copper", "context_type": "commodity", "symbol": "CU0", "frequency": "realtime",
        "instrument_type": "futures", "market": "SHFE", "contract": "CU0", "max_staleness_days": 0})
    cmd = cli.cmds[0]
    for flag, val in (("--frequency", "realtime"), ("--instrument-type", "futures"), ("--market", "SHFE"), ("--contract", "CU0"), ("--max-staleness-days", "0")):
        assert cmd[cmd.index(flag) + 1] == val
    assert result.status == "success" and result.result["unit"] == "CNY/ton"
    assert result.result["change_1d"] is None and result.result["change_1d_reason"] == "realtime_only_snapshot"
    assert set(result.quality["missing_fields"]) == {"change_1d", "change_5d", "change_20d"}


def test_adapter_stale_value_is_usable_with_real_date(monkeypatch):
    payload = tool_payload(value=4357.6, data_date="2026-06-30", status="partial_success", changes={"1p": change(0.28, 1)},
                           quality={"status": "stale", "is_fresh": False, "staleness_days": 6, "data_date_matches_as_of": False,
                                    "warnings": ["stale: latest data_date 2026-06-30 is 6d before as_of 2026-07-06 (tolerance 3d)"]})
    result = adapter_with(monkeypatch, FakeCLI(payload)).collect_snapshot(context=CTX, as_of=AS_OF)
    assert result.status == "success"
    assert result.result["data_date"] == "2026-06-30" and result.result["as_of"] == AS_OF
    assert result.quality["is_fresh"] is False and result.quality["status"] == "stale" and result.quality["staleness_days"] == 6
    assert any(w.startswith("stale:") for w in result.quality["warnings"])


def test_adapter_legacy_vendor_fields_ignored_and_reported(monkeypatch):
    cli = FakeCLI(tool_payload(value=1.0, data_date=AS_OF))
    result = adapter_with(monkeypatch, cli).collect_snapshot(context={
        **CTX, "akshare_func": "stock_zh_index_daily_em", "akshare_args": {"symbol": "000300"}, "value_column": "收盘", "unit": "index_points"}, as_of=AS_OF)
    assert result.status == "success"
    assert result.quality["legacy_fields_ignored"] == ["akshare_args", "akshare_func", "unit", "value_column"]
    assert "stock_zh_index_daily_em" not in " ".join(cli.cmds[0])


def test_adapter_failure_classification(monkeypatch):
    # Missing value: tool says "missing" -> retryable.
    missing = tool_payload(value=None, data_date=None, status="failed", quality={"usable": False, "status": "missing", "is_fresh": None, "staleness_days": None})
    r = adapter_with(monkeypatch, FakeCLI(missing)).collect_snapshot(context=CTX, as_of=AS_OF)
    assert r.status == "failed" and r.errors[0]["error_code"] == "MARKET_CONTEXT_NO_USABLE_VALUE" and r.errors[0]["retryable"] is True
    # Unknown symbol: config problem -> non-retryable with hint pointing at the tool config.
    bad = tool_payload(value=None, data_date=None, status="failed", quality={"usable": False, "status": "failed"},
                       errors=[{"error_code": "INVALID_REQUEST", "error_message": "no market-context binding for commodity:XYZ", "retryable": False}])
    r = adapter_with(monkeypatch, FakeCLI(bad)).collect_snapshot(context={"context_id": "c", "context_type": "commodity", "symbol": "XYZ"})
    assert r.status == "failed" and r.errors[0]["retryable"] is False and "market_context_sources.yaml" in r.errors[0]["suggested_action"]
    # Tool crashed on a network error -> retryable; environment problem -> manual.
    r = adapter_with(monkeypatch, FakeCLI(rc=1, stdout="", stderr="Traceback ... ConnectionError")).collect_snapshot(context=CTX)
    assert r.errors[0]["error_code"] == "MARKET_CONTEXT_CLI_FAILED" and r.errors[0]["retryable"] is True
    r = adapter_with(monkeypatch, FakeCLI(rc=1, stdout="", stderr="ModuleNotFoundError: No module named 'stock_data_ingestion'")).collect_snapshot(context=CTX)
    assert r.errors[0]["retryable"] is False and r.errors[0]["suggested_action"]
    # Timeout -> retryable; interpreter missing -> manual; bad target -> manual without a tool call.
    r = adapter_with(monkeypatch, FakeCLI(exc=subprocess.TimeoutExpired("cmd", 1))).collect_snapshot(context=CTX)
    assert r.errors[0]["error_code"] == "MARKET_CONTEXT_TIMEOUT" and r.errors[0]["retryable"] is True
    cli = FakeCLI(exc=FileNotFoundError("python"))
    r = adapter_with(monkeypatch, cli).collect_snapshot(context=CTX)
    assert r.errors[0]["error_code"] == "MARKET_CONTEXT_CLI_UNAVAILABLE" and r.errors[0]["retryable"] is False
    r = adapter_with(monkeypatch, cli).collect_snapshot(context={"context_id": "x"})
    assert r.errors[0]["error_code"] == "MARKET_CONTEXT_INVALID_TARGET" and len(cli.cmds) == 1


# ---------------------------------------------------------------------------
# Persistence / migration / coverage
# ---------------------------------------------------------------------------
def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "intel.db")
    store.init_schema()
    return store


def _target(context_id="index_csi_300", symbol="000300", context_type="equity_index"):
    return {"target_type": "market_context", "target_id": context_id, "context_id": context_id, "context_type": context_type,
            "name": context_id, "symbol": symbol, "collect_mic": False, "collect_stock": False}


def test_persistence_stores_data_date_freshness_and_provenance(tmp_path, monkeypatch):
    store = _store(tmp_path)
    payload = tool_payload(value=4357.6, data_date="2026-06-30", status="partial_success", changes={"1p": change(0.28, 1)},
                           quality={"status": "stale", "is_fresh": False, "staleness_days": 6})
    result = adapter_with(monkeypatch, FakeCLI(payload)).collect_snapshot(context=CTX, as_of=AS_OF)
    ResultPersister(store).save_market_context_snapshot(task={"task_id": "t", "target": _target(), "as_of": AS_OF}, result=result, run_id="run_x")
    with store.session() as con:
        row = dict(con.execute("SELECT * FROM market_context_snapshots").fetchone())
    assert row["as_of"] == AS_OF and row["data_date"] == "2026-06-30" and row["is_fresh"] == 0 and row["freshness_status"] == "stale"
    assert row["provider"] == "akshare" and row["source_api"] == "stock_zh_index_daily" and row["metric"] == "close" and row["change_kind"] == "percent"
    assert loads_json(row["provenance_json"])["record_ids"] == ["rec_1"]
    assert loads_json(row["quality_json"])["staleness_days"] == 6
    assert loads_json(row["payload_json"])["run_id"] == "run_x"


def test_v10_migration_adds_columns_to_legacy_database(tmp_path):
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO schema_migrations(version) VALUES (9);
        CREATE TABLE market_context_snapshots (
          snapshot_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, context_type TEXT NOT NULL, name TEXT, symbol TEXT,
          as_of TEXT NOT NULL, value REAL, unit TEXT, change_1d REAL, change_5d REAL, change_20d REAL, source_url TEXT,
          payload_json TEXT NOT NULL DEFAULT '{}', idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO market_context_snapshots(snapshot_id, context_id, context_type, as_of, value, unit, payload_json, idempotency_key)
          VALUES ('old1', 'index_csi_300', 'equity_index', '2026-07-01', 4000.0, 'index_points', '{"akshare_func":"stock_zh_index_daily_em"}', 'k1');
        """
    )
    con.commit()
    con.close()
    store = SQLiteStore(db)
    store.init_schema()
    with store.session() as c:
        versions = {r["version"] for r in c.execute("SELECT version FROM schema_migrations")}
        row = dict(c.execute("SELECT * FROM market_context_snapshots WHERE snapshot_id='old1'").fetchone())
    assert 10 in versions
    # Legacy row survives; unknown facts stay NULL instead of being invented.
    assert row["value"] == 4000.0 and row["data_date"] is None and row["is_fresh"] is None and row["provider"] is None
    assert row["quality_json"] == "{}" and row["provenance_json"] == "{}"
    store.init_schema()  # idempotent


def test_coverage_counts_only_fresh_rows(tmp_path, monkeypatch):
    store = _store(tmp_path)
    DemandRegistry(store, SQLiteMessageQueue(store)).register({
        "schema_version": "demand.v1", "demand_id": "demand_market_context_daily", "demand_type": "market_context_daily",
        "source_type": "research_pool_request", "status": "active",
        "targets": [_target("index_csi_300"), _target("fx_hkd_cny", "HKDCNY", "fx"), _target("rate_cn_cgb_10y", "CN_CGB_10Y", "interest_rate"), _target("legacy_ctx")],
    })
    persister = ResultPersister(store)
    fresh = adapter_with(monkeypatch, FakeCLI(tool_payload(value=4026.0, data_date=AS_OF))).collect_snapshot(context=CTX, as_of=AS_OF)
    persister.save_market_context_snapshot(task={"task_id": "t1", "target": _target(), "as_of": AS_OF}, result=fresh)
    stale_payload = tool_payload(value=1.68, data_date="2026-06-30", context_id="rate_cn_cgb_10y", context_type="interest_rate", symbol="CN_CGB_10Y",
                                 metric="rate_value", unit="percent", status="partial_success",
                                 quality={"status": "stale", "is_fresh": False, "staleness_days": 6},
                                 changes={"1p": change(0.0152, 1, kind="percentage_point", unit="observation")})
    stale = adapter_with(monkeypatch, FakeCLI(stale_payload)).collect_snapshot(
        context={"context_id": "rate_cn_cgb_10y", "context_type": "interest_rate", "symbol": "CN_CGB_10Y"}, as_of=AS_OF)
    persister.save_market_context_snapshot(task={"task_id": "t2", "target": _target("rate_cn_cgb_10y", "CN_CGB_10Y", "interest_rate"), "as_of": AS_OF}, result=stale)
    legacy = ToolResult(tool_name="market_context_collector", operation="market_context_snapshot", request={}, status="success",
                        result={"context_id": "legacy_ctx", "context_type": "equity_index", "as_of": AS_OF, "value": 1.0})
    persister.save_market_context_snapshot(task={"task_id": "t3", "target": _target("legacy_ctx"), "as_of": AS_OF}, result=legacy)

    out = CoverageEvaluator(store).market_context_coverage(trade_date=AS_OF)
    assert out["expected_contexts"] == 4 and out["contexts_with_snapshot"] == 3
    assert out["contexts_fresh"] == 1 and out["coverage_ratio"] == 0.25
    assert out["stale_contexts"] == ["rate_cn_cgb_10y"] and out["unknown_freshness"] == ["legacy_ctx"]
    assert out["missing_snapshot"] == ["fx_hkd_cny"]
    rate_row = next(r for r in out["rows"] if r["context_id"] == "rate_cn_cgb_10y")
    assert rate_row["data_date"] == "2026-06-30" and rate_row["staleness_days"] == 6 and rate_row["change_kind"] == "percentage_point"
    assert rate_row["provider"] == "akshare"


# ---------------------------------------------------------------------------
# Agent task handling
# ---------------------------------------------------------------------------
def _agent(root: Path) -> IntelligenceCollectorAgent:
    root.mkdir(parents=True, exist_ok=True)
    config = CollectorConfig(
        raw={"capability_verification": {"run_on_startup": False}, "quality": {},
             "tools": {"market_context_collector": {"enabled": True, "timeout_seconds": 5}}},
        path=root / "config.yaml",
        runtime=RuntimeConfig(agent_id="mctx_test", agent_group="intelligence_collector", state_sqlite_path=root / "state.db",
                              bus_sqlite_path=root / "bus.db", data_sqlite_path=root / "data.db", workspace_root=root,
                              log_dir=root / "logs", reports_dir=root / "reports"),
        model=AgentModelConfig(primary="offline/no_model_call", fallbacks=[], require_registered=False),
        tools=ToolConfig(mic_enabled=False, stock_enabled=True, mic_config_dir=None, stock_config_dir="/stock/cfg",
                         python_executable="/venv/bin/python", stock_working_dir="/stock"),
    )
    return IntelligenceCollectorAgent(config)


def _dispatch(agent: IntelligenceCollectorAgent, suffix: str, target=None):
    task = {"task_id": f"task-{suffix}", "task_type": "market_context_snapshot", "tool_name": "market_context_collector",
            "idempotency_key": f"mctx-task-{suffix}", "as_of": f"{AS_OF}T09:00:00+08:00", "demand_id": "demand_market_context_daily",
            "target": target or _target()}
    ticket_id = agent.tickets.create_ticket(ticket_type="COLLECTION_TASK_TICKET", source_agent="t", target_agent_id=agent.config.runtime.agent_id,
                                            priority="normal", payload=task)
    agent.queue.publish("intelligence.collection", {"ticket_id": ticket_id}, target_agent_id=agent.config.runtime.agent_id, idempotency_key=f"m-{suffix}")
    agent.run_once(topics=["intelligence.collection"])
    return ticket_id


def _issues(agent, issue_type):
    with agent.data_store.session() as con:
        return [dict(r) for r in con.execute("SELECT * FROM data_quality_issues WHERE issue_type=?", (issue_type,))]


def test_agent_wires_adapter_with_stock_tool_settings(tmp_path):
    agent = _agent(tmp_path / "a")
    mc = agent.market_context
    assert (mc.python_executable, mc.config_dir, mc.working_dir, mc.timeout_seconds) == ("/venv/bin/python", "/stock/cfg", "/stock", 5)
    assert (mc.python_executable, mc.config_dir, mc.working_dir) == (agent.stock.python_executable, agent.stock.config_dir, agent.stock.working_dir)


def test_agent_fresh_snapshot_counts_as_coverage(tmp_path, monkeypatch):
    agent = _agent(tmp_path / "a")
    cli = FakeCLI(tool_payload(value=4026.0, data_date=AS_OF, changes={"1p": change(0.02, 1)}))
    monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: cli(cmd))
    ticket_id = _dispatch(agent, "fresh")
    assert cli.cmds[0][0] == "/venv/bin/python" and cli.cmds[0][3:5] == ["--config-dir", "/stock/cfg"]
    assert agent.tickets.get(ticket_id)["status"] == "done"
    cov = CoverageEvaluator(agent.data_store).market_context_coverage(trade_date=AS_OF)
    assert cov["contexts_fresh"] == 1 and cov["rows"][0]["data_date"] == AS_OF
    assert _issues(agent, "market_context_stale") == [] and _issues(agent, "market_context_collect_failed") == []


def test_agent_upstream_quarantined_value_does_not_count_as_coverage(tmp_path, monkeypatch):
    agent = _agent(tmp_path / "a")
    payload = tool_payload(value=4026.0, data_date=AS_OF, status="failed", quality={
        "usable": False, "status": "failed", "is_fresh": True,
        "warnings": ["upstream_validation_blocked: quarantined; value retained for inspection only"],
    })
    cli = FakeCLI(payload)
    monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: cli(cmd))
    ticket_id = _dispatch(agent, "quarantined")
    assert agent.tickets.get(ticket_id)["status"] == "failed"
    cov = CoverageEvaluator(agent.data_store).market_context_coverage(trade_date=AS_OF)
    assert cov["contexts_with_snapshot"] == 0 and cov["contexts_fresh"] == 0
    assert _issues(agent, "market_context_collect_failed")


def test_agent_unknown_snapshot_date_is_not_saved_and_is_retried(tmp_path, monkeypatch):
    """Tool could not confirm the data date of a realtime snapshot (SNAPSHOT_DATE_UNCONFIRMED).

    The agent must not store it as a valid snapshot, must not report it as 'today, fresh',
    and must requeue the ticket because the error is retryable.
    """
    agent = _agent(tmp_path / "a")
    payload = tool_payload(
        value=None, data_date=None, status="failed", metric="latest", unit="CNY/ton",
        context_id="commodity_cu0", context_type="commodity", symbol="CU0",
        quality={"usable": False, "status": "unknown_date", "is_fresh": None, "staleness_days": None, "data_date_matches_as_of": None,
                 "warnings": ["snapshot_date_unconfirmed: copper/CU0 realtime snapshot has no confirmed data date"]},
        errors=[{"error_code": "SNAPSHOT_DATE_UNCONFIRMED", "retryable": True,
                 "error_message": "realtime snapshot of copper/CU0 has no confirmable data date",
                 "suggested_action": "Retry after the next session's daily bar is published, or use frequency=1d."}],
    )
    payload["result"]["observed_at"] = None
    cli = FakeCLI(payload)
    monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: cli(cmd))
    target = {**_target("commodity_cu0", "CU0", "commodity"), "frequency": "realtime"}
    ticket_id = _dispatch(agent, "unknown-date", target=target)
    assert agent.tickets.get(ticket_id)["status"] == "open", "retryable: scheduled for retry, not failed/done"
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM market_context_snapshots").fetchone()[0] == 0
    cov = CoverageEvaluator(agent.data_store).market_context_coverage(trade_date=AS_OF)
    assert cov["contexts_with_snapshot"] == 0 and cov["contexts_fresh"] == 0
    issues = _issues(agent, "market_context_collect_failed")
    assert issues and "SNAPSHOT_DATE_UNCONFIRMED" in issues[0]["payload_json"]
    mapped = adapter_with(monkeypatch, FakeCLI(payload)).collect_snapshot(context=target, as_of=AS_OF)
    assert mapped.status == "failed" and mapped.quality["status"] == "unknown_date" and mapped.quality["is_fresh"] is None
    assert mapped.result["data_date"] is None and mapped.result["observed_at"] is None and mapped.result["value"] is None
    assert mapped.errors[0]["error_code"] == "SNAPSHOT_DATE_UNCONFIRMED" and mapped.errors[0]["retryable"] is True


def test_agent_business_param_mismatch_is_manual_not_retried(tmp_path, monkeypatch):
    """Tool rejected contract/instrument_type/tenor inconsistent with the symbol (INVALID_REQUEST)."""
    agent = _agent(tmp_path / "a")
    message = "symbol=CU0 的 contract 配置为 CU0，请求 contract=CU2612，与 symbol 绑定不一致。请使用已注册的 CU2612 symbol。"
    payload = tool_payload(value=None, data_date=None, status="failed", context_id="commodity_cu0", context_type="commodity", symbol="CU0",
                           quality={"usable": False, "status": "failed", "is_fresh": None},
                           errors=[{"error_code": "INVALID_REQUEST", "error_message": message, "retryable": False}])
    cli = FakeCLI(payload)
    monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: cli(cmd))
    target = {**_target("commodity_cu0", "CU0", "commodity"), "contract": "CU2612"}
    ticket_id = _dispatch(agent, "param-mismatch", target=target)
    assert "--contract" in cli.cmds[0] and cli.cmds[0][cli.cmds[0].index("--contract") + 1] == "CU2612"
    assert agent.tickets.get(ticket_id)["status"] == "failed"
    with agent.data_store.session() as con:
        assert con.execute("SELECT COUNT(*) FROM market_context_snapshots").fetchone()[0] == 0
    issues = _issues(agent, "market_context_collect_failed")
    assert issues and "需人工处理" in issues[0]["summary_cn"] and message in issues[0]["payload_json"]


def test_agent_stale_snapshot_is_saved_but_flagged(tmp_path, monkeypatch):
    agent = _agent(tmp_path / "a")
    payload = tool_payload(value=4357.6, data_date="2026-06-30", status="partial_success",
                           quality={"status": "stale", "is_fresh": False, "staleness_days": 6})
    monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: FakeCLI(payload)(cmd))
    ticket_id = _dispatch(agent, "stale")
    assert agent.tickets.get(ticket_id)["status"] == "done"
    cov = CoverageEvaluator(agent.data_store).market_context_coverage(trade_date=AS_OF)
    assert cov["contexts_with_snapshot"] == 1 and cov["contexts_fresh"] == 0 and cov["stale_contexts"] == ["index_csi_300"]
    issues = _issues(agent, "market_context_stale")
    assert len(issues) == 1 and issues[0]["severity"] == "P3" and "2026-06-30" in issues[0]["summary_cn"]
    with agent.bus_store.session() as con:
        results = [json.loads(r["payload_json"]) for r in con.execute("SELECT payload_json FROM messages WHERE topic='collection.result'")]
    assert results and results[0]["status"] == "stale" and results[0]["usable"] is False
    # A stale-but-answered request is not a tool outage: the breaker must not trip.
    assert not (agent.breaker.state("market_context_collector") or {}).get("consecutive_failures")


def test_agent_retryable_failure_requeues_and_config_failure_asks_for_manual_action(tmp_path, monkeypatch):
    agent = _agent(tmp_path / "a")
    monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: FakeCLI(rc=1, stdout="", stderr="ConnectionError: reset")(cmd))
    retry_ticket = _dispatch(agent, "retry")
    assert agent.tickets.get(retry_ticket)["status"] == "open"  # scheduled for retry via the queue
    with agent.bus_store.session() as con:
        retry_msgs = [dict(r) for r in con.execute("SELECT status, attempts FROM messages WHERE idempotency_key='m-retry'")]
    assert retry_msgs and retry_msgs[0]["status"] in {"open", "leased", "retry"} and retry_msgs[0]["attempts"] >= 1

    bad = tool_payload(value=None, data_date=None, status="failed", quality={"usable": False, "status": "failed"},
                       errors=[{"error_code": "INVALID_REQUEST", "error_message": "no market-context binding for commodity:XYZ", "retryable": False}])
    monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: FakeCLI(bad)(cmd))
    manual_ticket = _dispatch(agent, "manual", target=_target("c_xyz", "XYZ", "commodity"))
    assert agent.tickets.get(manual_ticket)["status"] == "failed"
    issues = [i for i in _issues(agent, "market_context_collect_failed") if i["target_id"] == "c_xyz"]
    assert len(issues) == 1 and "需人工处理" in issues[0]["summary_cn"] and "market_context_sources.yaml" in issues[0]["summary_cn"]
    with agent.bus_store.session() as con:
        manual_msgs = [dict(r) for r in con.execute("SELECT status FROM messages WHERE idempotency_key='m-manual'")]
    assert manual_msgs[0]["status"] in {"done", "acked", "completed", "dead"}


# ---------------------------------------------------------------------------
# Structured logging: trace propagation, --debug forwarding, tool stderr sink, agent_outcome
# ---------------------------------------------------------------------------
def _tool_stderr_line(event, trace_id, level="DEBUG", **extra):
    return json.dumps({"event": event, "timestamp": "2026-07-06T09:00:00+08:00", "level": level, "trace_id": trace_id, "request_id": "req_1",
                       "parent_request_id": None, "ingestion_run_id": None, "context_id": "index_csi_300", "context_type": "equity_index",
                       "symbol": "000300", "idempotency_key": None, **extra}, ensure_ascii=False)


def test_adapter_forwards_debug_and_trace_and_sinks_stderr_on_success_failure_and_timeout(monkeypatch, tmp_path):
    from agent_trade_intel.logging_setup import ToolLogSink

    sink = ToolLogSink(tmp_path / "logs" / "tool_stderr.jsonl")
    stderr = _tool_stderr_line("record_write", "trace-1") + "\n" + _tool_stderr_line("request_summary", "trace-1", level="INFO") + "\n"
    cli = FakeCLI(tool_payload(value=4026.0, data_date=AS_OF), stderr=stderr)
    adapter = adapter_with(monkeypatch, cli, debug=True, log_sink=sink)
    ok = adapter.collect_snapshot(context=CTX, as_of=AS_OF, trace_id="trace-1")
    cmd = cli.cmds[0]
    assert ok.status == "success"
    assert cmd.index("--debug") < cmd.index("fetch"), "--debug is a top-level tool CLI flag"
    assert cmd[cmd.index("--trace-id") + 1] == "trace-1"
    lines = [json.loads(l) for l in (tmp_path / "logs" / "tool_stderr.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [l["event"] for l in lines] == ["record_write", "request_summary"], "stderr is persisted on success, not only on failure"
    assert all(l["trace_id"] == "trace-1" for l in lines)

    # Failure: JSON lines kept verbatim, a traceback line is wrapped so the file stays JSONL.
    failed = FakeCLI(rc=1, stdout="", stderr=_tool_stderr_line("persist_failed", "trace-2", level="ERROR") + "\nTraceback (most recent call last):\n  boom\n")
    adapter_with(monkeypatch, failed, debug=False, log_sink=sink).collect_snapshot(context=CTX, as_of=AS_OF, trace_id="trace-2")
    assert "--debug" not in failed.cmds[0]
    lines = [json.loads(l) for l in (tmp_path / "logs" / "tool_stderr.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[2]["event"] == "persist_failed" and lines[2]["level"] == "ERROR"
    assert lines[3]["event"] == "tool_stderr_text" and lines[3]["trace_id"] == "trace-2" and lines[3]["line"].startswith("Traceback")
    assert lines[4]["event"] == "tool_stderr_text" and lines[4]["call_outcome"] == "rc=1"

    # Timeout: the partial stderr carried by TimeoutExpired is persisted too.
    timeout = subprocess.TimeoutExpired("cmd", 1, stderr=_tool_stderr_line("provider_call", "trace-3").encode("utf-8"))
    r = adapter_with(monkeypatch, FakeCLI(exc=timeout), log_sink=sink).collect_snapshot(context=CTX, trace_id="trace-3")
    assert r.errors[0]["error_code"] == "MARKET_CONTEXT_TIMEOUT"
    lines = [json.loads(l) for l in (tmp_path / "logs" / "tool_stderr.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["event"] == "provider_call" and lines[-1]["trace_id"] == "trace-3"
    # Without a sink nothing is written and nothing breaks.
    adapter_with(monkeypatch, FakeCLI(tool_payload(value=1.0, data_date=AS_OF), stderr="x")).collect_snapshot(context=CTX)


def test_agent_emits_agent_outcome_and_passes_ticket_trace(tmp_path, monkeypatch):
    import logging

    from agent_trade_intel.logging_setup import JsonlEventFormatter, PACKAGE_LOGGER, _StructuredEventFilter

    agent = _agent(tmp_path / "a")
    captured: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(json.loads(JsonlEventFormatter().format(record)))

    handler = Capture()
    handler.addFilter(_StructuredEventFilter())
    pkg = logging.getLogger(PACKAGE_LOGGER)
    pkg.addHandler(handler)
    old_level = pkg.level
    pkg.setLevel(logging.INFO)
    try:
        cli = FakeCLI(tool_payload(value=4026.0, data_date=AS_OF), stderr=_tool_stderr_line("request_summary", "ignored", level="INFO") + "\n")
        monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: cli(cmd))
        ticket_id = _dispatch(agent, "trace")
        stale = tool_payload(value=4357.6, data_date="2026-06-30", status="partial_success", quality={"status": "stale", "is_fresh": False, "staleness_days": 6})
        monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: FakeCLI(stale)(cmd))
        stale_ticket = _dispatch(agent, "trace-stale")
        monkeypatch.setattr(MarketContextAdapter, "_run_cli", lambda self, cmd: FakeCLI(rc=1, stdout="", stderr="ConnectionError")(cmd))
        retry_ticket = _dispatch(agent, "trace-retry")
    finally:
        pkg.removeHandler(handler)
        pkg.setLevel(old_level)

    outcomes = [e for e in captured if e["event"] == "agent_outcome"]
    assert len(outcomes) == 3 and all(e["level"] == "INFO" for e in outcomes)
    ticket = agent.tickets.get(ticket_id)
    first = outcomes[0]
    assert first["trace_id"] == (ticket.get("correlation_id") or ticket_id)
    assert cli.cmds[0][cli.cmds[0].index("--trace-id") + 1] == first["trace_id"], "the tool receives the same trace id"
    assert first["ticket_id"] == ticket_id and first["request_id"] == "mctx_test" and first["tool_request"]["symbol"] == "000300"
    assert first["snapshot_saved"] is True and first["counted_as_coverage"] is True and first["final_status"] == "success"
    assert first["value"] == 4026.0 and first["data_date"] == AS_OF and first["is_fresh"] is True
    assert {"request_id", "parent_request_id", "ingestion_run_id", "idempotency_key", "context_id", "context_type", "symbol"} <= set(first)
    second = outcomes[1]
    assert second["ticket_id"] == stale_ticket and second["snapshot_saved"] is True and second["counted_as_coverage"] is False and second["final_status"] == "stale"
    third = outcomes[2]
    assert third["ticket_id"] == retry_ticket and third["snapshot_saved"] is False and third["counted_as_coverage"] is False
    assert third["final_status"] == "failed" and third["message_action"] == "retry" and third["error_codes"] == ["MARKET_CONTEXT_CLI_FAILED"]
    # Tool stderr of the successful call was copied by the agent into its own log dir.
    sunk = (tmp_path / "a" / "logs" / "tool_stderr.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(l).get("event") == "request_summary" for l in sunk)
    assert agent.market_context.debug is False, "package logger at INFO -> tool not asked for DEBUG"


def test_agent_cli_debug_flag_enables_debug_and_tool_forwarding(tmp_path, monkeypatch):
    import logging

    from agent_trade_intel import logging_setup

    monkeypatch.setattr(logging_setup, "_configured", False)
    pkg = logging.getLogger(logging_setup.PACKAGE_LOGGER)
    old_handlers, old_level = list(pkg.handlers), pkg.level
    try:
        pkg.handlers.clear()
        logging_setup.setup_logging(tmp_path / "logs", level="INFO", debug=True)
        assert pkg.level == logging.DEBUG and logging_setup.debug_enabled()
        assert any(getattr(h, "baseFilename", "").endswith("agent.jsonl") for h in pkg.handlers)
        agent = _agent(tmp_path / "a")
        assert agent.market_context.debug is True and agent.market_context.log_sink is not None
        logging_setup.emit(logging_setup.get_logger("t"), logging.INFO, "probe", trace_id="t-1", foo="bar")
        for h in pkg.handlers:
            h.flush()
        lines = [json.loads(l) for l in (tmp_path / "logs" / "agent.jsonl").read_text(encoding="utf-8").splitlines()]
        assert lines[-1]["event"] == "probe" and lines[-1]["trace_id"] == "t-1" and lines[-1]["foo"] == "bar" and lines[-1]["parent_request_id"] is None
        rotating = next(h for h in pkg.handlers if getattr(h, "baseFilename", "").endswith("agent.jsonl"))
        assert rotating.maxBytes == 20 * 1024 * 1024 and rotating.backupCount == 5
    finally:
        for h in pkg.handlers:
            h.close()
        pkg.handlers.clear()
        pkg.handlers.extend(old_handlers)
        pkg.setLevel(old_level)
        monkeypatch.setattr(logging_setup, "_configured", False)


# ---------------------------------------------------------------------------
# Request center field ownership
# ---------------------------------------------------------------------------
def test_request_center_rejects_targets_without_business_identity(tmp_path):
    import pytest
    import yaml

    from agent_trade_intel.config import load_config

    mic_dir = tmp_path / "mic_config"
    mic_dir.mkdir()
    (mic_dir / "target_profiles.yaml").write_text(yaml.safe_dump({"target_profiles": {}}), encoding="utf-8")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "agent": {"agent_id": "t", "agent_group": "intelligence_collector"},
        "openclaw": {"model": {"primary": "openai/gpt-5.5", "fallbacks": [], "require_registered": False, "allow_openclaw_default": False}},
        "runtime": {"sqlite_path": str(tmp_path / "intel.db"), "workspace_root": str(tmp_path), "log_dir": str(tmp_path / "logs"), "timezone": "Asia/Shanghai"},
        "tools": {"market_intelligence_collector": {"enabled": True, "config_dir": str(mic_dir)}},
    }, allow_unicode=True), encoding="utf-8")
    store = _store(tmp_path)
    center = RequestCenter(load_config(cfg_path), data_store=store, bus_store=store)

    with pytest.raises(ValueError, match="context_type"):
        center.request_batch({"market_contexts": [{"context_id": "x", "akshare_func": "f"}]})
    with pytest.raises(ValueError, match="symbol"):
        center.request_batch({"market_contexts": [{"context_id": "x", "context_type": "fx"}]})
    out = center.request_batch({"market_contexts": [
        {"context_id": "fx_old", "context_type": "fx", "symbol": "CNYHKD", "akshare_func": "currency_boc_sina", "value_column": "现汇卖出价", "unit": "CNY_per_100HKD"},
        {"context_id": "cu", "context_type": "commodity", "symbol": "CU0", "frequency": "realtime", "instrument_type": "futures", "metrics": "latest", "max_staleness_days": 0},
    ]})
    warnings = out["warnings"]
    assert any("fx_old" in w and "akshare_func" in w for w in warnings)
    assert any("CNYHKD" in w and "HKDCNY" in w for w in warnings), "direction confusion must be surfaced"
    targets = {t["context_id"]: t for t in center.registry.get("demand_market_context_daily")["targets"]}
    assert "akshare_func" not in targets["fx_old"] and "unit" not in targets["fx_old"]
    assert targets["cu"]["frequency"] == "realtime" and targets["cu"]["metrics"] == ["latest"] and targets["cu"]["max_staleness_days"] == 0
