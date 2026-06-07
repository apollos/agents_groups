#!/usr/bin/env python3
"""
Live Tushare stock-data coverage probe.

Purpose
-------
This is a diagnostic command-line script, not a unit test. It calls Tushare Pro
for a small set of stock tickers and checks whether the data categories needed
by the stock-data ingestion project can be retrieved from Tushare, whether the
responses are empty, whether required columns are missing, and what likely
follow-up action is needed.

It does not write SQLite/Parquet/raw object store files and does not depend on
stock_data_ingestion package internals. It only requires: tushare, pandas, and
optionally python-dotenv.

Typical usage
-------------
    python tushare_stock_probe.py --tickers 600519.SH 000001.SZ

Optional:
    python tushare_stock_probe.py \
        --tickers 600519.SH 000001.SZ \
        --start-date 20250101 --end-date 20250630 \
        --financial-start-date 20220101 --financial-end-date 20250630 \
        --output-dir logs/tushare_probe_outputs

Environment
-----------
The script reads TUSHARE_TOKEN from the environment. If it is missing or empty,
it attempts to load .env without overriding already exported non-empty values.
You can also pass --env-file /path/to/.env.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

TICKER_RE = re.compile(r"^(?P<code>\d{6})\.(?P<suffix>SH|SZ|BJ)$", re.IGNORECASE)
PREFIX_RE = re.compile(r"^(?P<suffix>sh|sz|bj)(?P<code>\d{6})$", re.IGNORECASE)

EXCHANGE_BY_SUFFIX = {
    "SH": "SSE",
    "SZ": "SZSE",
    "BJ": "BSE",
}

# Status severity order used in the markdown summary.
STATUS_ORDER = {
    "PASS": 0,
    "PASS_WITH_WARNING": 1,
    "EMPTY": 2,
    "MISSING_COLUMNS": 3,
    "FAILED": 4,
    "SKIPPED": 5,
}


@dataclass(frozen=True)
class ProbeSpec:
    """Description of one Tushare API probe."""

    scope: str
    api_name: str
    method_name: str
    mode: str  # per_ticker, trade_calendar, global_filter, custom_stock_company
    required_columns: tuple[str, ...]
    fields: tuple[str, ...] = ()
    date_kind: str | None = None  # market, financial, event, calendar
    empty_is_possible: bool = False
    hint_if_empty: str = ""
    hint_if_failed: str = ""


@dataclass
class ProbeResult:
    scope: str
    api_name: str
    method_name: str
    status: str
    ticker: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    initial_error: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    hint: str | None = None
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    elapsed_seconds: float | None = None
    retried_without_fields: bool = False


PROBE_SPECS: list[ProbeSpec] = [
    ProbeSpec(
        scope="security_master",
        api_name="stock_basic",
        method_name="stock_basic",
        mode="per_ticker",
        required_columns=("ts_code", "symbol", "name", "list_status", "list_date"),
        fields=(
            "ts_code",
            "symbol",
            "name",
            "area",
            "industry",
            "fullname",
            "enname",
            "cnspell",
            "market",
            "exchange",
            "curr_type",
            "list_status",
            "list_date",
            "delist_date",
            "is_hs",
            "act_name",
            "act_ent_type",
        ),
        hint_if_failed="检查 TUSHARE_TOKEN、积分权限；如果全量查询失败，应避免先 normalize 全量退市异常代码。",
    ),
    ProbeSpec(
        scope="security_master_company_profile",
        api_name="stock_company",
        method_name="stock_company",
        mode="custom_stock_company",
        required_columns=("ts_code", "com_name", "exchange"),
        fields=(
            "ts_code",
            "com_name",
            "com_id",
            "exchange",
            "chairman",
            "manager",
            "secretary",
            "reg_capital",
            "setup_date",
            "province",
            "city",
            "introduction",
            "website",
            "email",
            "office",
            "employees",
            "main_business",
            "business_scope",
        ),
        hint_if_empty="stock_company 有些账号或接口版本可能更适合按 exchange 拉取再本地过滤，本脚本会自动尝试 fallback。",
    ),
    ProbeSpec(
        scope="trade_calendar",
        api_name="trade_cal",
        method_name="trade_cal",
        mode="trade_calendar",
        required_columns=("exchange", "cal_date", "is_open", "pretrade_date"),
        fields=("exchange", "cal_date", "is_open", "pretrade_date"),
        date_kind="calendar",
        hint_if_failed="交易日历不是单只股票数据；如果 BSE 失败，先确认 Tushare trade_cal 是否支持 BSE 参数。",
    ),
    ProbeSpec(
        scope="historical_bars_daily",
        api_name="daily",
        method_name="daily",
        mode="per_ticker",
        required_columns=("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"),
        fields=("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"),
        date_kind="market",
        hint_if_empty="所选日期区间可能没有交易日，或股票当期停牌/未上市；尝试扩大 --start-date/--end-date。",
    ),
    ProbeSpec(
        scope="adj_factor",
        api_name="adj_factor",
        method_name="adj_factor",
        mode="per_ticker",
        required_columns=("ts_code", "trade_date", "adj_factor"),
        fields=("ts_code", "trade_date", "adj_factor"),
        date_kind="market",
        hint_if_empty="复权因子可能需要更长日期区间；尝试扩大 --start-date/--end-date。",
    ),
    ProbeSpec(
        scope="valuation_metric_daily_basic",
        api_name="daily_basic",
        method_name="daily_basic",
        mode="per_ticker",
        required_columns=("ts_code", "trade_date", "close", "pe", "pb", "total_share", "float_share", "total_mv"),
        fields=(
            "ts_code",
            "trade_date",
            "close",
            "turnover_rate",
            "turnover_rate_f",
            "volume_ratio",
            "pe",
            "pe_ttm",
            "pb",
            "ps",
            "ps_ttm",
            "dv_ratio",
            "dv_ttm",
            "total_share",
            "float_share",
            "free_share",
            "total_mv",
            "circ_mv",
        ),
        date_kind="market",
        hint_if_empty="daily_basic 要求至少 ts_code 或 trade_date；若单票为空，尝试更近交易日或确认积分权限。",
    ),
    ProbeSpec(
        scope="trading_status_limit_price",
        api_name="stk_limit",
        method_name="stk_limit",
        mode="per_ticker",
        required_columns=("ts_code", "trade_date", "up_limit", "down_limit"),
        fields=("trade_date", "ts_code", "pre_close", "up_limit", "down_limit"),
        date_kind="market",
        hint_if_empty="涨跌停价按交易日更新；周末或未来日期可能没有数据，尝试扩大日期区间。",
    ),
    ProbeSpec(
        scope="trading_status_suspend",
        api_name="suspend_d",
        method_name="suspend_d",
        mode="per_ticker",
        required_columns=("ts_code", "trade_date", "suspend_type"),
        fields=("ts_code", "trade_date", "suspend_timing", "suspend_type"),
        date_kind="market",
        empty_is_possible=True,
        hint_if_empty="空结果通常表示该股票在区间内没有停复牌事件，不一定是接口失败。",
    ),
    ProbeSpec(
        scope="money_flow",
        api_name="moneyflow",
        method_name="moneyflow",
        mode="per_ticker",
        required_columns=("ts_code", "trade_date", "buy_sm_vol", "sell_sm_vol", "buy_lg_amount", "sell_lg_amount"),
        fields=(
            "ts_code",
            "trade_date",
            "buy_sm_vol",
            "buy_sm_amount",
            "sell_sm_vol",
            "sell_sm_amount",
            "buy_md_vol",
            "buy_md_amount",
            "sell_md_vol",
            "sell_md_amount",
            "buy_lg_vol",
            "buy_lg_amount",
            "sell_lg_vol",
            "sell_lg_amount",
            "buy_elg_vol",
            "buy_elg_amount",
            "sell_elg_vol",
            "sell_elg_amount",
            "net_mf_vol",
            "net_mf_amount",
        ),
        date_kind="market",
        hint_if_empty="资金流向数据起始于 2010 年；如果区间较新但为空，需确认积分权限或接口字段。",
    ),
    ProbeSpec(
        scope="financial_statement_income",
        api_name="income",
        method_name="income",
        mode="per_ticker",
        required_columns=("ts_code", "ann_date", "end_date", "total_revenue", "revenue", "n_income"),
        fields=(
            "ts_code",
            "ann_date",
            "f_ann_date",
            "end_date",
            "report_type",
            "comp_type",
            "end_type",
            "basic_eps",
            "diluted_eps",
            "total_revenue",
            "revenue",
            "total_profit",
            "n_income",
            "n_income_attr_p",
        ),
        date_kind="financial",
        hint_if_empty="财报接口按单只股票查询历史数据；如果最近公告区间为空，尝试扩大 --financial-start-date。",
    ),
    ProbeSpec(
        scope="financial_statement_balancesheet",
        api_name="balancesheet",
        method_name="balancesheet",
        mode="per_ticker",
        required_columns=("ts_code", "ann_date", "end_date", "total_assets", "total_liab"),
        fields=(
            "ts_code",
            "ann_date",
            "f_ann_date",
            "end_date",
            "report_type",
            "comp_type",
            "end_type",
            "total_share",
            "money_cap",
            "total_assets",
            "total_liab",
            "total_hldr_eqy_exc_min_int",
        ),
        date_kind="financial",
        hint_if_empty="财报接口按单只股票查询历史数据；如果最近公告区间为空，尝试扩大 --financial-start-date。",
    ),
    ProbeSpec(
        scope="financial_statement_cashflow",
        api_name="cashflow",
        method_name="cashflow",
        mode="per_ticker",
        required_columns=("ts_code", "ann_date", "end_date", "net_profit"),
        fields=(
            "ts_code",
            "ann_date",
            "f_ann_date",
            "end_date",
            "comp_type",
            "report_type",
            "end_type",
            "net_profit",
            "n_cashflow_act",
            "n_cash_flows_inv_act",
            "n_cash_flows_fnc_act",
            "c_cash_equ_end_period",
            "free_cashflow",
        ),
        date_kind="financial",
        hint_if_empty="财报接口按单只股票查询历史数据；如果最近公告区间为空，尝试扩大 --financial-start-date。",
    ),
    ProbeSpec(
        scope="financial_indicator",
        api_name="fina_indicator",
        method_name="fina_indicator",
        mode="per_ticker",
        required_columns=("ts_code", "ann_date", "end_date", "eps", "roe", "grossprofit_margin"),
        fields=(
            "ts_code",
            "ann_date",
            "end_date",
            "eps",
            "dt_eps",
            "roe",
            "roe_dt",
            "roa",
            "grossprofit_margin",
            "netprofit_margin",
            "debt_to_assets",
            "current_ratio",
            "quick_ratio",
        ),
        date_kind="financial",
        hint_if_empty="fina_indicator 每次最多返回有限记录；为空时先扩大报告/公告日期区间或检查积分权限。",
    ),
    ProbeSpec(
        scope="corporate_action_dividend",
        api_name="dividend",
        method_name="dividend",
        mode="per_ticker",
        required_columns=("ts_code", "end_date", "ann_date", "div_proc"),
        fields=(
            "ts_code",
            "end_date",
            "ann_date",
            "div_proc",
            "stk_div",
            "stk_bo_rate",
            "stk_co_rate",
            "cash_div",
            "cash_div_tax",
            "record_date",
            "ex_date",
            "pay_date",
            "div_listdate",
            "imp_ann_date",
            "base_date",
            "base_share",
        ),
        date_kind="event",
        empty_is_possible=True,
        hint_if_empty="所选公告/实施日期区间内可能没有分红送股事件；扩大 --event-start-date。",
    ),
    ProbeSpec(
        scope="corporate_action_repurchase",
        api_name="repurchase",
        method_name="repurchase",
        mode="global_filter",
        required_columns=("ts_code", "ann_date", "proc"),
        fields=("ts_code", "ann_date", "end_date", "proc", "exp_date", "vol", "amount", "high_limit", "low_limit"),
        date_kind="event",
        empty_is_possible=True,
        hint_if_empty="回购接口按公告日期区间拉取后过滤 ticker；区间内没有目标股票回购不代表接口失败。",
    ),
    ProbeSpec(
        scope="corporate_action_share_float",
        api_name="share_float",
        method_name="share_float",
        mode="per_ticker",
        required_columns=("ts_code", "ann_date", "float_date", "float_share"),
        fields=("ts_code", "ann_date", "float_date", "float_share", "float_ratio", "holder_name", "share_type"),
        date_kind="event",
        empty_is_possible=True,
        hint_if_empty="限售股解禁是事件数据，区间内没有事件不代表接口失败。",
    ),
]

NOT_PROBED_ITEMS = [
    {
        "scope": "realtime_quote",
        "reason": "本脚本先诊断 Tushare Pro 的股票基础、日线、财务、资金流、公司行为等批量/历史接口；实时行情需另行确认具体 Tushare 实时接口、权限和返回结构。",
    },
    {
        "scope": "minute_bars",
        "reason": "分钟线接口在不同 Tushare 版本/权限下差异较大；应在确认 exact API 后单独增加探针。",
    },
    {
        "scope": "concept_membership",
        "reason": "概念板块通常不是 stock_basic 的直接字段，需要选择 THS/DC/TDX 等板块接口后再做成分映射。",
    },
    {
        "scope": "index_data",
        "reason": "本脚本按“只针对股票 ticker”设计，不测试指数基础信息、指数行情和指数成分。",
    },
]


def today_yyyymmdd() -> str:
    return date.today().strftime("%Y%m%d")


def days_ago_yyyymmdd(days: int) -> str:
    return (date.today() - timedelta(days=days)).strftime("%Y%m%d")


def validate_yyyymmdd(value: str, arg_name: str) -> str:
    if not re.fullmatch(r"\d{8}", value or ""):
        raise argparse.ArgumentTypeError(f"{arg_name} must be YYYYMMDD, got {value!r}")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{arg_name} must be a valid date, got {value!r}") from exc
    return value


def normalize_ticker(value: str) -> str:
    raw = str(value).strip().upper()
    match = TICKER_RE.fullmatch(raw)
    if match:
        return f"{match.group('code')}.{match.group('suffix').upper()}"
    match = PREFIX_RE.fullmatch(raw)
    if match:
        suffix = match.group("suffix").upper()
        return f"{match.group('code')}.{suffix}"
    raise ValueError(f"cannot parse stock ticker {value!r}; expected e.g. 600519.SH or 000001.SZ")


def ticker_exchange(ticker: str) -> str:
    suffix = ticker.split(".", 1)[1].upper()
    return EXCHANGE_BY_SUFFIX.get(suffix, "")


def is_missing_env(value: str | None) -> bool:
    return value is None or str(value).strip() == ""


def parse_bool_env(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def candidate_env_files(explicit: str | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_var_path = os.getenv("STOCK_DATA_ENV_FILE")
    if env_var_path:
        candidates.append(Path(env_var_path).expanduser())

    cwd = Path.cwd().resolve()
    for p in [cwd, *cwd.parents]:
        candidates.append(p / ".env")
        candidates.append(p / "config" / ".env")

    # De-duplicate while preserving order.
    seen: set[str] = set()
    result: list[Path] = []
    for p in candidates:
        key = str(p.resolve()) if p.exists() else str(p.absolute())
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def load_env(explicit_env_file: str | None = None) -> Path | None:
    """Load .env only if TUSHARE_TOKEN is absent/empty, without overriding by default."""

    if not is_missing_env(os.getenv("TUSHARE_TOKEN")):
        return None

    override = parse_bool_env(os.getenv("STOCK_DATA_ENV_OVERRIDE"))
    for path in candidate_env_files(explicit_env_file):
        if not path.exists() or not path.is_file():
            continue
        try:
            try:
                from dotenv import load_dotenv  # type: ignore

                load_dotenv(path, override=override)
                return path
            except Exception:
                # Fall back to a minimal parser if python-dotenv is unavailable.
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if not stripped or stripped.startswith("#") or "=" not in stripped:
                            continue
                        key, value = stripped.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if not key:
                            continue
                        if override or is_missing_env(os.getenv(key)):
                            os.environ[key] = value
                return path
        except Exception:
            continue
    return None


def sanitize_value(value: Any) -> Any:
    """Convert dataframe values to JSON-safe scalars."""

    if value is None:
        return None
    # pandas/numpy scalars often expose item().
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    try:
        # NaN check without importing numpy.
        if value != value:  # noqa: PLR0124
            return None
    except Exception:
        pass
    return value


def dataframe_sample(df: Any, max_rows: int) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    try:
        rows = df.head(max_rows).to_dict(orient="records")
        return [{k: sanitize_value(v) for k, v in row.items()} for row in rows]
    except Exception:
        return []


def classify_error(exc: BaseException) -> str:
    message = str(exc).lower()
    if "token" in message or "权限" in message or "permission" in message or "积分" in message:
        return "permission_or_token_or_quota"
    if "每分钟" in message or "频次" in message or "rate" in message or "limit" in message:
        return "rate_limit"
    if "timeout" in message or "timed out" in message or "connection" in message or "network" in message:
        return "network"
    if "字段" in message or "field" in message or "不存在" in message or "invalid" in message:
        return "field_or_parameter"
    if "参数" in message or "param" in message:
        return "field_or_parameter"
    return "api_error"


def hint_for_error(category: str, spec: ProbeSpec, exc: BaseException) -> str:
    if spec.hint_if_failed:
        base = spec.hint_if_failed
    else:
        base = "检查 token、积分权限、字段列表、日期区间和接口参数。"
    if category == "permission_or_token_or_quota":
        return f"{base} 错误像是 token/积分/权限问题。"
    if category == "rate_limit":
        return f"{base} 错误像是流控/频率限制；调大 --sleep-seconds 后重试。"
    if category == "field_or_parameter":
        return f"{base} 错误像是字段或参数不兼容；脚本已尽量在字段失败时无 fields 重试。"
    if category == "network":
        return f"{base} 错误像是网络连接问题。"
    return base


def get_df_columns(df: Any) -> list[str]:
    try:
        return [str(c) for c in list(df.columns)]
    except Exception:
        return []


def count_rows(df: Any) -> int:
    try:
        return int(len(df))
    except Exception:
        return 0


def is_empty_df(df: Any) -> bool:
    try:
        return bool(df.empty)
    except Exception:
        return count_rows(df) == 0


def call_tushare_api(
    pro: Any,
    method_name: str,
    params: dict[str, Any],
    fields: Iterable[str],
    retry_without_fields: bool = True,
) -> tuple[Any, str | None, bool]:
    """Call a Tushare API. Returns (df, initial_error, retried_without_fields)."""

    clean_params = {k: v for k, v in params.items() if v not in (None, "")}
    fields_str = ",".join(fields)
    method = getattr(pro, method_name, None)

    def call_with_fields() -> Any:
        if method is not None:
            if fields_str:
                return method(**clean_params, fields=fields_str)
            return method(**clean_params)
        if fields_str:
            return pro.query(method_name, **clean_params, fields=fields_str)
        return pro.query(method_name, **clean_params)

    def call_without_fields() -> Any:
        if method is not None:
            return method(**clean_params)
        return pro.query(method_name, **clean_params)

    try:
        return call_with_fields(), None, False
    except Exception as first_exc:
        if not retry_without_fields:
            raise
        try:
            return call_without_fields(), str(first_exc), True
        except Exception:
            raise first_exc


def evaluate_result(
    spec: ProbeSpec,
    ticker: str | None,
    params: dict[str, Any],
    df: Any,
    started: float,
    initial_error: str | None,
    retried_without_fields: bool,
    max_sample_rows: int,
) -> ProbeResult:
    elapsed = round(time.perf_counter() - started, 3)
    columns = get_df_columns(df)
    rows = count_rows(df)
    missing = [c for c in spec.required_columns if c not in columns]

    if rows == 0:
        hint = spec.hint_if_empty or "空结果：需要确认日期区间、ticker、权限或该类事件是否确实不存在。"
        return ProbeResult(
            scope=spec.scope,
            api_name=spec.api_name,
            method_name=spec.method_name,
            status="EMPTY",
            ticker=ticker,
            params=params,
            rows=rows,
            columns=columns,
            missing_columns=missing,
            initial_error=initial_error,
            hint=hint,
            sample_rows=[],
            elapsed_seconds=elapsed,
            retried_without_fields=retried_without_fields,
        )

    if missing:
        return ProbeResult(
            scope=spec.scope,
            api_name=spec.api_name,
            method_name=spec.method_name,
            status="MISSING_COLUMNS",
            ticker=ticker,
            params=params,
            rows=rows,
            columns=columns,
            missing_columns=missing,
            initial_error=initial_error,
            hint="接口返回了数据，但缺少我们标准化所需的关键字段；需检查 fields 参数或改字段映射。",
            sample_rows=dataframe_sample(df, max_sample_rows),
            elapsed_seconds=elapsed,
            retried_without_fields=retried_without_fields,
        )

    status = "PASS_WITH_WARNING" if initial_error else "PASS"
    hint = None
    if initial_error:
        hint = "带 fields 调用失败后无 fields 重试成功；说明字段列表可能与当前 Tushare 接口不完全匹配。"
    return ProbeResult(
        scope=spec.scope,
        api_name=spec.api_name,
        method_name=spec.method_name,
        status=status,
        ticker=ticker,
        params=params,
        rows=rows,
        columns=columns,
        missing_columns=[],
        initial_error=initial_error,
        hint=hint,
        sample_rows=dataframe_sample(df, max_sample_rows),
        elapsed_seconds=elapsed,
        retried_without_fields=retried_without_fields,
    )


def failed_result(
    spec: ProbeSpec,
    ticker: str | None,
    params: dict[str, Any],
    exc: BaseException,
    started: float,
    include_traceback: bool,
) -> ProbeResult:
    category = classify_error(exc)
    message = str(exc)
    if include_traceback:
        message = message + "\n" + traceback.format_exc()
    return ProbeResult(
        scope=spec.scope,
        api_name=spec.api_name,
        method_name=spec.method_name,
        status="FAILED",
        ticker=ticker,
        params=params,
        rows=0,
        columns=[],
        missing_columns=list(spec.required_columns),
        error_category=category,
        error_message=message,
        hint=hint_for_error(category, spec, exc),
        elapsed_seconds=round(time.perf_counter() - started, 3),
    )


def date_params_for_spec(spec: ProbeSpec, args: argparse.Namespace) -> dict[str, str]:
    if spec.date_kind == "market":
        return {"start_date": args.start_date, "end_date": args.end_date}
    if spec.date_kind == "financial":
        return {"start_date": args.financial_start_date, "end_date": args.financial_end_date}
    if spec.date_kind == "event":
        return {"start_date": args.event_start_date, "end_date": args.event_end_date}
    if spec.date_kind == "calendar":
        return {"start_date": args.calendar_start_date, "end_date": args.calendar_end_date}
    return {}


def filter_df_by_tickers(df: Any, tickers: list[str]) -> Any:
    if df is None or is_empty_df(df) or "ts_code" not in get_df_columns(df):
        return df
    try:
        return df[df["ts_code"].astype(str).isin(tickers)]
    except Exception:
        return df


def probe_per_ticker_api(pro: Any, spec: ProbeSpec, tickers: list[str], args: argparse.Namespace) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for ticker in tickers:
        params = {"ts_code": ticker, **date_params_for_spec(spec, args)}
        started = time.perf_counter()
        try:
            df, initial_error, retried = call_tushare_api(pro, spec.method_name, params, spec.fields, args.retry_without_fields)
            result = evaluate_result(spec, ticker, params, df, started, initial_error, retried, args.max_sample_rows)
        except Exception as exc:  # noqa: BLE001 - diagnostic script should catch all API failures.
            result = failed_result(spec, ticker, params, exc, started, args.include_traceback)
        results.append(result)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    return results


def probe_stock_company(pro: Any, spec: ProbeSpec, tickers: list[str], args: argparse.Namespace) -> list[ProbeResult]:
    """Try stock_company(ts_code=...), then fallback to exchange query + local filter."""

    results: list[ProbeResult] = []
    for ticker in tickers:
        direct_params = {"ts_code": ticker}
        started = time.perf_counter()
        try:
            df, initial_error, retried = call_tushare_api(pro, spec.method_name, direct_params, spec.fields, args.retry_without_fields)
            if is_empty_df(df):
                exchange = ticker_exchange(ticker)
                fallback_params = {"exchange": exchange}
                df2, initial_error2, retried2 = call_tushare_api(pro, spec.method_name, fallback_params, spec.fields, args.retry_without_fields)
                df2 = filter_df_by_tickers(df2, [ticker])
                initial = initial_error or initial_error2
                retried = retried or retried2
                params = {"first_try": direct_params, "fallback_try": fallback_params}
                result = evaluate_result(spec, ticker, params, df2, started, initial, retried, args.max_sample_rows)
                if result.status == "PASS" or result.status == "PASS_WITH_WARNING":
                    result.hint = "stock_company(ts_code=...) 为空，但按 exchange 拉取后本地过滤成功；项目 adapter 可考虑采用此 fallback。"
            else:
                result = evaluate_result(spec, ticker, direct_params, df, started, initial_error, retried, args.max_sample_rows)
        except Exception as exc:  # noqa: BLE001
            result = failed_result(spec, ticker, direct_params, exc, started, args.include_traceback)
        results.append(result)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    return results


def probe_trade_calendar(pro: Any, spec: ProbeSpec, args: argparse.Namespace) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for exchange in args.calendar_exchanges:
        params = {"exchange": exchange, **date_params_for_spec(spec, args)}
        started = time.perf_counter()
        try:
            df, initial_error, retried = call_tushare_api(pro, spec.method_name, params, spec.fields, args.retry_without_fields)
            result = evaluate_result(spec, None, params, df, started, initial_error, retried, args.max_sample_rows)
        except Exception as exc:  # noqa: BLE001
            result = failed_result(spec, None, params, exc, started, args.include_traceback)
        results.append(result)
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    return results


def probe_global_filter_api(pro: Any, spec: ProbeSpec, tickers: list[str], args: argparse.Namespace) -> list[ProbeResult]:
    params = date_params_for_spec(spec, args)
    started = time.perf_counter()
    try:
        df, initial_error, retried = call_tushare_api(pro, spec.method_name, params, spec.fields, args.retry_without_fields)
        filtered = filter_df_by_tickers(df, tickers)
        result = evaluate_result(spec, ",".join(tickers), {"api_params": params, "local_filter_tickers": tickers}, filtered, started, initial_error, retried, args.max_sample_rows)
        if result.status == "EMPTY" and count_rows(df) > 0:
            result.hint = f"{spec.api_name} 返回了 {count_rows(df)} 行，但目标 ticker 在区间内没有记录；这通常不是接口失败。"
    except Exception as exc:  # noqa: BLE001
        result = failed_result(spec, ",".join(tickers), params, exc, started, args.include_traceback)
    if args.sleep_seconds > 0:
        time.sleep(args.sleep_seconds)
    return [result]


def run_probe(pro: Any, tickers: list[str], args: argparse.Namespace) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    enabled_scopes = set(args.scopes or [])
    for spec in PROBE_SPECS:
        if enabled_scopes and spec.scope not in enabled_scopes and spec.api_name not in enabled_scopes:
            continue
        if spec.mode == "per_ticker":
            results.extend(probe_per_ticker_api(pro, spec, tickers, args))
        elif spec.mode == "custom_stock_company":
            results.extend(probe_stock_company(pro, spec, tickers, args))
        elif spec.mode == "trade_calendar":
            results.extend(probe_trade_calendar(pro, spec, args))
        elif spec.mode == "global_filter":
            results.extend(probe_global_filter_api(pro, spec, tickers, args))
        else:
            results.append(
                ProbeResult(
                    scope=spec.scope,
                    api_name=spec.api_name,
                    method_name=spec.method_name,
                    status="SKIPPED",
                    hint=f"Unknown probe mode {spec.mode!r}",
                )
            )
    return results


def summarize(results: list[ProbeResult]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    failed_or_problematic = [r for r in results if r.status in {"FAILED", "MISSING_COLUMNS", "EMPTY", "PASS_WITH_WARNING"}]
    return {
        "total_checks": len(results),
        "status_counts": counts,
        "problematic_checks": len(failed_or_problematic),
        "overall_status": "PASS" if all(r.status == "PASS" for r in results) else "NEEDS_REVIEW",
    }


def markdown_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def result_sort_key(result: ProbeResult) -> tuple[int, str, str, str]:
    return (
        STATUS_ORDER.get(result.status, 99),
        result.scope,
        result.ticker or "",
        result.api_name,
    )


def write_markdown_report(path: Path, payload: dict[str, Any]) -> None:
    results = [ProbeResult(**r) for r in payload["results"]]
    sorted_results = sorted(results, key=result_sort_key)
    summary = payload["summary"]
    with path.open("w", encoding="utf-8") as f:
        f.write("# Tushare Stock Coverage Probe Report\n\n")
        f.write(f"Generated at: `{payload['generated_at']}`\n\n")
        f.write("## Run configuration\n\n")
        for key, value in payload["run_config"].items():
            f.write(f"- `{key}`: `{value}`\n")
        f.write("\n## Summary\n\n")
        f.write(f"- Overall status: **{summary['overall_status']}**\n")
        f.write(f"- Total checks: **{summary['total_checks']}**\n")
        f.write(f"- Problematic checks: **{summary['problematic_checks']}**\n")
        f.write(f"- Status counts: `{json.dumps(summary['status_counts'], ensure_ascii=False)}`\n\n")

        f.write("## Coverage table\n\n")
        f.write("| Status | Scope | API | Ticker/Exchange | Rows | Missing columns | Hint |\n")
        f.write("|---|---|---|---:|---:|---|---|\n")
        for r in sorted_results:
            f.write(
                "| {status} | {scope} | {api} | {ticker} | {rows} | {missing} | {hint} |\n".format(
                    status=markdown_escape(r.status),
                    scope=markdown_escape(r.scope),
                    api=markdown_escape(r.api_name),
                    ticker=markdown_escape(r.ticker or r.params.get("exchange") or ""),
                    rows=r.rows,
                    missing=markdown_escape(", ".join(r.missing_columns)),
                    hint=markdown_escape(r.hint or r.error_message or ""),
                )
            )

        f.write("\n## Failed / missing / empty details\n\n")
        for r in sorted_results:
            if r.status == "PASS":
                continue
            f.write(f"### {r.status}: {r.scope} / {r.api_name} / {r.ticker or r.params.get('exchange') or ''}\n\n")
            f.write(f"- Params: `{json.dumps(r.params, ensure_ascii=False, default=str)}`\n")
            f.write(f"- Rows: `{r.rows}`\n")
            f.write(f"- Columns: `{', '.join(r.columns)}`\n")
            if r.missing_columns:
                f.write(f"- Missing columns: `{', '.join(r.missing_columns)}`\n")
            if r.initial_error:
                f.write(f"- Initial fields error: `{r.initial_error}`\n")
            if r.error_category:
                f.write(f"- Error category: `{r.error_category}`\n")
            if r.error_message:
                f.write(f"- Error message: `{r.error_message}`\n")
            if r.hint:
                f.write(f"- Hint: {r.hint}\n")
            if r.sample_rows:
                f.write("- Sample rows:\n\n")
                f.write("```json\n")
                f.write(json.dumps(r.sample_rows, ensure_ascii=False, indent=2))
                f.write("\n```\n")
            f.write("\n")

        f.write("## Items intentionally not probed in this stock-only script\n\n")
        for item in payload["not_probed"]:
            f.write(f"- **{item['scope']}**: {item['reason']}\n")


def print_console_summary(results: list[ProbeResult], summary: dict[str, Any]) -> None:
    print("\nTushare stock coverage probe summary")
    print("====================================")
    print(f"Overall: {summary['overall_status']}")
    print(f"Status counts: {summary['status_counts']}")
    print("")
    print(f"{'STATUS':<18} {'SCOPE':<38} {'API':<18} {'TICKER':<14} {'ROWS':>6}  HINT")
    print("-" * 120)
    for r in sorted(results, key=result_sort_key):
        if r.status == "PASS" and not r.hint:
            hint = ""
        else:
            hint = r.hint or r.error_message or ""
        ticker = r.ticker or r.params.get("exchange") or ""
        print(f"{r.status:<18} {r.scope:<38} {r.api_name:<18} {str(ticker):<14} {r.rows:>6}  {hint[:80]}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe Tushare Pro stock-related APIs for selected stock tickers and report coverage gaps.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--tickers", nargs="+", default=["600519.SH", "000001.SZ"], help="Stock tickers to probe, e.g. 600519.SH 000001.SZ")
    parser.add_argument("--env-file", default=None, help="Optional .env file path. Used only if TUSHARE_TOKEN is missing/empty.")
    parser.add_argument("--start-date", default=days_ago_yyyymmdd(120), type=lambda v: validate_yyyymmdd(v, "--start-date"), help="Market-data start date YYYYMMDD")
    parser.add_argument("--end-date", default=today_yyyymmdd(), type=lambda v: validate_yyyymmdd(v, "--end-date"), help="Market-data end date YYYYMMDD")
    parser.add_argument("--financial-start-date", default=days_ago_yyyymmdd(365 * 5), type=lambda v: validate_yyyymmdd(v, "--financial-start-date"), help="Financial announcement start date YYYYMMDD")
    parser.add_argument("--financial-end-date", default=today_yyyymmdd(), type=lambda v: validate_yyyymmdd(v, "--financial-end-date"), help="Financial announcement end date YYYYMMDD")
    parser.add_argument("--event-start-date", default=days_ago_yyyymmdd(365 * 5), type=lambda v: validate_yyyymmdd(v, "--event-start-date"), help="Corporate action/event start date YYYYMMDD")
    parser.add_argument("--event-end-date", default=today_yyyymmdd(), type=lambda v: validate_yyyymmdd(v, "--event-end-date"), help="Corporate action/event end date YYYYMMDD")
    parser.add_argument("--calendar-start-date", default=days_ago_yyyymmdd(30), type=lambda v: validate_yyyymmdd(v, "--calendar-start-date"), help="Trade calendar start date YYYYMMDD")
    parser.add_argument("--calendar-end-date", default=days_ago_yyyymmdd(-30), type=lambda v: validate_yyyymmdd(v, "--calendar-end-date"), help="Trade calendar end date YYYYMMDD")
    parser.add_argument("--calendar-exchanges", nargs="+", default=["SSE", "SZSE", "BSE"], help="Exchange codes to test for trade_cal")
    parser.add_argument("--output-dir", default="logs/tushare_probe_outputs", help="Output directory for JSON and Markdown reports")
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="Sleep between API calls to reduce rate-limit risk")
    parser.add_argument("--max-sample-rows", type=int, default=3, help="Max sample rows recorded per check")
    parser.add_argument("--retry-without-fields", action=argparse.BooleanOptionalAction, default=True, help="Retry API without fields if fields call fails")
    parser.add_argument("--include-traceback", action="store_true", help="Include Python traceback in JSON/Markdown errors")
    parser.add_argument("--scopes", nargs="*", default=None, help="Optional subset of scope or API names to run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    env_file_loaded = load_env(args.env_file)

    token = os.getenv("TUSHARE_TOKEN")
    if is_missing_env(token):
        print("ERROR: TUSHARE_TOKEN is missing. Export it or put TUSHARE_TOKEN=... in .env.", file=sys.stderr)
        return 2

    try:
        tickers = [normalize_ticker(t) for t in args.tickers]
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        import tushare as ts  # type: ignore
    except Exception as exc:
        print(f"ERROR: cannot import tushare: {exc}. Install with: pip install tushare", file=sys.stderr)
        return 2

    try:
        ts.set_token(token)
        pro = ts.pro_api(token)
    except Exception as exc:
        print(f"ERROR: failed to initialize Tushare Pro API: {exc}", file=sys.stderr)
        return 2

    print("Running live Tushare probes...")
    print(f"Tickers: {', '.join(tickers)}")
    print(f"Market date range: {args.start_date} ~ {args.end_date}")
    print(f"Financial date range: {args.financial_start_date} ~ {args.financial_end_date}")
    if env_file_loaded:
        print(f"Loaded .env: {env_file_loaded}")

    results = run_probe(pro, tickers, args)
    summary = summarize(results)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"tushare_stock_probe_{stamp}.json"
    md_path = output_dir / f"tushare_stock_probe_{stamp}.md"

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "run_config": {
            "tickers": tickers,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "financial_start_date": args.financial_start_date,
            "financial_end_date": args.financial_end_date,
            "event_start_date": args.event_start_date,
            "event_end_date": args.event_end_date,
            "calendar_start_date": args.calendar_start_date,
            "calendar_end_date": args.calendar_end_date,
            "calendar_exchanges": args.calendar_exchanges,
            "env_file_loaded": str(env_file_loaded) if env_file_loaded else None,
            "tushare_token_present": True,
            "scopes": args.scopes,
        },
        "results": [asdict(r) for r in results],
        "not_probed": NOT_PROBED_ITEMS,
    }

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown_report(md_path, payload)
    print_console_summary(results, summary)
    print("")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")

    # Non-zero exit only for hard API failures or missing critical columns. Empty event-data results are common.
    hard_problem = any(r.status in {"FAILED", "MISSING_COLUMNS"} for r in results)
    return 1 if hard_problem else 0


if __name__ == "__main__":
    raise SystemExit(main())
