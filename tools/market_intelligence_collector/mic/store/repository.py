"""Repository: persistence + analyst query helpers.

Wraps the ORM with intent-revealing methods used by the pipeline and the
Analyst Agent API. Keeps SQLAlchemy details out of the rest of the codebase.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from mic import schemas
from mic.store import models as m
from mic.store.database import Database
from mic.utils import new_id, now, parse_time_window_days


def _to_dt(value: Any) -> datetime | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # --- Target profile ----------------------------------------------------

    def upsert_target_profile(self, profile: dict[str, Any]) -> None:
        with self.db.session() as s:
            row = s.get(m.TargetProfile, profile["target_id"])
            if row is None:
                row = m.TargetProfile(id=profile["target_id"], created_at=now())
                s.add(row)
            row.target_type = profile.get("type")
            row.canonical_name = profile.get("canonical_name")
            row.aliases = profile.get("aliases")
            row.products = profile.get("products")
            row.business_segments = profile.get("business_segments")
            row.customers = profile.get("known_customers") or profile.get("customers")
            row.suppliers = profile.get("known_suppliers") or profile.get("suppliers")
            row.competitors = profile.get("competitors")
            row.upstream_terms = profile.get("upstream_terms")
            row.downstream_terms = profile.get("downstream_terms")
            row.metadata_ = {
                k: v for k, v in profile.items()
                if k not in {
                    "target_id", "type", "canonical_name", "aliases", "products",
                    "business_segments", "known_customers", "customers",
                    "known_suppliers", "suppliers", "competitors",
                    "upstream_terms", "downstream_terms",
                }
            }
            row.updated_at = now()

    # --- Search run / queries ---------------------------------------------

    def create_search_run(
        self, target_id: str, task_profile: dict, budget_profile: dict,
        query_plan_version: str, model_policy_version: str,
    ) -> str:
        run_id = new_id("run")
        with self.db.session() as s:
            s.add(m.SearchRun(
                id=run_id, target_id=target_id, task_profile=task_profile,
                budget_profile=budget_profile, query_plan_version=query_plan_version,
                model_policy_version=model_policy_version, started_at=now(),
                status="running",
            ))
        return run_id

    def finish_search_run(self, run_id: str, status: str, summary: dict) -> None:
        with self.db.session() as s:
            row = s.get(m.SearchRun, run_id)
            if row:
                row.status = status
                row.summary = summary
                row.finished_at = now()

    def save_query(self, run_id: str, query: dict) -> str:
        qid = new_id("q")
        with self.db.session() as s:
            s.add(m.SearchQuery(
                id=qid, search_run_id=run_id, query_text=query["query_text"],
                query_family=query.get("query_family"),
                priority_score=query.get("priority_score"),
                language=query.get("language"), region=query.get("region"),
                expected_value_reason=query.get("expected_value_reason"),
                executed=query.get("executed", False), created_at=now(),
            ))
        return qid

    def mark_query_executed(self, query_id: str) -> None:
        with self.db.session() as s:
            row = s.get(m.SearchQuery, query_id)
            if row:
                row.executed = True

    # --- Source links ------------------------------------------------------

    def save_source_link(self, run_id: str, query_id: str, hit: schemas.SearchHit,
                         canonical_url: str, source_type: str) -> str:
        link_id = new_id("link")
        with self.db.session() as s:
            s.add(m.SourceLink(
                id=link_id, search_run_id=run_id, query_id=query_id,
                provider=hit.provider, rank=hit.rank, title=hit.title,
                snippet=hit.snippet, url=hit.url, canonical_url=canonical_url,
                domain=hit.domain, source_type=source_type,
                publish_time_guess=_to_dt(hit.publish_time_guess),
                retrieved_at=now(), read_status="pending",
                metadata_={"query_family": hit.query_family},
            ))
        return link_id

    def find_link_by_canonical(self, canonical_url: str) -> m.SourceLink | None:
        with self.db.session() as s:
            return s.scalars(
                select(m.SourceLink).where(m.SourceLink.canonical_url == canonical_url)
            ).first()

    def find_link_by_content_hash(self, content_hash: str) -> m.SourceLink | None:
        with self.db.session() as s:
            return s.scalars(
                select(m.SourceLink).where(m.SourceLink.content_hash == content_hash)
            ).first()

    def find_analyzed_link_by_canonical(
        self, canonical_url: str, target_id: str,
        exclude_run_id: str | None = None,
    ) -> m.SourceLink | None:
        """Latest already-analyzed link for this canonical URL (cross-run reuse).

        Only returns links that produced a MergedAnalysis for the same target, so
        the structured result can be cloned instead of re-reading / re-calling
        models (spec 15.1 caching contract).
        """
        if not canonical_url:
            return None
        with self.db.session() as s:
            stmt = (
                select(m.SourceLink)
                .join(m.MergedAnalysis,
                      m.MergedAnalysis.source_link_id == m.SourceLink.id)
                .where(m.SourceLink.canonical_url == canonical_url)
                .where(m.MergedAnalysis.target_id == target_id)
            )
            if exclude_run_id:
                stmt = stmt.where(m.SourceLink.search_run_id != exclude_run_id)
            return s.scalars(stmt.order_by(m.MergedAnalysis.created_at.desc())).first()

    def find_analyzed_link_by_content_hash(
        self, content_hash: str, target_id: str,
        exclude_link_id: str | None = None,
    ) -> m.SourceLink | None:
        """Latest already-analyzed link with the same body content hash."""
        if not content_hash:
            return None
        with self.db.session() as s:
            stmt = (
                select(m.SourceLink)
                .join(m.MergedAnalysis,
                      m.MergedAnalysis.source_link_id == m.SourceLink.id)
                .where(m.SourceLink.content_hash == content_hash)
                .where(m.MergedAnalysis.target_id == target_id)
            )
            if exclude_link_id:
                stmt = stmt.where(m.SourceLink.id != exclude_link_id)
            return s.scalars(stmt.order_by(m.MergedAnalysis.created_at.desc())).first()

    def update_link_triage(self, link_id: str, score: float, decision: str,
                           reason: str | None = None,
                           signals: list[str] | None = None,
                           need_model: bool | None = None) -> None:
        with self.db.session() as s:
            row = s.get(m.SourceLink, link_id)
            if row:
                row.triage_score = score
                row.triage_decision = decision
                # Persist explainability metadata so explain_source_analysis can
                # answer "why was this source selected?" (spec section 20).
                if reason is not None or signals is not None or need_model is not None:
                    meta = dict(row.metadata_ or {})
                    if reason is not None:
                        meta["triage_reason"] = reason
                    if signals is not None:
                        meta["matched_signals"] = list(signals)
                    if need_model is not None:
                        meta["need_model"] = bool(need_model)
                    row.metadata_ = meta

    def update_link_read(self, link_id: str, read_status: str,
                         content_hash: str | None, simhash: str | None) -> None:
        with self.db.session() as s:
            row = s.get(m.SourceLink, link_id)
            if row:
                row.read_status = read_status
                if content_hash:
                    row.content_hash = content_hash
                if simhash:
                    row.simhash = simhash

    def get_link(self, link_id: str) -> m.SourceLink | None:
        with self.db.session() as s:
            return s.get(m.SourceLink, link_id)

    # --- Read attempts -----------------------------------------------------

    def save_read_attempt(self, attempt: dict) -> str:
        aid = new_id("read")
        with self.db.session() as s:
            s.add(m.LinkReadAttempt(
                id=aid, source_link_id=attempt["source_link_id"],
                access_profile_id=attempt.get("access_profile_id"),
                read_status=attempt.get("read_status"),
                http_status=attempt.get("http_status"),
                content_type=attempt.get("content_type"),
                content_length=attempt.get("content_length"),
                extracted_title=attempt.get("extracted_title"),
                extracted_publish_time=_to_dt(attempt.get("extracted_publish_time")),
                content_hash=attempt.get("content_hash"),
                selected_passage_count=attempt.get("selected_passage_count"),
                failure_reason=attempt.get("failure_reason"), created_at=now(),
            ))
        return aid

    # --- Model runs / outputs ---------------------------------------------

    def save_model_run(self, run: dict) -> str:
        rid = new_id("mrun")
        with self.db.session() as s:
            s.add(m.ModelRun(
                id=rid, source_link_id=run.get("source_link_id"),
                task_name=run.get("task_name"), call_mode=run.get("call_mode"),
                provider_type=run.get("provider_type"), provider=run.get("provider"),
                model_name=run.get("model_name"), model_config_id=run.get("model_config_id"),
                model_policy_version=run.get("model_policy_version"),
                prompt_version=run.get("prompt_version"),
                schema_version=run.get("schema_version"),
                input_chars=run.get("input_chars"), input_tokens=run.get("input_tokens"),
                output_tokens=run.get("output_tokens"),
                reasoning_tokens=run.get("reasoning_tokens"),
                cached_tokens=run.get("cached_tokens"),
                estimated_cost=run.get("estimated_cost"), latency_ms=run.get("latency_ms"),
                status=run.get("status"), error_type=run.get("error_type"),
                error_message=run.get("error_message"),
                provider_request_id=run.get("provider_request_id"), created_at=now(),
            ))
        return rid

    def save_model_output(self, output: dict) -> str:
        oid = new_id("mout")
        with self.db.session() as s:
            s.add(m.ModelOutput(
                id=oid, model_run_id=output.get("model_run_id"),
                source_link_id=output.get("source_link_id"),
                output_json=output.get("output_json"),
                schema_valid=output.get("schema_valid"),
                validation_errors=output.get("validation_errors"),
                decision=output.get("decision"), overall_score=output.get("overall_score"),
                confidence=output.get("confidence"), created_at=now(),
            ))
        return oid

    # --- Merged analysis + structured objects -----------------------------

    def save_merged_analysis(self, target_id: str, source_link_id: str,
                            bundle: schemas.BundleExtraction, merge_meta: dict,
                            search_run_id: str | None = None) -> str:
        merged_id = new_id("merged")
        with self.db.session() as s:
            s.add(m.MergedAnalysis(
                id=merged_id, source_link_id=source_link_id, target_id=target_id,
                decision=bundle.decision, overall_score=bundle.overall_score,
                confidence=bundle.confidence,
                disagreement_level=merge_meta.get("disagreement_level"),
                merge_method=merge_meta.get("merge_method"),
                model_outputs=merge_meta.get("model_outputs"),
                field_conflicts=merge_meta.get("field_conflicts"), created_at=now(),
            ))

            b = bundle.brief
            s.add(m.AnalysisBrief(
                id=new_id("brief"), merged_analysis_id=merged_id,
                source_link_id=source_link_id, target_id=target_id,
                one_sentence=b.one_sentence, what_happened=b.what_happened,
                why_it_matters=b.why_it_matters,
                affected_business_lines=b.affected_business_lines,
                impact_channels=b.impact_channels, time_horizon=b.time_horizon,
                confidence=bundle.confidence, created_at=now(),
            ))

            for fact in bundle.facts:
                s.add(m.FactItemRow(
                    id=new_id("fact"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    fact_type=fact.fact_type, fact_statement=fact.fact_statement,
                    entities=fact.entities, metrics=fact.metrics, period=fact.period,
                    direction=fact.direction,
                    evidence_locator=fact.evidence_locator.model_dump(),
                    confidence=fact.confidence, created_at=now(),
                ))

            for metric in bundle.metrics:
                s.add(m.MetricObservationRow(
                    id=new_id("metric"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    metric_name=metric.metric_name, metric_value=metric.metric_value,
                    unit=metric.unit, period=metric.period, scope=metric.scope,
                    comparison=metric.comparison, interpretation=metric.interpretation,
                    impact_channels=metric.impact_channels, confidence=metric.confidence,
                    created_at=now(),
                ))

            for event in bundle.events:
                s.add(m.EventCardRow(
                    id=new_id("evt"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    event_type=event.event_type, event_date=_to_dt(event.event_date),
                    summary=event.summary, entities=event.entities, metrics=event.metrics,
                    impact=event.impact.model_dump(),
                    source_corroboration_status=event.source_corroboration_status,
                    confidence=event.confidence, created_at=now(),
                ))

            for rel in bundle.relations:
                s.add(m.RelationRecordRow(
                    id=new_id("rel"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    subject_entity=rel.subject_entity.model_dump(),
                    relation_type=rel.relation_type,
                    object_entity=rel.object_entity.model_dump(),
                    qualifiers=rel.qualifiers,
                    evidence_locator=rel.evidence_locator.model_dump(),
                    confidence=rel.confidence, created_at=now(),
                ))

            for risk in bundle.risks:
                s.add(m.RiskFlagRow(
                    id=new_id("risk"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    risk_type=risk.risk_type, risk_summary=risk.risk_summary,
                    severity=risk.severity, time_horizon=risk.time_horizon,
                    impact_channels=risk.impact_channels, confidence=risk.confidence,
                    created_at=now(),
                ))

            for cat in bundle.catalysts:
                s.add(m.CatalystItemRow(
                    id=new_id("cat"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    catalyst_type=cat.catalyst_type,
                    expected_date=_to_dt(cat.expected_date), description=cat.description,
                    potential_impact=cat.potential_impact, confidence=cat.confidence,
                    created_at=now(),
                ))

            for cs in bundle.customer_supplier_signals:
                s.add(m.CustomerSupplierSignalRow(
                    id=new_id("cs"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    signal_type=cs.signal_type,
                    customer_or_supplier=cs.customer_or_supplier, product=cs.product,
                    business_meaning=cs.business_meaning,
                    impact_channels=cs.impact_channels, confidence=cs.confidence,
                    created_at=now(),
                ))

            for pcm in bundle.price_cost_margin_signals:
                s.add(m.PriceCostMarginSignalRow(
                    id=new_id("pcm"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    signal_type=pcm.signal_type,
                    product_or_material=pcm.product_or_material, value=pcm.value,
                    unit=pcm.unit, period=pcm.period, direction=pcm.direction,
                    confidence=pcm.confidence, created_at=now(),
                ))

            for pol in bundle.policy_signals:
                s.add(m.PolicyRegulatorySignalRow(
                    id=new_id("pol"), merged_analysis_id=merged_id,
                    source_link_id=source_link_id, target_id=target_id,
                    policy_type=pol.policy_type, issuer=pol.issuer,
                    effective_date=_to_dt(pol.effective_date),
                    affected_entities=pol.affected_entities,
                    affected_products=pol.affected_products,
                    impact_channels=pol.impact_channels, summary=pol.summary,
                    confidence=pol.confidence, created_at=now(),
                ))

            for q in bundle.analyst_questions:
                s.add(m.AnalystQuestionRow(
                    id=new_id("q"), source_link_id=source_link_id, target_id=target_id,
                    related_event_id=q.related_event_id, question=q.question,
                    reason=q.reason, priority=q.priority,
                    suggested_queries=q.suggested_queries, status=q.status,
                    created_at=now(),
                ))

            # Bundle-level coverage gaps are first-class structured outputs in
            # spec §13.13/§16. Persist them with the current run id so
            # get_coverage_gaps() and the E2E DB counts reflect what the model
            # actually found missing for this source. The table is run/target
            # scoped by design and does not store raw content.
            for gap in bundle.coverage_gaps:
                s.add(m.CoverageGapRow(
                    id=new_id("gap"), search_run_id=search_run_id, target_id=target_id,
                    gap_type=gap.gap_type, description=gap.description,
                    suggested_next_queries=gap.suggested_next_queries,
                    priority=gap.priority, status="open", created_at=now(),
                ))
        return merged_id

    def save_coverage_gaps(self, run_id: str, target_id: str,
                          gaps: Sequence[schemas.CoverageGap]) -> None:
        with self.db.session() as s:
            for gap in gaps:
                s.add(m.CoverageGapRow(
                    id=new_id("gap"), search_run_id=run_id, target_id=target_id,
                    gap_type=gap.gap_type, description=gap.description,
                    suggested_next_queries=gap.suggested_next_queries,
                    priority=gap.priority, status="open", created_at=now(),
                ))

    def clone_latest_analysis(
        self, previous_source_link_id: str, new_source_link_id: str, target_id: str,
    ) -> dict[str, int]:
        """Clone the latest merged structured objects from one link to another.

        Implements the canonical/content-hash cache contract without storing raw
        text: the new run gets its own structured rows pointing at the new
        ``source_link_id`` while preserving merge metadata and values. Returns the
        per-type counts of cloned rows so the pipeline can tally reused output.
        """
        counts = {
            "briefs": 0, "facts": 0, "metrics": 0, "events": 0, "relations": 0,
            "risks": 0, "catalysts": 0, "customer_supplier_signals": 0,
            "price_cost_margin_signals": 0, "policy_signals": 0,
            "analyst_questions": 0,
        }
        with self.db.session() as s:
            merged = s.scalars(
                select(m.MergedAnalysis)
                .where(m.MergedAnalysis.source_link_id == previous_source_link_id)
                .order_by(m.MergedAnalysis.created_at.desc())
            ).first()
            if merged is None:
                return counts
            new_merged_id = new_id("merged")
            s.add(m.MergedAnalysis(
                id=new_merged_id, source_link_id=new_source_link_id, target_id=target_id,
                decision=merged.decision, overall_score=merged.overall_score,
                confidence=merged.confidence, disagreement_level=merged.disagreement_level,
                merge_method=f"cache_reuse:{merged.merge_method or 'unknown'}",
                model_outputs=merged.model_outputs, field_conflicts=merged.field_conflicts,
                created_at=now(),
            ))

            brief = s.scalars(select(m.AnalysisBrief).where(
                m.AnalysisBrief.source_link_id == previous_source_link_id)).first()
            if brief:
                s.add(m.AnalysisBrief(
                    id=new_id("brief"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    one_sentence=brief.one_sentence, what_happened=brief.what_happened,
                    why_it_matters=brief.why_it_matters,
                    affected_business_lines=brief.affected_business_lines,
                    impact_channels=brief.impact_channels, time_horizon=brief.time_horizon,
                    confidence=brief.confidence, created_at=now(),
                ))
                counts["briefs"] += 1

            for fact in s.scalars(select(m.FactItemRow).where(
                    m.FactItemRow.source_link_id == previous_source_link_id)).all():
                s.add(m.FactItemRow(
                    id=new_id("fact"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    fact_type=fact.fact_type, fact_statement=fact.fact_statement,
                    entities=fact.entities, metrics=fact.metrics, period=fact.period,
                    direction=fact.direction, evidence_locator=fact.evidence_locator,
                    confidence=fact.confidence, created_at=now(),
                ))
                counts["facts"] += 1

            for metric in s.scalars(select(m.MetricObservationRow).where(
                    m.MetricObservationRow.source_link_id == previous_source_link_id)).all():
                s.add(m.MetricObservationRow(
                    id=new_id("metric"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    metric_name=metric.metric_name, metric_value=metric.metric_value,
                    unit=metric.unit, period=metric.period, scope=metric.scope,
                    comparison=metric.comparison, interpretation=metric.interpretation,
                    impact_channels=metric.impact_channels,
                    confidence=metric.confidence, created_at=now(),
                ))
                counts["metrics"] += 1

            for event in s.scalars(select(m.EventCardRow).where(
                    m.EventCardRow.source_link_id == previous_source_link_id)).all():
                s.add(m.EventCardRow(
                    id=new_id("evt"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    event_type=event.event_type, event_date=event.event_date,
                    summary=event.summary, entities=event.entities, metrics=event.metrics,
                    impact=event.impact,
                    source_corroboration_status=event.source_corroboration_status,
                    confidence=event.confidence, created_at=now(),
                ))
                counts["events"] += 1

            for rel in s.scalars(select(m.RelationRecordRow).where(
                    m.RelationRecordRow.source_link_id == previous_source_link_id)).all():
                s.add(m.RelationRecordRow(
                    id=new_id("rel"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    subject_entity=rel.subject_entity, relation_type=rel.relation_type,
                    object_entity=rel.object_entity, qualifiers=rel.qualifiers,
                    evidence_locator=rel.evidence_locator, confidence=rel.confidence,
                    created_at=now(),
                ))
                counts["relations"] += 1

            for risk in s.scalars(select(m.RiskFlagRow).where(
                    m.RiskFlagRow.source_link_id == previous_source_link_id)).all():
                s.add(m.RiskFlagRow(
                    id=new_id("risk"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    risk_type=risk.risk_type, risk_summary=risk.risk_summary,
                    severity=risk.severity, time_horizon=risk.time_horizon,
                    impact_channels=risk.impact_channels, confidence=risk.confidence,
                    created_at=now(),
                ))
                counts["risks"] += 1

            for cat in s.scalars(select(m.CatalystItemRow).where(
                    m.CatalystItemRow.source_link_id == previous_source_link_id)).all():
                s.add(m.CatalystItemRow(
                    id=new_id("cat"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    catalyst_type=cat.catalyst_type, expected_date=cat.expected_date,
                    description=cat.description, potential_impact=cat.potential_impact,
                    confidence=cat.confidence, created_at=now(),
                ))
                counts["catalysts"] += 1

            for cs in s.scalars(select(m.CustomerSupplierSignalRow).where(
                    m.CustomerSupplierSignalRow.source_link_id == previous_source_link_id)).all():
                s.add(m.CustomerSupplierSignalRow(
                    id=new_id("cs"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    signal_type=cs.signal_type,
                    customer_or_supplier=cs.customer_or_supplier, product=cs.product,
                    business_meaning=cs.business_meaning,
                    impact_channels=cs.impact_channels, confidence=cs.confidence,
                    created_at=now(),
                ))
                counts["customer_supplier_signals"] += 1

            for pcm in s.scalars(select(m.PriceCostMarginSignalRow).where(
                    m.PriceCostMarginSignalRow.source_link_id == previous_source_link_id)).all():
                s.add(m.PriceCostMarginSignalRow(
                    id=new_id("pcm"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    signal_type=pcm.signal_type,
                    product_or_material=pcm.product_or_material, value=pcm.value,
                    unit=pcm.unit, period=pcm.period, direction=pcm.direction,
                    confidence=pcm.confidence, created_at=now(),
                ))
                counts["price_cost_margin_signals"] += 1

            for pol in s.scalars(select(m.PolicyRegulatorySignalRow).where(
                    m.PolicyRegulatorySignalRow.source_link_id == previous_source_link_id)).all():
                s.add(m.PolicyRegulatorySignalRow(
                    id=new_id("pol"), merged_analysis_id=new_merged_id,
                    source_link_id=new_source_link_id, target_id=target_id,
                    policy_type=pol.policy_type, issuer=pol.issuer,
                    effective_date=pol.effective_date,
                    affected_entities=pol.affected_entities,
                    affected_products=pol.affected_products,
                    impact_channels=pol.impact_channels, summary=pol.summary,
                    confidence=pol.confidence, created_at=now(),
                ))
                counts["policy_signals"] += 1

            for q in s.scalars(select(m.AnalystQuestionRow).where(
                    m.AnalystQuestionRow.source_link_id == previous_source_link_id)).all():
                s.add(m.AnalystQuestionRow(
                    id=new_id("q"), source_link_id=new_source_link_id, target_id=target_id,
                    related_event_id=q.related_event_id, question=q.question,
                    reason=q.reason, priority=q.priority,
                    suggested_queries=q.suggested_queries, status=q.status,
                    created_at=now(),
                ))
                counts["analyst_questions"] += 1
        return counts

    def save_feedback(self, feedback: dict) -> str:
        fid = new_id("fb")
        with self.db.session() as s:
            s.add(m.Feedback(
                id=fid, object_type=feedback.get("object_type"),
                object_id=feedback.get("object_id"),
                useful_for_analysis=feedback.get("useful_for_analysis"),
                correct=feedback.get("correct"),
                impact_direction_correct=feedback.get("impact_direction_correct"),
                missing_fields=feedback.get("missing_fields"),
                notes=feedback.get("notes"),
                model_config_id=feedback.get("model_config_id"),
                query_family=feedback.get("query_family"),
                source_type=feedback.get("source_type"), created_at=now(),
            ))
        return fid

    # --- feedback-driven optimization (spec 22.2) -------------------------

    @staticmethod
    def _score_from_feedback(rows: list) -> float:
        """Map correct/useful ratios to a multiplier in [0.5, 1.2]."""
        if not rows:
            return 1.0
        good = sum(1 for r in rows
                   if (r.correct is True) or (r.useful_for_analysis is True))
        ratio = good / len(rows)
        return round(0.5 + 0.7 * ratio, 3)

    def model_feedback_scores(self) -> dict[str, float]:
        """Per-model historical feedback score, keyed by model_config_id."""
        with self.db.session() as s:
            rows = s.scalars(select(m.Feedback).where(
                m.Feedback.model_config_id.is_not(None))).all()
        grouped: dict[str, list] = {}
        for r in rows:
            grouped.setdefault(r.model_config_id, []).append(r)
        return {mid: self._score_from_feedback(rs) for mid, rs in grouped.items()}

    def family_feedback_weights(self) -> dict[str, float]:
        with self.db.session() as s:
            rows = s.scalars(select(m.Feedback).where(
                m.Feedback.query_family.is_not(None))).all()
        grouped: dict[str, list] = {}
        for r in rows:
            grouped.setdefault(r.query_family, []).append(r)
        return {fam: self._score_from_feedback(rs) for fam, rs in grouped.items()}

    def source_type_feedback_weights(self) -> dict[str, float]:
        with self.db.session() as s:
            rows = s.scalars(select(m.Feedback).where(
                m.Feedback.source_type.is_not(None))).all()
        grouped: dict[str, list] = {}
        for r in rows:
            grouped.setdefault(r.source_type, []).append(r)
        return {st: self._score_from_feedback(rs) for st, rs in grouped.items()}

    # --- Analyst queries (spec section 20) --------------------------------

    @staticmethod
    def _since_dt(since: str | None) -> datetime | None:
        days = parse_time_window_days(since)
        if days is None:
            return None
        return now() - timedelta(days=days)

    def get_recent_events(self, target_id: str, since: str | None = None,
                         event_types: list[str] | None = None,
                         min_confidence: float = 0.0) -> list[dict]:
        with self.db.session() as s:
            stmt = select(m.EventCardRow).where(m.EventCardRow.target_id == target_id)
            since_dt = self._since_dt(since)
            if since_dt is not None:
                stmt = stmt.where(m.EventCardRow.created_at >= since_dt)
            if event_types:
                stmt = stmt.where(m.EventCardRow.event_type.in_(event_types))
            stmt = stmt.where(m.EventCardRow.confidence >= min_confidence)
            rows = s.scalars(stmt.order_by(m.EventCardRow.confidence.desc())).all()
            return [self._event_to_dict(r) for r in rows]

    def get_metric_observations(self, target_id: str, metrics: list[str] | None = None,
                               since: str | None = None) -> list[dict]:
        with self.db.session() as s:
            stmt = select(m.MetricObservationRow).where(
                m.MetricObservationRow.target_id == target_id)
            since_dt = self._since_dt(since)
            if since_dt is not None:
                stmt = stmt.where(m.MetricObservationRow.created_at >= since_dt)
            if metrics:
                stmt = stmt.where(m.MetricObservationRow.metric_name.in_(metrics))
            rows = s.scalars(stmt.order_by(m.MetricObservationRow.created_at.desc())).all()
            return [{
                "metric_id": r.id, "metric_name": r.metric_name,
                "metric_value": r.metric_value, "unit": r.unit, "period": r.period,
                "scope": r.scope, "comparison": r.comparison,
                "interpretation": r.interpretation, "impact_channels": r.impact_channels,
                "confidence": r.confidence, "source_link_id": r.source_link_id,
            } for r in rows]

    def get_relations(self, target_id: str, relation_types: list[str] | None = None,
                     since: str | None = None) -> list[dict]:
        with self.db.session() as s:
            stmt = select(m.RelationRecordRow).where(
                m.RelationRecordRow.target_id == target_id)
            since_dt = self._since_dt(since)
            if since_dt is not None:
                stmt = stmt.where(m.RelationRecordRow.created_at >= since_dt)
            if relation_types:
                stmt = stmt.where(m.RelationRecordRow.relation_type.in_(relation_types))
            rows = s.scalars(stmt.order_by(m.RelationRecordRow.confidence.desc())).all()
            return [{
                "relation_id": r.id, "subject_entity": r.subject_entity,
                "relation_type": r.relation_type, "object_entity": r.object_entity,
                "qualifiers": r.qualifiers, "confidence": r.confidence,
                "source_link_id": r.source_link_id,
            } for r in rows]

    def search_facts(self, target_id: str, query: str | None = None,
                    fact_types: list[str] | None = None,
                    since: str | None = None) -> list[dict]:
        with self.db.session() as s:
            stmt = select(m.FactItemRow).where(m.FactItemRow.target_id == target_id)
            since_dt = self._since_dt(since)
            if since_dt is not None:
                stmt = stmt.where(m.FactItemRow.created_at >= since_dt)
            if fact_types:
                stmt = stmt.where(m.FactItemRow.fact_type.in_(fact_types))
            rows = s.scalars(stmt.order_by(m.FactItemRow.confidence.desc())).all()
            results = [{
                "fact_id": r.id, "fact_type": r.fact_type,
                "fact_statement": r.fact_statement, "entities": r.entities,
                "metrics": r.metrics, "period": r.period, "direction": r.direction,
                "confidence": r.confidence, "source_link_id": r.source_link_id,
            } for r in rows]
            if query:
                terms = [t for t in query.split() if t]
                results = [
                    r for r in results
                    if any(t in (r["fact_statement"] or "") for t in terms)
                ] or results
            return results

    def get_risks(self, target_id: str, since: str | None = None,
                 severity: list[str] | None = None) -> list[dict]:
        with self.db.session() as s:
            stmt = select(m.RiskFlagRow).where(m.RiskFlagRow.target_id == target_id)
            since_dt = self._since_dt(since)
            if since_dt is not None:
                stmt = stmt.where(m.RiskFlagRow.created_at >= since_dt)
            if severity:
                stmt = stmt.where(m.RiskFlagRow.severity.in_(severity))
            rows = s.scalars(stmt.order_by(m.RiskFlagRow.confidence.desc())).all()
            return [{
                "risk_id": r.id, "risk_type": r.risk_type,
                "risk_summary": r.risk_summary, "severity": r.severity,
                "time_horizon": r.time_horizon, "impact_channels": r.impact_channels,
                "confidence": r.confidence, "source_link_id": r.source_link_id,
            } for r in rows]

    def get_catalysts(self, target_id: str, from_date: str | None = None,
                     to_date: str | None = None) -> list[dict]:
        with self.db.session() as s:
            stmt = select(m.CatalystItemRow).where(
                m.CatalystItemRow.target_id == target_id)
            fd, td = _to_dt(from_date), _to_dt(to_date)
            if fd is not None:
                stmt = stmt.where(m.CatalystItemRow.expected_date >= fd)
            if td is not None:
                stmt = stmt.where(m.CatalystItemRow.expected_date <= td)
            rows = s.scalars(stmt.order_by(m.CatalystItemRow.expected_date.asc())).all()
            return [{
                "catalyst_id": r.id, "catalyst_type": r.catalyst_type,
                "expected_date": r.expected_date.isoformat() if r.expected_date else None,
                "description": r.description, "potential_impact": r.potential_impact,
                "confidence": r.confidence, "source_link_id": r.source_link_id,
            } for r in rows]

    def get_analyst_questions(self, target_id: str, priority: str | None = None,
                             status: str | None = None) -> list[dict]:
        with self.db.session() as s:
            stmt = select(m.AnalystQuestionRow).where(
                m.AnalystQuestionRow.target_id == target_id)
            if priority:
                stmt = stmt.where(m.AnalystQuestionRow.priority == priority)
            if status:
                stmt = stmt.where(m.AnalystQuestionRow.status == status)
            rows = s.scalars(stmt.order_by(m.AnalystQuestionRow.created_at.desc())).all()
            return [{
                "question_id": r.id, "question": r.question, "reason": r.reason,
                "priority": r.priority, "status": r.status,
                "suggested_queries": r.suggested_queries,
                "related_event_id": r.related_event_id,
                "source_link_id": r.source_link_id,
            } for r in rows]

    def get_coverage_gaps(self, target_id: str, priority: str | None = None,
                          status: str | None = None) -> list[dict]:
        with self.db.session() as s:
            stmt = select(m.CoverageGapRow).where(
                m.CoverageGapRow.target_id == target_id)
            if priority:
                stmt = stmt.where(m.CoverageGapRow.priority == priority)
            if status:
                stmt = stmt.where(m.CoverageGapRow.status == status)
            rows = s.scalars(stmt.order_by(m.CoverageGapRow.created_at.desc())).all()
            return [{
                "gap_id": r.id, "gap_type": r.gap_type, "description": r.description,
                "suggested_next_queries": r.suggested_next_queries,
                "priority": r.priority, "status": r.status,
                "search_run_id": r.search_run_id,
            } for r in rows]

    def count_rows_for_run(self, run_id: str) -> dict[str, Any]:
        """Row counts per table for a run; used by the e2e validation harness."""
        with self.db.session() as s:
            link_ids = list(s.scalars(select(m.SourceLink.id).where(
                m.SourceLink.search_run_id == run_id)).all())
            merged_ids = list(s.scalars(select(m.MergedAnalysis.id).where(
                m.MergedAnalysis.source_link_id.in_(link_ids))).all()) if link_ids else []

            def count(cls, where=None) -> int:
                stmt = select(func.count()).select_from(cls)
                if where is not None:
                    stmt = stmt.where(where)
                return int(s.scalar(stmt) or 0)

            def by_link(cls) -> int:
                return count(cls, cls.source_link_id.in_(link_ids)) if link_ids else 0

            def by_merged(cls) -> int:
                return count(cls, cls.merged_analysis_id.in_(merged_ids)) if merged_ids else 0

            return {
                "source_links": count(m.SourceLink, m.SourceLink.search_run_id == run_id),
                "queries": count(m.SearchQuery, m.SearchQuery.search_run_id == run_id),
                "read_attempts": by_link(m.LinkReadAttempt),
                "model_runs": by_link(m.ModelRun),
                "model_outputs": by_link(m.ModelOutput),
                "merged_analysis": by_link(m.MergedAnalysis),
                "briefs": by_link(m.AnalysisBrief),
                "facts": by_merged(m.FactItemRow),
                "metrics": by_merged(m.MetricObservationRow),
                "events": by_merged(m.EventCardRow),
                "relations": by_merged(m.RelationRecordRow),
                "risks": by_merged(m.RiskFlagRow),
                "catalysts": by_merged(m.CatalystItemRow),
                "customer_supplier_signals": by_merged(m.CustomerSupplierSignalRow),
                "price_cost_margin_signals": by_merged(m.PriceCostMarginSignalRow),
                "policy_signals": by_merged(m.PolicyRegulatorySignalRow),
                "analyst_questions": by_link(m.AnalystQuestionRow),
                "coverage_gaps": count(m.CoverageGapRow,
                                       m.CoverageGapRow.search_run_id == run_id),
            }

    def source_links_for_run(self, run_id: str, decision: str | None = None,
                             limit: int = 20) -> list[dict[str, Any]]:
        with self.db.session() as s:
            stmt = select(m.SourceLink).where(m.SourceLink.search_run_id == run_id)
            if decision is not None:
                stmt = stmt.where(m.SourceLink.triage_decision == decision)
            rows = s.scalars(
                stmt.order_by(m.SourceLink.triage_score.desc()).limit(limit)).all()
            return [{
                "source_link_id": r.id, "title": r.title, "url": r.url,
                "source_type": r.source_type, "triage_score": r.triage_score,
                "triage_decision": r.triage_decision, "read_status": r.read_status,
                "metadata": r.metadata_ or {},
            } for r in rows]

    def explain_source_analysis(self, source_link_id: str) -> dict:
        with self.db.session() as s:
            link = s.get(m.SourceLink, source_link_id)
            merged = s.scalars(select(m.MergedAnalysis).where(
                m.MergedAnalysis.source_link_id == source_link_id)).first()
            facts = s.scalars(select(m.FactItemRow).where(
                m.FactItemRow.source_link_id == source_link_id)).all()
            metrics = s.scalars(select(m.MetricObservationRow).where(
                m.MetricObservationRow.source_link_id == source_link_id)).all()
            events = s.scalars(select(m.EventCardRow).where(
                m.EventCardRow.source_link_id == source_link_id)).all()
            relations = s.scalars(select(m.RelationRecordRow).where(
                m.RelationRecordRow.source_link_id == source_link_id)).all()
            risks = s.scalars(select(m.RiskFlagRow).where(
                m.RiskFlagRow.source_link_id == source_link_id)).all()
            questions = s.scalars(select(m.AnalystQuestionRow).where(
                m.AnalystQuestionRow.source_link_id == source_link_id)).all()
            runs = s.scalars(select(m.ModelRun).where(
                m.ModelRun.source_link_id == source_link_id)).all()
            return {
                "source": {
                    "title": link.title if link else None,
                    "url": link.url if link else None,
                    "source_type": link.source_type if link else None,
                    "publish_time": (link.publish_time_guess.isoformat()
                                     if link and link.publish_time_guess else None),
                } if link else {},
                "why_selected": (link.metadata_ or {}).get("triage_reason")
                if link else None,
                "matched_signals": (link.metadata_ or {}).get("matched_signals", [])
                if link else [],
                "triage_decision": link.triage_decision if link else None,
                "models_used": [r.model_config_id for r in runs],
                "merged_decision": merged.decision if merged else None,
                "facts": [f.fact_statement for f in facts],
                "metrics": [{"name": x.metric_name, "value": x.metric_value} for x in metrics],
                "events": [e.summary for e in events],
                "relations": [{"subject": r.subject_entity, "type": r.relation_type,
                               "object": r.object_entity} for r in relations],
                "risks": [r.risk_summary for r in risks],
                "questions": [q.question for q in questions],
            }

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _event_to_dict(r: m.EventCardRow) -> dict:
        return {
            "event_id": r.id, "event_type": r.event_type,
            "event_date": r.event_date.isoformat() if r.event_date else None,
            "summary": r.summary, "entities": r.entities, "metrics": r.metrics,
            "impact": r.impact,
            "source_corroboration_status": r.source_corroboration_status,
            "confidence": r.confidence, "source_link_id": r.source_link_id,
        }
