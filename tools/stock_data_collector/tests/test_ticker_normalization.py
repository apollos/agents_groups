from __future__ import annotations

import pytest

from stock_data_ingestion.normalization.ticker import (
    TickerNormalizationError,
    infer_exchange,
    is_a_share_ticker,
    is_hk_ticker,
    normalize_ticker,
    to_akshare_symbol,
    to_baostock_symbol,
    to_joinquant_symbol,
    to_tushare_symbol,
    validate_a_share_ticker,
    validate_hk_ticker,
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
    assert normalize_ticker("hk00005") == "00005.HK"
    assert normalize_ticker("00005.HK") == "00005.HK"
    assert normalize_ticker("00005.XHKG") == "00005.HK"


def test_provider_symbol_conversion():
    assert to_tushare_symbol("sh600519") == "600519.SH"
    assert to_tushare_symbol("hk00005") == "00005.HK"
    assert to_akshare_symbol("000001.SZ") == "sz000001"
    assert to_akshare_symbol("00005.HK") == "hk00005"
    assert to_baostock_symbol("600519.SH") == "sh.600519"
    assert to_joinquant_symbol("600519.SH") == "600519.XSHG"
    with pytest.raises(TickerNormalizationError):
        to_baostock_symbol("00005.HK")
    with pytest.raises(TickerNormalizationError):
        to_baostock_symbol("430047.BJ")


def test_infer_exchange_and_invalid_ticker():
    assert infer_exchange("688001") == "SH"
    assert infer_exchange("300001") == "SZ"
    assert infer_exchange("430047") == "BJ"
    assert validate_a_share_ticker("not-a-code") is False
    assert validate_hk_ticker("hk00005") is True
    assert validate_hk_ticker("00005.HK") is True
    with pytest.raises(TickerNormalizationError) as exc:
        normalize_ticker("123456")
    assert "INVALID_TICKER" in str(exc.value)


def test_hk_ticker_normalization_and_provider_conversions():
    assert normalize_ticker("hk00700") == "00700.HK"
    assert normalize_ticker("00700.HK") == "00700.HK"
    assert normalize_ticker("00001.XHKG") == "00001.HK"
    assert validate_hk_ticker("00700.HK") is True
    assert is_hk_ticker("hk00005") is True
    assert is_a_share_ticker("600519.SH") is True
    assert is_a_share_ticker("00700.HK") is False
    assert to_tushare_symbol("hk00700") == "00700.HK"
    assert to_akshare_symbol("00700.HK") == "hk00700"
    with pytest.raises(TickerNormalizationError):
        to_baostock_symbol("00700.HK")
    with pytest.raises(TickerNormalizationError):
        to_joinquant_symbol("00700.HK")
