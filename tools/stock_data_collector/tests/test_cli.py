from __future__ import annotations

from stock_data_ingestion.cli import build_parser


def test_cli_parses_required_examples():
    parser = build_parser()
    args = parser.parse_args(["fetch", "historical-bars", "--tickers", "600519.SH", "000001.SZ", "--start-date", "2024-01-01", "--end-date", "2026-05-29", "--frequency", "1d", "--adjust", "qfq", "--cross-validate"])
    assert args.command == "fetch"
    assert args.fetch_command == "historical-bars"
    assert args.tickers == ["600519.SH", "000001.SZ"]
    args = parser.parse_args(["query", "conflicts", "--ticker", "600519.SH"])
    assert args.query_command == "conflicts"


def test_cli_parses_tushare_extended_fetch_commands():
    parser = build_parser()
    args = parser.parse_args([
        "fetch",
        "financial-statement",
        "--tickers",
        "600519.SH",
        "--start-date",
        "2024-01-01",
        "--end-date",
        "2025-12-31",
        "--statement-types",
        "income",
        "balancesheet",
    ])
    assert args.fetch_command == "financial-statement"
    assert args.statement_types == ["income", "balancesheet"]

    args = parser.parse_args([
        "fetch",
        "money-flow",
        "--tickers",
        "600519.SH",
        "--start-date",
        "2025-01-01",
        "--end-date",
        "2025-12-31",
    ])
    assert args.fetch_command == "money-flow"

    args = parser.parse_args([
        "fetch",
        "adj-factor",
        "--tickers",
        "600519.SH",
        "--start-date",
        "2025-01-01",
        "--end-date",
        "2025-12-31",
        "--providers",
        "tushare",
        "baostock",
        "--canonical-provider",
        "tushare",
        "--cross-validate",
    ])
    assert args.fetch_command == "adj-factor"
    assert args.tickers == ["600519.SH"]
    assert args.providers == ["tushare", "baostock"]
    assert args.canonical_provider == "tushare"
    assert args.cross_validate is True

    args = parser.parse_args([
        "fetch",
        "corporate-action",
        "--tickers",
        "600519.SH",
        "--action-types",
        "repurchase",
    ])
    assert args.action_types == ["repurchase"]


def test_cli_parses_eastmoney_cookie_verification_command():
    parser = build_parser()
    args = parser.parse_args([
        "verify",
        "eastmoney-cookie",
        "--ticker",
        "600519.SH",
        "--start-date",
        "2026-06-01",
        "--end-date",
        "2026-06-05",
    ])

    assert args.command == "verify"
    assert args.verify_command == "eastmoney-cookie"
    assert args.ticker == "600519.SH"
    assert args.start_date == "2026-06-01"
    assert args.end_date == "2026-06-05"


def test_cli_parses_meta_summary_query_command():
    parser = build_parser()
    args = parser.parse_args([
        "query",
        "meta-summary",
        "--ticker",
        "600519.SH",
        "--test-root",
        "data/smoke_600519_20260601_20260605",
    ])

    assert args.command == "query"
    assert args.query_command == "meta-summary"
    assert args.ticker == "600519.SH"
    assert args.test_root == "data/smoke_600519_20260601_20260605"
