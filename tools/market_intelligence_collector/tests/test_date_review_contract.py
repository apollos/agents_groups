"""Portable date-role regressions: labelled models test enforcement, not accuracy."""
from copy import deepcopy

import pytest

from mic.content_review import enforce_reviewed_dates, enforce_source_publication_dates, resolve_source_publication
from mic.content_review_policy import DATE_REVIEW_PROTOCOL, PROTOCOL
from mic.schemas import Passage
from mic.validate import BundleValidator


BUSINESS = "公司公告50股将于2026年9月7日上市流通。"
PUBLICATION = "本公告公开披露日期为2026年8月29日。"
SIGNATURE = "公司董事会 2026年8月29日"
OCCURRENCE = "双方于2026年8月29日签订供货合同。"
TEXT = {"business": BUSINESS, "publication": PUBLICATION, "signature": SIGNATURE, "occurrence": OCCURRENCE}
PASSAGES = [Passage(passage_id=pid, section="正文", text=text) for pid, text in TEXT.items()]


def evidence(pid):
    return [{"passage_id": pid, "quote": TEXT[pid]}]


def assessment(role="event_occurrence", basis="explicit_occurrence", pid="occurrence"):
    return {"protocol": DATE_REVIEW_PROTOCOL, "possible_roles": [role], "selected_role": role,
            "basis": basis, "evidence": evidence(pid)}


def observation(cid, statement, pid, fields=None, depends=()):
    return {"id": cid, "kind": "observation", "status": "source_supported",
            "statement": statement, "reason": "人工标签：正文支持该事项，日期角色另明确审核。",
            "evidence": evidence(pid), "depends_on": list(depends), "field_values": fields or {}}


def raw_event():
    claim = observation("event", OCCURRENCE, "occurrence", {
        "event_type": "major_order", "event_date": "2026-08-29",
        "event_date_assessment": assessment()})
    return {"decision": "save_structured", "overall_score": 80,
            "events": [{"event_type": "major_order", "event_date": "2026-08-29", "summary": OCCURRENCE,
                        "evidence_locator": {"passage_id": "occurrence"}}],
            "facts": [{"fact_statement": OCCURRENCE, "evidence_locator": {"passage_id": "occurrence"}}],
            "content_review": {"protocol": PROTOCOL, "claims": [claim],
                               "bindings": {"/events/0": ["event"], "/facts/0": ["event"]}}}


def validate(raw, passages=PASSAGES):
    result = BundleValidator({}).validate(raw, passages)
    assert result.schema_valid, result.errors
    return result.bundle


def test_explicit_event_date_uses_same_observation_and_survives_projection():
    raw = raw_event()
    original = deepcopy(raw)
    bundle = validate(raw)
    event = bundle.events[0]
    assert raw == original
    assert event.event_date == "2026-08-29"
    assert event.content_review["fields"]["event_date"]["date_review"] == DATE_REVIEW_PROTOCOL
    value = event.model_dump(mode="json")
    assert enforce_reviewed_dates("events", value) == value
    stored = {**value, "event_date": "2026-08-29T00:00:00"}
    assert enforce_reviewed_dates("events", stored, stored_utc=True) == stored


@pytest.mark.parametrize("damage", ["missing_value", "mismatched_value", "missing_assessment", "old_protocol",
    "multiple_roles", "unknown_role", "signature", "approval", "reporting_period", "scheduled_execution",
    "signature_basis", "unbound_quote", "empty_quote", "wrong_selected_role", "invalid_date"])
def test_unproved_event_date_is_held_without_erasing_supported_business(damage):
    raw = raw_event()
    values = raw["content_review"]["claims"][0]["field_values"]
    date = values["event_date_assessment"]
    if damage == "missing_value":
        values.pop("event_date")
    elif damage == "mismatched_value":
        values["event_date"] = "2026-08-30"
    elif damage == "missing_assessment":
        values.pop("event_date_assessment")
    elif damage == "old_protocol":
        date["protocol"] = "date_review_v0"
    elif damage == "multiple_roles":
        date["possible_roles"] = ["signature", "event_occurrence"]
    elif damage == "unknown_role":
        date.update(possible_roles=["unknown"], selected_role="unknown")
    elif damage in {"signature", "approval", "reporting_period", "scheduled_execution"}:
        date.update(possible_roles=[damage], selected_role=damage)
    elif damage == "signature_basis":
        date["basis"] = "signature"
    elif damage == "unbound_quote":
        date["evidence"] = evidence("publication")  # real passage, absent from this observation
    elif damage == "empty_quote":
        date["evidence"] = []
    elif damage == "wrong_selected_role":
        date["selected_role"] = "source_publication"
    elif damage == "invalid_date":
        values["event_date"] = "2026-02-30"
    bundle = validate(raw)
    assert bundle.events[0].event_date is None
    assert bundle.events[0].summary == bundle.facts[0].fact_statement == OCCURRENCE
    assert bundle.content_review["claims"]["event"]["status"] == "source_supported"
    assert bundle.events[0].content_review["fields"]["event_date"]["status"] == "pending_review"


