"""HK Connect structured data adapter (V0.8).

Thin CLI adapter over ``stock_data_collector``'s ``fetch hk-connect`` command.
Collection itself (Eastmoney access, EASTMONEY_COOKIE anti-scraping handling,
DNS/WAF hang deadlines, akshare fallbacks) lives in the tool — agents
orchestrate and verify, tools collect. The tool loads the cookie from its own
.env, so the agent runtime needs no Eastmoney credential configuration.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from .common import ToolResult
from .stock_data_adapter import _parse_json_stdout
from agent_trade_intel.logging_setup import get_logger

logger = get_logger("adapters.hk_connect")

# Mirrors the tool-side contract (stock_data_ingestion.services.hk_connect_service):
# the snapshot fields a high-quality snapshot fills. Kept here so agent-side
# evaluation and tests can key completeness off the same list without importing
# the tool package across the CLI boundary.
HK_REQUIRED_FIELDS = (
    "last_price_hkd",
    "turnover_hkd",
    "southbound_holding_shares",
    "southbound_holding_market_value_hkd",
    "southbound_holding_pct",
    "southbound_mv_change_1d",
    "southbound_mv_change_5d",
    "southbound_mv_change_10d",
)


def calc_ah_premium_pct(*, a_price_cny: float | None, h_price_hkd: float | None, cny_hkd: float | None) -> float | None:
    """A-share premium over H-share: A price converted to HKD vs H price, in percent."""
    if not a_price_cny or not h_price_hkd or not cny_hkd:
        return None
    return round((a_price_cny * cny_hkd / h_price_hkd - 1.0) * 100, 4)


class HKConnectAdapter:
    """Invoke ``stock_data_ingestion.cli fetch hk-connect`` and map the response.

    Mirrors :class:`StockDataCLIAdapter`'s subprocess conventions (same tool
    package, same config_dir / working_dir / python_executable semantics).
    """

    tool_name = "hk_connect_collector"

    def __init__(
        self,
        *,
        config_dir: str | None = None,
        python_executable: str = "python",
        working_dir: str | None = None,
        timeout_seconds: int = 300,
    ):
        self.config_dir = config_dir
        self.python_executable = python_executable
        self.working_dir = working_dir
        self.timeout_seconds = timeout_seconds

    def collect_snapshot(
        self,
        *,
        target_id: str | None,
        ticker: str,
        company_name: str | None = None,
        as_of: str | None = None,
    ) -> ToolResult:
        result = ToolResult(
            tool_name=self.tool_name,
            operation="hk_connect_daily_snapshot",
            request={"target_id": target_id, "ticker": ticker, "company_name": company_name, "as_of": as_of},
        )
        cmd = [self.python_executable, "-m", "stock_data_ingestion.cli"]
        if self.config_dir:
            cmd += ["--config-dir", self.config_dir]
        cmd += ["fetch", "hk-connect", "--tickers", ticker]
        if as_of:
            cmd += ["--as-of", str(as_of)[:10]]
        try:
            proc = self._run_cli(cmd)
        except subprocess.TimeoutExpired as exc:
            result.status = "failed"
            result.errors.append({"error_code": "HK_CONNECT_TIMEOUT", "error_message": str(exc), "retryable": True})
            result.quality = {"usable": False}
            return result.finish()
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.errors.append(
                {"error_code": "HK_CONNECT_CLI_UNAVAILABLE", "error_message": str(exc), "retryable": False}
            )
            result.quality = {"usable": False}
            return result.finish()

        payload = _parse_json_stdout(proc.stdout)
        if proc.returncode != 0 or not isinstance(payload, dict):
            result.status = "failed"
            result.errors.append(
                {
                    "error_code": "HK_CONNECT_CLI_FAILED",
                    "error_message": (proc.stderr or proc.stdout or "")[-1000:],
                    "retryable": True,
                }
            )
            if isinstance(payload, dict):
                result.errors.extend(payload.get("errors") or [])
            result.quality = {"usable": False}
            logger.warning("hk_connect CLI failed for %s: rc=%s", ticker, proc.returncode)
            return result.finish()

        snapshots = ((payload.get("data") or {}).get("hk_connect_snapshots")) or []
        snapshot = snapshots[0] if snapshots else {}
        quality = dict(snapshot.get("quality") or {})
        errors = list(snapshot.get("errors") or []) or list(payload.get("errors") or [])
        data = {k: v for k, v in snapshot.items() if k not in {"quality", "errors"}}
        if company_name and not data.get("company_name"):
            data["company_name"] = company_name

        if payload.get("status") in {"success", "partial_success"} and quality.get("usable"):
            result.status = "success"
            result.result = data
            result.quality = quality
        else:
            result.status = "failed"
            result.errors.extend(
                errors
                or [
                    {
                        "error_code": "HK_CONNECT_COLLECT_FAILED",
                        "error_message": "tool returned no usable snapshot",
                        "retryable": True,
                    }
                ]
            )
            result.quality = quality or {"usable": False}
            result.quality.setdefault("usable", False)
            hint = next((e.get("suggested_action") for e in result.errors if e.get("suggested_action")), None)
            logger.warning(
                "hk_connect collect failed for %s: %s%s",
                ticker,
                (result.errors[0].get("error_message") if result.errors else "unknown"),
                f" ({hint})" if hint else "",
            )
        return result.finish()

    def _run_cli(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """Subprocess boundary, kept separate so tests can fake the tool CLI."""
        logger.info("running hk-connect CLI: %s", cmd[2:])
        return subprocess.run(
            cmd,
            cwd=self.working_dir,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            env=os.environ.copy(),
        )
