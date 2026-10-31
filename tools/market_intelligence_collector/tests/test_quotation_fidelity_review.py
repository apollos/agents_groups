"""Verbatim evidence contract; labelled fixtures do not prove model understanding."""
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


ROW = "投 资 活 动 现\n金流量净额 -120 -100"
HEADER = "项目 本期 上期"
UNIT = "单位：元 币种：人民币"
QUESTION = [{"question_id": "q1", "question": "本期投资现金流净额"}]


def quotation_case(split=True):
    quotes = ROW.splitlines() if split else [ROW]
    evidence = [{"passage_id": "table", "quote": HEADER}]
    evidence += [{"passage_id": "table", "quote": q} for q in quotes]
    evidence += [{"passage_id": "units", "quote": UNIT}]
    claim = {"id": "cash", "kind": "observation", "status": "source_supported",
             "statement": "本期投资现金流净额为-120元。",
             "reason": "人工标签：断行共同给出指标，列头和另一段单位共同支持本期金额。",
             "evidence": evidence, "depends_on": [],
             "amount": {"value": -120, "unit": "元", "currency": "CNY", "evidence": deepcopy(evidence)}}
    summary = {"id": "summary", "kind": "observation", "status": "source_supported",
               "statement": claim["statement"], "reason": "人工标签：复述现金流金额。",
               "evidence": deepcopy(evidence), "depends_on": ["cash"]}
    raw = {"decision": "save_structured", "overall_score": 85,
           "facts": [{"fact_statement": claim["statement"],
                      "evidence_locator": {"passage_id": "table"}}],
           "metrics": [{"metric_name": "投资现金流净额", "metric_value": -120, "unit": "元",
                        "period": "current", "evidence_locator": {"passage_id": "table"}}],
           "brief": {"one_sentence": summary["statement"]},
           "content_review": {"protocol": PROTOCOL, "claims": [claim, summary],
                              "bindings": {"/facts/0": ["cash"], "/metrics/0": ["cash"],
                                           "/brief/one_sentence": ["summary"]},
                              "task_coverage": [{"question_id": "q1", "status": "covered",
                                  "answer_claim_ids": ["cash"], "uncovered_aspects": [],
                                  "reason": "本期金额有正式指标。"}]}}
    passages = [Passage(passage_id="table", section="table", text=HEADER + "\n" + ROW),
                Passage(passage_id="units", section="body", text=UNIT)]
    return raw, passages


def checked(raw, passages):
    result = BundleValidator({}).validate(raw, passages)
    assert result.schema_valid, result.errors
    return result.bundle


@pytest.mark.parametrize("split", [False, True])
def test_exact_multiline_or_split_quotes_preserve_amount_and_shared_claim(split):
    raw, passages = quotation_case(split)
    original = deepcopy(raw)
    bundle = checked(raw, passages)
    assert raw == original
    assert bundle.metrics[0].metric_value == -120 and bundle.metrics[0].unit == "元"
    assert bundle.facts[0].fact_statement == bundle.brief.one_sentence == raw["brief"]["one_sentence"]
    assert bundle.content_review["model_review"]["claims"][0]["evidence"] == raw["content_review"]["claims"][0]["evidence"]
    assert finalize_task_coverage(bundle, QUESTION)[0]["status"] == "covered"


@pytest.mark.parametrize("replacement,passage_id", [
    ("金 流 量 净 额 -120 -100", "table"),
    (ROW.replace("\n", " "), "table"),
    ("金流量净额 −120 -100", "table"),
    ("金流量净额 -120 -100", "units"),
])
def test_reformatted_or_mislocated_quote_is_not_silently_repaired(replacement, passage_id):
    raw, passages = quotation_case()
    raw["content_review"]["claims"][0]["evidence"][2] = {"passage_id": passage_id, "quote": replacement}
    original = deepcopy(raw)
    bundle = checked(raw, passages)
    assert raw == original
    assert bundle.metrics == [] and bundle.facts == [] and not bundle.brief.one_sentence
    assert bundle.content_review["claims"]["cash"]["reason"] == "citation_missing_or_unverified"
    assert bundle.content_review["claims"]["summary"]["reason"] == "dependency_not_supported"
    assert finalize_task_coverage(bundle, QUESTION)[0]["reason"] == "coverage_answer_not_published"
    assert bundle.content_review["model_review"]["claims"][0] == original["content_review"]["claims"][0]


@pytest.mark.parametrize("valid", [True, False])
def test_quote_verdict_survives_merge_storage_agent_and_export(config, tmp_path, valid):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore, loads_json
    from agent_trade_intel.persistence import ResultPersister
    from agent_trade_intel.reader import IntelligenceReader

    raw, passages = quotation_case()
    if not valid:
        raw["content_review"]["claims"][0]["evidence"][2]["quote"] = "金 流 量 净 额 -120 -100"
    bundle = checked(raw, passages)
    merged = MultiModelMerger(config).merge("link", "entity", [ModelContribution("model", "fixture", bundle)]).bundle
    finalize_task_coverage(merged, QUESTION)
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("entity", {}, {}, "offline", "offline")
    hit = SearchHit(query="offline", title="Report", url="https://example.invalid/quotation", domain="example.invalid")
    link = repo.save_source_link(run, "offline", hit, hit.url, "official")
    repo.save_merged_analysis("entity", link, merged, {}, search_run_id=run)
    report = {"search_run_id": run, "facts": repo.search_facts("entity"),
              "metrics": repo.get_metric_observations("entity"),
              "content_review": repo.explain_source_analysis(link)["content_review"]}
    assert [x["metric_value"] for x in report["metrics"]] == ([-120] if valid else [])
    assert len(report["facts"]) == int(valid)
    assert report["content_review"]["task_coverage"][0]["status"] == ("covered" if valid else "not_assessed")
    store, bus = SQLiteStore(tmp_path / "agent.db"), SQLiteStore(tmp_path / "bus.db")
    store.init_schema()
    bus.init_schema()
    task = {"task_id": "quote", "demand_id": "quote", "task_type": "mic_deep_collect",
            "idempotency_key": "quote", "target": {"target_id": "entity"}}
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence",
                        request={}, status="success", result=deepcopy(report)).finish()
    persister = ResultPersister(store)
    persister.save_run(task=task, ticket_id="quote", result=result)
    persister.save_mic_structures(task=task, result=result)
    row, = IntelligenceReader(store, bus).read_collection_status(demand_id="quote")["runs"]
    assert loads_json(row["result_json"], {}) == report
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("quotation_review_export", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    exported = exporter.content_review(tmp_path, {"search_run_id": run})
    metrics = [r["record"] for r in exported["formal_records"] if r["record_type"] == "metrics"]
    assert [r["metric_value"] for r in metrics] == ([-120] if valid else [])
    assert [r["record"]["fact_statement"] for r in exported["formal_records"] if r["record_type"] == "facts"] == [r["fact_statement"] for r in report["facts"]]
    db.engine.dispose()
