from __future__ import annotations

from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .logging_setup import get_logger

logger = get_logger("pools")

KNOWN_POOL_LAYERS = {"current_holding", "trading_candidate", "watchlist", "base_pool"}


class PoolRepository:
    """First-edition stand-in for the holding system / stock pool services.

    Pool membership is maintained via CLI (or by an upstream system writing the same table) and
    consumed when a Demand uses target_scope.scope_type == dynamic_pool.
    """

    def __init__(self, store: SQLiteStore):
        self.store = store

    def upsert_member(
        self,
        *,
        pool_layer: str,
        ticker: str,
        target_id: str | None = None,
        company_name: str | None = None,
        sellability: str | None = None,
        is_st: bool = False,
        is_suspended: bool = False,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.store.session() as con:
            con.execute(
                """
                INSERT INTO pool_members(
                  pool_layer, ticker, target_id, company_name, sellability,
                  is_st, is_suspended, status, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(pool_layer, ticker) DO UPDATE SET
                  target_id=excluded.target_id,
                  company_name=excluded.company_name,
                  sellability=excluded.sellability,
                  is_st=excluded.is_st,
                  is_suspended=excluded.is_suspended,
                  status=excluded.status,
                  metadata_json=excluded.metadata_json,
                  updated_at=datetime('now')
                """,
                (
                    pool_layer,
                    ticker,
                    target_id or f"ticker_{ticker}",
                    company_name,
                    sellability,
                    1 if is_st else 0,
                    1 if is_suspended else 0,
                    status,
                    dumps_json(metadata or {}),
                ),
            )
        return {"status": "ok", "pool_layer": pool_layer, "ticker": ticker}

    def remove_member(self, *, pool_layer: str, ticker: str) -> bool:
        with self.store.session() as con:
            cur = con.execute(
                "DELETE FROM pool_members WHERE pool_layer=? AND ticker=?",
                (pool_layer, ticker),
            )
        return bool(cur.rowcount)

    def list_members(self, *, pool_layer: str | None = None, status: str = "active") -> list[dict[str, Any]]:
        query = "SELECT * FROM pool_members WHERE status=?"
        params: list[Any] = [status]
        if pool_layer:
            query += " AND pool_layer=?"
            params.append(pool_layer)
        query += " ORDER BY pool_layer, ticker"
        with self.store.session() as con:
            rows = con.execute(query, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["is_st"] = bool(d["is_st"])
            d["is_suspended"] = bool(d["is_suspended"])
            d["metadata"] = loads_json(d.pop("metadata_json"), {})
            out.append(d)
        return out


def _dedupe_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for t in targets:
        key = t.get("target_id") or t.get("ticker") or t.get("company_name") or t.get("industry_name")
        if key:
            out[str(key)] = t
    return list(out.values())


def resolve_demand_targets(
    demand: dict[str, Any],
    pool_repo: PoolRepository | None = None,
    registry: Any = None,
) -> list[dict[str, Any]]:
    """Resolve a Demand into concrete collection targets.

    Precedence: explicit demand.targets > target_scope.include_tickers > dynamic_pool layers
    resolved via PoolRepository with sellability / exclude_st / exclude_suspended filters.

    demand.derived_from_demands is a runtime reference (V0.8): the target list of each source
    demand is re-read at planning time, so periodic review demands automatically follow target
    additions/removals in their daily source demand instead of drifting from a batch-time copy.
    """
    derived: list[dict[str, Any]] = []
    if registry is not None:
        for source_id in demand.get("derived_from_demands") or []:
            source = registry.get(str(source_id))
            if not source:
                logger.warning(
                    "demand %s: derived source demand not found: %s", demand.get("demand_id"), source_id
                )
                continue
            # Sources resolve without registry so derivation chains cannot recurse.
            derived.extend(resolve_demand_targets(source, pool_repo))
    targets = list(demand.get("targets") or [])
    if targets or derived:
        return _dedupe_targets([*targets, *derived])
    scope = demand.get("target_scope") or {}
    pool_layers = scope.get("pool_layers") or []
    for ticker in scope.get("include_tickers") or []:
        targets.append(
            {
                "target_type": "ticker",
                "ticker": ticker,
                "target_id": f"ticker_{ticker}",
                "company_name": None,
                "pool_layer": pool_layers[0] if pool_layers else None,
            }
        )
    if targets:
        return targets
    if scope.get("scope_type") == "dynamic_pool" and pool_repo is not None:
        filters = scope.get("filters") or {}
        for layer in pool_layers:
            for member in pool_repo.list_members(pool_layer=layer):
                if filters.get("sellability") and member.get("sellability") != filters["sellability"]:
                    continue
                if filters.get("exclude_st") and member.get("is_st"):
                    continue
                if filters.get("exclude_suspended") and member.get("is_suspended"):
                    continue
                targets.append(
                    {
                        "target_type": "ticker",
                        "ticker": member["ticker"],
                        "target_id": member.get("target_id") or f"ticker_{member['ticker']}",
                        "company_name": member.get("company_name"),
                        "pool_layer": layer,
                        "sellability": member.get("sellability"),
                        "is_st": member.get("is_st", False),
                        "is_suspended": member.get("is_suspended", False),
                    }
                )
        if not targets:
            logger.warning(
                "dynamic_pool demand %s resolved to zero targets (layers=%s)",
                demand.get("demand_id"),
                pool_layers,
            )
    return targets
