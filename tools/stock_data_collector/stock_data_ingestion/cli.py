from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from stock_data_ingestion.config import load_config
from stock_data_ingestion.env import ensure_env_loaded
from stock_data_ingestion.logging_config import setup_logging
from stock_data_ingestion.schemas.requests import Adjust, Frequency, RequestType, StockDataRequest
from stock_data_ingestion.services.collector import StockDataCollector
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.storage.raw_object_store import RawObjectStore


def _json_default(obj: Any) -> str:
    return str(obj)


def _normalize_adjust_alias(value: str) -> str:
    """Normalize user-facing adjustment aliases to the internal enum values.

    The internal schema uses ``none`` for unadjusted/raw bars. Many data
    vendors and users call the same concept ``raw``. Keep both accepted at
    the CLI boundary so examples and habits from Tushare/AKShare do not fail
    before a request is built.
    """
    normalized = (value or "none").strip().lower()
    return "none" if normalized == "raw" else normalized


def _response_payload(resp: Any) -> dict[str, Any]:
    return json.loads(resp.model_dump_json())


def _build_database(config):  # type: ignore[no-untyped-def]
    from stock_data_ingestion.storage.database import Database

    db = Database(config.storage.sqlite_path, enable_wal=config.storage.enable_wal)
    return db


def _build_collector(config_dir: str | None = None) -> StockDataCollector:
    config = load_config(config_dir)
    setup_logging(config.storage.log_path)
    raw_store = RawObjectStore(config.storage.raw_object_root)
    db = _build_database(config)
    db.init()
    runner = IngestionRunner(config, raw_store, database=db)
    return StockDataCollector(runner)


def _provider_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "providers": getattr(args, "providers", None),
        "canonical_provider": getattr(args, "canonical_provider", None),
    }


def _add_provider_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--providers",
        nargs="+",
        default=None,
        help="Providers for this request, e.g. tushare akshare. Overrides config/.env for this command.",
    )
    parser.add_argument(
        "--canonical-provider",
        default=None,
        help="Canonical provider for this request. Defaults to config canonical or first selected provider.",
    )


