"""Bundle protocol is checked on the remote object before schema defaults apply.

Fixtures are offline doubles; the review-only shape follows the saved r3 output of
link_4b4f7207e96c (no bundle fields, the review object at top level).
"""
from copy import deepcopy
import json
import math
from types import SimpleNamespace

import pytest

from mic.config import MICConfig
from mic.content_review_policy import PROTOCOL
from mic.modeling.adapter import ModelCallResult
from mic.modeling.call_planner import LinkModelResult
from mic.pipeline import Pipeline, RunStats
from mic.schemas import Passage
from mic.validate import BundleValidator, bundle_protocol_errors

PASSAGES = [Passage(passage_id="p4", section="正文", text="营业收入115.66亿元，同比增长178.67%")]
REVIEW_ONLY = {
    "protocol": PROTOCOL,
    "decision_reason": "p4直接给出2026H1营业收入115.66亿元……属70-84档。",
    "claims": [{"id": "rev", "kind": "observation", "status": "source_supported",
                "statement": "2026年上半年，兆易创新营业收入115.66亿元。", "reason": "p4明确列示。",
                "evidence": [{"passage_id": "p4", "quote": "营业收入115.66亿元，同比增长178.67%"}],
                "depends_on": []}],
    "bindings": {"/metrics/0": ["rev"]},
    "task_coverage": [],
}
VALID_SKIP = {"schema_version": "bundle_extraction_v0.3", "decision": "skip", "overall_score": 0,
              "confidence": 0.0, "content_review": {"protocol": PROTOCOL, "claims": [], "bindings": {}}}


def test_review_only_object_is_a_protocol_error_with_field_paths():
    original = deepcopy(REVIEW_ONLY)
    report = BundleValidator({}).validate(REVIEW_ONLY, PASSAGES)
    assert report.schema_valid is False and report.bundle is None
    assert report.error_code == "response_protocol_invalid"
    assert report.errors == ["schema_version: missing", "decision: missing", "overall_score: missing",
                             "confidence: missing", "content_review: missing"]
    assert REVIEW_ONLY == original


def test_valid_explicit_skip_with_zero_score_and_empty_arrays_passes():
    assert bundle_protocol_errors(VALID_SKIP) == []
    report = BundleValidator({}).validate(deepcopy(VALID_SKIP), PASSAGES)
    assert report.schema_valid and report.error_code is None
    assert (report.bundle.decision, report.bundle.overall_score) == ("skip", 0)


@pytest.mark.parametrize("path,value,expected", [
    ("overall_score", True, "overall_score: must be a finite number in [0, 100]"),
    ("overall_score", "72", "overall_score: must be a finite number in [0, 100]"),
    ("overall_score", math.nan, "overall_score: must be a finite number in [0, 100]"),
    ("overall_score", math.inf, "overall_score: must be a finite number in [0, 100]"),
    ("overall_score", 100.5, "overall_score: must be a finite number in [0, 100]"),
    ("confidence", 1.5, "confidence: must be a finite number in [0, 1]"),
    ("confidence", "0.9", "confidence: must be a finite number in [0, 1]"),
    ("decision", "save", "decision: must be save_structured | link_only | skip"),
    ("schema_version", "bundle_extraction_v0.2", "schema_version: must be 'bundle_extraction_v0.3'"),
    ("content_review", [], "content_review: must be an object"),
    ("content_review.protocol", "content_review_v1", "content_review.protocol: must be 'content_review_v2'"),
    ("content_review.claims", {}, "content_review.claims: must be an array"),
    ("content_review.bindings", [], "content_review.bindings: must be an object"),
])
def test_each_protocol_field_is_checked(path, value, expected):
    raw = deepcopy(VALID_SKIP)
    target, _, key = path.rpartition(".")
    (raw[target] if target else raw)[key] = value
    assert bundle_protocol_errors(raw) == [expected]


def test_missing_review_members_and_non_object_top_level():
    raw = deepcopy(VALID_SKIP)
    raw["content_review"] = {"protocol": PROTOCOL}
    assert bundle_protocol_errors(raw) == ["content_review.claims: missing", "content_review.bindings: missing"]
    assert bundle_protocol_errors([VALID_SKIP]) == ["$: must be a JSON object"]


def test_optional_business_arrays_and_source_link_id_may_be_omitted():
    raw = deepcopy(VALID_SKIP)
    assert "source_link_id" not in raw and "facts" not in raw
    assert bundle_protocol_errors(raw) == []


def _persist(result):
    saved_outputs = []
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = MICConfig(raw={})
    pipeline.policy_version = "offline"
    pipeline.validator = BundleValidator({})
    pipeline.repo = SimpleNamespace(save_model_run=lambda value: "mrun_offline",
                                    save_model_output=saved_outputs.append)
    stats = RunStats()
    link = LinkModelResult("link_offline", "bundle_extraction", "single_model", outputs=[result])
    contributions = pipeline._persist_model_outputs("link_offline", link, SimpleNamespace(passages=PASSAGES), stats)
    return contributions, saved_outputs[0], stats


def _result(parsed):
    return ModelCallResult(model_config_id="offline", provider="offline", provider_type="offline",
                           model_name="offline", status="success", raw_text=json.dumps(parsed, ensure_ascii=False),
                           parsed=parsed)


def test_protocol_invalid_output_is_saved_unchanged_and_excluded_from_merge():
    parsed = deepcopy(REVIEW_ONLY)
    result = _result(parsed)
    raw_text = result.raw_text
    contributions, saved, stats = _persist(result)
    assert contributions == []
    assert saved["schema_valid"] is False
    assert saved["decision"] is None and saved["overall_score"] is None and saved["confidence"] is None
    assert saved["output_json"] == REVIEW_ONLY and result.raw_text == raw_text
    assert saved["validation_errors"]["error_code"] == "response_protocol_invalid"
    assert saved["validation_errors"]["stages"] == {"http": "ok", "json_parse": "ok", "protocol": "invalid"}
    assert "overall_score: missing" in saved["validation_errors"]["errors"]
    assert stats.model_requests[0]["validation_error_code"] == "response_protocol_invalid"


def test_valid_skip_output_is_a_contribution_with_protocol_valid():
    contributions, saved, _ = _persist(_result(deepcopy(VALID_SKIP)))
    assert len(contributions) == 1
    assert saved["schema_valid"] is True and saved["decision"] == "skip" and saved["overall_score"] == 0
    assert saved["validation_errors"]["stages"] == {"http": "ok", "json_parse": "ok", "protocol": "valid"}
    assert "error_code" not in saved["validation_errors"]
