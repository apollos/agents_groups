"""Regression for run_a81652e65aca, with separate bad and gold responses.

The saved decisions are REAL model output. `gold_response` is a human-labelled
contract example, not a successful LLM request or a test of LLM accuracy.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from mic.content_review import contract_diagnostics
from mic.content_review_policy import PROTOCOL
from mic.event_resolution import build_context, candidate, finalize, resolve
from mic.merge import ModelContribution, MultiModelMerger
from mic.money import normalize_quoted_price
from mic.schemas import Passage, SearchHit
from mic.store.database import Database
from mic.store.repository import Repository
from mic.validate import BundleValidator

SAMPLES = json.loads((Path(__file__).parent / "fixtures/content_review_run_a81652e65aca.json").read_text())["samples"]
# Human labels for this saved material; no production code imports these.
FOCUS = [["project_result", "award_catl", "award_yuanjing"], ["award_catl", "award_yuanjing"]]
PARTIES = {
    "project_result": {"subject": "河北任丘智弘项目", "product": "储能系统"},
    "award_catl": {"subject": "宁德时代", "counterparty": None, "product": "钠离子储能系统"},
    "award_yuanjing": {"subject": "远景能源", "counterparty": None, "product": "磷酸铁锂储能系统"},
}


def passages(sample):
    return [Passage(**p, section="正文" if p["passage_id"] != "title" else "标题") for p in sample["passages"]]


def validate(raw, sample):
    report = BundleValidator({}).validate(raw, passages(sample))
    assert report.schema_valid, report.errors
    return report.bundle


def gold_response(index, context):
    sample = SAMPLES[index]
    review = deepcopy(sample["model_review"])
    review.update(protocol=PROTOCOL, bindings={})
    claims = {c["id"]: c for c in review["claims"]}
    body = {p["passage_id"]: p["text"] for p in sample["passages"]}
    raw = {"decision": "save_structured", "overall_score": 76, "confidence": .85,
           "events": [], "metrics": [], "facts": [], "content_review": review}
    for i, original in enumerate(sample["events"]):
        cid = FOCUS[index][i]
        event = deepcopy(original)
        event.update(event_type="tender" if cid == "project_result" else "major_order", event_date="2026-09-15",
                     impact={"direction": "unclear", "channels": [], "horizon": "unclear", "magnitude_guess": "unknown"},
                     tracking_variables=[], entities=deepcopy(PARTIES[cid]))
        if event.get("metrics", {}).get("amount_input"):
            event["metrics"] = deepcopy(event["metrics"]["amount_input"])
        # Source p0 explicitly dates the announcement; p1/p2 give the lots.
        if not any(e["passage_id"] == "p0" for e in claims[cid]["evidence"]):
            claims[cid]["evidence"].append({"passage_id": "p0", "quote": body["p0"]})
        party_id = "roles_" + cid
        review["claims"].append({"id": party_id, "kind": "identity", "status": "source_supported",
            "statement": "只保留原文明示的获标方/项目和产品；未明确的交易对手为空。", "reason": "人工核对保存的p0/p1/p2",
            "depends_on": [cid], "evidence": deepcopy(claims[cid]["evidence"]),
            "field_values": {"entities": deepcopy(PARTIES[cid])}})
        review["bindings"][f"/events/{i}"] = [cid]
        review["bindings"][f"/events/{i}/entities"] = [party_id]
        evidence = deepcopy(claims[cid]["evidence"])
        comparisons = []
        for j, can in enumerate(context["candidates"]):
            focus = FOCUS[0][j]
            scope = "equivalent" if cid == focus else "contained_by" if focus == "project_result" else "disjoint"
            relation = "same_event" if scope == "equivalent" else "related" if scope == "contained_by" else "different"
            comparisons.append({"candidate_ref": can["ref"], "scope_relation": scope,
                "same_occurrence": scope != "disjoint", "stage_relation": "same", "relation": relation,
                "reason": "人工标签：同一标段关联；总体公告相关；另一标段不同。",
                "current_evidence": evidence,
                "candidate_evidence": deepcopy(can["focus"][0]["evidence"]) if can.get("focus") else
                    [{"passage_id": p["passage_id"], "quote": p["text"]} for p in can["passages"]]})
        event["event_resolution"] = {"reviewed": True, "current_claim_ids": [cid],
            "verdict": "same_event" if any(c["relation"] == "same_event" for c in comparisons) else "new",
            "reason": "人工标签：按当前具体事项范围比较。", "current_evidence": evidence, "comparisons": comparisons}
        raw["events"].append(event)
        raw["facts"].append({"fact_type": "order", "fact_statement": event["summary"],
            "entities": deepcopy(event["entities"]), "metrics": deepcopy(event["metrics"]),
            "period": "2026-09-15", "evidence_locator": deepcopy(event["evidence_locator"])})
        review["bindings"][f"/facts/{i}"] = [cid]
        review["bindings"][f"/facts/{i}/entities"] = [party_id]
    for i, original in enumerate(sample["metric_candidates"]):
        metric = deepcopy(original)
        metric.update(interpretation="", impact_channels=[], comparison={})
        raw["metrics"].append(metric)
        review["bindings"][f"/metrics/{i}"] = sample["model_review"]["bindings"][f"/metrics/{i}/metric_value"]
    return raw


@pytest.mark.parametrize("sample", SAMPLES)
def test_real_borrowed_observation_cannot_approve_identity_or_impact(sample):
    review = deepcopy(sample["model_review"])
    # Exercise the stricter checks with the actual old mappings. A version
    # rejection alone would not prove the borrowed-claim bug was repaired.
    review["protocol"] = PROTOCOL
    bundle = validate({"events": deepcopy(sample["events"]), "content_review": review}, sample)
    assert bundle.events
    assert all(e.entities == {} for e in bundle.events)
    assert all(e.impact.horizon == "unclear" and e.impact.channels == [] for e in bundle.events)
    assert any(h["reason"] == "claim_kind_mismatch" for h in bundle.content_review["held"])
    assert contract_diagnostics([bundle.content_review])["complete"] is False


@pytest.mark.parametrize("index", [0, 1])
def test_whole_record_review_preserves_actual_metric_names_values_units_and_dates(index):
    raw = gold_response(index, build_context(None, []))
    bundle = validate(raw, SAMPLES[index])
    assert len(bundle.metrics) == 6
    assert [(m.metric_name, m.metric_value, m.unit, m.period) for m in bundle.metrics] == [
        (m["metric_name"], m["metric_value"], m["unit"], m["period"]) for m in raw["metrics"]]
    assert all(e.event_date == "2026-09-15" and e.event_type != "unknown" for e in bundle.events)
    assert all(e.entities == PARTIES[FOCUS[index][i]] for i, e in enumerate(bundle.events))
    assert contract_diagnostics([bundle.content_review])["complete"] is True


def test_unknown_counterparty_does_not_erase_known_award_subject_or_become_owner():
    raw = gold_response(0, build_context(None, []))
    raw["events"][1]["entities"]["counterparty"] = "河北任丘智弘项目（招标方）"
    bundle = validate(raw, SAMPLES[0])
    assert bundle.events[1].entities["subject"] == "宁德时代"
    assert bundle.events[1].entities["counterparty"] is None
    assert any(h["reason"] == "replaced_by_explicit_reviewed_value" for h in bundle.content_review["held"])


def test_same_typed_identity_claim_cannot_publish_different_roles_in_fact_and_event():
    raw = gold_response(0, build_context(None, []))
    raw["facts"][1]["entities"]["subject"] = "错误公司"
    bundle = validate(raw, SAMPLES[0])
    assert bundle.facts[1].entities == bundle.events[1].entities == PARTIES["award_catl"]


def test_supported_impact_requires_explicit_analysis_value_not_just_its_label():
    raw = gold_response(0, build_context(None, []))
    observation = deepcopy(raw["content_review"]["claims"][0])
    observation.update(id="impact", kind="analysis", statement="有正向影响。")
    # Deliberately lacks field_values; status/kind labels cannot approve 1m.
    raw["content_review"]["claims"].append(observation)
    raw["content_review"]["bindings"]["/events/1/impact"] = ["impact"]
    raw["events"][1]["impact"].update(direction="positive", horizon="1m")
    bundle = validate(raw, SAMPLES[0])
    assert bundle.events[1].impact.direction == bundle.events[1].impact.horizon == "unclear"
    assert any(h["reason"] == "reviewed_value_missing_or_conflicting" for h in bundle.content_review["held"])


@pytest.mark.parametrize("field", SAMPLES[0]["price_fields"])
def test_real_price_amount_misencoding_keeps_quote_separate_from_total_money(field):
    body = {p["passage_id"]: p["text"] for p in SAMPLES[0]["passages"]}
    pid = "p2" if field["amount"] == 1.035 else "p1"
    result, reason = normalize_quoted_price(field, body, pid, [])
    assert reason == "source_quote"
    assert result["amount"] is None
    assert result["unit_price"] == field["amount"] and result["unit_price_unit"] == "元/Wh"
    assert result["price_evidence"]["quote"] in body[pid]
    assert result["price_input"] == field


def test_old_reviews_are_not_silently_relabelled_as_v2():
    sample = SAMPLES[0]
    bundle = validate({"events": deepcopy(sample["events"]), "content_review": deepcopy(sample["model_review"])}, sample)
    assert bundle.events == []
    assert contract_diagnostics([sample["model_review"]])["complete"] is False


def test_real_article_level_comparisons_cannot_become_an_arbitrary_match():
    first = validate(gold_response(0, build_context(None, [])), SAMPLES[0])
    finalize(first, build_context(None, []), passages(SAMPLES[0]), run_id="saved", link_id="first")
    context = build_context(None, [{**e.model_dump(mode="json"), "source_link_id": "first"} for e in first.events])
    raw = deepcopy(SAMPLES[1]["events"][0]["event_resolution"]["model_decision"])
    for comp, can in zip(raw["comparisons"], context["candidates"]):
        comp["candidate_ref"] = can["ref"]
    assert resolve(raw, context, SAMPLES[1]["passages"])["reason"] == "event_scope_unverified"
    # A model cannot call containment "same_event" after correctly reporting scope.
    gold = gold_response(1, context)["events"][0]["event_resolution"]
    gold["comparisons"][0]["relation"] = "same_event"
    assert resolve(gold, context, SAMPLES[1]["passages"])["reason"] == "event_scope_relation_conflict"


def test_source_quote_cannot_smuggle_benchmark_approval_in_scope():
    raw = gold_response(0, build_context(None, []))
    raw["metrics"][1]["scope"]["usable_as_price_benchmark"] = True
    bundle = validate(raw, SAMPLES[0])
    assert bundle.metrics[1].metric_value == 1.035
    assert bundle.metrics[1].scope["usable_as_price_benchmark"] is False


def test_event_comparison_cannot_change_the_current_event_claim():
    context = build_context(None, [])
    raw = gold_response(0, context)
    raw["events"][1]["event_resolution"]["current_claim_ids"] = ["award_yuanjing"]
    bundle = validate(raw, SAMPLES[0])
    finalize(bundle, context, passages(SAMPLES[0]), run_id="anchor", link_id="first")
    assert bundle.events[1].event_resolution["reason"] == "current_event_anchor_mismatch"


@pytest.mark.parametrize("field,value", [("scope_relation", {"bad": "type"}), ("stage_relation", [])])
def test_malformed_scope_judgment_is_pending_without_crashing(field, value):
    source = SAMPLES[0]["events"][0]
    context = build_context({"candidates": [candidate(source, "candidate")]}, [])
    raw = gold_response(1, context)["events"][0]["event_resolution"]
    raw["comparisons"][0][field] = value
    assert resolve(raw, context, SAMPLES[1]["passages"])["reason"] == "event_scope_unverified"


def test_bad_field_values_do_not_crash_the_review_or_approve_the_field():
    raw = gold_response(0, build_context(None, []))
    parties = next(c for c in raw["content_review"]["claims"] if c["id"] == "roles_award_catl")
    parties["field_values"] = "invalid model payload"
    bundle = validate(raw, SAMPLES[0])
    assert bundle.events[1].entities == {}
    assert bundle.content_review["claims"]["roles_award_catl"]["reason"] == "invalid_claim"


@pytest.mark.parametrize("change,reason", [("foreign_currency", "conflicting_price_currency"),
                                           ("wrong_number", "amount_not_supported_by_citation")])
def test_price_format_repair_cannot_change_currency_or_invent_a_number(change, reason):
    field = deepcopy(SAMPLES[0]["price_fields"][0])
    if change == "foreign_currency":
        field["currency"] = "USD"
    else:
        field["amount"] = 99.8
    result, actual = normalize_quoted_price(field, {p["passage_id"]: p["text"] for p in SAMPLES[0]["passages"]}, "p2", [])
    assert result is None and actual == reason


def test_saved_material_through_merge_both_databases_export_and_redelivery(config, tmp_path):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore
    from agent_trade_intel.persistence import ResultPersister
    from agent_trade_intel.semantic_event_store import history
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("target", {}, {}, "q", "m")
    events, reviews = [], []
    for index, sample in enumerate(SAMPLES):
        context = build_context(None, events)
        raw = gold_response(index, context)
        hit = SearchHit(query="中标", title="公示", url=sample["events"][0]["source"]["url"], domain="media.test")
        lid = repo.save_source_link(run, "query", hit, hit.url, "media")
        merged = MultiModelMerger(config).merge(lid, "target", [ModelContribution("m", "human-labelled", validate(raw, sample))])
        bundle = merged.bundle
        finalize(bundle, context, passages(sample), run_id=run, link_id=lid)
        repo.save_merged_analysis("target", lid, bundle, {}, search_run_id=run)
        reviews.append(bundle.content_review)
        events += [{**e.model_dump(mode="json"), "source_link_id": lid, "source": sample["events"][0]["source"]} for e in bundle.events]
    assert len(repo.get_metric_observations("target")) == 12
    assert all(e.event_type != "unknown" for e in first_events(repo, "target"))
    assert contract_diagnostics(reviews)["complete"] is True
    s = SQLiteStore(tmp_path / "agent.db")
    s.init_schema()
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
        status="success", result={"search_run_id": run, "event_resolution_protocol": "semantic_event_v1",
        "content_review_protocol": PROTOCOL, "all_events": list(reversed(events))})
    persister, task = ResultPersister(s), {"target": {"target_id": "target", "company_name": "宁德时代"}}
    counts = persister.save_mic_structures(task=task, result=result)
    assert (counts["events"], counts["events_linked"], counts["events_pending"]) == (3, 2, 0)
    replay = persister.save_mic_structures(task=task, result=result)
    assert replay["events_replayed"] == 5 and replay["events"] == replay["events_pending"] == 0
    assert len(history(s, "target")["candidates"]) == 3
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("real_review_export", script)
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)
    exported = acceptance.content_review(tmp_path, {"search_run_id": run})
    assert sum(r["record_type"] == "metrics" for r in exported["formal_records"]) == 12
    assert all(r["record"]["content_review"]["protocol"] == PROTOCOL for r in exported["formal_records"])


def first_events(repo, target):
    from mic.schemas import EventCard
    return [EventCard.model_validate(e) for e in repo.get_recent_events(target)]
