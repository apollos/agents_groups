#!/usr/bin/env python3
"""AKShare stock-data capability probe for stock_data_ingestion.

This script mirrors the production adapter's AKShare access strategy: AKShare
calls are attempted first with optional Eastmoney browser cookies, then supported
fallbacks are probed when the primary endpoint fails.

Default output directory: logs/akshare_probe_outputs
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

STATUS_PASS = "PASS"
STATUS_PASS_WARNING = "PASS_WITH_WARNING"
STATUS_EMPTY = "EMPTY"
STATUS_MISSING_COLUMNS = "MISSING_COLUMNS"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_OPTIONAL_FAILED = "OPTIONAL_FAILED"


class ProbeJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:  # noqa: D401
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        try:
            import pandas as pd  # type: ignore

            if isinstance(obj, pd.Timestamp):
                return obj.isoformat()
            if pd.isna(obj):
                return None
        except Exception:  # noqa: BLE001
            pass
        if hasattr(obj, "item"):
            try:
                return obj.item()
            except Exception:  # noqa: BLE001
                pass
        return str(obj)


@dataclass
class ProbeResult:
    name: str
    api: str
    ticker: str | None = None
    status: str = STATUS_FAILED
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    expected_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    sample: list[dict[str, Any]] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    message: str | None = None
    traceback: str | None = None
    optional: bool = False
    optional_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def load_dotenv_from_parents() -> None:
    if os.getenv("AKSHARE_NO_DOTENV"):
        return
    for root in [Path.cwd(), *Path.cwd().parents]:
        candidate = root / ".env"
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and not os.getenv(key):
                os.environ[key] = value
        break


def df_to_records(df: Any) -> tuple[list[str], list[dict[str, Any]]]:
    if df is None:
        return [], []
    if hasattr(df, "empty") and bool(getattr(df, "empty")):
        cols = [str(c) for c in getattr(df, "columns", [])]
        return cols, []
    if hasattr(df, "to_dict"):
        cols = [str(c) for c in getattr(df, "columns", [])]
        rows = df.to_dict(orient="records")
        return cols, [dict(row) for row in rows]
    if isinstance(df, list):
        rows = [dict(row) for row in df]
        cols = sorted({str(k) for row in rows for k in row})
        return cols, rows
    return [], []


def call_case(
    *,
    name: str,
    api: str,
    fn: Callable[..., Any],
    params: dict[str, Any] | None = None,
    ticker: str | None = None,
    expected_columns: list[str] | None = None,
    filter_fn: Callable[[dict[str, Any]], bool] | None = None,
    optional: bool = False,
    optional_reason: str | None = None,
) -> ProbeResult:
    params = dict(params or {})
    expected_columns = list(expected_columns or [])
    try:
        with eastmoney_cookie_request_headers():
            df = fn(**params)
        columns, rows = df_to_records(df)
        if filter_fn is not None:
            rows = [row for row in rows if filter_fn(row)]
        missing = [] if not rows else [col for col in expected_columns if col not in columns and not any(col in row for row in rows)]
        status = STATUS_PASS
        message = None
        if not rows:
            status = STATUS_EMPTY
            if optional_reason:
                message = optional_reason
        elif missing:
            status = STATUS_MISSING_COLUMNS
            message = "Returned rows but missing expected columns. Check AKShare version or field mapping."
        return ProbeResult(
            name=name,
            api=api,
            ticker=ticker,
            status=status,
            rows=len(rows),
            columns=columns,
            expected_columns=expected_columns,
            missing_columns=missing,
            sample=rows[:3],
            params=params,
            message=message,
            optional=optional,
            optional_reason=optional_reason,
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            api=api,
            ticker=ticker,
            status=STATUS_OPTIONAL_FAILED if optional else STATUS_FAILED,
            params=params,
            expected_columns=expected_columns,
            message=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8),
            optional=optional,
            optional_reason=optional_reason,
        )


def eastmoney_headers() -> dict[str, str]:
    cookie = os.getenv("EASTMONEY_COOKIE", "").strip()
    if not cookie:
        return {}
    return {
        "User-Agent": os.getenv(
            "EASTMONEY_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": os.getenv("EASTMONEY_ACCEPT_LANGUAGE", "zh-CN,zh;q=0.9,en;q=0.8"),
        "Referer": "https://data.eastmoney.com/zjlx/detail.html",
        "Cookie": cookie,
        "Connection": "close",
    }


@contextmanager
def eastmoney_cookie_request_headers() -> Iterator[None]:
    headers = eastmoney_headers()
    if not headers:
        yield
        return

    import requests  # type: ignore

    original_get = requests.get

    def get_with_eastmoney_headers(url: Any, *args: Any, **kwargs: Any) -> Any:
        if "eastmoney.com" in str(url):
            kwargs["headers"] = {**headers, **dict(kwargs.get("headers") or {})}
        return original_get(url, *args, **kwargs)

    requests.get = get_with_eastmoney_headers
    try:
        yield
    finally:
        requests.get = original_get


def eastmoney_secid(ticker: str) -> str:
    c, ex = normalize_ticker(ticker).split(".")
    market = {"SH": "1", "SZ": "0", "BJ": "0"}[ex]
    return f"{market}.{c}"


def numeric(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "nan", "None", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def eastmoney_money_flow_direct(stock: str, market: str) -> list[dict[str, Any]]:
    import requests  # type: ignore

    exchange = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[market]
    ticker = f"{stock}.{exchange}"
    response = requests.get(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        params={
            "lmt": "0",
            "klt": "101",
            "secid": eastmoney_secid(ticker),
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "_": int(datetime.now().timestamp() * 1000),
        },
        headers=eastmoney_headers(),
        timeout=20,
    )
    response.raise_for_status()
    rows: list[dict[str, Any]] = []
    for item in ((response.json().get("data") or {}).get("klines") or []):
        parts = str(item).split(",")
        if len(parts) < 13:
            continue
        rows.append(
            {
                "日期": parts[0],
                "主力净流入-净额": numeric(parts[1]),
                "主力净流入-净占比": numeric(parts[6]),
                "超大单净流入-净额": numeric(parts[5]),
                "大单净流入-净额": numeric(parts[4]),
                "中单净流入-净额": numeric(parts[3]),
                "小单净流入-净额": numeric(parts[2]),
                "raw_source_api": "eastmoney_fflow_daykline",
            }
        )
    return rows


def fallback_result(primary: ProbeResult, fallback: ProbeResult, *, api: str) -> ProbeResult:
    if fallback.status in {STATUS_PASS, STATUS_PASS_WARNING, STATUS_EMPTY}:
        fallback.api = api
        fallback.message = f"Primary {primary.api} failed; fallback succeeded. Primary error: {primary.message}"
        return fallback
    primary.message = f"{primary.message}; fallback {fallback.api} also failed: {fallback.message}"
    return primary


def normalize_ticker(ticker: str) -> str:
    text = ticker.strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", text):
        return text
    compact = text.replace(".", "")
    lower = compact.lower()
    if lower.startswith(("sh", "sz", "bj")) and re.fullmatch(r"(sh|sz|bj)\d{6}", lower):
        return f"{lower[2:].upper()}.{lower[:2].upper()}"
    if re.fullmatch(r"\d{6}", text):
        if text.startswith(("600", "601", "603", "605", "688", "689")):
            return f"{text}.SH"
        if text.startswith(("000", "001", "002", "003", "300", "301")):
            return f"{text}.SZ"
        if text.startswith(("43", "82", "83", "87", "88", "89")):
            return f"{text}.BJ"
    raise ValueError(f"unsupported A-share ticker: {ticker}")


def code(ticker: str) -> str:
    return normalize_ticker(ticker).split(".")[0]


def em_prefix_symbol(ticker: str) -> str:
    c, ex = normalize_ticker(ticker).split(".")
    return f"{ex}{c}"


def tx_symbol(ticker: str) -> str:
    c, ex = normalize_ticker(ticker).split(".")
    return f"{ex.lower()}{c}"


def market_code(ticker: str) -> str:
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[normalize_ticker(ticker).split(".")[1]]


def date8(value: date | str | None, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    digits = re.sub(r"\D", "", str(value))
    return digits[:8] if len(digits) >= 8 else default


def parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 8:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    raise ValueError(f"invalid date: {value}")


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv_from_parents()
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {
            "generated_at": datetime.now().isoformat(),
            "summary": {"FAILED": 1},
            "run_config": vars(args),
            "results": [ProbeResult(name="import_akshare", api="import akshare", status=STATUS_FAILED, message=str(exc)).as_dict()],
        }

    tickers = [normalize_ticker(t) for t in args.tickers]
    start = parse_date(args.start_date, date.today() - timedelta(days=180))
    end = parse_date(args.end_date, date.today())
    start_text = date8(start, "19700101")
    end_text = date8(end, "20991231")
    results: list[ProbeResult] = []

    # Cross-market/code lists and stock master enrichers.
    # Production code now treats exchange lists as the primary security-master path.
    # stock_info_a_code_name is a convenience wrapper and is only probed when requested.
    if args.include_code_name_diagnostic:
        results.append(call_case(
            name="security_master_code_name",
            api="stock_info_a_code_name",
            fn=ak.stock_info_a_code_name,
            expected_columns=["code", "name"],
            optional=not args.strict_eastmoney,
            optional_reason="Diagnostic convenience wrapper; production code uses exchange-specific lists as primary.",
        ))
    else:
        results.append(ProbeResult(
            name="security_master_code_name",
            api="stock_info_a_code_name",
            status=STATUS_SKIPPED,
            message="Skipped by default; pass --include-code-name-diagnostic to probe this convenience wrapper.",
            optional=True,
            optional_reason="Production code uses exchange-specific lists as primary.",
        ).as_dict())
    results.append(call_case(name="security_master_sh_list", api="stock_info_sh_name_code", fn=ak.stock_info_sh_name_code, params={"symbol": "主板A股"}, expected_columns=["证券代码", "证券简称", "公司全称", "上市日期"]))
    results.append(call_case(name="security_master_sz_list", api="stock_info_sz_name_code", fn=ak.stock_info_sz_name_code, params={"symbol": "A股列表"}, expected_columns=["A股代码", "A股简称", "A股上市日期", "A股总股本", "A股流通股本", "所属行业"]))
    results.append(call_case(name="security_master_bj_list", api="stock_info_bj_name_code", fn=ak.stock_info_bj_name_code, expected_columns=["证券代码", "证券简称", "总股本", "流通股本", "上市日期", "所属行业"]))

    for ticker in tickers:
        c = code(ticker)
        ep = em_prefix_symbol(ticker)
        norm = normalize_ticker(ticker)
        market = market_code(ticker)
        tx = tx_symbol(ticker)

        results.append(call_case(
            name="security_master_individual",
            api="stock_individual_info_em",
            fn=ak.stock_individual_info_em,
            ticker=ticker,
            params={"symbol": c},
            expected_columns=["item", "value"],
            optional=not args.strict_eastmoney,
            optional_reason="Eastmoney-only optional detail supplement; Tencent has no equivalent security-master detail endpoint.",
        ))
        daily_primary = call_case(
            name="historical_bars_daily",
            api="stock_zh_a_hist",
            fn=ak.stock_zh_a_hist,
            ticker=ticker,
            params={"symbol": c, "period": "daily", "start_date": start_text, "end_date": end_text, "adjust": ""},
            expected_columns=["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"],
        )
        if daily_primary.status == STATUS_FAILED and hasattr(ak, "stock_zh_a_hist_tx"):
            daily_fallback = call_case(
                name="historical_bars_daily",
                api="stock_zh_a_hist_tx",
                fn=ak.stock_zh_a_hist_tx,
                ticker=ticker,
                params={"symbol": tx, "start_date": start_text, "end_date": end_text, "adjust": ""},
                expected_columns=["date", "open", "high", "low", "close", "amount"],
            )
            results.append(fallback_result(daily_primary, daily_fallback, api="stock_zh_a_hist+stock_zh_a_hist_tx"))
        else:
            results.append(daily_primary)
        if args.include_eastmoney_hist:
            results.append(call_case(name="historical_bars_daily_eastmoney_diagnostic", api="stock_zh_a_hist", fn=ak.stock_zh_a_hist, ticker=ticker, params={"symbol": c, "period": "daily", "start_date": start_text, "end_date": end_text, "adjust": ""}, expected_columns=["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"]))
        results.append(call_case(
            name="historical_bars_minute_5m",
            api="stock_zh_a_hist_min_em",
            fn=ak.stock_zh_a_hist_min_em,
            ticker=ticker,
            params={"symbol": c, "period": "5", "start_date": f"{start.isoformat()} 09:30:00", "end_date": f"{end.isoformat()} 15:00:00", "adjust": ""},
            expected_columns=["时间", "开盘", "最高", "最低", "收盘", "成交量", "成交额"],
            optional=not args.strict_eastmoney,
            optional_reason="Eastmoney-only minute OHLC path; Tencent stock_zh_a_hist_tx supports daily bars only.",
        ))
        results.append(call_case(name="valuation_metric", api="stock_value_em", fn=ak.stock_value_em, ticker=ticker, params={"symbol": c}, expected_columns=["数据日期", "当日收盘价", "总市值", "流通市值", "总股本", "流通股本", "PE(TTM)"]))
        results.append(call_case(name="financial_statement_income", api="stock_profit_sheet_by_report_em", fn=ak.stock_profit_sheet_by_report_em, ticker=ticker, params={"symbol": ep}, expected_columns=["SECUCODE", "SECURITY_CODE", "REPORT_DATE"]))
        results.append(call_case(name="financial_statement_balance", api="stock_balance_sheet_by_report_em", fn=ak.stock_balance_sheet_by_report_em, ticker=ticker, params={"symbol": ep}, expected_columns=["SECUCODE", "SECURITY_CODE", "REPORT_DATE"]))
        results.append(call_case(name="financial_statement_cashflow", api="stock_cash_flow_sheet_by_report_em", fn=ak.stock_cash_flow_sheet_by_report_em, ticker=ticker, params={"symbol": ep}, expected_columns=["SECUCODE", "SECURITY_CODE", "REPORT_DATE"]))
        results.append(call_case(name="financial_indicator_em", api="stock_financial_analysis_indicator_em", fn=ak.stock_financial_analysis_indicator_em, ticker=ticker, params={"symbol": norm, "indicator": "按报告期"}, expected_columns=["SECUCODE", "SECURITY_CODE", "REPORT_DATE", "EPSJB", "ROEJQ"]))
        money_primary = call_case(
            name="money_flow",
            api="stock_individual_fund_flow",
            fn=ak.stock_individual_fund_flow,
            ticker=ticker,
            params={"stock": c, "market": market},
            expected_columns=["日期", "主力净流入-净额", "主力净流入-净占比"],
            optional=not args.strict_eastmoney,
            optional_reason="Eastmoney-only AKShare individual money-flow path; no Tencent equivalent in AKShare docs.",
        )
        if money_primary.status in {STATUS_FAILED, STATUS_OPTIONAL_FAILED} and eastmoney_headers():
            money_fallback = call_case(
                name="money_flow",
                api="eastmoney_fflow_daykline",
                fn=eastmoney_money_flow_direct,
                ticker=ticker,
                params={"stock": c, "market": market},
                expected_columns=["日期", "主力净流入-净额", "主力净流入-净占比"],
            )
            results.append(fallback_result(money_primary, money_fallback, api="stock_individual_fund_flow+eastmoney_fflow_daykline"))
        else:
            results.append(money_primary)
        results.append(call_case(name="corporate_action_dividend", api="stock_history_dividend_detail", fn=ak.stock_history_dividend_detail, ticker=ticker, params={"symbol": c, "indicator": "分红"}, expected_columns=["公告日期", "送股", "转增", "派息", "除权除息日", "股权登记日"]))
        results.append(call_case(
            name="corporate_action_rights_issue",
            api="stock_history_dividend_detail",
            fn=ak.stock_history_dividend_detail,
            ticker=ticker,
            params={"symbol": c, "indicator": "配股"},
            expected_columns=["公告日期"],
            optional=True,
            optional_reason="Event may be absent for the selected stock/date range.",
        ))

    # Full-table APIs filtered locally.
    wanted_codes = {code(t) for t in tickers}
    results.append(call_case(
        name="realtime_quote",
        api="stock_zh_a_spot_em",
        fn=ak.stock_zh_a_spot_em,
        expected_columns=["代码", "名称", "最新价", "成交量", "成交额", "总市值", "流通市值"],
        filter_fn=lambda row: str(row.get("代码")) in wanted_codes,
        optional=not args.strict_eastmoney,
        optional_reason="Eastmoney-only all-A realtime quote path; Tencent AKShare realtime is A+H-only, not all requested A shares.",
    ))
    if hasattr(ak, "stock_zh_a_st_em"):
        results.append(call_case(
            name="trading_status_st",
            api="stock_zh_a_st_em",
            fn=ak.stock_zh_a_st_em,
            expected_columns=["代码", "名称"],
            filter_fn=lambda row: str(row.get("代码")) in wanted_codes,
            optional=not args.strict_eastmoney,
            optional_reason="Eastmoney-only ST/risk-warning path; no Tencent all-A ST list equivalent in AKShare docs.",
        ))
    else:
        results.append(ProbeResult(name="trading_status_st", api="stock_zh_a_st_em", status=STATUS_SKIPPED, message="AKShare version has no stock_zh_a_st_em").as_dict())

    for offset in range(min(args.max_trading_status_days, max((end - start).days + 1, 1))):
        day = end - timedelta(days=offset)
        results.append(call_case(
            name=f"trading_status_suspend:{day:%Y%m%d}",
            api="stock_tfp_em",
            fn=ak.stock_tfp_em,
            params={"date": day.strftime("%Y%m%d")},
            expected_columns=["代码", "名称", "停牌时间", "停牌原因"],
            filter_fn=lambda row: str(row.get("代码")) in wanted_codes,
            optional=True,
            optional_reason="Sparse event API; EMPTY usually means no suspension event for selected tickers/date.",
        ))

    # Board membership: potentially slow, capped by --max-boards.
    if args.include_boards:
        industry_boards = call_case(name="industry_board_names", api="stock_board_industry_name_em", fn=ak.stock_board_industry_name_em, expected_columns=["板块名称"])
        results.append(industry_boards)
        for board in industry_boards.sample[: args.max_boards]:
            board_name = board.get("板块名称") or board.get("名称") or board.get("板块代码")
            if board_name:
                results.append(call_case(name=f"industry_membership:{board_name}", api="stock_board_industry_cons_em", fn=ak.stock_board_industry_cons_em, params={"symbol": str(board_name)}, expected_columns=["代码", "名称"], filter_fn=lambda row: str(row.get("代码")) in wanted_codes))
        concept_boards = call_case(name="concept_board_names", api="stock_board_concept_name_em", fn=ak.stock_board_concept_name_em, expected_columns=["板块名称"])
        results.append(concept_boards)
        for board in concept_boards.sample[: args.max_boards]:
            board_name = board.get("板块名称") or board.get("名称") or board.get("板块代码")
            if board_name:
                results.append(call_case(name=f"concept_membership:{board_name}", api="stock_board_concept_cons_em", fn=ak.stock_board_concept_cons_em, params={"symbol": str(board_name)}, expected_columns=["代码", "名称"], filter_fn=lambda row: str(row.get("代码")) in wanted_codes))
    else:
        results.append(ProbeResult(name="industry_concept", api="stock_board_*", status=STATUS_SKIPPED, message="Pass --include-boards to probe board membership; this can be slow.").as_dict())

    if args.include_index:
        for index_code in args.index_codes:
            symbol = index_code.lower() if index_code.lower().startswith(("sh", "sz")) else f"sh{index_code}"
            results.append(call_case(name=f"index_bars:{index_code}", api="stock_zh_index_daily_em", fn=ak.stock_zh_index_daily_em, params={"symbol": symbol, "start_date": start_text, "end_date": end_text}, expected_columns=["date", "open", "close", "high", "low"]))
            if hasattr(ak, "index_stock_cons_weight_csindex"):
                results.append(call_case(name=f"index_constituents:{index_code}", api="index_stock_cons_weight_csindex", fn=ak.index_stock_cons_weight_csindex, params={"symbol": re.sub(r"\D", "", index_code)}, expected_columns=["成分券代码"]))

    if hasattr(ak, "stock_repurchase_em"):
        results.append(call_case(name="corporate_action_repurchase", api="stock_repurchase_em", fn=ak.stock_repurchase_em, expected_columns=["股票代码"], filter_fn=lambda row: str(row.get("股票代码") or row.get("代码")) in wanted_codes))

    results.append(ProbeResult(name="adj_factor", api="N/A", status=STATUS_SKIPPED, message="AKShare has adjusted prices via stock_zh_a_hist but no standalone adj-factor table compatible with the canonical schema.").as_dict())

    result_dicts = [r.as_dict() if isinstance(r, ProbeResult) else r for r in results]
    summary: dict[str, int] = {}
    for item in result_dicts:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    return {"generated_at": datetime.now().isoformat(), "summary": summary, "run_config": vars(args), "results": result_dicts}


def write_outputs(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"akshare_stock_probe_{stamp}.json"
    md_path = output_dir / f"akshare_stock_probe_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, cls=ProbeJSONEncoder), encoding="utf-8")

    lines = ["# AKShare Stock Probe", "", f"Generated at: `{report['generated_at']}`", "", "## Summary", ""]
    for status, count in sorted(report["summary"].items()):
        lines.append(f"- **{status}**: {count}")
    lines.extend(["", "## Results", "", "| name | ticker | api | status | rows | missing_columns | message |", "|---|---:|---|---:|---:|---|---|"])
    for item in report["results"]:
        missing = ", ".join(item.get("missing_columns") or [])
        msg = (item.get("message") or "").replace("|", "/")
        lines.append(f"| {item.get('name')} | {item.get('ticker') or ''} | `{item.get('api')}` | {item.get('status')} | {item.get('rows', 0)} | {missing} | {msg} |")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe AKShare stock APIs used by stock_data_ingestion.")
    parser.add_argument("--tickers", nargs="+", required=True, help="A-share tickers, e.g. 600519.SH 000001.SZ")
    parser.add_argument("--start-date", default=None, help="YYYYMMDD; default: today - 180 days")
    parser.add_argument("--end-date", default=None, help="YYYYMMDD; default: today")
    parser.add_argument("--output-dir", default="logs/akshare_probe_outputs")
    parser.add_argument("--include-boards", action="store_true", help="Probe industry/concept board membership. This can be slow.")
    parser.add_argument("--max-boards", type=int, default=5, help="Max industry/concept boards to scan when --include-boards is set.")
    parser.add_argument("--include-index", action="store_true", help="Probe index bars/constituents as well.")
    parser.add_argument("--include-eastmoney-hist", action="store_true", help="Also probe Eastmoney stock_zh_a_hist as a separate diagnostic. Daily bars already try it first, then Tencent fallback.")
    parser.add_argument("--include-code-name-diagnostic", action="store_true", help="Also probe stock_info_a_code_name; production code uses exchange-specific lists as primary.")
    parser.add_argument("--strict-eastmoney", action="store_true", help="Treat Eastmoney-only optional probes as FAILED instead of OPTIONAL_FAILED.")
    parser.add_argument("--index-codes", nargs="*", default=["000300"], help="Index codes for --include-index.")
    parser.add_argument("--max-trading-status-days", type=int, default=3, help="Number of latest dates in range to query stock_tfp_em.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = run_probe(args)
    json_path, md_path = write_outputs(report, Path(args.output_dir))
    print(json.dumps({"summary": report["summary"], "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False, indent=2))
    return 0 if report["summary"].get(STATUS_FAILED, 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
