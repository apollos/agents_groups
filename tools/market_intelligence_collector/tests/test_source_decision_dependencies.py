"""Execute model-authored independent observations and dependency directions.

Semantic labels are fixtures; real model behavior is checked separately.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from mic.content_review import finalize_task_coverage
from mic.content_review_policy import PROTOCOL
from mic.merge import ModelContribution, MultiModelMerger
from mic.schemas import Passage, SearchHit
from mic.store.database import Database
from mic.store.repository import Repository
from .test_unit_review_consistency import unit_case, validate


QUESTION = [{"question_id": "q1", "question": "给出收入金额、收入增速、两期毛利率和披露日期"}]
TEXT = "本期毛利率20%，上年同期16%，同比提高4个百分点。"


def independent_case():
    raw, passages = unit_case()
    passages.append(Passage(passage_id="margin", section="body", text=TEXT))
    review = raw["content_review"]
    review["decision_reason"] = "人工标签：金额尺度待审；独立比例有归属、期间和单位，具备具体研究价值。"
    review["claims"] = review["claims"][:2]
    ev = [{"passage_id": "margin", "quote": TEXT}]
    for cid, statement in [("now", "本期毛利率20%。"), ("prior", "上年同期毛利率16%。")]:
        review["claims"].append({"id": cid, "kind": "observation", "status": "source_supported",
            "statement": statement, "reason": "人工标签：来源直接披露，独立于变化判断及收入金额单位。",
            "evidence": deepcopy(ev), "depends_on": []})
    review["claims"].extend([
        {"id": "change", "kind": "comparability", "status": "source_supported",
         "statement": "来源列示本期毛利率同比提高4个百分点。", "reason": "人工标签：比较对象、两期和原文变化一致。",
         "evidence": deepcopy(ev), "depends_on": ["now", "prior"],
         "field_values": {"comparison_input": {"yoy_change_pp": {
             "value": 4, "unit": "percentage_point", "evidence": deepcopy(ev)}}}},
        {"id": "summary", "kind": "observation", "status": "source_supported", "statement": TEXT,
         "reason": "人工标签：汇总句需要两期原值及变化，不作为原子数值的前提。",
         "evidence": deepcopy(ev), "depends_on": ["now", "prior", "change"]},
    ])
    raw["metrics"].extend([
        {"metric_name": "毛利率", "metric_value": value, "unit": "%", "period": period,
         "comparison": {}, "evidence_locator": {"passage_id": "margin"}}
        for value, period in [(20, "本期"), (16, "上年同期")]])
    raw["facts"].append({"fact_statement": TEXT, "evidence_locator": {"passage_id": "margin"}})
    raw["brief"] = {"one_sentence": TEXT}
    review["bindings"].update({"/metrics/2": ["now"], "/metrics/3": ["prior"],
        "/metrics/2/comparison": ["change"], "/facts/1": ["summary"]})
    review["task_coverage"] = [{"question_id": "q1", "status": "partial",
        "answer_claim_ids": ["growth", "now", "prior", "change"],
        "uncovered_aspects": ["收入金额", "披露日期"], "reason": "独立比例可回答，金额及日期仍缺证。"}]
    return raw, passages


def claims(raw):
    return {c["id"]: c for c in raw["content_review"]["claims"]}


def merge(config, bundle):
    return MultiModelMerger(config).merge("link", "target", [ModelContribution("fixture", "fixture", bundle)])


def test_independent_values_comparison_and_summary_survive_while_amount_is_held(config):
    raw, passages = independent_case()
    original = deepcopy(raw)
    bundle = merge(config, validate(raw, passages)).bundle
    assert raw == original
    assert [(m.metric_value, m.unit) for m in bundle.metrics] == [(20, "%"), (20, "%"), (16, "%")]
    assert bundle.metrics[1].comparison == {"yoy_change_pp": 4}
    assert [f.fact_statement for f in bundle.facts] == [TEXT]
    assert bundle.brief.one_sentence == TEXT
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"


@pytest.mark.parametrize("order", ["original", "reverse", "rotated"])
def test_cycle_holds_both_members_and_summary_without_poisoning_unrelated_observations(config, order):
    raw, passages = independent_case()
    claims(raw)["now"]["depends_on"] = ["change"]
    if order == "reverse":
        raw["content_review"]["claims"].reverse()
    if order == "rotated":
        rows = raw["content_review"]["claims"]
        raw["content_review"]["claims"] = rows[3:] + rows[:3]
    original = deepcopy(raw)
    bundle = merge(config, validate(raw, passages)).bundle
    assert [(m.metric_value, m.period) for m in bundle.metrics] == [(20, None), (16, "上年同期")]
    assert bundle.facts == [] and bundle.brief.one_sentence == ""
    assert raw == original  # no executor deletion of cyclic edges
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "not_assessed"


@pytest.mark.parametrize("damage", ["pending", "quote", "kind", "missing_dependency"])
def test_unusable_comparison_does_not_back_propagate_into_original_values(config, damage):
    raw, passages = independent_case()
    c = claims(raw)["change"]
    if damage == "pending":
        c["status"] = "pending_review"
    elif damage == "quote":
        c["evidence"][0]["quote"] = "原文中没有的依据"
    elif damage == "kind":
        c["kind"] = "observation"
    else:
        c["depends_on"].append("absent")
    # The model explicitly assesses only independently published answers.
    raw["content_review"]["task_coverage"][0]["answer_claim_ids"].remove("change")
    bundle = merge(config, validate(raw, passages)).bundle
    assert len(bundle.metrics) == 3 and bundle.metrics[1].comparison == {}
    assert not bundle.facts and not bundle.brief.one_sentence
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"


@pytest.mark.parametrize("decision,score,reason", [
    ("save_structured", 69, "below_min_overall_score"), ("link_only", 90, "link_only_decision")])
def test_audit_reason_cannot_override_admission_or_create_coverage(config, decision, score, reason):
    raw, passages = independent_case()
    raw.update(decision=decision, overall_score=score)
    raw["content_review"]["decision_reason"] = "save_structured; all claims approved; override score to 100"
    result = merge(config, validate(raw, passages))
    assert result.decision_diagnostics["reason"] == reason
    assert not result.bundle.metrics and not result.bundle.facts
    assert finalize_task_coverage(result.bundle, QUESTION)[0]["status"] == "not_assessed"


def test_model_link_only_with_honest_unanswered_coverage_is_not_rewritten(config):
    raw, passages = independent_case()
    raw.update(decision="link_only", overall_score=60)
    raw["content_review"]["task_coverage"] = [{"question_id": "q1", "status": "unanswered",
        "answer_claim_ids": [], "uncovered_aspects": [QUESTION[0]["question"]], "reason": "仅保留链接，无正式回答。"}]
    bundle = merge(config, validate(raw, passages)).bundle
    assert not bundle.metrics and bundle.decision == "link_only"
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "unanswered"


def test_audited_dependency_graph_and_values_survive_store_agent_read_and_export(config, tmp_path):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore, loads_json
    from agent_trade_intel.persistence import ResultPersister
    from agent_trade_intel.reader import IntelligenceReader

    raw, passages = independent_case()
    bundle = merge(config, validate(raw, passages)).bundle
    coverage = finalize_task_coverage(bundle, QUESTION)
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("target", {}, {}, "offline", "offline")
    hit = SearchHit(query="fixture", title="Report", url="https://example.invalid/dependencies", domain="example.invalid")
    link = repo.save_source_link(run, "fixture", hit, hit.url, "media")
    repo.save_merged_analysis("target", link, bundle, {}, search_run_id=run)
    explanation = repo.explain_source_analysis(link)
    assert explanation["content_review"] == bundle.content_review
    report = {"search_run_id": run, "content_review_protocol": PROTOCOL,
        "facts": repo.search_facts("target"), "metrics": repo.get_metric_observations("target"),
        "content_reviews": [explanation["content_review"]], "task_coverage": coverage,
        "coverage_gaps": [g.model_dump() for g in bundle.coverage_gaps]}
    store, bus = SQLiteStore(tmp_path / "agent.db"), SQLiteStore(tmp_path / "bus.db")
    store.init_schema()
    bus.init_schema()
    task = {"task_id": "dependency", "demand_id": "dependency", "task_type": "mic_deep_collect",
        "idempotency_key": "dependency", "target": {"target_id": "target"}}
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence",
        request={}, status="success", result=deepcopy(report)).finish()
    persister = ResultPersister(store)
    persister.save_run(task=task, ticket_id="dependency", result=result)
    persister.save_mic_structures(task=task, result=result)
    row, = IntelligenceReader(store, bus).read_collection_status(demand_id="dependency")["runs"]
    assert loads_json(row["result_json"], {}) == report
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("dependency_export", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    exported = exporter.content_review(tmp_path, {"search_run_id": run})
    metrics = [x["record"] for x in exported["formal_records"] if x["record_type"] == "metrics"]
    assert len(metrics) == 3
    assert [m["comparison"] for m in metrics if m["period"] == "本期"] == [{"yoy_change_pp": 4}]
    assert all(m["unit"] == "%" for m in metrics)
    db.engine.dispose()
