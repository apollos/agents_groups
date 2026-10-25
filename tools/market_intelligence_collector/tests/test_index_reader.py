"""Synthetic index material contracts, with no remote pages/model calls."""
from copy import deepcopy
import json

import pytest

from mic.browser.contracts import FetchResult
from mic.config import MICConfig
from mic.pipeline import Pipeline, RunStats
from mic.profile import TargetProfile
from mic.reader import LinkReader
from mic.schemas import SearchHit, TriageResult
from mic.task_triage import validate_task_triage
from tests.browser_doubles import FakeBrowserSession, make_context


URL = "https://issuer.example/information/index.html"
PROFILE = TargetProfile(target_id="test", type="company", canonical_name="示例公司")
HTML = ('<html><head><title>文件目录</title>'
        '<meta property="article:published_time" content="2026-10-01"></head><body>'
        '<main><ul><li><a href="../report-a.pdf">示例公司中期报告</a> <time>2026-08-16</time></li>'
        '<li><a href="../report-b.pdf">示例公司年度报告</a> <time>2026-04-19</time></li>'
        '<li><a href="../report-c.pdf">示例公司前次报告</a> <time>2025-08-18</time></li></ul>'
        '<nav class="pagination"><span aria-current="page">1</span>'
        '<a rel="next" href="?page=2">2</a></nav></main></body></html>')


def _review(source_id="l1"):
    return validate_task_triage([{"id": source_id}], {"results": [{
        "id": source_id, "triage_decision": "read", "read_priority": 80,
        "need_model": True, "read_intent": "index", "task_read_rank": 1,
        "reason": "人工模型替身请求读取目录以核对列示范围，未批准事实或最新性。",
    }]})[source_id]["task_review"]


def _setup(html=HTML, *, limits=None, blocked=None, max_chars=None):
    config = MICConfig(raw={
        "output_schema": {"limits": {"strict_evidence_review": True}},
        "access_profiles": {"browser_fetch": {"default_mode": "http_then_browser"}},
    })
    if max_chars is not None:
        config.raw["call_governance"] = {"budgets": {"max_input_chars_per_model_call": max_chars}}
    reader = LinkReader(config)
    calls = []

    def fetch(url):
        calls.append(url)
        return FetchResult(transport="http", requested_url=url, final_url=url,
                           http_status=200, content_type="text/html", html=html,
                           blocked_reason=blocked, counted_as="http_read_attempts")

    reader._fetch_http_result = fetch
    context = make_context(limits=limits)
    browser = FakeBrowserSession(pages={URL: HTML})
    context.set_browser_factory(lambda _: browser)
    return reader, context, browser, calls


def _read(reader, context, **kwargs):
    return reader.read("l1", URL, PROFILE, context=context,
                       read_intent="index", intent_review=_review(), **kwargs)


def _material_chars(result):
    return (len(json.dumps(result.material_scope, ensure_ascii=False))
            + len(json.dumps([p.model_dump() for p in result.passages], ensure_ascii=False))
            + len(json.dumps({"title": result.title}, ensure_ascii=False)))


def test_index_keeps_entry_date_link_and_scope_without_inventing_page_publication():
    reader, context, browser, calls = _setup()
    result = _read(reader, context)
    assert result.read_status == "read"
    assert result.publish_time is None and result.publication_time["status"] == "unknown"
    assert [p.text for p in result.passages] == [
        "示例公司中期报告 2026-08-16", "示例公司年度报告 2026-04-19", "示例公司前次报告 2025-08-18",
    ]
    assert {p.section for p in result.passages} == {"index_entry"}
    material = result.material_scope
    assert material["entries"][0]["links"] == [
        {"text": "示例公司中期报告", "url": "https://issuer.example/report-a.pdf"},
    ]
    assert material["entries"][0]["locator"]
    assert material["pagination"]["detected"] is True
    assert material["pagination"]["links"][0]["url"] == URL + "?page=2"
    assert material["ordering"] == material["completeness"] == "unknown"
    assert material["followed_links"] == 0 and not material["truncated"]
    assert calls == [URL] and browser.navigations == []
    assert context.budget.used_summary()["http_read_attempts"] == 1
    assert context.budget.used_summary()["browser_read_attempts"] == 0
    assert result.fetch_diagnostics["parser_version"] == "index_scope_v1"
    assert "entries" not in Pipeline._read_diagnostics(result)["body_scope"]


