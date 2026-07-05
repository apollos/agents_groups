"""SQLAlchemy ORM models (spec section 16).

Tables map 1:1 to the spec's PostgreSQL schema. JSONB columns are declared with
the portable ``JSON`` type so the same models run on SQLite (default) and
PostgreSQL. Only links and structured results are persisted; never raw content.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TargetProfile(Base):
    __tablename__ = "target_profile"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    target_type: Mapped[str | None] = mapped_column(String)
    canonical_name: Mapped[str | None] = mapped_column(String)
    aliases: Mapped[dict | None] = mapped_column(JSON)
    products: Mapped[dict | None] = mapped_column(JSON)
    business_segments: Mapped[dict | None] = mapped_column(JSON)
    customers: Mapped[dict | None] = mapped_column(JSON)
    suppliers: Mapped[dict | None] = mapped_column(JSON)
    competitors: Mapped[dict | None] = mapped_column(JSON)
    upstream_terms: Mapped[dict | None] = mapped_column(JSON)
    downstream_terms: Mapped[dict | None] = mapped_column(JSON)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class SearchRun(Base):
    __tablename__ = "search_run"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    target_id: Mapped[str | None] = mapped_column(String)
    task_profile: Mapped[dict | None] = mapped_column(JSON)
    budget_profile: Mapped[dict | None] = mapped_column(JSON)
    query_plan_version: Mapped[str | None] = mapped_column(String)
    model_policy_version: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str | None] = mapped_column(String)
    summary: Mapped[dict | None] = mapped_column(JSON)


class SearchQuery(Base):
    __tablename__ = "search_query"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    search_run_id: Mapped[str | None] = mapped_column(String, ForeignKey("search_run.id"))
    query_text: Mapped[str | None] = mapped_column(Text)
    query_family: Mapped[str | None] = mapped_column(String)
    priority_score: Mapped[float | None] = mapped_column(Float)
    language: Mapped[str | None] = mapped_column(String)
    region: Mapped[str | None] = mapped_column(String)
    expected_value_reason: Mapped[dict | None] = mapped_column(JSON)
    executed: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class SourceLink(Base):
    __tablename__ = "source_link"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    search_run_id: Mapped[str | None] = mapped_column(String, ForeignKey("search_run.id"))
    query_id: Mapped[str | None] = mapped_column(String)
    provider: Mapped[str | None] = mapped_column(String)
    rank: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String)
    source_name: Mapped[str | None] = mapped_column(String)
    source_type: Mapped[str | None] = mapped_column(String)
    document_type: Mapped[str | None] = mapped_column(String)
    publish_time_guess: Mapped[datetime | None] = mapped_column(DateTime)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime)
    access_profile_id: Mapped[str | None] = mapped_column(String)
    content_hash: Mapped[str | None] = mapped_column(String)
    simhash: Mapped[str | None] = mapped_column(String)
    read_status: Mapped[str | None] = mapped_column(String)
    triage_score: Mapped[float | None] = mapped_column(Float)
    triage_decision: Mapped[str | None] = mapped_column(String)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON)


class LinkReadAttempt(Base):
    __tablename__ = "link_read_attempt"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_link_id: Mapped[str | None] = mapped_column(String, ForeignKey("source_link.id"))
    access_profile_id: Mapped[str | None] = mapped_column(String)
    read_status: Mapped[str | None] = mapped_column(String)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String)
    content_length: Mapped[int | None] = mapped_column(Integer)
    extracted_title: Mapped[str | None] = mapped_column(Text)
    extracted_publish_time: Mapped[datetime | None] = mapped_column(DateTime)
    content_hash: Mapped[str | None] = mapped_column(String)
    selected_passage_count: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class ModelRun(Base):
    __tablename__ = "model_run"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_link_id: Mapped[str | None] = mapped_column(String, ForeignKey("source_link.id"))
    task_name: Mapped[str | None] = mapped_column(String)
    call_mode: Mapped[str | None] = mapped_column(String)
    provider_type: Mapped[str | None] = mapped_column(String)
    provider: Mapped[str | None] = mapped_column(String)
    model_name: Mapped[str | None] = mapped_column(String)
    model_config_id: Mapped[str | None] = mapped_column(String)
    model_policy_version: Mapped[str | None] = mapped_column(String)
    prompt_version: Mapped[str | None] = mapped_column(String)
    schema_version: Mapped[str | None] = mapped_column(String)
    input_chars: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String)
    error_type: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)
    provider_request_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class ModelOutput(Base):
    __tablename__ = "model_output"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    model_run_id: Mapped[str | None] = mapped_column(String, ForeignKey("model_run.id"))
    source_link_id: Mapped[str | None] = mapped_column(String)
    output_json: Mapped[dict | None] = mapped_column(JSON)
    schema_valid: Mapped[bool | None] = mapped_column(Boolean)
    validation_errors: Mapped[dict | None] = mapped_column(JSON)
    decision: Mapped[str | None] = mapped_column(String)
    overall_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class MergedAnalysis(Base):
    __tablename__ = "merged_analysis"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    decision: Mapped[str | None] = mapped_column(String)
    overall_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    disagreement_level: Mapped[str | None] = mapped_column(String)
    merge_method: Mapped[str | None] = mapped_column(String)
    model_outputs: Mapped[dict | None] = mapped_column(JSON)
    field_conflicts: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class AnalysisBrief(Base):
    __tablename__ = "analysis_brief"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    one_sentence: Mapped[str | None] = mapped_column(Text)
    what_happened: Mapped[str | None] = mapped_column(Text)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    affected_business_lines: Mapped[dict | None] = mapped_column(JSON)
    impact_channels: Mapped[dict | None] = mapped_column(JSON)
    time_horizon: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class FactItemRow(Base):
    __tablename__ = "fact_item"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    fact_type: Mapped[str | None] = mapped_column(String)
    fact_statement: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[dict | None] = mapped_column(JSON)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    period: Mapped[str | None] = mapped_column(String)
    direction: Mapped[str | None] = mapped_column(String)
    evidence_locator: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class MetricObservationRow(Base):
    __tablename__ = "metric_observation"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    metric_name: Mapped[str | None] = mapped_column(String)
    metric_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String)
    period: Mapped[str | None] = mapped_column(String)
    scope: Mapped[dict | None] = mapped_column(JSON)
    comparison: Mapped[dict | None] = mapped_column(JSON)
    interpretation: Mapped[str | None] = mapped_column(Text)
    impact_channels: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class EventCardRow(Base):
    __tablename__ = "event_card"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    event_type: Mapped[str | None] = mapped_column(String)
    event_date: Mapped[datetime | None] = mapped_column(DateTime)
    summary: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[dict | None] = mapped_column(JSON)
    metrics: Mapped[dict | None] = mapped_column(JSON)
    impact: Mapped[dict | None] = mapped_column(JSON)
    # Model-attributed research variable evidence (V0.8.1). Persisted so cache/reuse
    # clones keep confirmed coverage instead of degrading to keyword candidates.
    tracking_variables: Mapped[dict | None] = mapped_column(JSON)
    source_corroboration_status: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class RelationRecordRow(Base):
    __tablename__ = "relation_record"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    subject_entity: Mapped[dict | None] = mapped_column(JSON)
    relation_type: Mapped[str | None] = mapped_column(String)
    object_entity: Mapped[dict | None] = mapped_column(JSON)
    qualifiers: Mapped[dict | None] = mapped_column(JSON)
    evidence_locator: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class RiskFlagRow(Base):
    __tablename__ = "risk_flag"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    risk_type: Mapped[str | None] = mapped_column(String)
    risk_summary: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str | None] = mapped_column(String)
    time_horizon: Mapped[str | None] = mapped_column(String)
    impact_channels: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class CatalystItemRow(Base):
    __tablename__ = "catalyst_item"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    catalyst_type: Mapped[str | None] = mapped_column(String)
    expected_date: Mapped[datetime | None] = mapped_column(DateTime)
    description: Mapped[str | None] = mapped_column(Text)
    potential_impact: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class CustomerSupplierSignalRow(Base):
    __tablename__ = "customer_supplier_signal"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    signal_type: Mapped[str | None] = mapped_column(String)
    customer_or_supplier: Mapped[str | None] = mapped_column(String)
    product: Mapped[str | None] = mapped_column(String)
    business_meaning: Mapped[str | None] = mapped_column(Text)
    impact_channels: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class PriceCostMarginSignalRow(Base):
    __tablename__ = "price_cost_margin_signal"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    signal_type: Mapped[str | None] = mapped_column(String)
    product_or_material: Mapped[str | None] = mapped_column(String)
    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String)
    period: Mapped[str | None] = mapped_column(String)
    direction: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class PolicyRegulatorySignalRow(Base):
    __tablename__ = "policy_regulatory_signal"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    merged_analysis_id: Mapped[str | None] = mapped_column(String)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    policy_type: Mapped[str | None] = mapped_column(String)
    issuer: Mapped[str | None] = mapped_column(String)
    effective_date: Mapped[datetime | None] = mapped_column(DateTime)
    affected_entities: Mapped[dict | None] = mapped_column(JSON)
    affected_products: Mapped[dict | None] = mapped_column(JSON)
    impact_channels: Mapped[dict | None] = mapped_column(JSON)
    summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class AnalystQuestionRow(Base):
    __tablename__ = "analyst_question"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_link_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    related_event_id: Mapped[str | None] = mapped_column(String)
    question: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(String)
    suggested_queries: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class CoverageGapRow(Base):
    __tablename__ = "coverage_gap"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    search_run_id: Mapped[str | None] = mapped_column(String)
    target_id: Mapped[str | None] = mapped_column(String)
    gap_type: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    suggested_next_queries: Mapped[dict | None] = mapped_column(JSON)
    priority: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)


class Feedback(Base):
    """Analyst feedback (spec section 22)."""

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    object_type: Mapped[str | None] = mapped_column(String)
    object_id: Mapped[str | None] = mapped_column(String)
    useful_for_analysis: Mapped[bool | None] = mapped_column(Boolean)
    correct: Mapped[bool | None] = mapped_column(Boolean)
    impact_direction_correct: Mapped[bool | None] = mapped_column(Boolean)
    missing_fields: Mapped[dict | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    # Optional optimization hints (spec 22.2): tie feedback to a model / query
    # family / source type so weights can be tuned downstream.
    model_config_id: Mapped[str | None] = mapped_column(String)
    query_family: Mapped[str | None] = mapped_column(String)
    source_type: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
