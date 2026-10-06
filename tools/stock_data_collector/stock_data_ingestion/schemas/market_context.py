"""Market-context request/response contract (indices, FX, commodities, rates).

Callers (e.g. the intelligence collector agent) describe *what* they need in business
terms: a context type, a business symbol, the day the data should be available for, or a
date range. Which vendor function answers that request is decided by the tool's
``config/market_context_sources.yaml``; vendor function names never appear here.

The response separates three timestamps that are easy to confuse:

* ``request_window.as_of`` – the day the caller asked for;
* ``data_date`` / ``observed_at`` – the date (and time, for snapshots) the data itself
  belongs to, taken from the provider row, never from the request or the clock;
* ``collected_at`` – when the tool fetched it.

``quality.is_fresh`` tells whether ``data_date`` satisfies the caller's staleness
tolerance; a stale value is still returned with its real date, never relabelled.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stock_data_ingestion.normalization.datetime_utils import normalize_trade_date, now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorRecord


class ContextType(StrEnum):
    equity_index = "equity_index"
    hk_index = "hk_index"
    fx = "fx"
    commodity = "commodity"
    interest_rate = "interest_rate"


class ContextFrequency(StrEnum):
    d1 = "1d"
    realtime = "realtime"


# Default freshness tolerance (calendar days between as_of and data_date) per type.
# Daily series tolerate a weekend; snapshots must be same-day. Callers override via
# ``max_staleness_days``.
DEFAULT_MAX_STALENESS_DAYS: dict[str, int] = {
    ContextType.equity_index: 3,
    ContextType.hk_index: 3,
    ContextType.fx: 3,
    ContextType.commodity: 3,
    ContextType.interest_rate: 3,
}
REALTIME_MAX_STALENESS_DAYS = 0

# Headline metric per type when the caller does not name one.
DEFAULT_HEADLINE_METRIC: dict[str, str] = {
    ContextType.equity_index: "close",
    ContextType.hk_index: "close",
    ContextType.fx: "spot_sell",
    ContextType.commodity: "close",
    ContextType.interest_rate: "rate_value",
}
REALTIME_HEADLINE_METRIC = {ContextType.commodity: "latest"}

CHANGE_PERIODS = (1, 5, 20)


def _request_id() -> str:
    return f"mctx_{uuid4().hex[:16]}"


class MarketContextRequest(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    request_id: str = Field(default_factory=_request_id)
    schema_version: str = "market_context_request.v0.1"
    context_id: str
    context_type: ContextType
    symbol: str
    # Latest-value mode: data available as of this day (defaults to today, Asia/Shanghai).
    as_of: Optional[date] = None
    # History mode: both dates set -> return the series in range, no "latest" relabelling.
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    frequency: ContextFrequency = ContextFrequency.d1
    metrics: list[str] = Field(default_factory=list)
    # Category-specific business parameters (all optional; the symbol registry supplies
    # defaults). They are part of the business identity and of the idempotency key.
    market: Optional[str] = None
    contract: Optional[str] = None
    instrument_type: Optional[str] = None
    tenor: Optional[str] = None
    rate_type: Optional[str] = None
    max_staleness_days: Optional[int] = None
    # Provider selection is normally left to config; explicit values are filtered through
    # the tool's allow-list exactly like other request types.
    providers: Optional[list[str]] = None
    canonical_provider: Optional[str] = None
    cross_validate: bool = False
    save_raw: bool = True
    save_cleaned: bool = True
    export_parquet: bool = True
    requested_by: str = "manual"
    # Correlation id handed over by the caller (the agent); inherited by internal sub-requests.
    trace_id: Optional[str] = None
    created_at: datetime = Field(default_factory=now_asia_shanghai)

    @field_validator("as_of", "start_date", "end_date", mode="before")
    @classmethod
    def normalize_dates(cls, value: Any) -> Any:
        if value is None or isinstance(value, date):
            return value
        text = str(value).strip()
        if not text:
            return None
        # Accept ISO datetimes from agents (keep the date part only).
        if "T" in text or " " in text:
            text = text.replace(" ", "T").split("T", 1)[0]
        return normalize_trade_date(text)

    @field_validator("symbol", "context_id", mode="before")
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        if value is None:
            return value
        text = str(value).strip()
        if not text:
            raise ValueError("INVALID_REQUEST: context_id and symbol must be non-empty")
        return text

    @model_validator(mode="after")
    def validate_window(self) -> "MarketContextRequest":
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("INVALID_REQUEST: start_date and end_date must be given together")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("INVALID_DATE_RANGE: start_date must be <= end_date")
        if self.start_date and str(self.frequency) == ContextFrequency.realtime:
            raise ValueError("INVALID_REQUEST: realtime snapshots have no history window")
        if self.max_staleness_days is not None and self.max_staleness_days < 0:
            raise ValueError("INVALID_REQUEST: max_staleness_days must be >= 0")
        return self

    @property
    def mode(self) -> str:
        return "history" if self.start_date is not None else "latest"

    def effective_as_of(self) -> date:
        if self.mode == "history":
            return self.end_date  # type: ignore[return-value]
        return self.as_of or now_asia_shanghai().date()

    def effective_max_staleness_days(self) -> int:
        if self.max_staleness_days is not None:
            return int(self.max_staleness_days)
        if str(self.frequency) == ContextFrequency.realtime:
            return REALTIME_MAX_STALENESS_DAYS
        return DEFAULT_MAX_STALENESS_DAYS.get(str(self.context_type), 3)


class ChangeMetric(BaseModel):
    """Change over ``periods`` observations with explicit unit and calendar semantics."""

    periods: int
    period_unit: str  # trading_day | observation | snapshot_vs_prev_close
    kind: str  # percent | percentage_point | absolute
    value: Optional[float] = None
    basis_points: Optional[float] = None  # filled for percentage_point changes
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    from_value: Optional[float] = None
    to_value: Optional[float] = None
    reason: Optional[str] = None  # why value is None (insufficient_history, realtime_only, ...)


class MarketContextObservation(BaseModel):
    data_date: date
    observed_at: Optional[datetime] = None
    values: dict[str, Optional[float]] = Field(default_factory=dict)
    record_id: Optional[str] = None
    provider: Optional[str] = None
    source_api: Optional[str] = None
    raw_payload_id: Optional[str] = None
    raw_payload_ref: Optional[str] = None
    raw_row_index: Optional[int] = None
    data_quality: Optional[float] = None
    validation_status: Optional[str] = None


class MarketContextSource(BaseModel):
    provider: Optional[str] = None
    source_api: Optional[str] = None
    source_site: Optional[str] = None
    source_url: Optional[str] = None
    adapter_version: Optional[str] = None
    providers_attempted: list[dict[str, Any]] = Field(default_factory=list)


class MarketContextQuality(BaseModel):
    usable: bool = False
    status: str = "missing"  # fresh | stale | missing | failed
    is_fresh: Optional[bool] = None
    staleness_days: Optional[int] = None
    max_staleness_days: int = 0
    data_date_matches_as_of: Optional[bool] = None
    observations: int = 0
    missing_fields: list[str] = Field(default_factory=list)
    single_source: bool = True
    cross_validated: bool = False
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data_quality_score: Optional[float] = None


class MarketContextProvenance(BaseModel):
    stock_data_request_id: Optional[str] = None
    ingestion_run_ids: list[str] = Field(default_factory=list)
    idempotency_key: Optional[str] = None
    record_ids: list[str] = Field(default_factory=list)
    raw_payload_ids: list[str] = Field(default_factory=list)
    raw_payload_refs: list[str] = Field(default_factory=list)
    tables_written: list[str] = Field(default_factory=list)
    parquet_refs: list[str] = Field(default_factory=list)


class MarketContextResult(BaseModel):
    context_id: str
    context_type: str
    symbol: str
    name: Optional[str] = None
    identity: dict[str, Any] = Field(default_factory=dict)
    request_window: dict[str, Any] = Field(default_factory=dict)
    data_date: Optional[date] = None
    observed_at: Optional[datetime] = None
    collected_at: datetime = Field(default_factory=now_asia_shanghai)
    metric: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    values: dict[str, Optional[float]] = Field(default_factory=dict)
    changes: dict[str, ChangeMetric] = Field(default_factory=dict)
    series: list[MarketContextObservation] = Field(default_factory=list)
    source: MarketContextSource = Field(default_factory=MarketContextSource)
    quality: MarketContextQuality = Field(default_factory=MarketContextQuality)
    provenance: MarketContextProvenance = Field(default_factory=MarketContextProvenance)


class MarketContextResponse(BaseModel):
    schema_version: str = "market_context_response.v0.1"
    request_id: str
    status: str  # success | partial_success | failed
    created_at: datetime = Field(default_factory=now_asia_shanghai)
    completed_at: datetime = Field(default_factory=now_asia_shanghai)
    timezone: str = "Asia/Shanghai"
    request: MarketContextRequest
    result: Optional[MarketContextResult] = None
    errors: list[ErrorRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stock_data_response: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "CHANGE_PERIODS",
    "ChangeMetric",
    "ContextFrequency",
    "ContextType",
    "DEFAULT_HEADLINE_METRIC",
    "DEFAULT_MAX_STALENESS_DAYS",
    "MarketContextObservation",
    "MarketContextProvenance",
    "MarketContextQuality",
    "MarketContextRequest",
    "MarketContextResponse",
    "MarketContextResult",
    "MarketContextSource",
    "REALTIME_HEADLINE_METRIC",
]
