from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorRecord
from stock_data_ingestion.schemas.quality import (
    DataQualityConflict,
    MergeMethod,
    SourceRole,
    ValidationStatus,
)


class AdapterFetchStatus(StrEnum):
    success = "success"
    partial_success = "partial_success"
    failed = "failed"
    unavailable = "unavailable"
    empty_result = "empty_result"


class ProviderFetchResult(BaseModel):
    provider: str
    source_api: str
    source_site: str
    adapter_version: str
    status: AdapterFetchStatus
    raw_payload_id: Optional[str] = None
    raw_payload_ref: Optional[str] = None
    raw_hash: Optional[str] = None
    raw_records: list[dict[str, Any]] = Field(default_factory=list)
    rows_fetched: int = 0
    started_at: datetime = Field(default_factory=now_asia_shanghai)
    completed_at: datetime = Field(default_factory=now_asia_shanghai)
    error: Optional[ErrorRecord] = None

    @model_validator(mode="after")
    def set_rows(self) -> "ProviderFetchResult":
        if self.rows_fetched == 0 and self.raw_records:
            self.rows_fetched = len(self.raw_records)
        return self


class ProviderComparisonResult(BaseModel):
    comparison_id: str = Field(default_factory=lambda: f"cmp_{uuid4().hex}")
    record_type: str
    comparison_key: str
    canonical_provider: str
    compared_provider: str
    status: str
    checked_fields: list[str] = Field(default_factory=list)
    matched_fields: list[str] = Field(default_factory=list)
    conflicted_fields: list[str] = Field(default_factory=list)
    conflicts: list[DataQualityConflict] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_asia_shanghai)
    request_id: Optional[str] = None
    ingestion_run_id: Optional[str] = None


STANDARD_RECORD_METADATA_FIELDS = {
    "record_id",
    "schema_version",
    "record_type",
    "provider",
    "source_api",
    "source_site",
    "adapter_version",
    "canonical_provider",
    "effective_provider",
    "source_role",
    "merge_method",
    "validation_status",
    "field_provenance",
    "supplement_flags",
    "conflict_ids",
    "canonical_value_suspect",
    "fetch_time",
    "provider_update_time",
    "ingested_at",
    "request_id",
    "ingestion_run_id",
    "request_params_hash",
    "idempotency_key",
    "raw_payload_id",
    "raw_payload_ref",
    "raw_hash",
    "raw_format",
    "raw_row_index",
    "data_quality",
    "quality_flags",
}


def required_provenance_fields(record: BaseModel) -> list[str]:
    """Return non-null business fields that must have field-level provenance.

    Metadata/audit fields describe the ingestion operation itself. Every other populated
    field is treated as business data and must be traceable to a raw payload before the
    record can enter the standard data layer.
    """

    fields: list[str] = []
    for field_name in record.__class__.model_fields:
        if field_name in STANDARD_RECORD_METADATA_FIELDS:
            continue
        value = getattr(record, field_name, None)
        if value is None:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        fields.append(field_name)
    return fields


class StandardRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    record_id: str = Field(default_factory=lambda: f"rec_{uuid4().hex}")
    schema_version: str = "stock_record.v0.1"
    record_type: str
    provider: str
    source_api: str
    source_site: str
    adapter_version: str = "0.1.0"
    canonical_provider: str = "tushare"
    effective_provider: str
    source_role: SourceRole = SourceRole.canonical
    merge_method: MergeMethod = MergeMethod.canonical_only
    validation_status: ValidationStatus = ValidationStatus.unvalidated
    field_provenance: dict[str, Any] = Field(default_factory=dict)
    supplement_flags: dict[str, Any] = Field(default_factory=dict)
    conflict_ids: list[str] = Field(default_factory=list)
    canonical_value_suspect: bool = False
    fetch_time: datetime = Field(default_factory=now_asia_shanghai)
    provider_update_time: Optional[datetime] = None
    ingested_at: datetime = Field(default_factory=now_asia_shanghai)
    request_id: str
    ingestion_run_id: str
    request_params_hash: str
    idempotency_key: str
    raw_payload_id: str
    raw_payload_ref: str
    raw_hash: str
    raw_format: str = "jsonl.gz"
    raw_row_index: int = 0
    data_quality: float = 0.0
    quality_flags: list[str] = Field(default_factory=list)

    @field_validator("data_quality")
    @classmethod
    def clamp_quality(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @model_validator(mode="after")
    def require_provenance(self) -> "StandardRecord":
        if not self.field_provenance:
            raise ValueError("missing_field_provenance: standard records require field-level provenance")
        missing = [field for field in required_provenance_fields(self) if field not in self.field_provenance]
        if missing:
            raise ValueError(f"missing_field_provenance: fields without provenance: {sorted(missing)}")
        if not self.raw_payload_id or not self.raw_payload_ref:
            raise ValueError("missing_raw_payload_ref: standard records must be traceable to raw payload")
        return self


class StockIdentityMixin(BaseModel):
    normalized_ticker: str
    provider_symbol: str
    exchange: str
    market: str = "A_share"
    asset_type: str = "stock"


class TimezoneMixin(BaseModel):
    timezone: str = "Asia/Shanghai"


class CurrencyMixin(BaseModel):
    currency: str = "CNY"


class SecurityMasterRecord(StandardRecord, StockIdentityMixin, CurrencyMixin):
    record_type: str = "security_master"
    name: Optional[str] = None
    company_full_name: Optional[str] = None
    list_date: Optional[date] = None
    delist_date: Optional[date] = None
    list_status: Optional[str] = None
    board: Optional[str] = None
    area: Optional[str] = None
    industry: Optional[str] = None
    total_share: Optional[float] = None
    float_share: Optional[float] = None
    main_business: Optional[str] = None


class TradeCalendarRecord(StandardRecord, TimezoneMixin):
    record_type: str = "trade_calendar"
    exchange: str
    calendar_date: date
    is_open: bool
    prev_trade_date: Optional[date] = None
    next_trade_date: Optional[date] = None
    trading_sessions: list[dict[str, str]] = Field(default_factory=list)
    lunch_break: Optional[dict[str, str]] = None


class TradingStatusRecord(StandardRecord, StockIdentityMixin, TimezoneMixin):
    record_type: str = "trading_status"
    trade_date: date
    is_trading: bool
    is_suspended: bool = False
    suspend_reason: Optional[str] = None
    is_st: bool = False
    is_star_st: bool = False
    has_delisting_risk: bool = False
    list_status: Optional[str] = None
    limit_up_price: Optional[float] = None
    limit_down_price: Optional[float] = None
    hit_limit_up: bool = False
    hit_limit_down: bool = False
    tradability_status: str = "tradable"
    not_tradable_reason: Optional[str] = None


class BarRecord(StandardRecord, StockIdentityMixin, CurrencyMixin, TimezoneMixin):
    record_type: str = "bar"
    trade_date: date
    timestamp: datetime
    frequency: str
    bar_start_time: datetime
    bar_end_time: datetime
    trading_session: Optional[str] = None
    is_complete: bool
    open: float
    high: float
    low: float
    close: float
    pre_close: Optional[float] = None
    change: Optional[float] = None
    pct_change: Optional[float] = None
    volume: float
    volume_unit: str = "share"
    amount: float
    amount_unit: str = "CNY"
    vwap: Optional[float] = None
    turnover_rate: Optional[float] = None
    turnover_rate_free_float: Optional[float] = None
    adjust: str
    adj_factor: Optional[float] = None

    @field_validator("adjust")
    @classmethod
    def validate_adjust(cls, value: str) -> str:
        if value not in {"none", "qfq", "hfq"}:
            raise ValueError("INVALID_REQUEST: adjust must be one of none, qfq, hfq")
        return value

    @model_validator(mode="after")
    def validate_prices(self) -> "BarRecord":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("NORMALIZATION_FAILED: high price is lower than open/close/low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("NORMALIZATION_FAILED: low price is higher than open/close/high")
        if self.volume < 0 or self.amount < 0:
            raise ValueError("NORMALIZATION_FAILED: volume and amount must be non-negative")
        return self


class RealtimeQuoteRecord(StandardRecord, StockIdentityMixin, CurrencyMixin, TimezoneMixin):
    record_type: str = "realtime_quote"
    quote_time: datetime
    quote_time_bucket: datetime
    latest_price: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    pre_close: Optional[float] = None
    change: Optional[float] = None
    pct_change: Optional[float] = None
    volume: Optional[float] = None
    amount: Optional[float] = None
    bid1_price: Optional[float] = None
    bid1_volume: Optional[float] = None
    ask1_price: Optional[float] = None
    ask1_volume: Optional[float] = None
    bid_ask_spread: Optional[float] = None
    limit_up_price: Optional[float] = None
    limit_down_price: Optional[float] = None
    distance_to_limit_up_pct: Optional[float] = None
    distance_to_limit_down_pct: Optional[float] = None


class AdjFactorRecord(StandardRecord, StockIdentityMixin):
    record_type: str = "adj_factor"
    trade_date: date
    adj_factor: Optional[float] = None
    fore_adjust_factor: Optional[float] = None
    back_adjust_factor: Optional[float] = None
    event_adjust_factor: Optional[float] = None
    factor_event_date: Optional[date] = None
    factor_method: Optional[str] = None

    @model_validator(mode="after")
    def require_some_factor(self) -> "AdjFactorRecord":
        if self.adj_factor is None and self.fore_adjust_factor is None and self.back_adjust_factor is None and self.event_adjust_factor is None:
            raise ValueError("NORMALIZATION_FAILED: at least one adjustment factor field is required")
        return self


class FinancialStatementRecord(StandardRecord, StockIdentityMixin, CurrencyMixin):
    record_type: str = "financial_statement"
    report_period: str
    report_date: Optional[date] = None
    announcement_date: Optional[date] = None
    statement_type: str
    report_type: str
    operating_revenue: Optional[float] = None
    operating_profit: Optional[float] = None
    net_profit: Optional[float] = None
    parent_net_profit: Optional[float] = None
    total_assets: Optional[float] = None
    total_liabilities: Optional[float] = None
    parent_equity: Optional[float] = None
    operating_cash_flow: Optional[float] = None


class FinancialIndicatorRecord(StandardRecord, StockIdentityMixin, CurrencyMixin):
    record_type: str = "financial_indicator"
    report_period: str
    report_date: Optional[date] = None
    announcement_date: Optional[date] = None
    roe: Optional[float] = None
    roa: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    revenue_yoy: Optional[float] = None
    net_profit_yoy: Optional[float] = None
    debt_asset_ratio: Optional[float] = None
    current_ratio: Optional[float] = None
    ocf_to_net_profit: Optional[float] = None
    eps: Optional[float] = None
    bps: Optional[float] = None


class ValuationMetricRecord(StandardRecord, StockIdentityMixin, CurrencyMixin):
    record_type: str = "valuation_metric"
    trade_date: date
    pe: Optional[float] = None
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    ps: Optional[float] = None
    ps_ttm: Optional[float] = None
    pcf_ncf_ttm: Optional[float] = None
    dividend_yield: Optional[float] = None
    total_market_value: Optional[float] = None
    float_market_value: Optional[float] = None
    turnover_rate: Optional[float] = None
    volume_ratio: Optional[float] = None
    amount: Optional[float] = None


class IndustryMembershipRecord(StandardRecord, StockIdentityMixin):
    record_type: str = "industry_membership"
    industry_system: str
    industry_code: Optional[str] = None
    industry_name: str
    industry_level: Optional[int] = None
    parent_industry_code: Optional[str] = None
    effective_date: Optional[date] = None
    end_date: Optional[date] = None
    source_methodology: str


class ConceptMembershipRecord(StandardRecord, StockIdentityMixin):
    record_type: str = "concept_membership"
    concept_code: Optional[str] = None
    concept_name: str
    concept_strength: Optional[float] = None
    evidence: Optional[str] = None
    effective_date: Optional[date] = None
    end_date: Optional[date] = None
    source_methodology: str


class MoneyFlowRecord(StandardRecord, StockIdentityMixin, CurrencyMixin, TimezoneMixin):
    record_type: str = "money_flow"
    trade_date: date
    frequency: str = "1d"
    source_methodology: str
    main_net_inflow: Optional[float] = None
    super_large_net_inflow: Optional[float] = None
    large_net_inflow: Optional[float] = None
    medium_net_inflow: Optional[float] = None
    small_net_inflow: Optional[float] = None
    main_net_inflow_ratio: Optional[float] = None


class IndexRecord(StandardRecord, CurrencyMixin):
    record_type: str = "index"
    index_code: str
    index_name: str
    exchange: Optional[str] = None
    market: str = "A_share"
    asset_type: str = "index"
    list_date: Optional[date] = None
    index_provider: Optional[str] = None


class IndexBarRecord(StandardRecord, CurrencyMixin, TimezoneMixin):
    record_type: str = "index_bar"
    index_code: str
    index_name: Optional[str] = None
    exchange: Optional[str] = None
    market: str = "A_share"
    asset_type: str = "index"
    trade_date: date
    timestamp: datetime
    frequency: str
    open: float
    high: float
    low: float
    close: float
    pre_close: Optional[float] = None
    change: Optional[float] = None
    pct_change: Optional[float] = None
    volume: Optional[float] = None
    amount: Optional[float] = None
    adjust: str = "none"


class IndexConstituentRecord(StandardRecord, StockIdentityMixin):
    record_type: str = "index_constituent"
    index_code: str
    index_name: Optional[str] = None
    weight: Optional[float] = None
    effective_date: date
    end_date: Optional[date] = None


class CorporateActionRecord(StandardRecord, StockIdentityMixin, CurrencyMixin):
    record_type: str = "corporate_action"
    action_type: str
    announcement_date: Optional[date] = None
    record_date: Optional[date] = None
    ex_date: Optional[date] = None
    dividend_payment_date: Optional[date] = None
    cash_dividend_per_share: Optional[float] = None
    stock_bonus_ratio: Optional[float] = None
    rights_issue_ratio: Optional[float] = None
    rights_issue_price: Optional[float] = None


class RawPayloadIndexRecord(BaseModel):
    raw_payload_id: str
    raw_payload_ref: str
    provider: str
    source_api: str
    source_site: str
    adapter_version: str
    request_id: str
    ingestion_run_id: str
    request_type: str
    sanitized_request_params: dict[str, Any] = Field(default_factory=dict)
    request_params_hash: str
    idempotency_key: str
    fetch_started_at: datetime
    fetch_completed_at: datetime
    provider_update_time: Optional[datetime] = None
    raw_format: str = "jsonl.gz"
    content_encoding: str = "gzip"
    timezone: str = "Asia/Shanghai"
    raw_hash: str
    rows_fetched: int
    created_at: datetime = Field(default_factory=now_asia_shanghai)


AdapterFetchResult = ProviderFetchResult


__all__ = [
    "AdapterFetchStatus",
    "ProviderFetchResult",
    "AdapterFetchResult",
    "ProviderComparisonResult",
    "StandardRecord",
    "SecurityMasterRecord",
    "TradeCalendarRecord",
    "TradingStatusRecord",
    "BarRecord",
    "RealtimeQuoteRecord",
    "AdjFactorRecord",
    "FinancialStatementRecord",
    "FinancialIndicatorRecord",
    "ValuationMetricRecord",
    "IndustryMembershipRecord",
    "ConceptMembershipRecord",
    "MoneyFlowRecord",
    "IndexRecord",
    "IndexBarRecord",
    "IndexConstituentRecord",
    "CorporateActionRecord",
    "RawPayloadIndexRecord",
    "DataQualityConflict",
    "ErrorRecord",
    "required_provenance_fields",
]
