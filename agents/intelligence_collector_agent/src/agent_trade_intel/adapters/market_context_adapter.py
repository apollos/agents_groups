"""Structured market-context adapter (V0.8.1): indices / FX / commodities / rates.

The tracking plan (md §5.4 / §7.1) needs pre-market context that text search cannot answer
reliably: commodity prices vs inventory, CNY/HKD FX, benchmark indices, long-bond yields.
This collector intentionally uses a configurable provider/function mapping instead of
hard-coding one AKShare endpoint per asset: AKShare endpoint names and column labels change
over time, so each context declares ``akshare_func`` + columns in YAML and can be tuned
without code changes.

Example context entry (see examples/research_pool_full.yaml ``market_contexts:``)::

    {
      "context_id": "commodity_copper",
      "context_type": "commodity",
      "name": "铜",
      "akshare_func": "futures_zh_spot",
      "akshare_args": {"symbol": "铜"},
      "date_column": "日期",
      "value_column": "最新价",
      "unit": "CNY/ton"
    }

``akshare`` is imported lazily, mirroring hk_connect_adapter: when it is missing only
market-context tasks fail (non-retryable), the rest of the agent keeps working.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .common import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_VALUE_COLUMNS = ("收盘", "收盘价", "最新价", "现价", "close", "Close", "value")
DEFAULT_DATE_COLUMNS = ("日期", "date", "Date", "交易日期", "时间")


@dataclass
class MarketContextSnapshot:
    context_id: str
    context_type: str
    name: str | None
    symbol: str | None
    as_of: str
    value: float | None
    unit: str | None = None
    change_1d: float | None = None
    change_5d: float | None = None
    change_20d: float | None = None
    source_url: str | None = None
    provider: str = "akshare"
    payload: dict[str, Any] | None = None


class MarketContextAdapter:
    """Collect one latest-value snapshot (+ recent change percentages) per context.

    Each context provides either ``akshare_func`` (+ optional ``akshare_args``) or a
    ``context_type`` known to ``_call_spec``. Time-series endpoints yield 1/5/20-period
    changes; one-row realtime tables leave the changes null (visible in payload_json).
    """

    tool_name = "market_context_collector"

    def __init__(self, provider: str = "akshare"):
        self.provider = provider

    def collect_snapshot(self, *, context: dict[str, Any], as_of: str | None = None) -> ToolResult:
        result = ToolResult(
            tool_name=self.tool_name,
            operation="market_context_snapshot",
            request={"context": context, "as_of": as_of},
        )
        try:
            import akshare as ak  # noqa: F401
        except ImportError:
            result.status = "failed"
            result.errors.append(
                {
                    "error_code": "AKSHARE_NOT_INSTALLED",
                    "error_message": "market_context_collector requires the akshare package (pip install akshare)",
                    "retryable": False,
                }
            )
            result.quality = {"usable": False}
            return result.finish()
        try:
            snapshot = self._collect_akshare(ak=ak, context=context, as_of=as_of)
            missing = [
                field
                for field in ("value", "change_1d", "change_5d", "change_20d")
                if getattr(snapshot, field) is None
            ]
            result.status = "success"
            result.result = asdict(snapshot)
            result.quality = {
                "usable": snapshot.value is not None,
                "provider": self.provider,
                "missing_fields": missing,
                "field_completeness": round((4 - len(missing)) / 4, 4),
            }
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.errors.append(
                {"error_code": "MARKET_CONTEXT_COLLECT_FAILED", "error_message": str(exc), "retryable": True}
            )
            result.quality = {"usable": False}
            logger.warning("market context collect failed for %s: %s", context.get("context_id"), exc)
        return result.finish()

    def _collect_akshare(self, *, ak: Any, context: dict[str, Any], as_of: str | None) -> MarketContextSnapshot:
        call_spec = self._call_spec(context)
        func = getattr(ak, call_spec["func"])
        df = func(**call_spec.get("args", {}))
        rows = df.to_dict("records") if hasattr(df, "to_dict") else []
        if not rows:
            raise ValueError(f"empty market context result: {context.get('context_id')}")

        date_col = context.get("date_column") or _first_existing(rows[0], DEFAULT_DATE_COLUMNS)
        value_col = context.get("value_column") or _first_existing(rows[0], DEFAULT_VALUE_COLUMNS)
        if value_col is None:
            raise ValueError(
                f"cannot infer value column for {context.get('context_id')}; set value_column in YAML"
            )

        rows = _sort_rows(rows, date_col)
        values = [_to_float(row.get(value_col)) for row in rows]
        latest = values[-1] if values else None

        return MarketContextSnapshot(
            context_id=str(context["context_id"]),
            context_type=str(context.get("context_type") or "market_context"),
            name=context.get("name"),
            symbol=context.get("symbol"),
            as_of=(str(as_of)[:10] if as_of else _today()),
            value=latest,
            unit=context.get("unit"),
            change_1d=_pct_change(values, 1),
            change_5d=_pct_change(values, 5),
            change_20d=_pct_change(values, 20),
            source_url=context.get("source_url") or "https://akshare.akfamily.xyz/",
            provider=self.provider,
            payload={
                "akshare_func": call_spec["func"],
                "value_column": value_col,
                "date_column": date_col,
                "row_count": len(rows),
            },
        )

    def _call_spec(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("akshare_func"):
            args = dict(context.get("akshare_args") or {})
            if context.get("symbol") and "symbol" not in args:
                args["symbol"] = context.get("symbol")
            return {"func": context["akshare_func"], "args": args}
        context_type = str(context.get("context_type") or "")
        symbol = context.get("symbol")
        if context_type == "equity_index":
            return {"func": "stock_zh_index_daily_em", "args": {"symbol": symbol}}
        if context_type == "hk_index":
            return {"func": "stock_hk_index_daily_sina", "args": {"symbol": symbol}}
        raise ValueError("market context target must provide akshare_func, or use a supported context_type")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _first_existing(row: dict[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in row:
            return name
    return None


def _to_float(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _sort_rows(rows: list[dict[str, Any]], date_col: str | None) -> list[dict[str, Any]]:
    if not date_col:
        return rows
    return sorted(rows, key=lambda row: str(row.get(date_col) or ""))


def _pct_change(values: list[float | None], periods: int) -> float | None:
    clean = [v for v in values if v is not None]
    if len(clean) <= periods:
        return None
    prev = clean[-1 - periods]
    latest = clean[-1]
    if not prev:
        return None
    return round((latest / prev - 1.0) * 100.0, 4)
