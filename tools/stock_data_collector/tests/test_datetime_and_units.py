from __future__ import annotations

import pytest

from stock_data_ingestion.normalization.datetime_utils import build_quote_time_bucket, infer_bar_start_end, normalize_timestamp, normalize_trade_date
from stock_data_ingestion.normalization.units import compute_vwap, normalize_amount, normalize_currency, normalize_turnover_rate, normalize_volume


def test_datetime_standardization_and_bar_window():
    day = normalize_trade_date("20260529")
    ts = normalize_timestamp("2026-05-29T10:03:07+08:00")
    start, end = infer_bar_start_end(day, "5m", ts)
    assert start.isoformat().startswith("2026-05-29T10:03:07")
    assert (end - start).total_seconds() == 300
    assert build_quote_time_bucket(ts, seconds=3).second == 6


def test_unit_standardization_more_cases():
    assert normalize_volume(1, unit="lot") == (100.0, "share")
    assert normalize_amount(1, unit="万元") == (10000.0, "CNY")
    assert normalize_currency("人民币") == "CNY"
    assert compute_vwap(1000, 100) == 10
    assert normalize_turnover_rate("1.23") == 1.23
    with pytest.raises(ValueError):
        normalize_volume(1, unit="bad_unit")
