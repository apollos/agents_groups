"""Research effectiveness evaluation (V0.8): coverage matrices over collected data.

The evaluators are read-only queries over the data store. They work from day one:
with no data they report coverage_ratio 0 / empty matrices instead of waiting for a
"large enough" collection history, so the research loop can be judged immediately.
"""

from __future__ import annotations

from typing import Any

from .db import SQLiteStore, loads_json
from .time_utils import local_day_utc_range

# source_type values counted as authoritative corroboration in the coverage matrix.
AUTHORITATIVE_SOURCE_TYPES = ("official", "exchange", "regulator", "company")


class CoverageEvaluator:
    def __init__(self, store: SQLiteStore, *, timezone: str = "Asia/Shanghai"):
        self.store = store
        self.timezone = timezone

    def target_variable_coverage(
        self,
        *,
        trade_date: str,
        demand_id: str | None = None,
        confirmed_only: bool = True,
    ) -> dict[str, Any]:
        """target x tracking_variable coverage matrix for one local trading day.

        Expected cells come from demand targets that declare tracking_variables; observed
        cells come from event_variable_links joined onto that day's structured_events.
        confirmed_only limits observations to review_status='accepted' links (model-confirmed
        or human-reviewed); with False, pending keyword candidates also count.
        """
        day_range = local_day_utc_range(trade_date, self.timezone)
        status_clause = "AND evl.review_status = 'accepted'" if confirmed_only else ""
        with self.store.session() as con:
            targets = self._load_targets(con, demand_id=demand_id)
            expected = [
                {
                    "target_id": target.get("target_id"),
                    "ticker": target.get("ticker"),
                    "tracking_variable": str(variable),
                }
                for target in targets
                for variable in target.get("tracking_variables") or []
            ]
            rows = con.execute(
                f"""
                SELECT
                    ev.target_id,
                    evl.tracking_variable,
                    COUNT(*) AS event_count,
                    MAX(ev.confidence) AS max_confidence,
                    MAX(ev.source_type IN ({",".join("?" * len(AUTHORITATIVE_SOURCE_TYPES))})) AS has_authoritative_source
                FROM structured_events ev
                JOIN event_variable_links evl ON ev.event_id = evl.event_id
                WHERE ev.created_at >= ? AND ev.created_at < ?
                {status_clause}
                GROUP BY ev.target_id, evl.tracking_variable
                """,
                (*AUTHORITATIVE_SOURCE_TYPES, *day_range),
            ).fetchall()
        observed = {(r["target_id"], r["tracking_variable"]): dict(r) for r in rows}
        matrix = []
        for item in expected:
            got = observed.get((item["target_id"], item["tracking_variable"]))
            matrix.append(
                {
                    **item,
                    "covered": bool(got),
                    "event_count": got["event_count"] if got else 0,
                    "max_confidence": got["max_confidence"] if got else None,
                    "has_authoritative_source": bool(got["has_authoritative_source"]) if got else False,
                }
            )
        covered = sum(1 for x in matrix if x["covered"])
        return {
            "trade_date": trade_date,
            "demand_id": demand_id,
            "confirmed_only": confirmed_only,
            "expected_cells": len(expected),
            "covered_cells": covered,
            "coverage_ratio": round(covered / len(expected), 4) if expected else None,
            "matrix": matrix,
        }

    def hk_connect_coverage(self, *, trade_date: str) -> dict[str, Any]:
        """Structured HK-connect coverage: which .HK targets have a snapshot for the day.

        Beyond row existence, field_completeness (V0.8.1) separates "has a snapshot" from
        "has a high-quality snapshot": low_completeness lists tickers whose required-field
        fill ratio is below 1.0.
        """
        with self.store.session() as con:
            rows = con.execute(
                """
                SELECT ticker, target_id, hk_connect_eligible, southbound_holding_pct,
                       southbound_holding_market_value_hkd, ah_premium_pct,
                       buyback_amount_hkd, turnover_hkd,
                       field_completeness_json, missing_fields_json
                FROM hk_connect_snapshots
                WHERE as_of = ?
                ORDER BY ticker
                """,
                (trade_date,),
            ).fetchall()
            expected = sorted(
                {
                    str(t.get("ticker"))
                    for t in self._load_targets(con, demand_id=None)
                    if str(t.get("ticker") or "").upper().endswith(".HK") and t.get("collect_hk_connect") is not False
                }
            )
        snapshot_tickers = {r["ticker"] for r in rows}
        out_rows = []
        ratios = []
        low_completeness = []
        for r in rows:
            completeness = loads_json(r["field_completeness_json"], {}) or {}
            ratio = completeness.get("ratio")
            if ratio is not None:
                ratios.append(float(ratio))
                if float(ratio) < 1.0:
                    low_completeness.append(
                        {
                            "ticker": r["ticker"],
                            "ratio": ratio,
                            "missing_fields": loads_json(r["missing_fields_json"], []) or [],
                        }
                    )
            row = {k: r[k] for k in r.keys() if not k.endswith("_json")}
            row["field_completeness"] = completeness
            out_rows.append(row)
        return {
            "trade_date": trade_date,
            "expected_hk_targets": len(expected),
            "hk_targets_with_snapshot": len(rows),
            "missing_snapshot": [t for t in expected if t not in snapshot_tickers],
            "missing_southbound": [r["ticker"] for r in rows if r["southbound_holding_pct"] is None],
            "avg_field_completeness": round(sum(ratios) / len(ratios), 4) if ratios else None,
            "low_completeness": low_completeness,
            "rows": out_rows,
        }

    def market_context_coverage(self, *, trade_date: str) -> dict[str, Any]:
        """Structured market-context coverage: which contexts have a snapshot for the day."""
        with self.store.session() as con:
            expected = sorted(
                {
                    str(t.get("context_id") or t.get("target_id"))
                    for t in self._load_targets(con, demand_id=None)
                    if t.get("target_type") == "market_context"
                }
            )
            rows = con.execute(
                """
                SELECT context_id, context_type, name, symbol, value, unit,
                       change_1d, change_5d, change_20d, source_url
                FROM market_context_snapshots
                WHERE as_of = ?
                ORDER BY context_id
                """,
                (trade_date,),
            ).fetchall()
        observed = {r["context_id"] for r in rows}
        return {
            "trade_date": trade_date,
            "expected_contexts": len(expected),
            "contexts_with_snapshot": len(rows),
            "missing_snapshot": [c for c in expected if c not in observed],
            "missing_value": [r["context_id"] for r in rows if r["value"] is None],
            "rows": [dict(r) for r in rows],
        }

    def _load_targets(self, con, demand_id: str | None) -> list[dict[str, Any]]:
        if demand_id:
            rows = con.execute(
                "SELECT payload_json FROM collection_demands WHERE demand_id=?", (demand_id,)
            ).fetchall()
        else:
            rows = con.execute("SELECT payload_json FROM collection_demands WHERE status='active'").fetchall()
        payloads = {}
        for row in rows:
            payload = loads_json(row["payload_json"], {})
            payloads[str(payload.get("demand_id"))] = payload
        targets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for payload in payloads.values():
            direct = list(payload.get("targets") or [])
            # derived_from_demands (runtime reference) resolves against the same table.
            for source_id in payload.get("derived_from_demands") or []:
                source = payloads.get(str(source_id))
                if source is None:
                    row = con.execute(
                        "SELECT payload_json FROM collection_demands WHERE demand_id=?", (str(source_id),)
                    ).fetchone()
                    source = loads_json(row["payload_json"], {}) if row else {}
                direct.extend(source.get("targets") or [])
            for target in direct:
                key = str(target.get("target_id") or target.get("ticker") or "")
                if key and key not in seen:
                    seen.add(key)
                    targets.append(target)
        return targets
