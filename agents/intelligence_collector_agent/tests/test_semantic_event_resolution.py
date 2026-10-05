"""Offline contract tests; labelled model doubles do NOT certify live LLM accuracy."""
import copy
import json
from pathlib import Path

import pytest

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.persistence import ResultPersister
from agent_trade_intel.semantic_event_store import history
from mic.event_resolution import PROTOCOL, build_context, candidate, finalize, resolve
from mic.schemas import BundleExtraction

FIXTURE = json.loads((Path(__file__).parent / "fixtures/semantic_event_961feb_live.json").read_text())
TASK = {"target": {"target_id": "company_300750", "ticker": "300750.SZ", "company_name": "宁德时代"}}


def citation(passages, pid=None):
    p = next((p for p in passages if p["passage_id"] == pid), passages[0])
    return [{"passage_id": p["passage_id"], "quote": p["text"]}]


def decision(event, context, *, match=None, relation="same_event"):
    current = citation(event["source_context"], event["evidence_locator"]["passage_id"])
    return {"reviewed": True, "verdict": relation if match else "new",
            "reason": "对照原文，项目、标段、获标方及公告阶段一致。" if match else "候选为空或均为其他事项。",
            "current_evidence": current,
            "comparisons": [{"candidate_ref": c["ref"], "relation": relation if c["ref"] == match else "different",
                             "scope_relation": "equivalent" if c["ref"] == match else "disjoint",
                             "same_occurrence": c["ref"] == match,
                             "stage_relation": "progression" if relation == "follow_up" else "same",
                             "reason": "两处原文描述同一标段中标。" if c["ref"] == match else "总体公告与分标段中标或另一标段不同。",
                             "current_evidence": current, "candidate_evidence": citation(c["passages"])}
                            for c in context["candidates"]]}


def finalize_event(event, context, raw, index):
    event = copy.deepcopy(event)
    bundle = BundleExtraction(events=[{**event, "event_resolution": raw}])
    finalize(bundle, context, event["source_context"], run_id=f"fixture_{index}", link_id=event["source_link_id"])
    out = bundle.events[0].model_dump(mode="json")
    out.update(source_link_id=event["source_link_id"], source=event["source"])
    return out


def real_rows():
    original = FIXTURE["events"]
    empty = build_context(None, [])
    first = [finalize_event(e, empty, decision(e, empty), i) for i, e in enumerate(original[:2])]
    context = build_context(None, first)
    # Gold labels for this fixed material: the two lot awards each match; the
    # project-level notice is separate. No production code contains these labels.
    second = []
    for i, match in [(2, 0), (3, None), (4, 1)]:
        raw = decision(original[i], context, match=context["candidates"][match]["ref"] if match is not None else None)
        second.append(finalize_event(original[i], context, raw, i))
    return first + second


def store(tmp_path):
    s = SQLiteStore(tmp_path / "data.db")
    s.init_schema()
    return s


def save(s, events, task=TASK):
    r = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={})
    r.status = "success"
    r.result = {"search_run_id": "fixture", "event_resolution_protocol": PROTOCOL, "all_events": events}
    return ResultPersister(s).save_mic_structures(task=task, result=r)


def rows(s, table):
    with s.session() as con:
        return [dict(r) for r in con.execute("SELECT * FROM " + table)]


def test_real_five_rows_keep_three_matters_two_sources_even_when_report_sorted(tmp_path):
    s = store(tmp_path)
    events = real_rows()
    counts = save(s, list(reversed(events)))
    assert (counts["events"], counts["events_linked"], counts["events_pending"]) == (3, 2, 0)
    assert sorted(r["source_count"] for r in rows(s, "structured_events")) == [1, 2, 2]
    assert all(r["dedup_status"] == "semantic" and r["business_key"] is None for r in rows(s, "structured_events"))
    assert len(rows(s, "structured_event_sources")) == 5
    replay = save(s, events)
    assert replay["events_replayed"] == 5 and replay["events"] == replay["events_linked"] == 0


