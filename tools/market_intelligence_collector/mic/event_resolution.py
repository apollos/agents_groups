"""Evidence-grounded event comparisons inside the existing extraction request.

No model calls, entity regexes or identity-key equality here. The model compares
source context; code checks coverage, references and verbatim evidence. Grounded
citations make a decision auditable, not infallible.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL = "semantic_event_v1"
COMPARISON_CONTRACT = "event_scope_v2"
MAX_CANDIDATES = 24


def fingerprint(event: dict) -> str:
    value = {k: event.get(k) for k in (
        "summary", "event_type", "event_date", "entities", "metrics", "evidence_locator",
        "source_context", "source_link_id",
    )}
    if event.get("content_review"):
        value["content_review"] = event["content_review"]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str).encode()).hexdigest()


def candidate(event: dict, ref: str) -> dict:
    passages = event.get("source_context") or []
    if not passages:
        ev = event.get("evidence_locator") or {}
        if ev.get("excerpt"):
            passages = [{"passage_id": ev.get("passage_id") or "legacy",
                         "text": ev["excerpt"]}]
    review = event.get("content_review") or {}
    focus_ids = (review.get("fields", {}).get("summary") or {}).get("claim_ids", [])
    focus = [review.get("claims", {}).get(cid) for cid in focus_ids]
    return {"ref": ref, "fingerprint": fingerprint(event),
            "focus": [c for c in focus if c], "evidence_locator": event.get("evidence_locator"),
            "summary": event.get("summary"), "event_type": event.get("event_type"),
            "event_date": event.get("event_date"), "entities": event.get("entities"),
            "metrics": event.get("metrics"), "source": event.get("source"),
            "passages": passages}


def build_context(history: dict | None, events: list[dict]) -> dict:
    history = history or {}
    complete = bool(history.get("complete", True))
    candidates = list(history.get("candidates") or [])
    for event in events:
        resolution = event.get("event_resolution") or {}
        if resolution.get("status") == "resolved" and resolution.get("relation") == "same_event":
            continue  # already represented by its canonical candidate
        if resolution.get("ref"):
            candidates.append(candidate(event, resolution["ref"]))
        else:
            complete = False  # cached/legacy rows have not been compared this run
    # Broad same-target recall, deliberately independent of project/subject/type keys.
    unique = {c["ref"]: c for c in candidates}
    candidates = list(unique.values())
    return {"protocol": PROTOCOL, "comparison_contract": COMPARISON_CONTRACT,
            "candidates": candidates[-MAX_CANDIDATES:],
            "complete": complete and len(candidates) <= MAX_CANDIDATES,
            "candidate_count": len(candidates)}


def _grounded(citations: Any, passages: list[dict]) -> bool:
    if not isinstance(citations, list) or not citations:
        return False
    texts = {p.get("passage_id"): p.get("text", "") for p in passages}
    return all(isinstance(c, dict) and isinstance(c.get("quote"), str)
               and len(c["quote"].strip()) >= 8
               and c["quote"] in texts.get(c.get("passage_id"), "") for c in citations)


def resolve(raw: dict, context: dict, passages: list[dict], *, current_claim_ids=None) -> dict:
    """Fail closed on missing decisions, incomplete recall, fabricated citations.

