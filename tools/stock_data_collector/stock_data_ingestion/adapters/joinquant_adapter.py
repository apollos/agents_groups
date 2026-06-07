from __future__ import annotations

import importlib.util
import os
from datetime import date, datetime
from typing import Any

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.normalization.ticker import normalize_ticker, to_joinquant_symbol
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest

try:  # Compatible with projects that have applied the .env autoload patch.
    from stock_data_ingestion.env import ensure_env_loaded
except Exception:  # pragma: no cover - older project versions may not have env.py.
    ensure_env_loaded = None  # type: ignore[assignment]


class JoinQuantAdapter(BaseDataAdapter):
    provider_name = "joinquant"
    source_site = "joinquant"
    adapter_version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        # Newer BaseDataAdapter versions already call ensure_env_loaded(). This
        # defensive call keeps direct JoinQuantAdapter() usage safe on mixed patch levels.
        if ensure_env_loaded is not None:
            ensure_env_loaded()
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

    @staticmethod
    def _is_missing(value: Any) -> bool:
        if value is None:
            return True
        text = str(value).strip()
        if text.lower() in {"", "nat", "nan", "none", "null"}:
            return True
        try:
            return bool(value != value)  # Covers float('nan') without importing numpy/pandas.
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    def _serialize_provider_value(cls, value: Any) -> Any:
        if cls._is_missing(value):
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:  # noqa: BLE001
                return str(value)
        return value

    @classmethod
    def _date_to_iso(cls, value: Any) -> str | None:
        serialized = cls._serialize_provider_value(value)
        if cls._is_missing(serialized):
            return None
        return str(serialized).strip()[:10]

    @classmethod
    def _is_open_ended_end_date(cls, value: Any) -> bool:
        iso_value = cls._date_to_iso(value)
        if iso_value is None:
            return True
        # JQData commonly uses a far-future end_date for active securities.
        return iso_value >= "2099-12-31" or iso_value.startswith(("2200", "2999", "9999"))

    @staticmethod
    def _exchange_from_normalized_ticker(ticker: str) -> str:
        return ticker.split(".", 1)[1]

    def fetch_security_master(self, request: StockDataRequest) -> ProviderFetchResult:
        """Fetch A-share security master records from JQData.

        JQData exposes stock master data as a market-wide table through
        ``get_all_securities(types=["stock"])``. It is not a per-ticker query
        endpoint, so this adapter fetches the stock table once and filters locally
        when ``request.tickers`` is supplied.
        """
        source_api = "get_all_securities"
        started = now_asia_shanghai()
        if not self.username or not self.password:
            return self._unavailable_result(source_api, ErrorCode.AUTH_FAILED, "JQDATA_USERNAME/JQDATA_PASSWORD are missing")
        if importlib.util.find_spec("jqdatasdk") is None:
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "jqdatasdk is not installed")

        try:
            import jqdatasdk as jq  # type: ignore

            if not self.authenticate():
                return self._unavailable_result(source_api, ErrorCode.AUTH_FAILED, "JQData authentication failed")
            if not hasattr(jq, "get_all_securities"):
                return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "jqdatasdk.get_all_securities is not available")

            df = jq.get_all_securities(types=["stock"])
            if df is None:
                return self._success_result(source_api, [], started)

            df = df.copy()
            if "provider_symbol" not in df.columns:
                if "code" in df.columns:
                    provider_symbols = [str(value).strip() for value in df["code"].tolist()]
                else:
                    provider_symbols = [str(value).strip() for value in df.index.tolist()]
                df.insert(0, "provider_symbol", provider_symbols)

            allowed = set(request.tickers or [])
            records: list[dict[str, Any]] = []
            for raw_row in df.to_dict(orient="records"):
                row = {key: self._serialize_provider_value(value) for key, value in raw_row.items()}
                provider_symbol = str(row.get("provider_symbol") or row.get("code") or row.get("symbol") or "").strip()
                if not provider_symbol:
                    continue

                try:
                    normalized_ticker = normalize_ticker(provider_symbol)
                except Exception:  # noqa: BLE001
                    # A single legacy/non-standard provider symbol must not fail
                    # the whole JoinQuant provider fetch.
                    continue

                if allowed and normalized_ticker not in allowed:
                    continue

                start_date = self._date_to_iso(row.get("start_date") or row.get("list_date"))
                raw_end_date = row.get("end_date") or row.get("delist_date")
                delist_date = None if self._is_open_ended_end_date(raw_end_date) else self._date_to_iso(raw_end_date)
                list_status = row.get("list_status") or ("D" if delist_date else "L")

                display_name = row.get("display_name") or row.get("name")
                original_name = row.get("name")

                normalized_row: dict[str, Any] = dict(row)
                normalized_row.update(
                    {
                        "provider_symbol": provider_symbol,
                        "normalized_ticker": normalized_ticker,
                        "symbol": provider_symbol.split(".", 1)[0],
                        "exchange": self._exchange_from_normalized_ticker(normalized_ticker),
                        "name": display_name,
                        "list_date": start_date,
                        "delist_date": delist_date,
                        "list_status": list_status,
                        "market": row.get("market") or "A_share",
                        "asset_type": row.get("asset_type") or row.get("type") or "stock",
                        "currency": row.get("currency") or "CNY",
                    }
                )
                if original_name and display_name and original_name != display_name:
                    normalized_row["jq_original_name"] = original_name

                records.append(normalized_row)

            return self._success_result(source_api, records, started)
        except KeyError as exc:
            return self._error_result(source_api, started, exc, ErrorCode.PROVIDER_SCHEMA_CHANGED, retryable=False)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

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
