from __future__ import annotations

from datetime import timedelta
from typing import Any

from .demand import demand_targets
from .ids import make_idempotency_key, new_id
from .time_utils import floor_bucket, parse_dt


class TaskGraphPlanner:
    """Convert one COLLECTION_REQUEST_TICKET + Demand into executable task payloads."""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def plan(self, demand: dict[str, Any], *, request_ticket_id: str, as_of: str, market_phase: str) -> list[dict[str, Any]]:
        demand_type = demand.get("demand_type")
        tasks: list[dict[str, Any]] = []
        if demand_type == "intraday_monitoring":
            tasks.extend(self._intraday_tasks(demand, request_ticket_id, as_of, market_phase))
        elif demand_type == "black_swan_scan":
            tasks.extend(self._mic_tasks(demand, request_ticket_id, as_of, task_type="black_swan_scan"))
        elif demand_type == "candidate_full_snapshot":
            tasks.extend(self._candidate_snapshot_tasks(demand, request_ticket_id, as_of))
        elif demand_type == "tool_capability_check":
            tasks.append(self._task(demand, request_ticket_id, None, "tool_capability_check", "internal", as_of))
        elif demand_type in {"daily_collection", "on_demand_research", "coverage_gap_followup"}:
            tasks.extend(self._mic_tasks(demand, request_ticket_id, as_of, task_type="mic_deep_collect"))
            if demand_type == "daily_collection" and market_phase in {"post_market", "off_hours"}:
                tasks.extend(self._stock_daily_tasks(demand, request_ticket_id, as_of))
        else:
            tasks.extend(self._mic_tasks(demand, request_ticket_id, as_of, task_type="mic_deep_collect"))
        return tasks

    def _intraday_tasks(self, demand: dict[str, Any], request_ticket_id: str, as_of: str, market_phase: str) -> list[dict[str, Any]]:
        """Plan intraday tasks with per-task cadence buckets.

        Each task's bucket_start is floored to its own cadence (snapshot vs black swan). Because
        bucket_start is part of the task idempotency key, repeated runtime ticks inside the same
        bucket dedupe to the same Ticket/Message instead of re-running real tools.
        """
        tasks: list[dict[str, Any]] = []
        dt = parse_dt(as_of, self.config.get("runtime", {}).get("timezone", "Asia/Shanghai"))
        cadence = self.config.get("cadence", {}) or {}
        schedule = self.config.get("schedule", {}) or {}
        snapshot_allowed = self._snapshot_phase_allowed(market_phase, schedule, demand)
        black_swan_allowed = self._black_swan_phase_allowed(market_phase, schedule, demand)
        for target in demand_targets(demand):
            snap_minutes = self._snapshot_minutes(target, cadence)
            if snapshot_allowed and self._snapshot_eligible(target):
                snap_start = floor_bucket(dt, snap_minutes)
                tasks.append(
                    self._task(
                        demand,
                        request_ticket_id,
                        target,
                        "intraday_snapshot_10m",
                        "stock_data_collector",
                        as_of,
                        bucket_start=snap_start.isoformat(),
                        bucket_end=(snap_start + timedelta(minutes=snap_minutes)).isoformat(),
                        bucket_size=f"{snap_minutes}m",
                    )
                )
            if black_swan_allowed:
                bs_minutes = self._black_swan_minutes(target, cadence)
                bs_start = floor_bucket(dt, bs_minutes)
                tasks.append(
                    self._task(
                        demand,
                        request_ticket_id,
                        target,
                        "black_swan_scan",
                        "market_intelligence_collector",
                        as_of,
                        bucket_start=bs_start.isoformat(),
                        bucket_end=(bs_start + timedelta(minutes=bs_minutes)).isoformat(),
                        bucket_size=f"{bs_minutes}m",
                    )
                )
        return tasks


    @staticmethod
    def _snapshot_phase_allowed(market_phase: str, schedule: dict[str, Any], demand: dict[str, Any]) -> bool:
        if market_phase == "intraday":
            return True
        if market_phase == "lunch_break":
            return bool(schedule.get("allow_lunch_break_intraday", False))
        if market_phase == "non_trading_day":
            demand_allow = bool((demand.get("schedule_window") or {}).get("allow_non_trading_day", False))
            return demand_allow and bool(schedule.get("allow_non_trading_day_intraday", False))
        return bool(schedule.get("allow_off_hours_intraday", False))

    @staticmethod
    def _black_swan_phase_allowed(market_phase: str, schedule: dict[str, Any], demand: dict[str, Any]) -> bool:
        if market_phase == "intraday":
            return True
        if market_phase == "lunch_break":
            return bool(schedule.get("allow_lunch_break_black_swan", True))
        if market_phase == "non_trading_day":
            demand_allow = bool((demand.get("schedule_window") or {}).get("allow_non_trading_day", False))
            return demand_allow and bool(schedule.get("allow_non_trading_day_black_swan", False))
        return bool(schedule.get("allow_off_hours_black_swan", True))

    @staticmethod
    def _snapshot_eligible(target: dict[str, Any]) -> bool:
        return target.get("sellability") == "sellable" or target.get("pool_layer") in {"current_holding", "trading_candidate"}

    @staticmethod
    def _snapshot_minutes(target: dict[str, Any], cadence: dict[str, Any]) -> int:
        default = int(cadence.get("intraday_bucket_minutes", 10))
        layer = target.get("pool_layer")
        if layer == "current_holding":
            if target.get("sellability") == "sellable":
                return int(cadence.get("held_sellable_intraday_minutes", default))
            return int(cadence.get("held_t1_locked_intraday_minutes", 30))
        if layer == "trading_candidate":
            return int(cadence.get("trading_candidate_intraday_minutes", default))
        if layer == "watchlist":
            return int(cadence.get("watchlist_intraday_minutes", 60))
        return default

    @staticmethod
    def _black_swan_minutes(target: dict[str, Any], cadence: dict[str, Any]) -> int:
        if target.get("pool_layer") == "current_holding" and target.get("sellability") == "sellable":
            return int(cadence.get("black_swan_held_sellable_minutes", 60))
        return int(cadence.get("black_swan_candidate_minutes", 120))

    def _candidate_snapshot_tasks(self, demand: dict[str, Any], request_ticket_id: str, as_of: str) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for target in demand_targets(demand):
            tasks.append(self._task(demand, request_ticket_id, target, "candidate_full_stock_snapshot", "stock_data_collector", as_of))
            tasks.append(self._task(demand, request_ticket_id, target, "mic_deep_collect", "market_intelligence_collector", as_of))
        return tasks

    def _mic_tasks(self, demand: dict[str, Any], request_ticket_id: str, as_of: str, *, task_type: str) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        targets = demand_targets(demand) or [None]
        for target in targets:
            tasks.append(self._task(demand, request_ticket_id, target, task_type, "market_intelligence_collector", as_of))
        return tasks

    def _stock_daily_tasks(self, demand: dict[str, Any], request_ticket_id: str, as_of: str) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for target in demand_targets(demand):
            tasks.append(self._task(demand, request_ticket_id, target, "post_close_stock_refresh", "stock_data_collector", as_of))
        return tasks

    def _task(
        self,
        demand: dict[str, Any],
        request_ticket_id: str,
        target: dict[str, Any] | None,
        task_type: str,
        tool_name: str,
        as_of: str,
        **extra: Any,
    ) -> dict[str, Any]:
        ticker = target.get("ticker") if target else None
        target_id = target.get("target_id") if target else None
        task_id = new_id("task")
        payload = {
            "task_id": task_id,
            "demand_id": demand["demand_id"],
            "request_ticket_id": request_ticket_id,
            "task_type": task_type,
            "tool_name": tool_name,
            "target": target,
            "as_of": as_of,
            "task_profile": demand.get("task_profile", {}),
            "alert_policy": demand.get("alert_policy", {}),
            "output_contract": demand.get("output_contract", {}),
            "test_mode": bool(demand.get("test_mode", False)),
            **extra,
        }
        payload["idempotency_key"] = make_idempotency_key(
            "collection_task",
            demand["demand_id"],
            task_type,
            target_id or ticker or "targetless",
            extra.get("bucket_start") or as_of[:10],
        )
        return payload
