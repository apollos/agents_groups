from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import Boolean, Date, DateTime, Float, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai


class Base(DeclarativeBase):
    pass


class AuditMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_asia_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_asia_shanghai, onupdate=now_asia_shanghai, nullable=False
    )


class StandardColumnsMixin(AuditMixin):
    record_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    effective_provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_api: Mapped[str] = mapped_column(String(128), nullable=False)
    source_site: Mapped[str] = mapped_column(String(128), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.1.0")
    canonical_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="tushare")
    source_role: Mapped[str] = mapped_column(String(64), nullable=False)
    merge_method: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    field_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    supplement_flags: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    conflict_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    canonical_value_suspect: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetch_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_update_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    ingestion_run_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    request_params_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    raw_payload_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    raw_payload_ref: Mapped[str] = mapped_column(Text, nullable=False)
    raw_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    raw_format: Mapped[str] = mapped_column(String(32), nullable=False, default="jsonl.gz")
    raw_row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    data_quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    quality_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class StockColumnsMixin:
    normalized_ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    provider_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="A_share")
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False, default="stock")


class SecurityModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "securities"
    __table_args__ = (
        UniqueConstraint("normalized_ticker", "effective_provider", name="uq_securities_ticker_provider"),
        Index("ix_securities_exchange_status", "exchange", "list_status"),
    )
    name: Mapped[Optional[str]] = mapped_column(String(128))
    company_full_name: Mapped[Optional[str]] = mapped_column(String(256))
    list_date: Mapped[Optional[date]] = mapped_column(Date)
    delist_date: Mapped[Optional[date]] = mapped_column(Date)
    list_status: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    board: Mapped[Optional[str]] = mapped_column(String(64))
    area: Mapped[Optional[str]] = mapped_column(String(64))
    industry: Mapped[Optional[str]] = mapped_column(String(128))
    total_share: Mapped[Optional[float]] = mapped_column(Float)
    float_share: Mapped[Optional[float]] = mapped_column(Float)
    main_business: Mapped[Optional[str]] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")


class TickerMappingModel(AuditMixin, Base):
    __tablename__ = "ticker_mappings"
    __table_args__ = (UniqueConstraint("provider", "provider_symbol", name="uq_provider_symbol"),)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    normalized_ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False, default="validated")
    raw_payload_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    data_quality: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class TradeCalendarModel(StandardColumnsMixin, Base):
    __tablename__ = "trade_calendar"
    __table_args__ = (
        UniqueConstraint("exchange", "calendar_date", "effective_provider", name="uq_trade_calendar"),
        Index("ix_trade_calendar_open", "exchange", "calendar_date", "is_open"),
    )
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    calendar_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    prev_trade_date: Mapped[Optional[date]] = mapped_column(Date)
    next_trade_date: Mapped[Optional[date]] = mapped_column(Date)
    trading_sessions: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False, default=list)
    lunch_break: Mapped[Optional[dict[str, str]]] = mapped_column(JSON)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")


class TradingStatusModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "trading_status"
    __table_args__ = (UniqueConstraint("normalized_ticker", "trade_date", "effective_provider", name="uq_trading_status"),)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    is_trading: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspend_reason: Mapped[Optional[str]] = mapped_column(Text)
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_star_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_delisting_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    list_status: Mapped[Optional[str]] = mapped_column(String(32))
    limit_up_price: Mapped[Optional[float]] = mapped_column(Float)
    limit_down_price: Mapped[Optional[float]] = mapped_column(Float)
    hit_limit_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hit_limit_down: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tradability_status: Mapped[str] = mapped_column(String(64), nullable=False, default="tradable")
    not_tradable_reason: Mapped[Optional[str]] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")


class BarColumnsMixin:
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    frequency: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    bar_start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trading_session: Mapped[Optional[str]] = mapped_column(String(32))
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    pre_close: Mapped[Optional[float]] = mapped_column(Float)
    change: Mapped[Optional[float]] = mapped_column(Float)
    pct_change: Mapped[Optional[float]] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    volume_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="share")
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    amount_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="CNY")
    vwap: Mapped[Optional[float]] = mapped_column(Float)
    turnover_rate: Mapped[Optional[float]] = mapped_column(Float)
    turnover_rate_free_float: Mapped[Optional[float]] = mapped_column(Float)
    adjust: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    adj_factor: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")