def raw_publication(*, ambiguous=False):
    pid = "signature" if ambiguous else "publication"
    role = assessment("source_publication", "signature" if ambiguous else "explicit_publication", pid)
    if ambiguous:
        role.update(possible_roles=["signature", "source_publication"], selected_role=None)
    publication = observation("publication", "当前公告于2026年8月29日公开披露。", pid, {
        "source_publication": {"published_at": "2026-08-29", "date_role": "source_publication", "assessment": role}})
    business = observation("business", BUSINESS, "business", {"event_type": "financing"})
    brief = observation("brief", "公司于2026年8月29日公告50股的流通安排。", "business",
                        depends=["publication", "business"])
    brief["evidence"] += evidence(pid)
    return {"decision": "save_structured", "overall_score": 80,
        "events": [{"event_type": "financing", "event_date": "2026-08-29", "summary": BUSINESS,
                    "evidence_locator": {"passage_id": "business"}}],
        "facts": [{"fact_statement": BUSINESS, "evidence_locator": {"passage_id": "business"}}],
        "metrics": [{"metric_name": "安排流通股数", "metric_value": 50, "unit": "股",
                     "period": "2026-09-07", "evidence_locator": {"passage_id": "business"}}],
        "brief": {"one_sentence": brief["statement"]},
        "content_review": {"protocol": PROTOCOL, "claims": [business, publication, brief],
            "bindings": {"/source_publication": ["publication"], "/events/0": ["business"],
                         "/facts/0": ["business"], "/metrics/0": ["business"], "/brief/one_sentence": ["brief"]}}}


@pytest.mark.parametrize("damage", ["ambiguous", "signature_only", "missing_assessment", "wrong_basis", "unbound_quote"])
def test_source_date_rejection_propagates_to_brief_but_preserves_arrangement(damage):
    raw = raw_publication(ambiguous=True)
    claim = raw["content_review"]["claims"][1]
    value = claim["field_values"]["source_publication"]
    if damage == "signature_only":
        value["assessment"].update(possible_roles=["signature"], selected_role="signature")
    elif damage == "missing_assessment":
        value.pop("assessment")
    elif damage == "wrong_basis":
        value["assessment"].update(possible_roles=["source_publication"], selected_role="source_publication")
    elif damage == "unbound_quote":
        value["assessment"] = assessment("source_publication", "explicit_publication", "publication")
    bundle = validate(raw)
    assert bundle.content_review["source_publication"]["status"] == "unknown"
    assert bundle.content_review["claims"]["publication"]["status"] == "pending_review"
    assert bundle.content_review["claims"]["brief"]["reason"] == "dependency_not_supported"
    assert bundle.brief.one_sentence == ""
    assert bundle.facts[0].fact_statement == bundle.events[0].summary == BUSINESS
    assert bundle.events[0].event_date is None  # no silent substitution with the scheduled date
    assert bundle.metrics[0].metric_value == 50 and bundle.metrics[0].period == "2026-09-07"
    assert bundle.content_review["model_review"] == raw["content_review"]


def test_explicit_source_date_propagates_marker_and_preserves_current_parser_evidence():
    bundle = validate(raw_publication())
    publication = bundle.content_review["source_publication"]
    assert publication["status"] == "known"
    assert publication["date_review"] == DATE_REVIEW_PROTOCOL
    assert enforce_source_publication_dates(publication) == publication
    resolved = resolve_source_publication([bundle], {"status": "unknown", "published_at": None})
    assert resolved["status"] == "known"
    assert enforce_source_publication_dates(resolved) == resolved
    assert bundle.brief.one_sentence
    parser = {"status": "known", "published_at": "2026-08-29T11:00:00+08:00", "source": "meta:article:published_time"}
    assert enforce_source_publication_dates(parser) == parser


def test_conflicting_source_dates_downgrade_claims_before_materializing_dependents():
    raw = raw_publication()
    other = deepcopy(raw["content_review"]["claims"][1])
    other.update(id="other_publication", statement="另一处公开日期为2026年8月30日。")
    text = "本公告另一处标注公开日期2026年8月30日。"
    quote = {"passage_id": "other", "quote": text}
    other["evidence"] = [quote]
    other["field_values"]["source_publication"].update(published_at="2026-08-30")
    other["field_values"]["source_publication"]["assessment"]["evidence"] = [quote]
    raw["content_review"]["claims"].append(other)
    raw["content_review"]["bindings"]["/source_publication"].append("other_publication")
    bundle = validate(raw, [*PASSAGES, Passage(passage_id="other", section="正文", text=text)])
    assert bundle.content_review["source_publication"]["status"] == "conflict"
    assert bundle.content_review["claims"]["publication"]["reason"] == "conflicting_publication_dates"
    assert bundle.content_review["claims"]["other_publication"]["reason"] == "conflicting_publication_dates"
    assert bundle.brief.one_sentence == ""
    assert bundle.events[0].summary == BUSINESS


def test_one_observation_cannot_approve_a_different_date_in_second_event():
    raw = raw_event()
    raw["events"].append({**raw["events"][0], "event_date": "2026-08-30"})
    raw["content_review"]["bindings"]["/events/1"] = ["event"]
    bundle = validate(raw)
    assert [event.event_date for event in bundle.events] == ["2026-08-29", None]
    assert len(bundle.facts) == 1


