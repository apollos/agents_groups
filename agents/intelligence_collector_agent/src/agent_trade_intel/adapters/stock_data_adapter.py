from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .common import ToolResult
from agent_trade_intel.logging_setup import get_logger

logger = get_logger("adapters.stock_data")


class StockDataCLIAdapter:
    """CLI adapter for stock_data_collector / stock_data_ingestion.

    The adapter calls the real CLI. It does not mock stock_data_collector.
    """

    tool_name = "stock_data_collector"

    def __init__(self, *, config_dir: str | None, python_executable: str = "python", working_dir: str | None = None, timeout_seconds: int = 180):
        self.config_dir = config_dir
        self.python_executable = python_executable
        self.working_dir = working_dir
        self.timeout_seconds = timeout_seconds

    def cli_available(self) -> ToolResult:
        """Cheap probe: can the stock_data CLI module be invoked at all?"""
        result = ToolResult(tool_name=self.tool_name, operation="cli_available", request={"cmd": "--help"})
        cmd = [self.python_executable, "-m", "stock_data_ingestion.cli", "--help"]
        try:
            proc = subprocess.run(cmd, cwd=self.working_dir, text=True, capture_output=True, timeout=min(self.timeout_seconds, 60), env=os.environ.copy())
            result.result = {"returncode": proc.returncode}
            if proc.returncode == 0:
                result.status = "success"
                result.quality = {"usable": True}
            else:
                result.status = "failed"
                result.quality = {"usable": False}
                result.errors.append({"error_code": "STOCK_DATA_CLI_FAILED", "error_message": proc.stderr[-1000:], "retryable": False})
        except Exception as exc:
            result.status = "failed"
            result.quality = {"usable": False}
            result.errors.append({"error_code": "STOCK_DATA_CLI_UNAVAILABLE", "error_message": str(exc), "retryable": False})
        return result.finish()

    def verify_eastmoney_cookie(self) -> ToolResult:
        return self._run("verify_eastmoney_cookie", ["verify", "eastmoney-cookie"])

    def fetch_trading_status(self, *, tickers: list[str], start_date: str | None = None, end_date: str | None = None) -> ToolResult:
        args = ["fetch", "trading-status", "--tickers", *tickers]
        args += _date_args(start_date, end_date)
        return self._run("fetch_trading_status", args)

    def fetch_historical_bars(
        self,
        *,
        tickers: list[str],
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "1d",
        adjust: str = "none",
        cross_validate: bool = False,
    ) -> ToolResult:
        args = ["fetch", "historical-bars", "--tickers", *tickers, "--frequency", frequency, "--adjust", adjust]
        args += _date_args(start_date, end_date)
        if cross_validate:
            args.append("--cross-validate")
        return self._run("fetch_historical_bars", args)

    def fetch_valuation(self, *, tickers: list[str], start_date: str | None = None, end_date: str | None = None) -> ToolResult:
        args = ["fetch", "valuation", "--tickers", *tickers] + _date_args(start_date, end_date)
        return self._run("fetch_valuation", args)

    def fetch_money_flow(self, *, tickers: list[str], start_date: str | None = None, end_date: str | None = None) -> ToolResult:
        args = ["fetch", "money-flow", "--tickers", *tickers] + _date_args(start_date, end_date)
        return self._run("fetch_money_flow", args)

    def fetch_adj_factor(self, *, tickers: list[str], start_date: str | None = None, end_date: str | None = None) -> ToolResult:
        args = ["fetch", "adj-factor", "--tickers", *tickers] + _date_args(start_date, end_date)
        return self._run("fetch_adj_factor", args)

    def fetch_financial_indicator(self, *, tickers: list[str], start_date: str | None = None, end_date: str | None = None) -> ToolResult:
        args = ["fetch", "financial-indicator", "--tickers", *tickers] + _date_args(start_date, end_date)
        return self._run("fetch_financial_indicator", args)

    def fetch_financial_statement(self, *, tickers: list[str], start_date: str | None = None, end_date: str | None = None) -> ToolResult:
        args = ["fetch", "financial-statement", "--tickers", *tickers] + _date_args(start_date, end_date)
        return self._run("fetch_financial_statement", args)

    def fetch_corporate_action(self, *, tickers: list[str], start_date: str | None = None, end_date: str | None = None) -> ToolResult:
        args = ["fetch", "corporate-action", "--tickers", *tickers] + _date_args(start_date, end_date)
        return self._run("fetch_corporate_action", args)


    def query_bars(
        self,
        *,
        ticker: str,
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "1d",
        adjust: str = "none",
        trading_ready: bool = False,
        minimum_quality: float | None = None,
    ) -> ToolResult:
        args = ["query", "bars", "--ticker", ticker, "--frequency", frequency, "--adjust", adjust]
        args += _date_args(start_date, end_date)
        if trading_ready:
            args.append("--trading-ready")
        if minimum_quality is not None:
            args += ["--minimum-quality", str(minimum_quality)]
        return self._run("query_bars", args)

    def query_meta_summary(self, *, ticker: str) -> ToolResult:
        return self._run("query_meta_summary", ["query", "meta-summary", "--ticker", ticker])

    def _run(self, operation: str, args: list[str]) -> ToolResult:
        cmd = [self.python_executable, "-m", "stock_data_ingestion.cli"]
        if self.config_dir:
            cmd += ["--config-dir", self.config_dir]
        cmd += args
        result = ToolResult(
            tool_name=self.tool_name,
            operation=operation,
            request={"cmd": cmd, "cwd": self.working_dir},
        )
        logger.info("running stock_data CLI: operation=%s args=%s", operation, args)
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.working_dir,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=os.environ.copy(),
            )
            parsed_stdout = _parse_json_stdout(proc.stdout)
            result.result = {
                "returncode": proc.returncode,
                "stdout": parsed_stdout,
                "stderr": proc.stderr[-4000:] if proc.stderr else "",
            }
            if proc.returncode == 0:
                # verify eastmoney-cookie returns true/false instead of StockDataResponse JSON.
                if isinstance(parsed_stdout, bool):
                    result.status = "success" if parsed_stdout else "failed"
                    if not parsed_stdout:
                        result.errors.append(
                            {"error_code": "EASTMONEY_COOKIE_INVALID", "error_message": "verify eastmoney-cookie returned false", "retryable": False}
                        )
                    result.quality = _quality_from_stock_response(parsed_stdout)
                elif operation.startswith("query_") and isinstance(parsed_stdout, list):
                    result.status = "success"
                    result.quality = {"usable": len(parsed_stdout) > 0, "data_quality": 1.0 if parsed_stdout else 0.0, "rows_fetched": len(parsed_stdout), "conflicts": []}
                else:
                    status = (parsed_stdout or {}).get("status") if isinstance(parsed_stdout, dict) else "success"
                    result.status = "success" if status in {"success", "partial_success"} else "failed"
                    result.quality = _quality_from_stock_response(parsed_stdout)
                result.raw_result_ref = _raw_ref(parsed_stdout)
            else:
                result.status = "failed"
                result.errors.append(
                    {"error_code": "STOCK_DATA_CLI_FAILED", "error_message": proc.stderr[-1000:] or proc.stdout[-1000:], "retryable": True}
                )
                if isinstance(parsed_stdout, dict):
                    result.errors.extend(parsed_stdout.get("errors", []) or [])
                    result.quality = _quality_from_stock_response(parsed_stdout)
                else:
                    result.quality = {"usable": False}
        except subprocess.TimeoutExpired as exc:
            result.status = "failed"
            result.errors.append({"error_code": "STOCK_DATA_TIMEOUT", "error_message": str(exc), "retryable": True})
            result.quality = {"usable": False}
        except Exception as exc:
            result.status = "failed"
            result.errors.append({"error_code": "STOCK_DATA_ADAPTER_ERROR", "error_message": str(exc), "retryable": True})
            result.quality = {"usable": False}
        if result.status == "failed":
            logger.warning("stock_data CLI failed: operation=%s errors=%s", operation, result.errors)
        return result.finish()


