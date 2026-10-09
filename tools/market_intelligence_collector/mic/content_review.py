"""Enforce model-authored claims once, across every output location.

No business keywords or identity heuristics. This module checks references,
propagates review states and materializes reviewed fields. It does not certify
that a model's semantic judgement is correct or that a reported fact is true.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json

from mic.content_review_policy import (CORE_FIELDS, GROUPS, METADATA_FIELDS,
                                       NARRATIVE_FIELDS, PROTOCOL, STATES, field_kinds)
from mic.money import normalize_cny_fields, normalize_quoted_price, reason_status


def _present(value):
    if isinstance(value, dict):
        return any(_present(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_present(v) for v in value)
    return value is not None and value != ""


def _quote_supported(evidence, passages):
    return (isinstance(evidence, list) and bool(evidence)
            and all(isinstance(e, dict) and isinstance(e.get("quote"), str)
                    and bool(e["quote"].strip()) and isinstance(e.get("passage_id"), str)
                    and e.get("passage_id") != "title"
                    and e["quote"] in passages.get(e.get("passage_id"), "") for e in evidence))


def _digest(item):
    value = item.model_dump(mode="json") if hasattr(item, "model_dump") else deepcopy(item)
    # Event comparison and context are attached later, with their own fingerprint.
    for field in (*METADATA_FIELDS, "source_link_id"):
        value.pop(field, None)
    for field in list(value):
        if field.endswith("_id"):
            value.pop(field)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=str).encode()).hexdigest()


def seal(bundle):
    for group in ("brief", *GROUPS):
        objects = [bundle.brief] if group == "brief" else getattr(bundle, group)
        for obj in objects:
            if obj.content_review.get("protocol") == PROTOCOL:
                obj.content_review["materialized_digest"] = _digest(obj)


class ContentReview:
    def __init__(self, bundle, passages, warnings):
        self.bundle = bundle
        self.warnings = warnings
        self.passages = {p.passage_id: p.text for p in passages}
        raw = deepcopy(bundle.content_review)
        self.raw = raw
        valid = raw.get("protocol") == PROTOCOL
        rows = raw.get("claims", []) if valid else []
        rows = rows if isinstance(rows, list) else []
        counts = Counter(c.get("id") for c in rows if isinstance(c, dict) and isinstance(c.get("id"), str))
        self.claims = {c["id"]: c for c in rows if isinstance(c, dict)
                       and isinstance(c.get("id"), str) and counts[c["id"]] == 1}
        self.bindings = raw.get("bindings", {}) if valid else {}
        if not isinstance(self.bindings, dict):
            self.bindings = {}
        self.states = {}
        self.reasons = {}
        self.held = []
        self.fields = {}
        for cid in self.claims:
            self.state(cid)

    def state(self, cid, visiting=()):
        if cid in visiting:
            return "pending_review"
        if cid in self.states:
            return self.states[cid]
        claim = self.claims.get(cid)
        if not claim:
            return "pending_review"
        state, reason = claim.get("status"), claim.get("reason")
        dependencies = claim.get("depends_on", [])
        if (state not in STATES or not isinstance(reason, str) or not reason.strip()
                or not isinstance(claim.get("kind"), str)
                or claim["kind"] not in {"observation", "identity", "analysis", "comparability", "limitation"}
                or not isinstance(claim.get("statement"), str) or not claim["statement"].strip()
                or ("field_values" in claim and not isinstance(claim["field_values"], dict))
                or not isinstance(dependencies, list) or any(not isinstance(d, str) for d in dependencies)):
            state, reason = "pending_review", "invalid_claim"
            dependencies = []
        if state == "source_supported" and not _quote_supported(claim.get("evidence"), self.passages):
            state, reason = "pending_review", "citation_missing_or_unverified"
        if any(self.state(dep, (*visiting, cid)) != "source_supported" for dep in dependencies):
            if state != "unsupported":
                state, reason = "pending_review", "dependency_not_supported"
        self.states[cid], self.reasons[cid] = state, reason
        return state

    def ids(self, path, inherited=None):
        value = self.bindings.get(path, inherited)
        if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
            return []
        return list(dict.fromkeys(value))

    def hold(self, path, value, reason, ids=(), status="pending_review"):
        self.held.append({"path": path, "status": status, "reason": reason,
                          "claim_ids": list(ids), "original": deepcopy(value)})
        self.warnings.append(f"content review: {path}; {reason}; {status}")

    def reviewed_value(self, ids, field):
        """Materialize the model's explicit typed judgment, not a borrowed ID.

        JSON member names are protocol structure, never entity matching rules.
        All approved values of the same field must agree; no last-writer wins.
        """
        values = []
        for cid in ids:
            current = self.claims[cid].get("field_values", {})
            for part in field.split("/"):
                if not isinstance(current, dict) or part not in current:
                    break
                current = current[part]
            else:
                values.append(current)
        if not values:
            return False, None
        if all(isinstance(v, dict) for v in values):
            combined = {}
            for v in values:
                for key, item in v.items():
                    if key in combined and combined[key] != item:
                        return False, None
                    combined[key] = item
            return True, deepcopy(combined)
        return (True, deepcopy(values[0])) if all(v == values[0] for v in values) else (False, None)

    def field(self, path, value, default, *, inherited=None, narrative=False, group="", field=""):
        ids = self.ids(path, inherited)
        # Explicit child bindings override a shared parent binding. This lets an
        # unknown owner be held without erasing the known event subject.
        if isinstance(value, dict) and any(p.startswith(path + "/") for p in self.bindings):
            result = {}
            for key, child in value.items():
                child_default = default.get(key) if isinstance(default, dict) else None
                result[key] = self.field(path + "/" + key, child, child_default,
                                         inherited=ids, narrative=False, group=group, field=field + "/" + key)
            return result
        if not _present(value) or value == default:
            return deepcopy(value)
        kinds = field_kinds(group, field.split("/")[0])
        accepted = [cid for cid in ids if cid in self.claims and self.state(cid) == "source_supported"
                    and self.claims[cid].get("kind") in kinds]
        held = [cid for cid in ids if cid not in accepted]
        self.fields[path] = {"claim_ids": ids,
                             "states": {cid: self.state(cid) for cid in ids},
                             "status": "source_supported" if ids and not held else "pending_review"}
        if ids and not held:
            if narrative:
                return "；".join(dict.fromkeys(self.claims[cid]["statement"] for cid in accepted))
            if kinds != {"observation"}:
                valid, reviewed = self.reviewed_value(accepted, field)
                if not valid:
                    self.fields[path]["status"] = "pending_review"
                    self.hold(path, value, "reviewed_value_missing_or_conflicting", ids)
                    return deepcopy(default)
                if reviewed != value:
                    self.hold(path, value, "replaced_by_explicit_reviewed_value", ids)
                return reviewed
            return deepcopy(value)
        wrong_kind = any(not isinstance(self.claims[cid].get("kind"), str)
                         or self.claims[cid]["kind"] not in kinds for cid in ids if cid in self.claims)
        reason = "claim_kind_mismatch" if wrong_kind else "claim_not_supported" if ids else "missing_record_review"
        if any(cid not in self.claims for cid in ids):
            reason = "unknown_claim_id"
        elif any(self.reasons.get(cid) == "invalid_claim" for cid in ids):
            reason = "invalid_claim"
        self.hold(path, value, reason, ids)
        if narrative and accepted:
            self.fields[path]["status"] = "partially_held"
            return "；".join(dict.fromkeys(self.claims[cid]["statement"] for cid in accepted))
        return deepcopy(default)

    def record(self, obj, path):
        default = type(obj)().model_dump(mode="json")
        before = obj.model_dump(mode="json")
        out = deepcopy(before)
        start = len(self.held)
        group = path.split("/")[1]
        record_ids = self.ids(path)
        for name, value in before.items():
            if name in METADATA_FIELDS or name.endswith("_id"):
                continue
            out[name] = self.field(path + "/" + name, value, default[name],
                                   inherited=record_ids, narrative=name in NARRATIVE_FIELDS,
                                   group=group, field=name)
        # A model-supplied 'official_confirmed' is not independent verification.
        if "source_corroboration_status" in out:
            out["source_corroboration_status"] = "single_source"
        for name in ("metrics", "qualifiers"):
            values = out.get(name)
            if not isinstance(values, dict) or values.get("amount") is None:
                continue
            money_path = path + "/" + name
            ids = self.ids(money_path + "/amount", self.ids(money_path, record_ids))
            # The amount survived field review only because its claims are
            # source_supported. The model's reviewed decomposition (currency,
            # value, unit, citations) is applied as-is; code checks structure,
            # citation membership and does the Decimal arithmetic. Claims that
            # are inference / pending_review / unsupported stay in the audit
            # record and never supply an amount.
            specifications = [self.claims[cid]["amount"] for cid in ids
                              if cid in self.claims and self.claims[cid].get("amount") is not None
                              and self.state(cid) == "source_supported"]
            pid = before.get("evidence_locator", {}).get("passage_id")
            updated, reason = normalize_quoted_price(values, self.passages, pid, specifications)
            if reason == "not_unit_price":
                updated, reason = normalize_cny_fields(values, self.passages, specifications)
            if updated is not None:
                out[name] = updated
                if updated.get("amount_conversion"):
                    self.fields[money_path + "/amount"] = {
                        "claim_ids": ids, "states": {cid: self.state(cid) for cid in ids},
                        "status": "source_supported", "conversion": updated["amount_conversion"]}
            else:
                status = "pending_review" if reason == "amount_not_supported_by_citation" else reason_status(reason)
                self.hold(money_path + "/amount", values, reason, ids, status)
                self.fields[money_path + "/amount"] = {
                    "claim_ids": ids, "states": {cid: self.state(cid) for cid in ids},
                    "status": status, "reason": reason}
                out[name] = {**values, "amount": None, "amount_candidate": values["amount"],
                             "amount_status": status, "amount_reason": reason}
        audit = {"protocol": PROTOCOL,
                 "fields": {k[len(path) + 1:]: v for k, v in self.fields.items() if k.startswith(path + "/")},
                 "held_fields": deepcopy(self.held[start:])}
        ids = {cid for value in audit["fields"].values() for cid in value["claim_ids"]}
        audit["claims"] = {cid: {**self.claims[cid], "effective_status": self.state(cid),
                                 "effective_reason": self.reasons[cid]} for cid in ids if cid in self.claims}
        comparability = [cid for cid in ids if self.claims.get(cid, {}).get("kind") == "comparability"]
        if "scope" in out:
            # A reported price is not automatically a benchmark, even when
            # the model places an approval flag inside factual scope metadata.
            proposals = [(self.claims[cid].get("field_values") or {}).get("scope", {})
                         if isinstance(self.claims[cid].get("field_values", {}), dict) else {}
                         for cid in comparability]
            allowed = bool(comparability) and all(self.state(cid) == "source_supported" for cid in comparability)
            allowed = allowed and bool(proposals) and all(isinstance(p, dict) and p.get("usable_as_price_benchmark") is True for p in proposals)
            out["scope"]["usable_as_price_benchmark"] = allowed
            out["scope"]["source_price_basis_status"] = "source_supported" if allowed else "pending_review"
        out["content_review"] = audit
        return type(obj).model_validate(out)

    def apply(self):
        self.bundle.brief = self.record(self.bundle.brief, "/brief")
        for group in GROUPS:
            kept = []
            for index, obj in enumerate(getattr(self.bundle, group)):
                path = f"/{group}/{index}"
                reviewed = self.record(obj, path)
                required = CORE_FIELDS[group]
                usable = all(_present(getattr(reviewed, field)) for field in required)
                if group == "relations":
                    usable = usable and bool(reviewed.subject_entity.name and reviewed.object_entity.name)
                if usable:
                    kept.append(reviewed)
                else:
                    self.hold(path, obj.model_dump(mode="json"), "core_claim_not_supported")
            setattr(self.bundle, group, kept)
        self.bundle.content_review = {
            "protocol": PROTOCOL, "status": "applied", "model_review": self.raw,
            "claims": {cid: {"status": self.state(cid), "reason": self.reasons[cid]}
                       for cid in self.claims}, "held": self.held,
            "counts": dict(Counter(item["status"] for item in self.held)),
            "protocol_errors": [item for item in self.held if item["reason"] in {
                "missing_record_review", "claim_kind_mismatch", "reviewed_value_missing_or_conflicting",
                "unknown_claim_id", "invalid_claim"}],
        }
        return self.held


def enforce_integrity(bundle):
    """Merged/cached/exported values cannot reuse a review of different content."""
    from mic.schemas import Brief
    if bundle.content_review.get("protocol") != PROTOCOL:
        return
    for group in ("brief", *GROUPS):
        objects = [bundle.brief] if group == "brief" else getattr(bundle, group)
        kept = []
        for obj in objects:
            if (obj.content_review.get("protocol") == PROTOCOL
                    and obj.content_review.get("materialized_digest") == _digest(obj)):
                kept.append(obj)
            else:
                bundle.content_review.setdefault("held", []).append({
                    "path": group, "status": "pending_review", "reason": "reviewed_content_changed",
                    "original": obj.model_dump(mode="json")})
        if group == "brief":
            bundle.brief = kept[0] if kept else Brief()
        else:
            setattr(bundle, group, kept)


def reviewed_event(event):
    """Verify the materialized event at the Agent boundary, before admission."""
    from mic.schemas import EventCard
    from pydantic import ValidationError
    try:
        obj = EventCard.model_validate(event)
    except (ValidationError, TypeError):
        return False
    return (obj.content_review.get("protocol") == PROTOCOL
            and obj.content_review.get("materialized_digest") == _digest(obj)
            and bool(obj.summary))


def contract_diagnostics(reviews):
    """Report protocol/format failures separately from legitimate uncertainty."""
    leaves = []
    def visit(review):
        if isinstance(review, dict) and review.get("contributions"):
            for child in review["contributions"]:
                visit(child)
        else:
            leaves.append(review if isinstance(review, dict) else {})
    for review in reviews:
        visit(review)
    reasons = Counter(h.get("reason") for r in leaves for h in r.get("protocol_errors", []))
    outdated = sum(r.get("protocol") != PROTOCOL or r.get("status") != "applied" for r in leaves)
    formatting = sum(h.get("status") == "format_pending" for r in leaves for h in r.get("held", []))
    return {"protocol": PROTOCOL, "reviewed_sources": len(leaves),
            "protocol_errors": sum(reasons.values()), "protocol_error_reasons": dict(reasons),
            "outdated_or_missing_reviews": outdated, "format_pending": formatting,
            "complete": bool(leaves) and not reasons and not outdated and not formatting}
