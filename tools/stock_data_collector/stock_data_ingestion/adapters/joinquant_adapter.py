from __future__ import annotations

import importlib.util
import os
from typing import Any

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.normalization.ticker import normalize_ticker, to_joinquant_symbol
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest


class JoinQuantAdapter(BaseDataAdapter):
    provider_name = "joinquant"
    source_site = "joinquant"
    adapter_version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        self.username = os.getenv("JQDATA_USERNAME")
        self.password = os.getenv("JQDATA_PASSWORD")

    def is_available(self) -> bool:
        return bool(self.username and self.password) and importlib.util.find_spec("jqdatasdk") is not None

    def authenticate(self) -> bool:
        if not self.username or not self.password or importlib.util.find_spec("jqdatasdk") is None:
            return False
        import jqdatasdk as jq  # type: ignore

        jq.auth(self.username, self.password)
        self._authenticated = True
        return True

    def _classify_error(self, exc: Exception) -> ErrorCode:
        msg = str(exc).lower()
        if "auth" in msg or "login" in msg or "用户名" in msg or "密码" in msg:
            return ErrorCode.AUTH_FAILED
        if "permission" in msg or "权限" in msg:
            return ErrorCode.PERMISSION_DENIED
        if "timeout" in msg:
            return ErrorCode.PROVIDER_TIMEOUT
        return ErrorCode.UNKNOWN_ERROR

    def fetch_historical_bars(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "get_price"
        started = now_asia_shanghai()
        if not self.username or not self.password:
            return self._unavailable_result(source_api, ErrorCode.AUTH_FAILED, "JQDATA_USERNAME/JQDATA_PASSWORD are missing")
        if importlib.util.find_spec("jqdatasdk") is None:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "jqdatasdk is not installed")
        try:
            import jqdatasdk as jq  # type: ignore

            self.authenticate()
            records: list[dict[str, Any]] = []
            frequency = {"1d": "daily", "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m"}.get(str(request.frequency or "1d"), "daily")
            for ticker in request.tickers:
                symbol = to_joinquant_symbol(ticker)
                df = jq.get_price(
                    symbol,
                    start_date=request.start_date.isoformat() if request.start_date else None,
                    end_date=request.end_date.isoformat() if request.end_date else None,
                    frequency=frequency,
                    fields=["open", "close", "high", "low", "volume", "money"],
                    fq=None if request.adjust == "none" else request.adjust,
                )
                if df is not None:
                    df = df.reset_index().rename(columns={"index": "time"})
                    for row in df.to_dict(orient="records"):
                        row["provider_symbol"] = symbol
                        row["normalized_ticker"] = normalize_ticker(ticker)
                        records.append(row)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_financial_indicator(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "finance.run_query"
        started = now_asia_shanghai()
        if not self.is_available():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "jqdatasdk unavailable or credentials missing")
        try:
            # A conservative placeholder keeps all JQData access inside this adapter; production users can expand fields.
            return self._success_result(source_api, [], started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def normalize_raw_data(self, result: ProviderFetchResult, request: StockDataRequest) -> list[Any]:
        return result.raw_records

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return normalize_ticker(symbol)

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        return to_joinquant_symbol(ticker)