def test_next_cycle_retrieves_prior_original_context_and_adds_source(tmp_path):
    s = store(tmp_path)
    first = real_rows()[0]
    save(s, [first])
    context = build_context(history(s, "company_300750"), [])
    assert context["candidates"][0]["passages"][0]["text"].startswith("2026年9月15日")
    second = FIXTURE["events"][2]
    current = finalize_event(second, context, decision(second, context, match=context["candidates"][0]["ref"]), 10)
    counts = save(s, [current])
    assert counts["events"] == 0 and counts["events_linked"] == 1
    assert len(rows(s, "structured_events")) == 1


def test_follow_up_is_a_linked_new_progress_record(tmp_path):
    s = store(tmp_path)
    save(s, real_rows()[:1])
    ctx = build_context(history(s, "company_300750"), [])
    e = copy.deepcopy(FIXTURE["events"][0])
    e.update(summary="宁德时代中标后完成任丘智弘二标段储能系统交付。", event_date="2026-10-05")
    e["source_context"] = [{"passage_id": "p2", "text": e["summary"]}]
    e["evidence_locator"] = {"passage_id": "p2", "excerpt": e["summary"]}
    raw = decision(e, ctx, match=ctx["candidates"][0]["ref"], relation="follow_up")
    raw["reason"] = "同一项目中标之后的交付进展，须保留新事项。"
    counts = save(s, [finalize_event(e, ctx, raw, 20)])
    assert counts["events_follow_up"] == counts["events"] == 1
    assert len(rows(s, "structured_events")) == 2
    assert len(rows(s, "event_progress_links")) == 1


@pytest.mark.parametrize("change", ["missing", "fabricated_quote", "unknown_ref", "incomplete", "uncertain"])
def test_bad_comparison_is_pending_never_confirmed_new(tmp_path, change):
    s = store(tmp_path)
    first = real_rows()[0]
    save(s, [first])
    ctx = build_context(history(s, "company_300750"), [])
    e = FIXTURE["events"][2]
    raw = decision(e, ctx, match=ctx["candidates"][0]["ref"])
    if change == "missing": raw = {}
    if change == "fabricated_quote": raw["comparisons"][0]["candidate_evidence"][0]["quote"] = "原文没有这句中标项目描述"
    if change == "unknown_ref": raw["comparisons"][0]["candidate_ref"] = "unknown"
    if change == "incomplete": ctx["complete"] = False
    if change == "uncertain": raw["comparisons"][0]["relation"] = "uncertain"
    event = finalize_event(e, ctx, raw, 30)
    counts = save(s, [event])
    assert counts["events_pending"] == 1 and counts["events"] == counts["events_linked"] == 0
    assert len(rows(s, "structured_events")) == 1
    assert len(rows(s, "pending_event_resolutions")) == 1


def test_stale_candidate_fingerprint_and_cross_target_reference_are_rejected(tmp_path):
    s = store(tmp_path)
    save(s, real_rows()[:1])
    ctx = build_context(history(s, "company_300750"), [])
    e = FIXTURE["events"][2]
    current = finalize_event(e, ctx, decision(e, ctx, match=ctx["candidates"][0]["ref"]), 40)
    other = {"target": {"target_id": "unrelated", "company_name": "其他公司"}}
    assert save(s, [current], task=other)["events_pending"] == 1
    current["event_resolution"]["candidate_fingerprint"] = "stale"
    assert save(s, [current])["events_pending"] == 1


def test_identical_summary_from_different_url_adds_evidence_not_replay(tmp_path):
    s = store(tmp_path)
    first = real_rows()[0]
    save(s, [first])
    ctx = build_context(history(s, "company_300750"), [])
    second = copy.deepcopy(first)
    second["source"]["url"] = "https://example.test/repost"
    raw = decision(second, ctx, match=ctx["candidates"][0]["ref"])
    counts = save(s, [finalize_event(second, ctx, raw, 50)])
    assert counts["events_linked"] == 1 and counts["events_replayed"] == 0


