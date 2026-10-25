"""Offline reading-order regression through real planner, review and repository.

The candidate scores and model decisions are human-authored fixtures. These
tests establish enforcement and delivery, not real-model semantic accuracy.
"""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from mic.api import AnalystAPI
from mic.content_review_policy import PROTOCOL
from mic.modeling.adapter import ModelAdapter, ModelCallResult
from mic.planner import PlannedQuery
from mic.reader import LinkReader, ReadResult
from mic.schemas import Passage, SearchHit, TriageResult
from mic.search import SearchProvider


TARGET = "company_300750"
QUESTION = "收集公司最新已披露定期报告，核对报告期、披露日期与最新性，并提取营业收入及同期。"
OLD_URL = "https://www.catl.com/annual-report.pdf"
NEW_URL = "https://reports.example/half-year-report.pdf"
TITLES = {OLD_URL: "公司 2025 年年度报告", NEW_URL: "公司 2026 年半年度报告"}
PRIORITIES = {OLD_URL: 99, NEW_URL: 60}


class _Sources(SearchProvider):
    name = "offline-task-triage"

    def __init__(self, state):
        self.state = state

    @property
    def browser_backed(self):
        return self.state.browser

    def search(self, query, query_family=None, limit=10):
        self.state.searches += 1
        return [SearchHit(
            query=query, query_family=query_family, title=TITLES[url],
            snippet="待阅读的定期报告候选；搜索摘要不是正文证据。", url=url,
            domain=url.split("/")[2], rank=i + 1, provider=self.name,
            discovery={"relevance": {
                "target_match": True, "task_match": True, "site_ok": True,
                "content_form_ok": not (self.state.gated and url == NEW_URL),
            }},
        ) for i, url in enumerate((OLD_URL, NEW_URL))]


def _bundle(payload, score):
    passage = payload["selected_passages"][0]
    text, passage_id = passage["text"], passage["passage_id"]
    is_index = passage["section"] == "index_entry"
    return {
        "decision": "save_structured", "overall_score": score, "confidence": .9,
        "facts": [{"fact_type": "document_listing" if is_index else "claim",
                   "fact_statement": text, "confidence": .9,
                   "evidence_locator": {"passage_id": passage_id}}],
        "content_review": {
            "protocol": PROTOCOL,
            "claims": [{"id": "report_content", "kind": "observation",
                        "status": "source_supported", "statement": text,
                        "reason": "人工测试响应：仅复述已读正文。",
                        "evidence": [{"passage_id": passage_id, "quote": text}],
                        "depends_on": []}],
            "bindings": {"/facts/0": ["report_content"]},
            "task_coverage": [{"question_id": "q1", "status": "partial",
                               "answer_claim_ids": ["report_content"],
                               "uncovered_aspects": ["披露日期与最新性"],
                               "reason": "本替身不提供日期或最新性证据。"}],
        },
    }


