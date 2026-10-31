"""One comparison projection for numbers, statuses and explicitly bound context.

Fixtures supply semantic labels; source understanding is checked in live evidence.
"""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from mic.content_review import finalize_task_coverage
from mic.merge import ModelContribution, MultiModelMerger
from mic.schemas import Passage, SearchHit
from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository
from .test_comparison_input_review import validate, QUESTION
from .test_comparison_normalization import input_case


NA = {"yoy": None, "yoy_status": "not_applicable"}


def status_case(value=None):
    raw = input_case()
    text = "报告期收入240万元，同比栏标注不适用；对比期间不同，样本数为0。"
    evidence = [{"passage_id": "p0", "quote": text}]
    for claim in raw["content_review"]["claims"]:
        claim["evidence"] = deepcopy(evidence)
    rate = raw["content_review"]["claims"][1]
    rate.update(statement="来源将收入同比标注为不适用。", reason="人工标签：来源明确给出比较状态和范围。",
                field_values={"comparison": deepcopy(NA if value is None else value)})
    raw["content_review"]["claims"][2]["statement"] = text
    raw["brief"]["one_sentence"] = text
    return raw, [Passage(passage_id="p0", section="body", text=text)]


@pytest.mark.parametrize("key", ["yoy", "mom", "qoq", "change_ratio"])
def test_empty_metric_receives_supported_status_without_duplicate_model_output(key):
    value = {key: None, key + "_status": "not_applicable"}
    raw, passages = status_case(value)
    before = deepcopy(raw)
    bundle = validate(raw, passages)
    assert bundle.metrics[0].comparison == value
    assert bundle.brief.one_sentence and finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"
    assert raw == before and bundle.content_review["model_review"] == before["content_review"]


@pytest.mark.parametrize("binding", ["status_only", "split", "nested"])
def test_child_binding_does_not_publish_unbound_siblings(binding):
    value = {**NA, "basis": {"period_relation": "different", "comparable": False}}
    raw, passages = status_case(value)
    bindings = raw["content_review"]["bindings"]
    bindings.pop("/metrics/0/comparison")
    if binding == "nested":
        bindings["/metrics/0/comparison/basis/period_relation"] = ["rate"]
        expected = {"basis": {"period_relation": "different"}}
    else:
        bindings["/metrics/0/comparison/yoy_status"] = ["rate"]
        expected = {"yoy_status": "not_applicable"}
        if binding == "split":
            bindings["/metrics/0/comparison/yoy"] = ["rate"]
            expected["yoy"] = None
    bundle = validate(raw, passages)
    assert bundle.metrics[0].comparison == expected
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"


def test_nested_context_false_and_zero_are_published_from_explicit_judgment():
    value = {"basis": {"comparable": False, "sample_size": 0}, "note": "different periods"}
    raw, passages = status_case(value)
    bundle = validate(raw, passages)
    assert bundle.metrics[0].comparison == value
    assert bundle.metrics[0].comparison["basis"]["comparable"] is False


def test_disjoint_nested_claims_merge_without_losing_either_context_member():
    raw, passages = status_case({"basis": {"period_relation": "different"}})
    other = deepcopy(raw["content_review"]["claims"][1])
    other.update(id="context", field_values={"comparison": {"basis": {"comparable": False}}})
    raw["content_review"]["claims"].append(other)
    raw["content_review"]["bindings"]["/metrics/0/comparison"].append("context")
    bundle = validate(raw, passages)
    assert bundle.metrics[0].comparison == {"basis": {"period_relation": "different", "comparable": False}}


def test_pending_child_overrides_supported_parent_without_leaking_its_value():
    raw, passages = status_case({"basis": {"period_relation": "different", "comparable": False}})
    other = deepcopy(raw["content_review"]["claims"][1])
    other.update(id="child", status="pending_review")
    raw["content_review"]["claims"].append(other)
    raw["content_review"]["bindings"]["/metrics/0/comparison/basis/comparable"] = ["child"]
    bundle = validate(raw, passages)
    assert bundle.metrics[0].comparison == {"basis": {"period_relation": "different"}}