def test_contextual_year_can_use_multiple_citations_without_an_extra_claim():
    raw = raw_event()
    passages = [Passage(passage_id="date", section="正文", text="8月29日，双方签订供货合同。"),
                Passage(passage_id="context", section="正文", text="本文于2026年8月30日刊发，回顾昨日签约。")]
    quotes = [{"passage_id": p.passage_id, "quote": p.text} for p in passages]
    claim = raw["content_review"]["claims"][0]
    claim["evidence"] = quotes
    claim["field_values"]["event_date_assessment"].update(basis="contextual_date", evidence=quotes)
    raw["events"][0]["evidence_locator"] = {"passage_id": "date"}
    raw["facts"][0]["evidence_locator"] = {"passage_id": "date"}
    bundle = validate(raw, passages)
    assert bundle.events[0].event_date == "2026-08-29"
    assert len(bundle.content_review["claims"]) == 1


def test_known_publication_marker_cannot_authorize_a_changed_value():
    publication = validate(raw_publication()).content_review["source_publication"]
    publication["published_at"] = "2026-08-30"
    assert enforce_source_publication_dates(publication)["status"] == "unknown"


def test_legacy_dates_are_projected_without_mutating_or_forging_original_audit():
    bundle = validate(raw_event())
    record = bundle.events[0].model_dump(mode="json")
    record["content_review"]["fields"]["event_date"].pop("date_review")
    original = deepcopy(record)
    result = enforce_reviewed_dates("events", record)
    assert record == original
    assert result["event_date"] is None
    assert result["content_review"]["claims"] == original["content_review"]["claims"]
    assert result["content_review"]["prior_materialized_digest"] == original["content_review"]["materialized_digest"]
    assert "materialized_digest" not in result["content_review"]
    publication = {"status": "known", "published_at": "2026-08-29", "source": "model_content_review",
                   "time_window": {"allowed": True, "status": "in_window"}}
    result = enforce_source_publication_dates(publication)
    assert result["published_at"] is None and result["time_window"]["allowed"] is False
    assert result["date_review_correction"]["original_materialized_value"] == publication


def test_stored_explicit_timezone_is_compared_using_repository_utc_semantics():
    raw = raw_event()
    stamp = "2026-08-29T09:30:00+08:00"
    raw["events"][0]["event_date"] = stamp
    raw["content_review"]["claims"][0]["field_values"]["event_date"] = stamp
    # A separate exact time citation makes this a valid timestamp fixture.
    text = "双方于2026年8月29日09时30分（北京时间）签订供货合同。"
    raw["content_review"]["claims"][0]["evidence"] = [{"passage_id": "time", "quote": text}]
    raw["content_review"]["claims"][0]["field_values"]["event_date_assessment"]["evidence"] = [{"passage_id": "time", "quote": text}]
    bundle = validate(raw, [*PASSAGES, Passage(passage_id="time", section="正文", text=text)])
    record = {**bundle.events[0].model_dump(mode="json"), "event_date": "2026-08-29T01:30:00"}
    assert enforce_reviewed_dates("events", record, stored_utc=True) == record
    assert enforce_reviewed_dates("events", {**record, "event_date": "2026-08-29T09:30:00"}, stored_utc=True)["event_date"] is None


def test_date_accepts_a_continuous_short_quote_in_the_verified_parent_passage():
    raw = raw_event()
    raw["content_review"]["claims"][0]["field_values"]["event_date_assessment"]["evidence"][0]["quote"] = OCCURRENCE[:-1]
    bundle = validate(raw)
    record = bundle.events[0].model_dump(mode="json")
    assert record["event_date"] == "2026-08-29"
    assert enforce_reviewed_dates("events", record) == record
    raw = raw_publication()
    raw["content_review"]["claims"][1]["field_values"]["source_publication"]["assessment"]["evidence"][0]["quote"] = PUBLICATION[3:-1]
    publication = validate(raw).content_review["source_publication"]
    assert publication["status"] == "known"
    assert enforce_source_publication_dates(publication) == publication


@pytest.mark.parametrize("damage", ["other_passage", "empty", "stitched", "title", "unverified_parent"])
def test_short_date_quote_does_not_waive_provenance(damage):
    raw = raw_event()
    claim = raw["content_review"]["claims"][0]
    quote = claim["field_values"]["event_date_assessment"]["evidence"][0]
    if damage == "other_passage":
        quote["passage_id"] = "another"
    elif damage == "empty":
        quote["quote"] = " "
    elif damage == "stitched":
        quote["quote"] = "双方于2026年8月29日供货合同"
    elif damage == "title":
        quote["passage_id"] = "title"
    else:
        claim["evidence"][0]["quote"] += "不存在的句子。"
    passages = [*PASSAGES, Passage(passage_id="another", section="正文", text=OCCURRENCE),
                Passage(passage_id="title", section="标题", text=OCCURRENCE)]
    bundle = validate(raw, passages)
    assert not bundle.events or bundle.events[0].event_date is None
