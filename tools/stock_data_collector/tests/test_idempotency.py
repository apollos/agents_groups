from __future__ import annotations

from stock_data_ingestion.utils.idempotency import generate_idempotency_key


def test_idempotency_key_is_order_insensitive_for_tickers_and_fields():
    a = generate_idempotency_key(
        module_name="stock_data_ingestion",
        request_type="historical_bars",
        provider="tushare",
        tickers=["600519.SH", "000001.SZ"],
        start_date="2024-01-01",
        end_date="2026-05-29",
        frequency="1d",
        adjust="qfq",
        fields=["open", "high", "low", "close", "volume", "amount"],
        schema_version="v0.1",
    )
    b = generate_idempotency_key(
        module_name="stock_data_ingestion",
        request_type="historical_bars",
        provider="tushare",
        tickers=["000001.SZ", "600519.SH"],
        start_date="20240101",
        end_date="20260529",
        frequency="1d",
        adjust="qfq",
        fields=["amount", "volume", "close", "low", "high", "open"],
        schema_version="v0.1",
    )
    assert a == b
    assert a == "stock_data_ingestion:historical_bars:tushare:000001.SZ,600519.SH:20240101:20260529:1d:qfq:amount,close,high,low,open,volume:v0.1"


def test_idempotency_key_includes_provider_set_when_supplied():
    base = generate_idempotency_key(
        module_name="stock_data_ingestion",
        request_type="historical_bars",
        provider="tushare",
        tickers=["600519.SH"],
        start_date="2026-05-01",
        end_date="2026-05-29",
        frequency="1d",
        adjust="none",
        fields=["open", "close"],
        schema_version="v0.1",
    )
    with_provider_set = generate_idempotency_key(
        module_name="stock_data_ingestion",
        request_type="historical_bars",
        provider="tushare",
        tickers=["600519.SH"],
        start_date="2026-05-01",
        end_date="2026-05-29",
        frequency="1d",
        adjust="none",
        fields=["open", "close"],
        provider_set=["baostock", "tushare", "akshare"],
        schema_version="v0.1",
    )
    assert base != with_provider_set
    assert with_provider_set.endswith(":akshare,baostock,tushare:v0.1")
