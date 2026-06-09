"""Pydantic schemas (spec sections 12 and 13).

These model the *transient* model output (BundleExtraction) and the structured
objects that get persisted. Raw page content is never modeled or stored.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Enumerations (kept as Literal for validation, open via str fallbacks) ---

Decision = Literal["save_structured", "link_only", "skip"]
SourceType = Literal[
    "official", "exchange", "regulator", "company", "media",
    "industry", "forum", "social", "unknown",
]
ImpactChannel = Literal[
    "revenue", "margin", "cost", "supply", "demand", "valuation",
    "risk", "cashflow", "capex", "risk_premium",
]
TimeHorizon = Literal["intraday", "1w", "1m", "quarter", "annual", "long_term", "unclear"]
Direction = Literal["positive", "negative", "neutral", "mixed", "unclear"]


# --- Sub-objects ------------------------------------------------------------


class SourceQuality(BaseModel):
    source_type: str = "unknown"
    is_original_source: bool = False
    source_credibility_score: float = 0.0
    risk_flags: list[str] = Field(default_factory=list)


class Brief(BaseModel):
    one_sentence: str = ""
    what_happened: str = ""
    why_it_matters: str = ""
    affected_business_lines: list[str] = Field(default_factory=list)
    impact_channels: list[str] = Field(default_factory=list)
    time_horizon: str = "unclear"
    uncertainty: str = ""


class EvidenceLocator(BaseModel):
    passage_id: str | None = None
    section: str | None = None
    table_id: str | None = None


class FactItem(BaseModel):
    fact_id: str | None = None
    fact_type: str = "unknown"
    fact_statement: str = ""
    entities: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    period: str | None = None
    direction: str = "unclear"
    evidence_locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    confidence: float = 0.0


class MetricObservation(BaseModel):
    metric_id: str | None = None
    metric_name: str = ""
    metric_value: float | None = None
    unit: str | None = None
    period: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)
    comparison: dict[str, Any] = Field(default_factory=dict)
    interpretation: str = ""
    impact_channels: list[str] = Field(default_factory=list)
    evidence_locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    confidence: float = 0.0


class EventImpact(BaseModel):
    direction: str = "unclear"
    channels: list[str] = Field(default_factory=list)
    horizon: str = "unclear"
    magnitude_guess: str = "unknown"


class EventCard(BaseModel):
    event_id: str | None = None
    event_type: str = "unknown"
    event_date: str | None = None
    summary: str = ""
    entities: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    impact: EventImpact = Field(default_factory=EventImpact)
    source_corroboration_status: str = "single_source"
    evidence_locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    confidence: float = 0.0


class EntityRef(BaseModel):
    name: str = ""
    type: str = "company"
    ticker: str | None = None


class RelationRecord(BaseModel):
    relation_id: str | None = None
    subject_entity: EntityRef = Field(default_factory=EntityRef)
    relation_type: str = "unknown"
    object_entity: EntityRef = Field(default_factory=EntityRef)
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    evidence_locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    confidence: float = 0.0


class RiskFlag(BaseModel):
    risk_id: str | None = None
    risk_type: str = "unknown"
    risk_summary: str = ""
    severity: str = "low"
    time_horizon: str = "near_term"
    impact_channels: list[str] = Field(default_factory=list)
    evidence_locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    confidence: float = 0.0


class CatalystItem(BaseModel):
    catalyst_id: str | None = None
    catalyst_type: str = "unknown"
    expected_date: str | None = None
    description: str = ""
    potential_impact: str = ""
    confidence: float = 0.0


class AnalystQuestion(BaseModel):
    question_id: str | None = None
    related_event_id: str | None = None
    question: str = ""
    reason: str = ""
    priority: str = "medium"
    suggested_queries: list[str] = Field(default_factory=list)
    status: str = "open"


class CoverageGap(BaseModel):
    gap_id: str | None = None
    gap_type: str = "unknown"
    description: str = ""
    suggested_next_queries: list[str] = Field(default_factory=list)
    priority: str = "medium"


# --- Domain signals (spec 13.7 - 13.9) -------------------------------------


class CustomerSupplierSignal(BaseModel):
    signal_id: str | None = None
    signal_type: str = "unknown"  # new_customer | customer_loss | customer_order | ...
    customer_or_supplier: str = ""
    product: str | None = None
    business_meaning: str = ""
    impact_channels: list[str] = Field(default_factory=list)
    evidence_locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    confidence: float = 0.0


class PriceCostMarginSignal(BaseModel):
    signal_id: str | None = None
    signal_type: str = "unknown"  # product_price_up | raw_material_cost_up | ...
    product_or_material: str = ""
    value: float | None = None
    unit: str | None = None
    period: str | None = None
    direction: str = "unclear"
    evidence_locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    confidence: float = 0.0


class PolicyRegulatorySignal(BaseModel):
    signal_id: str | None = None
    policy_type: str = "unknown"  # subsidy | restriction | approval | tariff | ...
    issuer: str = ""
    effective_date: str | None = None
    affected_entities: list[str] = Field(default_factory=list)
    affected_products: list[str] = Field(default_factory=list)
    impact_channels: list[str] = Field(default_factory=list)
    summary: str = ""
    evidence_locator: EvidenceLocator = Field(default_factory=EvidenceLocator)
    confidence: float = 0.0


# --- Top-level bundle (spec 12.2) ------------------------------------------


class BundleExtraction(BaseModel):
    schema_version: str = "bundle_extraction_v0.3"
    source_link_id: str | None = None
    decision: str = "skip"
    overall_score: float = 0.0
    confidence: float = 0.0
    source_quality: SourceQuality = Field(default_factory=SourceQuality)
    brief: Brief = Field(default_factory=Brief)
    facts: list[FactItem] = Field(default_factory=list)
    metrics: list[MetricObservation] = Field(default_factory=list)
    events: list[EventCard] = Field(default_factory=list)
    relations: list[RelationRecord] = Field(default_factory=list)
    risks: list[RiskFlag] = Field(default_factory=list)
    catalysts: list[CatalystItem] = Field(default_factory=list)
    customer_supplier_signals: list[CustomerSupplierSignal] = Field(default_factory=list)
    price_cost_margin_signals: list[PriceCostMarginSignal] = Field(default_factory=list)
    policy_signals: list[PolicyRegulatorySignal] = Field(default_factory=list)
    analyst_questions: list[AnalystQuestion] = Field(default_factory=list)
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)


# --- Pipeline-internal transport objects -----------------------------------


class SearchHit(BaseModel):
    """A raw search engine result before triage."""

    query: str
    title: str = ""
    snippet: str = ""
    url: str
    domain: str = ""
    rank: int = 0
    provider: str = ""
    publish_time_guess: str | None = None
    query_family: str | None = None


class Passage(BaseModel):
    passage_id: str
    section: str
    text: str


class TriageResult(BaseModel):
    source_link_id: str
    triage_decision: Literal["read", "link_record_only", "skip_for_now"]
    read_priority: float = 0.0
    matched_signals: list[str] = Field(default_factory=list)
    need_model: bool = False
    suggested_task: str = "bundle_extraction"
    reason: str = ""
