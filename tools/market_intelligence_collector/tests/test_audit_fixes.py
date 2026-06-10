"""Tests for the spec-alignment audit fixes (max_tokens, PDF, feedback loop,
call triggers, merge semantics, stats hygiene)."""

import pytest

from mic.api import AnalystAPI
from mic.merge import ModelContribution, MultiModelMerger
from mic.modeling.adapter import ModelCallResult, ModelRegistry
from mic.modeling.call_planner import CallBudget, ModelCallPlanner
from mic.planner import PlannedQuery, QueryPlanner
from mic.profile import TargetProfile
from mic.reader import LinkReader
from mic.schemas import (
    BundleExtraction,
    CoverageGap,
    EventCard,
    SearchHit,
    TriageResult,
)
from mic.triage import SearchHitTriage

# --- A1: max_output_tokens from registry --------------------------------


def test_adapter_max_output_tokens_from_registry(config):
    registry = ModelRegistry(config)
    assert registry.adapters["deepseek_v4_pro"].max_output_tokens == 8192
    assert registry.adapters["siliconflow_qwen"].max_output_tokens == 4096
    # Models without an explicit value fall back to the registry default.
    default = config.model_registry.get("default_max_output_tokens", 4096)
    assert registry.adapters["openclaw_research"].max_output_tokens == default


# --- A3: query dedup keeps the winner unpenalized -----------------------


def test_planner_dedup_keeps_higher_score_without_penalty(config):
    planner = QueryPlanner(config)
    q_low = PlannedQuery(query_text="some unique query", query_family="f",
                         base_priority=50)
    q_high = PlannedQuery(query_text="some unique query", query_family="f",
                          base_priority=80)
    out = planner._score_all([q_low, q_high], entity_terms=[])
    assert len(out) == 1
    expected = 80 * planner.weights.get("topic_priority", 1.0)
    assert out[0].score == expected  # no duplication penalty applied


# --- B1: feedback weights reach planner and triage ----------------------


def test_planner_applies_family_feedback(config):
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    task = {"focus": ["operating_update"], "budget_profile": {"max_queries": 40}}
    planner = QueryPlanner(config)
    base = {q.query_text: q.score for q in planner.plan(profile, task)}
    boosted = planner.plan(profile, task,
                           family_feedback={"operating_metrics": 1.2})
    seen = False
    for q in boosted:
        if q.query_family == "operating_metrics" and q.query_text in base:
            assert q.score > base[q.query_text]
            seen = True
    assert seen


def test_triage_applies_source_type_feedback(config):
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    hit = SearchHit(provider="mock", query="宁德时代 订单", rank=1,
                    title="宁德时代获 5.6 亿元订单",
                    snippet="宁德时代中标储能订单", url="https://news.example.com/a",
                    domain="news.example.com")
    plain = SearchHitTriage(config).for_profile(profile).triage(hit, "l1")
    weighted = (SearchHitTriage(config).for_profile(profile)
                .set_source_feedback({"media": 0.5}).triage(hit, "l1"))
    assert weighted.read_priority == round(plain.read_priority * 0.5, 2)
    assert "source_type_feedback_x0.5" in weighted.matched_signals


# --- B2: call_model_when triggers ----------------------------------------


def _planner(config) -> ModelCallPlanner:
    return ModelCallPlanner(config, ModelRegistry(config),
                            CallBudget(max_model_calls_per_run=10))


def test_call_trigger_promotes_amount_signal(config):
    planner = _planner(config)
    tri = TriageResult(source_link_id="l1", triage_decision="read",
                       read_priority=30, need_model=False,
                       matched_signals=["amount_mentioned"])
    ok, reason = planner.should_call_model(tri, "media", duplicate=False)
    assert ok and reason == "contains_metric_or_amount"


def test_call_trigger_promotes_high_credibility_source(config):
    planner = _planner(config)
    tri = TriageResult(source_link_id="l1", triage_decision="read",
                       read_priority=30, need_model=False, matched_signals=[])
    ok, reason = planner.should_call_model(tri, "official", duplicate=False)
    assert ok and reason == "source_type_in"
    # An unprivileged source with no signals stays model-free.
    ok2, reason2 = planner.should_call_model(tri, "media", duplicate=False)
    assert not ok2 and reason2 == "triage_no_model"


# --- B4: early stop requires extracted objects ---------------------------


