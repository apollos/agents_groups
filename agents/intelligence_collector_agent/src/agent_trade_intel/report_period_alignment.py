"""Compare stock statement periods with MIC published metric periods.

The comparison uses structured period fields only. Supplier amounts stay inside
the comparison and never appear in the result or in MIC facts.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .db import loads_json
from .ids import stable_hash

_CORE_FIELDS = ("operating_revenue", "parent_net_profit", "operating_cash_flow")
_AMOUNT_KEYS = {
    *_CORE_FIELDS,
    "metric_value", "net_profit", "operating_profit", "total_assets", "total_liabilities",
    "parent_equity",
}


def period_end(value: Any) -> date | None:
    """Map a statement period or a published metric period onto its period end."""
    text = str(value or "").strip()
    if not text:
        return None
    compact = re.fullmatch(r"(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])", text)
    if compact:
        year, month, day = (int(part) for part in compact.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    years = re.findall(r"20\d{2}", text)
    if not years:
        return None
    year = int(years[-1])
    if any(token in text for token in ("前三季", "三季", "Q3", "q3")):
        return date(year, 9, 30)
    if any(token in text for token in ("半年", "中报", "H1", "h1", "二季")):
        return date(year, 6, 30)
    if any(token in text for token in ("一季", "Q1", "q1")):
        return date(year, 3, 31)
    if any(token in text for token in ("四季", "年度", "年报", "全年")):
        return date(year, 12, 31)
    return None


def align_report_periods(
    statements: list[dict[str, Any]],
    observations: list[dict[str, Any]] | None,
    *,
    demand_id: str | None = None,
    directory_unresolved: bool = False,
) -> dict[str, Any]:
    """Return one alignment for statements and published metric periods.

    ``observations is None`` means the MIC report did not carry period identities.
    An empty list means the run published no metric periods.
    """
    stock_rows = [row for row in statements if isinstance(row, dict) and period_end(row.get("report_period"))]
    if observations is None:
        status = "mic_periods_unavailable"
        mic_end = None
        mic_label = None
    else:
        parsed = [
            (end, str(row.get("period")))
            for row in observations
            if isinstance(row, dict) and (end := period_end(row.get("period"))) is not None
        ]
        if not stock_rows:
            status = "stock_statements_unavailable"
            mic_end, mic_label = max(parsed) if parsed else (None, None)
        elif not parsed:
            status = "mic_periods_unparsed" if observations else "later_period_unread"
            mic_end, mic_label = None, None
        else:
            mic_end, mic_label = max(parsed)
            stock_end = max(period_end(row.get("report_period")) for row in stock_rows)
            status = "later_period_unread" if stock_end > mic_end else "no_later_stock_period"
    if not stock_rows and observations is not None and status != "stock_statements_unavailable":
        status = "stock_statements_unavailable"
    latest_rows = _latest_rows(stock_rows)
    stock_end = max((period_end(row.get("report_period")) for row in latest_rows), default=None)
    if observations is None:
        mic_end, mic_label = None, None
    announcements = sorted({
        str(row.get("announcement_date"))
        for row in latest_rows
        if row.get("announcement_date")
    })
    providers = sorted({
        str(row.get("effective_provider") or row.get("provider"))
        for row in latest_rows
        if row.get("effective_provider") or row.get("provider")
    })
    record_ids = sorted({str(row["record_id"]) for row in latest_rows if row.get("record_id")})
    statement_types = sorted({str(row.get("statement_type")) for row in latest_rows if row.get("statement_type")})
    period = str(latest_rows[0].get("report_period")) if latest_rows else None
    gap = None
    if status == "later_period_unread" and period:
        gap = {
            "gap_id": "gap_" + stable_hash(["later_report_period", demand_id, period, mic_label], 24),
            "gap_type": "later_report_period_unread",
            "priority": "high",
            "status": "open",
            "description": _gap_description(
                period=period,
                announcements=announcements,
                providers=providers,
                mic_period=mic_label or "没有可对齐的报告期",
                directory_unresolved=directory_unresolved,
            ),
            "record_ids": record_ids,
        }
    result = {
        "demand_id": demand_id,
        "status": status,
        "stock_report_period": period,
        "stock_period_end": stock_end.isoformat() if stock_end else None,
        "announcement_dates": announcements,
        "providers": providers,
        "record_ids": record_ids,
        "statement_types": statement_types,
        "mic_latest_period": mic_label,
        "mic_period_end": mic_end.isoformat() if mic_end else None,
        "directory_unresolved": bool(directory_unresolved),
        "core_fields_consistent": _core_fields_consistent(latest_rows) if latest_rows else None,
        "gap": gap,
    }
    leaked = _AMOUNT_KEYS.intersection(result)
    if leaked:
        raise RuntimeError(f"alignment exposed amount fields: {sorted(leaked)}")
    return result


def alignment_for_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Align each demand that has both a statement fetch and a MIC collection."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        grouped.setdefault(str(run.get("demand_id") or ""), []).append(run)
    alignments = []
    for demand_id, items in grouped.items():
        statements = []
        observations: list[dict[str, Any]] | None = None
        directory = False
        saw_mic = False
        for run in items:
            result = _run_result(run)
            if run.get("operation") == "fetch_financial_statement":
                statements.extend(_statements(result))
            if run.get("operation") == "collect_intelligence" or run.get("tool_name") == "market_intelligence_collector":
                saw_mic = True
                periods = result.get("published_metric_periods") if isinstance(result, dict) else None
                if isinstance(periods, list):
                    observations = [*(observations or []), *[row for row in periods if isinstance(row, dict)]]
                directory = directory or _directory_unresolved(result)
        if not statements or not saw_mic:
            continue
        alignments.append(align_report_periods(
            statements,
            observations,
            demand_id=demand_id or None,
            directory_unresolved=directory,
        ))
    return alignments


def _latest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    latest = max(period_end(row.get("report_period")) for row in rows)
    return [row for row in rows if period_end(row.get("report_period")) == latest]


def _core_fields_consistent(rows: list[dict[str, Any]]) -> bool:
    for field in _CORE_FIELDS:
        values = {_number(row.get(field)) for row in rows}
        values.discard(None)
        if len(values) > 1:
            return False
    return True


def _number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return None


def _gap_description(
    *,
    period: str,
    announcements: list[str],
    providers: list[str],
    mic_period: str,
    directory_unresolved: bool,
) -> str:
    announced = "、".join(announcements) if announcements else "未记录"
    provider_text = "、".join(providers) if providers else "未记录"
    if mic_period == "没有可对齐的报告期":
        published = "情报采集没有已发布的定期报告期。"
    else:
        published = f"情报采集已发布的定期报告期止于{mic_period}。"
    text = (
        f"股票数据记录了更晚报告期{period}，公告日{announced}，提供方{provider_text}。"
        f"{published}"
        "该报告期原文未进入已发布指标。"
    )
    if directory_unresolved:
        text += "官方目录读取未形成可核对的目录条目。"
    text += "供应商报表数值不作为已核实财报事实。"
    return text


def _run_result(run: dict[str, Any]) -> dict[str, Any]:
    if isinstance(run.get("result"), dict):
        return run["result"]
    loaded = loads_json(run.get("result_json"), {})
    return loaded if isinstance(loaded, dict) else {}


def _statements(result: dict[str, Any]) -> list[dict[str, Any]]:
    stdout = result.get("stdout")
    payload = stdout if isinstance(stdout, dict) else result
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("financial_statements") if isinstance(data, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _directory_unresolved(result: dict[str, Any]) -> bool:
    diagnostics = result.get("collection_diagnostics")
    failures = diagnostics.get("read_failures") if isinstance(diagnostics, dict) else None
    if not isinstance(failures, dict):
        return False
    try:
        return int(failures.get("index_scope_unresolved") or 0) > 0
    except (TypeError, ValueError):
        return False
