"""One model-authored number; shared arithmetic, state and delivery checks."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

from mic.content_review import finalize_task_coverage
from mic.merge import ModelContribution, MultiModelMerger
from mic.schemas import Passage, SearchHit
from mic.store.database import Database
from mic.store.repository import Repository
from .test_comparison_input_review import fixture, validate, QUESTION, TEXT


def input_case():
    raw = fixture()
    raw["metrics"][0]["comparison"] = {}
    raw["content_review"]["claims"][1]["field_values"].pop("comparison")
    return raw


@pytest.mark.parametrize("key,value,unit,expected,text", [
    ("yoy", 147.42, "percent", 1.4742, "同比增长147.42%"),
    ("yoy", 0, "percent", 0, "同比增长0%"),
    ("mom", -5, "percent", -.05, "环比下降5%"),
    ("qoq", .15, "ratio", .15, "季度环比增长比例0.15"),
    ("change_ratio", 25, "basis_point", .0025, "相对增幅25个基点"),
    ("yoy_change_pp", -35.03, "percentage_point", -35.03, "同比下降35.03个百分点"),
    ("change_pp", -25, "basis_point", -.25, "差值下降25个基点"),
])
def test_normalization_uses_typed_input_and_preserves_original_review(key, value, unit, expected, text):
    raw = input_case()
    evidence = [{"passage_id": "p0", "quote": text}]
    for claim in raw["content_review"]["claims"]:
        claim["evidence"] = evidence
    raw["content_review"]["claims"][1]["field_values"]["comparison_input"] = {
        key: {"value": value, "unit": unit, "evidence": evidence}}
    original = deepcopy(raw)
    bundle = validate(raw, [Passage(passage_id="p0", section="body", text=text)])
    assert raw == original and bundle.content_review["model_review"] == original["content_review"]
    assert bundle.metrics[0].comparison == {key: expected}
    assert bundle.brief.one_sentence
    conversion = bundle.metrics[0].content_review["fields"]["comparison"]["conversion"][key]
    assert conversion["output_value"] == expected
    assert conversion["inputs"] == [{"claim_id": "rate", "value": value, "unit": unit}]
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"


@pytest.mark.parametrize("state", ["inference", "pending_review", "unsupported"])
def test_arithmetic_never_promotes_a_model_held_comparison(state):
    raw = input_case()
    raw["content_review"]["claims"][1]["status"] = state
    bundle = validate(raw)
    assert bundle.metrics[0].comparison == {} and bundle.metrics[0].metric_value == 240
    assert not bundle.brief.one_sentence
    assert bundle.content_review["claims"]["rate"]["status"] == state
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "not_assessed"


@pytest.mark.parametrize("damage", ["unit", "value", "boolean", "nan", "overflow", "underflow",
                                    "empty_quote", "wrong_passage", "parent_quote", "member", "kind", "dimension"])
def test_invalid_single_input_cannot_materialize_or_support_dependent_summary(damage):
    raw = input_case()
    claim = raw["content_review"]["claims"][1]
    item = claim["field_values"]["comparison_input"]["yoy"]
    if damage == "unit": item.pop("unit")
    elif damage == "value": item.pop("value")
    elif damage == "boolean": item["value"] = True
    elif damage == "nan": item["value"] = "NaN"
    elif damage == "overflow": item["value"] = "1e99999999999999999"
    elif damage == "underflow": item["value"] = "1e-400"
    elif damage == "empty_quote": item["evidence"] = []
    elif damage == "wrong_passage": item["evidence"][0]["passage_id"] = "other"
    elif damage == "parent_quote": claim["evidence"] = [{"passage_id": "p0", "quote": "编造"}]
    elif damage == "member": claim["field_values"]["comparison_input"]["unknown"] = item
    elif damage == "kind": claim["kind"] = "observation"
    else: item["unit"] = "percentage_point"
    bundle = validate(raw)
    assert bundle.metrics[0].comparison == {} and bundle.metrics[0].metric_value == 240
    assert not bundle.brief.one_sentence
    assert bundle.content_review["claims"]["rate"]["status"] == "pending_review"


@pytest.mark.parametrize("location", ["metric", "claim", "child", "null", "na", "metric_na", "child_na"])
def test_redundant_conflicts_remain_held_instead_of_overwritten(location):
    raw = input_case()
    if location == "claim":
        raw["content_review"]["claims"][1]["field_values"]["comparison"] = {"yoy": .14742}
    elif location == "na":
        raw["content_review"]["claims"][1]["field_values"]["comparison"] = {"yoy_status": "not_applicable"}
    elif location in {"metric_na", "child_na"}:
        raw["metrics"][0]["comparison"] = {"yoy_status": "not_applicable"}
        if location == "child_na":
            bindings = raw["content_review"]["bindings"]
            bindings["/metrics/0/comparison/yoy"] = bindings.pop("/metrics/0/comparison")
    else:
        raw["metrics"][0]["comparison"] = {"yoy": None if location == "null" else .14742}
        if location == "child":
            bindings = raw["content_review"]["bindings"]
            bindings["/metrics/0/comparison/yoy"] = bindings.pop("/metrics/0/comparison")
    original = deepcopy(raw)
    bundle = validate(raw)
    assert bundle.content_review["model_review"] == original["content_review"] and raw == original
    assert not bundle.metrics[0].comparison.get("yoy") and not bundle.brief.one_sentence


@pytest.mark.parametrize("child", [True, False])
def test_same_claim_can_supply_two_records_with_parent_or_child_binding(child):
    raw = input_case()
    raw["metrics"].append(deepcopy(raw["metrics"][0]))
    bindings = raw["content_review"]["bindings"]
    bindings["/metrics/1"] = ["amount"]
    bindings["/metrics/1/comparison" + ("/yoy" if child else "")] = ["rate"]
    bundle = validate(raw)
    assert [m.comparison for m in bundle.metrics] == [{"yoy": 1.4742}] * 2
    assert len(bundle.content_review["claims"]) == 3
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "partial"


def test_unbound_inputs_do_not_populate_a_related_metric():
    raw = input_case()
    raw["content_review"]["bindings"].pop("/metrics/0/comparison")
    bundle = validate(raw)
    assert bundle.metrics[0].comparison == {}
    assert finalize_task_coverage(bundle, QUESTION)[0]["reason"] == "coverage_answer_not_published"


def test_multiple_claims_with_conflicting_inputs_are_not_resolved_by_order():
    raw = input_case()
    other = deepcopy(raw["content_review"]["claims"][1])
    other["id"] = "other"
    other["field_values"]["comparison_input"]["yoy"]["value"] = 10
    raw["content_review"]["claims"].append(other)
    raw["content_review"]["bindings"]["/metrics/0/comparison"].append("other")
    bundle = validate(raw)
    assert bundle.metrics[0].comparison == {} and not bundle.brief.one_sentence
    assert bundle.content_review["claims"]["rate"]["status"] == "pending_review"


def test_numeric_and_different_not_applicable_members_can_coexist():
    raw = input_case()
    value = {"qoq": None, "qoq_status": "not_applicable"}
    raw["metrics"][0]["comparison"] = deepcopy(value)
    raw["content_review"]["claims"][1]["field_values"]["comparison"] = value
    text = TEXT + "季度环比不适用。"
    for claim in raw["content_review"]["claims"]:
        claim["evidence"] = [{"passage_id": "p0", "quote": text}]
    bundle = validate(raw, [Passage(passage_id="p0", section="body", text=text)])
    assert bundle.metrics[0].comparison == {**value, "yoy": 1.4742}


def test_numeric_and_not_applicable_claims_for_same_member_conflict():
    raw = input_case()
    other = deepcopy(raw["content_review"]["claims"][1])
    other.update(id="na", field_values={"comparison": {"yoy_status": "not_applicable"}})
    raw["content_review"]["claims"].append(other)
    raw["content_review"]["bindings"]["/metrics/0/comparison"].append("na")
    bundle = validate(raw)
    assert bundle.metrics[0].comparison == {} and not bundle.brief.one_sentence


def test_normalized_comparison_survives_merge_cache_agent_and_export(config, tmp_path):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore, loads_json
    from agent_trade_intel.persistence import ResultPersister
    from agent_trade_intel.reader import IntelligenceReader

    raw = input_case()
    bundle = validate(raw)
    merged = MultiModelMerger(config).merge("link", "entity", [ModelContribution("model", "fixture", bundle)]).bundle
    finalize_task_coverage(merged, QUESTION)
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("entity", {}, {}, "offline", "offline")
    hit = SearchHit(query="offline", title="Report", url="https://example.invalid/normalization", domain="example.invalid")
    link = repo.save_source_link(run, "offline", hit, hit.url, "official")
    repo.save_merged_analysis("entity", link, merged, {}, search_run_id=run)
    report = {"search_run_id": run, "facts": repo.search_facts("entity"),
              "metrics": repo.get_metric_observations("entity"),
              "content_review": repo.explain_source_analysis(link)["content_review"]}
    assert report["metrics"][0]["comparison"] == {"yoy": 1.4742}
    assert report["content_review"]["task_coverage"][0]["status"] == "partial"
    assert report["metrics"][0]["content_review"]["claims"]["rate"]["field_values"] == raw["content_review"]["claims"][1]["field_values"]
    assert repo.find_analyzed_link_by_canonical(hit.url, "entity", reviewed_only=True).id == link
    assert repo.clone_latest_analysis(link, "clone", "entity", reviewed_only=True)["metrics"] == 1
    assert all(r["comparison"] == {"yoy": 1.4742} for r in repo.get_metric_observations("entity"))
    store, bus = SQLiteStore(tmp_path / "agent.db"), SQLiteStore(tmp_path / "bus.db")
    store.init_schema()
    bus.init_schema()
    task = {"task_id": "normalize", "demand_id": "normalize", "task_type": "mic_deep_collect",
            "idempotency_key": "normalize", "target": {"target_id": "entity"}}
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence",
                        request={}, status="success", result=deepcopy(report)).finish()
    persister = ResultPersister(store)
    persister.save_run(task=task, ticket_id="normalize", result=result)
    persister.save_mic_structures(task=task, result=result)
    row, = IntelligenceReader(store, bus).read_collection_status(demand_id="normalize")["runs"]
    assert loads_json(row["result_json"], {}) == report
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("normalization_export", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    exported = exporter.content_review(tmp_path, {"search_run_id": run})
    metric, = [r["record"] for r in exported["formal_records"] if r["record_type"] == "metrics"]
    assert metric["comparison"] == {"yoy": 1.4742}
    assert metric["content_review"]["fields"]["comparison"]["conversion"]["yoy"]["output_value"] == 1.4742
    db.engine.dispose()