def _success(parsed: dict) -> ModelCallResult:
    return ModelCallResult(model_config_id="m", provider="p", provider_type="t",
                           model_name="n", status="success", parsed=parsed)


def test_early_stop_requires_objects(config):
    planner = _planner(config)
    no_objects = _success({"schema_version": "v", "confidence": 0.9, "facts": []})
    with_objects = _success({"schema_version": "v", "confidence": 0.9,
                             "facts": [{"fact_statement": "x"}]})
    assert planner._early_stop(no_objects, 0.4) is False
    assert planner._early_stop(with_objects, 0.4) is True


# --- B4 + A2: reader tables and PDF --------------------------------------

_HTML_WITH_TABLE = """
<html><head><title>宁德时代经营数据</title></head><body>
<p>宁德时代发布最新经营数据，签订 5.6 亿元订单。</p>
<table>
  <tr><th>指标</th><th>数值</th></tr>
  <tr><td>动力电池出货量</td><td>120 GWh</td></tr>
  <tr><td>毛利率</td><td>22.5%</td></tr>
</table>
</body></html>
"""


def test_reader_extracts_tables_as_passages(config):
    reader = LinkReader(config, search_provider=None)
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    title, _pt, body, tables, _imgs = reader._extract(_HTML_WITH_TABLE)
    assert len(tables) == 1
    assert "120 GWh" in tables[0] and "|" in tables[0]
    # Table rows are not duplicated into the flattened body.
    assert "120 GWh" not in body
    passages = reader._select_passages(title, body, tables, profile)
    table_passages = [p for p in passages if p.section.startswith("表格")]
    assert len(table_passages) == 1
    assert "毛利率" in table_passages[0].text


def _make_pdf(text: str) -> bytes:
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = w._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
        })
    })
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


_PDF_TEXT = "CATL signed an order of 560 million yuan on 2025-01-15"


def test_reader_parses_pdf_bytes(config):
    reader = LinkReader(config, search_provider=None)
    title, publish_time, body = reader._extract_pdf(_make_pdf(_PDF_TEXT))
    assert "560 million yuan" in body
    assert publish_time == "2025-01-15"


def test_read_routes_pdf_and_records_document_type(config, monkeypatch):
    reader = LinkReader(config, search_provider=None)
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    pdf = _make_pdf(_PDF_TEXT)
    monkeypatch.setattr(
        reader, "_fetch", lambda url: (pdf, 200, "application/pdf"))
    res = reader.read("l1", "https://static.example.com/notice.pdf", profile)
    assert res.read_status == "read"
    assert res.document_type == "pdf"
    assert res.content_hash


# --- anti-bot interstitial detection --------------------------------------

_BAIDU_CAPTCHA_HTML = """
<html><head><title>百度安全验证</title></head><body>
<div>网络环境异常，请完成验证后继续访问</div>
<div>拖动滑块完成拼图</div>
</body></html>
"""


def test_anti_bot_page_marked_failed(config, monkeypatch):
    reader = LinkReader(config, search_provider=None)
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    monkeypatch.setattr(
        reader, "_fetch", lambda url: (_BAIDU_CAPTCHA_HTML, 200, "text/html"))
    res = reader.read("l1", "https://baijiahao.baidu.com/s?id=1", profile)
    assert res.read_status == "failed"
    assert res.failure_reason == "anti_bot_page"
    assert res.passages == []


def test_long_article_mentioning_captcha_not_misclassified(config, monkeypatch):
    body = "<p>" + "宁德时代发布公告，签订 5.6 亿元订单。" * 40 + "近期百度安全验证机制升级。</p>"
    html = f"<html><head><title>宁德时代新闻</title></head><body>{body}</body></html>"
    reader = LinkReader(config, search_provider=None)
    profile = TargetProfile.from_config(config.get_target_profile("company_300750"))
    monkeypatch.setattr(reader, "_fetch", lambda url: (html, 200, "text/html"))
    res = reader.read("l1", "https://news.example.com/a", profile)
    assert res.read_status == "read"


# --- B3: multi-model agreement is NOT multi_source ------------------------


