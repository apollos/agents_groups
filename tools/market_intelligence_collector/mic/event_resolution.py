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
MAX_CANDIDATES = 24


def fingerprint(event: dict) -> str:
    value = {k: event.get(k) for k in (
        "summary", "event_type", "event_date", "entities", "metrics", "evidence_locator",
        "source_context", "source_link_id",
    )}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str).encode()).hexdigest()


def candidate(event: dict, ref: str) -> dict:
    passages = event.get("source_context") or []
    if not passages:
        ev = event.get("evidence_locator") or {}
        if ev.get("excerpt"):
            passages = [{"passage_id": ev.get("passage_id") or "legacy",
                         "text": ev["excerpt"]}]
    return {"ref": ref, "fingerprint": fingerprint(event),
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
    return {"protocol": PROTOCOL, "candidates": candidates[-MAX_CANDIDATES:],
            "complete": complete and len(candidates) <= MAX_CANDIDATES,
            "candidate_count": len(candidates)}


def _grounded(citations: Any, passages: list[dict]) -> bool:
    if not isinstance(citations, list) or not citations:
        return False
    texts = {p.get("passage_id"): p.get("text", "") for p in passages}
    return all(isinstance(c, dict) and isinstance(c.get("quote"), str)
               and len(c["quote"].strip()) >= 8
               and c["quote"] in texts.get(c.get("passage_id"), "") for c in citations)


def resolve(raw: dict, context: dict, passages: list[dict]) -> dict:
    """Fail closed on missing decisions, incomplete recall, fabricated citations.

The verdict is derived from the comparisons, never trusted from a model's
claimed validation status. All candidates must be addressed, including ones
the model regards as different. An explicit uncertainty is preserved.
"""
    out = {"protocol": PROTOCOL, "status": "pending", "relation": "uncertain",
           "reason": "missing_or_invalid_comparison", "model_decision": raw,
           "candidate_refs": [c["ref"] for c in context.get("candidates", [])]}
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
    for ref, comp in by_ref.items():
        if (not comp.get("reason") or not _grounded(comp.get("current_evidence"), passages)
                or not _grounded(comp.get("candidate_evidence"), expected[ref]["passages"])):
            out["reason"] = "comparison_evidence_unverified"
            return out
        if comp.get("relation") not in {"same_event", "follow_up", "different"}:
            out["reason"] = "model_uncertain"
            return out
        if comp["relation"] != "different":
            matches.append((ref, comp["relation"]))
    if len(matches) > 1:
        out["reason"] = "multiple_matching_candidates"
        return out
    relation = matches[0][1] if matches else "new"
    if raw.get("verdict") != relation or not raw.get("reason"):
        out["reason"] = "verdict_comparison_conflict"
        return out
    out.update(status="resolved", relation=relation, reason=raw["reason"])
    if matches:
        ref = matches[0][0]
        out.update(candidate_ref=ref, candidate_fingerprint=expected[ref]["fingerprint"])
    return out


def finalize(bundle, context: dict, passages, *, run_id: str, link_id: str,
             multiple_extractions: bool = False) -> None:
    full = [p.model_dump() if hasattr(p, "model_dump") else p for p in passages]
    for index, event in enumerate(bundle.events):
        raw = event.event_resolution
        decision = resolve(raw, context, full)
        if multiple_extractions:
            decision.update(status="pending", relation="uncertain",
                            reason="multiple_extractions_require_joint_comparison")
        decision["ref"] = f"mic:{run_id}:{link_id}:{index}"
        event.event_resolution = decision
        # Keep a bounded evidence window, including the article's introductory
        # project context: a lot paragraph often says only "the same project".
        cited = event.evidence_locator.passage_id
        at = next((i for i, p in enumerate(full) if p.get("passage_id") == cited), 0)
        indices = sorted({0, max(0, at - 1), at, min(len(full) - 1, at + 1)})
        event.source_context = [{"passage_id": full[i]["passage_id"], "text": full[i]["text"][:1500]}
                                for i in indices if 0 <= i < len(full)]
        decision["event_fingerprint"] = fingerprint({**event.model_dump(mode="json"), "source_link_id": link_id})
