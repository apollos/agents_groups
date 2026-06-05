from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from stock_data_ingestion.config import load_config
from stock_data_ingestion.logging_config import setup_logging
from stock_data_ingestion.schemas.requests import Adjust, Frequency
from stock_data_ingestion.services.collector import StockDataCollector
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.storage.raw_object_store import RawObjectStore


def _json_default(obj: Any) -> str:
    return str(obj)


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


def cmd_init_db(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    from stock_data_ingestion.storage.migrations import init_db

    init_db(config.storage.sqlite_path, enable_wal=config.storage.enable_wal)
    print(json.dumps({"status": "success", "sqlite_path": str(config.storage.sqlite_path), "wal": config.storage.enable_wal}, ensure_ascii=False))


def cmd_fetch_security_master(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_security_master(args.tickers)
    print(resp.model_dump_json(indent=2))


def cmd_fetch_trade_calendar(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_trade_calendar(args.exchange, args.start_date, args.end_date)
    print(resp.model_dump_json(indent=2))


def cmd_fetch_historical_bars(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_historical_bars(args.tickers, args.start_date, args.end_date, Frequency(args.frequency), Adjust(args.adjust), cross_validate=args.cross_validate)
    print(resp.model_dump_json(indent=2))


def cmd_fetch_valuation(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_valuation(args.tickers, args.start_date, args.end_date)
    print(resp.model_dump_json(indent=2))


def cmd_fetch_financial_indicator(args: argparse.Namespace) -> None:
    collector = _build_collector(args.config_dir)
    resp = collector.fetch_financial_indicator(args.tickers, args.start_date, args.end_date)
    print(resp.model_dump_json(indent=2))


def cmd_query_bars(args: argparse.Namespace) -> None:
    config = load_config(args.config_dir)
    db = _build_database(config)
    from stock_data_ingestion.services.query_service import QueryService

    with db.session() as session:
        df = QueryService(session).get_bars(args.ticker, args.start_date, args.end_date, args.frequency, args.adjust)
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
    sm.add_argument("--tickers", nargs="*", default=[])
    sm.set_defaults(func=cmd_fetch_security_master)

    cal = fetch_sub.add_parser("trade-calendar")
    cal.add_argument("--exchange", required=True)
    cal.add_argument("--start-date", required=True)
    cal.add_argument("--end-date", required=True)
    cal.set_defaults(func=cmd_fetch_trade_calendar)

    bars = fetch_sub.add_parser("historical-bars")
    bars.add_argument("--tickers", nargs="+", required=True)
    bars.add_argument("--start-date", required=True)
    bars.add_argument("--end-date", required=True)
    bars.add_argument("--frequency", choices=["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo"], default="1d")
    bars.add_argument("--adjust", choices=["none", "qfq", "hfq"], default="none")
    bars.add_argument("--cross-validate", action="store_true")
    bars.set_defaults(func=cmd_fetch_historical_bars)

    val = fetch_sub.add_parser("valuation")
    val.add_argument("--tickers", nargs="+", required=True)
    val.add_argument("--start-date", required=True)
    val.add_argument("--end-date", required=True)
    val.set_defaults(func=cmd_fetch_valuation)

    fin = fetch_sub.add_parser("financial-indicator")
    fin.add_argument("--tickers", nargs="+", required=True)
    fin.add_argument("--start-date", required=True)
    fin.add_argument("--end-date", required=True)
    fin.set_defaults(func=cmd_fetch_financial_indicator)

    query = sub.add_parser("query", help="Query data")
    query_sub = query.add_subparsers(dest="query_command", required=True)

    qbars = query_sub.add_parser("bars")
    qbars.add_argument("--ticker", required=True)
    qbars.add_argument("--start-date", required=True)
    qbars.add_argument("--end-date", required=True)
    qbars.add_argument("--frequency", default="1d")
    qbars.add_argument("--adjust", default="none")
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
    args.func(args)


if __name__ == "__main__":
    main()
