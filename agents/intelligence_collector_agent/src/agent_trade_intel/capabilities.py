from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .adapters.stock_data_adapter import StockDataCLIAdapter
from .db import SQLiteStore, dumps_json
from .ids import new_id, utc_now_iso
from .logging_setup import get_logger

logger = get_logger("capabilities")


@dataclass
class CapabilityCheckResult:
    capability_id: str
    status: str
    capabilities: dict[str, Any]
    errors: list[dict[str, Any]]


class ToolCapabilityVerifier:
    """Runtime capability verifier.

    This is deliberately a real check. It calls stock_data_collector with a small ticker/date
    window configured in YAML and records what actually works in this environment. A frequency is
    only marked usable when the fetch path is not critically bad *and* a subsequent query confirms
    rows exist for that frequency/date. This prevents false positives where the CLI returned success
    but no minute rows were actually stored.
    """

    def __init__(self, store: SQLiteStore, stock: StockDataCLIAdapter, config: dict[str, Any]):
        self.store = store
        self.stock = stock
        self.config = config

    def verify_stock_intraday(self, *, keepalive: Callable[[], None] | None = None) -> CapabilityCheckResult:
        cap_cfg = self.config.get("capability_verification", {}).get("stock_data_collector", {})
        ticker = cap_cfg.get("ticker", "600519.SH")
        start_date = cap_cfg.get("start_date")
        end_date = cap_cfg.get("end_date") or start_date
        frequencies = cap_cfg.get("frequencies", ["5m", "15m"])
        minimum_quality = float(self.config.get("quality", {}).get("minimum_quality_for_public_pool", 0.8))
        capabilities: dict[str, Any] = {"checked_at": utc_now_iso(), "ticker": ticker, "frequencies": {}}
        errors: list[dict[str, Any]] = []
        for freq in frequencies:
            if keepalive:
                keepalive()
            fetch_res = self.stock.fetch_historical_bars(
                tickers=[ticker],
                start_date=start_date,
                end_date=end_date,
                frequency=freq,
                adjust="none",
                cross_validate=False,
            )
            if keepalive:
                keepalive()
            query_res = self.stock.query_bars(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
                frequency=freq,
                adjust="none",
                trading_ready=False,
            )
            fetch_rows = int(fetch_res.quality.get("rows_fetched") or 0)
            inline_bars_count = int(fetch_res.quality.get("inline_bars_count") or 0)
            query_rows = int(query_res.quality.get("rows_fetched") or 0)
            data_quality = fetch_res.quality.get("data_quality")
            try:
                quality_ok = data_quality is None or float(data_quality) >= minimum_quality
            except Exception:
                quality_ok = False
            critical_conflict = any((c or {}).get("severity") == "critical" for c in fetch_res.quality.get("conflicts", []) or [])
            usable = (
                fetch_res.status in {"success", "partial_success"}
                and query_res.status == "success"
                and query_rows > 0
                and quality_ok
                and not critical_conflict
            )
            capabilities["frequencies"][freq] = {
                "status": "success" if usable else "unavailable",
                "usable": usable,
                "fetch_status": fetch_res.status,
                "query_status": query_res.status,
                "fetch_rows_fetched": fetch_rows,
                "inline_bars_count": inline_bars_count,
                "query_rows": query_rows,
                "has_minute_rows": query_rows > 0,
                "data_quality": data_quality,
                "minimum_quality": minimum_quality,
                "errors": fetch_res.errors + query_res.errors,
                "raw_result_ref": fetch_res.raw_result_ref,
            }
            if not usable:
                errors.extend(fetch_res.errors + query_res.errors)
                if query_rows <= 0:
                    errors.append({"error_code": "NO_MINUTE_ROWS", "error_message": f"no {freq} rows returned by query bars", "retryable": False})
        any_usable = any(v.get("usable") for v in capabilities["frequencies"].values())
        status = "available" if any_usable else "unavailable"
        capability_id = new_id("cap")
        logger.info("stock_data capability verification: status=%s frequencies=%s", status, {k: v.get("usable") for k, v in capabilities["frequencies"].items()})
        with self.store.session() as con:
            con.execute(
                """
                INSERT INTO tool_capabilities(capability_id, tool_name, status, capabilities_json, errors_json, notes)
                VALUES (?, 'stock_data_collector', ?, ?, ?, ?)
                """,
                (
                    capability_id,
                    status,
                    dumps_json(capabilities),
                    dumps_json(errors),
                    "intraday frequency verification",
                ),
            )
        return CapabilityCheckResult(capability_id=capability_id, status=status, capabilities=capabilities, errors=errors)

    def latest_stock_capabilities(self) -> dict[str, Any] | None:
        with self.store.session() as con:
            row = con.execute(
                "SELECT * FROM tool_capabilities WHERE tool_name='stock_data_collector' ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {
            "capability_id": row["capability_id"],
            "status": row["status"],
            "checked_at": row["checked_at"],
            "capabilities": json.loads(row["capabilities_json"]),
            "errors": json.loads(row["errors_json"]),
        }
