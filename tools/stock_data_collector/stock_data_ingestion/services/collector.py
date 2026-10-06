from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from stock_data_ingestion.config import parse_provider_list
from stock_data_ingestion.schemas.market_context import MarketContextRequest, MarketContextResponse
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


    def _resolve_market_window(self, start_date: str | date | None, end_date: str | date | None) -> tuple[str | date, str | date]:
        end = end_date or date.today()
        if start_date is not None:
            return start_date, end
        if isinstance(end, str):
            end_day = date.fromisoformat(end.replace("/", "-")[:10]) if "-" in end or "/" in end else date(int(end[:4]), int(end[4:6]), int(end[6:8]))
        else:
            end_day = end
        return end_day - timedelta(days=self.runner.config.data_sources.market_data_lookback_days), end

    def _resolve_financial_window(self, start_date: str | date | None, end_date: str | date | None) -> tuple[str | date, str | date, int]:
        end = end_date or date.today()
        quarters = self.runner.config.data_sources.financial_lookback_quarters
        if start_date is not None:
            return start_date, end, quarters
        if isinstance(end, str):
            end_day = date.fromisoformat(end.replace("/", "-")[:10]) if "-" in end or "/" in end else date(int(end[:4]), int(end[4:6]), int(end[6:8]))
        else:
            end_day = end
        # Use a conservative calendar-day window to avoid missing late announcements.
        return end_day - timedelta(days=max(quarters * 100, 400)), end, quarters

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
        start_date: str | date | None = None,
        end_date: str | date | None = None,
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
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        frequency: Frequency | str = Frequency.d1,
        adjust: Adjust | str = Adjust.none,
        cross_validate: bool = True,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        provider_kwargs = self._provider_request_kwargs(providers, canonical_provider)
        start_date, end_date = self._resolve_market_window(start_date, end_date)
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
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        start_date, end_date = self._resolve_market_window(start_date, end_date)
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.valuation_metric,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_adj_factor(
        self,
        tickers: list[str],
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        cross_validate: bool = True,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        provider_kwargs = self._provider_request_kwargs(providers, canonical_provider)
        start_date, end_date = self._resolve_market_window(start_date, end_date)
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.adj_factor,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            cross_validate=cross_validate and len(provider_kwargs["provider_priority"]) > 1,
            **provider_kwargs,
        )
        return self.runner.run(req)

    def fetch_financial_indicator(
        self,
        tickers: list[str],
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        start_date, end_date, quarters = self._resolve_financial_window(start_date, end_date)
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.financial_indicator,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            extra_params={"financial_lookback_quarters": quarters},
            **self._provider_request_kwargs(providers, canonical_provider),
        )
        return self.runner.run(req)

    def fetch_financial_statement(
        self,
        tickers: list[str],
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        statement_types: list[str] | None = None,
        period: str | None = None,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        start_date, end_date, quarters = self._resolve_financial_window(start_date, end_date)
        extra_params: dict[str, object] = {"financial_lookback_quarters": quarters}
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
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
    ) -> StockDataResponse:
        start_date, end_date = self._resolve_market_window(start_date, end_date)
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

    def fetch_market_context(
        self,
        context_id: str,
        context_type: str,
        symbol: str,
        *,
        as_of: str | date | None = None,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        frequency: str = "1d",
        metrics: list[str] | None = None,
        market: str | None = None,
        contract: str | None = None,
        instrument_type: str | None = None,
        tenor: str | None = None,
        rate_type: str | None = None,
        max_staleness_days: int | None = None,
        cross_validate: bool = False,
        providers: list[str] | None = None,
        canonical_provider: str | None = None,
        save_raw: bool = True,
        save_cleaned: bool = True,
        export_parquet: bool = True,
        requested_by: str = "manual",
    ) -> MarketContextResponse:
        """Market background data (A-share/HK indices, FX, commodities, rates) by business request.

        Callers name *what* they need (context type, business symbol, as-of day or range,
        category parameters); vendor functions and column mappings are resolved from
        ``config/market_context_sources.yaml``. See ``MarketContextService`` for the
        freshness / history / change-metric semantics of the response.
        """
        from stock_data_ingestion.services.market_context_service import MarketContextService

        request = MarketContextRequest(
            context_id=context_id,
            context_type=context_type,
            symbol=symbol,
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            metrics=list(metrics or []),
            market=market,
            contract=contract,
            instrument_type=instrument_type,
            tenor=tenor,
            rate_type=rate_type,
            max_staleness_days=max_staleness_days,
            cross_validate=cross_validate,
            providers=parse_provider_list(providers) if providers else None,
            canonical_provider=parse_provider_list([canonical_provider])[0] if canonical_provider else None,
            save_raw=save_raw,
            save_cleaned=save_cleaned,
            export_parquet=export_parquet,
            requested_by=requested_by,
        )
        return MarketContextService(self.runner).fetch(request)

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

