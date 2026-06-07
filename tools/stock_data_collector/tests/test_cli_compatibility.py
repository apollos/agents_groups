from __future__ import annotations

from stock_data_ingestion.cli import _normalize_adjust_alias, build_parser


def test_historical_bars_accepts_raw_adjust_alias() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "fetch",
            "historical-bars",
            "--tickers",
            "600519.SH",
            "000001.SZ",
            "--start-date",
            "20250101",
            "--end-date",
            "20250630",
            "--frequency",
            "1d",
            "--adjust",
            "raw",
        ]
    )

    assert args.adjust == "raw"
    assert _normalize_adjust_alias(args.adjust) == "none"


def test_trade_calendar_accepts_plural_exchanges() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "fetch",
            "trade-calendar",
            "--start-date",
            "20250101",
            "--end-date",
            "20250630",
            "--exchanges",
            "SSE",
            "SZSE",
            "BSE",
        ]
    )

    assert args.exchange is None
    assert args.exchanges == ["SSE", "SZSE", "BSE"]


def test_trade_calendar_keeps_single_exchange_option() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "fetch",
            "trade-calendar",
            "--exchange",
            "SSE",
            "--start-date",
            "20250101",
            "--end-date",
            "20250630",
        ]
    )

    assert args.exchange == "SSE"
    assert args.exchanges is None


def test_valuation_metric_alias_is_accepted() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "fetch",
            "valuation-metric",
            "--tickers",
            "600519.SH",
            "--start-date",
            "20250101",
            "--end-date",
            "20250630",
        ]
    )

    assert args.fetch_command == "valuation-metric"
