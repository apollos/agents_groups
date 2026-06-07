from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from stock_data_ingestion.env import ensure_env_loaded
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorCode, ErrorRecord
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest


class BaseDataAdapter(ABC):
    provider_name: str
    adapter_version: str = "0.1.0"
    source_site: str

    def __init__(self) -> None:
        # Adapter constructors may be used directly by application code, bypassing
        # CLI and load_config(). Load .env here as a final credential/config fallback.
        ensure_env_loaded()
        self._authenticated = False

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def authenticate(self) -> bool:
        raise NotImplementedError

    def _unavailable_result(self, source_api: str, error_code: ErrorCode, message: str) -> ProviderFetchResult:
        now = now_asia_shanghai()
        return ProviderFetchResult(
            provider=self.provider_name,
            source_api=source_api,
            source_site=self.source_site,
            adapter_version=self.adapter_version,
            status=AdapterFetchStatus.unavailable,
            started_at=now,
            completed_at=now,
            error=ErrorRecord(
                provider=self.provider_name,
                source_api=source_api,
                source_site=self.source_site,
                error_code=error_code,
                error_message=message,
                retryable=False,
                suggested_action="Check package installation and credentials.",
            ),
        )

    def _error_result(
        self,
        source_api: str,
        started_at: datetime,
        exc: Exception,
        code: ErrorCode = ErrorCode.UNKNOWN_ERROR,
        retryable: bool = False,
    ) -> ProviderFetchResult:
        return ProviderFetchResult(
            provider=self.provider_name,
            source_api=source_api,
            source_site=self.source_site,
            adapter_version=self.adapter_version,
            status=AdapterFetchStatus.failed,
            started_at=started_at,
            completed_at=now_asia_shanghai(),
            error=ErrorRecord.from_exception(
                exc,
                provider=self.provider_name,
                source_api=source_api,
                source_site=self.source_site,
                error_code=code,
                retryable=retryable,
            ),
        )

    def _success_result(self, source_api: str, raw_records: list[dict[str, Any]], started_at: datetime) -> ProviderFetchResult:
        status = AdapterFetchStatus.success if raw_records else AdapterFetchStatus.empty_result
        error = None
        if not raw_records:
            error = ErrorRecord(
                provider=self.provider_name,
                source_api=source_api,
                source_site=self.source_site,
                error_code=ErrorCode.EMPTY_RESULT,
                error_message="provider returned no rows",
                retryable=False,
            )
        return ProviderFetchResult(
            provider=self.provider_name,
            source_api=source_api,
            source_site=self.source_site,
            adapter_version=self.adapter_version,
            status=status,
            raw_records=raw_records,
            rows_fetched=len(raw_records),
            started_at=started_at,
            completed_at=now_asia_shanghai(),
            error=error,
        )

    # Every fetch method returns ProviderFetchResult/AdapterFetchResult, never a raw DataFrame.
    def fetch_security_master(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("security_master", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_trade_calendar(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("trade_calendar", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_trading_status(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("trading_status", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_historical_bars(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("historical_bars", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_realtime_quote(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("realtime_quote", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_adj_factor(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("adj_factor", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_financial_statement(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("financial_statement", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_financial_indicator(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("financial_indicator", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_valuation_metric(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("valuation_metric", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_industry_membership(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("industry_membership", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_concept_membership(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("concept_membership", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_money_flow(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("money_flow", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_index_data(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("index_data", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    def fetch_corporate_action(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._unavailable_result("corporate_action", ErrorCode.PROVIDER_UNAVAILABLE, "not implemented")

    @abstractmethod
    def normalize_raw_data(self, result: ProviderFetchResult, request: StockDataRequest) -> list[Any]:
        raise NotImplementedError

    @abstractmethod
    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        raise NotImplementedError
