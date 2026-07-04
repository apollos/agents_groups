from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .errors import ValidationError
from .ids import make_idempotency_key, new_id, stable_hash
from .logging_setup import get_logger
from .queue import SQLiteMessageQueue
from .tickets import TicketRepository, priority_to_int

logger = get_logger("demand")

REQUIRED_DEMAND_FIELDS = ["schema_version", "demand_id", "demand_type", "source_type", "status"]

# operator action -> (allowed current statuses, new status)
LIFECYCLE_TRANSITIONS = {
    "suspend": ({"active"}, "suspended"),
    "resume": ({"suspended"}, "active"),
    "cancel": ({"draft", "active", "suspended"}, "cancelled"),
}


class DemandRegistry:
    def __init__(self, store: SQLiteStore, queue: SQLiteMessageQueue | None = None):
        self.store = store
        self.queue = queue

    def validate(self, demand: dict[str, Any]) -> None:
        missing = [f for f in REQUIRED_DEMAND_FIELDS if not demand.get(f)]
        if missing:
            raise ValidationError(f"demand missing required fields: {missing}")
        if demand.get("schema_version") != "demand.v1":
            raise ValidationError("schema_version must be demand.v1")
        if demand.get("status") not in {"draft", "active", "suspended", "completed", "expired", "cancelled"}:
            raise ValidationError("invalid demand status")
        if "target_scope" not in demand and not demand.get("targets"):
            raise ValidationError("demand must include target_scope or targets")
        if demand.get("test_mode") and demand.get("alert_policy", {}).get("notify_owner"):
            raise ValidationError("test_mode demand cannot notify real owner")

    def register(self, demand: dict[str, Any], *, activate: bool = False) -> dict[str, Any]:
        self.validate(demand)
        payload = dict(demand)
        if activate:
            payload["status"] = "active"
        demand_id = str(payload["demand_id"])
        idem = payload.get("idempotency_key") or make_idempotency_key(
            "demand", payload.get("source_type"), payload.get("demand_type"), stable_hash(payload.get("target_scope", {}))
        )
        payload["idempotency_key"] = idem
        with self.store.session() as con:
            existing = con.execute(
                "SELECT demand_id, current_version FROM collection_demands WHERE idempotency_key=? OR demand_id=?",
                (idem, demand_id),
            ).fetchone()
            if existing:
                version = int(existing["current_version"]) + 1
                demand_id = str(existing["demand_id"])
                # Keep the persisted payload consistent with the primary key when an existing
                # idempotency_key is registered with a different demand_id.
                payload["demand_id"] = demand_id
                con.execute(
                    """
                    UPDATE collection_demands
                    SET current_version=?, demand_type=?, source_type=?, status=?, priority=?, owner=?,
                        active_from=?, active_to=?, target_scope_json=?, payload_json=?, test_mode=?,
                        updated_at=datetime('now')
                    WHERE demand_id=?
                    """,
                    (
                        version,
                        payload["demand_type"],
                        payload["source_type"],
                        payload.get("status", "draft"),
                        payload.get("priority", "normal"),
                        payload.get("owner"),
                        payload.get("active_from"),
                        payload.get("active_to"),
                        dumps_json(payload.get("target_scope", {})),
                        dumps_json(payload),
                        1 if payload.get("test_mode") else 0,
                        demand_id,
                    ),
                )
            else:
                version = 1
                con.execute(
                    """
                    INSERT INTO collection_demands(
                      demand_id, schema_version, current_version, demand_type, source_type, status,
                      priority, owner, active_from, active_to, target_scope_json, payload_json,
                      idempotency_key, test_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        demand_id,
                        payload.get("schema_version", "demand.v1"),
                        version,
                        payload["demand_type"],
                        payload["source_type"],
                        payload.get("status", "draft"),
                        payload.get("priority", "normal"),
                        payload.get("owner"),
                        payload.get("active_from"),
                        payload.get("active_to"),
                        dumps_json(payload.get("target_scope", {})),
                        dumps_json(payload),
                        idem,
                        1 if payload.get("test_mode") else 0,
                    ),
                )
            con.execute(
                """
                INSERT OR REPLACE INTO collection_demand_versions(demand_id, version, payload_json, created_by)
                VALUES (?, ?, ?, ?)
                """,
                (demand_id, version, dumps_json(payload), payload.get("created_by")),
            )
        message_id = None
        if self.queue is not None:
            # demand.registered drives the Runtime Controller (design §6.2). Version is part of the
            # idempotency key so re-registering (a new version) produces a new notification.
            message_id = self.queue.publish(
                "demand.registered",
                {"demand_id": demand_id, "version": version, "status": payload.get("status")},
                priority=10,
                idempotency_key=make_idempotency_key("message", "demand_registered", demand_id, version),
            )
        return {
            "status": "registered",
            "demand_id": demand_id,
            "version": version,
            "validation_status": "passed",
            "message_id": message_id,
        }

    def get(self, demand_id: str) -> dict[str, Any] | None:
        with self.store.session() as con:
            row = con.execute("SELECT * FROM collection_demands WHERE demand_id=?", (demand_id,)).fetchone()
        if not row:
            return None
        payload = loads_json(row["payload_json"], {})
        payload["current_version"] = int(row["current_version"])
        payload["_registry"] = {
            "current_version": int(row["current_version"]),
            "status": row["status"],
            "updated_at": row["updated_at"],
            "created_at": row["created_at"],
        }
        return payload

    def apply_lifecycle(self, demand_id: str, action: str) -> dict[str, Any]:
        """Operator lifecycle action: suspend / resume / cancel."""
        if action not in LIFECYCLE_TRANSITIONS:
            raise ValidationError(f"unknown lifecycle action: {action}")
        allowed_from, new_status = LIFECYCLE_TRANSITIONS[action]
        with self.store.session() as con:
            row = con.execute("SELECT * FROM collection_demands WHERE demand_id=?", (demand_id,)).fetchone()
            if not row:
                raise ValidationError(f"demand not found: {demand_id}")
            current = str(row["status"])
            if current not in allowed_from:
                raise ValidationError(f"cannot {action} demand in status '{current}'")
            payload = loads_json(row["payload_json"], {})
            payload["status"] = new_status
            version = int(row["current_version"]) + 1
            con.execute(
                """
                UPDATE collection_demands
                SET status=?, current_version=?, payload_json=?, updated_at=datetime('now')
                WHERE demand_id=?
                """,
                (new_status, version, dumps_json(payload), demand_id),
            )
            con.execute(
                "INSERT OR REPLACE INTO collection_demand_versions(demand_id, version, payload_json, created_by) VALUES (?, ?, ?, ?)",
                (demand_id, version, dumps_json(payload), f"cli:{action}"),
            )
        logger.info("demand %s %s: %s -> %s (v%s)", demand_id, action, current, new_status, version)
        message_id = None
        if self.queue is not None:
            message_id = self.queue.publish(
                "demand.changed",
                {"demand_id": demand_id, "version": version, "action": action, "old_status": current, "new_status": new_status},
                priority=10,
                idempotency_key=make_idempotency_key("message", "demand_changed", demand_id, version),
            )
        return {
            "status": "ok",
            "demand_id": demand_id,
            "old_status": current,
            "new_status": new_status,
            "version": version,
            "message_id": message_id,
        }

    def active(self, *, as_of: str | None = None) -> list[dict[str, Any]]:
        # as_of is ISO date/datetime. We compare date strings conservatively.
        date_part = (as_of or datetime.now(timezone.utc).date().isoformat())[:10]
        with self.store.session() as con:
            rows = con.execute(
                """
                SELECT * FROM collection_demands
                WHERE status='active'
                  AND (active_from IS NULL OR active_from <= ?)
                  AND (active_to IS NULL OR active_to >= ?)
                ORDER BY priority DESC, created_at ASC
                """,
                (date_part, date_part),
            ).fetchall()
        items = []
        for r in rows:
            payload = loads_json(r["payload_json"], {})
            payload["current_version"] = int(r["current_version"])
            payload["_registry"] = {"current_version": int(r["current_version"]), "status": r["status"], "updated_at": r["updated_at"], "created_at": r["created_at"]}
            items.append(payload)
        return items

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.store.session() as con:
            if status:
                rows = con.execute("SELECT * FROM collection_demands WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM collection_demands ORDER BY created_at DESC").fetchall()
        items = []
        for r in rows:
            payload = loads_json(r["payload_json"], {})
            payload["current_version"] = int(r["current_version"])
            payload["_registry"] = {"current_version": int(r["current_version"]), "status": r["status"], "updated_at": r["updated_at"], "created_at": r["created_at"]}
            items.append(payload)
        return items


class DemandCompiler:
    """Compile active Demand objects into business Tickets and queue Messages."""

    def __init__(self, store: SQLiteStore, queue: SQLiteMessageQueue, tickets: TicketRepository, agent_id: str, agent_group: str):
        self.store = store
        self.queue = queue
        self.tickets = tickets
        self.agent_id = agent_id
        self.agent_group = agent_group

    def compile_demand(self, demand: dict[str, Any], *, as_of: str, market_phase: str = "unknown") -> list[str]:
        demand_id = demand["demand_id"]
        schedule_window = demand.get("schedule_window") or {}
        if market_phase == "non_trading_day" and not bool(schedule_window.get("allow_non_trading_day", False)):
            logger.info("skip demand %s at non-trading day phase", demand_id)
            return []
        if demand.get("demand_type") == "intraday_monitoring" and market_phase in {"lunch_break", "pre_market", "post_market", "off_hours"}:
            # The request may still be compiled for black-swan scanning by planner when desired;
            # snapshot generation is guarded in TaskGraphPlanner. We keep the request so MIC
            # black-swan tasks can run if YAML allows the phase.
            pass
        correlation_id = f"corr_collection_{as_of[:10].replace('-', '')}_{stable_hash(demand_id + as_of, 8)}"
        ticket_payload = {
            "demand_id": demand_id,
            "demand_version": demand.get("current_version"),
            "as_of": as_of,
            "market_phase": market_phase,
            "demand_ref": f"db://collection_demands/{demand_id}",
        }
        ticket_id = self.tickets.create_ticket(
            ticket_type="COLLECTION_REQUEST_TICKET",
            source_agent="runtime_controller",
            target_agent_group=self.agent_group,
            target_agent_id=self.agent_id,
            priority=demand.get("priority", "normal"),
            correlation_id=correlation_id,
            related_tickers=_demand_tickers(demand),
            summary_cn=f"编译 Demand {demand_id} 为情报采集请求。",
            payload=ticket_payload,
            payload_ref=f"db://collection_demands/{demand_id}",
            idempotency_key=make_idempotency_key("collection_request", demand_id, as_of[:16], market_phase),
        )
        msg_id = self.queue.publish(
            "intelligence.collection",
            {"ticket_id": ticket_id, "ticket_type": "COLLECTION_REQUEST_TICKET"},
            priority=priority_to_int(demand.get("priority", "normal")),
            correlation_id=correlation_id,
            idempotency_key=make_idempotency_key("message", "collection_request", ticket_id),
            target_agent_id=self.agent_id,
            target_agent_group=self.agent_group,
        )
        return [ticket_id, msg_id]

    def compile_active_demands(self, registry: DemandRegistry, *, as_of: str, market_phase: str = "unknown") -> list[str]:
        out: list[str] = []
        for demand in registry.active(as_of=as_of):
            out.extend(self.compile_demand(demand, as_of=as_of, market_phase=market_phase))
        return out


def _demand_tickers(demand: dict[str, Any]) -> list[str]:
    tickers = []
    for t in demand.get("targets", []) or []:
        if t.get("ticker"):
            tickers.append(str(t["ticker"]))
    scope = demand.get("target_scope", {}) or {}
    for t in scope.get("include_tickers", []) or []:
        if t not in tickers:
            tickers.append(str(t))
    return tickers


def demand_targets(demand: dict[str, Any]) -> list[dict[str, Any]]:
    targets = list(demand.get("targets", []) or [])
    if not targets:
        scope = demand.get("target_scope", {}) or {}
        for ticker in scope.get("include_tickers", []) or []:
            targets.append(
                {
                    "target_type": "ticker",
                    "ticker": ticker,
                    "target_id": f"ticker_{ticker}",
                    "company_name": None,
                    "pool_layer": (scope.get("pool_layers") or [None])[0],
                }
            )
    return targets
