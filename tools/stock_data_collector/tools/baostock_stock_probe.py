#!/usr/bin/env python3
"""Live BaoStock capability probe for stock_data_ingestion.

This diagnostic tool calls BaoStock's documented Python API directly for a small
set of A-share tickers. It is not a unit test and does not write SQLite,
Parquet, or raw object store files. It checks whether the BaoStock interfaces
used by the project return rows, whether expected columns are present, and which
scope should be treated as supplemental, unsupported, or empty.

Typical usage:

    python tools/baostock_stock_probe.py --tickers 600519.SH 000001.SZ

Optional:

    python tools/baostock_stock_probe.py \
        --tickers 600519.SH 000001.SZ \
        --start-date 2026-05-01 --end-date 2026-05-29 \
        --year 2026 --quarter 1 \
        --output-dir logs/baostock_probe_outputs

BaoStock is login-based but does not require user credentials. The script skips
HK and BSE tickers because the documented BaoStock Python API accepts sh/sz
A-share style codes for the project-relevant stock interfaces.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

STATUS_PASS = "PASS"
STATUS_WARNING = "PASS_WITH_WARNING"
STATUS_EMPTY = "EMPTY"
STATUS_MISSING_COLUMNS = "MISSING_COLUMNS"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"

STATUS_ORDER = {
    STATUS_PASS: 0,
    STATUS_WARNING: 1,
    STATUS_EMPTY: 2,
    STATUS_MISSING_COLUMNS: 3,
    STATUS_FAILED: 4,
    STATUS_SKIPPED: 5,
}


@dataclass(frozen=True)
class ProbeSpec:
    scope: str
    api_name: str
    mode: str
    required_columns: tuple[str, ...]
    fields: str | None = None
    optional: bool = False
    hint_if_empty: str = ""
    hint_if_failed: str = ""


@dataclass
class ProbeResult:
    scope: str
    api_name: str
    status: str
    ticker: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    traceback: str | None = None
    hint: str | None = None
    elapsed_seconds: float | None = None


class ProbeJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:  # noqa: D401
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if hasattr(obj, "item"):
            try:
                return obj.item()
            except Exception:  # noqa: BLE001
                pass
        return str(obj)


def today() -> date:
    return datetime.now().date()


def parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 8:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    raise ValueError(f"invalid date: {value!r}")


def date_text(value: date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    digits = re.sub(r"\D", "", str(value))
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return str(value)


def normalize_a_share_ticker(ticker: str) -> str:
    text = ticker.strip().upper().replace("_", ".").replace("-", ".")
    lower = text.lower().replace(".", "")
    if re.fullmatch(r"(sh|sz)\d{6}", lower):
        return f"{lower[:2]}.{lower[2:]}"
    if re.fullmatch(r"\d{6}\.(SH|SZ)", text):
        code, exchange = text.split(".")
        return f"{exchange.lower()}.{code}"
    if re.fullmatch(r"\d{6}", text):
        if text.startswith(("600", "601", "603", "605", "688", "689", "900")):
            return f"sh.{text}"
        if text.startswith(("000", "001", "002", "003", "200", "300", "301")):
            return f"sz.{text}"
    raise ValueError(f"BaoStock probe supports documented sh/sz A-share tickers only: {ticker!r}")


def is_skipped_ticker(ticker: str) -> str | None:
    text = ticker.strip().upper()
    if text.endswith(".HK") or text.lower().startswith("hk"):
        return "BaoStock documented project-relevant Python stock interfaces do not provide HK daily bars; use Tushare hk_daily for HK Stock Connect."
    if text.endswith(".BJ") or text.lower().startswith("bj"):
        return "BaoStock documented project-relevant Python stock interfaces accept sh/sz stock codes; BSE is skipped."
    return None


def result_to_rows(result: Any) -> tuple[list[str], list[dict[str, Any]]]:
    error_code = str(getattr(result, "error_code", "0"))
    if error_code != "0":
        raise RuntimeError(f"BaoStock error {error_code}: {getattr(result, 'error_msg', '')}")
    if hasattr(result, "get_data"):
        df = result.get_data()
        if df is None:
            return [], []
        columns = [str(col) for col in getattr(df, "columns", [])]
        if bool(getattr(df, "empty", False)):
            return columns, []
        rows = df.to_dict(orient="records")
        return columns, [clean_row(dict(row)) for row in rows]
    fields = [str(field) for field in getattr(result, "fields", [])]
    rows: list[dict[str, Any]] = []
    while result.next():
        rows.append(clean_row(dict(zip(fields, result.get_row_data()))))
    return fields, rows


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in row.items():
        if value is None:
            cleaned[str(key)] = None
        elif isinstance(value, (date, datetime)):
            cleaned[str(key)] = value.isoformat()
        elif hasattr(value, "item"):
            try:
                cleaned[str(key)] = value.item()
            except Exception:  # noqa: BLE001
                cleaned[str(key)] = value
        else:
            cleaned[str(key)] = value
    return cleaned


def call_baostock(
    *,
    bs: Any,
    spec: ProbeSpec,
    ticker: str | None = None,
    params: dict[str, Any] | None = None,
    filter_fn: Callable[[dict[str, Any]], bool] | None = None,
) -> ProbeResult:
    params = dict(params or {})
    start = datetime.now()
    try:
        fn = getattr(bs, spec.api_name)
        result = fn(**params)
        columns, rows = result_to_rows(result)
        if filter_fn is not None:
            rows = [row for row in rows if filter_fn(row)]
        missing = [] if not rows else [col for col in spec.required_columns if col not in columns and not any(col in row for row in rows)]
        if not rows:
            status = STATUS_EMPTY
            hint = spec.hint_if_empty or None
        elif missing:
            status = STATUS_MISSING_COLUMNS
            hint = "Returned rows but missing expected columns. Check BaoStock SDK/API version and adapter field mapping."
        else:
            status = STATUS_PASS
            hint = None
        return ProbeResult(
            scope=spec.scope,
            api_name=spec.api_name,
            ticker=ticker,
            status=status,
            params=params,
            rows=len(rows),
            columns=columns,
            missing_columns=missing,
            sample_rows=rows[:3],
            hint=hint,
            elapsed_seconds=(datetime.now() - start).total_seconds(),
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            scope=spec.scope,
            api_name=spec.api_name,
            ticker=ticker,
            status=STATUS_FAILED,
            params=params,
            error_message=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8),
            hint=spec.hint_if_failed or "Check network connectivity, BaoStock SDK version, and requested parameters.",
            elapsed_seconds=(datetime.now() - start).total_seconds(),
        )


PROBE_SPECS = [
    ProbeSpec(
        scope="security_master",
        api_name="query_stock_basic",
        mode="global",
        required_columns=("code", "code_name", "ipoDate", "type", "status"),
        hint_if_empty="No security rows returned; try running after BaoStock service is available or query a specific ticker.",
    ),
    ProbeSpec(
        scope="trade_calendar",
        api_name="query_trade_dates",
        mode="global_date_range",
        required_columns=("calendar_date", "is_trading_day"),
    ),
    ProbeSpec(
        scope="all_stock_trading_status",
        api_name="query_all_stock",
        mode="trade_day",
        required_columns=("code", "tradeStatus", "code_name"),
        hint_if_empty="BaoStock returns same-day all-stock data only after daily K-line update; try a confirmed past trading day.",
    ),
    ProbeSpec(
        scope="historical_bars_daily_raw",
        api_name="query_history_k_data_plus",
        mode="per_ticker_kline",
        fields="date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST",
        required_columns=("date", "code", "open", "high", "low", "close", "volume", "amount", "adjustflag"),
    ),
    ProbeSpec(
        scope="valuation_metric_daily",
        api_name="query_history_k_data_plus",
        mode="per_ticker_valuation",
        fields="date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM",
        required_columns=("date", "code", "close", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM"),
    ),
    ProbeSpec(
        scope="adj_factor",
        api_name="query_adjust_factor",
        mode="per_ticker_adj_factor",
        required_columns=("code", "dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor"),
        hint_if_empty="No adjustment events found in this window; expand date range for stocks with dividends/splits.",
    ),
    ProbeSpec(
        scope="dividend_corporate_action",
        api_name="query_dividend_data",
        mode="per_ticker_dividend",
        required_columns=("code", "dividOperateDate", "dividCashPsBeforeTax", "dividStocksPs", "dividReserveToStockPs"),
        optional=True,
        hint_if_empty="Dividend data can be sparse for the selected year/ticker.",
    ),
    ProbeSpec(
        scope="financial_profit",
        api_name="query_profit_data",
        mode="per_ticker_quarter",
        required_columns=("code", "pubDate", "statDate", "roeAvg", "netProfit", "epsTTM", "MBRevenue"),
    ),
    ProbeSpec(
        scope="financial_growth",
        api_name="query_growth_data",
        mode="per_ticker_quarter",
        required_columns=("code", "pubDate", "statDate", "YOYNI", "YOYPNI"),
    ),
    ProbeSpec(
        scope="financial_balance",
        api_name="query_balance_data",
        mode="per_ticker_quarter",
        required_columns=("code", "pubDate", "statDate", "currentRatio", "liabilityToAsset"),
    ),
    ProbeSpec(
        scope="financial_cash_flow",
        api_name="query_cash_flow_data",
        mode="per_ticker_quarter",
        required_columns=("code", "pubDate", "statDate", "CFOToNP"),
    ),
    ProbeSpec(
        scope="financial_dupont",
        api_name="query_dupont_data",
        mode="per_ticker_quarter",
        required_columns=("code", "pubDate", "statDate", "dupontROE", "dupontNitogr"),
    ),
]


def build_params(spec: ProbeSpec, ticker: str | None, start_date: date, end_date: date, year: int, quarter: int) -> dict[str, Any]:
    if spec.mode == "global":
        return {}
    if spec.mode == "global_date_range":
        return {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
    if spec.mode == "trade_day":
        return {"day": end_date.isoformat()}
    if ticker is None:
        raise ValueError(f"{spec.scope} requires a ticker")
    symbol = normalize_a_share_ticker(ticker)
    if spec.mode in {"per_ticker_kline", "per_ticker_valuation"}:
        return {
            "code": symbol,
            "fields": spec.fields,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "frequency": "d",
            "adjustflag": "3",
        }
    if spec.mode == "per_ticker_adj_factor":
        return {"code": symbol, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
    if spec.mode == "per_ticker_dividend":
        return {"code": symbol, "year": str(year), "yearType": "operate"}
    if spec.mode == "per_ticker_quarter":
        return {"code": symbol, "year": year, "quarter": quarter}
    raise ValueError(f"unknown probe mode: {spec.mode}")


def skip_result(spec: ProbeSpec, ticker: str, reason: str) -> ProbeResult:
    return ProbeResult(scope=spec.scope, api_name=spec.api_name, ticker=ticker, status=STATUS_SKIPPED, hint=reason)


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import baostock as bs  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"baostock is not installed. Install with: pip install baostock>=0.9.2. Error: {exc}") from exc

    end = parse_date(args.end_date, today())
    start = parse_date(args.start_date, end - timedelta(days=60))
    if start > end:
        raise SystemExit("--start-date must be <= --end-date")
    year = args.year or end.year
    quarter = args.quarter or ((end.month - 1) // 3 + 1)

    login_result = bs.login()
    login_code = str(getattr(login_result, "error_code", "0"))
    login_msg = str(getattr(login_result, "error_msg", ""))
    if login_code != "0":
        raise SystemExit(f"BaoStock login failed: {login_code} {login_msg}")

    results: list[ProbeResult] = []
    try:
        global_specs = [spec for spec in PROBE_SPECS if spec.mode in {"global", "global_date_range", "trade_day"}]
        ticker_specs = [spec for spec in PROBE_SPECS if spec not in global_specs]

        for spec in global_specs:
            results.append(call_baostock(bs=bs, spec=spec, params=build_params(spec, None, start, end, year, quarter)))

        for raw_ticker in args.tickers:
            skip_reason = is_skipped_ticker(raw_ticker)
            for spec in ticker_specs:
                if skip_reason:
                    results.append(skip_result(spec, raw_ticker, skip_reason))
                    continue
                try:
                    params = build_params(spec, raw_ticker, start, end, year, quarter)
                except Exception as exc:  # noqa: BLE001
                    results.append(skip_result(spec, raw_ticker, str(exc)))
                    continue
                results.append(call_baostock(bs=bs, spec=spec, ticker=raw_ticker, params=params))
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass

    worst = max((STATUS_ORDER.get(result.status, 99) for result in results), default=99)
    status = next((status for status, rank in STATUS_ORDER.items() if rank == worst), STATUS_FAILED)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "login": {"error_code": login_code, "error_msg": login_msg},
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "year": year,
        "quarter": quarter,
        "tickers": args.tickers,
        "results": [asdict(result) for result in results],
    }


def write_outputs(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"baostock_probe_{stamp}.json"
    md_path = output_dir / f"baostock_probe_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, cls=ProbeJSONEncoder), encoding="utf-8")

    lines = [
        "# BaoStock Probe Report",
        "",
        f"- generated_at: `{report['generated_at']}`",
        f"- status: `{report['status']}`",
        f"- date_range: `{report['start_date']} -> {report['end_date']}`",
        f"- financial_quarter: `{report['year']}Q{report['quarter']}`",
        "",
        "| scope | api | ticker | status | rows | missing_columns | hint |",
        "|---|---|---|---:|---:|---|---|",
    ]
    for row in report["results"]:
        missing = ", ".join(row.get("missing_columns") or [])
        hint = (row.get("hint") or row.get("error_message") or "").replace("|", "\\|")
        lines.append(
            f"| {row['scope']} | {row['api_name']} | {row.get('ticker') or ''} | {row['status']} | {row.get('rows', 0)} | {missing} | {hint} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe BaoStock APIs used by stock_data_ingestion.")
    parser.add_argument("--tickers", nargs="+", default=["600519.SH", "000001.SZ"], help="A-share tickers to probe. HK/BSE are reported as skipped.")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD or YYYYMMDD. Defaults to 60 days before --end-date/today.")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD or YYYYMMDD. Defaults to today.")
    parser.add_argument("--year", type=int, default=None, help="Financial/dividend year. Defaults to end-date year.")
    parser.add_argument("--quarter", type=int, choices=[1, 2, 3, 4], default=None, help="Financial quarter. Defaults to end-date quarter.")
    parser.add_argument("--output-dir", type=Path, default=Path("logs/baostock_probe_outputs"))
    parser.add_argument("--no-output", action="store_true", help="Print JSON only; do not write report files.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_probe(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, cls=ProbeJSONEncoder))
    if not args.no_output:
        json_path, md_path = write_outputs(report, args.output_dir)
        print(f"\nWrote: {json_path}")
        print(f"Wrote: {md_path}")
    return 0 if report["status"] not in {STATUS_FAILED, STATUS_MISSING_COLUMNS} else 2


if __name__ == "__main__":
    raise SystemExit(main())