@pytest.fixture
def harness(config, monkeypatch):
    config.raw["search_providers"]["active"] = "mock"
    config.raw["call_governance"]["batching"]["serp_batch_triage"] = True
    for policy in config.model_policies["tasks"].values():
        policy.update(call_mode="single_model", models=[{"model_id": "qwen_plus", "priority": 1}])
    # The high-score candidate stays outside the legacy batch band. No tuning
    # of this range or the structured-admission threshold makes the test pass.
    assert config.model_policies["tasks"]["serp_batch_triage"]["trigger"][
        "use_model_when_rule_score_between"] == [45, 75]
    state = SimpleNamespace(searches=0, reads=[], requests=[], invalid=None,
                            overall_score=80, browser=False, gated=False, index=False)
    api = AnalystAPI(config)
    pipe = api.pipeline
    pipe.search = _Sources(state)
    pipe.reader.search_provider = pipe.search
    monkeypatch.setattr(pipe.planner, "plan", lambda *args, **kwargs: [
        PlannedQuery("公司 最新定期报告", "task_question", 100),
    ])

    def rule_triage(hit, source_link_id, **kwargs):
        return TriageResult(
            source_link_id=source_link_id, triage_decision="read",
            read_priority=PRIORITIES[hit.url], need_model=True,
            matched_signals=(["target_entity_match", "high_credibility_source"]
                             if hit.url == OLD_URL else ["target_entity_match"]),
            reason="固定的原规则评分，任务排序不改写该值。",
        )

    def read(source_link_id, url, profile, context=None, **kwargs):
        state.reads.append(url)
        context.budget.reserve("http_read_attempts")
        text = TITLES[url] + "列示营业收入。"
        return ReadResult(
            source_link_id=source_link_id, read_status="read", title=TITLES[url],
            http_status=200, content_hash="offline-report-" + url, simhash="0",
            content_length=len(text), document_type="pdf", final_url=url,
            passages=[Passage(passage_id="p1", section="正文", text=text)],
            publication_time={"status": "unknown", "published_at": None,
                              "reason": "pdf_publication_not_verified"},
        )

    def complete(adapter, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        state.requests.append(deepcopy(payload))
        if payload["task"] == "serp_batch_triage":
            rows = [{
                "id": item["id"], "triage_decision": "read", "read_priority": 1,
                "need_model": True, "task_read_rank": 1 if item["url"] == NEW_URL else 2,
                "read_intent": "index" if state.index and item["url"] == NEW_URL else "content",
                "reason": "人工模型替身：按完整问题选择先读较新报告，再核对其最新性。",
            } for item in payload["items"]]
            if state.invalid == "duplicate_rank":
                rows[0]["task_read_rank"] = rows[1]["task_read_rank"]
            elif state.invalid == "missing_id":
                rows = [row for row in rows if row["task_read_rank"] == 1]
            parsed = {"results": rows}
        else:
            assert payload["task"] == "bundle_extraction", "unexpected extra model operation"
            parsed = _bundle(payload, state.overall_score)
        return ModelCallResult(
            model_config_id=adapter.model_config_id, provider="offline",
            provider_type="stub", model_name="human-authored-task-order",
            status="success", parsed=parsed, raw_text=json.dumps(parsed, ensure_ascii=False),
            is_mock=True,
        )

    monkeypatch.setattr(pipe.triage, "triage", rule_triage)
    monkeypatch.setattr(pipe.reader, "read", read)
    monkeypatch.setattr(ModelAdapter, "complete", complete)
    return api, state


def _collect(api, *, calls=2):
    return api.collect_intelligence(TARGET, {
        "time_window": "", "questions": [QUESTION], "focus": ["financial_leading_indicator"],
        "budget_profile": {"max_queries": 1, "max_links_to_read": 1,
                           "max_search_hits": 2, "max_model_calls": calls},
    })


def _links(api, report):
    # Formal repository readback after the actual pipeline persisted rows.
    return {row["url"]: row for row in api.repo.source_links_for_run(report["search_run_id"])}


def test_complete_task_ranking_reads_new_report_before_old_first_hand_source(harness):
    api, state = harness
    report = _collect(api)
    assert [request["task"] for request in state.requests] == ["serp_batch_triage", "bundle_extraction"]
    triage, extract = state.requests
    assert triage["task_context"]["questions"][0]["question"] == QUESTION
    items = {item["url"]: item for item in triage["items"]}
    assert set(items) == {OLD_URL, NEW_URL}
    assert {url: item["rule_priority"] for url, item in items.items()} == PRIORITIES
    assert all(item["domain"] == url.split("/")[2] for url, item in items.items())
    assert state.reads == [NEW_URL]
    assert extract["source_metadata"]["url"] == NEW_URL
    assert report["summary"]["model_calls"] == 2
    assert report["summary"]["batch_triage_calls"] == 1
    assert report["summary"]["gateway_requests_sent"] == 0
    assert [fact["fact_statement"] for fact in api.search_facts(TARGET)] == [TITLES[NEW_URL] + "列示营业收入。"]

    links = _links(api, report)
    assert {url: row["triage_score"] for url, row in links.items()} == PRIORITIES
    assert "high_credibility_source" in links[OLD_URL]["metadata"]["matched_signals"]
    assert links[OLD_URL]["metadata"]["task_triage"]["task_read_rank"] == 2
    assert links[NEW_URL]["metadata"]["task_triage"]["task_read_rank"] == 1
    for row in links.values():
        assert set(row["metadata"]["task_triage"]["compared_source_ids"]) == {
            link["source_link_id"] for link in links.values()
        }
    assert links[OLD_URL]["read_status"] == "pending"
    review, = report["task_triage_reviews"]
    assert review["status"] == "accepted"
    assert set(review["input_ids"]) == {row["source_link_id"] for row in links.values()}
    assert len(review["raw"]["results"]) == 2
    assert report["collection_diagnostics"]["output_decisions"][0]["min_overall_score"] == 70


def test_single_model_slot_is_reserved_for_extraction_without_triage(harness):
    api, state = harness
    report = _collect(api, calls=1)
    assert [request["task"] for request in state.requests] == ["bundle_extraction"]
    assert state.reads == [OLD_URL]
    assert report["summary"]["model_calls"] == 1
    assert report["summary"]["batch_triage_calls"] == 0
    assert report["task_triage_reviews"] == []
    assert all("task_triage" not in row["metadata"] for row in _links(api, report).values())


@pytest.mark.parametrize("invalid", ["duplicate_rank", "missing_id"])
def test_invalid_task_ranking_falls_back_whole_batch_without_retry(harness, invalid):
    api, state = harness
    state.invalid = invalid
    report = _collect(api)
    assert [request["task"] for request in state.requests] == ["serp_batch_triage", "bundle_extraction"]
    assert state.reads == [OLD_URL]
    assert report["summary"]["model_calls"] == 2
    assert report["summary"]["batch_triage_calls"] == 1
    review, = report["task_triage_reviews"]
    assert review["status"] == "rejected" and review["errors"]
    assert all("task_triage" not in row["metadata"] for row in _links(api, report).values())
    assert {url: row["triage_score"] for url, row in _links(api, report).items()} == PRIORITIES


def test_browser_read_gate_still_rejects_model_top_ranked_candidate(harness):
    from tests.browser_doubles import browser_runtime_block

    api, state = harness
    state.browser = state.gated = True
    api.pipeline.config.set_browser_runtime(browser_runtime_block())
    report = _collect(api)
    assert report["collection_diagnostics"]["browser_run"] is True
    assert state.reads == [OLD_URL]
    assert [request["task"] for request in state.requests] == ["serp_batch_triage", "bundle_extraction"]
    assert report["task_triage_reviews"][0]["status"] == "accepted"
    candidate = _links(api, report)[NEW_URL]
    assert candidate["metadata"]["task_triage"]["task_read_rank"] == 1
    assert candidate["triage_decision"] == "link_record_only"
    assert candidate["metadata"]["need_model"] is False
    assert report["summary"]["read_gate_demoted"] == {"content_form": 1}
    assert candidate["triage_score"] == 60


def test_explicit_model_index_intent_reads_entries_once_and_persists_reviewed_listing(harness, monkeypatch):
    from tests.browser_doubles import browser_runtime_block

    api, state = harness
    state.browser = state.gated = state.index = True
    pipe = api.pipeline
    pipe.config.set_browser_runtime(browser_runtime_block())
    html = ('<html><head><title>公司文件目录</title></head><body><main><ul>'
            '<li><a href="/half.pdf">公司半年度报告</a><time>2026-08-18</time></li>'
            '<li><a href="/annual.pdf">公司年度报告</a><time>2026-04-16</time></li>'
            '</ul><nav class="pagination"><a rel="next" href="?page=2">下一页</a></nav>'
            '</main></body></html>')
    page_requests = []

    def page_body(url):
        page_requests.append(url)
        assert url == NEW_URL, "a listing must not fetch its entry or pagination links"
        return html

    def real_read(source_id, url, profile, **kwargs):
        state.reads.append(url)
        return LinkReader.read(pipe.reader, source_id, url, profile, **kwargs)

    monkeypatch.setattr(pipe.search, "page_body", page_body)
    monkeypatch.setattr(pipe.reader, "read", real_read)
    report = _collect(api)
    assert state.reads == page_requests == [NEW_URL]
    assert [request["task"] for request in state.requests] == ["serp_batch_triage", "bundle_extraction"]
    extract = state.requests[1]
    material = extract["source_metadata"]["material_scope"]
    assert material["kind"] == "index" and material["selected_entries"] == 2
    assert material["pagination"]["detected"] is True
    assert material["completeness"] == material["ordering"] == "unknown"
    assert material["entries"][0]["links"][0]["url"] == "https://reports.example/half.pdf"
    assert all(p["section"] == "index_entry" for p in extract["selected_passages"])
    assert extract["source_metadata"]["publication_time"]["status"] == "unknown"
    assert extract["source_metadata"]["publish_time"] is None
    assert report["summary"]["model_calls"] == 2
    assert report["summary"]["http_read_attempts"] == 1
    assert report["summary"]["browser_read_attempts"] == 0
    facts = api.search_facts(TARGET)
    assert len(facts) == 1 and facts[0]["fact_type"] == "document_listing"
    assert facts[0]["fact_statement"] == "公司半年度报告 2026-08-18"
    candidate = _links(api, report)[NEW_URL]
    assert candidate["read_status"] == "read"
    assert candidate["metadata"]["task_triage"]["read_intent"] == "index"


@pytest.mark.parametrize(("score", "published"), [(69, False), (70, True)])
def test_task_read_rank_never_lowers_structured_admission_threshold(harness, score, published):
    api, state = harness
    state.overall_score = score
    report = _collect(api)
    assert state.reads == [NEW_URL]
    decision, = report["collection_diagnostics"]["output_decisions"]
    assert decision["min_overall_score"] == 70
    assert decision["overall_score"] == score
    assert decision["decision_after"] == ("save_structured" if published else "link_only")
    assert bool(api.search_facts(TARGET)) is published
    assert report["summary"]["model_calls"] == 2


def _queue_candidate(source_id, score, *, rank=None, compared=None,
                     first_hand=False, related=False, task_priority=None):
    from mic.pipeline import RELATED_ONLY_SIGNAL

    signals = ["target_entity_match"]
    if first_hand:
        signals.append("high_credibility_source")
    if related:
        signals.append(RELATED_ONLY_SIGNAL)
    review = {}
    if compared is not None:
        review = {"protocol": "task_triage_v1", "status": "accepted",
                  "basis": "search_result_navigation_only", "source_link_id": source_id,
                  "task_read_rank": rank, "compared_source_ids": list(compared),
                  "reason": "人工排序审核仅比较本批输入。"}
        if task_priority is not None:
            review["task_read_priority"] = task_priority
    hit = SearchHit(query="offline", title=source_id, snippet="", rank=1,
                    url=f"https://example.invalid/{source_id}", domain="example.invalid",
                    provider="offline")
    triage = TriageResult(source_link_id=source_id, triage_decision="read",
                          read_priority=score, need_model=True, matched_signals=signals,
                          task_read_rank=rank, task_read_priority=task_priority,
                          task_review=review)
    return source_id, hit, triage


def _ordered_ids(candidates):
    from mic.pipeline import Pipeline

    return [source_id for source_id, _, _ in Pipeline._order_read_queue(candidates)]


def test_unassessed_official_candidate_keeps_reserved_slot_ahead_of_ranked_batch():
    # The budget reviewed only two media candidates. The unreviewed official
    # source must not be moved to the tail merely because it lacks a model rank.
    batch = ["media-old", "media-new"]
    candidates = [
        _queue_candidate("media-old", 99, rank=2, compared=batch),
        _queue_candidate("official-unassessed", 95, first_hand=True),
        _queue_candidate("media-new", 60, rank=1, compared=batch),
    ]
    assert _ordered_ids(candidates) == ["official-unassessed", "media-new", "media-old"]


def test_batch_reorders_its_own_positions_without_moving_unassessed_middle_candidate():
    batch = ["official-old", "report-new"]
    candidates = [
        _queue_candidate("official-old", 99, rank=2, compared=batch, first_hand=True),
        _queue_candidate("unassessed-middle", 80),
        _queue_candidate("report-new", 60, rank=1, compared=batch),
    ]
    assert _ordered_ids(candidates) == ["report-new", "unassessed-middle", "official-old"]


def test_two_batches_do_not_imply_cross_batch_semantic_comparison():
    batch_a, batch_b = ["a-old", "a-middle", "a-new"], ["b-old", "b-new"]
    candidates = [
        _queue_candidate("a-old", 99, rank=3, compared=batch_a),
        _queue_candidate("a-middle", 95, rank=2, compared=batch_a),
        _queue_candidate("b-old", 90, rank=2, compared=batch_b),
        _queue_candidate("a-new", 80, rank=1, compared=batch_a),
        _queue_candidate("b-new", 70, rank=1, compared=batch_b),
    ]
    # A occupies baseline positions 0/1/3; B occupies 2/4. Equal local ranks
    # cannot group both rank-1 entries before both rank-2 entries globally.
    assert _ordered_ids(candidates) == ["a-new", "a-middle", "b-new", "a-old", "b-old"]
    assert [tri.task_read_rank for _, _, tri in candidates] == [3, 2, 2, 1, 1]


def test_batch_ranking_cannot_move_related_only_source_ahead_of_target_sources():
    batch = ["target-a", "related", "target-b"]
    candidates = [
        _queue_candidate("target-a", 99, rank=2, compared=batch),
        _queue_candidate("related", 95, rank=1, compared=batch, related=True),
        _queue_candidate("target-b", 80, rank=3, compared=batch),
    ]
    assert _ordered_ids(candidates) == ["target-a", "target-b", "related"]


def test_filtered_batch_member_does_not_erase_valid_order_of_remaining_readable_sources():
    # The shared gate removed one of the model-reviewed candidates. Its id
    # remains in the comparison audit, while only allowed candidates are queued.
    batch = ["old", "blocked", "new"]
    candidates = [
        _queue_candidate("old", 99, rank=3, compared=batch, first_hand=True),
        _queue_candidate("unassessed", 80),
        _queue_candidate("new", 60, rank=1, compared=batch),
    ]
    assert _ordered_ids(candidates) == ["new", "unassessed", "old"]


def test_model_priority_crosses_batch_positions_without_reusing_local_ranks_globally():
    batch_a, batch_b = ["a1", "a2"], ["b1", "b2"]
    candidates = [
        _queue_candidate("a1", 99, rank=1, compared=batch_a, task_priority=80),
        _queue_candidate("a2", 98, rank=2, compared=batch_a, task_priority=70),
        _queue_candidate("b1", 40, rank=1, compared=batch_b, task_priority=95),
        _queue_candidate("b2", 30, rank=2, compared=batch_b, task_priority=90),
    ]
    assert _ordered_ids(candidates) == ["b1", "b2", "a1", "a2"]
    assert [tri.read_priority for _, _, tri in candidates] == [99, 98, 40, 30]


def test_cross_batch_priority_keeps_unreviewed_first_hand_and_middle_positions():
    a, b = ["a"], ["b"]
    candidates = [
        _queue_candidate("a", 99, rank=1, compared=a, task_priority=20),
        _queue_candidate("official-unassessed", 95, first_hand=True),
        _queue_candidate("unassessed-middle", 80),
        _queue_candidate("b", 60, rank=1, compared=b, task_priority=90),
    ]
    assert _ordered_ids(candidates) == ["official-unassessed", "b", "unassessed-middle", "a"]


def test_same_priority_preserves_local_rank_and_never_compares_ranks_across_batches():
    a, b = ["a1", "a2"], ["b1", "b2"]
    candidates = [
        _queue_candidate("a2", 99, rank=2, compared=a, task_priority=80),
        _queue_candidate("a1", 98, rank=1, compared=a, task_priority=80),
        _queue_candidate("b2", 40, rank=2, compared=b, task_priority=80),
        _queue_candidate("b1", 30, rank=1, compared=b, task_priority=80),
    ]
    assert _ordered_ids(candidates) == ["a1", "a2", "b1", "b2"]


def test_unbound_or_related_priority_cannot_override_target_navigation():
    candidates = [
        _queue_candidate("target", 90, rank=1, compared=["target"], task_priority=10),
        _queue_candidate("unbound", 70, rank=1, compared=["unbound"], task_priority=100),
        _queue_candidate("related", 99, rank=1, compared=["related"],
                         task_priority=100, related=True),
    ]
    candidates[1][2].task_review["source_link_id"] = "other"
    assert _ordered_ids(candidates) == ["target", "unbound", "related"]