class DailyBarModel(StandardColumnsMixin, StockColumnsMixin, BarColumnsMixin, Base):
    __tablename__ = "daily_bars"
    __table_args__ = (
        UniqueConstraint("normalized_ticker", "frequency", "trade_date", "timestamp", "adjust", "effective_provider", name="uq_daily_bars"),
        Index("ix_daily_bars_ticker_date", "normalized_ticker", "trade_date"),
    )


class WeeklyBarModel(StandardColumnsMixin, StockColumnsMixin, BarColumnsMixin, Base):
    __tablename__ = "weekly_bars"
    __table_args__ = (
        UniqueConstraint("normalized_ticker", "frequency", "trade_date", "timestamp", "adjust", "effective_provider", name="uq_weekly_bars"),
        Index("ix_weekly_bars_ticker_date", "normalized_ticker", "trade_date"),
    )


class MinuteBarModel(StandardColumnsMixin, StockColumnsMixin, BarColumnsMixin, Base):
    __tablename__ = "minute_bars"
    __table_args__ = (
        UniqueConstraint("normalized_ticker", "frequency", "trade_date", "timestamp", "adjust", "effective_provider", name="uq_minute_bars"),
        Index("ix_minute_bars_ticker_ts", "normalized_ticker", "timestamp"),
    )


class RealtimeQuoteModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "realtime_quotes"
    __table_args__ = (UniqueConstraint("normalized_ticker", "quote_time_bucket", "effective_provider", name="uq_realtime_quote_bucket"),)
    quote_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    quote_time_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    latest_price: Mapped[Optional[float]] = mapped_column(Float)
    open: Mapped[Optional[float]] = mapped_column(Float)
    high: Mapped[Optional[float]] = mapped_column(Float)
    low: Mapped[Optional[float]] = mapped_column(Float)
    pre_close: Mapped[Optional[float]] = mapped_column(Float)
    change: Mapped[Optional[float]] = mapped_column(Float)
    pct_change: Mapped[Optional[float]] = mapped_column(Float)
    volume: Mapped[Optional[float]] = mapped_column(Float)
    amount: Mapped[Optional[float]] = mapped_column(Float)
    bid1_price: Mapped[Optional[float]] = mapped_column(Float)
    bid1_volume: Mapped[Optional[float]] = mapped_column(Float)
    ask1_price: Mapped[Optional[float]] = mapped_column(Float)
    ask1_volume: Mapped[Optional[float]] = mapped_column(Float)
    bid_ask_spread: Mapped[Optional[float]] = mapped_column(Float)
    limit_up_price: Mapped[Optional[float]] = mapped_column(Float)
    limit_down_price: Mapped[Optional[float]] = mapped_column(Float)
    distance_to_limit_up_pct: Mapped[Optional[float]] = mapped_column(Float)
    distance_to_limit_down_pct: Mapped[Optional[float]] = mapped_column(Float)


class AdjFactorModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "adj_factors"
    __table_args__ = (UniqueConstraint("normalized_ticker", "trade_date", "effective_provider", name="uq_adj_factor"),)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    adj_factor: Mapped[float] = mapped_column(Float, nullable=False)


class FinancialStatementModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "financial_statements"
    __table_args__ = (
        UniqueConstraint("normalized_ticker", "report_period", "statement_type", "report_type", "effective_provider", name="uq_financial_statement"),
    )
    report_period: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    report_date: Mapped[Optional[date]] = mapped_column(Date)
    announcement_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    statement_type: Mapped[str] = mapped_column(String(64), nullable=False)
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    operating_revenue: Mapped[Optional[float]] = mapped_column(Float)
    operating_profit: Mapped[Optional[float]] = mapped_column(Float)
    net_profit: Mapped[Optional[float]] = mapped_column(Float)
    parent_net_profit: Mapped[Optional[float]] = mapped_column(Float)
    total_assets: Mapped[Optional[float]] = mapped_column(Float)
    total_liabilities: Mapped[Optional[float]] = mapped_column(Float)
    parent_equity: Mapped[Optional[float]] = mapped_column(Float)
    operating_cash_flow: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")


class FinancialIndicatorModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "financial_indicators"
    __table_args__ = (UniqueConstraint("normalized_ticker", "report_period", "effective_provider", name="uq_financial_indicator"),)
    report_period: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    report_date: Mapped[Optional[date]] = mapped_column(Date)
    announcement_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    roe: Mapped[Optional[float]] = mapped_column(Float)
    roa: Mapped[Optional[float]] = mapped_column(Float)
    gross_margin: Mapped[Optional[float]] = mapped_column(Float)
    net_margin: Mapped[Optional[float]] = mapped_column(Float)
    revenue_yoy: Mapped[Optional[float]] = mapped_column(Float)
    net_profit_yoy: Mapped[Optional[float]] = mapped_column(Float)
    debt_asset_ratio: Mapped[Optional[float]] = mapped_column(Float)
    current_ratio: Mapped[Optional[float]] = mapped_column(Float)
    ocf_to_net_profit: Mapped[Optional[float]] = mapped_column(Float)
    eps: Mapped[Optional[float]] = mapped_column(Float)
    bps: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")


class ValuationMetricModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "valuation_metrics"
    __table_args__ = (UniqueConstraint("normalized_ticker", "trade_date", "effective_provider", name="uq_valuation_metric"),)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    pe: Mapped[Optional[float]] = mapped_column(Float)
    pe_ttm: Mapped[Optional[float]] = mapped_column(Float)
    pb: Mapped[Optional[float]] = mapped_column(Float)
    ps: Mapped[Optional[float]] = mapped_column(Float)
    ps_ttm: Mapped[Optional[float]] = mapped_column(Float)
    dividend_yield: Mapped[Optional[float]] = mapped_column(Float)
    total_market_value: Mapped[Optional[float]] = mapped_column(Float)
    float_market_value: Mapped[Optional[float]] = mapped_column(Float)
    turnover_rate: Mapped[Optional[float]] = mapped_column(Float)
    volume_ratio: Mapped[Optional[float]] = mapped_column(Float)
    amount: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")


class IndustryMembershipModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "industry_memberships"
    __table_args__ = (UniqueConstraint("normalized_ticker", "industry_system", "industry_code", "effective_date", "provider", name="uq_industry_membership"),)
    industry_system: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    industry_code: Mapped[Optional[str]] = mapped_column(String(64))
    industry_name: Mapped[str] = mapped_column(String(256), nullable=False)
    industry_level: Mapped[Optional[int]] = mapped_column(Integer)
    parent_industry_code: Mapped[Optional[str]] = mapped_column(String(64))
    effective_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date)
    source_methodology: Mapped[str] = mapped_column(Text, nullable=False)


class ConceptMembershipModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "concept_memberships"
    __table_args__ = (UniqueConstraint("normalized_ticker", "concept_code", "concept_name", "provider", name="uq_concept_membership"),)
    concept_code: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    concept_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    concept_strength: Mapped[Optional[float]] = mapped_column(Float)
    evidence: Mapped[Optional[str]] = mapped_column(Text)
    effective_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date)
    source_methodology: Mapped[str] = mapped_column(Text, nullable=False)


class MoneyFlowModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "money_flow"
    __table_args__ = (UniqueConstraint("normalized_ticker", "trade_date", "frequency", "source_methodology", "provider", name="uq_money_flow"),)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    frequency: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    source_methodology: Mapped[str] = mapped_column(Text, nullable=False)
    main_net_inflow: Mapped[Optional[float]] = mapped_column(Float)
    super_large_net_inflow: Mapped[Optional[float]] = mapped_column(Float)
    large_net_inflow: Mapped[Optional[float]] = mapped_column(Float)
    medium_net_inflow: Mapped[Optional[float]] = mapped_column(Float)
    small_net_inflow: Mapped[Optional[float]] = mapped_column(Float)
    main_net_inflow_ratio: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")


class IndexModel(StandardColumnsMixin, Base):
    __tablename__ = "indices"
    __table_args__ = (UniqueConstraint("index_code", "effective_provider", name="uq_index"),)
    index_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    index_name: Mapped[str] = mapped_column(String(256), nullable=False)
    exchange: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="A_share")
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False, default="index")
    list_date: Mapped[Optional[date]] = mapped_column(Date)
    index_provider: Mapped[Optional[str]] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")


