from __future__ import annotations

import importlib.util
import os
from typing import Any

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.normalization.ticker import normalize_ticker, to_tushare_symbol
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest


class TushareAdapter(BaseDataAdapter):
    provider_name = "tushare"
    source_site = "tushare"
    adapter_version = "0.1.0"

    _STOCK_BASIC_FIELDS = (
        "ts_code,symbol,name,area,industry,fullname,enname,cnspell,market,exchange,"
        "curr_type,list_status,list_date,delist_date"
    )
    _SECURITY_MASTER_LIST_STATUSES = ("L", "D", "P")

    def __init__(self) -> None:
        super().__init__()
        self.token = os.getenv("TUSHARE_TOKEN")
        self._pro: Any | None = None

    def is_available(self) -> bool:
        return bool(self.token) and importlib.util.find_spec("tushare") is not None

    def authenticate(self) -> bool:
        if not self.token:
            return False
        if importlib.util.find_spec("tushare") is None:
            return False
        import tushare as ts  # type: ignore

        ts.set_token(self.token)
        self._pro = ts.pro_api(self.token)
        self._authenticated = True
        return True

    def _classify_error(self, exc: Exception) -> ErrorCode:
        msg = str(exc).lower()
        if "permission" in msg or "权限" in msg:
            return ErrorCode.PERMISSION_DENIED
        if "rate" in msg or "limit" in msg or "频次" in msg or "超过" in msg:
            return ErrorCode.RATE_LIMITED
        if "timeout" in msg or "timed out" in msg:
            return ErrorCode.PROVIDER_TIMEOUT
        return ErrorCode.UNKNOWN_ERROR

    @staticmethod
    def _dataframe_to_records(df: Any) -> list[dict[str, Any]]:
        if df is None:
            return []
        if bool(getattr(df, "empty", False)):
            return []
        return df.to_dict(orient="records")

    def _fetch_stock_basic_rows_for_requested_tickers(
        self,
        fields: str,
        requested_tickers: set[str],
    ) -> list[dict[str, Any]]:
        """Fetch stock_basic rows by exact ``ts_code`` for targeted requests.

        Do not fetch the full L/D/P universe before filtering. Tushare's delisted
        universe contains historical non-six-digit codes such as ``TS0018.SH``.
        Those are valid raw Tushare history, but they are outside this project's
        normalized A-share ticker grammar and must not break a request for normal
        tickers such as ``600519.SH`` and ``000001.SZ``.
        """

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for ticker in sorted(requested_tickers):
            ts_code = to_tushare_symbol(ticker)

            # Usually enough for currently listed securities. This keeps the fast
            # path to one exact Tushare query per ticker.
            query_records = self._dataframe_to_records(
                self._pro.stock_basic(ts_code=ts_code, fields=fields)
            )

            # Some accounts/API versions may require list_status for delisted or
            # pre-listing securities. Keep the query narrow: exact ts_code + status,
            # never full-market list_status when the caller supplied tickers.
            if not query_records:
                for status in (*self._SECURITY_MASTER_LIST_STATUSES, "G"):
                    query_records.extend(
                        self._dataframe_to_records(
                            self._pro.stock_basic(
                                ts_code=ts_code,
                                list_status=status,
                                fields=fields,
                            )
                        )
                    )

            for row in query_records:
                raw_ts_code = row.get("ts_code") or row.get("symbol") or ts_code
                try:
                    normalized = normalize_ticker(raw_ts_code)
                except Exception:  # noqa: BLE001
                    # Defensive only: an exact ts_code query should not return an
                    # unrelated/non-standard row. If it does, skip that row instead
                    # of marking the whole provider failed.
                    continue

                if normalized not in requested_tickers:
                    continue

                dedupe_key = (normalized, str(row.get("list_status", "")))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                rows.append(row)

        return rows

    def _fetch_full_security_master_rows(self, fields: str) -> list[dict[str, Any]]:
        """Fetch L/D/P security master rows without normalizing provider symbols.

        Full-universe sync must preserve raw Tushare rows. Non-standard historical
        symbols are handled later as row-level normalization errors, so one odd
        delisted code never turns into a provider-level failure.
        """

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for status in self._SECURITY_MASTER_LIST_STATUSES:
            status_rows = self._dataframe_to_records(
                self._pro.stock_basic(exchange="", list_status=status, fields=fields)
            )
            for row in status_rows:
                ts_code = str(row.get("ts_code") or "").strip()
                list_status = str(row.get("list_status") or status).strip()
                dedupe_key = (ts_code, list_status)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                rows.append(row)
        return rows

    def fetch_security_master(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "stock_basic"
        started = now_asia_shanghai()
        if not self.token:
            return self._unavailable_result(
                source_api,
                ErrorCode.TOKEN_MISSING,
                "TUSHARE_TOKEN is missing",
            )
        if not self.is_available() or not self.authenticate():
            return self._unavailable_result(
                source_api,
                ErrorCode.PROVIDER_UNAVAILABLE,
                "tushare SDK unavailable",
            )
        try:
            fields = self._STOCK_BASIC_FIELDS
            if request.tickers:
                raw_records = self._fetch_stock_basic_rows_for_requested_tickers(
                    fields,
                    set(request.tickers),
                )
            else:
                raw_records = self._fetch_full_security_master_rows(fields)
            return self._success_result(source_api, raw_records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_trade_calendar(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "trade_cal"
        started = now_asia_shanghai()
        if not self.token:
            return self._unavailable_result(source_api, ErrorCode.TOKEN_MISSING, "TUSHARE_TOKEN is missing")
        if not self.is_available() or not self.authenticate():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "tushare SDK unavailable")
        try:
            exchange = request.exchanges[0] if request.exchanges else "SSE"
            df = self._pro.trade_cal(exchange=exchange, start_date=request.start_date.strftime("%Y%m%d") if request.start_date else None, end_date=request.end_date.strftime("%Y%m%d") if request.end_date else None)
            return self._success_result(source_api, df.to_dict(orient="records") if df is not None else [], started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_historical_bars(self, request: StockDataRequest) -> ProviderFetchResult:
        frequency = request.frequency or "1d"
        source_api = {"1d": "daily", "1w": "weekly", "1mo": "monthly"}.get(str(frequency), "pro_bar")
        started = now_asia_shanghai()
        if not self.token:
            return self._unavailable_result(source_api, ErrorCode.TOKEN_MISSING, "TUSHARE_TOKEN is missing")
        if not self.is_available() or not self.authenticate():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "tushare SDK unavailable")
        try:
            records: list[dict[str, Any]] = []
            import tushare as ts  # type: ignore

            for ticker in request.tickers:
                ts_code = to_tushare_symbol(ticker)
                start = request.start_date.strftime("%Y%m%d") if request.start_date else None
                end = request.end_date.strftime("%Y%m%d") if request.end_date else None
                if source_api in {"daily", "weekly", "monthly"} and str(request.adjust or "none") == "none":
                    df = getattr(self._pro, source_api)(ts_code=ts_code, start_date=start, end_date=end)
                else:
                    freq = {"1d": "D", "1w": "W", "1mo": "M", "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "60m": "60min"}.get(str(frequency), "D")
                    df = ts.pro_bar(ts_code=ts_code, start_date=start, end_date=end, freq=freq, adj=None if request.adjust == "none" else request.adjust)
                if df is not None:
                    for row in df.to_dict(orient="records"):
                        row["provider_symbol"] = ts_code
                        records.append(row)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_adj_factor(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "adj_factor"
        started = now_asia_shanghai()
        if not self.token:
            return self._unavailable_result(source_api, ErrorCode.TOKEN_MISSING, "TUSHARE_TOKEN is missing")
        if not self.is_available() or not self.authenticate():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "tushare SDK unavailable")
        try:
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                df = self._pro.adj_factor(ts_code=to_tushare_symbol(ticker), start_date=request.start_date.strftime("%Y%m%d") if request.start_date else None, end_date=request.end_date.strftime("%Y%m%d") if request.end_date else None)
                if df is not None:
                    records.extend(df.to_dict(orient="records"))
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_financial_indicator(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "fina_indicator"
        started = now_asia_shanghai()
        if not self.token:
            return self._unavailable_result(source_api, ErrorCode.TOKEN_MISSING, "TUSHARE_TOKEN is missing")
        if not self.is_available() or not self.authenticate():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "tushare SDK unavailable")
        try:
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                df = self._pro.fina_indicator(ts_code=to_tushare_symbol(ticker), start_date=request.start_date.strftime("%Y%m%d") if request.start_date else None, end_date=request.end_date.strftime("%Y%m%d") if request.end_date else None)
                if df is not None:
                    records.extend(df.to_dict(orient="records"))
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_valuation_metric(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "daily_basic"
        started = now_asia_shanghai()
        if not self.token:
            return self._unavailable_result(source_api, ErrorCode.TOKEN_MISSING, "TUSHARE_TOKEN is missing")
        if not self.is_available() or not self.authenticate():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "tushare SDK unavailable")
        try:
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                df = self._pro.daily_basic(ts_code=to_tushare_symbol(ticker), start_date=request.start_date.strftime("%Y%m%d") if request.start_date else None, end_date=request.end_date.strftime("%Y%m%d") if request.end_date else None)
                if df is not None:
                    records.extend(df.to_dict(orient="records"))
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def normalize_raw_data(self, result: ProviderFetchResult, request: StockDataRequest) -> list[Any]:
        return result.raw_records

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return normalize_ticker(symbol)

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        return to_tushare_symbol(ticker)
