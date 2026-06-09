#!/usr/bin/env python3
"""End-to-end validation for the MIC pipeline (offline mock by default).

Runs a full ``collect_intelligence`` against a throwaway SQLite DB and asserts the
whole chain produced sane, persisted, queryable results without storing raw page
content. Exit code is 0 on PASS, 1 on FAIL.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import inspect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mic.api import AnalystAPI  # noqa: E402
from mic.config import load_config  # noqa: E402
from mic.logging_utils import configure_logging, logs_dir  # noqa: E402
from mic.store import get_database  # noqa: E402

logger = logging.getLogger("tools.e2e_validate")

_RAW_CONTENT_COLUMNS = {"html", "full_text", "raw_content", "screenshot", "page_body"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run MIC end-to-end validation")
    p.add_argument("--target-id", default="company_300750")
    p.add_argument("--focus",
                   default="operating_update,customer_change,supply_chain,policy,risk")
    p.add_argument("--time-window", default="30d")
    p.add_argument("--max-queries", type=int, default=12)
    p.add_argument("--max-links", type=int, default=6)
    p.add_argument("--max-model-calls", type=int, default=6)
    p.add_argument("--max-search-hits", type=int, default=120)
    p.add_argument("--db-path", default=None)
    p.add_argument("--log-dir", default=None)
    p.add_argument("--real", action="store_true",
                   help="Use real providers/models instead of offline mock")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def reset_db_singleton() -> None:
    import mic.store.database as dbmod
    if dbmod._DB is not None:
        try:
            dbmod._DB.engine.dispose()
        except Exception:  # noqa: BLE001
            pass
    dbmod._DB = None


def check_no_raw_content_columns(database_url: str) -> dict[str, Any]:
    db = get_database(database_url)
    inspector = inspect(db.engine)
    suspicious = []
    for table in inspector.get_table_names():
        for col in inspector.get_columns(table):
            if col["name"].lower() in _RAW_CONTENT_COLUMNS:
                suspicious.append(f"{table}.{col['name']}")
    return {"passed": not suspicious, "suspicious_columns": suspicious}


def add_check(checks: list[dict[str, Any]], name: str, condition: bool,
              detail: Any = None) -> None:
    checks.append({"name": name, "passed": bool(condition), "detail": detail})


def main() -> int:
    args = parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_directory = logs_dir(args.log_dir)
    log_path = configure_logging(log_dir=log_directory, log_file=f"e2e_{stamp}.log",
                                 console=not args.quiet)
    if not args.real:
        os.environ["MIC_ALLOW_MOCK"] = "true"
    db_path = Path(args.db_path) if args.db_path else log_directory / f"e2e_{stamp}.db"
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["MIC_DATABASE_URL"] = f"sqlite:///{db_path}"
    reset_db_singleton()

    task_profile = {
        "focus": [f.strip() for f in args.focus.split(",") if f.strip()],
        "time_window": args.time_window,
        "budget_profile": {
            "max_queries": args.max_queries,
            "max_search_hits": args.max_search_hits,
            "max_links_to_read": args.max_links,
            "max_model_calls": args.max_model_calls,
        },
    }
    logger.info("e2e_start target=%s db=%s", args.target_id, db_path)
    api = AnalystAPI(load_config())
    report = api.collect_intelligence(args.target_id, task_profile)
    run_id = report["search_run_id"]
    summary = report["summary"]
    structured = report["structured_outputs"]
    counts = api.repo.count_rows_for_run(run_id)
    links = api.repo.source_links_for_run(run_id, decision="read", limit=5)
    explanation = (api.explain_source_analysis(links[0]["source_link_id"])
                   if links else {})
    readbacks = {
        "events": api.get_recent_events(args.target_id, since=args.time_window),
        "metrics": api.get_metric_observations(args.target_id, since=args.time_window),
        "relations": api.get_relations(args.target_id, since="180d"),
        "risks": api.get_risks(args.target_id, since="180d"),
        "questions": api.get_analyst_questions(args.target_id),
        "facts": api.search_facts(args.target_id, query="订单 金额"),
        "coverage_gaps": api.get_coverage_gaps(args.target_id),
    }

    checks: list[dict[str, Any]] = []
    add_check(checks, "queries_executed", summary["queries_executed"] > 0, summary)
    add_check(checks, "search_hits", summary["search_hits"] > 0, summary)
    add_check(checks, "links_read", summary["links_read"] > 0, summary)
    add_check(checks, "model_budget_respected",
              summary["model_calls"] <= args.max_model_calls, summary)
    add_check(checks, "briefs_persisted", structured["briefs"] >= 1, structured)
    add_check(checks, "structured_signal_persisted",
              any(structured[k] > 0 for k in
                  ("facts", "metrics", "events", "relations", "risks")), structured)
    add_check(checks, "db_source_links",
              counts["source_links"] == summary["search_hits"], counts)
    add_check(checks, "db_model_outputs_present", counts["model_outputs"] >= 1, counts)
    add_check(checks, "api_readback",
              any(len(v) > 0 for v in readbacks.values()),
              {k: len(v) for k, v in readbacks.items()})
    add_check(checks, "explain_has_reason", bool(explanation.get("why_selected")),
              explanation)
    raw = check_no_raw_content_columns(os.environ["MIC_DATABASE_URL"])
    add_check(checks, "no_raw_content_columns", raw["passed"], raw)

    passed = all(c["passed"] for c in checks)
    validation = {
        "passed": passed,
        "timestamp_utc": stamp,
        "mode": "real" if args.real else "offline_mock",
        "target_id": args.target_id,
        "db_path": str(db_path),
        "log_path": str(log_path),
        "pipeline_log_path": report.get("log_file"),
        "report": report,
        "db_counts": counts,
        "top_read_links": links,
        "explanation": explanation,
        "readback_counts": {k: len(v) for k, v in readbacks.items()},
        "checks": checks,
    }
    report_path = log_directory / f"e2e_{stamp}_report.json"
    validation["json_path"] = str(report_path)
    report_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    logger.info("e2e_completed passed=%s report=%s", passed, report_path)

    if args.json:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
    else:
        print(f"{'PASS' if passed else 'FAIL'} MIC E2E validation")
        print(f"  run_id: {run_id}")
        print(f"  db: {db_path}")
        print(f"  tool_log: {log_path}")
        if report.get("log_file"):
            print(f"  pipeline_log: {report['log_file']}")
        print(f"  report: {report_path}")
        for check in checks:
            if not check["passed"]:
                print(f"  failed: {check['name']} => {check['detail']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
