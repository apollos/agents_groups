from __future__ import annotations

from typing import Any

from .adapters.common import ToolResult
from .db import SQLiteStore, dumps_json
from .ids import make_idempotency_key, new_id, stable_hash, utc_now_iso
from .variable_mapper import CandidateVariableMapper

# Model-attributed variable links at or above this confidence enter confirmed coverage
# directly; everything below (and every keyword candidate) stays pending for review.
MODEL_LINK_ACCEPT_CONFIDENCE = 0.65


class ResultPersister:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.candidate_mapper = CandidateVariableMapper()

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
        counts = {"events": 0, "coverage_gaps": 0, "event_variable_links": 0}
        target = task.get("target") or {}
        target_id = target.get("target_id") or task.get("target_id")
        ticker = target.get("ticker")
        company_name = target.get("company_name")
        allowed_variables = [str(v) for v in (target.get("tracking_variables") or [])]
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
                    event_id = str(existing["event_id"])
                else:
                    event_id = new_id("event")
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
                            event_id,
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
                # Variable links are saved even for duplicate events: an event first seen without
                # links can gain them once the target declares tracking_variables.
                counts["event_variable_links"] += self._save_event_variable_links(
                    con=con,
                    event_id=event_id,
                    target_id=target_id,
                    ticker=ticker,
                    event=ev,
                    allowed_variables=allowed_variables,
                )
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

    def _save_event_variable_links(
        self,
        *,
        con,
        event_id: str,
        target_id: str | None,
        ticker: str | None,
        event: dict[str, Any],
        allowed_variables: list[str],
    ) -> int:
        """Persist event -> tracking_variable links from model labels + keyword candidates.

        Model labels (mapping_method=mic_model) become accepted at high confidence and
        pending otherwise. Keyword candidates are always pending and never enter confirmed
        coverage; both methods can coexist per (event, variable) thanks to the composite key.
        """
        inserted = 0
        allowed = set(allowed_variables)
        for tv in event.get("tracking_variables") or []:
            item = {"variable": tv} if isinstance(tv, str) else dict(tv)
            variable = str(item.get("variable") or "").strip()
            if not variable:
                continue
            if allowed and variable not in allowed:
                # The model must select from the target's declared list; anything else is
                # a hallucinated variable name and would corrupt the coverage matrix.
                continue
            confidence = float(item.get("confidence") or 0)
            cur = con.execute(
                """
                INSERT OR IGNORE INTO event_variable_links(
                  event_id, target_id, ticker, tracking_variable, direction, strength,
                  mapping_method, mapping_confidence, review_status, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'mic_model', ?, ?, ?)
                """,
                (
                    event_id,
                    target_id,
                    ticker,
                    variable,
                    item.get("direction") or "unclear",
                    item.get("strength"),
                    confidence,
                    "accepted" if confidence >= MODEL_LINK_ACCEPT_CONFIDENCE else "pending",
                    dumps_json({"reasoning": item.get("reasoning") or ""}),
                ),
            )
            inserted += cur.rowcount if cur.rowcount > 0 else 0
        for candidate in self.candidate_mapper.map_event(event=event, allowed_variables=allowed_variables):
            cur = con.execute(
                """
                INSERT OR IGNORE INTO event_variable_links(
                  event_id, target_id, ticker, tracking_variable, direction, strength,
                  mapping_method, mapping_confidence, review_status, evidence_json
                ) VALUES (?, ?, ?, ?, ?, NULL, 'keyword_candidate', ?, 'pending', ?)
                """,
                (
                    event_id,
                    target_id,
                    ticker,
                    candidate.variable,
                    candidate.direction,
                    candidate.confidence,
                    dumps_json({"matched_keywords": candidate.evidence}),
                ),
            )
            inserted += cur.rowcount if cur.rowcount > 0 else 0
        return inserted

    def save_hk_connect_snapshot(self, *, task: dict[str, Any], result: ToolResult, run_id: str | None = None) -> int:
        """Upsert one HK-connect structured snapshot per (ticker, as_of)."""
        data = result.result if isinstance(result.result, dict) else {}
        target = task.get("target") or {}
        ticker = data.get("ticker") or target.get("ticker")
        as_of = data.get("as_of") or str(task.get("as_of") or "")[:10]
        idem = make_idempotency_key("hk_connect_snapshot", ticker, as_of)
        quality = result.quality if isinstance(result.quality, dict) else {}
        with self.store.session() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO hk_connect_snapshots(
                  snapshot_id, target_id, ticker, company_name, as_of,
                  hk_connect_eligible, last_price_hkd, turnover_hkd,
                  southbound_holding_shares, southbound_holding_market_value_hkd,
                  southbound_holding_pct, southbound_mv_change_1d, southbound_mv_change_5d,
                  southbound_mv_change_10d, buyback_amount_hkd, ah_premium_pct,
                  hk_liquidity_score, field_completeness_json, missing_fields_json,
                  provider_status_json, source_url, payload_json, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("hkc"),
                    target.get("target_id"),
                    ticker,
                    data.get("company_name") or target.get("company_name"),
                    as_of,
                    int(bool(data.get("hk_connect_eligible"))),
                    data.get("last_price_hkd"),
                    data.get("turnover_hkd"),
                    data.get("southbound_holding_shares"),
                    data.get("southbound_holding_market_value_hkd"),
                    data.get("southbound_holding_pct"),
                    data.get("southbound_mv_change_1d"),
                    data.get("southbound_mv_change_5d"),
                    data.get("southbound_mv_change_10d"),
                    data.get("buyback_amount_hkd"),
                    data.get("ah_premium_pct"),
                    data.get("hk_liquidity_score"),
                    dumps_json(quality.get("field_completeness") or {}),
                    dumps_json(quality.get("missing_fields") or []),
                    dumps_json(
                        {
                            "provider": quality.get("source"),
                            "unsourced_fields": quality.get("unsourced_fields") or [],
                            "errors": result.errors,
                        }
                    ),
                    data.get("source_url"),
                    dumps_json({**data, "run_id": run_id}),
                    idem,
                ),
            )
        return 1

    def save_market_context_snapshot(self, *, task: dict[str, Any], result: ToolResult, run_id: str | None = None) -> int:
        """Upsert one market-context snapshot per (context_id, as_of)."""
        data = result.result if isinstance(result.result, dict) else {}
        target = task.get("target") or {}
        context_id = data.get("context_id") or target.get("context_id") or target.get("target_id")
        as_of = data.get("as_of") or str(task.get("as_of") or "")[:10]
        idem = make_idempotency_key("market_context_snapshot", context_id, as_of)
        with self.store.session() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO market_context_snapshots(
                  snapshot_id, context_id, context_type, name, symbol, as_of,
                  value, unit, change_1d, change_5d, change_20d,
                  source_url, payload_json, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("mctx"),
                    context_id,
                    data.get("context_type") or target.get("context_type") or "market_context",
                    data.get("name") or target.get("name"),
                    data.get("symbol") or target.get("symbol"),
                    as_of,
                    data.get("value"),
                    data.get("unit"),
                    data.get("change_1d"),
                    data.get("change_5d"),
                    data.get("change_20d"),
                    data.get("source_url"),
                    dumps_json({**data, "run_id": run_id}),
                    idem,
                ),
            )
        return 1

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