def cmd_init_db(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    from stock_data_ingestion.storage.migrations import init_db

    init_db(config.storage.sqlite_path, enable_wal=config.storage.enable_wal)
    print(json.dumps({"status": "success", "sqlite_path": str(config.storage.sqlite_path), "wal": config.storage.enable_wal}, ensure_ascii=False))


def cmd_fetch_security_master(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_security_master(args.tickers, **_provider_args(args))
    print(resp.model_dump_json(indent=2))


def cmd_fetch_trade_calendar(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    exchanges = list(args.exchanges or [])
    if args.exchange:
        exchanges.insert(0, args.exchange)
    exchanges = list(dict.fromkeys(exchanges))
    if not exchanges:
        raise SystemExit("trade-calendar requires --exchange EXCHANGE or --exchanges EXCHANGE [EXCHANGE ...]")

    responses = [collector.fetch_trade_calendar(exchange, args.start_date, args.end_date, **_provider_args(args)) for exchange in exchanges]
    if len(responses) == 1:
        print(responses[0].model_dump_json(indent=2))
        return

    payloads = [_response_payload(resp) for resp in responses]
    statuses = [str(payload.get("status", "unknown")) for payload in payloads]
    if all(status == "success" for status in statuses):
        status = "success"
    elif any(status in {"success", "partial_success"} for status in statuses):
        status = "partial_success"
    else:
        status = "failed"
    # Surface an aggregate persistence summary at the top level so agents relying on the
    # documented response contract still see whether writes happened, even though each
    # exchange keeps its own detailed `persistence`/`data` block under `responses`.
    saved_flags = [bool((payload.get("persistence") or {}).get("saved")) for payload in payloads]
    persistence_summary = {
        "saved": bool(saved_flags) and all(saved_flags),
        "saved_any": any(saved_flags),
        "exchanges_saved": sum(1 for flag in saved_flags if flag),
        "exchanges_total": len(payloads),
    }
    print(
        json.dumps(
            {"status": status, "exchanges": exchanges, "persistence": persistence_summary, "responses": payloads},
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


def cmd_fetch_historical_bars(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    adjust = Adjust(_normalize_adjust_alias(args.adjust))
    resp = collector.fetch_historical_bars(args.tickers, args.start_date, args.end_date, Frequency(args.frequency), adjust, cross_validate=args.cross_validate, **_provider_args(args))
    print(resp.model_dump_json(indent=2))


def cmd_fetch_valuation(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_valuation(args.tickers, args.start_date, args.end_date, **_provider_args(args))
    print(resp.model_dump_json(indent=2))


def cmd_fetch_adj_factor(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_adj_factor(args.tickers, args.start_date, args.end_date, cross_validate=args.cross_validate, **_provider_args(args))
    print(resp.model_dump_json(indent=2))


def cmd_fetch_financial_indicator(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_financial_indicator(args.tickers, args.start_date, args.end_date, **_provider_args(args))
    print(resp.model_dump_json(indent=2))


def cmd_fetch_financial_statement(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_financial_statement(
        args.tickers,
        args.start_date,
        args.end_date,
        statement_types=args.statement_types,
        period=args.period,
        **_provider_args(args),
    )
    print(resp.model_dump_json(indent=2))


def cmd_fetch_money_flow(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_money_flow(args.tickers, args.start_date, args.end_date, **_provider_args(args))
    print(resp.model_dump_json(indent=2))


def cmd_fetch_trading_status(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_trading_status(args.tickers, args.start_date, args.end_date, **_provider_args(args))
    print(resp.model_dump_json(indent=2))


def cmd_fetch_corporate_action(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    action_types = args.action_types or None
    resp = collector.fetch_corporate_action(
        args.tickers,
        args.start_date,
        args.end_date,
        action_types=action_types,
        event_date_field=args.event_date_field,
        **_provider_args(args),
    )
    print(resp.model_dump_json(indent=2))


def cmd_query_bars(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    db = _build_database(config)
    from stock_data_ingestion.services.query_service import QueryService

    with db.session() as session:
        service = QueryService(session)
        if getattr(args, "trading_ready", False):
            df = service.get_trading_ready_bars(args.ticker, args.start_date, args.end_date, args.frequency, _normalize_adjust_alias(args.adjust), minimum_quality=args.minimum_quality)
        else:
            df = service.get_bars(args.ticker, args.start_date, args.end_date, args.frequency, _normalize_adjust_alias(args.adjust))
    print(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def cmd_query_conflicts(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    db = _build_database(config)
    from stock_data_ingestion.services.query_service import QueryService

    with db.session() as session:
        df = QueryService(session).get_conflicts(args.ticker)
    print(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def cmd_query_meta_summary(args: argparse.Namespace) -> None:
    from sqlalchemy import func, inspect, select

    from stock_data_ingestion.normalization.ticker import normalize_ticker
    from stock_data_ingestion.storage import models

    config = load_config(args.config_dir)
    db = _build_database(config)
    ticker = normalize_ticker(args.ticker) if args.ticker else None

    standard_models = {
        "securities": models.SecurityModel,
        "trade_calendar": models.TradeCalendarModel,
        "trading_status": models.TradingStatusModel,
        "daily_bars": models.DailyBarModel,
        "adj_factors": models.AdjFactorModel,
        "valuation_metrics": models.ValuationMetricModel,
        "financial_statements": models.FinancialStatementModel,
        "financial_indicators": models.FinancialIndicatorModel,
        "money_flow": models.MoneyFlowModel,
        "corporate_actions": models.CorporateActionModel,
    }
    metadata_models = {
        "raw_payload_index": models.RawPayloadIndexModel,
        "source_fetch_logs": models.SourceFetchLogModel,
        "provider_comparisons": models.ProviderComparisonModel,
        "data_quality_conflicts": models.DataQualityConflictModel,
        "ingestion_requests": models.IngestionRequestModel,
        "ingestion_runs": models.IngestionRunModel,
    }

    with db.session() as session:
        existing_tables = set(inspect(session.bind).get_table_names())

        def table_count(model: type[Any]) -> int:
            if model.__tablename__ not in existing_tables:
                return 0
            return int(session.scalar(select(func.count()).select_from(model)) or 0)

        rows_by_table = {name: table_count(model) for name, model in {**standard_models, **metadata_models}.items()}
        company_count = 0
        daily_trading_days = 0
        daily_range = {"min": None, "max": None}
        if ticker:
            stock_models = [
                model
                for model in standard_models.values()
                if model.__tablename__ in existing_tables and hasattr(model, "normalized_ticker")
            ]
            seen: set[str] = set()
            for model in stock_models:
                rows = session.execute(select(model.normalized_ticker).where(model.normalized_ticker == ticker).distinct()).scalars().all()
                seen.update(str(row) for row in rows)
            company_count = len(seen)

            if models.DailyBarModel.__tablename__ in existing_tables:
                daily_trading_days = int(
                    session.scalar(
                        select(func.count(func.distinct(models.DailyBarModel.trade_date))).where(models.DailyBarModel.normalized_ticker == ticker)
                    )
                    or 0
                )
                min_date, max_date = session.execute(
                    select(func.min(models.DailyBarModel.trade_date), func.max(models.DailyBarModel.trade_date)).where(models.DailyBarModel.normalized_ticker == ticker)
                ).one()
                daily_range = {
                    "min": str(min_date) if min_date is not None else None,
                    "max": str(max_date) if max_date is not None else None,
                }

        provider_fetch_summary: list[dict[str, Any]] = []
        if models.SourceFetchLogModel.__tablename__ in existing_tables:
            for provider, source_api, status, calls, rows_fetched in session.execute(
                select(
                    models.SourceFetchLogModel.provider,
                    models.SourceFetchLogModel.source_api,
                    models.SourceFetchLogModel.status,
                    func.count(),
                    func.sum(models.SourceFetchLogModel.rows_fetched),
                )
                .group_by(models.SourceFetchLogModel.provider, models.SourceFetchLogModel.source_api, models.SourceFetchLogModel.status)
                .order_by(models.SourceFetchLogModel.provider, models.SourceFetchLogModel.source_api, models.SourceFetchLogModel.status)
            ):
                provider_fetch_summary.append(
                    {
                        "provider": provider,
                        "source_api": source_api,
                        "status": status,
                        "calls": int(calls or 0),
                        "rows_fetched": int(rows_fetched or 0),
                    }
                )

        provider_comparison_summary: list[dict[str, Any]] = []
        if models.ProviderComparisonModel.__tablename__ in existing_tables:
            for record_type, compared_provider, status, comparisons in session.execute(
                select(
                    models.ProviderComparisonModel.record_type,
                    models.ProviderComparisonModel.compared_provider,
                    models.ProviderComparisonModel.status,
                    func.count(),
                )
                .group_by(models.ProviderComparisonModel.record_type, models.ProviderComparisonModel.compared_provider, models.ProviderComparisonModel.status)
                .order_by(models.ProviderComparisonModel.record_type, models.ProviderComparisonModel.compared_provider, models.ProviderComparisonModel.status)
            ):
                provider_comparison_summary.append(
                    {
                        "record_type": record_type,
                        "compared_provider": compared_provider,
                        "status": status,
                        "comparisons": int(comparisons or 0),
                    }
                )

        conflict_summary: list[dict[str, Any]] = []
        if models.DataQualityConflictModel.__tablename__ in existing_tables:
            for record_type, field_name, severity, conflicts in session.execute(
                select(
                    models.DataQualityConflictModel.record_type,
                    models.DataQualityConflictModel.field_name,
                    models.DataQualityConflictModel.severity,
                    func.count(),
                )
                .group_by(models.DataQualityConflictModel.record_type, models.DataQualityConflictModel.field_name, models.DataQualityConflictModel.severity)
                .order_by(models.DataQualityConflictModel.record_type, models.DataQualityConflictModel.severity, models.DataQualityConflictModel.field_name)
            ):
                conflict_summary.append(
                    {
                        "record_type": record_type,
                        "field_name": field_name,
                        "severity": severity,
                        "conflicts": int(conflicts or 0),
                    }
                )

    sqlite_path = Path(config.storage.sqlite_path)
    test_root = Path(args.test_root) if args.test_root else sqlite_path.parent
    summary = {
        "ticker": ticker,
        "test_root": str(test_root),
        "sqlite_path": str(sqlite_path),
        "company_count_for_ticker": company_count,
        "daily_bar_trading_days": daily_trading_days,
        "daily_bar_date_range": daily_range,
        "rows_by_table": rows_by_table,
        "standard_rows_total": sum(rows_by_table[name] for name in standard_models),
        "metadata_rows_total": sum(rows_by_table[name] for name in metadata_models),
        "disk_usage_bytes": {
            "sqlite": sqlite_path.stat().st_size if sqlite_path.exists() else 0,
            "raw_objects": _dir_size(Path(config.storage.raw_object_root)),
            "parquet": _dir_size(Path(config.storage.parquet_root)),
            "logs": _dir_size(Path(config.storage.log_path).parent),
            "test_root_total": _dir_size(test_root),
        },
        "provider_fetch_summary": provider_fetch_summary,
        "provider_comparison_summary": provider_comparison_summary,
        "conflict_summary": conflict_summary,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))


def cmd_verify_raw(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    raw_store = RawObjectStore(config.storage.raw_object_root)
    expected = args.expected_hash
    raw_payload_ref = args.raw_payload_id

    if expected is None:
        try:
            db = _build_database(config)
            from stock_data_ingestion.services.query_service import QueryService

            with db.session() as session:
                df = QueryService(session).get_raw_payload_index(args.raw_payload_id)
            if not df.empty:
                expected = str(df.iloc[0]["raw_hash"])
                raw_payload_ref = str(df.iloc[0]["raw_payload_ref"])
        except Exception:
            # Fall back to raw metadata when SQLAlchemy is unavailable or the DB has not been initialized.
            expected = None

    metadata, _ = raw_store.load_raw_payload(raw_payload_ref)
    expected = expected or metadata.get("raw_hash")
    computed = raw_store.compute_raw_hash(raw_payload_ref)
    result = {
        "raw_payload_id": args.raw_payload_id,
        "raw_payload_ref": raw_payload_ref,
        "computed_hash": computed,
        "expected_hash": expected,
        "verified": computed == expected if expected else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_verify_eastmoney_cookie(args: argparse.Namespace) -> None:
    import sys

    from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter
    from stock_data_ingestion.schemas.records import AdapterFetchStatus

    request = StockDataRequest(
        request_id="verify_eastmoney_cookie",
        request_type=RequestType.money_flow,
        tickers=[args.ticker],
        start_date=args.start_date,
        end_date=args.end_date,
        provider_priority=["akshare"],
        canonical_provider="akshare",
    )
    adapter = AKShareAdapter()

    # `verify eastmoney-cookie` answers one question: does the cookie-gated Eastmoney daykline
    # endpoint return data with the configured EASTMONEY_COOKIE? stdout stays a clean true/false
    # so scripts can branch on it; human-readable diagnostics go to stderr.
    cookie_present = bool(adapter._eastmoney_money_flow_headers())  # noqa: SLF001 - CLI verifies this adapter-specific credential path.
    cookie_path_ok = False
    cookie_error = ""
    if cookie_present:
        try:
            # Retry transient network failures so a flaky connection or short-lived Eastmoney
            # throttle does not get misreported as an invalid/expired cookie.
            rows = adapter._invoke_with_transient_retry(  # noqa: SLF001
                lambda: adapter._eastmoney_money_flow_records(request)  # noqa: SLF001
            )
            cookie_path_ok = bool(rows)
        except Exception as exc:  # noqa: BLE001
            cookie_error = f"{type(exc).__name__}: {exc}"

    print("true" if cookie_path_ok else "false")

    if cookie_path_ok:
        print("eastmoney-cookie: OK - cookie-gated daykline endpoint returned data.", file=sys.stderr)
    elif not cookie_present:
        print(
            "eastmoney-cookie: no EASTMONEY_COOKIE configured; set it in .env to enable cookie-gated Eastmoney endpoints.",
            file=sys.stderr,
        )
    else:
        print(
            f"eastmoney-cookie: cookie-gated daykline path failed ({cookie_error or 'no rows returned'}).",
            file=sys.stderr,
        )
        # Disambiguate "cookie expired/invalid" from "transient network / Eastmoney throttling"
        # by probing the AKShare-native money-flow path, which does not need the cookie.
        native_ok = False
        try:
            native = adapter.fetch_money_flow(request)
            native_ok = native.status == AdapterFetchStatus.success and native.rows_fetched > 0
        except Exception:  # noqa: BLE001
            native_ok = False
        if native_ok:
            print(
                "eastmoney-cookie: note - AKShare-native money-flow path still returned data, so "
                "`fetch money-flow` may succeed anyway. The cookie may be expired OR Eastmoney just throttled this probe.",
                file=sys.stderr,
            )
        else:
            print(
                "eastmoney-cookie: note - native money-flow path also returned no data; refresh EASTMONEY_COOKIE and/or retry later.",
                file=sys.stderr,
            )

    if not cookie_path_ok:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock_data_ingestion", description="A-share structured data ingestion CLI")
    parser.add_argument("--config-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db", help="Initialize SQLite database and tables")
    init_db.set_defaults(func=cmd_init_db)

    fetch = sub.add_parser("fetch", help="Fetch data")
    fetch_sub = fetch.add_subparsers(dest="fetch_command", required=True)

    sm = fetch_sub.add_parser("security-master")
    _add_provider_args(sm)
    sm.add_argument("--tickers", nargs="*", default=[])
    sm.set_defaults(func=cmd_fetch_security_master)

    cal = fetch_sub.add_parser("trade-calendar")
    _add_provider_args(cal)
    cal.add_argument("--exchange", required=False, help="Single exchange, e.g. SSE. Kept for backward compatibility.")
    cal.add_argument("--exchanges", nargs="+", default=None, help="One or more exchanges, e.g. SSE SZSE BSE.")
    cal.add_argument("--start-date", required=True)
    cal.add_argument("--end-date", required=True)
    cal.set_defaults(func=cmd_fetch_trade_calendar)

    bars = fetch_sub.add_parser("historical-bars")
    _add_provider_args(bars)
    bars.add_argument("--tickers", nargs="+", required=True)
    bars.add_argument("--start-date", required=False, help="Defaults to configured market_data_lookback_days before --end-date/today.")
    bars.add_argument("--end-date", required=False, help="Defaults to today.")
    bars.add_argument("--frequency", choices=["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"], default="1d")
    bars.add_argument("--adjust", choices=["none", "raw", "qfq", "hfq"], default="none", help="Adjustment mode. 'raw' is accepted as an alias of 'none'.")
    bars.add_argument("--cross-validate", action="store_true")
    bars.set_defaults(func=cmd_fetch_historical_bars)

    val = fetch_sub.add_parser("valuation", aliases=["valuation-metric"])
    _add_provider_args(val)
    val.add_argument("--tickers", nargs="+", required=True)
    val.add_argument("--start-date", required=False, help="Defaults to configured market_data_lookback_days before --end-date/today.")
    val.add_argument("--end-date", required=False, help="Defaults to today.")
    val.set_defaults(func=cmd_fetch_valuation)

    adj = fetch_sub.add_parser("adj-factor")
    _add_provider_args(adj)
    adj.add_argument("--tickers", nargs="+", required=True)
    adj.add_argument("--start-date", required=False, help="Defaults to configured market_data_lookback_days before --end-date/today.")
    adj.add_argument("--end-date", required=False, help="Defaults to today.")
    adj.add_argument("--cross-validate", action="store_true")
    adj.set_defaults(func=cmd_fetch_adj_factor)

    fin = fetch_sub.add_parser("financial-indicator")
    _add_provider_args(fin)
    fin.add_argument("--tickers", nargs="+", required=True)
    fin.add_argument("--start-date", required=False, help="Defaults to a window covering configured financial_lookback_quarters.")
    fin.add_argument("--end-date", required=False, help="Defaults to today.")
    fin.set_defaults(func=cmd_fetch_financial_indicator)

    fstmt = fetch_sub.add_parser("financial-statement")
    _add_provider_args(fstmt)
    fstmt.add_argument("--tickers", nargs="+", required=True)
    fstmt.add_argument("--start-date", required=False, help="Announcement start date unless --period is supplied. Defaults to financial lookback window.")
    fstmt.add_argument("--end-date", required=False, help="Announcement end date unless --period is supplied. Defaults to today.")
    fstmt.add_argument("--statement-types", nargs="*", choices=["income", "balancesheet", "cashflow", "income_statement", "balance_sheet", "cash_flow"], default=None)
    fstmt.add_argument("--period", required=False, help="Optional report period, e.g. 20250331. When set, Tushare period is used instead of start/end.")
    fstmt.set_defaults(func=cmd_fetch_financial_statement)

    money = fetch_sub.add_parser("money-flow")
    _add_provider_args(money)
    money.add_argument("--tickers", nargs="+", required=True)
    money.add_argument("--start-date", required=False, help="Defaults to configured market_data_lookback_days before --end-date/today.")
    money.add_argument("--end-date", required=False, help="Defaults to today.")
    money.set_defaults(func=cmd_fetch_money_flow)

    status = fetch_sub.add_parser("trading-status")
    _add_provider_args(status)
    status.add_argument("--tickers", nargs="+", required=True)
    status.add_argument("--start-date", required=False)
    status.add_argument("--end-date", required=False)
    status.set_defaults(func=cmd_fetch_trading_status)

    corp = fetch_sub.add_parser("corporate-action")
    _add_provider_args(corp)
    corp.add_argument("--tickers", nargs="+", required=True)
    corp.add_argument("--start-date", required=False, help="Optional event start date. If omitted, Tushare corporate-action endpoints default to full history.")
    corp.add_argument("--end-date", required=False, help="Optional event end date. If omitted, defaults to today for sparse event endpoints.")
    corp.add_argument("--action-types", nargs="*", choices=["dividend", "share_float", "repurchase"], default=None)
    corp.add_argument("--event-date-field", choices=["ann_date", "record_date", "ex_date", "imp_ann_date", "pay_date", "div_listdate", "base_date", "end_date"], default=None)
    corp.set_defaults(func=cmd_fetch_corporate_action)

    query = sub.add_parser("query", help="Query data")
    query_sub = query.add_subparsers(dest="query_command", required=True)

    qbars = query_sub.add_parser("bars")
    qbars.add_argument("--ticker", required=True)
    qbars.add_argument("--start-date", required=True)
    qbars.add_argument("--end-date", required=True)
    qbars.add_argument("--frequency", default="1d")
    qbars.add_argument("--adjust", default="none", choices=["none", "raw", "qfq", "hfq"])
    qbars.add_argument("--trading-ready", action="store_true", help="Exclude quarantined records and require minimum data quality.")
    qbars.add_argument("--minimum-quality", type=float, default=0.80)
    qbars.set_defaults(func=cmd_query_bars)

    qconf = query_sub.add_parser("conflicts")
    qconf.add_argument("--ticker", required=False)
    qconf.set_defaults(func=cmd_query_conflicts)

    qmeta = query_sub.add_parser("meta-summary")
    qmeta.add_argument("--ticker", required=False, help="Optional ticker used for company count and daily-bar range checks.")
    qmeta.add_argument("--test-root", required=False, help="Optional root directory used for total disk usage. Defaults to sqlite parent.")
    qmeta.set_defaults(func=cmd_query_meta_summary)

    verify = sub.add_parser("verify", help="Verify artifacts")
    verify_sub = verify.add_subparsers(dest="verify_command", required=True)
    raw = verify_sub.add_parser("raw")
    raw.add_argument("--raw-payload-id", required=True)
    raw.add_argument("--expected-hash")
    raw.set_defaults(func=cmd_verify_raw)

    em_cookie = verify_sub.add_parser("eastmoney-cookie", help="Check whether EASTMONEY_COOKIE can access Eastmoney money-flow data")
    em_cookie.add_argument("--ticker", default="600519.SH")
    em_cookie.add_argument("--start-date", default="2026-06-01")
    em_cookie.add_argument("--end-date", default="2026-06-05")
    em_cookie.set_defaults(func=cmd_verify_eastmoney_cookie)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    # CLI entry point: load all .env variables before command handlers construct
    # config, database, collectors, or provider adapters.
    ensure_env_loaded(config_dir=getattr(args, "config_dir", None))
    args.func(args)


if __name__ == "__main__":
    main()
