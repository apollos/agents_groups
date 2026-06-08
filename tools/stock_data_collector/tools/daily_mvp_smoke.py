#!/usr/bin/env python3
"""Dry-run smoke check for the first-phase daily post-market ingestion design.

This tool does not call vendor APIs and does not write stock data. It validates
configuration, shows the provider order for each first-phase request type, and
prints the rolling lookback windows that the daily pipeline should use.

Typical usage:

    python tools/daily_mvp_smoke.py
    python tools/daily_mvp_smoke.py --config-dir config --as-of 2026-06-08
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stock_data_ingestion.config import load_config

PHASE_ONE_REQUEST_TYPES = [
    "security_master",
    "trade_calendar",
    "historical_bars",
    "adj_factor",
    "valuation_metric",
    "financial_statement",
    "financial_indicator",
    "money_flow",
    "corporate_action",
    "index_data",
]

HK_REQUEST_TYPES = [
    "hk_connect_membership",
    "hk_daily_bar",
]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run check for first-phase daily MVP configuration.")
    parser.add_argument("--config-dir", default="config", help="Directory containing data_sources.yaml/storage.yaml/data_quality.yaml")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="Reference date, YYYY-MM-DD")
    parser.add_argument(
        "--use-env-overrides",
        action="store_true",
        help="Honor STOCK_DATA_* provider-selection environment variables. By default this smoke check validates YAML only.",
    )
    return parser.parse_args(argv)


def clear_provider_selection_env() -> None:
    """Avoid stale local .env provider overrides during design-level smoke checks."""
    prefixes = ("STOCK_DATA_PROVIDERS", "STOCK_DATA_PROVIDER_PRIORITY")
    exact = {
        "STOCK_DATA_CANONICAL_PROVIDER",
        "STOCK_DATA_DISABLED_PROVIDERS",
        "STOCK_DATA_ENABLE_TUSHARE",
        "STOCK_DATA_ENABLE_AKSHARE",
        "STOCK_DATA_ENABLE_BAOSTOCK",
        "STOCK_DATA_ENABLE_JOINQUANT",
    }
    os.environ["STOCK_DATA_DISABLE_ENV_AUTOLOAD"] = "true"
    for key in list(os.environ):
        if key in exact or key.startswith(prefixes):
            os.environ.pop(key, None)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    as_of = date.fromisoformat(args.as_of)
    config_dir = Path(args.config_dir)
    if not args.use_env_overrides:
        clear_provider_selection_env()
    cfg = load_config(config_dir)
    data_sources = cfg.data_sources

    market_start = as_of - timedelta(days=int(data_sources.market_data_lookback_days))
    summary: dict[str, Any] = {
        "config_dir": str(config_dir.resolve()),
        "as_of": as_of.isoformat(),
        "canonical_provider": data_sources.effective_canonical_provider(),
        "active_provider_priority": data_sources.effective_provider_priority(),
        "validator_providers": data_sources.validator_providers,
        "supplement_providers": data_sources.supplement_providers,
        "market_data_lookback": {
            "days": data_sources.market_data_lookback_days,
            "start_date": market_start.isoformat(),
            "end_date": as_of.isoformat(),
        },
        "financial_lookback": {
            "quarters": data_sources.financial_lookback_quarters,
            "note": "Quarter windows are expanded by the adapter/runner at execution time.",
        },
        "default_daily_update_time": data_sources.default_daily_update_time,
        "request_provider_order": {
            request_type: data_sources.providers_for_request(request_type) for request_type in PHASE_ONE_REQUEST_TYPES
        },
        "hk_stock_connect_scope": {
            "membership": "stock_hsgt or equivalent provider support is required.",
            "daily_bars": "Tushare hk_daily or another HK-capable provider is required. BaoStock is A-share-only for this scope.",
            "request_types": HK_REQUEST_TYPES,
        },
        "provider_flags": {
            provider: {
                "enabled": data_sources.provider_is_enabled(provider),
                "role": data_sources.providers[provider].role,
                "allow_cross_validation": data_sources.providers[provider].allow_cross_validation,
                "allow_field_level_supplement": data_sources.providers[provider].allow_field_level_supplement,
            }
            for provider in data_sources.providers
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    problems: list[str] = []
    if data_sources.effective_canonical_provider() != "tushare":
        problems.append("canonical_provider should be tushare for the approved phase-one design")
    for required in ["tushare", "akshare", "baostock"]:
        if not data_sources.provider_is_enabled(required):
            problems.append(f"required provider {required!r} is not enabled")
    if data_sources.provider_is_enabled("joinquant"):
        problems.append("joinquant should be disabled by default")
    if data_sources.market_data_lookback_days < 365:
        problems.append("market_data_lookback_days should be around 400")
    if data_sources.financial_lookback_quarters < 8:
        problems.append("financial_lookback_quarters should be at least 8")
    if "baostock" not in data_sources.providers_for_request("historical_bars"):
        problems.append("baostock should be configured as a historical_bars supplement/validator")
    if "baostock" in data_sources.providers_for_request("money_flow"):
        problems.append("baostock should not be configured for money_flow because its documented Python API lacks this endpoint")

    if problems:
        print("\nFAILED CONFIG CHECKS:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("\nDaily MVP config smoke check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
