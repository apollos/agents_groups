"""Offline model stubs prove task ranking enforcement, not model understanding."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from mic.modeling.adapter import ModelCallResult
from mic.modeling.call_planner import CallBudget, ModelCallPlanner
from mic.modeling.prompts import build_batch_triage_messages
from mic.task_triage import index_read_authorized, validate_task_triage


ITEMS = [
    {"id": "old", "title": "甲公司 2025 年年度报告", "snippet": "营业收入与利润",
     "url": "https://company.example/annual.pdf", "domain": "company.example",
     "rule_priority": 99},
    {"id": "new", "title": "甲公司 2026 年半年度报告", "snippet": "半年度报告原文",
     "url": "https://disclosure.example/half.pdf", "domain": "disclosure.example",
     "rule_priority": 60},
]
CONTEXT = {
    "questions": [{"question_id": "q1", "question": "截至运行时最新已披露定期报告及三指标",
                   "period": None}],
    "instructions": "保留原文证据和缺口",
}


def _response():
    # Human-authored expected model response. The program must not derive this
    # order by inspecting years, company names, or score differences itself.
    return {"results": [
        {"id": "old", "triage_decision": "read", "read_priority": 80,
         "need_model": True, "task_read_rank": 2,
         "reason": "该资料较旧，保留作为比较来源，不能确认其最新性。"},
        {"id": "new", "triage_decision": "read", "read_priority": 90,
         "need_model": True, "task_read_rank": 1,
         "reason": "较新的定期报告原文候选，优先读取并核对披露依据。"},
    ]}


def test_full_question_and_source_navigation_context_reach_same_batch_prompt():
    messages = build_batch_triage_messages(deepcopy(ITEMS), deepcopy(CONTEXT))
    payload = json.loads(messages[1]["content"])
    assert payload["task_context"] == CONTEXT
    assert payload["items"] == ITEMS
    assert payload["output_schema_hint"]["results"][0]["task_read_rank"] == 1
    assert payload["output_schema_hint"]["results"][0]["read_intent"] == "content|index"
    assert "不是已审核的事实证据" in messages[0]["content"]
    assert "报告期已结束" in messages[0]["content"]


def test_valid_complete_ranking_retains_semantics_as_audit_only():
    raw, review = _response(), {}
    actual = validate_task_triage(ITEMS, raw, review=review)
    assert sorted(actual, key=lambda key: actual[key]["task_read_rank"]) == ["new", "old"]
    assert actual["new"]["task_review"]["basis"] == "search_result_navigation_only"
    assert actual["new"]["task_review"]["source_link_id"] == "new"
    assert review["status"] == "accepted" and review["errors"] == []
    assert review["raw"] == raw
    assert actual["new"]["read_intent"] == "content"
    assert actual["new"]["task_read_priority"] == 90
    assert actual["new"]["task_review"]["task_read_priority"] == 90
    assert not index_read_authorized("new", "index", actual["new"]["task_review"])
    raw["results"][0]["reason"] = "changed afterwards"
    assert review["raw"]["results"][0]["reason"] != "changed afterwards"


@pytest.mark.parametrize(("field", "value"), [
    ("id", "unknown"), ("id", 1), ("id", []),
    ("triage_decision", "approve"), ("triage_decision", {}),
    ("read_priority", "99"), ("read_priority", True),
    ("read_priority", float("nan")), ("read_priority", float("inf")),
    ("read_priority", -1), ("read_priority", 101), ("read_priority", 10 ** 400),
    ("need_model", "false"), ("need_model", 0), ("need_model", None),
    ("task_read_rank", 0), ("task_read_rank", 3), ("task_read_rank", True),
    ("task_read_rank", 1.0), ("task_read_rank", "1"),
    ("reason", ""), ("reason", " "), ("reason", None),
    ("read_intent", "directory"), ("read_intent", None), ("read_intent", True),
])
def test_invalid_single_result_rejects_entire_ranking(field, value):
    raw, review = _response(), {}
    raw["results"][0][field] = value
    assert validate_task_triage(ITEMS, raw, review=review) == {}
    assert review["status"] == "rejected" and review["errors"]
    # The valid second candidate must not escape as a partial ranking.
    assert review["raw"]["results"][1]["id"] == "new"


@pytest.mark.parametrize("mutation", ["duplicate_id", "duplicate_rank", "missing_id",
                                      "extra_id", "not_object", "not_list", "old_protocol"])
def test_batch_must_bind_every_input_once_with_complete_unique_ranks(mutation):
    raw, review = _response(), {}
    if mutation == "duplicate_id":
        raw["results"][1]["id"] = "old"
    elif mutation == "duplicate_rank":
        raw["results"][1]["task_read_rank"] = 2
    elif mutation == "missing_id":
        raw["results"].pop()
    elif mutation == "extra_id":
        raw["results"].append({**raw["results"][0], "id": "invented"})
    elif mutation == "not_object":
        raw["results"][0] = None
    elif mutation == "not_list":
        raw["results"] = {}
    else:
        for row in raw["results"]:
            row.pop("task_read_rank")
            row.pop("reason")
    assert validate_task_triage(ITEMS, raw, review=review) == {}
    assert review["status"] == "rejected"


def test_no_year_or_company_heuristic_overrides_the_models_semantic_order():
    raw = _response()
    raw["results"][0]["task_read_rank"] = 1
    raw["results"][0]["read_priority"] = 95
    raw["results"][0]["reason"] = "用户需要旧报告的历史修订信息。"
    raw["results"][1]["task_read_rank"] = 2
    actual = validate_task_triage(ITEMS, raw)
    assert actual["old"]["task_read_rank"] == 1


def test_conflicting_priority_and_rank_rejects_whole_batch_without_guessing():
    raw, review = _response(), {}
    raw["results"][0]["read_priority"] = 95
    assert validate_task_triage(ITEMS, raw, review=review) == {}
    assert review["errors"] == ["task_priority_rank_conflict"]


def test_equal_priority_uses_the_models_local_rank_and_accepts_old_response_shape():
    raw = _response()
    for row in raw["results"]:
        row["read_priority"] = 80
    actual = validate_task_triage(ITEMS, raw)
    assert actual["new"]["task_read_rank"] == 1
    assert actual["new"]["task_read_priority"] == actual["old"]["task_read_priority"] == 80
    assert all(row["read_intent"] == "content" for row in actual.values())


def test_index_intent_is_explicit_bound_navigation_and_requires_later_model_review():
    raw = _response()
    raw["results"][1]["read_intent"] = "index"
    actual = validate_task_triage(ITEMS, raw)
    review = actual["new"]["task_review"]
    assert index_read_authorized("new", "index", review)
    assert not index_read_authorized("old", "index", review)
    assert not index_read_authorized("new", "content", review)
    assert not index_read_authorized("new", "index", {**review, "status": "rejected"})
    raw["results"][1]["need_model"] = False
    audit = {}
    assert validate_task_triage(ITEMS, raw, review=audit) == {}
    assert "index_requires_model_review" in audit["errors"]


def _planner(config, raw, *, calls=3, task_context=CONTEXT, status="success"):
    adapter = SimpleNamespace(usable=True, messages=[])

    def complete(messages):
        adapter.messages.append(messages)
        return ModelCallResult(model_config_id="stub", provider="offline", provider_type="stub",
                               model_name="stub", status=status, parsed=deepcopy(raw),
                               raw_text=json.dumps(raw, ensure_ascii=False), is_mock=True)

    adapter.complete = complete
    planner = ModelCallPlanner(config, SimpleNamespace(get=lambda key: adapter),
                               CallBudget(max_model_calls_per_run=calls))
    planner.policies = {"serp_batch_triage": {"models": [{"model_id": "stub"}]}}
    planner.task_context = deepcopy(task_context)
    return planner, adapter


def test_planner_uses_one_existing_call_and_keeps_extraction_reservation(config):
    planner, adapter = _planner(config, _response())
    actual = planner.batch_triage(ITEMS)
    assert actual["new"]["task_read_rank"] == 1
    assert len(adapter.messages) == planner.budget.calls_used == 1
    assert planner.budget.batch_triage_calls_used == 1
    assert json.loads(adapter.messages[0][1]["content"])["task_context"] == CONTEXT
    assert planner.task_triage_reviews[0]["status"] == "accepted"
    # Only two triage calls fit a total budget of three: one extraction slot remains.
    planner.batch_triage(ITEMS)
    assert planner.batch_triage(ITEMS) == {}
    assert len(adapter.messages) == 2 and planner.budget.can_call(1)


def test_only_model_call_is_never_spent_on_ranking(config):
    planner, adapter = _planner(config, _response(), calls=1)
    assert planner.batch_triage(ITEMS) == {}
    assert adapter.messages == [] and planner.budget.calls_used == 0


def test_planner_rejects_invalid_response_without_retry_or_partial_application(config):
    raw = _response()
    raw["results"].pop()
    planner, adapter = _planner(config, raw)
    assert planner.batch_triage(ITEMS) == {}
    assert len(adapter.messages) == 1
    assert planner.task_triage_reviews[0]["raw"] == raw
    assert planner.task_triage_reviews[0]["status"] == "rejected"


def test_failed_request_never_uses_even_parseable_ranking(config):
    planner, adapter = _planner(config, _response(), status="output_truncated")
    assert planner.batch_triage(ITEMS) == {}
    assert len(adapter.messages) == 1
    assert planner.task_triage_reviews[0]["errors"] == ["model_request_unsuccessful"]


def test_without_questions_old_batch_protocol_and_priority_are_unchanged(config):
    raw = {"results": [{"id": "old", "triage_decision": "read", "read_priority": 90,
                        "need_model": True}]}
    planner, adapter = _planner(config, raw, task_context=None)
    assert planner.batch_triage(ITEMS) == {"old": raw["results"][0]}
    payload = json.loads(adapter.messages[0][1]["content"])
    assert "task_context" not in payload
    assert "task_read_rank" not in payload["output_schema_hint"]["results"][0]
    assert planner.task_triage_reviews == []
