"""Market-context adapter (V0.9): thin CLI layer over ``stock_data_collector``.

Indices (A-share / HK), FX, commodities and rates are collected by the stock tool's
``stock_data_ingestion.cli fetch market-context`` command. The agent describes the
business need (context type, business symbol, as-of day, category parameters) and maps
the tool's structured answer into the agent result format. It never imports a data
vendor, never assembles vendor arguments, never parses vendor columns or infers dates,
and never falls back to fetching on its own: source selection, retries across sources,
raw retention, standardization, quality checks and storage all live in the tool.

Vendor bindings (former ``akshare_func`` / ``akshare_args`` / ``date_column`` /
``value_column`` target fields) now live in
``tools/stock_data_collector/config/market_context_sources.yaml``. Legacy fields still
present on a target are ignored here and reported in ``quality.legacy_fields_ignored``
so a stale research-pool config is visible instead of silently steering collection.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from .common import ToolResult
from .stock_data_adapter import _parse_json_stdout
from agent_trade_intel.logging_setup import get_logger

logger = get_logger("adapters.market_context")

# Target fields that belong to the tool-side data-source config, not to the agent target.
LEGACY_VENDOR_FIELDS = ("akshare_func", "akshare_args", "date_column", "value_column", "provider", "unit")
# Business parameters forwarded to the tool CLI (target key -> CLI flag).
CATEGORY_PARAMS = {
    "market": "--market",
    "contract": "--contract",
    "instrument_type": "--instrument-type",
    "tenor": "--tenor",
    "rate_type": "--rate-type",
}
CHANGE_KEYS = {"change_1d": "1p", "change_5d": "5p", "change_20d": "20p"}
# Tool error codes that mean "fix configuration / environment", not "try again later".
NON_RETRYABLE_TOOL_CODES = {"INVALID_REQUEST", "INVALID_TICKER", "INVALID_DATE_RANGE", "TOKEN_MISSING", "AUTH_FAILED", "PERMISSION_DENIED", "PROVIDER_UNAVAILABLE"}


class MarketContextAdapter:
    """Invoke ``stock_data_ingestion.cli fetch market-context`` and map the response.

    Same subprocess conventions as :class:`StockDataCLIAdapter` / :class:`HKConnectAdapter`
    (shared tool package, config_dir / working_dir / python_executable semantics).
    """

    tool_name = "market_context_collector"

    def __init__(
        self,
        *,
        config_dir: str | None = None,
        python_executable: str = "python",
        working_dir: str | None = None,
        timeout_seconds: int = 180,
    ):
        self.config_dir = config_dir
        self.python_executable = python_executable
        self.working_dir = working_dir
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    def collect_snapshot(self, *, context: dict[str, Any], as_of: str | None = None) -> ToolResult:
        context_id = str(context.get("context_id") or context.get("target_id") or "")
        result = ToolResult(
            tool_name=self.tool_name,
            operation="market_context_snapshot",
            request={"context_id": context_id, "context_type": context.get("context_type"), "symbol": context.get("symbol"), "as_of": as_of},
        )
        legacy_ignored = sorted(k for k in LEGACY_VENDOR_FIELDS if context.get(k) not in (None, "", {}, []))
        if legacy_ignored:
            logger.info("market_context %s: legacy vendor fields ignored (%s); bindings live in the stock tool config", context_id, ",".join(legacy_ignored))

        try:
            cmd = self._build_command(context, as_of)
        except ValueError as exc:
            return self._fail(result, "MARKET_CONTEXT_INVALID_TARGET", str(exc), retryable=False, legacy=legacy_ignored,
                              suggested_action="Fix the market_contexts entry in the research pool YAML (context_type + symbol are required).")

        try:
            proc = self._run_cli(cmd)
        except subprocess.TimeoutExpired as exc:
            return self._fail(result, "MARKET_CONTEXT_TIMEOUT", str(exc), retryable=True, legacy=legacy_ignored)
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, "MARKET_CONTEXT_CLI_UNAVAILABLE", str(exc), retryable=False, legacy=legacy_ignored,
                              suggested_action="Check tools.python_executable / tools.stock_data_collector.working_dir; the stock_data_ingestion package must be importable.")

        payload = _parse_json_stdout(proc.stdout)
        if proc.returncode != 0 or not isinstance(payload, dict) or "result" not in payload:
            tail = (proc.stderr or proc.stdout or "")[-1000:]
            env_problem = any(token in tail for token in ("ModuleNotFoundError", "No module named", "INVALID_MARKET_CONTEXT_CONFIG", "FileNotFoundError"))
            return self._fail(
                result,
                "MARKET_CONTEXT_CLI_FAILED",
                tail or f"tool exited with rc={proc.returncode} and no JSON output",
                retryable=not env_problem,
                legacy=legacy_ignored,
                suggested_action="Inspect the stock_data_collector environment/config (stderr above)." if env_problem else None,
            )
        return self._map_response(result, context, payload, legacy_ignored, as_of)

    # ------------------------------------------------------------------
    def _build_command(self, context: dict[str, Any], as_of: str | None) -> list[str]:
        context_type = str(context.get("context_type") or "").strip()
        symbol = str(context.get("symbol") or "").strip()
        if not context_type or context_type == "market_context":
            raise ValueError("market_context target needs an explicit context_type (equity_index|hk_index|fx|commodity|interest_rate)")
        if not symbol:
            raise ValueError("market_context target needs a business symbol (e.g. 000300, HSTECH, HKDCNY, CU0, CN_CGB_10Y)")
        cmd = [self.python_executable, "-m", "stock_data_ingestion.cli"]
        if self.config_dir:
            cmd += ["--config-dir", self.config_dir]
        cmd += ["fetch", "market-context", "--context-type", context_type, "--symbol", symbol, "--compact"]
        context_id = context.get("context_id") or context.get("target_id")
        if context_id:
            cmd += ["--context-id", str(context_id)]
        if as_of:
            cmd += ["--as-of", str(as_of)[:10]]
        frequency = context.get("frequency")
        if frequency:
            cmd += ["--frequency", str(frequency)]
        for key, flag in CATEGORY_PARAMS.items():
            if context.get(key) not in (None, ""):
                cmd += [flag, str(context[key])]
        if context.get("max_staleness_days") is not None:
            cmd += ["--max-staleness-days", str(int(context["max_staleness_days"]))]
        metrics = context.get("metrics")
        if isinstance(metrics, str):
            metrics = [metrics]
        if metrics:
            cmd += ["--metrics", *[str(m) for m in metrics]]
        cmd += ["--requested-by", "intelligence_collector_agent"]
        return cmd

    def _map_response(self, result: ToolResult, context: dict[str, Any], payload: dict[str, Any], legacy: list[str], as_of: str | None) -> ToolResult:
        tool_result = payload.get("result") or {}
        quality = dict(tool_result.get("quality") or {})
        changes = tool_result.get("changes") or {}
        tool_errors = list(payload.get("errors") or [])
        tool_warnings = list(payload.get("warnings") or [])
        head_change = next(iter(changes.values()), {}) if changes else {}

        data: dict[str, Any] = {
            "context_id": tool_result.get("context_id") or context.get("context_id"),
            "context_type": tool_result.get("context_type") or context.get("context_type"),
            "name": context.get("name") or tool_result.get("name"),
            "tool_name_cn": tool_result.get("name"),
            "symbol": tool_result.get("symbol") or context.get("symbol"),
            # Request day vs actual data day are different facts; keep both.
            "as_of": (str(as_of)[:10] if as_of else (tool_result.get("request_window") or {}).get("as_of")),
            "data_date": tool_result.get("data_date"),
            "observed_at": tool_result.get("observed_at"),
            "collected_at": tool_result.get("collected_at"),
            "metric": tool_result.get("metric"),
            "value": tool_result.get("value"),
            "unit": tool_result.get("unit"),
            "values": tool_result.get("values") or {},
            "change_kind": head_change.get("kind"),
            "change_period_unit": head_change.get("period_unit"),
            "changes": changes,
            "identity": tool_result.get("identity") or {},
            "request_window": tool_result.get("request_window") or {},
            "source_url": (tool_result.get("source") or {}).get("source_url"),
            "provider": (tool_result.get("source") or {}).get("provider"),
            "source_api": (tool_result.get("source") or {}).get("source_api"),
            "source": tool_result.get("source") or {},
            "quality": quality,
            "provenance": tool_result.get("provenance") or {},
            "tool_request_id": payload.get("request_id"),
            "tool_status": payload.get("status"),
            "tool_warnings": tool_warnings,
            "series_tail": [
                {"data_date": o.get("data_date"), "values": o.get("values")}
                for o in (tool_result.get("series") or [])[-6:]
            ],
        }
        for key, period in CHANGE_KEYS.items():
            change = changes.get(period) or {}
            data[key] = change.get("value")
            if change.get("value") is None and change.get("reason"):
                data[f"{key}_reason"] = change["reason"]

        usable = bool(quality.get("usable"))
        result.quality = {
            "usable": usable,
            "status": quality.get("status") or ("missing" if not usable else "fresh"),
            "is_fresh": quality.get("is_fresh"),
            "staleness_days": quality.get("staleness_days"),
            "max_staleness_days": quality.get("max_staleness_days"),
            "data_date": tool_result.get("data_date"),
            "data_date_matches_as_of": quality.get("data_date_matches_as_of"),
            "provider": data["provider"],
            "source_api": data["source_api"],
            "missing_fields": list(quality.get("missing_fields") or []) + [k for k in CHANGE_KEYS if data.get(k) is None],
            "field_completeness": round(sum(1 for k in ("value", *CHANGE_KEYS) if data.get(k) is not None) / 4, 4),
            "single_source": quality.get("single_source"),
            "cross_validated": quality.get("cross_validated"),
            "conflicts": len(quality.get("conflicts") or []),
            "anomalies": quality.get("anomalies") or [],
            "warnings": list(quality.get("warnings") or []) + tool_warnings,
            "data_quality_score": quality.get("data_quality_score"),
            "tool_status": payload.get("status"),
            "legacy_fields_ignored": legacy,
        }
        if payload.get("status") in {"success", "partial_success"} and usable:
            result.status = "success"
            result.result = data
            if tool_errors:
                result.errors.extend(tool_errors)
            return result.finish()

        result.status = "failed"
        result.result = data
        errors = tool_errors or [
            {
                "error_code": "MARKET_CONTEXT_NO_USABLE_VALUE",
                "error_message": f"tool returned no usable observation (status={quality.get('status')}, as_of={data['as_of']})",
                "retryable": quality.get("status") == "missing",
            }
        ]
        for err in errors:
            if err.get("error_code") in NON_RETRYABLE_TOOL_CODES:
                err["retryable"] = False
                err.setdefault("suggested_action", "Register the symbol in tools/stock_data_collector/config/market_context_sources.yaml or fix the target's context_type/symbol.")
        result.errors.extend(errors)
        hint = next((e.get("suggested_action") for e in result.errors if e.get("suggested_action")), None)
        logger.warning(
            "market_context collect failed for %s: %s%s",
            data["context_id"],
            (result.errors[0].get("error_message") if result.errors else "unknown"),
            f" ({hint})" if hint else "",
        )
        return result.finish()

    def _fail(self, result: ToolResult, code: str, message: str, *, retryable: bool, legacy: list[str], suggested_action: str | None = None) -> ToolResult:
        result.status = "failed"
        error = {"error_code": code, "error_message": message, "retryable": retryable}
        if suggested_action:
            error["suggested_action"] = suggested_action
        result.errors.append(error)
        result.quality = {"usable": False, "status": "failed", "legacy_fields_ignored": legacy}
        logger.warning("market_context %s: %s", code, message[:300])
        return result.finish()

    def _run_cli(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Subprocess boundary, kept separate so tests can fake the tool CLI."""
        logger.info("running market-context CLI: %s", cmd[2:])
        return subprocess.run(
            cmd,
            cwd=self.working_dir,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            env=os.environ.copy(),
        )
