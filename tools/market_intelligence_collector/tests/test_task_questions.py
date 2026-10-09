"""Explicit research questions (task_profile.questions) and the read-stage changes made
with them: official-domain identity in triage, first-hand read slot, read queue
continuation after failed reads, PDF table blocks and symbol glyph normalisation.

Scenario behind these tests: "collect CATL's FY2025 revenue / net profit / operating
cash flow and YoY". focus families never asked for it (not searched); the official
annual-report summary PDF was then skipped by triage (no company name in its title),
lost its slot to aggregator pages that yielded no article, and finally reached the
model without its financial table and with a Wingdings glyph breaking the unit claim.
"""

from __future__ import annotations

import pytest

from mic.modeling.call_planner import CallBudget, ModelCallPlanner
from mic.modeling.prompts import build_bundle_messages
from mic.pipeline import Pipeline
from mic.planner import QUESTION_SCORE, QueryPlanner
from mic.profile import TargetProfile
from mic.reader import LinkReader, pdf_table_blocks
from mic.schemas import SearchHit, TriageResult
from mic.task_questions import (
    QUESTION_FAMILY, TaskQuestion, parse_task_questions, question_terms, task_context,
)
from mic.triage import SearchHitTriage

QUESTION = {
    "question": "宁德时代2025年全年营业收入、归母净利润、经营活动现金流量净额及同比；明确报告期、单位、来源",
    "search_terms": ["2025年年度报告 营业收入 归母净利润 经营活动产生的现金流量净额 同比"],
    "period": "2025年度",
}


@pytest.fixture
def profile(config):
    return TargetProfile.from_config(config.get_target_profile("company_300750"))


# --- task_questions -------------------------------------------------------------------

def test_parse_accepts_strings_and_objects_and_rejects_malformed():
    qs = parse_task_questions({"questions": [QUESTION, "宁德时代 2025年 业绩快报"]})
    assert [q.period for q in qs] == ["2025年度", None]
    assert qs[1].search_phrases() == ["宁德时代 2025年 业绩快报"]
    assert parse_task_questions({}) == [] and parse_task_questions({"questions": []}) == []
    with pytest.raises(ValueError):
        parse_task_questions({"questions": [{"period": "2025"}]})
    with pytest.raises(ValueError):
        parse_task_questions({"questions": "x" * 0 or [""]})


def test_terms_come_from_explicit_search_terms_only_when_given():
    q = TaskQuestion.from_value(QUESTION)
    assert q.terms() == ["2025年年度报告", "营业收入", "归母净利润", "经营活动产生的现金流量净额", "同比", "2025年度"]
    plain = TaskQuestion.from_value("营业收入、归母净利润 及 同比")
    assert plain.terms() == ["营业收入", "归母净利润", "同比"]
    # The target's own names are entity terms already, not task terms.
    assert "宁德时代" not in question_terms([TaskQuestion.from_value("宁德时代 营业收入")], exclude=["宁德时代"])


def test_task_context_is_none_without_questions_and_lists_question_and_period():
    assert task_context([]) is None
    ctx = task_context(parse_task_questions({"questions": [QUESTION]}))
    assert ctx["questions"] == [{"question": QUESTION["question"], "period": "2025年度"}]


# --- planner --------------------------------------------------------------------------

def test_planner_puts_question_queries_first_within_the_same_budget(config, profile):
    planner = QueryPlanner(config)
    task = {"focus": ["financial_leading_indicator"], "budget_profile": {"max_queries": 2},
            "questions": [QUESTION]}
    plan = planner.plan(profile, task, coverage_first=True)
    assert len(plan) == 2  # no budget inflation
    assert plan[0].query_family == QUESTION_FAMILY and plan[0].score == QUESTION_SCORE
    assert plan[0].query_text == "宁德时代 " + QUESTION["search_terms"][0]
    assert plan[1].query_family != QUESTION_FAMILY
    # Without questions the plan is unchanged from before.
    baseline = planner.plan(profile, {k: v for k, v in task.items() if k != "questions"}, coverage_first=True)
    assert [q.query_text for q in baseline][0] == plan[1].query_text


def test_planner_does_not_prefix_target_when_phrase_already_names_it(config, profile):
    plan = QueryPlanner(config).plan(
        profile, {"focus": [], "budget_profile": {"max_queries": 1},
                  "questions": ["CATL 2025 annual report revenue"]})
    assert plan[0].query_text == "CATL 2025 annual report revenue"


# --- triage ---------------------------------------------------------------------------