def test_candidate_retrieval_is_not_gated_by_subject_project_or_event_type():
    first = real_rows()[:2]
    ctx = build_context(None, first)
    assert len(ctx["candidates"]) == 2
    assert {c["entities"]["subject"] for c in ctx["candidates"]} == {"宁德时代", "河北任丘智弘储能项目"}
    # The short "河北任丘" / "同一项目" second source sees both complete originals.
    assert all("智弘" in c["passages"][0]["text"] for c in ctx["candidates"])


def test_candidate_overflow_and_cache_gaps_are_explicit():
    e = real_rows()[0]
    candidates = [candidate(e, f"event:{i}") for i in range(25)]
    ctx = build_context({"candidates": candidates, "complete": True}, [])
    assert len(ctx["candidates"]) == 24 and ctx["candidate_count"] == 25 and not ctx["complete"]
    assert not build_context(None, [{"summary": "legacy cached row"}])["complete"]


def test_multiple_semantic_matches_do_not_arbitrarily_choose_one():
    e = real_rows()[0]
    ctx = build_context({"candidates": [candidate(e, "event:a"), candidate(e, "event:b")]}, [])
    raw = decision(e, ctx, match="event:a")
    raw["comparisons"][1]["relation"] = "same_event"
    raw["comparisons"][1].update(scope_relation="equivalent", same_occurrence=True, stage_relation="same")
    assert resolve(raw, ctx, e["source_context"])["reason"] == "multiple_matching_candidates"


def test_identical_amount_and_capacity_do_not_override_different_project_decision(tmp_path):
    s = store(tmp_path)
    save(s, real_rows()[:1])
    ctx = build_context(history(s, "company_300750"), [])
    event = copy.deepcopy(FIXTURE["events"][0])
    event["summary"] = "宁德时代中标另一个天津滨海项目，金额4141.622万元，容量40MWh。"
    event["source_context"] = [{"passage_id": "p2", "text": event["summary"]}]
    event["evidence_locator"] = {"passage_id": "p2", "excerpt": event["summary"]}
    raw = decision(event, ctx)
    raw["comparisons"][0]["reason"] = "天津滨海与河北任丘智弘是不同项目，金额和容量相同不足以合并。"
    counts = save(s, [finalize_event(event, ctx, raw, 60)])
    assert counts["events"] == 1 and counts["events_linked"] == 0


def test_changed_event_cannot_reuse_old_model_verdict(tmp_path):
    s = store(tmp_path)
    event = real_rows()[0]
    event["summary"] = "改写后未经语义复核的新内容"
    assert save(s, [event])["events_pending"] == 1
    assert rows(s, "structured_events") == []


def test_pending_only_output_is_not_reported_usable(tmp_path):
    from agent_trade_intel.quality import QualityGate
    e = real_rows()[0]
    e["event_resolution"]["status"] = "pending"
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
                        status="success", result={"event_resolution_protocol": PROTOCOL,
                                                  "all_events": [e], "structured_outputs": {"events": 1}})
    result.quality["event_ledger"] = save(store(tmp_path), [e])
    quality = QualityGate({}).evaluate(result)
    assert not quality["usable"] and quality["structured_output_count"] == 0
    assert any(i["issue_type"] == "event_resolution_pending" for i in quality["issues"])


def test_multiple_extractions_never_collapse_different_projects_by_type_counterparty():
    from mic.config import MICConfig
    from mic.merge import ModelContribution, MultiModelMerger
    e = real_rows()[0]
    other = copy.deepcopy(e)
    other["summary"] = "另一个项目，恰好有相同类型和交易对手"
    inputs = [ModelContribution(model_config_id=str(i), provider="fixture",
                               bundle=BundleExtraction(events=[event])) for i, event in enumerate([e, other])]
    events, _ = MultiModelMerger(MICConfig(raw={}))._merge_events(inputs)
    assert len(events) == 2
    bundle = BundleExtraction(events=events)
    finalize(bundle, build_context(None, []), e["source_context"], run_id="multi", link_id="source",
             multiple_extractions=True)
    assert all(ev.event_resolution["status"] == "pending" for ev in bundle.events)
    assert all(ev.event_resolution["reason"] == "multiple_extractions_require_joint_comparison" for ev in bundle.events)
