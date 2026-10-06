"""Scenario 07: quarantined upstream data returned by the tool to the Agent (simulated, via real subprocess boundary).

Run: /home/yu/.venv/mydev/bin/python /tmp/mctx_verify/sim_agent_driver.py
The Agent spawns /tmp/mctx_verify/fake_python.sh -m stock_data_ingestion.cli --debug fetch market-context ...
(sim_cli.py = real tool CLI with FakeAK + newest HSTECH bar quarantined; temp SQLite).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

AGENT_DIR = Path("/home/yu/Workspace/agents_groups/agents/intelligence_collector_agent")
TOOL_DIR = Path("/home/yu/Workspace/agents_groups/tools/stock_data_collector")
NAME = "07_quarantined_via_agent"
EVIDENCE = AGENT_DIR / "docs/acceptance/evidence/market_context_review_20261006" / NAME
WORK = Path("/tmp/mctx_verify/work") / NAME
for p in (EVIDENCE, WORK):
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True)
os.environ["SIM_WORK_DIR"] = str(WORK)

from agent_trade_intel import logging_setup  # noqa: E402
from agent_trade_intel.adapters.market_context_adapter import MarketContextAdapter  # noqa: E402
from agent_trade_intel.agent import IntelligenceCollectorAgent  # noqa: E402
from agent_trade_intel.config import AgentModelConfig, CollectorConfig, RuntimeConfig, ToolConfig  # noqa: E402
from agent_trade_intel.evaluation import CoverageEvaluator  # noqa: E402

AS_OF = "2026-10-06"
log_dir = WORK / "logs"
logging_setup.setup_logging(log_dir, level="INFO", debug=True)  # == `intel-agent --debug`
logging_setup.set_default_log_fields(data_mode="simulated")

root = WORK / "agent"
root.mkdir()
config = CollectorConfig(
    raw={"capability_verification": {"run_on_startup": False}, "quality": {}, "tools": {"market_context_collector": {"enabled": True, "timeout_seconds": 120}}},
    path=root / "config.yaml",
    runtime=RuntimeConfig(agent_id="mctx_sim", agent_group="intelligence_collector", state_sqlite_path=root / "state.db", bus_sqlite_path=root / "bus.db",
                          data_sqlite_path=root / "data.db", workspace_root=root, log_dir=log_dir, reports_dir=root / "reports"),
    model=AgentModelConfig(primary="offline/no_model_call", fallbacks=[], require_registered=False),
    tools=ToolConfig(mic_enabled=False, stock_enabled=True, mic_config_dir=None, stock_config_dir=None,
                     python_executable="/tmp/mctx_verify/fake_python.sh", stock_working_dir=str(TOOL_DIR)),
)
agent = IntelligenceCollectorAgent(config)
assert agent.market_context.debug is True
commands: list[list[str]] = []
_orig_run = MarketContextAdapter._run_cli


def _spy(self, cmd):
    commands.append(list(cmd))
    return _orig_run(self, cmd)


MarketContextAdapter._run_cli = _spy

target = {"target_id": "index_hstech", "context_id": "index_hstech", "target_type": "market_context", "context_type": "hk_index", "name": "恒生科技指数", "symbol": "HSTECH", "metrics": ["close"]}
task = {"task_id": "task-quarantine", "task_type": "market_context_snapshot", "tool_name": "market_context_collector", "idempotency_key": "mctx-task-quarantine",
        "as_of": f"{AS_OF}T09:00:00+08:00", "demand_id": "demand_market_context_daily", "target": target}
ticket_id = agent.tickets.create_ticket(ticket_type="COLLECTION_TASK_TICKET", source_agent="verify", target_agent_id=config.runtime.agent_id, priority="normal", payload=task,
                                        correlation_id="corr-07-quarantine")
agent.queue.publish("intelligence.collection", {"ticket_id": ticket_id}, target_agent_id=config.runtime.agent_id, idempotency_key="m-quarantine")
agent.run_once(topics=["intelligence.collection"])

for h in __import__("logging").getLogger(logging_setup.PACKAGE_LOGGER).handlers:
    h.flush()

# ---- collect evidence -------------------------------------------------------------------
responses = sorted((WORK / "responses").glob("response_*.json"))
for i, p in enumerate(responses, 1):
    shutil.copy(p, EVIDENCE / f"response_{i}.json")
shutil.copy(log_dir / "tool_stderr.jsonl", EVIDENCE / "debug.jsonl")
shutil.copy(log_dir / "agent.jsonl", EVIDENCE / "agent.jsonl")
payload = json.loads(responses[0].read_text(encoding="utf-8"))
ticket = agent.tickets.get(ticket_id)
cov = CoverageEvaluator(agent.data_store).market_context_coverage(trade_date=AS_OF)
with agent.data_store.session() as con:
    snapshots = [dict(r) for r in con.execute("SELECT * FROM market_context_snapshots")]
    issues = [dict(r) for r in con.execute("SELECT issue_type, severity, summary_cn, target_id, payload_json FROM data_quality_issues")]
    runs = [dict(r) for r in con.execute("SELECT run_id, ticket_id, tool_name, operation, status FROM collection_runs")]
with agent.bus_store.session() as con:
    results = [json.loads(r["payload_json"]) for r in con.execute("SELECT payload_json FROM messages WHERE topic='collection.result'")]

tool_db = sqlite3.connect(WORK / "db.sqlite")
tool_db.row_factory = sqlite3.Row
head = [dict(r) for r in tool_db.execute("select record_id, index_code, trade_date, close, validation_status, request_id from index_bars where index_code='HSTECH' order by trade_date desc limit 3")]
stock_request_id = payload["result"]["provenance"]["stock_data_request_id"]
links = [dict(r) for r in tool_db.execute("select * from market_context_request_records where request_id=?", (stock_request_id,))]
agent_events = [json.loads(l) for l in (EVIDENCE / "agent.jsonl").read_text(encoding="utf-8").splitlines()]
outcome = next(e for e in agent_events if e["event"] == "agent_outcome")
tool_events = [json.loads(l) for l in (EVIDENCE / "debug.jsonl").read_text(encoding="utf-8").splitlines()]
changes = payload["result"]["changes"]

checks = {
    "agent_forwarded_debug_and_trace": "--debug" in commands[0] and commands[0][commands[0].index("--trace-id") + 1] == "corr-07-quarantine",
    "tool_response_failed_unusable": payload["status"] == "failed" and payload["result"]["quality"]["usable"] is False and payload["result"]["quality"]["status"] == "failed",
    "tool_value_retained_for_inspection": payload["result"]["value"] is not None and payload["result"]["data_date"] == "2026-10-05",
    "upstream_validation_blocked_warning": any(w.startswith("upstream_validation_blocked") for w in payload["result"]["quality"]["warnings"] + payload["warnings"]),
    "blocked_endpoint_changes_are_null_with_reason": bool(changes) and all(c["value"] is None and c.get("reason") for c in changes.values()),
    "head_bar_quarantined_in_tool_db": head[0]["validation_status"] == "quarantined",
    "request_record_links_written": len(links) > 0,
    "agent_ticket_failed_not_retried": ticket["status"] == "failed",
    "no_snapshot_saved": len(snapshots) == 0,
    "not_counted_in_coverage": cov["contexts_with_snapshot"] == 0 and cov["contexts_fresh"] == 0,
    "data_quality_issue_raised": any(i["issue_type"] == "market_context_collect_failed" for i in issues),
    "collection_result_failed_unusable": bool(results) and results[0]["status"] == "failed" and results[0]["usable"] is False,
    "agent_outcome_event": outcome["trace_id"] == "corr-07-quarantine" and outcome["snapshot_saved"] is False and outcome["counted_as_coverage"] is False and outcome["final_status"] == "failed" and outcome["request_id"] == payload["request_id"],
    "tool_stderr_sunk_with_same_trace": bool(tool_events) and all(e.get("trace_id") == "corr-07-quarantine" for e in tool_events if e["event"] != "tool_stderr_text"),
    "tool_events_include_quality_decision_and_summary": {"quality_decision", "request_summary", "upstream_validation_blocked", "record_write"} <= {e["event"] for e in tool_events},
    "data_mode_simulated_everywhere": all(e.get("data_mode") == "simulated" for e in tool_events if e["event"] != "tool_stderr_text") and outcome["data_mode"] == "simulated",
}
inputs = {
    "scenario": NAME,
    "description": "隔离数据：模拟 HSTECH 最新日线 validation_status=quarantined，经工具 CLI（真实子进程边界）返回 Agent。Agent 不保存快照、不计入覆盖；被拦截端点的涨跌幅为 null+reason。",
    "data_mode": "simulated",
    "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=TOOL_DIR, text=True).strip(),
    "driver": "sim_agent_driver.py + sim_cli.py + fake_python.sh (copied alongside)",
    "agent_cli_equivalent": "intel-agent --config ... --debug agent run-once  (setup_logging(debug=True); adapter forwards --debug/--trace-id)",
    "ticket": {"ticket_id": ticket_id, "correlation_id": "corr-07-quarantine", "task": task},
    "tool_command": commands[0],
    "tool_request_id": payload["request_id"],
    "stock_data_request_id": stock_request_id,
    "temp_tool_sqlite": str(WORK / "db.sqlite"),
    "temp_agent_sqlite": str(root / "data.db"),
    "checks": checks,
}
db_export = {
    "tool_index_bars_HSTECH_newest_3": head,
    "tool_request_record_links": links,
    "agent_market_context_snapshots": snapshots,
    "agent_collection_runs": runs,
    "agent_data_quality_issues": issues,
    "agent_collection_result_messages": results,
    "agent_ticket": {k: ticket.get(k) for k in ("ticket_id", "status", "correlation_id", "status_reason", "note", "updated_at") if k in ticket},
    "agent_coverage_2026-10-06": cov,
}
(EVIDENCE / "inputs.json").write_text(json.dumps(inputs, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
(EVIDENCE / "db_export.json").write_text(json.dumps(db_export, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
failed = {k: v for k, v in checks.items() if v is not True}
print(f"[{NAME}] checks: {len(checks) - len(failed)}/{len(checks)} passed" + (f"  FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