def _hit(url, title, snippet="", rank=1):
    from mic.utils import domain_of
    return SearchHit(query="q", title=title, snippet=snippet, url=url, domain=domain_of(url), rank=rank)


def test_official_domain_counts_as_target_identity_and_first_hand_source(config, profile):
    tri = SearchHitTriage(config).for_profile(profile)
    assert profile.official_domains == ["catl.com"]
    official = tri.triage(_hit("https://www.catl.com/uploads/x/2025.pdf", "2025年年度报告摘要"), "l1")
    assert "target_official_domain" in official.matched_signals
    assert "high_credibility_source" in official.matched_signals
    assert tri.source_type("www.catl.com") == "company"
    other = tri.triage(_hit("https://www.example-media.com/a/1", "2025年年度报告摘要"), "l2")
    assert official.read_priority > other.read_priority
    assert tri.source_type("www.example-media.com") == "media"


def test_task_terms_add_bounded_bonus_and_signal(config, profile):
    tri = SearchHitTriage(config).for_profile(profile)
    hit = _hit("https://m.example.com/a", "宁德时代 营业收入 归母净利润 经营活动现金流 同比")
    base = tri.triage(hit, "l1").read_priority
    tri.set_task_terms(["营业收入", "归母净利润", "经营活动现金流", "同比"])
    with_terms = tri.triage(hit, "l2")
    assert "task_question_match" in with_terms.matched_signals
    assert with_terms.read_priority == base + 18  # 6 per term, capped at 3 terms


# --- read queue -----------------------------------------------------------------------

def _tri(link_id, score, signals):
    return TriageResult(source_link_id=link_id, triage_decision="read", read_priority=score,
                        matched_signals=signals, need_model=True, suggested_task="bundle_extraction",
                        reason="")


def test_first_hand_slot_reserved_without_reordering_the_rest():
    queue = [("a", None, _tri("a", 111, ["target_entity_match"])),
             ("b", None, _tri("b", 106, ["target_entity_match"])),
             ("c", None, _tri("c", 76, ["target_official_domain", "high_credibility_source"])),
             ("d", None, _tri("d", 70, ["target_entity_match"]))]
    assert [x[0] for x in Pipeline._reserve_first_hand_slot(queue)] == ["c", "a", "b", "d"]
    # Already first: unchanged. No first-hand candidate: unchanged.
    assert [x[0] for x in Pipeline._reserve_first_hand_slot(queue[2:])] == ["c", "d"]
    assert [x[0] for x in Pipeline._reserve_first_hand_slot(queue[:2])] == ["a", "b"]
    # A related-only candidate never takes the slot even if it is first-hand.
    from mic.pipeline import RELATED_ONLY_SIGNAL
    related = [("a", None, _tri("a", 100, ["target_entity_match"])),
               ("r", None, _tri("r", 90, [RELATED_ONLY_SIGNAL, "high_credibility_source"]))]
    assert [x[0] for x in Pipeline._reserve_first_hand_slot(related)] == ["a", "r"]


def test_failed_reads_do_not_consume_links_to_read_slots(config, monkeypatch):
    """Two unlucky top candidates no longer end the read stage with nothing analysed."""
    from mic.planner import PlannedQuery
    from mic.reader import ReadResult
    config.raw["call_governance"]["batching"]["serp_batch_triage"] = False
    pipe = Pipeline(config)
    monkeypatch.setattr(pipe.search, "search", lambda query, *a, **kw: [
        SearchHit(query=query, title=f"宁德时代 公告 中标 {i} 亿元", snippet="金额 1.2 亿元",
                  url=f"https://news.example.com/{i}", domain="news.example.com", rank=i,
                  query_family="orders_tender", provider="mock") for i in range(1, 5)])
    monkeypatch.setattr(pipe.planner, "plan", lambda *a, **k: [PlannedQuery("宁德时代 中标", "orders_tender", 80)])
    monkeypatch.setattr(pipe.triage, "triage", lambda hit, link_id, **kw: TriageResult(
        source_link_id=link_id, triage_decision="read", read_priority=100 - hit.rank, need_model=False))
    order: list[str] = []

    def fake_read(link_id, url, profile, context=None, strategy=None):
        order.append(url)
        if url.endswith("/1") or url.endswith("/2"):
            return ReadResult(source_link_id=link_id, read_status="failed", failure_reason="anti_bot_page")
        return ReadResult(source_link_id=link_id, read_status="read", title="t", http_status=200,
                          content_hash=url, simhash="0", passages=[], publication_time={"status": "unknown"})

    monkeypatch.setattr(pipe.reader, "read", fake_read)
    report = pipe.collect_intelligence("company_300750", {
        "focus": ["operating_update"], "time_window": "",
        "budget_profile": {"max_queries": 1, "max_links_to_read": 2, "max_model_calls": 0}})
    assert order == [f"https://news.example.com/{i}" for i in (1, 2, 3, 4)]
    assert report["summary"]["links_read"] == 2
    assert report["summary"]["links_selected_for_read"] == 4
    assert report["collection_diagnostics"]["read_status"] == "partial"


