from __future__ import annotations

from datetime import date
from uuid import uuid4

from stock_data_ingestion.config import parse_provider_list
from stock_data_ingestion.schemas.requests import Adjust, Frequency, RequestType, StockDataRequest
from stock_data_ingestion.schemas.responses import StockDataResponse
from stock_data_ingestion.services.ingestion_runner import IngestionRunner


def _request_id() -> str:
    return f"req_{uuid4().hex[:16]}"


class StockDataCollector:
    def __init__(self, runner: IngestionRunner) -> None:
        self.runner = runner

    def _provider_request_kwargs(
        self,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> dict[str, object]:
        """Return provider-related request fields from explicit args or config.

        This makes provider selection work consistently for CLI usage and for
        direct Python usage through StockDataCollector. IngestionRunner also
        enforces the final config allow-list, so manually constructed requests
        cannot accidentally use a disabled provider.
        """
        configured = self.runner.config.data_sources.effective_provider_priority()
        provider_priority = parse_provider_list(providers) if providers else configured
        if not provider_priority:
            raise ValueError("INVALID_PROVIDER_CONFIG: at least one provider must be selected")
        canonical = parse_provider_list([canonical_provider])[0] if canonical_provider else self.runner.config.data_sources.effective_canonical_provider()
        if canonical not in provider_priority:
            canonical = provider_priority[0]
        return {
            "provider_priority": provider_priority,
            "canonical_provider": canonical,
        }

    def fetch_security_master(
        self,
        tickers: list[str] | None = None,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.security_master,
            tickers=tickers or [],
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_trade_calendar(
        self,
        exchange: str,
        start_date: str | date,
        end_date: str | date,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.trade_calendar,
            exchanges=[exchange],
            start_date=start_date,
            end_date=end_date,
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_historical_bars(
        self,
        tickers: list[str],
        start_date: str | date,
        end_date: str | date,
        frequency: Frequency | str = Frequency.d1,
        adjust: Adjust | str = Adjust.none,
        cross_validate: bool = True,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        provider_kwargs = self._provider_request_kwargs(providers, canonical_provider)
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.historical_bars,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjust=adjust,
            fields=["open", "high", "low", "close", "volume", "amount"],
            cross_validate=cross_validate and len(provider_kwargs["provider_priority"]) > 1,
            **provider_kwargs,
        )
        return self.runner.run(req)

    def fetch_valuation(
        self,
        tickers: list[str],
        start_date: str | date,
        end_date: str | date,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.valuation_metric,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_financial_indicator(
        self,
        tickers: list[str],
        start_date: str | date,
        end_date: str | date,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.financial_indicator,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_financial_statement(
        self,
        tickers: list[str],
        start_date: str | date,
        end_date: str | date,
        statement_types: list[str] | None = None,
        period: str | None = None,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        extra_params: dict[str, object] = {}
        if statement_types:
            extra_params["statement_types"] = statement_types
        if period:
            extra_params["period"] = period
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.financial_statement,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            extra_params=extra_params,
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_money_flow(
        self,
        tickers: list[str],
        start_date: str | date,
        end_date: str | date,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.money_flow,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_trading_status(
        self,
        tickers: list[str],
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.trading_status,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_corporate_action(
        self,
        tickers: list[str],
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        action_types: list[str] | None = None,
        event_date_field: str | None = None,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        extra_params: dict[str, object] = {}
        if action_types:
            extra_params["action_types"] = action_types
        if event_date_field:
            extra_params["event_date_field"] = event_date_field
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.corporate_action,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            extra_params=extra_params,
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

