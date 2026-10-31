"""Storage contract, cache and legacy SQLite migration regressions."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import Column, MetaData, Table, inspect, select

from mic.config import MICConfig
from mic.evidence_review import EvidenceReview
from mic.merge import ModelContribution, MultiModelMerger
from mic.schemas import BundleExtraction, Passage
from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository


def fixture():
    return BundleExtraction.model_validate({
        "schema_version": "bundle_extraction_v0.3", "decision": "save_structured", "overall_score": 80, "confidence": .8,
        "brief": {"one_sentence": "合成存储测试样本", "uncertainty": "当前材料未包含交易对手确认，待核查。"},
        "facts": [{"fact_type": "price", "fact_statement": "合成报价", "confidence": .8,
            "metrics": {"amount": None, "unit_price": .518, "unit_price_unit": "元/Wh"},
            "evidence_locator": {"passage_id": "p2", "section": "报价", "table_id": "table-1"}}],
        "metrics": [{"metric_name": "容量", "metric_value": 360, "unit": "MWh", "confidence": .8,
            "scope": {"value_evidence": {"operator": "+", "operands": [120, 240], "passage_id": "p2"}},
            "evidence_locator": {"passage_id": "p2", "section": "一标段", "table_id": None}}],
        "events": [{"event_type": "major_order", "summary": "合成订单", "event_date": None,
            "entities": {"subject": "测试公司", "counterparty": None, "counterparty_candidate": "候选公司"},
            "metrics": {"amount": 41416220, "amount_unit": "元", "amount_raw": 4141.622,
                        "currency": "CNY", "event_date_review": {"status": "pending_review", "candidate": "2026-09-15"}},
            "evidence_locator": {"passage_id": "p3", "section": "二标段"}, "confidence": .8}],
    })


class StorageEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("MIC_STORAGE_TEST_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.url = "sqlite:///" + str(Path(self.temp.name) / "test.db")
        self.db = Database(self.url)
        self.addCleanup(lambda: self.db.engine.dispose())
        self.db.create_all()
        self.repo = Repository(self.db)
        self.bundle = fixture()

    def save(self):
        self.repo.save_merged_analysis("target", "source", self.bundle, {"merge_method": "unit_fixture"}, allow_legacy_write=True)

    def test_new_columns_nullable(self):
        for table, field in (("event_card", "evidence_locator"), ("metric_observation", "evidence_locator"),
                             ("analysis_brief", "uncertainty")):
            columns = {c["name"]: c for c in inspect(self.db.engine).get_columns(table)}
            self.assertIn(field, columns)
            self.assertTrue(columns[field]["nullable"])

    def test_event_locator_and_review_fields(self):
        self.save()
        actual = self.repo.get_recent_events("target")[0]
        self.assertEqual(actual["evidence_locator"], self.bundle.events[0].evidence_locator.model_dump())
        self.assertEqual(actual["metrics"], self.bundle.events[0].metrics)
        self.assertEqual(actual["entities"], self.bundle.events[0].entities)
        self.assertIsNone(actual["event_date"])

    def test_metric_locator_and_derivation(self):
        self.save()
        actual = self.repo.get_metric_observations("target")[0]
        self.assertEqual(actual["evidence_locator"], self.bundle.metrics[0].evidence_locator.model_dump())
        self.assertEqual(actual["scope"], self.bundle.metrics[0].scope)

    def generated_metric_diagnostics(self):
        metric = self.bundle.metrics[0]
        metric.scope = {}
        metric.period = "2026-09-15"
        passages = [Passage(passage_id="p2", section="body", text="120MWh + 240MWh")]
        EvidenceReview(self.bundle, passages, []).check_fields()
        self.assertEqual(metric.scope["value_evidence"]["operands"], [120, 240])
        self.assertEqual(metric.scope["value_evidence"]["value"], 360)
        self.assertEqual(metric.scope["value_evidence"]["passage_id"], "p2")
        self.assertEqual(metric.scope["period_review"]["status"], "pending_review")
        self.assertIsNone(metric.period)
        return copy.deepcopy(metric.scope)

    def test_system_generated_diagnostics_survive_read_and_cache_without_new_approval(self):
        diagnostics = self.generated_metric_diagnostics()
        original = copy.deepcopy(self.bundle.model_dump())
        self.save()
        result = self.repo.clone_latest_analysis("source", "cached-source", "cache-target")
        self.assertNotIn("cache_reuse_rejected", result)
        self.assertEqual(result["metrics"], 1)
        for target in ("target", "cache-target"):
            metric, = self.repo.get_metric_observations(target)
            self.assertEqual(metric["scope"], diagnostics)
            self.assertEqual(metric["metric_value"], 360)
            self.assertIsNone(metric["period"])
            self.assertFalse(metric.get("content_review"))
            self.assertFalse(metric["scope"].get("usable_as_price_benchmark"))
        self.assertEqual(self.bundle.model_dump(), original)
        with self.db.session() as session:
            stored = session.scalars(select(m.MetricObservationRow).where(
                m.MetricObservationRow.source_link_id == "source")).one()
            self.assertEqual(stored.scope, diagnostics)
            self.assertFalse(stored.content_review)

    def test_system_diagnostics_do_not_authorize_unreviewed_business_scope(self):
        diagnostics = self.generated_metric_diagnostics()
        self.bundle.metrics[0].scope["region"] = "Synthetic Region"
        original = copy.deepcopy(self.bundle.metrics[0].scope)
        self.save()
        actual, = self.repo.get_metric_observations("target")
        self.assertEqual(actual["metric_value"], 360)
        self.assertIsNone(actual["scope"].get("region"))
        for key, value in diagnostics.items():
            self.assertEqual(actual["scope"][key], value)
        self.assertFalse(actual["scope"].get("usable_as_price_benchmark"))
        result = self.repo.clone_latest_analysis("source", "cached-source", "cache-target")
        self.assertEqual(result["cache_reuse_rejected"], "scope_review_requires_refresh")
        self.assertEqual(result["metrics"], 0)
        self.assertEqual(self.repo.get_metric_observations("cache-target"), [])
        self.assertEqual(self.bundle.metrics[0].scope, original)
        with self.db.session() as session:
            stored = session.scalars(select(m.MetricObservationRow)).one()
            self.assertEqual(stored.scope, original)
            self.assertFalse(stored.content_review)

    def test_fact_locator_and_price(self):
        self.save()
        actual = self.repo.search_facts("target")[0]
        self.assertEqual(actual["evidence_locator"], self.bundle.facts[0].evidence_locator.model_dump())
        self.assertEqual(actual["metrics"], self.bundle.facts[0].metrics)

    def test_brief_scoped_uncertainty(self):
        self.save()
        self.assertEqual(self.repo.explain_source_analysis("source")["uncertainty"], self.bundle.brief.uncertainty)
        with self.db.session() as s:
            self.assertEqual(s.scalars(select(m.AnalysisBrief)).one().uncertainty, self.bundle.brief.uncertainty)

    def test_reopen_persists_fields_and_does_not_mutate_bundle(self):
        original = copy.deepcopy(self.bundle.model_dump())
        self.save()
        self.db.engine.dispose()
        self.db = Database(self.url)
        self.repo = Repository(self.db)
        self.assertEqual(self.repo.get_recent_events("target")[0]["metrics"], original["events"][0]["metrics"])
        self.assertEqual(self.repo.get_recent_events("target")[0]["evidence_locator"], original["events"][0]["evidence_locator"])
        self.assertEqual(self.bundle.model_dump(), original)

    def test_cache_clone_preserves_locators_and_uncertainty(self):
        self.save()
        result = self.repo.clone_latest_analysis("source", "cached-source", "cache-target")
        self.assertEqual(result["cloned_events"][0]["evidence_locator"], self.bundle.events[0].evidence_locator.model_dump())
        for method, expected in ((self.repo.get_recent_events, self.bundle.events[0]),
                                 (self.repo.get_metric_observations, self.bundle.metrics[0]),
                                 (self.repo.search_facts, self.bundle.facts[0])):
            actual = method("cache-target")[0]
            self.assertEqual(actual["source_link_id"], "cached-source")
            self.assertEqual(actual["evidence_locator"], expected.evidence_locator.model_dump())
        self.assertEqual(self.repo.explain_source_analysis("cached-source")["uncertainty"], self.bundle.brief.uncertainty)

    def legacy(self):
        self.db.engine.dispose()
        path = Path(self.temp.name) / "legacy.db"
        self.db = Database("sqlite:///" + str(path))
        meta = MetaData()
        old = {}
        omitted = {"event_card": {"evidence_locator", "tracking_variables"},
                   "metric_observation": {"evidence_locator"}, "analysis_brief": {"uncertainty"}}
        for name, fields in omitted.items():
            old[name] = Table(name, meta, *[
                Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
                for c in m.Base.metadata.tables[name].columns if c.name not in fields])
        meta.create_all(self.db.engine)
        with self.db.engine.begin() as con:
            con.execute(old["event_card"].insert(), {"id": "old-e", "source_link_id": "legacy", "target_id": "old",
                "summary": "历史事件", "metrics": {"amount": 123}, "confidence": .5})
            con.execute(old["metric_observation"].insert(), {"id": "old-m", "source_link_id": "legacy", "target_id": "old",
                "metric_name": "历史容量", "metric_value": 123, "scope": {"old": True}})
            con.execute(old["analysis_brief"].insert(), {"id": "old-b", "source_link_id": "legacy", "target_id": "old",
                "one_sentence": "旧简报"})
        self.db.create_all()
        self.repo = Repository(self.db)

    def test_legacy_migration_keeps_unknown_as_null(self):
        self.legacy()
        self.assertIsNone(self.repo.get_recent_events("old")[0]["evidence_locator"])
        self.assertIsNone(self.repo.get_metric_observations("old")[0]["evidence_locator"])
        with self.db.session() as s:
            self.assertIsNone(s.get(m.AnalysisBrief, "old-b").uncertainty)
            self.assertEqual(s.get(m.AnalysisBrief, "old-b").one_sentence, "旧简报")
        self.assertEqual(self.repo.get_recent_events("old")[0]["metrics"], {"amount": 123})

    def test_legacy_migration_is_repeatable(self):
        self.legacy()
        before = self.repo.get_recent_events("old")
        for _ in range(2):
            self.db.create_all()
        self.assertEqual(self.repo.get_recent_events("old"), before)
        for name in ("event_card", "metric_observation", "analysis_brief"):
            cols = [c["name"] for c in inspect(self.db.engine).get_columns(name)]
            self.assertEqual(len(cols), len(set(cols)))

    def test_migrated_database_accepts_new_evidence(self):
        self.legacy()
        self.save()
        self.assertEqual(self.repo.get_recent_events("target")[0]["evidence_locator"], self.bundle.events[0].evidence_locator.model_dump())
        self.assertEqual(len(self.repo.get_recent_events("old")), 1)

    def test_failed_write_rolls_back_all_objects(self):
        self.bundle.events[0].metrics["bad_json"] = object()
        with self.assertRaises(Exception):
            self.save()
        with self.db.session() as s:
            self.assertEqual(s.scalars(select(m.MergedAnalysis)).all(), [])
            self.assertEqual(s.scalars(select(m.AnalysisBrief)).all(), [])
            self.assertEqual(s.scalars(select(m.FactItemRow)).all(), [])

    def test_68_still_link_only_and_70_can_save(self):
        config = MICConfig(raw={"merge_policy": {"rules": {"save_structured": {"min_overall_score": 70}}}})
        merger = MultiModelMerger(config)
        for score, decision, count in ((68, "link_only", 0), (70, "save_structured", 1)):
            item = self.bundle.model_copy(deep=True)
            item.overall_score = score
            merged = merger.merge("source", "target", [ModelContribution("test", "offline", item)])
            self.assertEqual(merged.bundle.decision, decision)
            self.assertEqual(len(merged.bundle.events), count)
            self.assertEqual(merged.bundle.brief.uncertainty, self.bundle.brief.uncertainty)


if __name__ == "__main__":
    unittest.main()
