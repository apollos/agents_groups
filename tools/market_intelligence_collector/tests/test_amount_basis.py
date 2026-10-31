"""Structured value/unit/currency judgments constrain amount publication.

These fixtures are model-labelled doubles. They do not show that code can tell
whether a quotation actually states a currency or a scale.
"""
from copy import deepcopy

import pytest
from sqlalchemy import select

from mic.content_review import cache_review_current, finalize_task_coverage
from mic.content_review_policy import PROTOCOL, REVIEW_REVISION
from mic.merge import ModelContribution, MultiModelMerger
from mic.schemas import Passage, SearchHit
from mic.store import models as m
from mic.store.database import Database
from mic.store.repository import Repository
from mic.validate import BundleValidator


TABLE = "项目 | 本期 | 上期 | 增减(%)\n收入 | 120 | 100 | 20"
ROW = "收入 | 120 | 100 | 20"
HEADER = "The reporting convention for this statement is CNY, 万元."
QUESTION = "给出收入金额及单位"


def _evidence(quote=ROW, passage_id="table"):
    return [{"passage_id": passage_id, "quote": quote}]


def _basis(currency_status="pending_review", unit_status="source_supported",
           value_status="source_supported", currency_evidence=None, unit_evidence=None,
           value_evidence=None):
    return {
        "value": {"status": value_status, "evidence": [] if value_evidence is None else value_evidence},
        "unit": {"status": unit_status, "evidence": [] if unit_evidence is None else unit_evidence},
        "currency": {"status": currency_status, "evidence": [] if currency_evidence is None else currency_evidence},
    }


def amount_case(basis, *, parent_status="source_supported", reason="人工标签：数值可见。",
                claim_evidence=None, passages=None, header=False):
    passages = list(passages or [Passage(passage_id="table", text=TABLE, section="table")])
    if header:
        passages.append(Passage(passage_id="context", text=HEADER, section="body"))
    if claim_evidence is None:
        claim_evidence = _evidence() + (_evidence(HEADER, "context") if header else [])
    amount = {"id": "revenue", "kind": "observation", "status": parent_status,
              "statement": "本期收入120万元。", "reason": reason,
              "evidence": claim_evidence, "depends_on": [],
              "amount": {"currency": "CNY", "value": 120, "unit": "万元",
                         "evidence": claim_evidence, "basis": basis}}
    growth = {"id": "growth", "kind": "observation", "status": "source_supported",
              "statement": "原表列示收入同比增长20%。", "reason": "人工标签：同比百分比为直接披露。",
              "evidence": [{"passage_id": "table", "quote": TABLE}], "depends_on": []}
    summary = {"id": "summary", "kind": "observation", "status": "source_supported",
               "statement": "本期收入120万元，同比增长20%。", "reason": "人工标签：整句依赖两项前提。",
               "evidence": claim_evidence, "depends_on": ["revenue", "growth"]}
    event = {"id": "event_amount", "kind": "observation", "status": "source_supported",
             "statement": "公司披露本期收入120万元。", "reason": "人工标签：事件摘要包含该金额。",
             "evidence": claim_evidence, "depends_on": ["revenue"]}
    raw = {"schema_version": "bundle_extraction_v0.3", "decision": "save_structured", "overall_score": 85, "confidence": .9,
           "facts": [{"fact_statement": amount["statement"],
                      "metrics": {"amount": 120, "currency": "CNY", "amount_unit": "万元"},
                      "evidence_locator": {"passage_id": "table"}}],
           "metrics": [{"metric_name": "收入", "metric_value": 120, "unit": "万元",
                        "evidence_locator": {"passage_id": "table"}},
                       {"metric_name": "收入同比", "metric_value": 20, "unit": "%",
                        "evidence_locator": {"passage_id": "table"}}],
           "events": [{"summary": event["statement"], "evidence_locator": {"passage_id": "table"}}],
           "brief": {"one_sentence": summary["statement"]},
           "coverage_gaps": [{"gap_type": "missing_metric",
                              "description": "当前选段未确认收入金额的币种。"}],
           "content_review": {"protocol": PROTOCOL, "claims": [amount, growth, summary, event],
               "bindings": {"/facts/0": ["revenue"], "/metrics/0": ["revenue"],
                            "/metrics/1": ["growth"], "/brief/one_sentence": ["summary"],
                            "/events/0": ["event_amount"]},
               "task_coverage": [{"question_id": "q1", "status": "covered",
                   "answer_claim_ids": ["revenue"], "uncovered_aspects": [],
                   "reason": "人工标签：覆盖引用了待审金额。"}]}}
    return raw, passages


def validate(raw, passages):
    result = BundleValidator({}).validate(raw, passages)
    assert result.schema_valid, result.errors
    return result.bundle


