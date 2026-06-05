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
