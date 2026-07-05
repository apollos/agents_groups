from __future__ import annotations

from typing import Any

from .adapters.common import ToolResult
from .db import SQLiteStore, dumps_json
from .ids import make_idempotency_key, new_id, stable_hash, utc_now_iso


class ResultPersister:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def save_run(self, *, task: dict[str, Any], ticket_id: str | None, result: ToolResult, demand_id: str | None = None) -> str:
        run_id = new_id("run")
        idem = make_idempotency_key("run", task.get("idempotency_key"), result.tool_name, result.operation)
        with self.store.session() as con:
            existing = con.execute("SELECT run_id FROM collection_runs WHERE idempotency_key=?", (idem,)).fetchone()
            if existing:
                return str(existing["run_id"])
            con.execute(
                """
                INSERT INTO collection_runs(
                  run_id, task_id, ticket_id, demand_id, tool_name, operation, status,
                  started_at, completed_at, request_json, result_json, result_ref,
                  raw_result_ref, quality_json, errors_json, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task.get("task_id"),
                    ticket_id,
                    demand_id or task.get("demand_id"),
                    result.tool_name,
                    result.operation,
                    result.status,
                    result.started_at,
                    result.completed_at,
                    dumps_json(result.request),
                    dumps_json(result.result),
                    result.result_ref,
                    result.raw_result_ref,
                    dumps_json(result.quality),
                    dumps_json(result.errors),
                    idem,
                ),
            )
        return run_id

    def save_mic_structures(self, *, task: dict[str, Any], result: ToolResult) -> dict[str, int]:
        report = result.result if isinstance(result.result, dict) else {}
        counts = {"events": 0, "coverage_gaps": 0}
        target = task.get("target") or {}
        target_id = target.get("target_id") or task.get("target_id")
        ticker = target.get("ticker")
        company_name = target.get("company_name")
        # Prefer the full event list (MIC >= V0.7.3); top_events keeps older reports working.
        # Coverage accounting must not lose minor events that miss the top-5 display cut.
        top_events = report.get("all_events") or report.get("top_events") or []
        with self.store.session() as con:
            retrieved_at = utc_now_iso()
            for ev in top_events:
                summary = ev.get("summary") or ev.get("summary_cn") or str(ev)[:200]
                event_type = ev.get("event_type") or "other"
                event_date = ev.get("event_date") or ev.get("date")
                # Evidence fields from MIC's per-event source block (nested or flat legacy shape).
                source = ev.get("source") or {}
                source_url = source.get("url") or ev.get("source_url")
                source_domain = source.get("domain") or ev.get("source_domain")
                source_type = source.get("source_type") or ev.get("source_type")
                published_at = source.get("published_at") or ev.get("published_at")
                query_family = source.get("query_family") or ev.get("query_family")
                idem = make_idempotency_key("event", target_id or ticker, event_type, event_date or "unknown", stable_hash(summary, 12))
                existing = con.execute("SELECT event_id FROM structured_events WHERE idempotency_key=?", (idem,)).fetchone()
                if existing:
                    continue
                con.execute(
                    """
                    INSERT INTO structured_events(
                      event_id, target_id, ticker, company_name, event_type, event_date,
                      summary_cn, impact_json, source_refs_json,
                      source_url, source_domain, source_type, published_at, retrieved_at, query_family,
                      confidence, data_quality, source_run_id, payload_json, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("event"),
                        target_id,
                        ticker,
                        company_name,
                        event_type,
                        event_date,
                        summary,
                        dumps_json(ev.get("impact", {})),
                        dumps_json([ev.get("source_link_id")] if ev.get("source_link_id") else []),
                        source_url,
                        source_domain,
                        source_type,
                        published_at,
                        retrieved_at,
                        query_family,
                        ev.get("confidence"),
                        ev.get("data_quality"),
                        report.get("search_run_id"),
                        dumps_json(ev),
                        idem,
                    ),
                )
                counts["events"] += 1
            # coverage_gaps are not always returned inline. If MIC returns count only, this stays 0.
            for gap in report.get("coverage_gaps", []) or []:
                description = gap.get("description") or str(gap)[:300]
                gap_id = gap.get("gap_id") or new_id("gap")
                con.execute(
                    """
                    INSERT OR IGNORE INTO coverage_gaps(
                      gap_id, target_id, ticker, priority, status, description,
                      suggested_next_queries_json, source_run_id
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
                    """,
                    (
                        gap_id,
                        target_id,
                        ticker,
                        gap.get("priority", "normal"),
                        description,
                        dumps_json(gap.get("suggested_next_queries", [])),
                        report.get("search_run_id"),
                    ),
                )
                counts["coverage_gaps"] += 1
        return counts

    def save_quality_issue(self, *, severity: str, issue_type: str, summary_cn: str, payload: dict[str, Any], ticker: str | None = None, target_id: str | None = None, tool_name: str | None = None, request_id: str | None = None) -> str:
        issue_id = new_id("dq")
        with self.store.session() as con:
            con.execute(
                """
                INSERT INTO data_quality_issues(
                  issue_id, severity, issue_type, target_id, ticker, tool_name, request_id,
                  summary_cn, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_id, severity, issue_type, target_id, ticker, tool_name, request_id, summary_cn, dumps_json(payload)),
            )
        return issue_id