# --- model prompt ----------------------------------------------------------------------

def test_task_context_reaches_the_extraction_prompt(config, profile):
    import json
    planner = ModelCallPlanner(config, registry=None, budget=CallBudget(max_model_calls_per_run=1))
    planner.task_context = task_context(parse_task_questions({"questions": [QUESTION]}))
    messages = build_bundle_messages(profile, {"url": "u"}, [], {}, task_context=planner.task_context)
    payload = json.loads(messages[1]["content"])
    assert payload["task_context"]["questions"][0]["period"] == "2025年度"
    assert "task_context.questions" in messages[0]["content"]
    assert json.loads(build_bundle_messages(profile, {}, [], {})[1]["content"])["task_context"] is None


# --- PDF text -------------------------------------------------------------------------

PDF_LINES = [
    "宁德时代新能源科技股份有限公司 2025 年年度报告摘要",
    "（三）主要会计数据和财务指标",
    "公司是否需追溯调整或重述以前年度会计数据",
    "□是 ■否",
    "单位：千元",
    "项目 2025 年 2024 年 本年比上年增减 2023 年",
    "营业收入 423,701,834 362,012,554 17.04% 400,917,045",
    "归属于上市公司股东的",
    "净利润 72,201,282 50,744,682 42.28% 44,121,248",
    "归属于上市公司股东的",
    "扣除非经常性损益的净",
    "利润",
    "64,507,864 44,992,919 43.37% 40,091,674",
    "经营活动产生的现金流",
    "量净额 133,219,982 96,990,345 37.35% 92,826,124",
    "基本每股收益（元/股） 16.14 11.58 39.38% 10.06",
    "公司是全球领先的零碳新能源科技公司，主要从事动力电池、储能电池的研发、生产、销售。",
    "经公司董事会审议通过的 2025 年度利润分配预案为：拟以 4,531,886,650 股为基数，向全体股东每 10 股派发现金 69.57 元。",
]


def test_pdf_table_block_keeps_unit_header_wrapped_labels_and_rows_together():
    blocks = pdf_table_blocks(PDF_LINES)
    assert len(blocks) == 1, blocks
    block = blocks[0]
    for needle in ("单位：千元", "项目 2025 年 2024 年", "营业收入 423,701,834", "扣除非经常性损益的净\n利润\n64,507,864",
                   "量净额 133,219,982 96,990,345 37.35%", "基本每股收益"):
        assert needle in block
    # Prose quoting a few numbers is not a table row.
    assert "利润分配预案" not in block and "全球领先" not in block


def test_pdf_passages_carry_the_table_and_task_terms_steer_paragraphs(config, profile):
    reader = LinkReader(config, search_provider=None)
    reader.set_task_terms(["营业收入", "归母净利润", "经营活动产生的现金流量净额"])
    body = "\n".join(PDF_LINES)
    passages = reader._select_passages("2025年年度报告摘要.pdf", body, pdf_table_blocks(PDF_LINES), profile)
    tables = [p for p in passages if p.section.startswith("表格")]
    assert len(tables) == 1 and "单位：千元" in tables[0].text and "133,219,982" in tables[0].text
    assert len(passages) <= reader.max_passages
    # Table lines are not duplicated as paragraph passages.
    assert not any(p.text.startswith("营业收入 423,701,834") for p in passages if p.passage_id.startswith("p"))


def test_pdf_extraction_keeps_short_unit_lines_and_normalises_symbol_glyphs(config, monkeypatch):
    reader = LinkReader(config, search_provider=None)

    class _Page:
        def __init__(self, text):
            self._t = text

        def extract_text(self):
            return self._t

    class _Reader:
        metadata = None

        def __init__(self, _data):
            self.pages = [_Page("3\n□是 \uf052否\n单位：千元\n利润\n营业收入 423,701,834 362,012,554 17.04% 400,917,045")]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", _Reader)
    _title, _pt, body = reader._extract_pdf(b"%PDF-")
    lines = body.split("\n")
    assert "3" not in lines  # bare page number dropped
    assert "单位：千元" in lines and "利润" in lines  # short lines kept
    assert "□是 ■否" in lines and "\uf052" not in body  # private-use glyph rendered visibly
