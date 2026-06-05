from __future__ import annotations

import importlib.util
from typing import Any

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.normalization.ticker import normalize_ticker, to_akshare_symbol
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest


class AKShareAdapter(BaseDataAdapter):
    provider_name = "akshare"
    source_site = "eastmoney"
    adapter_version = "0.1.0"

    def is_available(self) -> bool:
        return importlib.util.find_spec("akshare") is not None

    def authenticate(self) -> bool:
        self._authenticated = self.is_available()
        return self._authenticated

    def fetch_security_master(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_info_a_code_name"
        started = now_asia_shanghai()
        if not self.is_available():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "akshare is not installed")
        try:
            import akshare as ak  # type: ignore

            df = ak.stock_info_a_code_name()
            raw_records = df.to_dict(orient="records") if df is not None else []
            if request.tickers:
                wanted_codes = {ticker.split(".")[0] for ticker in request.tickers}
                raw_records = [r for r in raw_records if str(r.get("code") or r.get("代码")) in wanted_codes]
            return self._success_result(source_api, raw_records, started)
        except KeyError as exc:
            return self._error_result(source_api, started, exc, ErrorCode.PROVIDER_SCHEMA_CHANGED, retryable=False)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_historical_bars(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_zh_a_hist"
        started = now_asia_shanghai()
        if not self.is_available():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "akshare is not installed")
        try:
            import akshare as ak  # type: ignore

            records: list[dict[str, Any]] = []
            period = {"1d": "daily", "1w": "weekly", "1mo": "monthly"}.get(str(request.frequency or "1d"), "daily")
            adjust = "" if str(request.adjust or "none") == "none" else str(request.adjust)
            for ticker in request.tickers:
                symbol = to_akshare_symbol(ticker)[2:]
                df = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period=period,
                    start_date=request.start_date.strftime("%Y%m%d") if request.start_date else "19700101",
                    end_date=request.end_date.strftime("%Y%m%d") if request.end_date else "20991231",
                    adjust=adjust,
                )
                required = {"日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"}
                if df is not None and not required.issubset(set(df.columns)):
                    raise KeyError(f"AKShare schema changed. Missing {required - set(df.columns)}")
                if df is not None:
                    for row in df.to_dict(orient="records"):
                        row["provider_symbol"] = symbol
                        row["normalized_ticker"] = normalize_ticker(ticker)
                        records.append(row)
            return self._success_result(source_api, records, started)
        except KeyError as exc:
            return self._error_result(source_api, started, exc, ErrorCode.PROVIDER_SCHEMA_CHANGED, retryable=False)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def fetch_realtime_quote(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_zh_a_spot_em"
        started = now_asia_shanghai()
        if not self.is_available():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "akshare is not installed")
        try:
            import akshare as ak  # type: ignore

            df = ak.stock_zh_a_spot_em()
            raw_records = df.to_dict(orient="records") if df is not None else []
            if request.tickers:
                codes = {ticker.split(".")[0] for ticker in request.tickers}
                raw_records = [r for r in raw_records if str(r.get("代码")) in codes]
            return self._success_result(source_api, raw_records, started)
        except KeyError as exc:
            return self._error_result(source_api, started, exc, ErrorCode.PROVIDER_SCHEMA_CHANGED, retryable=False)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, ErrorCode.UNKNOWN_ERROR, retryable=True)

    def normalize_raw_data(self, result: ProviderFetchResult, request: StockDataRequest) -> list[Any]:
        return result.raw_records

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return normalize_ticker(symbol)

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        return to_akshare_symbol(ticker)
