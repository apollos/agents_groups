"""Labelled model doubles test date-review enforcement, not live understanding."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from mic.content_review import resolve_source_publication
from mic.content_review_policy import DATE_REVIEW_PROTOCOL, PROTOCOL, RULE_TEXT, SCHEMA_HINT
from mic.publication_time import PublicationWindow
from mic.schemas import BundleExtraction, Passage
from mic.validate import BundleValidator


UNKNOWN = {"status": "unknown", "published_at": None, "reason": "pdf_publication_not_verified"}


def case(date="2026-09-15", *, role="source_publication", status="source_supported"):
    text = f"甲公司公开披露本报告，披露日期为 {date}。本报告的报告期末为 2026-06-30。"
    claim = {"id": "date", "kind": "observation", "status": status,
             "statement": f"本报告于 {date} 公开披露。",
             "reason": "人工标签：所引正文明确给出当前报告的公开披露日期。",
             "evidence": [{"passage_id": "p1", "quote": text}], "depends_on": [],
             "field_values": {"source_publication": {"published_at": date, "date_role": role,
                 "assessment": {"protocol": DATE_REVIEW_PROTOCOL, "possible_roles": [role],
                     "selected_role": role, "basis": "explicit_publication",
                     "evidence": [{"passage_id": "p1", "quote": text}]}}}}
    raw = {"schema_version": "bundle_extraction_v0.3", "confidence": 0.0, "decision": "link_only", "overall_score": 60,
           "content_review": {"protocol": PROTOCOL, "claims": [claim],
                              "bindings": {"/source_publication": ["date"]}}}
    passages = [Passage(passage_id="p1", section="正文", text=text),
                Passage(passage_id="title", section="标题", text=text)]
    return raw, passages


def validated(raw, passages):
    report = BundleValidator({}).validate(raw, passages)
    assert report.schema_valid, report.errors
    return report.bundle


def reviewed(date="2026-09-15", **kwargs):
    return validated(*case(date, **kwargs))


def test_publication_is_source_metadata_without_requiring_a_published_fact():
    raw, passages = case()
    original = deepcopy(raw)
    bundle = validated(raw, passages)
    result = bundle.content_review["source_publication"]
    assert raw == original
    assert result["status"] == "known"
    assert result["published_at"] == "2026-09-15T00:00:00+08:00"
    assert result["precision"] == "day"
    assert result["claim_ids"] == ["date"]
    assert result["evidence"] == raw["content_review"]["claims"][0]["evidence"]
    assert bundle.facts == [] and bundle.decision == "link_only"
    resolved = resolve_source_publication([bundle], UNKNOWN)
    assert resolved["status"] == "known"
    assert resolved["source"] == "model_content_review"
    assert resolved["extracted_publication"] == UNKNOWN
    assert resolved["claim_refs"] == [{"contribution_index": 0, "claim_id": "date"}]
    window = PublicationWindow(30, datetime(2026, 10, 4, 1, tzinfo=timezone.utc))
    assert window.assess(resolved)["allowed"]
    assert not window.assess(resolve_source_publication([reviewed("2026-01-01")], UNKNOWN))["allowed"]


@pytest.mark.parametrize("role", ["report_period", "event_date", "board_approval", "signature_date", None, ""])
def test_other_date_roles_cannot_authorize_source_publication(role):
    bundle = reviewed(role=role)
    assert bundle.content_review["source_publication"]["status"] == "unknown"
    assert resolve_source_publication([bundle], UNKNOWN)["published_at"] is None


@pytest.mark.parametrize("damage", [
    "inference", "pending", "unsupported", "analysis_kind", "unknown_dependency", "pending_dependency",
    "no_quote", "fabricated_quote", "title_quote", "no_binding", "unknown_binding", "duplicate_binding",
    "duplicate_claim", "invalid_protocol", "no_field_value", "invalid_date",
])
def test_missing_or_unverified_claims_remain_unknown(damage):
    raw, passages = case()
    review = raw["content_review"]
    claim = review["claims"][0]
    if damage in {"inference", "pending", "unsupported"}:
        claim["status"] = "pending_review" if damage == "pending" else damage
    elif damage == "analysis_kind":
        claim["kind"] = "analysis"
    elif damage == "unknown_dependency":
        claim["depends_on"] = ["missing"]
    elif damage == "pending_dependency":
        prerequisite = deepcopy(claim)
        prerequisite.update(id="prerequisite", status="pending_review")
        review["claims"].append(prerequisite)
        claim["depends_on"] = ["prerequisite"]
    elif damage == "no_quote":
        claim["evidence"] = []
    elif damage == "fabricated_quote":
        claim["evidence"][0]["quote"] = "正文中没有的披露日期。"
    elif damage == "title_quote":
        claim["evidence"].append({"passage_id": "title", "quote": passages[1].text})
    elif damage == "no_binding":
        review["bindings"] = {}
        # A model cannot bypass the binding by copying the expected output shape.
        review["source_publication"] = {"status": "known", "published_at": "2026-09-15"}
    elif damage == "unknown_binding":
        review["bindings"]["/source_publication"] = ["missing"]
    elif damage == "duplicate_binding":
        review["bindings"]["/source_publication"] = ["date", "date"]
    elif damage == "duplicate_claim":
        review["claims"].append(deepcopy(claim))
    elif damage == "invalid_protocol":
        review["protocol"] = "content_review_v1"
    elif damage == "no_field_value":
        claim["field_values"] = {}
    elif damage == "invalid_date":
        claim["field_values"]["source_publication"]["published_at"] = "2026-02-30"
    if damage == "invalid_protocol":
        report = BundleValidator({}).validate(raw, passages)
        assert not report.schema_valid and report.bundle is None
        assert report.error_code == "response_protocol_invalid"
        return
    bundle = validated(raw, passages)
    assert bundle.content_review["source_publication"]["status"] == "unknown", damage
    result = resolve_source_publication([bundle], UNKNOWN)
    assert result["status"] == "unknown" and result["published_at"] is None


def test_conflicting_bound_dates_fail_closed_with_original_claims_preserved():
    raw, passages = case()
    other, other_passages = case("2026-09-16")
    claim = other["content_review"]["claims"][0]
    claim["id"] = "other_date"
    claim["evidence"][0]["passage_id"] = "p2"
    claim["field_values"]["source_publication"]["assessment"]["evidence"][0]["passage_id"] = "p2"
    passages.append(other_passages[0].model_copy(update={"passage_id": "p2"}))
    raw["content_review"]["claims"].append(claim)
    raw["content_review"]["bindings"]["/source_publication"].append("other_date")
    bundle = validated(raw, passages)
    audit = bundle.content_review["source_publication"]
    assert audit["status"] == "conflict" and audit["published_at"] is None
    assert len(audit["candidates"]) == 2
    assert resolve_source_publication([bundle], UNKNOWN)["status"] == "conflict"
    assert bundle.content_review["model_review"]["claims"] == raw["content_review"]["claims"]


def test_multiple_contributions_keep_local_claim_ids_and_require_all_reviews():
    result = resolve_source_publication([reviewed(), reviewed()], UNKNOWN)
    assert result["status"] == "known"
    assert result["claim_refs"] == [
        {"contribution_index": 0, "claim_id": "date"},
        {"contribution_index": 1, "claim_id": "date"},
    ]
    assert resolve_source_publication([reviewed(), reviewed("2026-09-16")], UNKNOWN)["status"] == "conflict"
    assert resolve_source_publication([reviewed(), BundleExtraction()], UNKNOWN)["status"] == "unknown"
    assert resolve_source_publication([], UNKNOWN)["status"] == "unknown"


def test_precision_and_timezone_agreement_do_not_choose_between_conflicting_times():
    same = resolve_source_publication([reviewed("2026-09-15T10:00:00+08:00"),
                                       reviewed("2026-09-15T02:00:00Z")], UNKNOWN)
    assert same["status"] == "known" and same["precision"] == "second"
    day = resolve_source_publication([reviewed(), reviewed("2026-09-15T10:00:00+08:00")], UNKNOWN)
    assert day["status"] == "known" and day["precision"] == "second"
    conflict = resolve_source_publication([reviewed("2026-09-15T10:00:00+08:00"),
                                           reviewed("2026-09-15T11:00:00+08:00")], UNKNOWN)
    assert conflict["status"] == "conflict" and conflict["published_at"] is None


def test_parser_metadata_retains_provenance_but_cannot_override_model_conflict():
    parser = {"status": "known", "published_at": "2026-09-15T00:00:00+08:00",
              "precision": "day", "source": "meta:article:published_time",
              "candidates": [{"published_at": "2026-09-15T00:00:00+08:00", "precision": "day"}]}
    before = deepcopy(parser)
    for contributions in ([], [BundleExtraction()], [reviewed()]):
        resolved = resolve_source_publication(contributions, parser)
        assert resolved["status"] == "known"
        assert resolved["source"] == parser["source"]
        assert resolved["published_at"] == parser["published_at"]
    assert parser == before
    resolved = resolve_source_publication([reviewed("2026-09-16")], parser)
    assert resolved["status"] == "conflict" and resolved["published_at"] is None
    assert resolved["extracted_publication"] == parser
    conflict = {"status": "conflict", "published_at": None, "reason": "conflicting_publication_dates"}
    assert resolve_source_publication([reviewed()], conflict)["status"] == "conflict"


def test_canonical_policy_distinguishes_publication_period_and_latestness():
    assert 'bindings["/source_publication"]' in RULE_TEXT
    assert "报告期、来源披露日期" in RULE_TEXT
    assert "官方完整目录" in RULE_TEXT and "uncovered_aspects" in RULE_TEXT
    example = SCHEMA_HINT["source_publication_binding_example"]
    assert example["bindings"]["/source_publication"] == [example["claim"]["id"]]
