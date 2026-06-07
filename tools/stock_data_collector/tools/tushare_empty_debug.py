#!/usr/bin/env python3
"""
Targeted Tushare EMPTY-result debugger for stock-level data.

This script is intentionally independent from stock_data_ingestion project code.
It diagnoses whether EMPTY probe results are caused by:
  - API parameter misuse,
  - event data genuinely absent in the selected date range,
  - unsupported exchange parameters,
  - permission/rate-limit/network errors.

Focus areas based on the previous probe:
  - dividend
  - share_float
  - suspend_d
  - trade_cal BSE
  - daily_basic sanity check

Usage:
  python tools/tushare_empty_debug.py --tickers 600519.SH 000001.SZ
  python tools/tushare_empty_debug.py --tickers 600519.SH 000001.SZ --event-start-date 19900101
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import pandas as pd
except Exception as exc:  # pragma: no cover - diagnostic script
    print("ERROR: pandas is required. Install with: pip install pandas", file=sys.stderr)
    raise

try:
    import tushare as ts
except Exception as exc:  # pragma: no cover - diagnostic script
    print("ERROR: tushare is required. Install with: pip install tushare", file=sys.stderr)
    raise


DATE_COLUMNS = [
    "trade_date",
    "ann_date",
    "f_ann_date",
    "end_date",
    "record_date",
    "ex_date",
    "pay_date",
    "div_listdate",
    "imp_ann_date",
    "base_date",
    "float_date",
]

DIVIDEND_FIELDS = (
    "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,"
    "cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,"
    "imp_ann_date,base_date,base_share"
)

SHARE_FLOAT_FIELDS = "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type"
SUSPEND_FIELDS = "ts_code,trade_date,suspend_timing,suspend_type"
TRADE_CAL_FIELDS = "exchange,cal_date,is_open,pretrade_date"
DAILY_BASIC_FIELDS = (
    "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
    "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
    "free_share,total_mv,circ_mv"
)
STOCK_BASIC_FIELDS = "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date"


@dataclass
class ProbeCase:
    case_id: str
    scope: str
    api_name: str
    ticker: str | None
    params: dict[str, Any]
    status: str
    rows: int
    columns: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    date_ranges: dict[str, dict[str, Any]] = field(default_factory=dict)
    filtered_counts: dict[str, int] = field(default_factory=dict)
    unique_values: dict[str, list[Any]] = field(default_factory=dict)
    elapsed_seconds: float | None = None
    error_type: str | None = None
    error_message: str | None = None
    hint: str | None = None


@dataclass
class ProbeReport:
    generated_at: str
    config: dict[str, Any]
    cases: list[ProbeCase]
    findings: list[str]
    recommended_code_changes: list[str]


def yyyymmdd(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def load_dotenv_if_needed() -> str | None:
    """Load .env without overriding non-empty existing environment variables."""
    if os.getenv("TUSHARE_TOKEN"):
        return None

    candidates: list[Path] = []
    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        candidates.append(p / ".env")
        candidates.append(p / "config" / ".env")

    for env_path in candidates:
        if not env_path.exists() or not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and not os.getenv(key):
                os.environ[key] = value
        return str(env_path)
    return None


def clean_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def df_to_records(df: pd.DataFrame, n: int = 5) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    rows = df.head(n).to_dict(orient="records")
    return [{k: clean_value(v) for k, v in row.items()} for row in rows]


def date_range_summary(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    if df is None or df.empty:
        return summary
    for col in DATE_COLUMNS:
        if col not in df.columns:
            continue
        s = df[col].dropna().astype(str)
        s = s[s.str.fullmatch(r"\d{8}", na=False)]
        if s.empty:
            continue
        summary[col] = {
            "min": s.min(),
            "max": s.max(),
            "non_null": int(s.shape[0]),
            "unique": int(s.nunique()),
        }
    return summary


def filter_count_by_date_columns(df: pd.DataFrame, start_date: str, end_date: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if df is None or df.empty:
        return counts
    for col in DATE_COLUMNS:
        if col not in df.columns:
            continue
        s = df[col].dropna().astype(str)
        mask = s.str.fullmatch(r"\d{8}", na=False) & (s >= start_date) & (s <= end_date)
        counts[col] = int(mask.sum())
    return counts


def unique_values(df: pd.DataFrame, columns: Iterable[str], max_values: int = 20) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    if df is None or df.empty:
        return out
    for col in columns:
        if col in df.columns:
            vals = [clean_value(v) for v in df[col].dropna().unique().tolist()[:max_values]]
            out[col] = vals
    return out


def call_api(
    case_id: str,
    scope: str,
    api_name: str,
    ticker: str | None,
    call: Callable[[], pd.DataFrame],
    params: dict[str, Any],
    event_start_date: str,
    event_end_date: str,
    unique_cols: Iterable[str] = (),
    hint_if_empty: str | None = None,
) -> tuple[ProbeCase, pd.DataFrame | None]:
    start = time.perf_counter()
    try:
        df = call()
        elapsed = round(time.perf_counter() - start, 3)
        if df is None:
            df = pd.DataFrame()
        status = "PASS" if not df.empty else "EMPTY"
        case = ProbeCase(
            case_id=case_id,
            scope=scope,
            api_name=api_name,
            ticker=ticker,
            params=params,
            status=status,
            rows=int(len(df)),
            columns=list(map(str, df.columns.tolist())),
            sample_rows=df_to_records(df),
            date_ranges=date_range_summary(df),
            filtered_counts=filter_count_by_date_columns(df, event_start_date, event_end_date),
            unique_values=unique_values(df, unique_cols),
            elapsed_seconds=elapsed,
            hint=hint_if_empty if status == "EMPTY" else None,
        )
        return case, df
    except Exception as exc:
        elapsed = round(time.perf_counter() - start, 3)
        return (
            ProbeCase(
                case_id=case_id,
                scope=scope,
                api_name=api_name,
                ticker=ticker,
                params=params,
                status="FAILED",
                rows=0,
                elapsed_seconds=elapsed,
                error_type=type(exc).__name__,
                error_message=str(exc),
                hint=classify_error(str(exc)),
            ),
            None,
        )


def classify_error(msg: str) -> str:
    lower = msg.lower()
    if "权限" in msg or "permission" in lower or "积分" in msg:
        return "可能是接口权限/积分不足；先用 Tushare 数据工具或账号权限中心确认。"
    if "频" in msg or "limit" in lower or "timeout" in lower:
        return "可能是限频或网络超时；降低频率后重试。"
    if "field" in lower or "字段" in msg:
        return "可能是 fields 列表包含当前接口不支持的字段；尝试不传 fields。"
    if "参数" in msg or "param" in lower:
        return "可能是参数名或参数组合不被接口接受；对照 Tushare 文档检查。"
    return "未分类错误；请贴出完整异常。"


def build_findings(cases: list[ProbeCase]) -> tuple[list[str], list[str]]:
    by_id = {c.case_id: c for c in cases}
    findings: list[str] = []
    changes: list[str] = []

    # Daily basic sanity.
    daily_cases = [c for c in cases if c.scope == "daily_basic_sanity"]
    if daily_cases and all(c.status == "PASS" for c in daily_cases):
        findings.append("daily_basic：估值/股本/市值字段可正常获取；不需要因为 total_share/float_share 缺失而改 Tushare daily_basic 调用。")

    # Dividend parameter diagnosis.
    dividend_tickers = sorted({c.ticker for c in cases if c.scope.startswith("dividend") and c.ticker})
    for ticker in dividend_tickers:
        wrong = by_id.get(f"dividend_with_start_end:{ticker}")
        only = by_id.get(f"dividend_ts_code_only:{ticker}")
        if wrong and only:
            if wrong.status == "EMPTY" and only.rows > 0:
                findings.append(
                    f"dividend/{ticker}：ts_code + start_date/end_date 返回空，但 ts_code-only 返回 {only.rows} 行；说明 dividend 不能按 start_date/end_date 这样筛，应该先按 ts_code 获取后本地按 ann_date/record_date/ex_date/imp_ann_date/pay_date 过滤。"
                )
                changes.append(
                    "TushareAdapter.fetch_corporate_action(dividend) 不应向 pro.dividend 传 start_date/end_date；应传 ts_code，再在本地按 ann_date/record_date/ex_date/imp_ann_date/pay_date 做区间过滤。"
                )
            elif only.status == "EMPTY":
                findings.append(f"dividend/{ticker}：ts_code-only 也为空；该股票在 Tushare dividend 中可能确实没有记录，或权限/接口返回范围需再查。")

    # Share float event diagnosis.
    share_tickers = sorted({c.ticker for c in cases if c.scope.startswith("share_float") and c.ticker})
    for ticker in share_tickers:
        recent = by_id.get(f"share_float_recent:{ticker}")
        only = by_id.get(f"share_float_ts_code_only:{ticker}")
        long_range = by_id.get(f"share_float_long_range:{ticker}")
        if only and only.rows > 0:
            if recent and recent.status == "EMPTY":
                findings.append(
                    f"share_float/{ticker}：近区间为空，但 ts_code-only 返回 {only.rows} 行；说明接口可取，近区间没有解禁事件或日期过滤字段不匹配。"
                )
            changes.append(
                "share_float 可保留 start_date/end_date 参数；但 EMPTY 应作为 event_absent，而不是 provider_failed。必要时也可 ts_code-only 回填全历史后本地过滤。"
            )
        elif long_range and long_range.rows > 0:
            findings.append(f"share_float/{ticker}：长区间返回 {long_range.rows} 行；近区间为空更可能是事件不存在。")
        elif only and only.status == "EMPTY":
            findings.append(f"share_float/{ticker}：ts_code-only 也为空；该股票可能没有限售解禁记录。")

    # Suspend diagnosis.
    for ticker in sorted({c.ticker for c in cases if c.scope.startswith("suspend") and c.ticker}):
        recent = by_id.get(f"suspend_recent:{ticker}")
        long_range = by_id.get(f"suspend_long_range:{ticker}")
        only = by_id.get(f"suspend_ts_code_only:{ticker}")
        if recent and recent.status == "EMPTY" and (long_range and long_range.status == "EMPTY") and (only and only.status == "EMPTY"):
            findings.append(f"suspend_d/{ticker}：recent、long_range、ts_code-only 都为空；更可能是该股没有停复牌事件，不应视为错误。")
        elif long_range and long_range.rows > 0:
            findings.append(f"suspend_d/{ticker}：长区间返回 {long_range.rows} 行；近区间为空只是近期没有停复牌。")

    market_suspend = by_id.get("suspend_known_date_all_market")
    if market_suspend:
        if market_suspend.status == "PASS":
            findings.append("suspend_d：已用一个全市场历史日期验证接口本身可返回数据；个股为空一般代表无事件。")
        elif market_suspend.status == "EMPTY":
            findings.append("suspend_d：全市场历史日期也为空，需要换一个已知停牌日期再验证，或确认接口权限。")

    # Trade calendar diagnosis.
    bse = by_id.get("trade_cal:BSE")
    blank = by_id.get("trade_cal:blank")
    sse = by_id.get("trade_cal:SSE")
    szse = by_id.get("trade_cal:SZSE")
    stock_basic_bse = by_id.get("stock_basic_exchange:BSE")
    if bse and bse.status == "EMPTY" and sse and sse.rows > 0 and szse and szse.rows > 0:
        findings.append("trade_cal/BSE：BSE 返回空，而 SSE/SZSE 正常；这更像是 Tushare trade_cal 不支持 BSE 参数，而不是交易日历整体失败。")
        changes.append("Tushare trade_calendar 不要把 BSE 当作必然可用的 exchange 参数；BSE 可用 exchange='' 或 SSE/SZSE 日历派生，或标记 source_api 不支持。")
    if stock_basic_bse and stock_basic_bse.rows > 0 and bse and bse.status == "EMPTY":
        findings.append("stock_basic(exchange='BSE') 能返回北交所股票但 trade_cal(exchange='BSE') 为空时，可确认是 trade_cal 参数支持问题，而不是 BSE 市场不存在。")

    return findings, sorted(set(changes))


def write_markdown(report: ProbeReport, path: Path) -> None:
    lines: list[str] = []
    lines.append("# Tushare EMPTY Debug Report")
    lines.append("")
    lines.append(f"Generated at: `{report.generated_at}`")
    lines.append("")
    lines.append("## Config")
    for k, v in report.config.items():
        lines.append(f"- `{k}`: `{v}`")
    lines.append("")
    lines.append("## Findings")
    if report.findings:
        for item in report.findings:
            lines.append(f"- {item}")
    else:
        lines.append("- No automatic findings generated.")
    lines.append("")
    lines.append("## Recommended code changes")
    if report.recommended_code_changes:
        for item in report.recommended_code_changes:
            lines.append(f"- {item}")
    else:
        lines.append("- No code change suggested by automatic rules.")
    lines.append("")
    lines.append("## Case table")
    lines.append("| Status | Case | API | Ticker/Exchange | Rows | Params | Date ranges | Filtered counts | Hint |")
    lines.append("|---|---|---|---:|---:|---|---|---|---|")
    for c in report.cases:
        date_ranges = json.dumps(c.date_ranges, ensure_ascii=False, sort_keys=True)
        filtered = json.dumps(c.filtered_counts, ensure_ascii=False, sort_keys=True)
        params = json.dumps(c.params, ensure_ascii=False, sort_keys=True)
        target = c.ticker or c.params.get("exchange") or ""
        hint = c.hint or c.error_message or ""
        lines.append(f"| {c.status} | {c.case_id} | {c.api_name} | {target} | {c.rows} | `{params}` | `{date_ranges}` | `{filtered}` | {hint} |")
    lines.append("")
    lines.append("## Sample rows")
    for c in report.cases:
        if c.sample_rows:
            lines.append(f"### {c.case_id}")
            lines.append("```json")
            lines.append(json.dumps(c.sample_rows, ensure_ascii=False, indent=2))
            lines.append("```")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    default_end = yyyymmdd(datetime.now())
    default_start = yyyymmdd(datetime.now() - timedelta(days=180))
    parser = argparse.ArgumentParser(description="Targeted Tushare EMPTY-result debugger")
    parser.add_argument("--tickers", nargs="+", default=["600519.SH", "000001.SZ"], help="TS tickers, e.g. 600519.SH 000001.SZ")
    parser.add_argument("--start-date", default=default_start, help="Recent data start date, YYYYMMDD")
    parser.add_argument("--end-date", default=default_end, help="Recent data end date, YYYYMMDD")
    parser.add_argument("--event-start-date", default="19900101", help="Long event start date for corporate action diagnostics")
    parser.add_argument("--event-end-date", default=default_end, help="Event end date, YYYYMMDD")
    parser.add_argument("--calendar-start-date", default=default_start, help="Calendar start date, YYYYMMDD")
    parser.add_argument("--calendar-end-date", default=default_end, help="Calendar end date, YYYYMMDD")
    parser.add_argument("--known-suspend-date", default="20200312", help="Known all-market suspend_d sample date to validate endpoint")
    parser.add_argument("--output-dir", default="logs/tushare_probe_outputs", help="Output directory")
    args = parser.parse_args()

    env_loaded = load_dotenv_if_needed()
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        print("ERROR: TUSHARE_TOKEN is missing. Export it or put it in .env", file=sys.stderr)
        return 2

    ts.set_token(token)
    pro = ts.pro_api(token)

    cases: list[ProbeCase] = []

    # 1. daily_basic sanity: confirms valuation/share fields are available.
    for ticker in args.tickers:
        params = {"ts_code": ticker, "start_date": args.start_date, "end_date": args.end_date, "fields": DAILY_BASIC_FIELDS}
        case, _ = call_api(
            f"daily_basic:{ticker}",
            "daily_basic_sanity",
            "daily_basic",
            ticker,
            lambda ticker=ticker, params=params: pro.daily_basic(**params),
            params,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["ts_code"],
        )
        cases.append(case)

    # 2. dividend: compare problematic start/end call vs ts_code-only.
    for ticker in args.tickers:
        params_bad = {"ts_code": ticker, "start_date": args.event_start_date, "end_date": args.event_end_date, "fields": DIVIDEND_FIELDS}
        case, _ = call_api(
            f"dividend_with_start_end:{ticker}",
            "dividend_parameter_check",
            "dividend",
            ticker,
            lambda params=params_bad: pro.dividend(**params),
            params_bad,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["div_proc"],
            hint_if_empty="If ts_code-only returns rows, start_date/end_date are not valid dividend filters.",
        )
        cases.append(case)

        params_only = {"ts_code": ticker, "fields": DIVIDEND_FIELDS}
        case, _ = call_api(
            f"dividend_ts_code_only:{ticker}",
            "dividend_ts_code_only",
            "dividend",
            ticker,
            lambda params=params_only: pro.dividend(**params),
            params_only,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["div_proc"],
            hint_if_empty="No dividend records for this stock, or account/API issue.",
        )
        cases.append(case)

    # 3. share_float: event data; test recent, long, and ts_code-only.
    for ticker in args.tickers:
        params_recent = {"ts_code": ticker, "start_date": args.start_date, "end_date": args.end_date, "fields": SHARE_FLOAT_FIELDS}
        case, _ = call_api(
            f"share_float_recent:{ticker}",
            "share_float_recent",
            "share_float",
            ticker,
            lambda params=params_recent: pro.share_float(**params),
            params_recent,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["share_type"],
            hint_if_empty="No unlock events in recent interval is normal for many stocks.",
        )
        cases.append(case)

        params_long = {"ts_code": ticker, "start_date": args.event_start_date, "end_date": args.event_end_date, "fields": SHARE_FLOAT_FIELDS}
        case, _ = call_api(
            f"share_float_long_range:{ticker}",
            "share_float_long_range",
            "share_float",
            ticker,
            lambda params=params_long: pro.share_float(**params),
            params_long,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["share_type"],
            hint_if_empty="No unlock events in long interval may mean this stock has no records in share_float.",
        )
        cases.append(case)

        params_only = {"ts_code": ticker, "fields": SHARE_FLOAT_FIELDS}
        case, _ = call_api(
            f"share_float_ts_code_only:{ticker}",
            "share_float_ts_code_only",
            "share_float",
            ticker,
            lambda params=params_only: pro.share_float(**params),
            params_only,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["share_type"],
            hint_if_empty="No share_float records for this stock.",
        )
        cases.append(case)

    # 4. suspend_d: event data; validate recent, long, ts_code-only, and known all-market date.
    for ticker in args.tickers:
        params_recent = {"ts_code": ticker, "start_date": args.start_date, "end_date": args.end_date, "fields": SUSPEND_FIELDS}
        case, _ = call_api(
            f"suspend_recent:{ticker}",
            "suspend_recent",
            "suspend_d",
            ticker,
            lambda params=params_recent: pro.suspend_d(**params),
            params_recent,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["suspend_type"],
            hint_if_empty="No recent suspend/resume event is normal.",
        )
        cases.append(case)

        params_long = {"ts_code": ticker, "start_date": args.event_start_date, "end_date": args.event_end_date, "fields": SUSPEND_FIELDS}
        case, _ = call_api(
            f"suspend_long_range:{ticker}",
            "suspend_long_range",
            "suspend_d",
            ticker,
            lambda params=params_long: pro.suspend_d(**params),
            params_long,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["suspend_type"],
            hint_if_empty="No long-range suspend/resume event may still be normal for this stock.",
        )
        cases.append(case)

        params_only = {"ts_code": ticker, "fields": SUSPEND_FIELDS}
        case, _ = call_api(
            f"suspend_ts_code_only:{ticker}",
            "suspend_ts_code_only",
            "suspend_d",
            ticker,
            lambda params=params_only: pro.suspend_d(**params),
            params_only,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["suspend_type"],
            hint_if_empty="No suspend/resume records for this stock.",
        )
        cases.append(case)

    params_known = {"suspend_type": "S", "trade_date": args.known_suspend_date, "fields": SUSPEND_FIELDS}
    case, _ = call_api(
        "suspend_known_date_all_market",
        "suspend_known_date_all_market",
        "suspend_d",
        None,
        lambda params=params_known: pro.suspend_d(**params),
        params_known,
        args.event_start_date,
        args.event_end_date,
        unique_cols=["suspend_type"],
        hint_if_empty="If empty, try another known suspend date or check permission.",
    )
    cases.append(case)

    # 5. trade_cal: BSE vs known supported exchanges.
    for exchange in ["", "SSE", "SZSE", "BSE"]:
        label = exchange or "blank"
        params = {"exchange": exchange, "start_date": args.calendar_start_date, "end_date": args.calendar_end_date, "fields": TRADE_CAL_FIELDS}
        case, _ = call_api(
            f"trade_cal:{label}",
            "trade_cal_exchange_check",
            "trade_cal",
            None,
            lambda params=params: pro.trade_cal(**params),
            params,
            args.event_start_date,
            args.event_end_date,
            unique_cols=["exchange", "is_open"],
            hint_if_empty="This exchange parameter may not be supported by Tushare trade_cal.",
        )
        cases.append(case)

    # 6. Check whether BSE stocks exist in stock_basic while trade_cal BSE is empty.
    params_bse_stock = {"exchange": "BSE", "list_status": "L", "fields": STOCK_BASIC_FIELDS}
    case, _ = call_api(
        "stock_basic_exchange:BSE",
        "stock_basic_bse_existence_check",
        "stock_basic",
        None,
        lambda params=params_bse_stock: pro.stock_basic(**params),
        params_bse_stock,
        args.event_start_date,
        args.event_end_date,
        unique_cols=["exchange", "market", "list_status"],
        hint_if_empty="If empty, Tushare stock_basic may not use exchange='BSE' for BSE stocks in this account/version.",
    )
    cases.append(case)

    findings, changes = build_findings(cases)

    report = ProbeReport(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        config={
            "tickers": args.tickers,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "event_start_date": args.event_start_date,
            "event_end_date": args.event_end_date,
            "calendar_start_date": args.calendar_start_date,
            "calendar_end_date": args.calendar_end_date,
            "known_suspend_date": args.known_suspend_date,
            "env_loaded": env_loaded,
            "tushare_token_present": bool(token),
        },
        cases=cases,
        findings=findings,
        recommended_code_changes=changes,
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"tushare_empty_debug_{stamp}.json"
    md_path = out_dir / f"tushare_empty_debug_{stamp}.md"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(asdict(report), f, ensure_ascii=False, indent=2)
    write_markdown(report, md_path)

    print("\n=== Tushare EMPTY Debug Summary ===")
    print(f"Output JSON: {json_path}")
    print(f"Output MD:   {md_path}")
    print("\nFindings:")
    for item in findings or ["No automatic findings generated."]:
        print(f"- {item}")
    print("\nRecommended code changes:")
    for item in changes or ["No code change suggested by automatic rules."]:
        print(f"- {item}")
    print("\nCase status counts:")
    counts: dict[str, int] = {}
    for c in cases:
        counts[c.status] = counts.get(c.status, 0) + 1
    print(json.dumps(counts, ensure_ascii=False, indent=2))

    # Non-zero only if there are API failures, not just EMPTY event data.
    return 1 if any(c.status == "FAILED" for c in cases) else 0


if __name__ == "__main__":
    raise SystemExit(main())