def test_multi_model_agreement_stays_single_source(config):
    merger = MultiModelMerger(config)
    e = {"event_type": "order", "summary": "签订订单",
         "entities": {"counterparty": "客户B"}, "confidence": 0.8}
    a = BundleExtraction(decision="save_structured", overall_score=80,
                         confidence=0.8, events=[EventCard(**e)])
    b = BundleExtraction(decision="save_structured", overall_score=80,
                         confidence=0.8, events=[EventCard(**e)])
    res = merger.merge("l1", "company_300750", [
        ModelContribution(model_config_id="m1", provider="deepseek", bundle=a),
        ModelContribution(model_config_id="m2", provider="qwen_dashscope", bundle=b)])
    assert res.bundle.events[0].source_corroboration_status == "single_source"


def test_merge_dedups_coverage_gaps(config):
    merger = MultiModelMerger(config)
    gap = CoverageGap(gap_type="missing_amount", description="缺少金额")
    a = BundleExtraction(decision="save_structured", overall_score=80,
                         confidence=0.8, coverage_gaps=[gap])
    b = BundleExtraction(decision="save_structured", overall_score=80,
                         confidence=0.8, coverage_gaps=[gap.model_copy()])
    res = merger.merge("l1", "company_300750", [
        ModelContribution(model_config_id="m1", provider="deepseek", bundle=a),
        ModelContribution(model_config_id="m2", provider="qwen_dashscope", bundle=b)])
    assert len(res.bundle.coverage_gaps) == 1


# --- B4: API version pinning ----------------------------------------------


def test_collect_intelligence_rejects_unknown_policy_version():
    import pytest
    api = AnalystAPI()
    with pytest.raises(ValueError, match="model_policy_version"):
        api.collect_intelligence("company_300750", {
            "budget_profile": {"max_queries": 5}},
            model_policy_version="model_policy_v999")


# --- C: search_facts no-match returns empty -------------------------------


def test_search_facts_no_match_returns_empty():
    api = AnalystAPI()
    api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 20, "max_links_to_read": 10,
                           "max_model_calls": 10},
    })
    all_facts = api.search_facts("company_300750", query="")
    assert all_facts  # the mock run produced facts
    none = api.search_facts("company_300750", query="zzz_nonexistent_term_xyz")
    assert none == []


# --- search-hit budget coherence ------------------------------------------


def test_default_max_hits_derived_from_budget(config):
    from mic.pipeline import Pipeline
    pipe = Pipeline(config)
    engines = len(getattr(getattr(pipe.search, "primary", pipe.search),
                          "providers", [])) or 1
    assert pipe._default_max_hits(10) == min(800, 10 * engines * pipe._hits_per_query)
    assert pipe._default_max_hits(999) == 800  # hard guardrail


def test_hit_budget_truncation_is_reported():
    api = AnalystAPI()
    report = api.collect_intelligence("company_300750", {
        "focus": ["operating_update", "customer_change"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 20, "max_search_hits": 3,
                           "max_links_to_read": 3, "max_model_calls": 3},
    })
    s = report["summary"]
    assert s["search_hits"] == 3
    # The tight hit budget must be visible, not silent.
    assert s["queries_skipped_by_hit_budget"] > 0
    assert s["queries_executed"] + s["queries_skipped_by_hit_budget"] <= \
        s["queries_generated"] + s["queries_executed"]


# --- A2: document_type / source_name / access_profile_id persisted -------


def test_source_link_metadata_columns_populated():
    api = AnalystAPI()
    report = api.collect_intelligence("company_300750", {
        "focus": ["operating_update"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 10, "max_links_to_read": 6,
                           "max_model_calls": 6},
    })
    links = api.repo.source_links_for_run(report["search_run_id"], limit=50)
    assert links
    read_link = next(
        (lk for lk in links if lk["read_status"] in ("read", "link_record_only")), None)
    assert read_link is not None
    row = api.repo.get_link(read_link["source_link_id"])
    assert row.source_name  # set at save time from the hit domain
    assert row.document_type == "html"
    assert row.access_profile_id


# --- entity normalization: alias dedup + vague-entity filtering ------------


