"""Research effectiveness aggregation for the dashboard (reviewer round 6).

The live dashboard was an ops-health board (queue / tickets / runs / heartbeats). This
service adds the second layer — "研究池是否有效" — by aggregating what the research loop
already persists: coverage matrices (CoverageEvaluator), HK-connect field completeness,
market context snapshots, research cards, quality issues / gaps and the latest persisted
golden-set recall. Everything is read-only; dashboard.py owns HTTP and page rendering.

Coverage numbers must match the CLI (`intel-agent eval coverage` etc.) — the service
delegates to the same evaluators instead of re-implementing the queries.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone as dt_timezone
from typing import Any

from .db import SQLiteStore, loads_json
from .evaluation import CoverageEvaluator, load_demand_targets

# Cards with confirmed coverage below this ratio are flagged for manual follow-up.
LOW_COVERAGE_CARD_RATIO = 0.3


class ResearchDashboardService:
    def __init__(self, *, data_store: SQLiteStore, timezone: str = "Asia/Shanghai"):
        self.data_store = data_store
        self.timezone = timezone

    # ---------------------------------------------------------------- summary

    def summary(self, *, trade_date: str, demand_id: str | None = None) -> dict[str, Any]:
        evaluator = CoverageEvaluator(self.data_store, timezone=self.timezone)
        confirmed = evaluator.target_variable_coverage(
            trade_date=trade_date, demand_id=demand_id, confirmed_only=True
        )
        candidate = evaluator.target_variable_coverage(
            trade_date=trade_date, demand_id=demand_id, confirmed_only=False
        )
        hk = evaluator.hk_connect_coverage(trade_date=trade_date)
        market = evaluator.market_context_coverage(trade_date=trade_date)

        target_meta = self._target_meta(self._load_targets(demand_id=demand_id))

        confirmed_keys = {
            (row["target_id"], row["tracking_variable"]) for row in confirmed["matrix"] if row["covered"]
        }
        candidate_keys = {
            (row["target_id"], row["tracking_variable"]) for row in candidate["matrix"] if row["covered"]
        }
        candidate_only = candidate_keys - confirmed_keys
        authoritative_covered_cells = sum(
            1 for row in confirmed["matrix"] if row["covered"] and row.get("has_authoritative_source")
        )

        research_cards = self._research_card_summary(demand_id=demand_id, trade_date=trade_date)
        quality = self._quality_summary()
        golden = self._latest_golden_run()

        return {
            "trade_date": trade_date,
            "demand_id": demand_id,
            "generated_at": datetime.now(dt_timezone.utc).isoformat(timespec="seconds"),
            "coverage": {
                "confirmed": self._compact_coverage(confirmed),
                "candidate_inclusive": self._compact_coverage(candidate),
                "candidate_only_cells": len(candidate_only),
                "authoritative_covered_cells": authoritative_covered_cells,
                "zero_covered_targets": self._zero_covered_targets(confirmed["matrix"], target_meta),
                "candidate_only_examples": self._candidate_only_examples(candidate_only, target_meta),
            },
            "groups": {
                "by_industry": self._group_coverage(confirmed["matrix"], target_meta, field="industry_id"),
                "by_theme": self._group_by_theme(confirmed["matrix"], target_meta),
                "by_pool_layer": self._group_coverage(confirmed["matrix"], target_meta, field="pool_layer"),
            },
            "hk_connect": hk,
            "market_context": market,
            "research_cards": research_cards,
            "quality": quality,
            "golden": golden,
            "research_health_score": self._research_health_score(
                confirmed=confirmed,
                authoritative_covered_cells=authoritative_covered_cells,
                hk=hk,
                market=market,
                research_cards=research_cards,
                quality=quality,
            ),
        }

    # --------------------------------------------------------------- coverage

    def coverage_matrix(
        self,
        *,
        trade_date: str,
        demand_id: str | None = None,
        include_candidates: bool = False,
    ) -> dict[str, Any]:
        evaluator = CoverageEvaluator(self.data_store, timezone=self.timezone)
        out = evaluator.target_variable_coverage(
            trade_date=trade_date, demand_id=demand_id, confirmed_only=not include_candidates
        )
        meta = self._target_meta(self._load_targets(demand_id=demand_id))
        for row in out["matrix"]:
            row.update(meta.get(row["target_id"], {}))
        return out

    # ----------------------------------------------------------------- target

    def target_detail(self, *, target_id: str) -> dict[str, Any]:
        with self.data_store.session() as con:
            card = con.execute(
                "SELECT card_json, updated_at FROM research_cards WHERE target_id=?", (target_id,)
            ).fetchone()
            events = con.execute(
                """
                SELECT event_id, event_type, event_date, summary_cn,
                       source_type, source_url, published_at, confidence, created_at
                FROM structured_events
                WHERE target_id=?
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (target_id,),
            ).fetchall()
            links = con.execute(
                """
                SELECT event_id, tracking_variable, direction, mapping_method,
                       mapping_confidence, review_status, created_at
                FROM event_variable_links
                WHERE target_id=?
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (target_id,),
            ).fetchall()
            gaps = con.execute(
                """
                SELECT gap_id, priority, description, suggested_next_queries_json, created_at
                FROM coverage_gaps
                WHERE target_id=? AND status='open'
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (target_id,),
            ).fetchall()
        research_card = None
        if card:
            research_card = loads_json(card["card_json"], {}) or {}
            research_card["updated_at"] = card["updated_at"]
        return {
            "target_id": target_id,
            "research_card": research_card,
            "events": [dict(row) for row in events],
            "variable_links": [dict(row) for row in links],
            "open_gaps": [
                dict(row) | {"suggested_next_queries": loads_json(row["suggested_next_queries_json"], [])}
                for row in gaps
            ],
        }

    # ---------------------------------------------------------------- helpers

    def _load_targets(self, demand_id: str | None = None) -> list[dict[str, Any]]:
        with self.data_store.session() as con:
            return load_demand_targets(con, demand_id)

    @staticmethod
    def _target_meta(targets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            str(t.get("target_id")): {
                "ticker": t.get("ticker"),
                "company_name": t.get("company_name") or t.get("name"),
                "industry_id": t.get("industry_id"),
                "theme_ids": t.get("theme_ids") or [],
                "pool_layer": t.get("pool_layer"),
            }
            for t in targets
            if t.get("target_id")
        }

    @staticmethod
    def _compact_coverage(obj: dict[str, Any]) -> dict[str, Any]:
        return {
            "expected_cells": obj.get("expected_cells", 0),
            "covered_cells": obj.get("covered_cells", 0),
            "coverage_ratio": obj.get("coverage_ratio"),
        }

    @staticmethod
    def _zero_covered_targets(
        matrix: list[dict[str, Any]], target_meta: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        total_by_target: dict[str, int] = defaultdict(int)
        covered_by_target: dict[str, int] = defaultdict(int)
        for row in matrix:
            total_by_target[row["target_id"]] += 1
            if row["covered"]:
                covered_by_target[row["target_id"]] += 1
        return [
            {"target_id": target_id, **target_meta.get(target_id, {}), "expected_variables": total}
            for target_id, total in total_by_target.items()
            if total > 0 and covered_by_target[target_id] == 0
        ]

    @staticmethod
    def _candidate_only_examples(
        candidate_only: set[tuple[str, str]],
        target_meta: dict[str, dict[str, Any]],
        *,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        return [
            {"target_id": target_id, "tracking_variable": variable, **target_meta.get(target_id, {})}
            for target_id, variable in sorted(candidate_only)[:limit]
        ]

    @staticmethod
    def _group_rows(groups: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
        return [
            {
                "group": key,
                "expected_cells": val["expected"],
                "covered_cells": val["covered"],
                "coverage_ratio": round(val["covered"] / val["expected"], 4) if val["expected"] else None,
                "authoritative_cells": val["authoritative"],
            }
            for key, val in sorted(groups.items())
        ]

    @classmethod
    def _group_coverage(
        cls,
        matrix: list[dict[str, Any]],
        target_meta: dict[str, dict[str, Any]],
        *,
        field: str,
    ) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, int]] = defaultdict(
            lambda: {"expected": 0, "covered": 0, "authoritative": 0}
        )
        for row in matrix:
            meta = target_meta.get(row["target_id"], {})
            group_key = str(meta.get(field) or "unknown")
            groups[group_key]["expected"] += 1
            if row["covered"]:
                groups[group_key]["covered"] += 1
                if row.get("has_authoritative_source"):
                    groups[group_key]["authoritative"] += 1
        return cls._group_rows(groups)

    @classmethod
    def _group_by_theme(
        cls, matrix: list[dict[str, Any]], target_meta: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, int]] = defaultdict(
            lambda: {"expected": 0, "covered": 0, "authoritative": 0}
        )
        for row in matrix:
            meta = target_meta.get(row["target_id"], {})
            for theme in meta.get("theme_ids") or ["unknown"]:
                groups[str(theme)]["expected"] += 1
                if row["covered"]:
                    groups[str(theme)]["covered"] += 1
                    if row.get("has_authoritative_source"):
                        groups[str(theme)]["authoritative"] += 1
        return cls._group_rows(groups)

    def _research_card_summary(self, *, demand_id: str | None, trade_date: str) -> dict[str, Any]:
        targets = self._load_targets(demand_id=demand_id)
        target_ids = {str(t.get("target_id")) for t in targets if t.get("target_id")}
        with self.data_store.session() as con:
            rows = con.execute(
                "SELECT target_id, ticker, company_name, as_of, card_json, updated_at "
                "FROM research_cards ORDER BY updated_at DESC"
            ).fetchall()
        cards = []
        for row in rows:
            if target_ids and row["target_id"] not in target_ids:
                continue
            card = loads_json(row["card_json"], {}) or {}
            cards.append(
                {
                    "target_id": row["target_id"],
                    "ticker": row["ticker"],
                    "company_name": row["company_name"],
                    "as_of": row["as_of"],
                    "updated_at": row["updated_at"],
                    "coverage_ratio": card.get("coverage_ratio"),
                    "missing_variables": card.get("missing_variables") or [],
                    "pool_layer_suggestion": card.get("pool_layer_suggestion"),
                    "open_questions_count": len(card.get("open_questions") or []),
                }
            )
        stale = [c for c in cards if str(c.get("as_of") or "") < trade_date]
        low = [
            c
            for c in cards
            if c.get("coverage_ratio") is not None and c["coverage_ratio"] < LOW_COVERAGE_CARD_RATIO
        ]
        suggestions: dict[str, int] = defaultdict(int)
        for c in cards:
            suggestions[str(c.get("pool_layer_suggestion") or "unknown")] += 1
        return {
            "cards_total": len(cards),
            "fresh_cards": len(cards) - len(stale),
            "stale_cards": stale[:30],
            "low_coverage_cards": low[:30],
            "pool_layer_suggestions": dict(suggestions),
            "recent_cards": cards[:50],
        }

    def _quality_summary(self) -> dict[str, Any]:
        with self.data_store.session() as con:
            issues = con.execute(
                """
                SELECT issue_id, severity, issue_type, target_id, ticker, summary_cn, created_at
                FROM data_quality_issues
                WHERE status='open'
                ORDER BY
                    CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                    created_at DESC
                LIMIT 50
                """
            ).fetchall()
            gaps = con.execute(
                """
                SELECT gap_id, target_id, ticker, priority, description, created_at
                FROM coverage_gaps
                WHERE status='open'
                ORDER BY
                    CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                    created_at DESC
                LIMIT 50
                """
            ).fetchall()
        issue_rows = [dict(r) for r in issues]
        gap_rows = [dict(r) for r in gaps]
        return {
            "open_issues": issue_rows,
            "open_p0_p1_issues": [x for x in issue_rows if x.get("severity") in {"P0", "P1"}],
            "open_high_priority_gaps": [x for x in gap_rows if x.get("priority") == "high"],
            "open_gaps": gap_rows,
        }

    def _latest_golden_run(self) -> dict[str, Any] | None:
        with self.data_store.session() as con:
            row = con.execute(
                "SELECT run_id, golden_file, expected_count, matched_count, recall, result_json, created_at "
                "FROM golden_eval_runs ORDER BY created_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        result = loads_json(row["result_json"], {}) or {}
        return {
            "run_id": row["run_id"],
            "golden_file": row["golden_file"],
            "expected_count": row["expected_count"],
            "matched_count": row["matched_count"],
            "recall": row["recall"],
            "missed": result.get("missed") or [],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------ health score

    @staticmethod
    def _safe_ratio(num: int | float | None, den: int | float | None) -> float | None:
        if den in (None, 0):
            return None
        return max(0.0, min(1.0, float(num or 0) / float(den)))

    @staticmethod
    def _weighted_score(parts: dict[str, tuple[float | None, float]]) -> float | None:
        total_weight = sum(weight for value, weight in parts.values() if value is not None)
        if total_weight == 0:
            return None
        score = sum((value or 0) * weight for value, weight in parts.values() if value is not None)
        return round(score / total_weight, 4)

    def _research_health_score(
        self,
        *,
        confirmed: dict[str, Any],
        authoritative_covered_cells: int,
        hk: dict[str, Any],
        market: dict[str, Any],
        research_cards: dict[str, Any],
        quality: dict[str, Any],
    ) -> float | None:
        """Trend-watching composite only — acceptance still requires the detail tables."""
        authority_ratio = self._safe_ratio(authoritative_covered_cells, confirmed.get("expected_cells"))
        market_ratio = self._safe_ratio(market.get("contexts_with_snapshot"), market.get("expected_contexts"))
        card_ratio = self._safe_ratio(research_cards.get("fresh_cards"), research_cards.get("cards_total"))
        quality_ratio = 0.0 if quality.get("open_p0_p1_issues") else 1.0
        return self._weighted_score(
            {
                "coverage": (confirmed.get("coverage_ratio"), 0.35),
                "authority": (authority_ratio, 0.20),
                "hk": (hk.get("avg_field_completeness"), 0.15),
                "market": (market_ratio, 0.10),
                "cards": (card_ratio, 0.10),
                "quality": (quality_ratio, 0.10),
            }
        )
