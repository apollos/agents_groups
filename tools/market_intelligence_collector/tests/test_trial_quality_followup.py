"""Regression cases observed in the 2026-10-04 CATL trial review."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from mic.schemas import Passage
from mic.store.database import Database
from mic.store.models import LinkReadAttempt
from mic.store.repository import Repository, _to_dt
from mic.validate import BundleValidator

TEXT = ("30MW/120MWh大容量+60MW/240MWh长寿命，"
        "远景能源中标价为19461.6万元，合单价0.518元/Wh。")


def validate(raw, text=TEXT):
    return BundleValidator({"strict_evidence_review": True}).validate(
        raw, [Passage(passage_id="p1", section="正文", text=text)])


@pytest.mark.parametrize("value", ["2026-09-15T14:41:00+08:00", "2026-09-15T06:41:00Z",
                                  datetime.fromisoformat("2026-09-15T14:41:00+08:00")])
def test_offset_datetime_persists_as_same_utc_instant(tmp_path, value):
    database = Database("sqlite:///" + str(tmp_path / "mic.db"))
    database.create_all()
    repository = Repository(database)
    aid = repository.save_read_attempt({"source_link_id": "fixture", "extracted_publish_time": value})
    with database.session() as session:
        row = session.get(LinkReadAttempt, aid)
        assert row.extracted_publish_time == datetime(2026, 9, 15, 6, 41)
    database.engine.dispose()


def test_legacy_partial_dates_and_invalid_values():
    assert _to_dt("2026-09") == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert _to_dt("2026") == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _to_dt("2026-09-15") == datetime(2026, 9, 15, tzinfo=timezone.utc)
    assert _to_dt("not a date") is None
    assert _to_dt(None) is None


def test_unresolved_price_preserves_quote_but_removes_benchmark_and_channels():
    raw = {"metrics": [{"metric_value": .518, "unit": "元/Wh",
                        "interpretation": "磷酸铁锂储能中标单价作为钠电单价的对比基准。",
                        "impact_channels": ["cost", "margin"],
                        "evidence_locator": {"passage_id": "p1"}}],
           "analyst_questions": [{"question": "订单毛利率如何？",
                                  "reason": "钠电单价1.035元/Wh显著高于磷酸铁锂，盈利性未披露。"}]}
    original = deepcopy(raw)
    report = validate(raw)
    metric = report.bundle.metrics[0]
    assert metric.metric_value == .518
    assert metric.scope["source_price_basis_review"]["computed_price_yuan_per_Wh"] == "0.5406"
    assert metric.scope["source_price_basis_status"] == "pending_review"
    assert metric.scope["usable_as_price_benchmark"] is False
    assert metric.impact_channels == []
    assert "暂不用于" in metric.interpretation
    assert "高于" not in report.bundle.analyst_questions[0].reason
    assert raw == original
    second = validate(report.bundle.model_dump())
    assert second.bundle.model_dump() == report.bundle.model_dump()
    assert second.quality_reviews == []


def test_impact_channels_cannot_bypass_interpretation_check():
    raw = {"metrics": [{"metric_value": 1.035, "unit": "元/Wh",
                        "interpretation": "本项目报价观察。", "impact_channels": ["margin"],
                        "evidence_locator": {"passage_id": "p1"}}]}
    result = validate(raw, "中标单价1.035元/Wh。")
    assert result.bundle.metrics[0].impact_channels == []


def test_order_amount_does_not_establish_company_revenue_contribution():
    raw = {"brief": {"uncertainty": "未见官方原文与合同细节；订单规模对宁德时代整体收入影响有限。"},
           "metrics": [{"metric_value": 4141.622, "unit": "万元",
                        "interpretation": "单笔订单，规模较小，对整体收入贡献有限。",
                        "impact_channels": ["revenue"], "evidence_locator": {"passage_id": "p1"}}]}
    result = validate(raw, "宁德时代中标价为4141.622万元。")
    metric = result.bundle.metrics[0]
    assert metric.metric_value == 4141.622
    assert metric.scope["materiality_review"]["status"] == "pending_review"
    assert "贡献有限" not in metric.interpretation
    assert "未见官方原文与合同细节" in result.bundle.brief.uncertainty
    assert "影响有限" not in result.bundle.brief.uncertainty
    assert "尚未核实" in result.bundle.brief.uncertainty


def test_literal_source_materiality_statement_is_not_discarded():
    claim = "对整体收入贡献有限"
    raw = {"metrics": [{"metric_value": 100, "unit": "万元", "interpretation": claim,
                        "evidence_locator": {"passage_id": "p1"}}]}
    assert validate(raw, "金额100万元，公司称对整体收入贡献有限。").bundle.metrics[0].interpretation == claim
