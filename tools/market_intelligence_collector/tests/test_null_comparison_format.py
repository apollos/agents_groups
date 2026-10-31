"""Empty comparison compatibility must not relax typing or semantic review."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from mic.content_review_policy import PROTOCOL
from mic.schemas import MetricObservation, Passage
from mic.validate import BundleValidator


@pytest.mark.parametrize("fields", [{}, {"comparison": None}, {"comparison": {}}])
def test_missing_null_and_empty_comparison_mean_no_judgment(fields):
    before = deepcopy(fields)
    metric = MetricObservation.model_validate(fields)
    assert metric.comparison == {}
    assert "yoy_status" not in metric.comparison
    assert fields == before


@pytest.mark.parametrize("value", [[], [None], [["yoy", 0]], "", "{}", "null", 0, 1, False, True])
def test_other_comparison_types_still_fail(value):
    with pytest.raises(ValidationError) as caught:
        MetricObservation.model_validate({"comparison": value})
    assert caught.value.errors()[0]["loc"] == ("comparison",)
    assert caught.value.errors()[0]["type"] == "dict_type"


@pytest.mark.parametrize("value", [{"yoy": 0}, {"yoy": -0.1},
                                  {"yoy": None, "yoy_status": "not_applicable"}])
def test_dict_values_are_not_repaired_by_the_format_validator(value):
    assert MetricObservation.model_validate({"comparison": value}).comparison == value


def test_only_comparison_null_is_normalized():
    with pytest.raises(ValidationError) as caught:
        MetricObservation.model_validate({"comparison": None, "scope": None})
    assert [error["loc"] for error in caught.value.errors()] == [("scope",)]


def reviewed_raw():
    text = "公司披露本期收入240万元。"
    evidence = [{"passage_id": "p0", "quote": text}]
    return text, {
        "schema_version": "bundle_extraction_v0.3", "confidence": 0.0, "decision": "save_structured", "overall_score": 80,
        "brief": {"one_sentence": "未经审核的强烈看多判断。"},
        "metrics": [
            {"metric_name": "收入", "metric_value": 240, "unit": "万元",
             "comparison": None, "evidence_locator": {"passage_id": "p0"}},
            {"metric_name": "预测收入", "metric_value": 999, "unit": "万元",
             "comparison": None, "evidence_locator": {"passage_id": "p0"}},
        ],
        "content_review": {
            "protocol": PROTOCOL,
            "claims": [
                {"id": "observed", "kind": "observation", "status": "source_supported",
                 "statement": text, "reason": "正文直接披露该数值及单位。",
                 "evidence": evidence, "depends_on": []},
                {"id": "held", "kind": "observation", "status": "pending_review",
                 "statement": "预测收入999万元。", "reason": "来源没有给出预测值。",
                 "evidence": evidence, "depends_on": []},
            ],
            "bindings": {"/metrics/0": ["observed"], "/metrics/1": ["held"]},
        },
    }


def test_null_comparison_reaches_full_shared_review_without_authorizing_content():
    text, raw = reviewed_raw()
    before = deepcopy(raw)
    report = BundleValidator({}).validate(raw, [Passage(passage_id="p0", section="正文", text=text)])
    assert report.schema_valid, report.errors
    bundle = report.bundle
    assert len(bundle.metrics) == 1
    assert bundle.metrics[0].metric_value == 240
    assert bundle.metrics[0].comparison == {}
    assert bundle.brief.one_sentence == ""
    assert bundle.content_review["claims"]["held"]["status"] == "pending_review"
    assert bundle.content_review["model_review"] == raw["content_review"]
    assert bundle.metrics[0].content_review["materialized_digest"]
    assert report.warnings  # The held record and unreviewed brief were reviewed.
    assert raw == before  # Preserve the model's original null in audit input.


def test_bad_comparison_still_rejects_bundle_before_shared_review():
    text, raw = reviewed_raw()
    raw["metrics"][0]["comparison"] = []
    report = BundleValidator({}).validate(raw, [Passage(passage_id="p0", section="正文", text=text)])
    assert not report.schema_valid
    assert report.bundle is None
    assert report.errors == ["metrics.0.comparison: Input should be a valid dictionary"]
    assert report.error_code == "schema_invalid"