def test_default_content_path_still_rejects_link_directory():
    reader, context, browser, calls = _setup()
    result = reader.read("l1", URL, PROFILE, context=context)
    assert result.read_status == "failed" and result.failure_reason == "article_scope_unresolved"
    assert result.material_scope == {} and result.passages == []
    assert calls == [URL] and browser.navigations == []


@pytest.mark.parametrize("review", [None, {}, _review("other"),
                                    {**_review(), "status": "rejected"},
                                    {**_review(), "need_model": False}])
def test_unapproved_index_intent_is_rejected_before_fetch(review):
    reader, context, browser, calls = _setup()
    result = reader.read("l1", URL, PROFILE, context=context,
                         read_intent="index", intent_review=review)
    assert result.read_status == "failed" and result.failure_reason == "unapproved_read_intent"
    assert calls == browser.navigations == []
    assert context.budget.used_summary()["http_read_attempts"] == 0


def test_index_path_preserves_browser_rescue_and_attempt_budgets():
    captcha = '<html><head><title>安全验证</title></head><body>请输入验证码</body></html>'
    reader, context, browser, calls = _setup(captcha)
    result = _read(reader, context)
    assert result.read_status == "read" and result.transport == "browser"
    assert result.passages[0].section == "index_entry"
    assert calls == browser.navigations == [URL]
    used = context.budget.used_summary()
    assert (used["http_read_attempts"], used["browser_read_attempts"]) == (1, 1)


def test_index_cannot_bypass_captcha_or_exhausted_browser_budget():
    reader, context, browser, calls = _setup(HTML, blocked="captcha", limits={"max_browser_read_attempts": 0})
    result = _read(reader, context)
    assert result.read_status == "failed" and result.failure_reason == "captcha"
    assert not result.passages and browser.navigations == [] and calls == [URL]


def test_unscoped_index_does_not_trigger_extra_browser_or_article_fallback():
    reader, context, browser, calls = _setup('<article><p>只有一段普通文本，没有目录。</p></article>')
    result = _read(reader, context)
    assert result.read_status == "failed" and result.failure_reason == "index_scope_unresolved"
    assert browser.navigations == [] and calls == [URL]


@pytest.mark.parametrize("budget", ["passages", "characters"])
def test_index_limits_select_whole_prefix_entries_and_mark_incomplete(budget):
    reader, context, browser, calls = _setup()
    if budget == "passages":
        reader.max_passages = 1
    else:
        baseline_reader, baseline_context, _, _ = _setup()
        baseline_reader.max_passages = 1
        baseline = _read(baseline_reader, baseline_context)
        reader.max_chars = _material_chars(baseline) + 16
    result = _read(reader, context)
    assert result.read_status == "read"
    assert [p.text for p in result.passages] == ["示例公司中期报告 2026-08-16"]
    assert result.material_scope["truncated"] is True
    assert result.material_scope["completeness"] == "incomplete"
    assert len(result.material_scope["entries"]) == 1
    assert result.material_scope["entries"][0]["text"] == result.passages[0].text
    assert calls == [URL] and browser.navigations == []
    assert _material_chars(result) <= reader.max_chars


