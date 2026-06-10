"""Analyst Agent API (spec section 20).

Thin, stable facade used by analyst agents. ``collect_intelligence`` runs the
full pipeline; the ``get_*`` / ``search_*`` methods read structured results back
out of the store. This is the contract a downstream analyst agent codes against.
"""

from __future__ import annotations

from typing import Any

from mic.config import MICConfig, load_config
from mic.pipeline import Pipeline
from mic.store import Repository, get_database


class AnalystAPI:
    def __init__(self, config: MICConfig | None = None):
        self.config = config or load_config()
        self.pipeline = Pipeline(self.config)
        self.repo = Repository(get_database(self.config.database_url))

    # 20.1
    def collect_intelligence(self, target_id: str, task_profile: dict[str, Any],
                            model_policy_version: str | None = None,
                            query_plan_version: str | None = None) -> dict:
        return self.pipeline.collect_intelligence(
            target_id, task_profile,
            model_policy_version=model_policy_version,
            query_plan_version=query_plan_version)

    # 20.2
    def get_recent_events(self, target_id: str, since: str = "30d",
                         event_types: list[str] | None = None,
                         min_confidence: float = 0.0) -> list[dict]:
        return self.repo.get_recent_events(target_id, since, event_types, min_confidence)

    # 20.3
    def get_metric_observations(self, target_id: str, metrics: list[str] | None = None,
                               since: str = "60d") -> list[dict]:
        return self.repo.get_metric_observations(target_id, metrics, since)

    # 20.4
    def get_relations(self, target_id: str, relation_types: list[str] | None = None,
                     since: str = "180d") -> list[dict]:
        return self.repo.get_relations(target_id, relation_types, since)

    # 20.5
    def search_facts(self, target_id: str, query: str = "",
                    filters: dict[str, Any] | None = None) -> list[dict]:
        filters = filters or {}
        return self.repo.search_facts(
            target_id, query=query, fact_types=filters.get("fact_type"),
            since=filters.get("since"))

    # 20.6
    def get_risks(self, target_id: str, since: str = "90d",
                 severity: list[str] | None = None) -> list[dict]:
        return self.repo.get_risks(target_id, since, severity)

    # 20.7
    def get_catalysts(self, target_id: str, from_date: str | None = None,
                     to_date: str | None = None) -> list[dict]:
        return self.repo.get_catalysts(target_id, from_date, to_date)

    # 20.8
    def get_analyst_questions(self, target_id: str, priority: str | None = None,
                             status: str | None = "open") -> list[dict]:
        return self.repo.get_analyst_questions(target_id, priority, status)

    # 20.9
    def explain_source_analysis(self, source_link_id: str) -> dict:
        return self.repo.explain_source_analysis(source_link_id)

    # 20.10
    def get_coverage_gaps(self, target_id: str, priority: str | None = None,
                         status: str | None = "open") -> list[dict]:
        return self.repo.get_coverage_gaps(target_id, priority, status)

    # 22 - feedback
    def submit_feedback(self, feedback: dict[str, Any]) -> str:
        return self.repo.save_feedback(feedback)
