from mic.api import AnalystAPI
from mic.planner import QueryPlanner
from mic.profile import TargetProfile


def test_query_planner_scores_and_caps(config):
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    planner = QueryPlanner(config)
    plan = planner.plan(profile, {
        "focus": ["operating_update", "customer_change"],
        "budget_profile": {"max_queries": 20},
    })
    assert 0 < len(plan) <= 20
    # Sorted descending by score.
    scores = [q.score for q in plan]
    assert scores == sorted(scores, reverse=True)
    # Queries containing the company name should score above the floor.
    assert all(q.score >= planner.min_score for q in plan)


def test_collect_intelligence_end_to_end():
    api = AnalystAPI()
    report = api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change", "risk"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 30, "max_links_to_read": 15, "max_model_calls": 20},
    })
    s = report["summary"]
    assert s["queries_executed"] > 0
    assert s["search_hits"] > 0
    assert s["links_read"] > 0
    assert s["model_calls"] <= 20  # budget respected
    # Mock content guarantees at least some structured output.
    assert report["structured_outputs"]["briefs"] >= 1


def test_analyst_api_readback():
    api = AnalystAPI()
    api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 25, "max_links_to_read": 12, "max_model_calls": 15},
    })
    events = api.get_recent_events("company_300750", since="30d")
    relations = api.get_relations("company_300750", since="180d")
    assert isinstance(events, list)
    assert isinstance(relations, list)
    # The mock emits supplier_of relations for order-bearing pages.
    assert any(r["relation_type"] == "supplier_of" for r in relations) or relations == []


def test_budget_caps_model_calls():
    api = AnalystAPI()
    report = api.collect_intelligence("industry_pv_glass", {
        "focus": ["industry_supply_demand", "policy"],
        "time_window": "60d",
        "budget_profile": {"max_queries": 40, "max_links_to_read": 30, "max_model_calls": 5},
    })
    assert report["summary"]["model_calls"] <= 5


def test_signals_and_efficiency_fields():
    api = AnalystAPI()
    report = api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change", "policy", "price_cost_margin"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 40, "max_links_to_read": 20, "max_model_calls": 25},
    })
    so = report["structured_outputs"]
    # New signal objects (spec 13.7-13.9) are produced and counted.
    assert so["customer_supplier_signals"] >= 1
    eff = report["call_efficiency"]
    assert "batch_triage_saved_calls_estimate" in eff
    assert "passage_selection_saved_tokens_estimate" in eff
    assert eff["passage_selection_saved_tokens_estimate"] >= 0


def test_new_focus_areas_planned():
    from mic.config import load_config
    from mic.planner import QueryPlanner
    from mic.profile import TargetProfile
    cfg = load_config()
    profile = TargetProfile.from_config(cfg.get_target_profile("company_300750"))
    plan = QueryPlanner(cfg).plan(profile, {
        "focus": ["capital_markets", "overseas_trade"],
        "budget_profile": {"max_queries": 60},
    })
    families = {q.query_family for q in plan}
    assert "capital_markets" in families
    assert "overseas_trade" in families


def test_relation_direction_conflict_triggers_arbitration():
    # Two contributions asserting inverse relations on the same ordered pair must
    # produce a relation_direction conflict that the planner flags for arbitration.
    from mic.config import load_config
    from mic.merge import ModelContribution, MultiModelMerger
    from mic.modeling.call_planner import ModelCallPlanner
    from mic.schemas import BundleExtraction, EntityRef, RelationRecord
    cfg = load_config()
    a = BundleExtraction(decision="save_structured", confidence=0.7, relations=[
        RelationRecord(subject_entity=EntityRef(name="公司A"), relation_type="supplier_of",
                       object_entity=EntityRef(name="客户B"), confidence=0.7)])
    b = BundleExtraction(decision="save_structured", confidence=0.7, relations=[
        RelationRecord(subject_entity=EntityRef(name="公司A"), relation_type="customer_of",
                       object_entity=EntityRef(name="客户B"), confidence=0.7)])
    merger = MultiModelMerger(cfg)
    res = merger.merge("link_x", "company_300750", [
        ModelContribution(model_config_id="m1", provider="deepseek", bundle=a),
        ModelContribution(model_config_id="m2", provider="qwen_dashscope", bundle=b)])
    assert any(c["field"] == "relation_direction" for c in res.field_conflicts)
    assert ModelCallPlanner.arbitration_triggered(res.field_conflicts, cfg)


