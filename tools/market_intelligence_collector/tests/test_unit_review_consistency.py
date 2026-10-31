"""Model-labelled unit judgments through the existing shared review and storage.

These are semantic doubles, not a claim that code understands missing headers.
The live B8-material replay is recorded separately under logs/acceptance/.
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
QUESTION = "给出收入金额及单位"


def unit_case(supported=False, currency="CNY", unit="万元"):
    passages = [Passage(passage_id="table", text=TABLE, section="table")]
    evidence = [{"passage_id": "table", "quote": "收入 | 120 | 100 | 20"}]
    if supported:
        # Unit/currency evidence can be in another paragraph, with arbitrary
        # wording. No production text classifier decides whether it applies.
        header = f"The reporting convention for this statement is {currency}, {unit}."
        passages.append(Passage(passage_id="context", text=header, section="body"))
        evidence.append({"passage_id": "context", "quote": header})
    amount = {"id": "revenue", "kind": "observation",
              "status": "source_supported" if supported else "pending_review",
              "statement": f"本期收入120{unit}。",
              "reason": "人工标签：跨段单位依据适用于本表。" if supported else "人工标签：数值可见，但尺度与币种依据不足。",
              "evidence": evidence, "depends_on": [],
              "amount": {"currency": currency, "value": 120, "unit": unit, "evidence": evidence}}
    growth = {"id": "growth", "kind": "observation", "status": "source_supported",
              "statement": "原表列示收入同比增长20%。", "reason": "人工标签：同比百分比为直接披露，不是用金额计算。",
              "evidence": [{"passage_id": "table", "quote": TABLE}], "depends_on": []}
    summary = {"id": "summary", "kind": "observation", "status": "source_supported",
               "statement": f"本期收入120{unit}，同比增长20%。", "reason": "人工标签：整句依赖两项前提。",
               "evidence": evidence, "depends_on": ["revenue", "growth"]}
    return {"decision": "save_structured", "overall_score": 85, "confidence": .9,
            "facts": [{"fact_statement": amount["statement"],
                       "metrics": {"amount": 120, "currency": currency, "amount_unit": unit},
                       "evidence_locator": {"passage_id": "table"}}],
            "metrics": [{"metric_name": "收入", "metric_value": 120, "unit": unit,
                         "evidence_locator": {"passage_id": "table"}},
                        {"metric_name": "收入同比", "metric_value": 20, "unit": "%",
                         "evidence_locator": {"passage_id": "table"}}],
            "brief": {"one_sentence": summary["statement"]},
            "coverage_gaps": [] if supported else [{"gap_type": "missing_metric",
                "description": "当前选段未确认收入金额的币种与尺度。"}],
            "content_review": {"protocol": PROTOCOL, "claims": [amount, growth, summary],
                "bindings": {"/facts/0": ["revenue"], "/metrics/0": ["revenue"],
                             "/metrics/1": ["growth"], "/brief/one_sentence": ["summary"]},
                "task_coverage": [{"question_id": "q1", "status": "covered",
                    "answer_claim_ids": ["revenue"], "uncovered_aspects": [],
                    "reason": "Intentionally stale coverage: executor must reject a held answer."}]}}, passages


def validate(raw, passages):
    result = BundleValidator({}).validate(raw, passages)
    assert result.schema_valid, result.errors
    return result.bundle


@pytest.mark.parametrize("state", ["pending_review", "inference", "unsupported"])
def test_unapproved_amount_is_held_everywhere_and_independent_percent_survives(state):
    raw, passages = unit_case()
    raw["content_review"]["claims"][0]["status"] = state
    original = deepcopy(raw)
    bundle = validate(raw, passages)
    assert raw == original
    assert bundle.facts == [] and bundle.brief.one_sentence == ""
    assert [(x.metric_name, x.metric_value, x.unit) for x in bundle.metrics] == [("收入同比", 20, "%")]
    assert bundle.content_review["claims"]["summary"]["status"] == "pending_review"
    assert bundle.content_review["claims"]["summary"]["reason"] == "dependency_not_supported"
    assert bundle.content_review["model_review"]["claims"][0] == original["content_review"]["claims"][0]
    coverage, = finalize_task_coverage(bundle, [{"question_id": "q1", "question": QUESTION}])
    assert coverage["status"] == "not_assessed" and coverage["answer_claim_ids"] == []


def test_explicit_pending_premise_holds_amount_even_if_child_claim_is_marked_supported():
    raw, passages = unit_case()
    raw["content_review"]["claims"][0].update(status="source_supported", depends_on=["unit_basis"])
    raw["content_review"]["claims"].append({
        "id": "unit_basis", "kind": "observation", "status": "pending_review",
        "statement": "本表金额的尺度和币种尚待确认。", "reason": "Model-labelled unresolved prerequisite.",
        "evidence": [{"passage_id": "table", "quote": TABLE}], "depends_on": []})
    bundle = validate(raw, passages)
    assert bundle.facts == [] and len(bundle.metrics) == 1 and not bundle.brief.one_sentence
    assert bundle.content_review["claims"]["revenue"]["reason"] == "dependency_not_supported"


@pytest.mark.parametrize("currency,unit,expected", [("CNY", "千元", 120000),
    ("CNY", "万元", 1200000), ("USD", "million dollars", 120), ("CNY", "万元等值", 120)])
def test_model_supported_cross_passage_units_survive_without_text_patterns(currency, unit, expected):
    raw, passages = unit_case(True, currency, unit)
    bundle = validate(raw, passages)
    assert bundle.facts[0].metrics["amount"] == expected
    assert bundle.metrics[0].unit == unit and bundle.metrics[0].metric_value == 120
    assert bundle.brief.one_sentence == raw["brief"]["one_sentence"]
    assert bundle.facts[0].content_review["fields"]["fact_statement"]["status"] == "source_supported"


def store_case(config, tmp_path, supported=False):
    raw, passages = unit_case(supported)
    bundle = validate(raw, passages)
    merged = MultiModelMerger(config).merge("link", "entity", [ModelContribution("model", "fixture", bundle)]).bundle
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("entity", {}, {}, "offline", "offline")
    hit = SearchHit(query="offline", title="Report", url="https://example.invalid/report", domain="example.invalid")
    link = repo.save_source_link(run, "offline", hit, hit.url, "official")
    repo.save_merged_analysis("entity", link, merged, {}, search_run_id=run)
    repo.update_link_read(link, "read", "unit-body", "unit-simhash")
    return db, repo, run, link, merged


def test_merge_store_query_cache_and_export_do_not_restore_pending_amount(config, tmp_path):
    db, repo, run, link, merged = store_case(config, tmp_path)
    assert repo.search_facts("entity") == []
    metrics = repo.get_metric_observations("entity")
    assert [(x["metric_name"], x["unit"]) for x in metrics] == [("收入同比", "%")]
    explained = repo.explain_source_analysis(link)
    assert explained["facts"] == [] and len(explained["metrics"]) == 1
    assert cache_review_current(merged.content_review)
    assert repo.find_analyzed_link_by_canonical("https://example.invalid/report", "entity", reviewed_only=True).id == link
    cloned = repo.clone_latest_analysis(link, "new-source", "entity", reviewed_only=True)
    assert cloned["facts"] == 0 and cloned["metrics"] == 1
    with db.session() as session:
        assert all(row.one_sentence == "" for row in session.scalars(select(m.AnalysisBrief)))
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("unit_review_export", script)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    exported = exporter.content_review(tmp_path, {"search_run_id": run})
    assert exported["formal_records"]
    assert all(row["record"].get("metric_name") != "收入" for row in exported["formal_records"])


def test_old_prompt_cache_is_rejected_without_rewriting_historical_rows(config, tmp_path):
    db, repo, _, link, _ = store_case(config, tmp_path, True)
    with db.session() as session:
        row = session.scalars(select(m.MergedAnalysis)).one()
        audit = deepcopy(row.content_review)
        def old(value):
            if isinstance(value, dict):
                value.pop("review_revision", None)
                for child in value.values(): old(child)
            elif isinstance(value, list):
                for child in value: old(child)
        old(audit)
        row.content_review = audit
    assert repo.find_analyzed_link_by_canonical("https://example.invalid/report", "entity", reviewed_only=True) is None
    assert repo.find_analyzed_link_by_content_hash("unit-body", "entity", reviewed_only=True) is None
    result = repo.clone_latest_analysis(link, "new-source", "entity", reviewed_only=True)
    assert result["cache_reuse_rejected"] == "content_review_requires_refresh"
    with db.session() as session:
        assert session.scalars(select(m.MergedAnalysis)).one().content_review == audit
    # Archived reads remain historical; rejection does not invent a new review.
    assert len(repo.get_metric_observations("entity")) == 2


def test_every_merged_contribution_needs_current_review_revision():
    good = {"review_revision": REVIEW_REVISION}
    assert cache_review_current({"contributions": [good, {"contributions": [good]}]})
    assert not cache_review_current({"review_revision": REVIEW_REVISION, "contributions": [good, {}]})


def test_agent_formal_run_readback_preserves_the_reviewed_records_and_limitations(config, tmp_path):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore, loads_json
    from agent_trade_intel.persistence import ResultPersister
    from agent_trade_intel.reader import IntelligenceReader

    _, repo, run, link, bundle = store_case(config, tmp_path)
    report = {"search_run_id": run, "content_review_protocol": PROTOCOL,
              "structured_outputs": {"events": 0, "facts": 0, "metrics": 1},
              "facts": repo.search_facts("entity"), "metrics": repo.get_metric_observations("entity"),
              "content_reviews": [bundle.content_review],
              "coverage_gaps": [{"gap_id": "unit-gap", "source_link_id": link,
                  "search_run_id": run, "gap_type": "missing_metric", "priority": "medium",
                  "description": "当前来源金额单位待审。", "suggested_next_queries": [], "status": "open"}]}
    store, bus = SQLiteStore(tmp_path / "agent.db"), SQLiteStore(tmp_path / "bus.db")
    store.init_schema()
    bus.init_schema()
    task = {"task_id": "unit-task", "demand_id": "unit-demand", "task_type": "mic_deep_collect",
            "idempotency_key": "unit-task", "target": {"target_id": "entity"}}
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence",
                        request={}, status="success", result=deepcopy(report)).finish()
    persister = ResultPersister(store)
    persister.save_run(task=task, ticket_id="unit-ticket", result=result)
    persister.save_mic_structures(task=task, result=result)
    row, = IntelligenceReader(store, bus).read_collection_status(demand_id="unit-demand")["runs"]
    delivered = loads_json(row["result_json"], {})
    assert delivered == report
    assert delivered["facts"] == []
    assert [metric["metric_name"] for metric in delivered["metrics"]] == ["收入同比"]
    with store.session() as con:
        assert con.execute("SELECT description FROM coverage_gaps").fetchone()[0] == "当前来源金额单位待审。"