def test_open_currency_publishes_scale_without_cny():
    basis = _basis(currency_status="pending_review",
                   unit_evidence=_evidence(HEADER, "context"),
                   value_evidence=_evidence())
    raw, passages = amount_case(basis, header=True,
                                reason="人工标签：尺度有原文，币种没有。")
    original = deepcopy(raw)
    bundle = validate(raw, passages)
    assert raw == original
    assert bundle.facts[0].fact_statement == "本期收入120万元。"
    assert bundle.facts[0].metrics["amount"] == 120
    assert bundle.facts[0].metrics["currency"] is None
    assert bundle.facts[0].metrics["amount_unit"] == "万元"
    assert bundle.facts[0].metrics["amount_currency_reason"] == "currency_basis_not_supported"
    assert [(item.metric_name, item.metric_value, item.unit) for item in bundle.metrics] == [
        ("收入", 120, "万元"), ("收入同比", 20, "%")]
    assert bundle.events[0].summary == "公司披露本期收入120万元。"
    assert bundle.content_review["claims"]["revenue"]["status"] == "source_supported"
    assert bundle.content_review["review_revision"] == REVIEW_REVISION == "amount_scale_currency_20261031"
    assert bundle.coverage_gaps[0].description == "当前选段未确认收入金额的币种。"


def test_missing_unit_basis_holds_amount_dependents_and_keeps_independent_percent():
    basis = _basis(unit_status="pending_review", currency_status="pending_review",
                   value_evidence=_evidence())
    raw, passages = amount_case(basis, header=True, reason="人工标签：尺度依据不足。")
    bundle = validate(raw, passages)
    assert bundle.facts == [] and bundle.events == [] and bundle.brief.one_sentence == ""
    assert [(item.metric_name, item.metric_value, item.unit) for item in bundle.metrics] == [("收入同比", 20, "%")]
    assert bundle.content_review["claims"]["revenue"]["reason"] == "amount_basis_not_supported"
    assert bundle.content_review["claims"]["summary"]["reason"] == "dependency_not_supported"
    assert bundle.content_review["claims"]["event_amount"]["reason"] == "dependency_not_supported"
    coverage, = finalize_task_coverage(bundle, [{"question_id": "q1", "question": QUESTION}])
    assert coverage["status"] == "not_assessed" and coverage["answer_claim_ids"] == []


def test_complete_basis_publishes_amount_summary_and_event():
    basis = _basis(currency_status="source_supported",
                   currency_evidence=_evidence(HEADER, "context"),
                   unit_evidence=_evidence(HEADER, "context"),
                   value_evidence=_evidence())
    raw, passages = amount_case(basis, header=True, reason="人工标签：三项依据均有当前原文。")
    bundle = validate(raw, passages)
    assert bundle.facts[0].metrics["amount"] == 1_200_000
    assert [(item.metric_name, item.unit) for item in bundle.metrics] == [("收入", "万元"), ("收入同比", "%")]
    assert bundle.events[0].summary == "公司披露本期收入120万元。"
    assert "120万元" in bundle.brief.one_sentence
    assert bundle.content_review["claims"]["revenue"]["status"] == "source_supported"


@pytest.mark.parametrize("component", ["value", "unit"])
def test_inference_on_value_or_unit_holds_the_complete_amount(component):
    basis = _basis(currency_status="source_supported",
                   currency_evidence=_evidence(HEADER, "context"),
                   unit_evidence=_evidence(HEADER, "context"),
                   value_evidence=_evidence())
    basis[component]["status"] = "inference"
    raw, passages = amount_case(basis, header=True)
    bundle = validate(raw, passages)
    assert bundle.facts == [] and bundle.events == []
    assert [item.metric_name for item in bundle.metrics] == ["收入同比"]
    assert bundle.content_review["claims"]["revenue"]["reason"] == "amount_basis_not_supported"


def test_inference_on_currency_keeps_the_scale_and_clears_cny():
    basis = _basis(currency_status="source_supported",
                   currency_evidence=_evidence(HEADER, "context"),
                   unit_evidence=_evidence(HEADER, "context"),
                   value_evidence=_evidence())
    basis["currency"]["status"] = "inference"
    raw, passages = amount_case(basis, header=True)
    bundle = validate(raw, passages)
    assert bundle.facts[0].metrics["amount"] == 120
    assert bundle.facts[0].metrics["currency"] is None
    assert bundle.facts[0].metrics["amount_unit"] == "万元"
    assert bundle.content_review["claims"]["revenue"]["status"] == "source_supported"


def test_basis_quote_missing_from_passage_is_pending_not_a_text_search():
    basis = _basis(currency_status="source_supported",
                   currency_evidence=[{"passage_id": "context", "quote": "币种：人民币"}],
                   unit_evidence=_evidence(HEADER, "context"),
                   value_evidence=_evidence())
    raw, passages = amount_case(basis, header=True)
    bundle = validate(raw, passages)
    assert bundle.facts[0].metrics["amount"] == 120
    assert bundle.facts[0].metrics["currency"] is None
    assert bundle.content_review["claims"]["revenue"]["status"] == "source_supported"