def test_cascade_mode_executes():
    from mic.config import load_config
    from mic.modeling.adapter import ModelRegistry
    from mic.modeling.call_planner import CallBudget, ModelCallPlanner
    cfg = load_config()
    planner = ModelCallPlanner(cfg, ModelRegistry(cfg), CallBudget(max_model_calls_per_run=10))
    policy = cfg.model_policies["tasks"]["cascade_extraction"]
    msgs = [{"role": "system", "content": "x"},
            {"role": "user", "content": '{"task":"bundle_extraction","selected_passages":[]}'}]
    outputs = planner._cascade(policy, msgs)
    assert len(outputs) >= 1


def test_arbitrate_executes_and_returns_output():
    from mic.config import load_config
    from mic.modeling.adapter import ModelRegistry
    from mic.modeling.call_planner import CallBudget, ModelCallPlanner
    from mic.profile import TargetProfile
    from mic.reader import ReadResult
    from mic.schemas import Passage
    cfg = load_config()
    planner = ModelCallPlanner(cfg, ModelRegistry(cfg), CallBudget(max_model_calls_per_run=10))
    profile = TargetProfile.from_config(cfg.get_target_profile("company_300750"))
    read = ReadResult(source_link_id="link_x", read_status="read",
                      passages=[Passage(passage_id="p0", section="正文", text="宁德时代与特斯拉签订订单")])
    outputs = planner.arbitrate(profile, read, {"source_link_id": "link_x"},
                                [{"field": "amount", "values": [12, 23]}])
    assert len(outputs) >= 1
    assert outputs[0].parsed is not None


def test_task_splitting_on_long_text():
    from mic.config import load_config
    from mic.modeling.adapter import ModelRegistry
    from mic.modeling.call_planner import CallBudget, ModelCallPlanner
    from mic.profile import TargetProfile
    from mic.reader import ReadResult
    from mic.schemas import Passage, TriageResult
    cfg = load_config()
    planner = ModelCallPlanner(cfg, ModelRegistry(cfg), CallBudget(max_model_calls_per_run=10))
    planner.max_input_chars = 50  # force splitting
    profile = TargetProfile.from_config(cfg.get_target_profile("company_300750"))
    passages = [Passage(passage_id=f"p{i}", section=f"第{i}段", text="宁德时代订单" * 10)
                for i in range(3)]
    read = ReadResult(source_link_id="link_x", read_status="read", passages=passages)
    tri = TriageResult(source_link_id="link_x", triage_decision="read",
                       read_priority=70, need_model=True)
    result = planner.run_for_link(profile, read, {"source_link_id": "link_x"}, tri,
                                  "media", 70)
    assert result.was_split is True
    assert len(result.outputs) >= 2


def test_explain_includes_triage_reason():
    api = AnalystAPI()
    report = api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 20, "max_links_to_read": 10, "max_model_calls": 10},
    })
    links = api.repo.source_links_for_run(report["search_run_id"], decision="read", limit=1)
    assert links, "expected at least one read source link"
    explanation = api.explain_source_analysis(links[0]["source_link_id"])
    # Triage metadata is now persisted, so explainability is non-empty.
    assert explanation["why_selected"]
    assert isinstance(explanation["matched_signals"], list)


def test_cross_run_reuse_clones_analysis():
    api = AnalystAPI()
    task = {
        "focus": ["operating_update", "customer_change"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 20, "max_links_to_read": 12, "max_model_calls": 15},
    }
    first = api.collect_intelligence("company_300750", task)
    assert first["summary"]["cached_or_reused_results"] == 0
    # Same target + deterministic mock search => identical canonical URLs, so the
    # second run should reuse prior analysis instead of re-calling models.
    second = api.collect_intelligence("company_300750", task)
    assert second["summary"]["cached_or_reused_results"] > 0
    # Reused structured rows are still tallied (cloned briefs/events/etc.).
    assert second["structured_outputs"]["briefs"] >= 1


def test_storage_policy_no_raw_content_columns():
    from sqlalchemy import inspect

    from mic.config import load_config
    from mic.store import get_database
    api = AnalystAPI()
    api.collect_intelligence("company_300750", {
        "focus": ["operating_update"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 10, "max_links_to_read": 6, "max_model_calls": 6},
    })
    inspector = inspect(get_database(load_config().database_url).engine)
    raw = {"html", "full_text", "raw_content", "screenshot", "page_body"}
    suspicious = [f"{t}.{c['name']}" for t in inspector.get_table_names()
                  for c in inspector.get_columns(t) if c["name"].lower() in raw]
    assert suspicious == []


