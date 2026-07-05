"""Deterministic company research cards for the iterative research pool (V0.8.1).

The tracking plan (md 使用方式) lists "公司研究卡维护" as a first-class output. The first
version deliberately avoids another LLM call: it rolls up structured facts the agent has
already collected — events, accepted event-variable links, open coverage gaps and the latest
HK-connect snapshot — into one JSON card per target. A downstream Analyst Agent can turn the
card into prose later; the card itself is a reproducible artifact suitable for tests and for
the monthly review loop (variable coverage, positive/negative evidence, pool-layer hint).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .ids import utc_now_iso


@dataclass
class ResearchCardSpec:
    target_id: str
    ticker: str | None = None
    company_name: str | None = None
    industry_id: str | None = None
    theme_ids: list[str] | None = None
    tracking_variables: list[str] | None = None


class ResearchCardBuilder:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def refresh(self, *, target_id: str, as_of: str | None = None, lookback_days: int = 30) -> dict[str, Any]:
        as_of_date = (as_of or utc_now_iso())[:10]
        since = (
            datetime.fromisoformat(as_of_date).replace(tzinfo=timezone.utc) - timedelta(days=lookback_days)
        ).date().isoformat()

        with self.store.session() as con:
            spec = self._load_target_spec(con, target_id)
            events = self._recent_events(con, target_id, since)
            variable_links = self._accepted_variable_links(con, target_id, since)
            gaps = self._open_coverage_gaps(con, target_id)
            hk_snapshot = self._latest_hk_snapshot(con, spec.ticker)

            expected = spec.tracking_variables or []
            covered = sorted({link["tracking_variable"] for link in variable_links})
            missing = [v for v in expected if v not in covered]
            positive = [e for e in events if _direction(e).startswith("positive")][:8]
            negative = [e for e in events if _direction(e).startswith("negative")][:8]

            card = {
                "target_id": target_id,
                "ticker": spec.ticker,
                "company_name": spec.company_name,
                "industry_id": spec.industry_id,
                "theme_ids": spec.theme_ids or [],
                "as_of": as_of_date,
                "lookback_days": lookback_days,
                "tracking_variables": expected,
                "covered_variables": covered,
                "missing_variables": missing,
                "coverage_ratio": round(len(covered) / len(expected), 4) if expected else None,
                "latest_positive_evidence": _compact_events(positive),
                "latest_negative_evidence": _compact_events(negative),
                "latest_events": _compact_events(events[:20]),
                "open_questions": [
                    {"description": row["description"], "priority": row["priority"]} for row in gaps[:20]
                ],
                "hk_connect_snapshot": hk_snapshot,
                "pool_layer_suggestion": self._pool_layer_suggestion(
                    expected=expected,
                    covered=covered,
                    negative_events=negative,
                    gaps=gaps,
                    hk_snapshot=hk_snapshot,
                ),
            }
            self._upsert_card(con, card)
        return card

    def export(self, *, target_id: str | None = None, demand_id: str | None = None) -> list[dict[str, Any]]:
        """Export stored cards, optionally restricted to one target or one demand's targets.

        research_cards has no demand column by design (a target can belong to several
        demands), so demand filtering resolves the demand's current target ids first.
        """
        with self.store.session() as con:
            allowed_ids: set[str] | None = None
            if demand_id:
                row = con.execute(
                    "SELECT payload_json FROM collection_demands WHERE demand_id=?", (demand_id,)
                ).fetchone()
                payload = loads_json(row["payload_json"], {}) if row else {}
                allowed_ids = {
                    str(t.get("target_id")) for t in payload.get("targets") or [] if t.get("target_id")
                }
            params: list[Any] = []
            sql = "SELECT target_id, card_json FROM research_cards WHERE 1=1"
            if target_id:
                sql += " AND target_id=?"
                params.append(target_id)
            rows = con.execute(sql + " ORDER BY updated_at DESC", params).fetchall()
        cards = []
        for row in rows:
            if allowed_ids is not None and str(row["target_id"]) not in allowed_ids:
                continue
            cards.append(loads_json(row["card_json"], {}))
        return cards

    @staticmethod
    def _load_target_spec(con, target_id: str) -> ResearchCardSpec:
        rows = con.execute("SELECT payload_json FROM collection_demands WHERE status='active'").fetchall()
        for row in rows:
            payload = loads_json(row["payload_json"], {})
            for target in payload.get("targets") or []:
                if target.get("target_id") == target_id:
                    return ResearchCardSpec(
                        target_id=target_id,
                        ticker=target.get("ticker"),
                        company_name=target.get("company_name") or target.get("name"),
                        industry_id=target.get("industry_id"),
                        theme_ids=target.get("theme_ids") or [],
                        tracking_variables=target.get("tracking_variables") or [],
                    )
        return ResearchCardSpec(target_id=target_id)

    @staticmethod
    def _recent_events(con, target_id: str, since: str) -> list[dict[str, Any]]:
        rows = con.execute(
            """
            SELECT event_id, event_type, event_date, summary_cn, impact_json,
                   source_type, source_url, confidence, created_at
            FROM structured_events
            WHERE target_id=? AND substr(created_at, 1, 10) >= ?
            ORDER BY COALESCE(confidence, 0) DESC, created_at DESC
            """,
            (target_id, since),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _accepted_variable_links(con, target_id: str, since: str) -> list[dict[str, Any]]:
        rows = con.execute(
            """
            SELECT evl.tracking_variable, evl.direction, evl.mapping_method,
                   evl.mapping_confidence, evl.review_status
            FROM event_variable_links evl
            JOIN structured_events ev ON ev.event_id = evl.event_id
            WHERE evl.target_id=? AND substr(ev.created_at, 1, 10) >= ?
              AND evl.review_status='accepted'
            """,
            (target_id, since),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _open_coverage_gaps(con, target_id: str) -> list[dict[str, Any]]:
        rows = con.execute(
            """
            SELECT description, priority, created_at
            FROM coverage_gaps
            WHERE target_id=? AND status='open'
            ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                     created_at DESC
            LIMIT 50
            """,
            (target_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _latest_hk_snapshot(con, ticker: str | None) -> dict[str, Any] | None:
        if not ticker or not str(ticker).upper().endswith(".HK"):
            return None
        row = con.execute(
            """
            SELECT ticker, as_of, hk_connect_eligible, last_price_hkd, turnover_hkd,
                   southbound_holding_shares, southbound_holding_market_value_hkd,
                   southbound_holding_pct, southbound_mv_change_1d, southbound_mv_change_5d,
                   southbound_mv_change_10d, buyback_amount_hkd, ah_premium_pct,
                   field_completeness_json, missing_fields_json
            FROM hk_connect_snapshots
            WHERE ticker=?
            ORDER BY as_of DESC, created_at DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if not row:
            return None
        out = {k: row[k] for k in row.keys() if not k.endswith("_json")}
        out["field_completeness"] = loads_json(row["field_completeness_json"], {}) or {}
        out["missing_fields"] = loads_json(row["missing_fields_json"], []) or []
        return out

    @staticmethod
    def _pool_layer_suggestion(
        *,
        expected: list[str],
        covered: list[str],
        negative_events: list[dict[str, Any]],
        gaps: list[dict[str, Any]],
        hk_snapshot: dict[str, Any] | None,
    ) -> str:
        """Heuristic maintenance hint for the monthly review; a human still decides."""
        if hk_snapshot and not hk_snapshot.get("hk_connect_eligible"):
            return "hk_not_connect_eligible_verify_before_inclusion"
        if expected and len(covered) / len(expected) < 0.2:
            return "keep_watch_or_reduce_budget_until_coverage_improves"
        if len(negative_events) >= 3:
            return "review_for_downgrade_or_risk_watch"
        if sum(1 for g in gaps if g.get("priority") == "high") >= 3:
            return "needs_followup_before_upgrade"
        return "keep_current_layer"

    @staticmethod
    def _upsert_card(con, card: dict[str, Any]) -> None:
        con.execute(
            """
            INSERT INTO research_cards(
                target_id, ticker, company_name, industry_id, theme_ids_json,
                as_of, card_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(target_id) DO UPDATE SET
                ticker=excluded.ticker,
                company_name=excluded.company_name,
                industry_id=excluded.industry_id,
                theme_ids_json=excluded.theme_ids_json,
                as_of=excluded.as_of,
                card_json=excluded.card_json,
                updated_at=datetime('now')
            """,
            (
                card["target_id"],
                card.get("ticker"),
                card.get("company_name"),
                card.get("industry_id"),
                dumps_json(card.get("theme_ids") or []),
                card.get("as_of"),
                dumps_json(card),
            ),
        )


def _direction(event: dict[str, Any]) -> str:
    impact = loads_json(event.get("impact_json"), {}) or {}
    return str(impact.get("direction") or "unclear")


def _compact_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "event_date": event.get("event_date"),
            "summary_cn": event.get("summary_cn"),
            "source_type": event.get("source_type"),
            "source_url": event.get("source_url"),
            "confidence": event.get("confidence"),
        }
        for event in events
    ]