@pytest.mark.parametrize("damage", ["inference", "pending_review", "unsupported", "quote", "binding", "kind", "dependency"])
def test_invalid_status_cannot_become_a_formal_coverage_answer(damage):
    raw, passages = status_case()
    rate = raw["content_review"]["claims"][1]
    if damage in {"inference", "pending_review", "unsupported"}: rate["status"] = damage
    elif damage == "quote": rate["evidence"][0]["quote"] = "不是原文"
    elif damage == "kind": rate["kind"] = "observation"
    elif damage == "dependency": rate["depends_on"] = ["unknown"]
    else: raw["content_review"]["bindings"].pop("/metrics/0/comparison")
    bundle = validate(raw, passages)
    assert bundle.metrics[0].comparison == {} and bundle.metrics[0].metric_value == 240
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "not_assessed"
    if damage != "binding": assert not bundle.brief.one_sentence


@pytest.mark.parametrize("damage", ["status", "numeric", "child", "nested", "ancestor_null", "bool_number"])
def test_explicit_conflict_is_held_instead_of_rewritten(damage):
    raw, passages = status_case()
    if damage in {"status", "child"}:
        raw["metrics"][0]["comparison"] = {"yoy_status": "applicable"}
        if damage == "child":
            b = raw["content_review"]["bindings"]
            b["/metrics/0/comparison/yoy_status"] = b.pop("/metrics/0/comparison")
    elif damage == "numeric": raw["metrics"][0]["comparison"] = {"yoy": .5}
    else:
        value = {"basis": {"comparable": False}}
        raw["content_review"]["claims"][1]["field_values"]["comparison"] = value
        raw["metrics"][0]["comparison"] = {"basis": {"comparable": True if damage == "nested" else 0}}
        if damage == "ancestor_null":
            raw["metrics"][0]["comparison"] = {"basis": None}
            b = raw["content_review"]["bindings"]
            b["/metrics/0/comparison/basis/comparable"] = b.pop("/metrics/0/comparison")
    before = deepcopy(raw)
    bundle = validate(raw, passages)
    assert bundle.content_review["claims"]["rate"]["status"] == "pending_review"
    assert not bundle.brief.one_sentence and bundle.metrics[0].metric_value == 240
    assert raw == before and bundle.content_review["model_review"] == before["content_review"]


def test_conflicting_status_claims_propagate_to_the_shared_summary():
    raw, passages = status_case()
    other = deepcopy(raw["content_review"]["claims"][1])
    other.update(id="other", field_values={"comparison": {"yoy_status": "applicable"}})
    raw["content_review"]["claims"].append(other)
    raw["content_review"]["bindings"]["/metrics/0/comparison"].append("other")
    bundle = validate(raw, passages)
    assert bundle.metrics[0].comparison == {} and not bundle.brief.one_sentence


def test_rate_and_status_in_separate_child_bindings_cannot_publish_a_contradiction():
    raw = input_case()
    other = deepcopy(raw["content_review"]["claims"][1])
    other.update(id="na", field_values={"comparison": {"yoy_status": "not_applicable"}})
    raw["content_review"]["claims"].append(other)
    b = raw["content_review"]["bindings"]
    b.pop("/metrics/0/comparison")
    b.update({"/metrics/0/comparison/yoy": ["rate"], "/metrics/0/comparison/yoy_status": ["na"]})
    bundle = validate(raw)
    assert bundle.metrics[0].comparison == {} and not bundle.brief.one_sentence
    assert bundle.content_review["claims"]["na"]["status"] == "pending_review"


def test_numeric_and_non_numeric_members_share_the_omitted_output_path():
    raw = input_case()
    raw["content_review"]["claims"][1]["field_values"]["comparison"] = {"qoq": None, "qoq_status": "not_applicable"}
    bundle = validate(raw)
    assert bundle.metrics[0].comparison == {"yoy": 1.4742, "qoq": None, "qoq_status": "not_applicable"}


