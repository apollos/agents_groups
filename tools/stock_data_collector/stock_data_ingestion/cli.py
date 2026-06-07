from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from stock_data_ingestion.config import load_config
from stock_data_ingestion.env import ensure_env_loaded
from stock_data_ingestion.logging_config import setup_logging
from stock_data_ingestion.schemas.requests import Adjust, Frequency
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
    print(json.dumps({"status": status, "exchanges": exchanges, "responses": payloads}, ensure_ascii=False, indent=2, default=_json_default))


def cmd_fetch_historical_bars(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    adjust = Adjust(_normalize_adjust_alias(args.adjust))
    resp = collector.fetch_historical_bars(args.tickers, args.start_date, args.end_date, Frequency(args.frequency), adjust, cross_validate=args.cross_validate, **_provider_args(args))
    print(resp.model_dump_json(indent=2))


def cmd_fetch_valuation(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_valuation(args.tickers, args.start_date, args.end_date, **_provider_args(args))
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
        df = QueryService(session).get_bars(args.ticker, args.start_date, args.end_date, args.frequency, _normalize_adjust_alias(args.adjust))
    print(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def cmd_query_conflicts(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    db = _build_database(config)
    from stock_data_ingestion.services.query_service import QueryService

    with db.session() as session:
        df = QueryService(session).get_conflicts(args.ticker)
    print(df.to_json(orient="records", force_ascii=False, date_format="iso"))


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
    bars.add_argument("--start-date", required=True)
    bars.add_argument("--end-date", required=True)
    bars.add_argument("--frequency", choices=["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"], default="1d")
    bars.add_argument("--adjust", choices=["none", "raw", "qfq", "hfq"], default="none", help="Adjustment mode. 'raw' is accepted as an alias of 'none'.")
    bars.add_argument("--cross-validate", action="store_true")
    bars.set_defaults(func=cmd_fetch_historical_bars)

    val = fetch_sub.add_parser("valuation", aliases=["valuation-metric"])
    _add_provider_args(val)
    val.add_argument("--tickers", nargs="+", required=True)
    val.add_argument("--start-date", required=True)
    val.add_argument("--end-date", required=True)
    val.set_defaults(func=cmd_fetch_valuation)

    fin = fetch_sub.add_parser("financial-indicator")
    _add_provider_args(fin)
    fin.add_argument("--tickers", nargs="+", required=True)
    fin.add_argument("--start-date", required=True)
    fin.add_argument("--end-date", required=True)
    fin.set_defaults(func=cmd_fetch_financial_indicator)

    fstmt = fetch_sub.add_parser("financial-statement")
    _add_provider_args(fstmt)
    fstmt.add_argument("--tickers", nargs="+", required=True)
    fstmt.add_argument("--start-date", required=True, help="Announcement start date unless --period is supplied.")
    fstmt.add_argument("--end-date", required=True, help="Announcement end date unless --period is supplied.")
    fstmt.add_argument("--statement-types", nargs="*", choices=["income", "balancesheet", "cashflow", "income_statement", "balance_sheet", "cash_flow"], default=None)
    fstmt.add_argument("--period", required=False, help="Optional report period, e.g. 20250331. When set, Tushare period is used instead of start/end.")
    fstmt.set_defaults(func=cmd_fetch_financial_statement)

    money = fetch_sub.add_parser("money-flow")
    _add_provider_args(money)
    money.add_argument("--tickers", nargs="+", required=True)
    money.add_argument("--start-date", required=True)
    money.add_argument("--end-date", required=True)
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
    qbars.set_defaults(func=cmd_query_bars)

    qconf = query_sub.add_parser("conflicts")
    qconf.add_argument("--ticker", required=False)
    qconf.set_defaults(func=cmd_query_conflicts)

    verify = sub.add_parser("verify", help="Verify artifacts")
    verify_sub = verify.add_subparsers(dest="verify_command", required=True)
    raw = verify_sub.add_parser("raw")
    raw.add_argument("--raw-payload-id", required=True)
    raw.add_argument("--expected-hash")
    raw.set_defaults(func=cmd_verify_raw)
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
