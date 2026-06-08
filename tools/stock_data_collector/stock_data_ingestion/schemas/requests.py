from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai, normalize_trade_date
from stock_data_ingestion.normalization.ticker import normalize_ticker
from stock_data_ingestion.utils.idempotency import generate_idempotency_key


class RequestType(StrEnum):
    security_master = "security_master"
    trade_calendar = "trade_calendar"
    trading_status = "trading_status"
    historical_bars = "historical_bars"
    realtime_quote = "realtime_quote"
    adj_factor = "adj_factor"
    financial_statement = "financial_statement"
    financial_indicator = "financial_indicator"
    valuation_metric = "valuation_metric"
    industry_concept = "industry_concept"
    money_flow = "money_flow"
    index_data = "index_data"
    corporate_action = "corporate_action"
    batch_refresh = "batch_refresh"
    cross_validation = "cross_validation"


class Frequency(StrEnum):
    m1 = "1m"
    m5 = "5m"
    m15 = "15m"
    m30 = "30m"
    m60 = "60m"
    d1 = "1d"
    w1 = "1w"
    mo1 = "1mo"
    realtime = "realtime"


class Adjust(StrEnum):
    none = "none"
    qfq = "qfq"
    hfq = "hfq"


class StockDataRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    request_id: str
    schema_version: str = "stock_data_request.v0.1"
    request_type: RequestType
    tickers: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    universe_id: Optional[str] = None
    market: str = "A_share"
    exchanges: list[str] = Field(default_factory=lambda: ["SSE", "SZSE", "BSE"])
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    frequency: Optional[Frequency] = None
    adjust: Optional[Adjust] = Adjust.none
    fields: list[str] = Field(default_factory=list)
    provider_priority: list[str] = Field(default_factory=lambda: ["tushare", "akshare", "baostock", "joinquant"])
    canonical_provider: str = "tushare"
    fallback_enabled: bool = True
    cross_validate: bool = True
    save_raw: bool = True
    save_cleaned: bool = True
    export_parquet: bool = True
    idempotency_key: Optional[str] = None
    requested_by: str = "manual"
    created_at: datetime = Field(default_factory=now_asia_shanghai)
    extra_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tickers", mode="before")
    @classmethod
    def normalize_tickers(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [normalize_ticker(ticker) for ticker in value]

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def normalize_dates(cls, value: Any) -> Any:
        if value is None or isinstance(value, date):
            return value
        return normalize_trade_date(str(value))

    @model_validator(mode="after")
    def validate_and_fill_idempotency(self) -> "StockDataRequest":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("INVALID_DATE_RANGE: start_date must be <= end_date")
        if self.request_type in {RequestType.historical_bars, RequestType.realtime_quote, RequestType.valuation_metric, RequestType.financial_indicator} and not self.tickers:
            raise ValueError("INVALID_REQUEST: tickers are required for this request_type")
        if self.idempotency_key is None:
            self.idempotency_key = generate_idempotency_key(
                module_name="stock_data_ingestion",
                request_type=str(self.request_type),
                provider=self.canonical_provider,
                tickers=self.tickers,
                universe_id=self.universe_id,
                start_date=self.start_date,
                end_date=self.end_date,
                frequency=str(self.frequency) if self.frequency else None,
                adjust=str(self.adjust) if self.adjust else None,
                fields=self.fields,
                provider_set=self.provider_priority,
                schema_version="v0.1",
            )
        return self