def test_lone_null_does_not_imply_not_applicable_or_a_published_answer():
    raw, passages = status_case({"yoy": None})
    bundle = validate(raw, passages)
    assert bundle.metrics[0].comparison == {"yoy": None}
    assert finalize_task_coverage(bundle, QUESTION)[0]["reason"] == "coverage_answer_not_published"


def saved_case(config, tmp_path, value=None):
    raw, passages = status_case(value)
    bundle = validate(raw, passages)
    merged = MultiModelMerger(config).merge("link", "entity", [ModelContribution("model", "fixture", bundle)]).bundle
    finalize_task_coverage(merged, QUESTION)
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("entity", {}, {}, "offline", "offline")
    hit = SearchHit(query="offline", title="Report", url="https://example.invalid/status", domain="example.invalid")
    link = repo.save_source_link(run, "offline", hit, hit.url, "official")
    repo.save_merged_analysis("entity", link, merged, {}, search_run_id=run)
    return db, repo, run, link, merged


@pytest.mark.parametrize("value", [NA, {"basis": {"comparable": False, "sample_size": 0}}])
def test_comparison_status_and_context_survive_cache_agent_and_export(config, tmp_path, value):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore, loads_json
    from agent_trade_intel.persistence import ResultPersister
    from agent_trade_intel.reader import IntelligenceReader

    db, repo, run, link, _ = saved_case(config, tmp_path, value)
    report = {"search_run_id": run, "facts": repo.search_facts("entity"),
              "metrics": repo.get_metric_observations("entity"),
              "content_review": repo.explain_source_analysis(link)["content_review"]}
    assert report["metrics"][0]["comparison"] == value
    assert report["content_review"]["task_coverage"][0]["status"] == "partial"
    assert repo.clone_latest_analysis(link, "clone", "entity", reviewed_only=True)["metrics"] == 1
    assert all(r["comparison"] == value for r in repo.get_metric_observations("entity"))
    store, bus = SQLiteStore(tmp_path / "agent.db"), SQLiteStore(tmp_path / "bus.db")
    store.init_schema()
    bus.init_schema()
    task = {"task_id": "status", "demand_id": "status", "task_type": "mic_deep_collect",
            "idempotency_key": "status", "target": {"target_id": "entity"}}
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence",
                        request={}, status="success", result=deepcopy(report)).finish()
    persister = ResultPersister(store)
    persister.save_run(task=task, ticket_id="status", result=result)
    persister.save_mic_structures(task=task, result=result)
    row, = IntelligenceReader(store, bus).read_collection_status(demand_id="status")["runs"]
    assert loads_json(row["result_json"], {}) == report
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("status_export", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    exported = exporter.content_review(tmp_path, {"search_run_id": run})
    metric, = [r["record"] for r in exported["formal_records"] if r["record_type"] == "metrics"]
    assert metric["comparison"] == value
    db.engine.dispose()


@pytest.mark.parametrize("damage", ["status_value", "missing_audit", "nested_value"])
def test_stored_non_numeric_comparison_is_not_trusted_without_matching_review(config, tmp_path, damage):
    value = {"basis": {"comparable": False}} if damage == "nested_value" else NA
    db, repo, _, link, _ = saved_case(config, tmp_path, value)
    with db.session() as session:
        row = session.scalars(select(m.MetricObservationRow)).one()
        if damage == "missing_audit":
            audit = deepcopy(row.content_review)
            audit["fields"].pop("comparison")
            row.content_review = audit
        else:
            row.comparison = {"basis": {"comparable": True}} if damage == "nested_value" else {"yoy_status": "applicable"}
        original_value, original_audit = deepcopy(row.comparison), deepcopy(row.content_review)
    formal = repo.get_metric_observations("entity")[0]
    assert formal["comparison"] == {} and formal["metric_value"] == 240
    assert "materialized_digest" not in formal["content_review"]
    assert repo.clone_latest_analysis(link, "clone", "entity", reviewed_only=True)["cache_reuse_rejected"] == "comparison_review_requires_refresh"
    with db.session() as session:
        row = session.scalars(select(m.MetricObservationRow)).one()
        assert row.comparison == original_value and row.content_review == original_audit
    db.engine.dispose()
