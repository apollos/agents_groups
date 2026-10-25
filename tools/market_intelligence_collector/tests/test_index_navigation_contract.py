"""Offline navigation-triage boundaries; no live model behavior is asserted."""

from copy import deepcopy
import json

import pytest

from mic.modeling.prompts import build_batch_triage_messages
from mic.task_triage import (
    INDEX_BASIS, SEARCH_BASIS, index_read_authorized, validate_task_triage,
)
from tests.test_task_triage import CONTEXT, ITEMS, _planner, _response


def navigation_items():
    items = deepcopy(ITEMS)
    for ordinal, item in enumerate(items):
        item["navigation"] = {
            "protocol": "index_navigation_v1",
            "parent_source_link_id": "parent_index",
            "parent_url": "https://company.example/list",
            "kind": "entry", "entry_id": f"entry_{ordinal + 1}",
            "link_index": 0, "entry_text": item["snippet"],
            "link_text": item["title"], "original_url": item["url"],
            "scope": {"truncated": True, "ordering": "unknown", "completeness": "incomplete"},
        }
    return items


def navigation_context():
    return {
        "protocol": "index_navigation_v1", "parent_source_link_id": "parent_index",
        "read_state": {
            "attempted_source_ids": ["prior_failed", "parent_index"],
            "successful_source_ids": ["parent_index"],
            "note": "Successful retrieval is not approved evidence.",
        },
    }


def test_index_basis_accepts_complete_bound_ranking_without_source_approval():
    raw, audit = _response(), {}
    raw["results"][1]["read_intent"] = "index"
    result = validate_task_triage(navigation_items(), raw, review=audit, basis=INDEX_BASIS)
    assert set(result) == {"old", "new"}
    assert audit["status"] == "accepted" and audit["errors"] == []
    assert audit["basis"] == INDEX_BASIS
    review = result["new"]["task_review"]
    assert review["source_link_id"] == "new"
    assert review["basis"] == INDEX_BASIS
    assert index_read_authorized("new", "index", review)
    assert not index_read_authorized("old", "index", review)
    assert not index_read_authorized("new", "content", review)
    assert not index_read_authorized("new", "index", {**review, "status": "rejected"})
    assert "source_supported" not in json.dumps(result)


@pytest.mark.parametrize("intent", ["content", "index"])
def test_index_child_read_without_later_model_review_rejects_entire_batch(intent):
    raw, audit = _response(), {}
    raw["results"][0].update(read_intent=intent, need_model=False)
    assert validate_task_triage(navigation_items(), raw, basis=INDEX_BASIS, review=audit) == {}
    assert audit["status"] == "rejected"
    assert "index_child_requires_model_review" in audit["errors"]
    assert audit["raw"]["results"][1]["need_model"] is True


@pytest.mark.parametrize("decision", ["link_record_only", "skip_for_now"])
def test_non_read_index_child_can_decline_model_review_without_becoming_authorized(decision):
    raw = _response()
    raw["results"][0].update(triage_decision=decision, need_model=False, read_intent="index")
    result = validate_task_triage(navigation_items(), raw, basis=INDEX_BASIS)
    assert set(result) == {"old", "new"}
    assert not index_read_authorized("old", "index", result["old"]["task_review"])


@pytest.mark.parametrize("basis", ["index", "content_approval", "", "index_result_navigation_only_v2"])
def test_unknown_basis_never_authorizes_whole_or_partial_ranking(basis):
    audit = {}
    assert validate_task_triage(navigation_items(), _response(), basis=basis, review=audit) == {}
    assert "invalid_navigation_basis" in audit["errors"]
    valid = validate_task_triage(ITEMS, _response(), basis=SEARCH_BASIS)
    assert not index_read_authorized(
        "new", "index",
        {**valid["new"]["task_review"], "read_intent": "index", "basis": basis})


def test_default_search_basis_preserves_legacy_content_without_need_model():
    raw = _response()
    raw["results"][0].update(read_intent="content", need_model=False)
    default = validate_task_triage(ITEMS, raw)
    explicit = validate_task_triage(ITEMS, raw, basis=SEARCH_BASIS)
    assert default == explicit and set(default) == {"old", "new"}
    assert default["old"]["need_model"] is False
    assert default["old"]["task_review"]["basis"] == SEARCH_BASIS
    raw["results"][1]["read_intent"] = "index"
    legacy_index = validate_task_triage(ITEMS, raw)
    assert index_read_authorized("new", "index", legacy_index["new"]["task_review"])


@pytest.mark.parametrize("task_context", [None, {}, {"questions": []}, {"instructions": "read next"}])
def test_planner_navigation_without_task_questions_makes_zero_requests(config, task_context):
    planner, adapter = _planner(config, _response(), task_context=task_context)
    assert planner.batch_triage(navigation_items(), navigation_context=navigation_context()) == {}
    assert adapter.messages == []
    assert planner.budget.calls_used == planner.budget.batch_triage_calls_used == 0
    assert planner.triage_results == planner.task_triage_reviews == []


def test_full_navigation_material_and_current_budget_reach_existing_model_request(config):
    items, context = navigation_items(), navigation_context()
    original_items, original_context = deepcopy(items), deepcopy(context)
    reading_budget = {
        "limits": {"max_model_calls": 8, "max_links_to_read": 4},
        "remaining": {"model_calls": 3, "successful_reads": 2},
    }
    planner, adapter = _planner(config, _response())
    planner.triage_budget_context = deepcopy(reading_budget)
    result = planner.batch_triage(items, navigation_context=context)
    assert set(result) == {"old", "new"} and len(adapter.messages) == 1
    payload = json.loads(adapter.messages[0][1]["content"])
    assert payload["task_context"] == CONTEXT
    assert payload["items"] == original_items
    assert payload["navigation_context"] == original_context
    assert payload["reading_budget"] == reading_budget
    assert all(row["task_review"]["basis"] == INDEX_BASIS for row in result.values())
    audit = planner.task_triage_reviews[0]
    assert audit["navigation_context"] == original_context
    assert audit["reading_budget"] == reading_budget
    assert audit["basis"] == INDEX_BASIS and audit["status"] == "accepted"
    assert items == original_items and context == original_context


