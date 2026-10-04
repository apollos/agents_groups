"""Offline regressions from the first persistent CATL pilot."""
from copy import deepcopy
from pathlib import Path

from mic.browser.contracts import RawResult
from mic.browser.relevance import content_form_ok, judge
from mic.config import MICConfig, load_config
from mic.pipeline import Pipeline, RunStats
from mic.planner import QueryPlanner
from mic.profile import TargetProfile
from mic.run_context import TargetIdentity
from mic.schemas import SearchHit, TriageResult


def catl():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config")
    return cfg, TargetProfile.from_config(cfg.get_target_profile("company_300750"))


def test_catl_two_query_budget_covers_orders_and_announcements():
    cfg, profile = catl()
    planner = QueryPlanner(cfg)
    task = {"focus": ["operating_update"], "budget_profile": {"max_queries": 2}}
    before = deepcopy(task)
    old = planner.plan(profile, task)
    assert [q.query_family for q in old] == ["orders_tender", "orders_tender"]
    plan = planner.plan(profile, task, coverage_first=True)
    assert [q.query_family for q in plan] == ["orders_tender", "official_ir"]
    assert plan[0].query_text == "宁德时代 动力电池 中标"
    assert plan[1].query_text == "宁德时代 重大合同 公告"
    assert (plan[0].score, plan[1].score) == (122, 115)
    assert all(q.score >= planner.min_score for q in plan)
    assert all(any("覆盖优先" in why for why in q.why) for q in plan)
    assert task == before
    # Every smaller executed prefix starts with the same coverage choices.
    larger = planner.plan(profile, {**task, "budget_profile": {"max_queries": 20}}, coverage_first=True)
    assert [q.query_text for q in larger[:2]] == [q.query_text for q in plan]


def test_diversity_keeps_score_floor_feedback_and_single_family_capacity():
    cfg = MICConfig(raw={"query_scoring": {"min_score_to_execute": 45,
        "weights": {"entity_match": 0, "time_sensitivity": 0}},
        "query_families": {"families": {
            "orders_tender": {"base_priority": 90, "templates": ["{company} 订单", "{company} 合同"]},
            "official_ir": {"base_priority": 80, "templates": ["{company} 公告"]},
            "weak": {"base_priority": 10, "templates": ["{company} 闲聊"]}}},
        "source_packs": {"packs": {"tender": {"base_priority": 89, "templates": ["{company} 招标"]}}}})
    planner = QueryPlanner(cfg)
    profile = TargetProfile(target_id="fixture", type="company", canonical_name="测试公司")
    task = {"budget_profile": {"max_queries": 2}}
    plan = planner.plan(profile, task, coverage_first=True)
    assert {q.query_family for q in plan} == {"official_ir", "source_pack:tender"}
    boosted = planner.plan(profile, task, family_feedback={"official_ir": 2}, coverage_first=True)
    assert boosted[0].query_family == "official_ir"
    lowered = planner.plan(profile, task, family_feedback={"official_ir": .1}, coverage_first=True)
    assert len(lowered) == 2 and all(q.query_family in ("orders_tender", "source_pack:tender") for q in lowered)
    assert all(q.query_family != "weak" for q in boosted + lowered)
    zero = planner.plan(profile, {"budget_profile": {"max_queries": 0}}, coverage_first=True)
    assert zero == []


def test_enterprise_directory_cannot_consume_read_budget_after_triage():
    url = "https://www.bidcenter.com.cn/enterprise/ndsdxnykjgfyxgs/"
    title = "宁德时代新能源科技股份有限公司_最新招标采购公告_采招网"
    identity = TargetIdentity(target_id="company_300750", canonical_name="宁德时代", aliases=["宁德时代新能源科技股份有限公司"])
    relevance = judge(RawResult(title=title, raw_href=url, url=url, snippet="宁德时代中标采购公告", rank_in_page=1),
                      "宁德时代 中标", identity)
    assert relevance.target_match and relevance.content_form_ok is False
    assert "enterprise_directory" in relevance.reasons
    hit = SearchHit(query="宁德时代 中标", title=title, url=url,
                    discovery={"relevance": relevance.as_dict()})
    promoted = TriageResult(source_link_id="directory", triage_decision="read", read_priority=99, need_model=True)
    stats = RunStats()
    final = Pipeline._apply_read_gate(hit, promoted, stats)
    assert final.triage_decision == "link_record_only" and not final.need_model
    assert stats.read_gate_demoted == {"content_form": 1}
    # No blanket site or keyword ban; article-shaped paths remain eligible.
    assert content_form_ok("https://www.bidcenter.com.cn/news-123456-4.html", "宁德时代项目中标公告")[0]
    assert content_form_ok("https://news.example.com/enterprise/catl-order.html", "宁德时代项目中标公告")[0]
    assert content_form_ok("https://bidcenter.com.cn.example.net/enterprise/catl", "宁德时代项目中标公告")[0]


def test_browser_pipeline_passes_effective_cap_and_coverage_mode():
    # Stop at the planner boundary: no search, model, database or browser work.
    from types import SimpleNamespace
    cfg, profile = catl()
    pipe = Pipeline.__new__(Pipeline)
    pipe.search = SimpleNamespace(browser_backed=True)
    pipe._hits_per_query = 10
    pipe._family_feedback = {}
    pipe.vision = SimpleNamespace(reset_run=lambda: None)
    task = {"focus": ["operating_update"], "budget_profile": {"max_queries": 20}}
    original = deepcopy(task)
    class PlanningObserved(Exception):
        pass
    def capture(actual_profile, actual_task, *, family_feedback, coverage_first):
        assert actual_profile is profile and coverage_first is True
        assert actual_task["budget_profile"]["max_queries"] == 2
        raise PlanningObserved()
    pipe.planner = SimpleNamespace(plan=capture)
    context = SimpleNamespace(budget=SimpleNamespace(limits={"max_queries": 2, "max_search_hits": 20,
        "max_links_to_read": 6, "max_hits_per_query": 30}), browser_runtime={"cache": {"reuse_analysis": False}})
    try:
        pipe._execute("offline", profile, task, None, RunStats(), context, None)
    except PlanningObserved:
        pass
    else:
        raise AssertionError("planner was not invoked")
    assert task == original