class IndexBarModel(StandardColumnsMixin, Base):
    __tablename__ = "index_bars"
    __table_args__ = (UniqueConstraint("index_code", "frequency", "trade_date", "timestamp", "effective_provider", name="uq_index_bar"),)
    index_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    index_name: Mapped[Optional[str]] = mapped_column(String(256))
    exchange: Mapped[Optional[str]] = mapped_column(String(16))
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="A_share")
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False, default="index")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    frequency: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    pre_close: Mapped[Optional[float]] = mapped_column(Float)
    change: Mapped[Optional[float]] = mapped_column(Float)
    pct_change: Mapped[Optional[float]] = mapped_column(Float)
    volume: Mapped[Optional[float]] = mapped_column(Float)
    amount: Mapped[Optional[float]] = mapped_column(Float)
    adjust: Mapped[str] = mapped_column(String(8), nullable=False, default="none")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")


class IndexConstituentModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "index_constituents"
    __table_args__ = (UniqueConstraint("index_code", "normalized_ticker", "effective_date", "effective_provider", name="uq_index_constituent"),)
    index_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    index_name: Mapped[Optional[str]] = mapped_column(String(256))
    weight: Mapped[Optional[float]] = mapped_column(Float)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[Optional[date]] = mapped_column(Date)


class CorporateActionModel(StandardColumnsMixin, StockColumnsMixin, Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (UniqueConstraint("normalized_ticker", "action_type", "announcement_date", "ex_date", "effective_provider", name="uq_corporate_action"),)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    announcement_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    record_date: Mapped[Optional[date]] = mapped_column(Date)
    ex_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    dividend_payment_date: Mapped[Optional[date]] = mapped_column(Date)
    cash_dividend_per_share: Mapped[Optional[float]] = mapped_column(Float)
    stock_bonus_ratio: Mapped[Optional[float]] = mapped_column(Float)
    rights_issue_ratio: Mapped[Optional[float]] = mapped_column(Float)
    rights_issue_price: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY")


class SourceFetchLogModel(AuditMixin, Base):
    __tablename__ = "source_fetch_logs"
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_api: Mapped[str] = mapped_column(String(128), nullable=False)
    source_site: Mapped[str] = mapped_column(String(128), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    raw_payload_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    raw_payload_ref: Mapped[Optional[str]] = mapped_column(Text)
    raw_hash: Mapped[Optional[str]] = mapped_column(String(80))
    rows_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    ingestion_run_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    error: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unvalidated")
    data_quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class ProviderComparisonModel(AuditMixin, Base):
    __tablename__ = "provider_comparisons"
    comparison_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    comparison_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    canonical_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    compared_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    checked_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    matched_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    conflicted_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    conflicts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    request_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    ingestion_run_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="comparison")
    raw_payload_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    data_quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unvalidated")


class DataQualityConflictModel(AuditMixin, Base):
    __tablename__ = "data_quality_conflicts"
    conflict_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    comparison_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    canonical_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_value: Mapped[Optional[Any]] = mapped_column(JSON)
    other_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    other_value: Mapped[Optional[Any]] = mapped_column(JSON)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tolerance: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[Optional[str]] = mapped_column(Text)
    request_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    ingestion_run_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    canonical_record_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    other_record_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="comparison")
    raw_payload_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    data_quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unvalidated")


class RawPayloadIndexModel(AuditMixin, Base):
    __tablename__ = "raw_payload_index"
    raw_payload_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False, index=True)
    raw_payload_ref: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_api: Mapped[str] = mapped_column(String(128), nullable=False)
    source_site: Mapped[str] = mapped_column(String(128), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    ingestion_run_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    request_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sanitized_request_params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    request_params_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    fetch_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetch_completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_update_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    raw_format: Mapped[str] = mapped_column(String(32), nullable=False, default="jsonl.gz")
    content_encoding: Mapped[str] = mapped_column(String(32), nullable=False, default="gzip")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    raw_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    rows_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False, default="validated")
    data_quality: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class IngestionRequestModel(AuditMixin, Base):
    __tablename__ = "ingestion_requests"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_ingestion_request_key"),)
    request_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created", index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="request")
    raw_payload_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    data_quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unvalidated")


class IngestionRunModel(AuditMixin, Base):
    __tablename__ = "ingestion_runs"
    ingestion_run_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    request_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)
    provider_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    error_records: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="runner")
    raw_payload_id: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    data_quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    validation_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unvalidated")


BAR_MODEL_BY_FREQUENCY = {
    "1d": DailyBarModel,
    "1w": WeeklyBarModel,
    "1mo": WeeklyBarModel,
    "1m": MinuteBarModel,
    "5m": MinuteBarModel,
    "15m": MinuteBarModel,
    "30m": MinuteBarModel,
    "60m": MinuteBarModel,
}
