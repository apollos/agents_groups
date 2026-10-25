"""Directory evidence stays directory evidence through the shared reviewer.

Model doubles exercise structural enforcement, not real semantic understanding.
"""
from copy import deepcopy
import json

import pytest

from mic.content_review import finalize_task_coverage
from mic.content_review_policy import PROTOCOL, RULE_TEXT
from mic.modeling.prompts import build_bundle_messages
from mic.profile import TargetProfile
from mic.schemas import Passage
from mic.validate import BundleValidator


ENTRY = "2026-08-10 甲公司半年度报告 https://example.test/report.pdf"
SCOPE = "本目录为机构公告列表，第1页，下一页。"
PASSAGES = [Passage(passage_id="entry1", section="index_entry", text=ENTRY),
            Passage(passage_id="scope1", section="index_scope", text=SCOPE)]


def claim(cid="listing", *, kind="observation", status="source_supported", pid="entry1",
          statement="目录列示《甲公司半年度报告》，条目日期为2026-08-10。", fields=None, depends=()):
    return {"id": cid, "kind": kind, "status": status, "statement": statement,
            "reason": "人工标注：仅支持目录列示范围，不代表已读报告正文。",
            "evidence": [{"passage_id": pid, "quote": ENTRY if pid == "entry1" else SCOPE}],
            "depends_on": list(depends), "field_values": fields or {}}


def raw_bundle():
    listing = claim()
    return {"decision": "save_structured", "overall_score": 80,
            "facts": [{"fact_type": "document_listing", "fact_statement": listing["statement"],
                       "evidence_locator": {"passage_id": "entry1"}}],
            "content_review": {"protocol": PROTOCOL, "claims": [listing],
                               "bindings": {"/facts/0": ["listing"]}}}


def validated(raw, passages=PASSAGES):
    report = BundleValidator({}).validate(raw, passages)
    assert report.schema_valid, report.errors
    return report.bundle


def test_directory_listing_is_publishable_and_remains_auditable():
    raw = raw_bundle()
    bundle = validated(raw)
    assert len(bundle.facts) == 1
    assert bundle.facts[0].fact_type == "document_listing"
    assert bundle.facts[0].fact_statement == raw["facts"][0]["fact_statement"]
    assert bundle.facts[0].evidence_locator.excerpt == ENTRY
    assert bundle.facts[0].metrics == {}
    assert bundle.content_review["source_publication"]["status"] == "unknown"


def test_directory_fact_requires_explicit_listing_role():
    raw = raw_bundle()
    raw["facts"][0]["fact_type"] = "finance"
    bundle = validated(raw)
    assert bundle.facts == []
    assert any(h["reason"] == "index_fact_requires_document_listing" for h in bundle.content_review["held"])


def test_directory_title_cannot_authorize_metric_or_business_event_with_same_claim():
    raw = raw_bundle()
    raw["content_review"]["claims"][0]["field_values"] = {"event_type": "earnings_change"}
    raw["metrics"] = [{"metric_name": "收入", "metric_value": 200, "unit": "元",
                       "evidence_locator": {"passage_id": "entry1"}}]
    raw["events"] = [{"event_type": "earnings_change", "summary": "公司收入为200元。",
                      "evidence_locator": {"passage_id": "entry1"}}]
    raw["content_review"]["bindings"].update({"/metrics/0": ["listing"], "/events/0": ["listing"]})
    bundle = validated(raw)
    assert bundle.metrics == bundle.events == []
    assert len(bundle.facts) == 1
    assert bundle.content_review["claims"]["listing"]["status"] == "source_supported"
    assert any(h["reason"] == "index_material_scope_only" for h in bundle.content_review["held"])


def test_directory_fact_does_not_smuggle_numbers_or_direction_in_auxiliary_fields():
    raw = raw_bundle()
    raw["facts"][0].update(metrics={"volume": 200, "unit": "台"}, direction="positive")
    raw["content_review"]["claims"].append(claim(
        "impact", kind="analysis", fields={"direction": "positive"}))
    raw["content_review"]["bindings"]["/facts/0/direction"] = ["impact"]
    bundle = validated(raw)
    assert bundle.facts[0].metrics == {}
    assert bundle.facts[0].direction == "unclear"


