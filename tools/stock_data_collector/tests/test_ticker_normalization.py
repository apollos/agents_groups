from __future__ import annotations

import pytest

from stock_data_ingestion.normalization.ticker import (
    TickerNormalizationError,
    infer_exchange,
    normalize_ticker,
    to_akshare_symbol,
    to_joinquant_symbol,
    to_tushare_symbol,
    validate_a_share_ticker,
)


def test_normalize_supported_formats():
    assert normalize_ticker("600519.SH") == "600519.SH"
    assert normalize_ticker("000001.SZ") == "000001.SZ"
    assert normalize_ticker("430047.BJ") == "430047.BJ"
    assert normalize_ticker("600519") == "600519.SH"
    assert normalize_ticker("sz000001") == "000001.SZ"
    assert normalize_ticker("sh600519") == "600519.SH"
    assert normalize_ticker("600519.XSHG") == "600519.SH"
    assert normalize_ticker("000001.XSHE") == "000001.SZ"


def test_provider_symbol_conversion():
    assert to_tushare_symbol("sh600519") == "600519.SH"
    assert to_akshare_symbol("000001.SZ") == "sz000001"
    assert to_joinquant_symbol("600519.SH") == "600519.XSHG"


def test_infer_exchange_and_invalid_ticker():
    assert infer_exchange("688001") == "SH"
    assert infer_exchange("300001") == "SZ"
    assert infer_exchange("430047") == "BJ"
    assert validate_a_share_ticker("not-a-code") is False
    with pytest.raises(TickerNormalizationError) as exc:
        normalize_ticker("123456")
    assert "INVALID_TICKER" in str(exc.value)
