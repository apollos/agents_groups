"""Shared policy regression using saved article text and labelled model doubles.

The labels exercise policy enforcement, not the accuracy of a live model.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from mic.content_review_policy import PROTOCOL, RULE_TEXT
from mic.event_resolution import build_context, finalize
from mic.merge import ModelContribution, MultiModelMerger
from mic.modeling.prompts import SYSTEM_PROMPT, ARBITRATION_SYSTEM
from mic.money import normalize_cny_fields
from mic.schemas import BundleExtraction, Passage, SearchHit
from mic.store.database import Database
from mic.store import models as m
from mic.store.repository import Repository
from mic.validate import BundleValidator


SAMPLE = json.loads((Path(__file__).parent / "fixtures/event_context_run_88f8aa20b573.json").read_text())["samples"][0]
PASSAGES = [Passage(**p, section="正文" if p["passage_id"] != "title" else "标题") for p in SAMPLE["passages"]]
TEXT = {p.passage_id: p.text for p in PASSAGES}


def claim(cid, statement, pid="p2", status="source_supported", dependencies=(), kind="observation"):
    return {"id": cid, "statement": statement, "kind": kind, "status": status,
            "reason": "人工标签：结合所引原文判断该具体主张的支持程度。",
            "evidence": [{"passage_id": pid, "quote": TEXT[pid]}], "depends_on": list(dependencies)}


def case():
    project = "项目为河北任丘智弘100MW/400MWh独立储能试点项目。"
    owner = "业主为河北任丘智弘。"
    award = "宁德时代中标二标段10MW/40MWh钠离子储能系统，中标价4141.622万元。"
    compare = "两个报价形成直接对标，可作为成本比较基准。"
    money = claim("money", "中标价4141.622万元。")
    money["amount"] = {"currency": "CNY", "value": 4141.622, "unit": "万元",
                       "evidence": {"passage_id": "p2", "quote": "4141.622万元"}}
    claims = [claim("project", project, "p0"), claim("owner", owner, "p0", "unsupported", kind="identity"),
              claim("award", award), money,
              claim("quote", "来源报道磷酸铁锂标段合单价0.518元/Wh。", "p1"),
              claim("comparison", compare, "p1", "pending_review", kind="comparability"),
              claim("profit", "由价格对标可以证明盈利优势。", "p1", dependencies=["comparison"], kind="analysis")]
    parties = claim("parties", "宁德时代为获标方；业主未确认。", kind="identity")
    parties["field_values"] = {"entities": {"subject": "宁德时代"}}
    project_role = claim("project_role", "文中该名称是项目名称。", "p0", kind="identity")
    project_role["field_values"] = {"entities": {"subject": "项目"}}
    claims.extend([parties, project_role])
    raw = {"decision": "save_structured", "overall_score": 76, "confidence": .8,
           "brief": {"one_sentence": project + owner, "why_it_matters": compare},
           "facts": [{"fact_statement": project + owner, "entities": {"subject": "项目", "owner": "河北任丘智弘"},
                      "evidence_locator": {"passage_id": "p0"}}],
           "metrics": [{"metric_name": "磷酸铁锂标段来源报价", "metric_value": .518, "unit": "元/Wh",
                        "interpretation": compare, "evidence_locator": {"passage_id": "p1"}},
                       {"metric_name": "来源报价", "metric_value": .518, "unit": "元/Wh",
                        "interpretation": "可证明盈利优势", "evidence_locator": {"passage_id": "p1"}}],
           "events": [{"summary": award, "event_date": "2026-09-15", "entities": {"subject": "宁德时代", "counterparty": "河北任丘智弘"},
                       "metrics": {"amount": 4141.622, "currency": "CNY万元"},
                       "evidence_locator": {"passage_id": "p2"}}],
           "content_review": {"protocol": PROTOCOL, "claims": claims, "bindings": {
               "/brief/one_sentence": ["project", "owner"], "/brief/why_it_matters": ["comparison"],
               "/facts/0/fact_statement": ["project", "owner"], "/facts/0/entities": ["project_role"],
               "/facts/0/entities/owner": ["owner"], "/events/0/summary": ["award"],
               "/events/0/event_date": ["project"], "/events/0/entities": ["parties"],
               "/events/0/entities/counterparty": ["owner"], "/events/0/metrics": ["money"],
               **{f"/metrics/{i}/{field}": ["quote"] for i in (0, 1) for field in ("metric_name", "metric_value", "unit")},
               "/metrics/0/interpretation": ["comparison"], "/metrics/1/interpretation": ["profit"],
           }}}
    return raw


def validate(raw):
    report = BundleValidator({}, require_content_review=True).validate(raw, PASSAGES)
    assert report.schema_valid, report.errors
    return report.bundle


def test_saved_material_owner_price_and_currency_follow_one_policy():
    raw = case()
    original = deepcopy(raw)
    bundle = validate(raw)
    assert raw == original
    assert bundle.facts[0].fact_statement == bundle.brief.one_sentence == raw["content_review"]["claims"][0]["statement"]
    assert bundle.facts[0].entities["owner"] is None
    assert bundle.events[0].entities["counterparty"] is None
    assert bundle.events[0].event_date == "2026-09-15"  # supported by p0, although event cites p2
    assert bundle.brief.why_it_matters == bundle.metrics[0].interpretation == bundle.metrics[1].interpretation == ""
    assert bundle.metrics[0].metric_value == .518 and bundle.metrics[0].unit == "元/Wh"
    assert bundle.metrics[0].scope["usable_as_price_benchmark"] is False
    assert bundle.content_review["claims"]["profit"]["reason"] == "dependency_not_supported"
    values = bundle.events[0].metrics
    assert (values["amount"], values["currency"], values["amount_unit"]) == (41416220, "CNY", "元")
    assert values["amount_input"]["currency"] == "CNY万元"
    assert values["amount_evidence"] == {"passage_id": "p2", "quote": "4141.622万元"}
    assert any(h["original"] == raw["brief"]["why_it_matters"] for h in bundle.content_review["held"])


@pytest.mark.parametrize("wording", ["形成直接对标", "可以当作另一条路线的参照价", "两种技术具备相同的成本比较口径", "apples-to-apples pricing"])
def test_rephrasing_cannot_change_shared_claim_state(wording):
    raw = case()
    raw["brief"]["why_it_matters"] = raw["metrics"][0]["interpretation"] = wording
    result = validate(raw)
    assert result.brief.why_it_matters == result.metrics[0].interpretation == ""


@pytest.mark.parametrize("damage", ["no_review", "unknown_id", "duplicate_id", "fabricated_quote", "title_only", "cycle", "unknown_dependency"])
def test_missing_invalid_or_circular_claims_never_pass(damage):
    raw = case()
    review = raw["content_review"]
    award = next(c for c in review["claims"] if c["id"] == "award")
    if damage == "no_review": raw.pop("content_review")
    if damage == "unknown_id": review["bindings"]["/events/0/summary"] = ["unknown"]
    if damage == "duplicate_id": review["claims"].append(deepcopy(award))
    if damage == "fabricated_quote": award["evidence"][0]["quote"] = "原文没有这一句话"
    if damage == "title_only": award["evidence"] = [{"passage_id": "title", "quote": TEXT["title"]}]
    if damage == "cycle": award["depends_on"] = ["award"]
    if damage == "unknown_dependency": award["depends_on"] = ["unknown"]
    bundle = validate(raw)
    assert bundle.events == []
    assert bundle.content_review["held"]


def test_model_approval_flag_cannot_skip_field_review():
    raw = case()
    raw["content_review"]["status"] = "applied"
    raw["content_review"]["bindings"].pop("/events/0/summary")
    raw["events"][0]["content_review"] = {"protocol": PROTOCOL, "status": "approved"}
    assert validate(raw).events == []


def test_inference_preserved_in_review_but_not_formal_summary():
    raw = case()
    raw["content_review"]["claims"][5]["status"] = "inference"
    bundle = validate(raw)
    assert bundle.content_review["claims"]["comparison"]["status"] == "inference"
    assert bundle.brief.why_it_matters == ""


def test_pending_nested_typed_fields_use_schema_defaults_without_crashing():
    raw = case()
    raw["events"][0]["impact"] = {"direction": "positive", "channels": ["margin"]}
    raw["content_review"]["bindings"]["/events/0/impact/channels"] = ["profit"]
    result = validate(raw)
    assert result.events[0].impact.direction == "unclear"
    assert result.events[0].impact.channels == []


def test_malformed_claim_kind_is_pending_instead_of_crashing():
    raw = case()
    raw["content_review"]["claims"][2]["kind"] = {"wrong": "type"}
    assert validate(raw).events == []


def test_format_problem_is_not_misreported_as_unsupported_evidence():
    raw = case()
    raw["events"][0]["metrics"]["amount_unit"] = "亿元"  # conflicts with CNY万元
    result = validate(raw)
    values = result.events[0].metrics
    assert values["amount"] is None and values["amount_candidate"] == 4141.622
    assert values["amount_status"] == "format_pending"
    assert result.events[0].content_review["fields"]["metrics/amount"]["status"] == "format_pending"
    assert result.content_review["claims"]["money"]["status"] == "source_supported"


@pytest.mark.parametrize("currency,unit,expected", [("CNY万元", None, 41416220), ("CNY", "万元", 41416220),
                                                    ("RMB万元", None, 41416220)])
def test_currency_scale_syntax_keeps_original_evidence(currency, unit, expected):
    spec = case()["content_review"]["claims"][3]["amount"]
    values = {"amount": 4141.622, "currency": currency}
    if unit: values["amount_unit"] = unit
    result, reason = normalize_cny_fields(values, TEXT, [spec])
    assert reason == "supported" and result["amount"] == expected
    assert result["amount_input"] == values


def test_model_unit_repair_cannot_replace_amount_or_currency():
    spec = case()["content_review"]["claims"][3]["amount"]
    for values in ({"amount": 123, "currency": "CNY"}, {"amount": 4141.622, "currency": "USD"}):
        result, reason = normalize_cny_fields(values, TEXT, [spec])
        assert result is None and reason.startswith("normalization_")


def test_amount_follows_the_semantic_review_not_a_regex():
    """The reviewed claim.amount decides; passage wording, foreign currency in the
    same paragraph and 'number followed by unit' syntax are not re-checked."""
    raw = case()
    money = next(c for c in raw["content_review"]["claims"] if c["id"] == "money")
    # Value, unit and currency may be cited from different fragments of the input.
    money["amount"]["evidence"] = [{"passage_id": "p2", "quote": "4141.622"}, {"passage_id": "p2", "quote": "万元"}]
    values = validate(raw).events[0].metrics
    assert (values["amount"], values["amount_raw"], values["amount_raw_unit"]) == (41416220, 4141.622, "万元")
    assert values["amount_evidence"] == money["amount"]["evidence"]
    # A claim that is not source_supported keeps the amount in the audit record only.
    for status in ("inference", "pending_review", "unsupported"):
        damaged = case()
        next(c for c in damaged["content_review"]["claims"] if c["id"] == "money")["status"] = status
        bundle = validate(damaged)
        assert bundle.events[0].metrics == {} or bundle.events[0].metrics.get("amount") is None
        assert bundle.content_review["claims"]["money"]["status"] == status
        assert any(h["path"] == "/events/0/metrics" for h in bundle.content_review["held"])
    # No claim.amount at all: the model gave no source basis, so the amount waits for review.
    missing = case()
    next(c for c in missing["content_review"]["claims"] if c["id"] == "money").pop("amount")
    bundle = validate(missing)
    values = bundle.events[0].metrics
    assert values["amount"] is None and values["amount_candidate"] == 4141.622
    assert (values["amount_status"], values["amount_reason"]) == ("pending_review", "amount_claim_missing")
    assert bundle.content_review["claims"]["money"]["status"] == "source_supported"


def test_thousand_yuan_table_amount_is_converted_not_rejected():
    table = ("（三）主要会计数据和财务指标\n□是 ■否\n单位：千元\n项目 2025 年 2024 年 本年比上年增减\n"
             "营业收入 423,701,834 362,012,554 17.04% 400,917,045")
    passages = [*PASSAGES, Passage(passage_id="t1", section="表格 1", text=table)]
    evidence = [{"passage_id": "t1", "quote": "营业收入 423,701,834 362,012,554 17.04%"},
                {"passage_id": "t1", "quote": "单位：千元"}]
    revenue = {"id": "revenue_2025", "statement": "宁德时代2025年度营业收入为423,701,834千元。", "kind": "observation",
               "status": "source_supported", "reason": "行名、年份列与表头单位说明共同支持。", "evidence": evidence,
               "depends_on": [], "amount": {"currency": "CNY", "value": 423701834, "unit": "千元", "evidence": evidence}}
    raw = {"decision": "save_structured", "overall_score": 80, "confidence": .9,
           "facts": [{"fact_type": "finance", "fact_statement": revenue["statement"], "period": "2025年度",
                      "metrics": {"amount": 423701834, "currency": "CNY", "amount_unit": "千元"},
                      "evidence_locator": {"passage_id": "t1"}}],
           "content_review": {"protocol": PROTOCOL, "claims": [revenue], "bindings": {"/facts/0": ["revenue_2025"]}}}
    report = BundleValidator({}, require_content_review=True).validate(raw, passages)
    assert report.schema_valid, report.errors
    fact = report.bundle.facts[0]
    values = fact.metrics
    assert (values["amount"], values["currency"], values["amount_unit"]) == (423701834000, "CNY", "元")
    assert (values["amount_raw"], values["amount_raw_unit"]) == (423701834, "千元")
    assert values["amount_evidence"] == evidence
    assert fact.content_review["fields"]["metrics"]["status"] == "source_supported"
    assert not any("amount" in h["path"] for h in fact.content_review["held_fields"])
    assert "normalization_unsupported_amount_unit" not in json.dumps(fact.content_review)


def test_unconvertible_unit_keeps_raw_amount_and_notes_it():
    raw = case()
    money = next(c for c in raw["content_review"]["claims"] if c["id"] == "money")
    money["amount"].update(unit="万元等值", evidence={"passage_id": "p2", "quote": "4141.622"})
    raw["events"][0]["metrics"] = {"amount": 4141.622, "currency": "CNY", "amount_unit": "万元等值"}
    bundle = validate(raw)
    values = bundle.events[0].metrics
    assert (values["amount"], values["amount_unit"], values["amount_raw"]) == (4141.622, "万元等值", 4141.622)
    assert values["amount_conversion"] == {"status": "not_converted", "reason": "unknown_amount_unit"}
    review = bundle.events[0].content_review["fields"]["metrics/amount"]
    assert review["status"] == "source_supported" and review["conversion"]["status"] == "not_converted"


def test_review_survives_merge_sqlite_queries_cache_and_explanation(config, tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'mic.db'}")
    db.create_all()
    repo = Repository(db)
    run = repo.create_search_run("target", {}, {}, "q", "m")
    hit = SearchHit(query="中标", title="中标", url=SAMPLE["url"], domain="energytrend.cn")
    lid = repo.save_source_link(run, "query", hit, hit.url, "media")
    bundle = validate(case())
    merged = MultiModelMerger(config).merge(lid, "target", [ModelContribution("m", "fixture", bundle)])
    finalize(merged.bundle, build_context(None, []), PASSAGES, run_id=run, link_id=lid)
    repo.save_merged_analysis("target", lid, merged.bundle, {}, search_run_id=run)
    assert repo.get_recent_events("target")[0]["content_review"]["fields"]["entities/counterparty"]["states"] == {"owner": "unsupported"}
    assert repo.get_metric_observations("target")[0]["interpretation"] == ""
    assert repo.search_facts("target")[0]["entities"]["owner"] is None
    assert repo.explain_source_analysis(lid)["content_review"]["claims"]["comparison"]["status"] == "pending_review"
    assert repo.find_analyzed_link_by_canonical(hit.url, "target", reviewed_only=True).id == lid
    cloned = repo.clone_latest_analysis(lid, "copy", "target", reviewed_only=True)
    assert cloned["events"] == 1 and cloned["content_review"]["protocol"] == PROTOCOL
    assert cloned["cloned_events"][0]["content_review"] == merged.bundle.events[0].content_review
    with db.session() as session:
        assert all(b.why_it_matters == "" and b.content_review for b in session.scalars(select(m.AnalysisBrief)))
    import importlib.util
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tools/collector_acceptance.py"
    spec = importlib.util.spec_from_file_location("review_export_test", script)
    acceptance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acceptance)
    exported = acceptance.content_review(tmp_path, {"search_run_id": run})
    assert exported["sources"][0]["merged"][0]["content_review"]["protocol"] == PROTOCOL
    assert all(isinstance(row["record"]["content_review"], dict) for row in exported["formal_records"])


def test_agent_boundary_preserves_review_and_rejects_missing_review(tmp_path):
    from agent_trade_intel.adapters.common import ToolResult
    from agent_trade_intel.db import SQLiteStore
    from agent_trade_intel.persistence import ResultPersister
    bundle = validate(case())
    bundle.events[0].event_resolution = {"reviewed": True, "verdict": "new", "reason": "候选为空",
        "current_claim_ids": ["award"],
        "current_evidence": [{"passage_id": "p2", "quote": TEXT["p2"]}], "comparisons": []}
    finalize(bundle, build_context(None, []), PASSAGES, run_id="run", link_id="source")
    event = {**bundle.events[0].model_dump(mode="json"), "source_link_id": "source"}
    store = SQLiteStore(tmp_path / "agent.db")
    store.init_schema()
    result = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
        status="success", result={"search_run_id": "run", "event_resolution_protocol": "semantic_event_v1",
                                  "content_review_protocol": PROTOCOL, "all_events": [event]})
    persister = ResultPersister(store)
    task = {"target": {"target_id": "target", "company_name": "宁德时代"}}
    assert persister.save_mic_structures(task=task, result=result)["events"] == 1
    with store.session() as con:
        payload = json.loads(con.execute("SELECT payload_json FROM structured_events").fetchone()[0])
        assert payload["content_review"] == event["content_review"]
    import importlib.util
    script = Path(__file__).resolve().parents[3] / "agents/intelligence_collector_agent/tests/test_event_evidence_export.py"
    spec = importlib.util.spec_from_file_location("review_agent_queue", script)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    result.result["structured_outputs"] = {"events": 1}
    queued = fixture.through_agent(result.result, tmp_path / "queue")
    assert len(queued["events"]) == 1 and queued["pending"] == []
    # The reader returns the stored payload, including the shared field states.
    assert queued["events"][0]["payload"]["content_review"] == event["content_review"]
    bad = deepcopy(event)
    bad["source_link_id"] = "other-source"
    bad.pop("content_review")
    result.result["all_events"] = [bad]
    assert persister.save_mic_structures(task=task, result=result)["events_pending"] == 1


def test_mutation_after_review_does_not_leak_into_store_or_merge(config):
    reviewed = validate(case())
    reviewed.events[0].summary = "未经审核重新加回业主身份"
    merged = MultiModelMerger(config).merge("link", "target", [ModelContribution("m", "test", reviewed)])
    assert merged.bundle.events == []
    assert any(h["reason"] == "reviewed_content_changed" for h in merged.bundle.content_review["held"])


def test_legacy_cache_does_not_bypass_new_live_review(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    db.create_all()
    repo = Repository(db)
    hit = SearchHit(query="query", title="title", url="https://example.test/old", domain="example.test")
    lid = repo.save_source_link("old", "q", hit, hit.url, "media")
    with pytest.raises(ValueError, match="content_review_required"):
        repo.save_merged_analysis("target", lid, BundleExtraction(decision="save_structured"), {})
    repo.save_merged_analysis("target", lid, BundleExtraction(decision="save_structured"), {}, allow_legacy_write=True)
    assert repo.find_analyzed_link_by_canonical(hit.url, "target", reviewed_only=True) is None
    assert repo.clone_latest_analysis(lid, "new", "target", reviewed_only=True)["briefs"] == 0


def test_existing_database_gets_nullable_review_columns(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'old-schema.db'}")
    with db.engine.begin() as con:
        con.execute(text("CREATE TABLE event_card (id VARCHAR PRIMARY KEY)"))
        con.execute(text("INSERT INTO event_card(id) VALUES ('old')"))
    db.create_all()
    db.create_all()
    assert "content_review" in {c["name"] for c in inspect(db.engine).get_columns("event_card")}
    with db.engine.connect() as con:
        assert con.execute(text("SELECT content_review FROM event_card WHERE id='old'")).scalar() is None


def test_extraction_and_arbitration_share_the_same_policy():
    assert RULE_TEXT in SYSTEM_PROMPT and RULE_TEXT in ARBITRATION_SYSTEM


def test_policy_text_and_examples_cover_event_and_summary_bindings():
    from mic.content_review_policy import SCHEMA_HINT as HINT
    from mic.modeling.prompts import SCHEMA_HINT as PROMPT_HINT
    for phrase in ("只绑定描述该事项的 observation claim", "/events/i/impact", "current_claim_ids",
                   "brief.one_sentence 非空时必须有绑定", "完整表达这句话", "depends_on 引用它所汇总的各个 claim",
                   "复用同一 claim id"):
        assert phrase in RULE_TEXT, phrase
    event = HINT["binding_examples"]["event"]
    assert set(event["bindings"]) == {"/events/0", "/events/0/entities", "/events/0/impact"}
    assert event["bindings"]["/events/0"] == event["event_resolution"]["current_claim_ids"]
    kinds = {c["id"]: c["kind"] for c in event["claims"]}
    assert kinds[event["bindings"]["/events/0"][0]] == "observation"
    assert kinds[event["bindings"]["/events/0/entities"][0]] == "identity"
    assert kinds[event["bindings"]["/events/0/impact"][0]] == "analysis"
    brief = HINT["binding_examples"]["brief"]
    summary = brief["claims"][0]
    assert brief["bindings"]["/brief/one_sentence"] == [summary["id"]] and len(summary["depends_on"]) >= 2
    assert PROMPT_HINT["content_review"] is HINT
    assert "输出前自查" in SYSTEM_PROMPT


def summary_case():
    """Two reviewed figures + two source-given comparisons, summarised in one sentence."""
    raw = case()
    review = raw["content_review"]
    catl = claim("catl_amount", "宁德时代中标价4141.622万元。")
    envision = claim("envision_amount", "远景能源中标价19461.6万元。", "p1")
    catl_price = claim("catl_price", "来源给出二标段合单价1.035元/Wh。", kind="comparability", dependencies=["catl_amount"])
    catl_price["field_values"] = {"comparison": {"unit_price": 1.035}}
    envision_price = claim("envision_price", "来源给出一标段合单价0.518元/Wh。", "p1", kind="comparability",
                           dependencies=["envision_amount"])
    envision_price["field_values"] = {"comparison": {"unit_price": 0.518}}
    sentence = "宁德时代中标二标段4141.622万元（合单价1.035元/Wh）；远景能源中标一标段19461.6万元（合单价0.518元/Wh）。"
    summary = claim("lots_summary", sentence, dependencies=["catl_amount", "catl_price", "envision_amount", "envision_price"])
    summary["evidence"] = [{"passage_id": "p1", "quote": TEXT["p1"]}, {"passage_id": "p2", "quote": TEXT["p2"]}]
    review["claims"].extend([catl, envision, catl_price, envision_price, summary])
    raw["brief"]["one_sentence"] = sentence
    review["bindings"]["/brief/one_sentence"] = ["lots_summary"]
    return raw, sentence


def test_one_sentence_is_published_only_through_a_complete_summary_claim():
    raw, sentence = summary_case()
    bundle = validate(raw)
    assert bundle.brief.one_sentence == sentence
    assert bundle.brief.content_review["fields"]["one_sentence"]["status"] == "source_supported"
    # One premise not supported by the source: the whole sentence waits, nothing is rewritten.
    raw, sentence = summary_case()
    next(c for c in raw["content_review"]["claims"] if c["id"] == "envision_price")["status"] = "pending_review"
    bundle = validate(raw)
    assert bundle.brief.one_sentence == ""
    held = next(h for h in bundle.content_review["held"] if h["path"] == "/brief/one_sentence")
    assert held["original"] == sentence and held["reason"] == "claim_not_supported"
    assert bundle.content_review["claims"]["lots_summary"]["reason"] == "dependency_not_supported"
    # Binding a partial claim publishes only that claim's statement, never the free-text sentence.
    raw, sentence = summary_case()
    raw["content_review"]["bindings"]["/brief/one_sentence"] = ["catl_amount"]
    bundle = validate(raw)
    assert bundle.brief.one_sentence == "宁德时代中标价4141.622万元。"
    assert any(h["path"] == "/brief/one_sentence" for h in bundle.content_review["held"]) is False
    # No binding at all: held as missing_record_review, the program does not pick a claim.
    raw, sentence = summary_case()
    raw["content_review"]["bindings"].pop("/brief/one_sentence")
    bundle = validate(raw)
    assert bundle.brief.one_sentence == ""
    assert any(h["path"] == "/brief/one_sentence" and h["reason"] == "missing_record_review"
               for h in bundle.content_review["held"])


def test_event_record_binds_observation_and_impact_binds_analysis_separately():
    raw = case()
    review = raw["content_review"]
    impact = claim("award_impact", "该中标对收入为正向影响（分析推断）。", kind="analysis", dependencies=["award"])
    impact["field_values"] = {"impact": {"direction": "positive", "channels": ["revenue"],
                                         "horizon": "annual", "magnitude_guess": "unknown"}}
    review["claims"].append(impact)
    raw["events"][0]["event_type"] = "major_order"
    raw["events"][0]["impact"] = {"direction": "positive", "channels": ["revenue"], "horizon": "annual", "magnitude_guess": "unknown"}
    # Wrong: analysis claim mixed into the record binding.
    review["bindings"]["/events/0"] = ["award", "award_impact"]
    review["bindings"].pop("/events/0/summary")
    bundle = validate(raw)
    event = bundle.events[0]
    assert event.impact.direction == "unclear" and event.impact.channels == []
    assert any(h["path"] == "/events/0/impact" for h in bundle.content_review["held"])
    assert event.event_type == "" or event.content_review["fields"]["event_type"]["status"] != "source_supported"
    # Right: record → observation, entities → identity, impact → analysis with field_values.
    raw = case()
    review = raw["content_review"]
    review["claims"].append(deepcopy(impact))
    raw["events"][0]["event_type"] = "major_order"
    raw["events"][0]["impact"] = {"direction": "positive", "channels": ["revenue"], "horizon": "annual", "magnitude_guess": "unknown"}
    review["bindings"].pop("/events/0/summary")
    review["bindings"]["/events/0"] = ["award"]
    review["bindings"]["/events/0/impact"] = ["award_impact"]
    bundle = validate(raw)
    event = bundle.events[0]
    assert event.event_type == "major_order" and event.content_review["fields"]["event_type"]["status"] == "source_supported"
    assert event.summary == raw["content_review"]["claims"][2]["statement"]
    assert event.impact.direction == "positive" and event.impact.channels == ["revenue"]
    assert event.content_review["fields"]["impact"]["status"] == "source_supported"
    assert event.entities["subject"] == "宁德时代"
    # The event anchor must equal the summary's effective binding.
    from mic.event_resolution import build_context, finalize
    good = deepcopy(bundle)
    good.events[0].event_resolution = {"reviewed": True, "verdict": "new", "reason": "候选为空", "comparisons": [],
                                       "current_claim_ids": ["award"],
                                       "current_evidence": [{"passage_id": "p2", "quote": TEXT["p2"]}]}
    finalize(good, build_context(None, []), PASSAGES, run_id="run", link_id="link")
    assert good.events[0].event_resolution["status"] == "resolved"
    bad = deepcopy(bundle)
    bad.events[0].event_resolution = {**good.events[0].event_resolution["model_decision"], "current_claim_ids": ["award", "award_impact"]}
    finalize(bad, build_context(None, []), PASSAGES, run_id="run", link_id="link")
    assert bad.events[0].event_resolution["reason"] == "current_event_anchor_mismatch"