def _date_args(start_date: str | None, end_date: str | None) -> list[str]:
    out: list[str] = []
    if start_date:
        out += ["--start-date", start_date[:10]]
    if end_date:
        out += ["--end-date", end_date[:10]]
    return out


def _parse_json_stdout(stdout: str) -> Any:
    text = (stdout or "").strip()
    if text == "true":
        return True
    if text == "false":
        return False
    if not text:
        return None
    # Some CLIs print logs before JSON. Try last JSON object/array.
    for candidate in [text, text[text.find("{") :], text[text.find("[") :]]:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return {"text": text[-4000:]}


def _quality_from_stock_response(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"usable": value, "data_quality": 1.0 if value else 0.0, "conflicts": []}
    if not isinstance(value, dict):
        return {"usable": False, "data_quality": 0.0, "conflicts": []}
    status = value.get("status")
    persistence = value.get("persistence", {}) or {}
    quality = value.get("quality_report", {}) or {}
    conflicts = quality.get("conflicts", []) or []
    high_or_critical = [c for c in conflicts if c.get("severity") in {"high", "critical"}]
    provider_results = value.get("provider_results", []) or []
    rows_fetched = sum(int((r or {}).get("rows_fetched") or 0) for r in provider_results if isinstance(r, dict))
    data = value.get("data", {}) or {}
    inline_bars_count = len(data.get("bars") or []) if isinstance(data.get("bars"), list) else 0
    return {
        "usable": status in {"success", "partial_success"} and bool(persistence.get("saved", True)) and not any(c.get("severity") == "critical" for c in conflicts),
        "status": status,
        "persistence_saved": persistence.get("saved"),
        "data_quality": quality.get("data_quality_score"),
        "conflicts": conflicts,
        "high_or_critical_conflicts": high_or_critical,
        "errors": value.get("errors", []) or [],
        "provider_results": provider_results,
        "rows_fetched": rows_fetched,
        "inline_bars_count": inline_bars_count,
    }


def _raw_ref(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    refs = (value.get("persistence", {}) or {}).get("raw_payload_refs") or []
    return refs[0] if refs else None