@pytest.mark.parametrize("dependent", [False, True])
def test_entry_date_never_becomes_directory_publication_even_through_dependencies(dependent):
    raw = raw_bundle()
    publication = claim("publication", pid="scope1" if dependent else "entry1",
                        fields={"source_publication": {"published_at": "2026-08-10",
                                                       "date_role": "source_publication"}},
                        depends=["listing"] if dependent else [])
    raw["content_review"]["claims"].append(publication)
    raw["content_review"]["bindings"]["/source_publication"] = ["publication"]
    bundle = validated(raw)
    review = bundle.content_review["source_publication"]
    assert review["status"] == "unknown"
    assert review["rejected_claims"] == [{"claim_id": "publication", "reason": "index_entry_not_source_publication"}]
    assert len(bundle.facts) == 1


def test_direct_body_evidence_remains_usable_when_directory_passages_are_also_present():
    raw = raw_bundle()
    text = "报告正文明确列示收入为200元。"
    observation = claim("amount", pid="body", statement=text)
    observation["evidence"] = [{"passage_id": "body", "quote": text}]
    observation["amount"] = {"currency": "CNY", "value": 200, "unit": "元",
                             "evidence": {"passage_id": "body", "quote": text}}
    raw["content_review"]["claims"].append(observation)
    raw["content_review"]["bindings"]["/metrics/0"] = ["amount"]
    raw["metrics"] = [{"metric_name": "收入", "metric_value": 200, "unit": "元",
                       "evidence_locator": {"passage_id": "body"}}]
    bundle = validated(raw, [*PASSAGES, Passage(passage_id="body", section="正文", text=text)])
    assert bundle.metrics[0].metric_value == 200
    assert len(bundle.facts) == 1


def test_unverified_latestness_is_held_while_listing_and_coverage_gap_survive():
    raw = raw_bundle()
    raw["content_review"]["claims"].extend([
        claim("latest", kind="comparability", status="pending_review", pid="scope1",
              statement="当前目录是否涵盖截至运行时全部披露尚不明确。"),
        claim("latest_listing", statement="该报告为截至运行时最新报告。", depends=["latest"]),
    ])
    raw["facts"].append({"fact_type": "document_listing", "fact_statement": "截至运行时最新报告。",
                         "evidence_locator": {"passage_id": "entry1"}})
    raw["content_review"]["bindings"]["/facts/1"] = ["latest_listing"]
    raw["content_review"]["task_coverage"] = [{
        "question_id": "q1", "status": "partial", "answer_claim_ids": ["listing"],
        "uncovered_aspects": ["最新报告及财务指标"], "reason": "目录分页不完整且正文未读取。"}]
    bundle = validated(raw)
    coverage = finalize_task_coverage(bundle, [{"question_id": "q1", "question": "查阅目录、最新报告及财务指标"}])
    assert len(bundle.facts) == 1
    assert bundle.content_review["claims"]["latest_listing"]["reason"] == "dependency_not_supported"
    assert coverage[0]["status"] == "partial"
    assert "最新报告及财务指标" in bundle.coverage_gaps[0].description


def test_index_material_metadata_and_canonical_limits_reach_existing_extraction_call():
    metadata = {"material_scope": {"kind": "index", "entries": [{"entry_id": "entry1", "text": ENTRY,
                 "links": ["https://example.test/report.pdf"]}], "ordering": "unknown", "completeness": "unknown"}}
    original = deepcopy(metadata)
    messages = build_bundle_messages(TargetProfile("target", "company", "甲公司"), metadata, PASSAGES, {})
    assert len(messages) == 2
    payload = json.loads(messages[1]["content"])
    assert payload["source_metadata"] == original == metadata
    assert payload["selected_passages"][0]["section"] == "index_entry"
    assert RULE_TEXT in messages[0]["content"]
    assert "document_listing" in messages[0]["content"]
    assert "截至运行时最新" in messages[0]["content"]