def test_entity_identity_key_prefers_ticker_and_detects_vague():
    from mic.schemas import EntityRef
    short = EntityRef(name="宁德时代", ticker="300750")
    full = EntityRef(name="宁德时代新能源科技股份有限公司", ticker="300750")
    assert short.identity_key() == full.identity_key()
    # No ticker: identity falls back to the normalized name.
    assert EntityRef(name="现代汽车").identity_key() == \
        EntityRef(name="现代汽车 ").identity_key()
    assert EntityRef(name="多家锂电设备商").is_vague()
    assert EntityRef(name="相关供应商").is_vague()
    assert EntityRef(name="").is_vague()
    assert not EntityRef(name="新宙邦").is_vague()
    # A ticker pins the entity even if the name looks collective.
    assert not EntityRef(name="多家锂电设备商", ticker="300450").is_vague()


def _rel(subj_name, subj_ticker, rel_type, obj_name, confidence=0.7):
    from mic.schemas import EntityRef, RelationRecord
    return RelationRecord(
        subject_entity=EntityRef(name=subj_name, ticker=subj_ticker),
        relation_type=rel_type,
        object_entity=EntityRef(name=obj_name),
        confidence=confidence)


def test_merge_collapses_alias_relations_and_drops_vague(config):
    merger = MultiModelMerger(config)
    a = BundleExtraction(decision="save_structured", overall_score=80,
                         confidence=0.8, relations=[
                             _rel("宁德时代", "300750", "supplier_of", "现代汽车", 0.6)])
    b = BundleExtraction(decision="save_structured", overall_score=80,
                         confidence=0.8, relations=[
                             _rel("宁德时代新能源科技股份有限公司", "300750",
                                  "supplier_of", "现代汽车", 0.8),
                             _rel("宁德时代", "300750", "customer_of", "多家锂电设备商")])
    res = merger.merge("l1", "company_300750", [
        ModelContribution(model_config_id="m1", provider="deepseek", bundle=a),
        ModelContribution(model_config_id="m2", provider="qwen_dashscope", bundle=b)])
    assert len(res.bundle.relations) == 1
    kept = res.bundle.relations[0]
    assert kept.confidence == 0.8  # higher-confidence alias wins
    assert kept.relation_type == "supplier_of"


def test_single_model_path_sanitizes_relations(config):
    merger = MultiModelMerger(config)
    a = BundleExtraction(decision="save_structured", overall_score=80,
                         confidence=0.8, relations=[
                             _rel("宁德时代", "300750", "supplier_of", "现代汽车", 0.6),
                             _rel("宁德时代新能源科技股份有限公司", "300750",
                                  "supplier_of", "现代汽车", 0.8),
                             _rel("宁德时代", "300750", "customer_of", "多家锂电设备商")])
    res = merger.merge("l1", "company_300750", [
        ModelContribution(model_config_id="m1", provider="deepseek", bundle=a)])
    assert res.merge_method == "single_model"
    assert len(res.bundle.relations) == 1
    assert res.bundle.relations[0].confidence == 0.8


def test_get_relations_dedups_aliases_across_links():
    from mic.store import models as m
    from mic.utils import new_id, now
    api = AnalystAPI()
    tid = f"company_test_reldedup_{new_id('t')}"

    def row(subj_name, link_id, conf, obj_name="现代汽车"):
        return m.RelationRecordRow(
            id=new_id("rel"), source_link_id=link_id, target_id=tid,
            subject_entity={"name": subj_name, "type": "company", "ticker": "300750"},
            relation_type="supplier_of",
            object_entity={"name": obj_name, "type": "company", "ticker": None},
            qualifiers={}, evidence_locator={}, confidence=conf, created_at=now())

    with api.repo.db.session() as s:
        s.add(row("宁德时代", "link_a", 0.9))
        s.add(row("宁德时代新能源科技股份有限公司", "link_b", 0.6))
        s.add(row("宁德时代", "link_c", 0.7, obj_name="多家锂电设备商"))

    rels = api.repo.get_relations(tid)
    assert len(rels) == 1  # aliases collapsed, vague counterparty dropped
    assert rels[0]["confidence"] == 0.9
    assert set(rels[0]["source_link_ids"]) == {"link_a", "link_b"}


# --- release / tool safety -------------------------------------------------


def test_e2e_real_mode_rejects_mock_active(config):
    from tools.e2e_validate import enforce_real_mode_config

    config.raw["search_providers"]["active"] = "mock"
    with pytest.raises(RuntimeError):
        enforce_real_mode_config(config)


def test_governance_does_not_advertise_unimplemented_same_event_cache(config):
    reuse = config.call_governance.get("reuse", {})
    assert reuse.get("same_event_cache") is False