def test_unit_quote_missing_from_passage_holds_the_amount():
    basis = _basis(currency_status="pending_review",
                   unit_evidence=[{"passage_id": "context", "quote": "单位：元"}],
                   value_evidence=_evidence())
    raw, passages = amount_case(basis, header=True)
    bundle = validate(raw, passages)
    assert bundle.facts == []
    assert bundle.content_review["claims"]["revenue"]["reason"] == "amount_basis_citation_unverified"


def test_basis_quote_must_be_covered_by_the_claim_evidence():
    basis = _basis(currency_status="source_supported",
                   currency_evidence=_evidence(HEADER, "context"),
                   unit_evidence=_evidence(),
                   value_evidence=_evidence())
    raw, passages = amount_case(basis, header=True, claim_evidence=_evidence())
    bundle = validate(raw, passages)
    assert bundle.facts[0].metrics["currency"] is None
    assert bundle.facts[0].metrics["amount"] == 120
    assert bundle.content_review["claims"]["revenue"]["status"] == "source_supported"


def test_malformed_basis_holds_the_amount():
    raw, passages = amount_case({"value": {"status": "source_supported", "evidence": _evidence()}})
    bundle = validate(raw, passages)
    assert bundle.facts == [] and bundle.events == []
    assert bundle.content_review["claims"]["revenue"]["reason"] == "amount_basis_invalid"


def test_historical_amount_without_basis_keeps_the_model_status_even_if_reason_admits_a_gap():
    raw, passages = amount_case(None, header=True, reason="按A股惯例填CNY，并按数量级推算尺度，依据仍不足。")
    raw["content_review"]["claims"][0]["amount"].pop("basis")
    bundle = validate(raw, passages)
    assert bundle.facts[0].metrics["amount"] == 1_200_000
    assert bundle.content_review["claims"]["revenue"]["status"] == "source_supported"
    assert "惯例" in bundle.content_review["model_review"]["claims"][0]["reason"]


def test_executor_does_not_scan_a_supported_statement_for_amount_words():
    basis = _basis(unit_status="pending_review", value_evidence=_evidence())
    raw, passages = amount_case(basis, header=True)
    raw["facts"].append({"fact_statement": "本期收入120万元，币种按惯例填写。",
                         "evidence_locator": {"passage_id": "table"}})
    raw["content_review"]["claims"].append({
        "id": "loose", "kind": "observation", "status": "source_supported",
        "statement": "本期收入120万元，币种按惯例填写。",
        "reason": "人工标签：未声明依赖，执行器不扫描陈述。",
        "evidence": [{"passage_id": "table", "quote": TABLE}], "depends_on": []})
    raw["content_review"]["bindings"]["/facts/1"] = ["loose"]
    bundle = validate(raw, passages)
    assert [item.fact_statement for item in bundle.facts] == ["本期收入120万元，币种按惯例填写。"]
    assert bundle.content_review["claims"]["revenue"]["reason"] == "amount_basis_not_supported"


def test_merge_store_and_export_do_not_restore_an_amount_with_incomplete_basis(config, tmp_path):
    basis = _basis(unit_evidence=_evidence(HEADER, "context"), value_evidence=_evidence())
    raw, passages = amount_case(basis, header=True)
    bundle = validate(raw, passages)
    merged = MultiModelMerger(config).merge("link", "entity", [ModelContribution("model", "fixture", bundle)]).bundle
    assert cache_review_current(merged.content_review)
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("entity", {}, {}, "offline", "offline")
    hit = SearchHit(query="offline", title="Report", url="https://example.invalid/amount-basis", domain="example.invalid")
    link = repo.save_source_link(run, "offline", hit, hit.url, "official")
    repo.save_merged_analysis("entity", link, merged, {}, search_run_id=run)
    facts = repo.search_facts("entity")
    assert [item["fact_statement"] for item in facts] == ["本期收入120万元。"]
    assert facts[0]["metrics"]["currency"] is None and facts[0]["metrics"]["amount"] == 120
    names = [(item["metric_name"], item["unit"]) for item in repo.get_metric_observations("entity")]
    assert ("收入", "万元") in names and ("收入同比", "%") in names
    with db.session() as session:
        assert any("120万元" in row.one_sentence for row in session.scalars(select(m.AnalysisBrief)))
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("amount_basis_export", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    exported = exporter.content_review(tmp_path, {"search_run_id": run})
    assert exported["formal_records"]
    assert any(row["record"].get("metric_name") == "收入" and row["record"].get("unit") == "万元"
               for row in exported["formal_records"])
    assert any("120万元" in (row["record"].get("fact_statement") or "") for row in exported["formal_records"])
    assert all((row["record"].get("metrics") or {}).get("currency") != "CNY" for row in exported["formal_records"])
