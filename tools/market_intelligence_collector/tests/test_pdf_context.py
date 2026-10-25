"""PDF selection preserves positional context without certifying its meaning.

All text and model-review labels below are synthetic; no PDF, model or network
request is needed. They verify transport and review, not model comprehension.
"""

from types import SimpleNamespace

import pytest

from mic.content_review_policy import PROTOCOL
from mic.profile import TargetProfile
from mic.reader import LinkReader, pdf_table_blocks
from mic.validate import BundleValidator


@pytest.fixture
def profile():
    return TargetProfile(target_id="example", type="company", canonical_name="样例公司")


def long_body():
    start = "本材料的编制范围与期间说明。\n财务报告期为2026年1月1日至2026年6月30日。\n签署日期与公开披露日期分别列示。"
    middle = "\n".join(f"样例公司第{i}项业务订单金额100万元，相关安排需结合完整说明。" for i in range(100))
    end = "本材料于2026年8月25日经董事会批准。\n签署人：样例公司董事会。\n签署日期：2026年8月26日。\n本材料公开披露日期为2026年8月28日。"
    return start, middle, end


def test_pdf_keeps_adjacent_date_roles_and_extracted_range_labels(config, profile, monkeypatch):
    reader = LinkReader(config)
    reader.max_chars = 1800
    start, middle, end = long_body()
    body = "\n".join((start, middle, end))
    monkeypatch.setattr(reader, "_extract_pdf", lambda raw: ("报告元数据标题", None, body))
    result = reader._parse_raw("pdf_source", b"%PDF-stub", 200, "application/pdf", profile)
    passages = {p.passage_id: p for p in result.passages}
    assert start in passages["pdf_start"].text
    assert end in passages["pdf_end"].text
    assert passages["pdf_start"].text in body and passages["pdf_end"].text in body
    assert "已抽取范围" in passages["pdf_end"].section
    assert "不代表完整文档末页" in passages["pdf_end"].section
    assert result.publish_time is None
    assert result.publication_time == {
        "status": "unknown", "published_at": None, "reason": "pdf_publication_not_verified"}
    assert sum(len(p.text) for p in result.passages) <= reader.max_chars
    assert len(result.passages) <= reader.max_passages


@pytest.mark.parametrize("max_chars,max_passages", [(0, 0), (40, 1), (80, 2), (400, 3), (1700, 8)])
def test_pdf_context_obeys_total_character_and_passage_limits(config, profile, max_chars, max_passages):
    reader = LinkReader(config)
    reader.max_chars, reader.max_passages = max_chars, max_passages
    body = "\n".join(long_body())
    passages = reader._select_passages("很长的元数据标题" * 1000, body, [], profile,
                                       preserve_document_edges=True)
    assert sum(len(p.text) for p in passages) <= max_chars
    assert len(passages) <= max_passages
    assert all(p.text for p in passages)


def test_table_unit_headers_and_wrapped_labels_keep_the_original_allocation(config, profile):
    reader = LinkReader(config).set_task_terms(["营业收入"])
    reader.max_chars = 1500
    table_lines = ["单位：千元", "项目 2026年 2025年", "营业收入 123,456 100,000",
                   "归属于上市公司股东的", "净利润 3,456 1,000"]
    # Long prose on either side also tests that PDF block reconstruction does
    # not absorb the date context into the table.
    start, middle, end = long_body()
    body = "\n".join((start, middle, *table_lines, middle, end))
    tables = pdf_table_blocks(body.split("\n"))
    baseline = reader._select_passages("报告", body, tables, profile)
    passages = reader._select_passages("报告", body, tables, profile, preserve_document_edges=True)
    old_tables = [(p.passage_id, p.text) for p in baseline if p.section.startswith("表格")]
    new_tables = [(p.passage_id, p.text) for p in passages if p.section.startswith("表格")]
    assert new_tables == old_tables
    assert new_tables == [("t1", "\n".join(table_lines))]
    assert end in next(p.text for p in passages if p.passage_id == "pdf_end")
    reader.max_chars = len(tables[0])
    tight = reader._select_passages("报告", body, tables, profile, preserve_document_edges=True)
    assert [(p.passage_id, p.text) for p in tight] == new_tables


def test_edge_window_does_not_make_a_new_partial_copy_of_a_table(config, profile):
    reader = LinkReader(config).set_task_terms(["营业收入"])
    reader.max_chars = 2600
    # A block deliberately longer than the edge window, but within the existing
    # table budget: neither the prefix nor suffix may split its rows or unit.
    table = "单位：千元\n项目 本期 上期\n" + "\n".join(
        f"营业收入第{i}项 12345678901234567890 98765432109876543210" for i in range(15))
    for body in (table + "\n" + long_body()[1], long_body()[1] + "\n" + table):
        passages = reader._select_passages("", body, [table], profile, preserve_document_edges=True)
        assert next(p.text for p in passages if p.passage_id == "t1") == table
        assert not any("营业收入第" in p.text for p in passages if p.passage_id.startswith("pdf_"))
        assert sum(len(p.text) for p in passages) <= reader.max_chars


def test_metadata_title_does_not_become_body_evidence(config, profile):
    reader = LinkReader(config)
    title = "仅在元数据中的披露日期2026年9月1日"
    passages = reader._select_passages(title, "\n".join(long_body()), [], profile,
                                       preserve_document_edges=True)
    assert not any(title in p.text for p in passages if p.passage_id != "title")
    raw = {"facts": [{"fact_statement": title}], "content_review": {
        "protocol": PROTOCOL, "claims": [{"id": "title_claim", "statement": title,
        "kind": "observation", "status": "source_supported", "reason": "人工测试标签",
        "evidence": [{"passage_id": "title", "quote": title}], "depends_on": []}],
        "bindings": {"/facts/0": ["title_claim"]}}}
    review = BundleValidator({"strict_evidence_review": True}).validate(raw, passages)
    assert not review.bundle.facts
    assert review.bundle.content_review["claims"]["title_claim"] == {
        "status": "pending_review", "reason": "citation_missing_or_unverified",
        "dependency_status": "pending_review"}


def test_pdf_still_extracts_only_first_twenty_pages(config, profile, monkeypatch):
    import pypdf

    extracted = []

    class Page:
        def __init__(self, number):
            self.number = number

        def extract_text(self):
            extracted.append(self.number)
            return f"正文第{self.number}页内容。\n" + "此页是当前抽取范围内的一页。" * 5

    monkeypatch.setattr(pypdf, "PdfReader", lambda raw: SimpleNamespace(
        metadata=None, pages=[Page(i) for i in range(1, 26)]))
    result = LinkReader(config)._parse_raw("pdf_source", b"%PDF-stub", 200, "application/pdf", profile)
    assert extracted == list(range(1, 21))
    ending = next(p for p in result.passages if p.passage_id == "pdf_end")
    assert "正文第20页内容" in ending.text and "正文第25页内容" not in ending.text
    assert "不代表完整文档末页" in ending.section


def test_html_selection_remains_opt_in_and_unchanged(config, profile, monkeypatch):
    reader = LinkReader(config)
    body = "\n".join(long_body())
    expected = reader._select_passages("网页标题", body, [], profile)
    monkeypatch.setattr(reader, "_extract", lambda raw: ("网页标题", None, body, [], []))
    config.output_schema.setdefault("limits", {})["strict_evidence_review"] = False
    result = reader._parse_raw("html_source", "<html></html>", 200, "text/html", profile)
    assert result.passages == expected
    assert all(not p.passage_id.startswith("pdf_") for p in result.passages)
