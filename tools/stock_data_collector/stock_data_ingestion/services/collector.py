from __future__ import annotations

from datetime import date
from uuid import uuid4

from stock_data_ingestion.schemas.requests import Adjust, Frequency, RequestType, StockDataRequest
from stock_data_ingestion.schemas.responses import StockDataResponse
from stock_data_ingestion.services.ingestion_runner import IngestionRunner


def _request_id() -> str:
    return f"req_{uuid4().hex[:16]}"


class StockDataCollector:
    def __init__(self, runner: IngestionRunner) -> None:
        self.runner = runner

    def fetch_security_master(self, tickers: list[str] | None = None) -> StockDataResponse:
        req = StockDataRequest(request_id=_request_id(), request_type=RequestType.security_master, tickers=tickers or [])
        return self.runner.run(req)

    def fetch_trade_calendar(self, exchange: str, start_date: str | date, end_date: str | date) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.trade_calendar,
            exchanges=[exchange],
            start_date=start_date,
            end_date=end_date,
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
    ) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.historical_bars,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjust=adjust,
            fields=["open", "high", "low", "close", "volume", "amount"],
            cross_validate=cross_validate,
        )
        return self.runner.run(req)

    def fetch_valuation(self, tickers: list[str], start_date: str | date, end_date: str | date) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.valuation_metric,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
        )
        return self.runner.run(req)

    def fetch_financial_indicator(self, tickers: list[str], start_date: str | date, end_date: str | date) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.financial_indicator,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
        )
        return self.runner.run(req)

    def fetch_financial_statement(
        self,
        tickers: list[str],
        start_date: str | date,
        end_date: str | date,
        statement_types: list[str] | None = None,
        period: str | None = None,
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
        )
        return self.runner.run(req)

    def fetch_money_flow(self, tickers: list[str], start_date: str | date, end_date: str | date) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.money_flow,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
        )
        return self.runner.run(req)

    def fetch_trading_status(self, tickers: list[str], start_date: str | date | None = None, end_date: str | date | None = None) -> StockDataResponse:
        req = StockDataRequest(
            request_id=_request_id(),
            request_type=RequestType.trading_status,
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
        )
        return self.runner.run(req)

    def fetch_corporate_action(
        self,
        tickers: list[str],
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        action_types: list[str] | None = None,
        event_date_field: str | None = None,
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
        )
        return self.runner.run(req)

