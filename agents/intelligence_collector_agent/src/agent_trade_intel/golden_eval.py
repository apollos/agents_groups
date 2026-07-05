"""Golden-set recall evaluation (V0.8).

A golden set is a hand-curated YAML list of events that *should* have been captured
(e.g. a known buyback announcement). The evaluator checks each expected event against
structured_events (+ event_variable_links) and reports recall. It runs offline against
the data store; with no data every item simply reports matched=false.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .db import SQLiteStore


class GoldenSetEvaluator:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def evaluate(self, golden_file: str | Path) -> dict[str, Any]:
        spec = yaml.safe_load(Path(golden_file).read_text(encoding="utf-8")) or {}
        expected = spec.get("golden_events") or []
        results = []
        hits = 0
        with self.store.session() as con:
            for item in expected:
                row = self._match_one(con, item)
                matched = row is not None
                hits += int(matched)
                results.append(
                    {
                        "expected_event_id": item.get("expected_event_id"),
                        "target_id": item.get("target_id"),
                        "matched": matched,
                        "matched_event_id": row["event_id"] if row else None,
                        "matched_summary": row["summary_cn"] if row else None,
                        "notes": item.get("notes"),
                    }
                )
        return {
            "expected_count": len(expected),
            "matched_count": hits,
            "recall": round(hits / len(expected), 4) if expected else None,
            "missed": [r["expected_event_id"] for r in results if not r["matched"]],
            "results": results,
        }

    def _match_one(self, con, item: dict[str, Any]):
        start, end = item["date_range"]
        params: list[Any] = [item.get("target_id"), str(start), str(end)]
        where = [
            "ev.target_id = ?",
            "COALESCE(ev.event_date, substr(ev.created_at, 1, 10)) BETWEEN ? AND ?",
        ]
        keywords = item.get("keywords") or []
        if keywords:
            where.append("(" + " OR ".join(["ev.summary_cn LIKE ?"] * len(keywords)) + ")")
            params.extend(f"%{kw}%" for kw in keywords)
        variables = item.get("expected_variables") or []
        if variables:
            where.append(
                "EXISTS (SELECT 1 FROM event_variable_links evl "
                "WHERE evl.event_id = ev.event_id AND evl.tracking_variable IN ({}))".format(
                    ",".join("?" * len(variables))
                )
            )
            params.extend(variables)
        source_types = item.get("must_have_source_type") or []
        if source_types:
            where.append("ev.source_type IN ({})".format(",".join("?" * len(source_types))))
            params.extend(source_types)
        sql = (
            "SELECT ev.event_id, ev.summary_cn, ev.confidence FROM structured_events ev "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY ev.confidence DESC LIMIT 1"
        )
        return con.execute(sql, params).fetchone()