The verdict is derived from the comparisons, never trusted from a model's
claimed validation status. All candidates must be addressed, including ones
the model regards as different. An explicit uncertainty is preserved.
"""
    out = {"protocol": PROTOCOL, "status": "pending", "relation": "uncertain",
           "reason": "missing_or_invalid_comparison", "model_decision": raw,
           "candidate_refs": [c["ref"] for c in context.get("candidates", [])]}
    scoped = context.get("comparison_contract") == COMPARISON_CONTRACT
    if scoped:
        out["comparison_contract"] = COMPARISON_CONTRACT
        anchor = raw.get("current_claim_ids")
        if current_claim_ids is not None and (not isinstance(anchor, list)
                or any(not isinstance(cid, str) for cid in anchor)
                or len(set(anchor)) != len(anchor) or set(anchor) != set(current_claim_ids)):
            out["reason"] = "current_event_anchor_mismatch"
            return out
    if raw.get("reviewed") is not True or not _grounded(raw.get("current_evidence"), passages):
        return out
    if not context.get("complete"):
        out["reason"] = "candidate_context_incomplete"
        return out
    comparisons = raw.get("comparisons")
    if not isinstance(comparisons, list) or any(not isinstance(c, dict)
            or not isinstance(c.get("candidate_ref"), str) for c in comparisons):
        return out
    by_ref = {c.get("candidate_ref"): c for c in comparisons}
    expected = {c["ref"]: c for c in context.get("candidates", [])}
    if len(by_ref) != len(comparisons) or set(by_ref) != set(expected):
        out["reason"] = "candidate_coverage_mismatch"
        return out
    matches = []
    related = []
    for ref, comp in by_ref.items():
        if (not comp.get("reason") or not _grounded(comp.get("current_evidence"), passages)
                or not _grounded(comp.get("candidate_evidence"), expected[ref]["passages"])):
            out["reason"] = "comparison_evidence_unverified"
            return out
        if scoped:
            scope, occurrence, stage = (comp.get("scope_relation"), comp.get("same_occurrence"),
                                        comp.get("stage_relation"))
            if not isinstance(scope, str) or not isinstance(stage, str):
                out["reason"] = "event_scope_unverified"
                return out
            if scope == "disjoint" or occurrence is False:
                expected_relation = "different"
            elif scope in {"contains", "contained_by", "overlaps"} and occurrence is True:
                expected_relation = "related"
            elif scope == "equivalent" and occurrence is True and stage in {"same", "progression"}:
                expected_relation = "same_event" if stage == "same" else "follow_up"
            else:
                out["reason"] = "event_scope_unverified"
                return out
            if comp.get("relation") != expected_relation:
                out["reason"] = "event_scope_relation_conflict"
                return out
        if comp.get("relation") not in {"same_event", "follow_up", "different", "related"}:
            out["reason"] = "model_uncertain"
            return out
        if comp["relation"] == "related":
            related.append(ref)
        if comp["relation"] in {"same_event", "follow_up"}:
            matches.append((ref, comp["relation"]))
    if len(matches) > 1:
        out["reason"] = "multiple_matching_candidates"
        return out
    relation = matches[0][1] if matches else "new"
    if raw.get("verdict") != relation or not raw.get("reason"):
        out["reason"] = "verdict_comparison_conflict"
        return out
    out.update(status="resolved", relation=relation, reason=raw["reason"])
    out["related_candidate_refs"] = related
    if matches:
        ref = matches[0][0]
        out.update(candidate_ref=ref, candidate_fingerprint=expected[ref]["fingerprint"])
    return out


def finalize(bundle, context: dict, passages, *, run_id: str, link_id: str,
             multiple_extractions: bool = False) -> None:
    full = [p.model_dump() if hasattr(p, "model_dump") else p for p in passages]
    for index, event in enumerate(bundle.events):
        raw = event.event_resolution
        review = event.content_review
        ids = (review.get("fields", {}).get("summary") or {}).get("claim_ids")
        decision = resolve(raw, context, full, current_claim_ids=ids)
        if multiple_extractions:
            decision.update(status="pending", relation="uncertain",
                            reason="multiple_extractions_require_joint_comparison")
        decision["ref"] = f"mic:{run_id}:{link_id}:{index}"
        event.event_resolution = decision
        # Keep a bounded evidence window plus title and opening body context.
        # Reader assigns p0 to the first body paragraph; index 0 may be the
        # title. Use those structural IDs, not list position or inferred roles.
        cited = event.evidence_locator.passage_id
        at = next((i for i, p in enumerate(full) if p.get("passage_id") == cited), 0)
        indices = {max(0, at - 1), at, min(len(full) - 1, at + 1)}
        indices.update(i for i, p in enumerate(full) if p.get("passage_id") in {"title", "p0"})
        event.source_context = [{"passage_id": full[i]["passage_id"], "text": full[i]["text"][:1500]}
                                for i in sorted(indices) if 0 <= i < len(full)]
        decision["event_fingerprint"] = fingerprint({**event.model_dump(mode="json"), "source_link_id": link_id})