def test_feedback_adjusts_model_weight():
    api = AnalystAPI()
    fid = api.submit_feedback({
        "object_type": "event", "object_id": "evt_x", "correct": True,
        "useful_for_analysis": True, "model_config_id": "qwen_plus",
        "query_family": "orders_tender", "source_type": "exchange",
    })
    assert fid
    scores = api.repo.model_feedback_scores()
    assert scores.get("qwen_plus", 0) > 1.0  # positive feedback raises weight


def test_editable_install_metadata_present():
    # README tells users to run: pip install -e ".[dev]". This file is the
    # minimal project metadata that makes that command work.
    from pathlib import Path
    assert (Path(__file__).resolve().parents[1] / "pyproject.toml").exists()


def test_real_search_provider_missing_key_falls_back_to_mock_when_allowed(monkeypatch):
    from mic.config import load_config
    from mic.search import MockSearchProvider, build_search_provider
    cfg = load_config()
    cfg.raw["search_providers"]["active"] = "serpapi"
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("MIC_ALLOW_MOCK", "true")
    provider = build_search_provider(cfg)
    assert isinstance(provider, MockSearchProvider)


def test_real_search_provider_missing_key_fails_fast_when_mock_disabled(monkeypatch):
    import pytest

    from mic.config import load_config
    from mic.search import build_search_provider
    cfg = load_config()
    cfg.raw["search_providers"]["active"] = "bing"
    monkeypatch.delenv("BING_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("MIC_ALLOW_MOCK", "false")
    with pytest.raises(RuntimeError):
        build_search_provider(cfg)


def test_max_search_hits_budget_is_strictly_respected():
    api = AnalystAPI()
    report = api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change"],
        "time_window": "30d",
        "budget_profile": {
            "max_queries": 20,
            "max_search_hits": 3,
            "max_links_to_read": 3,
            "max_model_calls": 3,
        },
    })
    assert report["summary"]["search_hits"] == 3
    assert report["summary"]["unique_source_links"] <= 3


def test_bundle_coverage_gaps_are_persisted_for_api_readback():
    api = AnalystAPI()
    report = api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change", "risk"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 20, "max_links_to_read": 8, "max_model_calls": 8},
    })
    assert report["structured_outputs"]["coverage_gaps"] > 0
    gaps = api.get_coverage_gaps("company_300750")
    assert len(gaps) >= report["structured_outputs"]["coverage_gaps"]


def test_merge_policy_min_overall_score_downgrades_to_link_only():
    from mic.config import load_config
    from mic.merge import ModelContribution, MultiModelMerger
    from mic.schemas import BundleExtraction, FactItem
    cfg = load_config()
    merger = MultiModelMerger(cfg)

    single = BundleExtraction(
        decision="save_structured", overall_score=50, confidence=0.9,
        facts=[FactItem(fact_type="claim", fact_statement="low quality claim")],
    )
    single_res = merger.merge("link_low", "company_300750", [
        ModelContribution(model_config_id="m1", provider="deepseek", bundle=single)
    ])
    assert single_res.bundle.decision == "link_only"
    assert single_res.bundle.facts == []

    a = BundleExtraction(decision="save_structured", overall_score=55, confidence=0.9)
    b = BundleExtraction(decision="save_structured", overall_score=60, confidence=0.8)
    multi_res = merger.merge("link_low2", "company_300750", [
        ModelContribution(model_config_id="m1", provider="deepseek", bundle=a),
        ModelContribution(model_config_id="m2", provider="qwen_dashscope", bundle=b),
    ])
    assert multi_res.bundle.decision == "link_only"


def test_single_model_mode_calls_exactly_one_model():
    from mic.config import load_config
    from mic.modeling.adapter import ModelRegistry
    from mic.modeling.call_planner import CallBudget, ModelCallPlanner
    cfg = load_config()
    planner = ModelCallPlanner(cfg, ModelRegistry(cfg), CallBudget(max_model_calls_per_run=10))
    policy = dict(cfg.model_policies["tasks"]["bundle_extraction"])
    policy["call_mode"] = "single_model"
    msgs = [{"role": "system", "content": "x"},
            {"role": "user", "content": '{"task":"bundle_extraction","selected_passages":[]}'}]
    outputs = planner._dispatch("single_model", policy, msgs)
    assert len(outputs) == 1
    assert planner.budget.calls_used == 1


def test_batch_triage_does_not_consume_the_only_model_call():
    api = AnalystAPI()
    report = api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change", "risk"],
        "time_window": "30d",
        "budget_profile": {
            "max_queries": 20,
            "max_links_to_read": 5,
            "max_model_calls": 1,
        },
    })
    assert report["summary"]["model_calls"] <= 1
    assert report["summary"]["batch_triage_calls"] == 0
    assert report["structured_outputs"]["briefs"] >= 1
