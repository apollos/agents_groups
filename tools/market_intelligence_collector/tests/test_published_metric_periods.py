"""A run report can name published metric periods without their values."""
import os
from pathlib import Path
import tempfile
import unittest

from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository


class PublishedMetricPeriodTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("MIC_STORAGE_TEST_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.db = Database("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        self.addCleanup(lambda: self.db.engine.dispose())
        self.db.create_all()
        self.repo = Repository(self.db)

    def test_period_identity_omits_the_stored_value(self):
        with self.db.session() as session:
            session.add(m.SearchRun(id="run-1"))
            session.add(m.SourceLink(id="link-1", search_run_id="run-1"))
            session.add(m.MetricObservationRow(
                id="metric-1", source_link_id="link-1", period="2026年半年度",
                metric_name="营业收入", metric_value=1736000000,
            ))
        periods = self.repo.published_metric_periods("run-1")
        assert periods == [{
            "metric_id": "metric-1",
            "period": "2026年半年度",
            "source_link_id": "link-1",
        }]
        assert self.repo.published_metric_periods("run-absent") == []
