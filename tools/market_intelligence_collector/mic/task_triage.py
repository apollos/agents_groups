"""Validate model-directed reading order without making semantic decisions.

Search titles/snippets can guide navigation; they cannot authorize facts,
publication dates, or a claim that a report is the latest disclosed report.
"""

from __future__ import annotations

from copy import deepcopy
import math


PROTOCOL = "task_triage_v1"
SEARCH_BASIS = "search_result_navigation_only"
INDEX_BASIS = "index_result_navigation_only"
_DECISIONS = {"read", "link_record_only", "skip_for_now"}
_READ_INTENTS = {"content", "index"}


def index_read_authorized(source_link_id: str, read_intent: str,
                          review: dict | None) -> bool:
    """Require bound, accepted model navigation intent, not inferred URL shape.

    This authorizes a material parser only. It grants no publication, factual,
    latestness or task-coverage approval; those remain shared-review decisions.
    """
    return bool(
        read_intent == "index" and isinstance(review, dict)
        and review.get("protocol") == PROTOCOL and review.get("status") == "accepted"
        and review.get("basis") in {SEARCH_BASIS, INDEX_BASIS}
        and review.get("source_link_id") == source_link_id
        and review.get("read_intent") == "index"
        and review.get("triage_decision") == "read" and review.get("need_model") is True
    )


def validate_task_triage(items: list[dict], raw: object, *,
                         review: dict | None = None,
                         basis: str = SEARCH_BASIS) -> dict[str, dict]:
    """Return a complete, strictly bound ranking, or reject the entire batch.

    ``review`` optionally receives the raw response and protocol errors for the
    run audit. A partially valid ranking is never returned, so invalid model
    output cannot silently promote a subset of the candidates.
    """
    errors: list[str] = []
    if basis not in {SEARCH_BASIS, INDEX_BASIS}:
        errors.append("invalid_navigation_basis")
    ids = [item.get("id") if isinstance(item, dict) else None for item in items]
    if not ids or any(type(value) is not str or not value for value in ids):
        errors.append("invalid_input_ids")
    elif len(set(ids)) != len(ids):
        errors.append("duplicate_input_ids")
    expected = {value for value in ids if type(value) is str and value}
    rows = raw.get("results") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        errors.append("results_not_list")
        rows = []
    seen: set[str] = set()
    ranks: set[int] = set()
    normalized: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            errors.append("result_not_object")
            continue
        source_id = row.get("id")
        if type(source_id) is not str or source_id not in expected:
            errors.append("unknown_result_id")
            continue
        if source_id in seen:
            errors.append("duplicate_result_id")
        seen.add(source_id)
        decision = row.get("triage_decision")
        if type(decision) is not str or decision not in _DECISIONS:
            errors.append("invalid_triage_decision")
        priority = row.get("read_priority")
        if (type(priority) not in (int, float) or not 0 <= priority <= 100
                or not math.isfinite(priority)):
            errors.append("invalid_read_priority")
        need_model = row.get("need_model")
        if type(need_model) is not bool:
            errors.append("invalid_need_model")
        # Old responses retain the article path. A directory route always needs
        # an explicit, valid model choice and a subsequent shared-review call.
        read_intent = row.get("read_intent", "content")
        if type(read_intent) is not str or read_intent not in _READ_INTENTS:
            errors.append("invalid_read_intent")
        if read_intent == "index" and decision == "read" and need_model is not True:
            errors.append("index_requires_model_review")
        if basis == INDEX_BASIS and decision == "read" and need_model is not True:
            errors.append("index_child_requires_model_review")
        rank = row.get("task_read_rank")
        if type(rank) is not int or not 1 <= rank <= len(items):
            errors.append("invalid_task_read_rank")
        elif rank in ranks:
            errors.append("duplicate_task_read_rank")
        else:
            ranks.add(rank)
        reason = row.get("reason")
        if type(reason) is not str or not reason.strip():
            errors.append("missing_task_reason")
        normalized[source_id] = {
            "id": source_id, "triage_decision": decision,
            "read_priority": priority, "need_model": need_model,
            "task_read_priority": priority,
            "read_intent": read_intent,
            "task_read_rank": rank, "reason": reason,
            "task_review": {
                "protocol": PROTOCOL, "status": "accepted",
                "basis": basis,
                "source_link_id": source_id, "task_read_rank": rank,
                "task_read_priority": priority,
                "read_intent": read_intent, "triage_decision": decision,
                "need_model": need_model,
                "reason": reason,
            },
        }
    if seen != expected:
        errors.append("missing_result_ids")
    if ranks != set(range(1, len(items) + 1)):
        errors.append("incomplete_task_ranking")
    if not errors:
        ranked = sorted(normalized.values(), key=lambda row: row["task_read_rank"])
        if any(left["task_read_priority"] < right["task_read_priority"]
               for left, right in zip(ranked, ranked[1:])):
            errors.append("task_priority_rank_conflict")
    if review is not None:
        review.update({
            "protocol": PROTOCOL,
            "status": "rejected" if errors else "accepted",
            "basis": basis,
            "input_ids": ids,
            "errors": list(dict.fromkeys(errors)),
            "raw": deepcopy(raw),
        })
    return {} if errors else normalized
