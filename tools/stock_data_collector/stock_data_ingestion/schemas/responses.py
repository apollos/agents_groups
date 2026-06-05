from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorRecord
from stock_data_ingestion.schemas.quality import QualityReport
from stock_data_ingestion.schemas.records import ProviderComparisonResult, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest


class ResponseStatus(StrEnum):
    success = "success"
    partial_success = "partial_success"
    failed = "failed"


class StockDataResponseData(BaseModel):
    securities: list[dict[str, Any]] = Field(default_factory=list)
    trade_calendar: list[dict[str, Any]] = Field(default_factory=list)
    trading_status: list[dict[str, Any]] = Field(default_factory=list)
    bars: list[dict[str, Any]] = Field(default_factory=list)
    realtime_quotes: list[dict[str, Any]] = Field(default_factory=list)
    adj_factors: list[dict[str, Any]] = Field(default_factory=list)
    financial_statements: list[dict[str, Any]] = Field(default_factory=list)
    financial_indicators: list[dict[str, Any]] = Field(default_factory=list)
    valuation_metrics: list[dict[str, Any]] = Field(default_factory=list)
    industry_memberships: list[dict[str, Any]] = Field(default_factory=list)
    concept_memberships: list[dict[str, Any]] = Field(default_factory=list)
    money_flow: list[dict[str, Any]] = Field(default_factory=list)
    indices: list[dict[str, Any]] = Field(default_factory=list)
    index_bars: list[dict[str, Any]] = Field(default_factory=list)
    index_constituents: list[dict[str, Any]] = Field(default_factory=list)
    corporate_actions: list[dict[str, Any]] = Field(default_factory=list)


class PersistenceReport(BaseModel):
    saved: bool = False
    tables_written: list[str] = Field(default_factory=list)
    parquet_refs: list[str] = Field(default_factory=list)
    raw_payload_refs: list[str] = Field(default_factory=list)
    raw_payload_ids: list[str] = Field(default_factory=list)


class StockDataResponse(BaseModel):
    schema_version: str = "stock_data_response.v0.1"
    request_id: str
    status: ResponseStatus
    created_at: datetime = Field(default_factory=now_asia_shanghai)
    completed_at: datetime = Field(default_factory=now_asia_shanghai)
    timezone: str = "Asia/Shanghai"
    canonical_provider: str = "tushare"
    request: StockDataRequest | dict[str, Any]
    provider_results: list[ProviderFetchResult] = Field(default_factory=list)
    provider_comparisons: list[ProviderComparisonResult] = Field(default_factory=list)
    data: StockDataResponseData = Field(default_factory=StockDataResponseData)
    quality_report: QualityReport = Field(default_factory=QualityReport)
    persistence: PersistenceReport = Field(default_factory=PersistenceReport)
    errors: list[ErrorRecord] = Field(default_factory=list)