@pytest.mark.parametrize("large_part", ["navigation_context", "item_navigation", "task_context"])
def test_oversized_complete_input_is_not_sent_or_silently_truncated(config, large_part):
    items, context, task = navigation_items(), navigation_context(), deepcopy(CONTEXT)
    text = "完整原始材料不可截断 " * 1200
    if large_part == "navigation_context":
        context["read_state"]["detail"] = text
    elif large_part == "item_navigation":
        items[1]["navigation"]["entry_text"] = text
    else:
        task["questions"][0]["question"] = text
    original = deepcopy((items, context, task))
    planner, adapter = _planner(config, _response(), task_context=task)
    planner.max_input_chars = 4000
    assert planner.batch_triage(items, navigation_context=context) == {}
    assert adapter.messages == [] and planner.triage_results == []
    assert planner.budget.calls_used == planner.budget.batch_triage_calls_used == 0
    assert (items, context, task) == original


def test_complete_input_character_limit_is_inclusive_and_one_less_does_not_send(config):
    items, context = navigation_items(), navigation_context()
    messages = build_batch_triage_messages(items, task_context=CONTEXT,
                                          navigation_context=context)
    exact_chars = len(messages[-1]["content"])
    planner, adapter = _planner(config, _response())
    planner.max_input_chars = exact_chars - 1
    assert planner.batch_triage(items, navigation_context=context) == {}
    assert adapter.messages == [] and planner.budget.calls_used == 0
    planner.max_input_chars = exact_chars
    assert set(planner.batch_triage(items, navigation_context=context)) == {"old", "new"}
    assert adapter.messages == [messages]
    assert planner.budget.calls_used == planner.budget.batch_triage_calls_used == 1


def test_search_and_navigation_share_total_model_budget_and_reserve_one_extraction(config):
    planner, adapter = _planner(config, _response(), calls=3)
    assert planner.batch_triage(ITEMS)
    assert planner.batch_triage(navigation_items(), navigation_context=navigation_context())
    assert planner.batch_triage(navigation_items(), navigation_context=navigation_context()) == {}
    assert planner.batch_triage(ITEMS) == {}
    assert len(adapter.messages) == planner.budget.calls_used == 2
    assert planner.budget.batch_triage_calls_used == 2
    assert planner.budget.can_call(1) and not planner.budget.can_call(2)
    assert [r["basis"] for r in planner.task_triage_reviews] == [SEARCH_BASIS, INDEX_BASIS]


@pytest.mark.parametrize("navigation_first", [False, True])
def test_navigation_and_search_share_triage_subbudget_in_both_directions(config, navigation_first):
    planner, adapter = _planner(config, _response(), calls=8)
    planner.budget.max_batch_triage_calls = 1
    if navigation_first:
        assert planner.batch_triage(navigation_items(), navigation_context=navigation_context())
        assert planner.batch_triage(ITEMS) == {}
    else:
        assert planner.batch_triage(ITEMS)
        assert planner.batch_triage(navigation_items(), navigation_context=navigation_context()) == {}
    assert len(adapter.messages) == planner.budget.calls_used == planner.budget.batch_triage_calls_used == 1
    assert planner.budget.can_call(7)


def test_existing_extraction_usage_also_reduces_navigation_room(config):
    planner, adapter = _planner(config, _response(), calls=3)
    planner.budget.record(1)  # Prior extraction consumed the same run budget.
    assert planner.batch_triage(ITEMS)
    assert planner.batch_triage(navigation_items(), navigation_context=navigation_context()) == {}
    assert len(adapter.messages) == 1
    assert planner.budget.calls_used == 2 and planner.budget.batch_triage_calls_used == 1
    assert planner.budget.can_call(1)


def test_last_model_call_cannot_be_spent_on_navigation(config):
    planner, adapter = _planner(config, _response(), calls=1)
    assert planner.batch_triage(navigation_items(), navigation_context=navigation_context()) == {}
    assert adapter.messages == [] and planner.budget.calls_used == 0


def test_extra_model_url_is_auditable_but_never_an_executable_selection_or_input_mutation(config):
    items, raw = navigation_items(), _response()
    original_items = deepcopy(items)
    unprovided_url = "http://127.0.0.1/private"
    raw["results"][1].update(url=unprovided_url, navigation={"original_url": unprovided_url})
    raw["results"][1]["reason"] = f"Read {unprovided_url} instead of the supplied URL"
    planner, adapter = _planner(config, raw)
    result = planner.batch_triage(items, navigation_context=navigation_context())
    assert set(result) == {"old", "new"}
    assert "url" not in result["new"] and "navigation" not in result["new"]
    assert "url" not in result["new"]["task_review"]
    assert items == original_items
    assert json.loads(adapter.messages[0][1]["content"])["items"] == original_items
    assert planner.task_triage_reviews[0]["raw"]["results"][1]["url"] == unprovided_url
    # This layer returns IDs and validated ranking; URL execution is separately
    # covered by pipeline tests and is never sourced from the free-form reason.
    by_id = {item["id"]: item for item in items}
    assert by_id[result["new"]["id"]]["url"] == ITEMS[1]["url"]