def test_reader_budgets_entry_metadata_as_well_as_short_passage_text():
    rows = ''.join('<li><a href="/' + 'x' * 1500 + f'/{i}.pdf">短标题附件 {i}</a>'
                   '<time>2026-10-01</time></li>' for i in range(8))
    # Keep this fixture at its explicit small budget to exercise truncation;
    # production defaults may allow all eight entries without truncation.
    reader, context, browser, calls = _setup('<ul>' + rows + '</ul>', max_chars=8000)
    result = _read(reader, context)
    assert result.read_status == "read" and 1 <= len(result.passages) < 8
    assert "max_input_chars_per_model_call" in result.material_scope["truncation_reasons"]
    assert result.material_scope["completeness"] == "incomplete"
    assert _material_chars(result) <= reader.max_chars
    assert calls == [URL] and browser.navigations == []


def test_index_title_shares_the_8000_character_budget_without_being_cut():
    rows = ''.join('<li><a href="/' + 'x' * 1500 + f'/{i}.pdf">短标题附件 {i}</a>'
                   '<time>2026-10-01</time></li>' for i in range(8))
    baseline_reader, baseline_context, _, _ = _setup('<ul>' + rows + '</ul>', max_chars=8000)
    baseline = _read(baseline_reader, baseline_context)
    title = '目录名称' * 450  # Below the parser's separate 2000-char title bound.
    reader, context, browser, calls = _setup(
        '<title>' + title + '</title><ul>' + rows + '</ul>', max_chars=8000)
    result = _read(reader, context)
    assert reader.max_chars == 8000
    assert result.read_status == 'read'
    assert 0 < len(result.passages) < len(baseline.passages)
    assert result.title == title
    assert _material_chars(result) <= 8000
    assert result.material_scope['truncated'] is True
    assert 'max_input_chars_per_model_call' in result.material_scope['truncation_reasons']
    for i, entry in enumerate(result.material_scope['entries']):
        assert entry['links'][0]['url'] == 'https://issuer.example/' + 'x' * 1500 + f'/{i}.pdf'
    assert calls == [URL] and browser.navigations == []


def test_title_plus_first_complete_entry_over_budget_rejects_material_read():
    baseline_reader, baseline_context, _, _ = _setup()
    baseline_reader.max_passages = 1
    baseline = _read(baseline_reader, baseline_context)
    title = '目录名称' * 450
    reader, context, browser, calls = _setup(HTML.replace('文件目录', title))
    reader.max_chars = _material_chars(baseline) + 128
    result = _read(reader, context)
    assert result.read_status == 'failed' and result.failure_reason == 'index_scope_unresolved'
    assert result.title == title
    assert result.passages == [] and result.material_scope == {}
    assert result.body_scope['truncated'] is True
    assert 'max_input_chars_per_model_call' in result.body_scope['truncation_reasons']
    assert result.publication_time['status'] == 'unknown'
    assert calls == [URL] and browser.navigations == []


def test_directory_hash_distinguishes_identical_titles_with_different_document_destinations():
    first, first_context, _, _ = _setup()
    second, second_context, _, _ = _setup(HTML.replace('report-a.pdf', 'revision-a.pdf'))
    original = _read(first, first_context)
    revised = _read(second, second_context)
    assert [p.text for p in original.passages] == [p.text for p in revised.passages]
    assert original.content_hash != revised.content_hash


@pytest.mark.parametrize("override,expected", [
    ({}, "read"), ({"site_ok": False}, "link_record_only"),
    ({"target_match": False}, "link_record_only"),
])
def test_index_intent_only_relaxes_content_form_gate(override, expected):
    relevance = {"site_ok": True, "target_match": True, "content_form_ok": False, **override}
    hit = SearchHit(query="offline", title="文件目录", snippet="", url=URL,
                    domain="issuer.example", rank=1, provider="offline",
                    discovery={"relevance": relevance})
    triage = TriageResult(source_link_id="l1", triage_decision="read", need_model=True,
                          read_intent="index", task_review=_review())
    gated = Pipeline._apply_read_gate(hit, triage, RunStats())
    assert gated.triage_decision == expected
    if not override:
        wrong_source = triage.model_copy(update={"task_review": deepcopy(_review("other"))})
        assert Pipeline._apply_read_gate(hit, wrong_source, RunStats()).triage_decision == "link_record_only"
