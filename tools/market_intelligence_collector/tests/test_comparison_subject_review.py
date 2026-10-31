"""Model-labelled comparison subjects, not a prose classifier or live-model proof."""
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
from mic.validate import BundleValidator


QUESTION = [{"question_id": "q1", "question": "收入金额及收入增速"}]


def subject_case(state="pending_review"):
    text = "本期收入同比增长20%；当前材料没有说明收入金额单位。"
    evidence = [{"passage_id": "p1", "quote": text}]
    growth = {"id": "growth", "kind": "observation", "status": "source_supported",
        "statement": "本期收入同比增长20%。", "reason": "人工标签：直接披露的收入增速。",
        "evidence": evidence, "depends_on": []}
    comparison = {"id": "comparison", "kind": "comparability", "status": state,
        "statement": "收入同比增速本身同比增长20%。",
        "reason": "人工标签：来源比较收入，未比较收入增速本身；不能授权所属指标的比较字段。",
        "evidence": evidence, "depends_on": ["growth"],
        "field_values": {"comparison": {"yoy": .2}, "comparison_input": {
            "yoy": {"value": 20, "unit": "percent", "evidence": evidence}}}}
    summary = {"id": "summary", "kind": "observation", "status": "source_supported",
        "statement": comparison["statement"], "reason": "人工标签：依赖被隔离的比较，必须同步隔离。",
        "evidence": evidence, "depends_on": ["comparison"]}
    raw = {"schema_version": "bundle_extraction_v0.3", "confidence": 0.0, "decision": "save_structured", "overall_score": 85,
        "facts": [{"fact_statement": growth["statement"], "evidence_locator": {"passage_id": "p1"}}],
        "metrics": [{"metric_name": "收入同比增速", "metric_value": 20, "unit": "%",
            "comparison": {"yoy": .2}, "evidence_locator": {"passage_id": "p1"}}],
        "brief": {"one_sentence": summary["statement"]},
        "content_review": {"protocol": PROTOCOL, "claims": [growth, comparison, summary],
            "bindings": {"/facts/0": ["growth"], "/metrics/0": ["growth"],
                         "/metrics/0/comparison": ["comparison"], "/brief/one_sentence": ["summary"]},
            "task_coverage": [{"question_id": "q1", "status": "partial",
                "answer_claim_ids": ["growth"], "uncovered_aspects": ["收入金额"],
                "reason": "增速可交付，金额缺证。"}]}}
    return raw, [Passage(passage_id="p1", section="body", text=text)]


def validate(raw, passages):
    report = BundleValidator({}).validate(raw, passages)
    assert report.schema_valid, report.errors
    return report.bundle


@pytest.mark.parametrize("state", ["pending_review", "unsupported", "inference"])
def test_model_rejected_subject_holds_comparison_and_dependencies_only(state):
    raw, passages = subject_case(state)
    original = deepcopy(raw)
    bundle = validate(raw, passages)
    assert raw == original
    assert bundle.metrics[0].metric_value == 20 and bundle.metrics[0].unit == "%"
    assert bundle.metrics[0].comparison == {}
    assert bundle.facts[0].fact_statement == "本期收入同比增长20%。"
    assert not bundle.brief.one_sentence
    assert bundle.content_review["claims"]["summary"]["reason"] == "dependency_not_supported"
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"
    # An otherwise valid quotation/conversion cannot revive a model-held field.
    bundle.content_review["model_review"]["task_coverage"][0]["answer_claim_ids"] = ["comparison"]
    assert finalize_task_coverage(bundle, QUESTION)[0]["reason"] == "coverage_answer_not_published"


@pytest.mark.parametrize("key,value,source_unit,output", [
    ("yoy", 25, "percent", .25), ("yoy_change_pp", 4, "percentage_point", 4)])
def test_percentage_metric_can_have_its_own_explicitly_supported_comparison(key, value, source_unit, output):
    raw, _ = subject_case()
    text = "收入同比增速本期20%，上年同期16%；增速本身同比提高25%，增加4个百分点。"
    evidence = [{"passage_id": "p1", "quote": text}]
    raw["brief"] = {}
    claims = raw["content_review"]["claims"]
    claims[:] = claims[:2]
    claims[0].update(statement="本期收入同比增速为20%。", evidence=evidence)
    claims[1].update(status="source_supported", statement="原文明确比较收入增速本身的两期值。",
        reason="人工标签：两期对象均为收入同比增速，引用明确给出该指标的变化。", evidence=evidence,
        field_values={"comparison": {key: output}, "comparison_input": {
            key: {"value": value, "unit": source_unit, "evidence": evidence}}})
    raw["metrics"][0]["comparison"] = {key: output}
    raw["content_review"]["bindings"].pop("/brief/one_sentence")
    bundle = validate(raw, [Passage(passage_id="p1", section="body", text=text)])
    assert bundle.metrics[0].metric_value == 20
    assert bundle.metrics[0].comparison == {key: output}
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"


def test_subject_rejection_survives_merge_storage_agent_readback_and_export(config, tmp_path):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore, loads_json
    from agent_trade_intel.persistence import ResultPersister
    from agent_trade_intel.reader import IntelligenceReader

    raw, passages = subject_case()
    bundle = validate(raw, passages)
    merged = MultiModelMerger(config).merge("link", "entity", [ModelContribution("model", "fixture", bundle)]).bundle
    finalize_task_coverage(merged, QUESTION)
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("entity", {}, {}, "offline", "offline")
    hit = SearchHit(query="offline", title="Report", url="https://example.invalid/subject", domain="example.invalid")
    link = repo.save_source_link(run, "offline", hit, hit.url, "official")
    repo.save_merged_analysis("entity", link, merged, {}, search_run_id=run)
    metric, = repo.get_metric_observations("entity")
    assert metric["comparison"] == {} and metric["metric_value"] == 20
    audit = repo.explain_source_analysis(link)["content_review"]
    assert audit["task_coverage"][0]["status"] == "partial"
    assert merged.content_review["model_review"]["claims"][1]["status"] == "pending_review"
    report = {"search_run_id": run, "metrics": [metric], "content_reviews": [audit],
              "task_coverage": audit["task_coverage"]}
    store, bus = SQLiteStore(tmp_path / "agent.db"), SQLiteStore(tmp_path / "bus.db")
    store.init_schema()
    bus.init_schema()
    task = {"task_id": "subject", "demand_id": "subject", "task_type": "mic_deep_collect",
            "idempotency_key": "subject", "target": {"target_id": "entity"}}
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence",
                        request={}, status="success", result=deepcopy(report)).finish()
    persister = ResultPersister(store)
    persister.save_run(task=task, ticket_id="subject", result=result)
    persister.save_mic_structures(task=task, result=result)
    row, = IntelligenceReader(store, bus).read_collection_status(demand_id="subject")["runs"]
    assert loads_json(row["result_json"], {}) == report
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("subject_review_export", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    exported = exporter.content_review(tmp_path, {"search_run_id": run})
    metric_row, = [row["record"] for row in exported["formal_records"] if row["record_type"] == "metrics"]
    assert metric_row["metric_value"] == 20 and metric_row["comparison"] == {}
    db.engine.dispose()
