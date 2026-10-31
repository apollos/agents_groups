"""Model-labelled bindings; real model understanding is verified separately."""
from copy import deepcopy

import pytest

from mic.content_review import finalize_task_coverage
from mic.content_review_policy import PROTOCOL
from mic.event_resolution import COMPARISON_CONTRACT, finalize
from mic.merge import ModelContribution, MultiModelMerger
from mic.schemas import Passage, SearchHit
from mic.store.database import Database
from mic.store.repository import Repository
from mic.validate import BundleValidator
from .test_unit_review_consistency import unit_case


def checked(raw, passages):
    report = BundleValidator({}).validate(raw, passages)
    assert report.schema_valid, report.errors
    return report.bundle


@pytest.mark.parametrize("kind,expected", [("observation", "partial"),
                                            ("comparability", "not_assessed")])
def test_independent_percent_requires_a_publishable_record_binding(kind, expected):
    raw, passages = unit_case()
    review = raw["content_review"]
    review["claims"][1]["kind"] = kind
    review["task_coverage"] = [{"question_id": "q1", "status": "partial",
        "answer_claim_ids": ["growth"], "uncovered_aspects": ["收入金额"],
        "reason": "金额单位待审，独立同比有直接依据。"}]
    bundle = checked(raw, passages)
    result, = finalize_task_coverage(bundle, [{"question_id": "q1", "question": "收入金额及同比"}])
    assert result["status"] == expected
    assert bundle.facts == [] and not bundle.brief.one_sentence
    assert [m.metric_value for m in bundle.metrics] == ([20] if kind == "observation" else [])


def test_two_period_amounts_survive_merge_storage_and_coverage(config, tmp_path):
    text = "单位：万元；币种：人民币。期间：2026H1、2025H1。收入120、100；利润-2、-3；经营现金流-40、-20。"
    evidence = [{"passage_id": "table", "quote": text}]
    passages = [Passage(passage_id="table", text=text, section="table")]
    raw = {"decision": "save_structured", "overall_score": 85, "metrics": [],
           "content_review": {"protocol": PROTOCOL, "claims": [], "bindings": {}}}
    expected = []
    for name, values in [("收入", (120, 100)), ("利润", (-2, -3)), ("经营现金流", (-40, -20))]:
        for period, value in zip(("2026H1", "2025H1"), values):
            cid = f"amount_{len(expected)}"
            raw["metrics"].append({"metric_name": name, "metric_value": value,
                "unit": "万元", "period": period, "evidence_locator": {"passage_id": "table"}})
            raw["content_review"]["claims"].append({"id": cid, "kind": "observation",
                "status": "source_supported", "statement": f"{period}{name}{value}万元。",
                "reason": "人工标签：行、期间列及币种尺度有独立依据。", "depends_on": [],
                "evidence": deepcopy(evidence),
                "amount": {"currency": "CNY", "value": value, "unit": "万元", "evidence": deepcopy(evidence)}})
            raw["content_review"]["bindings"][f"/metrics/{len(expected)}"] = [cid]
            expected.append((name, period, value, "万元"))
    ids = [c["id"] for c in raw["content_review"]["claims"]]
    raw["content_review"]["task_coverage"] = [{"question_id": "q1", "status": "partial",
        "answer_claim_ids": ids, "uncovered_aspects": ["最新"], "reason": "两期数据有据，最新性未证明。"}]
    bundle = checked(raw, passages)
    bundle = MultiModelMerger(config).merge("link", "entity", [ModelContribution("model", "fixture", bundle)]).bundle
    result, = finalize_task_coverage(bundle, [{"question_id": "q1", "question": "最新财报两期收入、利润和经营现金流"}])
    assert result["status"] == "partial" and result["answer_claim_ids"] == ids
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("entity", {}, {}, "offline", "offline")
    hit = SearchHit(query="offline", title="Report", url="https://example.invalid/periods", domain="example.invalid")
    link = repo.save_source_link(run, "offline", hit, hit.url, "official")
    repo.save_merged_analysis("entity", link, bundle, {}, search_run_id=run)
    metrics = repo.get_metric_observations("entity")
    assert sorted((m["metric_name"], m["period"], m["metric_value"], m["unit"]) for m in metrics) == sorted(expected)
    assert repo.explain_source_analysis(link)["content_review"] == bundle.content_review
    db.engine.dispose()


@pytest.mark.parametrize("occurrence,expected", [(True, "covered"), ("true", "not_assessed")])
def test_event_coverage_runs_after_shared_event_resolution(occurrence, expected):
    text = "公司正式发布甲型设备，并公布技术规格。"
    evidence = [{"passage_id": "p1", "quote": text}]
    comparison = {"candidate_ref": "previous", "relation": "same_event",
        "scope_relation": "equivalent", "same_occurrence": occurrence, "stage_relation": "same",
        "reason": "人工标签：同一次发布。", "current_evidence": evidence, "candidate_evidence": evidence}
    raw = {"decision": "save_structured", "overall_score": 85,
        "events": [{"summary": text, "event_type": "product_launch", "evidence_locator": {"passage_id": "p1"},
            "event_resolution": {"current_claim_ids": ["launch"], "reviewed": True,
                "verdict": "same_event", "reason": "同一发布事项", "current_evidence": evidence,
                "comparisons": [comparison]}}],
        "content_review": {"protocol": PROTOCOL,
            "claims": [{"id": "launch", "kind": "observation", "status": "source_supported",
                "statement": text, "evidence": evidence, "depends_on": [], "reason": "正文直接说明发布。",
                "field_values": {"event_type": "product_launch"}}],
            "bindings": {"/events/0": ["launch"]},
            "task_coverage": [{"question_id": "q1", "status": "covered", "answer_claim_ids": ["launch"],
                "uncovered_aspects": [], "reason": "已有发布事实。"}]}}
    passages = [Passage(passage_id="p1", section="body", text=text)]
    bundle = checked(raw, passages)
    questions = [{"question_id": "q1", "question": "设备是否发布"}]
    assert finalize_task_coverage(bundle, questions)[0]["status"] == "not_assessed"
    finalize(bundle, {"comparison_contract": COMPARISON_CONTRACT, "complete": True,
        "candidates": [{"ref": "previous", "fingerprint": "previous-fingerprint",
                        "passages": [{"passage_id": "p1", "text": text}]}]},
        passages, run_id="run", link_id="link")
    assert finalize_task_coverage(bundle, questions)[0]["status"] == expected
