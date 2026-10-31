"""Enforce model-authored claims once, across every output location.

No business keywords or identity heuristics. This module checks references,
propagates review states and materializes reviewed fields. It does not certify
that a model's semantic judgement is correct or that a reported fact is true.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import hashlib
import json

from mic.content_review_policy import (COMPARISON_RATE_UNITS, CORE_FIELDS, DATE_BASES, DATE_REVIEW_PROTOCOL, DATE_ROLES, DATE_ROLE_BASES,
                                       GROUPS, INDEX_FACT_TYPE, INDEX_PASSAGE_SECTIONS, METADATA_FIELDS,
                                       NARRATIVE_FIELDS, PROTOCOL, REVIEW_REVISION, REVIEWED_DEFAULTS, STATES,
                                       SCOPE_DERIVED_FIELDS, SCOPE_REVIEW_PROTOCOL,
                                       TASK_COVERAGE_PROTOCOL, field_kinds)
from mic.money import normalize_cny_fields, normalize_quoted_price, reason_status
from mic.publication_time import LOCAL_TZ, parse_published


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


# Model-authored amount components. Absence is a historical response and is not
# a downgrade; a supplied basis constrains publication without reading reasons.
_AMOUNT_BASIS_PARTS = ("value", "unit", "currency")


def _citations_covered(evidence, claim_evidence):
    if not isinstance(claim_evidence, list):
        return False
    covered = {(item.get("passage_id"), item.get("quote"))
               for item in claim_evidence if isinstance(item, dict)}
    return all(isinstance(item, dict) and (item.get("passage_id"), item.get("quote")) in covered
               for item in evidence)


def _basis_component_issue(component, claim, passages):
    if (not isinstance(component, dict) or component.get("status") not in STATES
            or not isinstance(component.get("evidence"), list)):
        return "invalid"
    if component["status"] != "source_supported":
        return "not_supported"
    evidence = component["evidence"]
    if (not _quote_supported(evidence, passages)
            or not _citations_covered(evidence, claim.get("evidence"))):
        return "citation"
    return None


def _amount_basis_outcome(claim, passages):
    """Return ``(hold_reason, publish_currency)``.

    Value and unit gate the amount. An open currency judgment clears the
    currency label and leaves the supported scale in place. Responses with no
    basis keep the model's own currency. This does not read reason text or
    search passages for numbers.
    """
    amount = claim.get("amount")
    if not isinstance(amount, dict) or "basis" not in amount:
        return None, True
    basis = amount.get("basis")
    if not isinstance(basis, dict) or any(part not in basis for part in _AMOUNT_BASIS_PARTS):
        return "amount_basis_invalid", False
    issues = {part: _basis_component_issue(basis.get(part), claim, passages) for part in _AMOUNT_BASIS_PARTS}
    for part in ("value", "unit"):
        issue = issues[part]
        if issue == "invalid":
            return "amount_basis_invalid", False
        if issue == "not_supported":
            return "amount_basis_not_supported", False
        if issue == "citation":
            return "amount_basis_citation_unverified", False
    return None, issues["currency"] is None


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


def _reviewed_value(claims, ids, field):
    """Read one explicit typed judgment shared by extraction and stored reads."""
    values = []
    for cid in ids:
        current = claims[cid].get("field_values", {})
        if field.split("/")[0] == "comparison":
            comparison, reason = _comparison_values(claims[cid])
            if reason:
                return False, None
            if comparison is not None:
                current = {**current, "comparison": comparison}
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
        for value in values:
            if field.split("/")[0] == "comparison":
                valid, combined = _merge_comparison_members(combined, value)
                if not valid:
                    return False, None
                continue
            for key, item in value.items():
                if key in combined and combined[key] != item:
                    return False, None
                combined[key] = item
        if field == "comparison" and _comparison_inconsistent(combined):
            return False, None
        return True, deepcopy(combined)
    equal = _same_comparison_value if field.split("/")[0] == "comparison" else lambda a, b: a == b
    return (True, deepcopy(values[0])) if all(equal(v, values[0]) for v in values) else (False, None)


def _same_comparison_value(left, right):
    # JSON booleans are judgments, not the numbers zero/one.
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_same_comparison_value(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same_comparison_value(a, b) for a, b in zip(left, right))
    return left == right


def _merge_comparison_members(left, right):
    result = deepcopy(left)
    for key, value in right.items():
        if key in result:
            if isinstance(result[key], dict) and isinstance(value, dict):
                valid, value = _merge_comparison_members(result[key], value)
                if not valid:
                    return False, None
            elif not _same_comparison_value(result[key], value):
                return False, None
        result[key] = deepcopy(value)
    return True, result


def _comparison_inconsistent(value):
    return any(value.get(key) is not None and value.get(key + "_status") == "not_applicable"
               for key in COMPARISON_RATE_UNITS)


def _comparison_leaves(value, prefix=()):
    for key, item in value.items():
        parts = (*prefix, key)
        if isinstance(item, dict) and item:
            yield from _comparison_leaves(item, parts)
        else:
            yield parts, item


def _comparison_explicit_conflict(proposed, approved, parts=()):
    """Omission is fillable; an explicitly different value is not."""
    if isinstance(approved, dict) and isinstance(proposed, dict):
        return any(key in proposed and _comparison_explicit_conflict(proposed[key], value, (*parts, key))
                   for key, value in approved.items())
    if len(parts) == 1 and parts[0] in COMPARISON_RATE_UNITS and approved is not None:
        return _finite_number(proposed) != _finite_number(approved)
    return not _same_comparison_value(proposed, approved)


def _original_claim(claim):
    return {key: value for key, value in claim.items() if key not in {
        "effective_status", "effective_reason", "dependency_status", "dependency_reason"}}


def _scope_items(scope, prefix="scope"):
    """Walk protocol member names; never infer dimensions from source text."""
    if not isinstance(scope, dict):
        return
    for key, value in scope.items():
        if prefix == "scope" and key in SCOPE_DERIVED_FIELDS:
            continue
        path = prefix + "/" + key
        if isinstance(value, dict):
            yield from _scope_items(value, path)
        elif _present(value):
            yield path, value


def _scope_judgment(claims, ids, field, value, states=None):
    if not ids or any(cid not in claims or claims[cid].get("kind") != "observation"
                      or (states.get(cid) if states is not None else
                          claims[cid].get("effective_status")) != "source_supported" for cid in ids):
        return False
    valid, approved = _reviewed_value(claims, ids, field)
    return valid and approved == value


def _nearest_field(fields, path):
    while path:
        if path in fields:
            value = fields[path]
            return value if isinstance(value, dict) else {}
        path = path.rpartition("/")[0]
    return {}


def _stored_scope_projection(record):
    scope = deepcopy(record.get("scope") or {})
    audit = record.get("content_review") or {}
    audit = audit if isinstance(audit, dict) else {}
    claims, fields = audit.get("claims", {}), audit.get("fields", {})
    claims = claims if isinstance(claims, dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    invalid, blocked = [], set()
    for path, value in _scope_items(scope):
        check = _nearest_field(fields, path)
        ids = check.get("claim_ids", [])
        valid = (audit.get("protocol") == PROTOCOL
                 and check.get("scope_review") == SCOPE_REVIEW_PROTOCOL
                 and check.get("status") == "source_supported" and isinstance(ids, list)
                 and all(isinstance(cid, str) for cid in ids)
                 and _scope_judgment(claims, ids, path, value))
        if not valid:
            invalid.append(path)
            blocked.update(cid for cid in ids if isinstance(cid, str))
            parent = scope
            parts = path.split("/")[1:]
            for part in parts[:-1]:
                parent = parent[part]
            parent[parts[-1]] = None
    if invalid:
        # A claim-level dependency cannot say whether it needs the number or
        # the dimension. Preserve the number, but do not export this premise.
        for field in CORE_FIELDS["metrics"]:
            blocked.update(cid for cid in _nearest_field(fields, field).get("claim_ids", [])
                           if isinstance(cid, str))
    return scope, invalid, {cid: _original_claim(claims[cid]) for cid in blocked
                            if isinstance(claims.get(cid), dict)}


def _scope_review_rejections(review, records=()):
    records = list(records)
    if not any(isinstance(leaf.get("model_review"), dict) and leaf["model_review"].get("claims")
               for leaf in _coverage_leaves(review)):
        # A legacy/orphan row can still carry its original local claims. Use
        # that exact graph when the merged review is unavailable, never invent
        # new claim links or semantically approve the old record.
        review = {"contributions": [{"model_review": {"claims": [
            _original_claim(claim) for claim in (record.get("content_review") or {}).get("claims", {}).values()
            if isinstance(claim, dict)], "bindings": {}}} for record in records]}
    seeds = [_stored_scope_projection(record)[2] for record in records]
    materialized = [claim for record in records for claim in (record.get("content_review") or {}).get("claims", {}).values()
                    if isinstance(claim, dict)]
    results = []
    for leaf in _coverage_leaves(review or {}):
        raw = leaf.get("model_review", {})
        claims = {c["id"]: c for c in raw.get("claims", [])
                  if isinstance(c, dict) and isinstance(c.get("id"), str)}
        blocked = {cid for seed in seeds for cid, original in seed.items()
                   if claims.get(cid) == original}
        blocked.update(cid for cid in leaf.get("scope_dependency_blocks", []) if cid in claims)
        if leaf.get("scope_review") != SCOPE_REVIEW_PROTOCOL:
            # An incomplete old cache has no trustworthy dimension projection.
            # Do not let deletion/missing metric rows authorize retained text.
            metric_ids = {cid for path, ids in raw.get("bindings", {}).items()
                          if isinstance(path, str) and path.startswith("/metrics/") and isinstance(ids, list)
                          for cid in ids if isinstance(cid, str) and claims.get(cid, {}).get("kind") == "observation"}
            blocked.update(cid for cid in metric_ids
                           if not any(_original_claim(row) == claims[cid] for row in materialized))
        dependents = set()
        while True:
            expanded = dependents | {cid for cid, claim in claims.items()
                if isinstance(claim.get("depends_on", []), list)
                and any(dep in blocked | dependents for dep in claim.get("depends_on", [])
                        if isinstance(dep, str))}
            if expanded == dependents:
                break
            dependents = expanded
        results.append((claims, blocked, dependents))
    return results


def cache_scopes_current(review, records=()):
    """An old bundle cannot reauthorize a dimension through its cached text."""
    records = list(records)
    if any(_stored_scope_projection(record)[1] for record in records):
        return False
    leaves = _coverage_leaves(review)
    return all((index < len(leaves) and leaves[index].get("scope_review") == SCOPE_REVIEW_PROTOCOL) or not blocked
               for index, (_, blocked, _) in enumerate(_scope_review_rejections(review, records)))


def enforce_scope_coverage(review, records=()):
    """Project legacy coverage claims without rewriting their original audit."""
    result = deepcopy(review)
    if not isinstance(result, dict):
        return result
    invalid = {index: blocked | dependents for index, (_, blocked, dependents)
               in enumerate(_scope_review_rejections(review, records))}
    previous = deepcopy(result.get("task_coverage", []))
    changed = False
    for row in result.get("task_coverage", []):
        refs = row.get("answer_claim_refs", [])
        rejected = (any(isinstance(ref, dict) and ref.get("claim_id") in invalid.get(ref.get("contribution_index"), set())
                        for ref in refs) if refs else
                    any(cid in blocked for cid in row.get("answer_claim_ids", []) for blocked in invalid.values()))
        if rejected:
            for assessment in row.get("assessments", []):
                if any(cid in invalid.get(assessment.get("contribution_index"), set())
                       for cid in assessment.get("answer_claim_ids", [])):
                    assessment.update(status="not_assessed", answer_claim_ids=[], uncovered_aspects=[],
                                      reason="scope_dependency_not_supported")
            row.update(status="not_assessed", answer_claim_ids=[], answer_claim_refs=[],
                       uncovered_aspects=[], reason="scope_dependency_not_supported")
            changed = True
    if changed:
        result["scope_coverage_correction"] = {"status": "pending_review", "original_task_coverage": previous,
                                                "reason": "scope_dependency_not_supported"}
    return result


def _scope_numeric_field(group, field):
    return group == "metrics" and field.split("/")[0] in {*CORE_FIELDS["metrics"], "period", "scope"}


def enforce_reviewed_scope(group, record, review=None, *, scope_records=()):
    """Shared conservative read view; never rewrite SQL or forge a new seal."""
    result = deepcopy(record)
    if group == "metrics":
        scope, invalid, _ = _stored_scope_projection(result)
        if invalid:
            # Failed optional dimensions do not erase independently valid data.
            _record_projection_correction(result, "scope", scope,
                                          "stored_scope_review_missing_or_conflicting")
    audit = result.get("content_review") or {}
    local = audit.get("claims", {})
    rejected, blocked = set(), set()
    context = list(scope_records) + ([record] if group == "metrics" else [])
    for originals, seeds, dependents in _scope_review_rejections(review, context):
        rejected.update(cid for cid in dependents if isinstance(local.get(cid), dict)
                        and _original_claim(local[cid]) == originals[cid])
        blocked.update(cid for cid in seeds if isinstance(local.get(cid), dict)
                       and _original_claim(local[cid]) == originals[cid])
    for name, check in audit.get("fields", {}).items():
        if isinstance(check, dict) and any(cid in rejected or
                cid in blocked and not _scope_numeric_field(group, name) for cid in check.get("claim_ids", [])):
            field = name.split("/")[0]
            old = result.get(field)
            if not _present(old):
                continue
            unknown = {} if isinstance(old, dict) else [] if isinstance(old, list) else "" if isinstance(old, str) else None
            unknown = REVIEWED_DEFAULTS.get(group, {}).get(field, unknown)
            _record_projection_correction(result, field, unknown, "stored_scope_dependency_not_supported")
    if group == "metrics" and any(local[cid].get("kind") == "comparability" for cid in rejected):
        scope = result.get("scope", {})
        if isinstance(scope, dict) and scope.get("usable_as_price_benchmark") is True:
            safe = {**scope, "usable_as_price_benchmark": False, "source_price_basis_status": "pending_review"}
            _record_projection_correction(result, "scope", safe, "stored_scope_dependency_not_supported")
    return result


def _evidence_within(quotes, evidence):
    """Exact continuous subquotes of already verified claim evidence only."""
    return (isinstance(quotes, list) and bool(quotes) and isinstance(evidence, list)
            and all(isinstance(q, dict) and isinstance(q.get("passage_id"), str)
                    and bool(q["passage_id"]) and q["passage_id"] != "title"
                    and isinstance(q.get("quote"), str) and bool(q["quote"].strip())
                    and any(isinstance(parent, dict) and parent.get("passage_id") == q["passage_id"]
                            and isinstance(parent.get("quote"), str) and q["quote"] in parent["quote"]
                            for parent in evidence) for q in quotes))


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except DecimalException:
        return None


def _comparison_values(claim):
    """Resolve explicit typed inputs, without altering model claims or statuses.

    An omitted output is derived. An explicitly supplied conflicting output is
    still rejected, including historical dual-value responses.
    """
    values = claim.get("field_values", {})
    if not isinstance(values, dict):
        return None, None  # invalid_claim is handled by the shared state check
    if "comparison" not in values and "comparison_input" not in values:
        return None, None
    comparison = values.get("comparison", {})
    inputs = values.get("comparison_input", {})
    if not isinstance(comparison, dict) or not isinstance(inputs, dict):
        return None, "comparison_input_missing_or_invalid"
    if (comparison or inputs) and claim.get("kind") not in field_kinds("metrics", "comparison"):
        return None, "comparison_claim_kind_invalid"
    if any(key not in COMPARISON_RATE_UNITS for key in inputs):
        return None, "comparison_input_member_invalid"
    resolved = deepcopy(comparison)
    for key, target_unit in COMPARISON_RATE_UNITS.items():
        if key not in inputs and comparison.get(key) is None:
            continue  # null / N/A semantics still use the existing review contract
        if key in inputs and comparison.get(key + "_status") == "not_applicable":
            return None, "comparison_value_conflicting"
        if claim.get("kind") != "comparability":
            return None, "comparison_claim_kind_invalid"
        item = inputs.get(key)
        if not isinstance(item, dict):
            return None, "comparison_input_missing_or_invalid"
        number = _finite_number(item.get("value"))
        if number is None:
            return None, "comparison_input_missing_or_invalid"
        factors = {"ratio": Decimal(1), "percent": Decimal("0.01"),
                   "percentage_point": Decimal("0.01"), "basis_point": Decimal("0.0001")}
        unit = item.get("unit")
        if not isinstance(unit, str) or unit not in factors:
            return None, "comparison_unit_missing_or_invalid"
        # Percentage points measure a difference, not a relative growth rate.
        if target_unit == "ratio" and unit == "percentage_point":
            return None, "comparison_unit_incompatible"
        if target_unit == "percentage_point" and unit == "percent":
            return None, "comparison_unit_incompatible"
        if not _evidence_within(item.get("evidence"), claim.get("evidence")):
            return None, "comparison_evidence_missing_or_unverified"
        try:
            expected = number * factors[unit]
            if target_unit == "percentage_point":
                expected *= 100
            output = float(expected)
        except (DecimalException, OverflowError, ValueError):
            return None, "comparison_input_missing_or_invalid"
        # Do not silently overflow, underflow or round an unrepresentable input.
        if _finite_number(output) != expected:
            return None, "comparison_input_missing_or_invalid"
        if key in comparison:
            explicit = _finite_number(comparison[key])
            if explicit is None:
                return None, "comparison_input_missing_or_invalid"
            if explicit != expected:
                return None, "comparison_value_conflicting"
        resolved[key] = output
    return resolved, None


def _comparison_claim_reason(claim):
    return _comparison_values(claim)[1]


def _date_assessment_reason(assessment, allowed_roles, evidence):
    """Validate an explicit model date judgment, never infer its semantic role."""
    if not isinstance(assessment, dict) or assessment.get("protocol") != DATE_REVIEW_PROTOCOL:
        return "date_assessment_missing_or_invalid"
    roles = assessment.get("possible_roles")
    role, basis = assessment.get("selected_role"), assessment.get("basis")
    if (not isinstance(roles, list) or len(roles) != 1
            or not isinstance(role, str) or role not in DATE_ROLES
            or roles != [role] or role not in allowed_roles):
        return "date_role_ambiguous_or_incompatible"
    if not isinstance(basis, str) or basis not in DATE_BASES or basis not in DATE_ROLE_BASES.get(role, set()):
        return "date_basis_incompatible"
    quotes = assessment.get("evidence")
    if not _evidence_within(quotes, evidence):
        return "date_evidence_missing_or_unverified"
    return None


def _publication_claim_reason(claim):
    values = claim.get("field_values", {})
    value = values.get("source_publication") if isinstance(values, dict) else None
    if claim.get("kind") != "observation":
        return "publication_claim_kind_invalid"
    if not isinstance(value, dict) or value.get("date_role") != "source_publication":
        return "publication_role_missing_or_invalid"
    if parse_published(value.get("published_at")) is None:
        return "invalid_publication_date"
    return _date_assessment_reason(value.get("assessment"), {"source_publication"}, claim.get("evidence"))


def _event_date_judgment(claims, ids, value, *, stored_utc=False):
    """A record observation may authorize a date only with its explicit value."""
    proposed = parse_published(value)
    if proposed is None:
        return None, "invalid_event_date"
    assessments = []
    for cid in ids:
        claim = claims.get(cid, {})
        values = claim.get("field_values", {})
        if not isinstance(values, dict) or "event_date" not in values:
            continue
        date = parse_published(values.get("event_date"))
        same_date = date is not None and date[0] == proposed[0]
        if date is not None and stored_utc:
            # Repository DateTime storage normalizes explicit offsets to UTC,
            # while date-only and naive inputs retain their wall time as UTC.
            try:
                stored = datetime.fromisoformat(value.replace("Z", "+00:00"))
                original = datetime.fromisoformat(values["event_date"].replace("Z", "+00:00"))
                stored = stored.replace(tzinfo=timezone.utc) if stored.tzinfo is None else stored
                original = original.replace(tzinfo=timezone.utc) if original.tzinfo is None else original
                same_date = stored == original
            except (ValueError, TypeError, AttributeError):
                same_date = False
        if not same_date:
            return None, "reviewed_event_date_missing_or_conflicting"
        assessment = values.get("event_date_assessment")
        reason = _date_assessment_reason(assessment, {"source_publication", "event_occurrence"}, claim.get("evidence"))
        if reason:
            return None, reason
        assessments.append({"claim_id": cid, "assessment": deepcopy(assessment)})
    return ((assessments, None) if assessments else (None, "reviewed_event_date_missing_or_conflicting"))


def _record_projection_correction(result, field, value, reason):
    audit = result.get("content_review")
    audit = audit if isinstance(audit, dict) else {}
    previous = audit.get("materialization_corrections", [])
    audit["materialization_corrections"] = (previous if isinstance(previous, list) else []) + [{
        "field": field, "original_materialized_value": result.get(field),
        "published_value": value, "status": "pending_review", "reason": reason}]
    old_digest = audit.pop("materialized_digest", None)
    if old_digest is not None:
        audit["prior_materialized_digest"] = old_digest
    result[field] = value
    result["content_review"] = audit


def enforce_reviewed_dates(group, record, *, stored_utc=False):
    """Project stored event dates safely; retain original audit, never reapprove."""
    result = deepcopy(record)
    if group != "events" or not _present(result.get("event_date")):
        return result
    audit = result.get("content_review")
    audit = audit if isinstance(audit, dict) else {}
    fields, claims = audit.get("fields", {}), audit.get("claims", {})
    fields = fields if isinstance(fields, dict) else {}
    claims = claims if isinstance(claims, dict) else {}
    check = fields.get("event_date", {})
    check = check if isinstance(check, dict) else {}
    ids = check.get("claim_ids", [])
    supported = (audit.get("protocol") == PROTOCOL and check.get("date_review") == DATE_REVIEW_PROTOCOL
                 and check.get("status") == "source_supported" and isinstance(ids, list) and bool(ids)
                 and all(isinstance(cid, str) and isinstance(claims.get(cid), dict)
                         and claims[cid].get("kind") == "observation"
                         and claims[cid].get("effective_status") == "source_supported" for cid in ids))
    value = result["event_date"]
    # DB datetime serialization can add a zero time to a model's date-only value.
    value = value.isoformat() if hasattr(value, "isoformat") else value
    _, reason = (_event_date_judgment(claims, ids, value, stored_utc=stored_utc)
                 if supported else (None, "stored_date_review_missing_or_unverified"))
    if reason:
        _record_projection_correction(result, "event_date", None, reason)
    return result


def enforce_source_publication_dates(publication):
    """Reject legacy model-date approvals without changing publisher metadata."""
    result = deepcopy(publication)
    if not isinstance(result, dict) or result.get("source") != "model_content_review" or result.get("status") != "known":
        return result
    assessments = result.get("date_assessments")
    valid = (result.get("date_review") == DATE_REVIEW_PROTOCOL and isinstance(assessments, list)
             and bool(assessments) and parse_published(result.get("published_at")) is not None)
    if valid:
        valid = all(isinstance(row, dict) and _date_assessment_reason(
            row.get("assessment"), {"source_publication"}, result.get("evidence")) is None
            and parse_published(row.get("published_at")) is not None
            for row in assessments)
    if valid:
        valid = _publication_agreement([result, *assessments]) is not None
    if not valid:
        result["date_review_correction"] = {"original_materialized_value": deepcopy(publication),
                                            "status": "pending_review", "reason": "stored_date_review_missing_or_unverified"}
        result.update(status="unknown", published_at=None, precision=None,
                      reason="stored_date_review_missing_or_unverified")
        # Never retain the prior time-window admission alongside an unknown date.
        if "time_window" in result:
            result["time_window"] = {"status": "publication_time_unverified", "allowed": False,
                                     "reason": "stored_date_review_missing_or_unverified"}
    return result


def enforce_reviewed_defaults(group, record):
    """Conservatively project old defaults at read/cache boundaries, without writes.

    Original review/held values stay in the audit. A new correction describes
    any legacy materialized value withheld from the formal projection. Missing
    review is never upgraded to source support or to the current protocol.
    """
    result = deepcopy(record)
    defaults = REVIEWED_DEFAULTS.get(group, {})
    audit = result.get("content_review") or {}
    audit = audit if isinstance(audit, dict) else {}
    fields = audit.get("fields", {})
    fields = fields if isinstance(fields, dict) else {}
    claims = audit.get("claims", {})
    claims = claims if isinstance(claims, dict) else {}
    corrections = []
    for field, default in defaults.items():
        value = result.get(field)
        if value == default:
            continue
        check = fields.get(field, {})
        check = check if isinstance(check, dict) else {}
        ids = check.get("claim_ids", [])
        supported = (audit.get("protocol") == PROTOCOL
                     and check.get("status") == "source_supported"
                     and isinstance(ids, list) and bool(ids)
                     and all(isinstance(cid, str) and isinstance(claims.get(cid), dict)
                             and claims[cid].get("effective_status") == "source_supported"
                             and isinstance(claims[cid].get("kind"), str)
                             and claims[cid].get("kind") in field_kinds(group, field) for cid in ids))
        valid, approved = _reviewed_value(claims, ids, field) if supported else (False, None)
        if valid and approved == value:
            continue
        result[field] = default
        corrections.append({"field": field, "original_materialized_value": value,
                            "published_value": default, "status": "pending_review",
                            "reason": "stored_field_review_missing_or_unverified"})
    if corrections:
        previous = audit.get("materialization_corrections", [])
        audit["materialization_corrections"] = (previous if isinstance(previous, list) else []) + corrections
        # The old seal describes the old payload, not this conservative view.
        # Keep it for audit without forging a new content-review authorization.
        old_digest = audit.pop("materialized_digest", None)
        if old_digest is not None:
            audit["prior_materialized_digest"] = old_digest
        result["content_review"] = audit
    if defaults and audit.get("protocol") != PROTOCOL:
        audit["read_review_status"] = "legacy_unreviewed"
        result["content_review"] = audit
    return result


def _comparison_review_rejections(review):
    """Return scoped legacy rejections, including their dependent claims."""
    results = []
    for leaf in _coverage_leaves(review or {}):
        raw = leaf.get("model_review", {})
        rows = raw.get("claims", []) if isinstance(raw, dict) else []
        rows = rows if isinstance(rows, list) else []
        claims = {c["id"]: c for c in rows if isinstance(c, dict) and isinstance(c.get("id"), str)}
        states = leaf.get("claims", {})
        states = states if isinstance(states, dict) else {}
        rejected = {cid for cid, claim in claims.items()
                    if isinstance(states.get(cid), dict) and states[cid].get("status") == "source_supported"
                    and _comparison_claim_reason(claim)}
        while True:
            dependents = {cid for cid, claim in claims.items()
                          if isinstance(claim.get("depends_on", []), list)
                          and any(dep in rejected for dep in claim.get("depends_on", []) if isinstance(dep, str))}
            expanded = rejected | dependents
            if expanded == rejected:
                break
            rejected = expanded
        results.append((claims, rejected))
    return results


def cache_comparisons_current(review):
    """Reject old approved numerical comparisons without re-authoring audit."""
    return not any(rejected for _, rejected in _comparison_review_rejections(review))


def enforce_reviewed_comparisons(group, record, review=None):
    """Read-side projection; preserve original claims/seal and valid amounts.

    The merged review supplies dependent claims absent from a record's local
    audit. Exact original claim matching keeps contribution-local IDs scoped.
    """
    result = deepcopy(record)
    audit = result.get("content_review")
    audit = audit if isinstance(audit, dict) else {}
    claims = audit.get("claims", {})
    claims = claims if isinstance(claims, dict) else {}
    fields = audit.get("fields", {})
    fields = fields if isinstance(fields, dict) else {}
    rejected = {cid for cid, claim in claims.items() if isinstance(claim, dict)
                and claim.get("effective_status") == "source_supported" and _comparison_claim_reason(claim)}
    for originals, invalid in _comparison_review_rejections(review):
        for cid in invalid:
            local = claims.get(cid)
            if isinstance(local, dict) and _original_claim(local) == originals[cid]:
                rejected.add(cid)
    corrections = set()
    for path, field in fields.items():
        if not isinstance(field, dict) or not isinstance(field.get("claim_ids"), list):
            continue
        if any(cid in rejected for cid in field["claim_ids"] if isinstance(cid, str)):
            corrections.add(path.split("/")[0])
    comparison = result.get("comparison")
    if group == "metrics" and isinstance(comparison, dict):
        if _comparison_inconsistent(comparison):
            corrections.add("comparison")
        for parts, candidate in _comparison_leaves(comparison):
            if not _present(candidate):
                continue
            path = "comparison/" + "/".join(parts)
            field = _nearest_field(fields, path)
            ids = field.get("claim_ids", [])
            valid = (audit.get("protocol") == PROTOCOL and field.get("status") == "source_supported"
                     and isinstance(ids, list) and bool(ids)
                     and all(isinstance(cid, str) and isinstance(claims.get(cid), dict)
                             and claims[cid].get("effective_status") == "source_supported"
                             and claims[cid].get("kind") in field_kinds("metrics", "comparison")
                             and not _comparison_claim_reason(claims[cid]) for cid in ids))
            found, value = _reviewed_value(claims, ids, path) if valid else (False, None)
            if not found or not _same_comparison_value(value, candidate):
                corrections.add("comparison")
    for field in sorted(corrections):
        if field not in result or not _present(result[field]):
            continue
        old = result[field]
        unknown = {} if isinstance(old, dict) else [] if isinstance(old, list) else "" if isinstance(old, str) else None
        unknown = REVIEWED_DEFAULTS.get(group, {}).get(field, unknown)
        _record_projection_correction(result, field, unknown, "stored_comparison_review_missing_or_conflicting")
    return result


def seal(bundle):
    for group in ("brief", *GROUPS):
        objects = [bundle.brief] if group == "brief" else getattr(bundle, group)
        for obj in objects:
            if obj.content_review.get("protocol") == PROTOCOL:
                obj.content_review["materialized_digest"] = _digest(obj)


def _publication_agreement(candidates):
    """Compare parsed dates only; their semantic role was reviewed by the model."""
    parsed = [(candidate, parse_published(candidate.get("published_at")))
              for candidate in candidates]
    if not parsed or any(value is None for _, value in parsed):
        return None
    days = {value[0].astimezone(LOCAL_TZ).date() for _, value in parsed}
    precise = [(candidate, value[0]) for candidate, value in parsed
               if candidate.get("precision", value[1]) == "second"]
    if len(days) != 1 or len({stamp for _, stamp in precise}) > 1:
        return None
    # A date-only citation is compatible with a timestamp on that local date.
    return deepcopy(precise[0][0] if precise else parsed[0][0])


class ContentReview:
    def __init__(self, bundle, passages, warnings):
        self.bundle = bundle
        self.proposed = bundle.model_dump(mode="json")
        self.warnings = warnings
        self.passages = {p.passage_id: p.text for p in passages}
        self.index_passages = {p.passage_id for p in passages if p.section in INDEX_PASSAGE_SECTIONS}
        self.index_entries = {p.passage_id for p in passages if p.section == "index_entry"}
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
        self.currency_withheld = set()
        self.held = []
        self.fields = {}
        self.date_rejections = {}
        self.scope_dependency_blocks = set()
        self.publication_conflict = None
        self.comparison_rejections = {}
        for cid in self.claims:
            self.state(cid)
        # Check coupled rate/status members even when bound through different
        # child paths. Only supported claims contribute; no prose interpretation.
        for index, metric in enumerate(self.proposed.get("metrics", [])):
            path = f"/metrics/{index}/comparison"
            candidate, _ = self.comparison_candidates(path, metric.get("comparison", {}))
            for key in COMPARISON_RATE_UNITS:
                if candidate.get(key) is not None and candidate.get(key + "_status") == "not_applicable":
                    for member in (key, key + "_status"):
                        for cid in self.comparison_ids(path, (member,)):
                            self.comparison_rejections[cid] = "comparison_materialized_value_conflicting"
        if self.comparison_rejections:
            self.states.clear()
            self.reasons.clear()
            for cid in self.claims:
                self.state(cid)
        for index, metric in enumerate(self.proposed.get("metrics", [])):
            path = f"/metrics/{index}"
            record_ids = self.ids(path)
            for field, value in _scope_items(metric.get("scope")):
                ids = self.scope_ids(path, field, record_ids)
                if not _scope_judgment(self.claims, ids, field, value, self.states):
                    self.scope_dependency_blocks.update(ids)
                    self.scope_dependency_blocks.update(record_ids)
                    for core in CORE_FIELDS["metrics"]:
                        self.scope_dependency_blocks.update(self.ids(path + "/" + core, record_ids))
        if self.scope_dependency_blocks:
            self.states.clear()
            self.reasons.clear()
            for cid in self.claims:
                self.state(cid)
        # Resolve contradictory publication dates before materializing any brief
        # or record that depends on them. The original claims remain in raw audit.
        publication = self.source_publication()
        if publication.get("status") == "conflict":
            self.publication_conflict = publication
            self.date_rejections = {cid: "conflicting_publication_dates"
                                    for cid in publication["claim_ids"]}
            self.states.clear()
            self.reasons.clear()
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
        if state == "source_supported":
            hold_reason, publish_currency = _amount_basis_outcome(claim, self.passages)
            if hold_reason:
                state, reason = "pending_review", hold_reason
            elif not publish_currency:
                self.currency_withheld.add(cid)
        if state == "source_supported":
            comparison_reason = (self.comparison_rejections.get(cid) or _comparison_claim_reason(claim)
                                 or self.comparison_output_reason(cid))
            if comparison_reason:
                state, reason = "pending_review", comparison_reason
        publication_ids = self.bindings.get("/source_publication", [])
        values = claim.get("field_values", {})
        publication_claim = ((isinstance(publication_ids, list) and cid in publication_ids)
                             or isinstance(values, dict) and "source_publication" in values)
        if state == "source_supported" and publication_claim:
            date_reason = (self.date_rejections.get(cid)
                           or ("index_entry_not_source_publication"
                               if self.evidence_passage_ids([cid]) & self.index_entries else None)
                           or _publication_claim_reason(claim))
            if date_reason:
                state, reason = "pending_review", date_reason
        if any(self.state(dep, (*visiting, cid)) != "source_supported"
               or dep in self.scope_dependency_blocks for dep in dependencies):
            if state != "unsupported":
                state, reason = "pending_review", ("scope_dependency_not_supported"
                    if any(dep in self.scope_dependency_blocks for dep in dependencies) else "dependency_not_supported")
        self.states[cid], self.reasons[cid] = state, reason
        return state

    def comparison_output_reason(self, cid):
        """Apply the same conflict check to numbers, statuses and nested values."""
        comparison, reason = _comparison_values(self.claims[cid])
        if reason:
            return reason
        if not isinstance(comparison, dict) or not comparison:
            return None
        for path, ids in self.bindings.items():
            if not isinstance(path, str) or not isinstance(ids, list) or cid not in ids:
                continue
            parts = path.strip("/").split("/")
            if len(parts) < 3 or parts[0] != "metrics" or parts[2] != "comparison":
                continue
            # Multiple claims at one binding must agree on overlapping members.
            if any(other not in self.claims for other in ids):
                return "comparison_materialized_value_conflicting"
            valid, approved = self.reviewed_value(ids, "/".join(parts[2:]))
            if not valid:
                return "comparison_materialized_value_conflicting"
            missing = object()
            output = self.proposed
            output_comparison = missing
            for index, part in enumerate(parts):
                if isinstance(output, list) and part.isdigit() and int(part) < len(output):
                    output = output[int(part)]
                else:
                    if output is not missing and not isinstance(output, dict):
                        return "comparison_materialized_value_conflicting"
                    output = output.get(part, missing) if isinstance(output, dict) else missing
                if index == 2:
                    output_comparison = output
            if output is not missing and _comparison_explicit_conflict(output, approved, tuple(parts[3:])):
                return "comparison_materialized_value_conflicting"
            if isinstance(output_comparison, dict):
                for key in COMPARISON_RATE_UNITS:
                    if (comparison.get(key) is not None and output_comparison.get(key + "_status") == "not_applicable"
                            or comparison.get(key + "_status") == "not_applicable" and output_comparison.get(key) is not None):
                        return "comparison_materialized_value_conflicting"
        return None

    def comparison_ids(self, path, parts):
        ids = self.ids(path)
        for part in parts:
            path += "/" + part
            ids = self.ids(path, ids)
        return ids

    def comparison_candidates(self, path, value):
        """Materialize all explicitly bound comparison members through one path."""
        result, conversions = deepcopy(value), {}
        members = set()
        for binding, ids in self.bindings.items():
            if not isinstance(binding, str) or not (binding == path or binding.startswith(path + "/")):
                continue
            suffix = tuple(binding[len(path):].strip("/").split("/")) if binding != path else ()
            for cid in self.ids(binding):
                comparison, reason = _comparison_values(self.claims.get(cid, {}))
                if reason or comparison is None:
                    continue
                members.update(parts for parts, _ in _comparison_leaves(comparison)
                               if parts[:len(suffix)] == suffix)
        for parts in sorted(members):
            ids = self.comparison_ids(path, parts)
            if not ids or any(cid not in self.claims or self.state(cid) != "source_supported"
                              or self.claims[cid].get("kind") not in field_kinds("metrics", "comparison") for cid in ids):
                continue
            valid, approved = self.reviewed_value(ids, "comparison/" + "/".join(parts))
            if not valid:
                continue
            parent = result
            for part in parts[:-1]:
                if not isinstance(parent, dict):
                    break
                parent = parent.setdefault(part, {})
            key = parts[-1]
            if not isinstance(parent, dict) or key in parent:
                continue
            parent[key] = deepcopy(approved)
            if len(parts) == 1 and key in COMPARISON_RATE_UNITS and approved is not None:
                conversions[key] = {"output_value": approved, "output_unit": COMPARISON_RATE_UNITS[key],
                    "inputs": [{"claim_id": cid, **{k: item[k] for k in ("value", "unit")}}
                               for cid in ids
                               if isinstance(item := self.claims[cid].get("field_values", {}).get(
                                   "comparison_input", {}).get(key), dict)]}
        return result, conversions

    def ids(self, path, inherited=None):
        value = self.bindings.get(path, inherited)
        if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
            return []
        return list(dict.fromkeys(value))

    def scope_ids(self, record_path, field, inherited=None):
        ids = inherited
        path = record_path
        for part in field.split("/"):
            path += "/" + part
            ids = self.ids(path, ids)
        return ids or []

    def scope_field(self, path, value, inherited=None):
        result = {key: deepcopy(item) for key, item in value.items() if key not in SCOPE_DERIVED_FIELDS}
        record_path = path.rpartition("/")[0]
        checks = []
        for field, candidate in _scope_items(value):
            ids = self.scope_ids(record_path, field, inherited)
            full_path = record_path + "/" + field
            valid = (_scope_judgment(self.claims, ids, field, candidate, self.states)
                     and not self.index_only(ids))
            self.fields[full_path] = {
                "claim_ids": ids, "states": {cid: self.state(cid) for cid in ids},
                "status": "source_supported" if valid else "pending_review",
                "scope_review": SCOPE_REVIEW_PROTOCOL}
            checks.append(self.fields[full_path])
            if not valid:
                self.hold(full_path, candidate, "reviewed_scope_missing_or_conflicting", ids)
                parent = result
                parts = field.split("/")[1:]
                for part in parts[:-1]:
                    parent = parent[part]
                parent[parts[-1]] = None
        ids = list(dict.fromkeys(cid for check in checks for cid in check["claim_ids"]))
        self.fields[path] = {"claim_ids": ids, "states": {cid: self.state(cid) for cid in ids},
                            "status": "source_supported" if all(check["status"] == "source_supported" for check in checks)
                            else "pending_review", "scope_review": SCOPE_REVIEW_PROTOCOL}
        benchmark_path = path + "/usable_as_price_benchmark"
        if benchmark_path in self.bindings:
            benchmark_ids = self.ids(benchmark_path)
            approved, value = self.reviewed_value(benchmark_ids, "scope/usable_as_price_benchmark") if all(
                cid in self.claims for cid in benchmark_ids) else (False, None)
            valid = (bool(benchmark_ids) and approved and value is True and all(
                self.claims[cid].get("kind") == "comparability" and self.state(cid) == "source_supported"
                for cid in benchmark_ids))
            self.fields[benchmark_path] = {"claim_ids": benchmark_ids,
                "states": {cid: self.state(cid) for cid in benchmark_ids},
                "status": "source_supported" if valid else "pending_review"}
        return result

    def hold(self, path, value, reason, ids=(), status="pending_review"):
        self.held.append({"path": path, "status": status, "reason": reason,
                          "claim_ids": list(ids), "original": deepcopy(value)})
        self.warnings.append(f"content review: {path}; {reason}; {status}")

    def reviewed_value(self, ids, field):
        """Materialize the model's explicit typed judgment, not a borrowed ID.

        JSON member names are protocol structure, never entity matching rules.
        All approved values of the same field must agree; no last-writer wins.
        """
        return _reviewed_value(self.claims, ids, field)

    def evidence_passage_ids(self, ids, visited=None):
        """Include dependencies so a derived date cannot hide index-entry evidence."""
        visited = set(visited or ())
        passages = set()
        for cid in ids:
            if cid in visited:
                continue
            visited.add(cid)
            claim = self.claims.get(cid, {})
            evidence = claim.get("evidence", [])
            if isinstance(evidence, list):
                passages.update(e.get("passage_id") for e in evidence
                                if isinstance(e, dict) and isinstance(e.get("passage_id"), str))
            dependencies = claim.get("depends_on", [])
            if isinstance(dependencies, list):
                passages.update(self.evidence_passage_ids(
                    [cid for cid in dependencies if isinstance(cid, str)], visited))
        return passages

    def index_only(self, ids):
        evidence = self.evidence_passage_ids(ids)
        return bool(evidence) and evidence <= self.index_passages

    def field(self, path, value, default, *, inherited=None, narrative=False, group="", field=""):
        ids = self.ids(path, inherited)
        if group == "metrics" and field == "scope" and isinstance(value, dict):
            return self.scope_field(path, value, inherited)
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
        if self.index_only(ids) and (
                group not in {"facts", "brief", "analyst_questions"}
                or group == "facts" and field.split("/")[0] in {"metrics", "direction"}
                or group == "brief" and field.split("/")[0] in {
                    "affected_business_lines", "impact_channels", "time_horizon"}):
            self.fields[path] = {"claim_ids": ids, "states": {cid: self.state(cid) for cid in ids},
                                 "status": "pending_review", "reason": "index_material_scope_only"}
            self.hold(path, value, "index_material_scope_only", ids)
            return deepcopy(default)
        kinds = field_kinds(group, field.split("/")[0])
        accepted = [cid for cid in ids if cid in self.claims and self.state(cid) == "source_supported"
                    and self.claims[cid].get("kind") in kinds
                    and (cid not in self.scope_dependency_blocks or _scope_numeric_field(group, field))]
        held = [cid for cid in ids if cid not in accepted]
        self.fields[path] = {"claim_ids": ids,
                             "states": {cid: self.state(cid) for cid in ids},
                             "status": "source_supported" if ids and not held else "pending_review"}
        if ids and not held:
            if narrative:
                return "；".join(dict.fromkeys(self.claims[cid]["statement"] for cid in accepted))
            # event_type is the occurrence judgment. A supported spec or summary claim
            # does not publish it; only that claim's explicit field_values.event_type does.
            if field.split("/")[0] == "event_type":
                valid, reviewed = self.reviewed_value(accepted, "event_type")
                if not valid or reviewed != value:
                    self.fields[path]["status"] = "pending_review"
                    self.hold(path, value, "reviewed_value_missing_or_conflicting", ids)
                    return deepcopy(default)
                return reviewed
            if group == "events" and field == "event_date":
                assessments, reason = _event_date_judgment(self.claims, accepted, value)
                self.fields[path]["date_review"] = DATE_REVIEW_PROTOCOL
                if reason:
                    self.fields[path].update(status="pending_review", reason=reason)
                    self.hold(path, value, reason, ids)
                    return deepcopy(default)
                self.fields[path]["date_assessments"] = assessments
                return deepcopy(value)
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
        if any(cid in self.scope_dependency_blocks for cid in held) and not _scope_numeric_field(group, field):
            reason = "scope_dependency_not_supported"
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
        conversions = {}
        if group == "metrics":
            before["comparison"], conversions = self.comparison_candidates(path + "/comparison", before["comparison"])
        if (group == "facts" and self.index_only(self.ids(path + "/fact_statement", record_ids))
                and before.get("fact_type") != INDEX_FACT_TYPE):
            self.hold(path, before, "index_fact_requires_document_listing", record_ids)
            return type(obj)()
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
            withheld_currency = [cid for cid in ids if cid in self.currency_withheld
                                 and self.state(cid) == "source_supported"]
            if withheld_currency and specifications:
                units = {spec.get("unit") for spec in specifications if isinstance(spec, dict)}
                if len(units) == 1 and None not in units:
                    out[name] = {**values, "currency": None, "amount_unit": next(iter(units)),
                                 "amount_currency_status": "pending_review",
                                 "amount_currency_reason": "currency_basis_not_supported"}
                    self.fields[money_path + "/amount"] = {
                        "claim_ids": ids, "states": {cid: self.state(cid) for cid in ids},
                        "status": "source_supported", "currency_status": "pending_review",
                        "reason": "currency_basis_not_supported"}
                    continue
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
        for key, conversion in conversions.items():
            if out.get("comparison", {}).get(key) != conversion["output_value"]:
                continue
            check = self.fields.get(path + "/comparison/" + key, self.fields.get(path + "/comparison"))
            if check and check.get("status") == "source_supported":
                check.setdefault("conversion", {})[key] = conversion
        audit = {"protocol": PROTOCOL,
                 "fields": {k[len(path) + 1:]: v for k, v in self.fields.items() if k.startswith(path + "/")},
                 "held_fields": deepcopy(self.held[start:])}
        ids = {cid for value in audit["fields"].values() for cid in value["claim_ids"]}
        audit["claims"] = {cid: {**self.claims[cid], "effective_status": self.state(cid),
                                 "effective_reason": self.reasons[cid],
                                 "dependency_status": "pending_review" if cid in self.scope_dependency_blocks else self.state(cid),
                                 "dependency_reason": "scope_review_incomplete" if cid in self.scope_dependency_blocks else self.reasons[cid]}
                           for cid in ids if cid in self.claims}
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
                if group == "events":
                    type_status = (reviewed.content_review.get("fields") or {}).get("event_type", {}).get("status")
                    if type_status is not None and type_status != "source_supported":
                        usable = False
                if usable:
                    kept.append(reviewed)
                else:
                    self.hold(path, obj.model_dump(mode="json"), "core_claim_not_supported")
            setattr(self.bundle, group, kept)
        self.bundle.content_review = {
            "protocol": PROTOCOL, "status": "applied", "model_review": self.raw,
            "review_revision": REVIEW_REVISION,
            "claims": {cid: {"status": self.state(cid), "reason": self.reasons[cid],
                             "dependency_status": "pending_review" if cid in self.scope_dependency_blocks else self.state(cid)}
                       for cid in self.claims}, "held": self.held,
            "scope_review": SCOPE_REVIEW_PROTOCOL,
            "scope_dependency_blocks": sorted(self.scope_dependency_blocks),
            "counts": dict(Counter(item["status"] for item in self.held)),
            "protocol_errors": [item for item in self.held if item["reason"] in {
                "missing_record_review", "claim_kind_mismatch", "reviewed_value_missing_or_conflicting",
                "unknown_claim_id", "invalid_claim", "date_assessment_missing_or_invalid",
                "reviewed_event_date_missing_or_conflicting", "date_evidence_missing_or_unverified"}],
            "source_publication": self.source_publication(),
        }
        return self.held

    def source_publication(self):
        """Materialize the explicit source-date role without reading date semantics."""
        if self.publication_conflict is not None:
            return deepcopy(self.publication_conflict)
        base = {"status": "unknown", "published_at": None, "precision": None,
                "source": "model_content_review", "claim_ids": [], "evidence": [],
                "date_review": DATE_REVIEW_PROTOCOL, "date_assessments": []}
        ids = self.bindings.get("/source_publication")
        if (not isinstance(ids, list) or not ids
                or any(not isinstance(cid, str) or not cid for cid in ids)
                or len(set(ids)) != len(ids)):
            return {**base, "reason": "publication_binding_missing_or_invalid"}
        base["claim_ids"] = list(ids)
        candidates, rejected = [], []
        for cid in ids:
            claim = self.claims.get(cid, {})
            if not claim:
                rejected.append({"claim_id": cid, "reason": "unknown_claim_id"})
                continue
            if self.state(cid) != "source_supported":
                rejected.append({"claim_id": cid, "reason": self.reasons.get(cid, "claim_not_supported")})
                continue
            if claim.get("kind") != "observation":
                rejected.append({"claim_id": cid, "reason": "publication_claim_kind_invalid"})
                continue
            if self.evidence_passage_ids([cid]) & self.index_entries:
                rejected.append({"claim_id": cid, "reason": "index_entry_not_source_publication"})
                continue
            value = claim.get("field_values", {}).get("source_publication")
            if not isinstance(value, dict) or value.get("date_role") != "source_publication":
                rejected.append({"claim_id": cid, "reason": "publication_role_missing_or_invalid"})
                continue
            date = parse_published(value.get("published_at"))
            if date is None:
                rejected.append({"claim_id": cid, "reason": "invalid_publication_date"})
                continue
            stamp, precision = date
            candidates.append({"status": "known", "published_at": stamp.isoformat(),
                               "precision": precision, "source": "model_content_review",
                               "date_review": DATE_REVIEW_PROTOCOL,
                               "date_assessments": [{"claim_id": cid, "published_at": stamp.isoformat(),
                                                     "assessment": deepcopy(value["assessment"])}],
                               "claim_ids": [cid], "evidence": deepcopy(claim["evidence"])})
        base["candidates"] = deepcopy(candidates)
        base["rejected_claims"] = rejected
        if len(candidates) > 1 and _publication_agreement(candidates) is None:
            return {**base, "status": "conflict", "reason": "conflicting_publication_dates"}
        if rejected:
            return {**base, "reason": "publication_claim_not_supported"}
        agreed = _publication_agreement(candidates)
        if agreed is None:
            return {**base, "reason": "publication_claim_not_supported"}
        evidence = []
        for candidate in candidates:
            for item in candidate["evidence"]:
                if item not in evidence:
                    evidence.append(item)
        return {**base, "status": "known", "published_at": agreed["published_at"],
                "precision": agreed["precision"], "evidence": evidence,
                "date_assessments": [row for candidate in candidates for row in candidate["date_assessments"]],
                "reason": "source_publication_claim_supported"}


def resolve_source_publication(bundles, extracted_publication):
    """Resolve independently reviewed source-date evidence, without a model call.

    The parser's explicit publisher metadata remains usable. Otherwise every
    extraction contribution must carry a valid source-date review; missing or
    conflicting reviews never become implicit approval. Contribution-local ids
    stay namespaced in ``model_assessments`` and ``claim_refs``.
    """
    extracted = deepcopy(extracted_publication) if isinstance(extracted_publication, dict) else {}
    assessments = []
    for index, bundle in enumerate(bundles):
        review = getattr(bundle, "content_review", {})
        value = review.get("source_publication") if isinstance(review, dict) else None
        if (not isinstance(review, dict) or review.get("protocol") != PROTOCOL
                or review.get("status") != "applied" or not isinstance(value, dict)):
            value = {"status": "unknown", "published_at": None,
                     "reason": "publication_review_missing"}
        else:
            value = enforce_source_publication_dates(value)
        if value.get("status") == "known":
            parsed = parse_published(value.get("published_at"))
            if parsed is None:
                value = {"status": "unknown", "published_at": None,
                         "reason": "invalid_publication_date"}
            else:
                value.update(published_at=parsed[0].isoformat(),
                             precision=value.get("precision") or parsed[1])
        assessments.append({"contribution_index": index, **value})
    audit = {"extracted_publication": extracted, "model_assessments": assessments,
             "claim_refs": [{"contribution_index": row["contribution_index"], "claim_id": cid}
                            for row in assessments if row.get("status") == "known"
                            for cid in row.get("claim_ids", [])]}
    base = {"status": "unknown", "published_at": None, "precision": None,
            "source": "model_content_review", **audit}
    if extracted.get("status") == "conflict" or any(row.get("status") == "conflict" for row in assessments):
        return {**base, "status": "conflict", "reason": "conflicting_publication_dates"}
    known = [row for row in assessments if row.get("status") == "known"]
    parser_known = extracted.get("status") == "known" and parse_published(extracted.get("published_at"))
    candidates = ([extracted] if parser_known else []) + known
    if candidates and _publication_agreement(candidates) is None:
        return {**base, "status": "conflict", "reason": "conflicting_publication_dates"}
    if parser_known:
        # Do not let a missing model assessment revoke independently extracted
        # publisher metadata or replace its original provenance.
        return {**extracted, **audit}
    if not assessments or len(known) != len(assessments):
        return {**base, "reason": "publication_review_missing_or_unverified"}
    agreed = _publication_agreement(known)
    if agreed is None:
        return {**base, "reason": "publication_review_missing_or_unverified"}
    evidence = []
    for row in known:
        for item in row.get("evidence", []):
            if item not in evidence:
                evidence.append(deepcopy(item))
    return {**base, "status": "known", "published_at": agreed["published_at"],
            "precision": agreed["precision"], "evidence": evidence,
            "date_review": DATE_REVIEW_PROTOCOL,
            "date_assessments": [{"contribution_index": row["contribution_index"], **item}
                                 for row in known for item in row["date_assessments"]],
            "reason": "source_publication_claim_supported"}


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


def _coverage_leaves(review):
    """Keep contribution scopes: identical claim ids from different models are not keys."""
    if not isinstance(review, dict):
        return [{}]
    children = review.get("contributions")
    if isinstance(children, list) and children:
        return [leaf for child in children for leaf in _coverage_leaves(child)]
    return [review]


def cache_review_current(review):
    """Require each contribution to have run under the current review policy.

    Old model judgments need re-extraction, not a keyword-based reinterpretation
    of their free-form reasons. Keep the historical records and seals intact.
    """
    return all(leaf.get("review_revision") == REVIEW_REVISION for leaf in _coverage_leaves(review))


def _coverage_answer_claims(bundle, review):
    """Claims authorizing nonempty published fields in this exact contribution.

    Reviewed comparisons and other auxiliary fields can answer a question too.
    Audit-only claims, empty values and fields on held records cannot do so.
    """
    if bundle.decision != "save_structured":
        return set()  # skip/link_only can retain audit objects, but never publish answers
    raw = review.get("model_review", {})
    if not isinstance(raw, dict):
        return set()
    claims = raw.get("claims", [])
    if not isinstance(claims, list):
        return set()
    counts = Counter(c.get("id") for c in claims
                     if isinstance(c, dict) and isinstance(c.get("id"), str))
    originals = {c["id"]: c for c in claims if isinstance(c, dict)
                 and isinstance(c.get("id"), str) and counts[c["id"]] == 1}
    accepted = set()
    for group in GROUPS:
        if group == "analyst_questions":
            continue  # a follow-up question is not an answer
        for item in getattr(bundle, group):
            if group == "events" and item.event_resolution.get("status") != "resolved":
                continue  # honor the shared event decision; pending events are not delivered answers
            audit = item.content_review
            if (audit.get("protocol") != PROTOCOL
                    or audit.get("materialized_digest") != _digest(item)):
                continue
            fields = audit.get("fields", {})
            materialized = item.model_dump(mode="json")
            for name, field in fields.items():
                if name.split("/")[0] in METADATA_FIELDS or name.endswith("_id"):
                    continue
                if field.get("status") != "source_supported":
                    continue
                value = materialized
                for part in name.split("/"):
                    value = value.get(part) if isinstance(value, dict) else None
                if not _present(value):
                    continue
                for cid in field.get("claim_ids", []):
                    checked = audit.get("claims", {}).get(cid, {})
                    if (checked.get("effective_status") != "source_supported"
                            or checked.get("dependency_status", "source_supported") != "source_supported"):
                        continue
                    original = _original_claim(checked)
                    # Exact review provenance, not a global lookup by a model's local id.
                    if cid in originals and original == originals[cid]:
                        accepted.add(cid)
    return accepted


def _coverage_assessment(bundle, review, question, known_ids):
    """Check structure/references, without deciding what a question means."""
    invalid = {"status": "not_assessed", "answer_claim_ids": [], "uncovered_aspects": []}
    raw = review.get("model_review", {})
    if (review.get("protocol") != PROTOCOL or review.get("status") != "applied"
            or not isinstance(raw, dict) or raw.get("protocol") != PROTOCOL):
        return {**invalid, "reason": "coverage_review_missing"}
    rows = raw.get("task_coverage")
    if rows is None:
        return {**invalid, "reason": "coverage_assessment_missing"}
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or any(not isinstance(row.get("question_id"), str)
                   or row["question_id"] not in known_ids for row in rows)):
        return {**invalid, "reason": "coverage_question_id_invalid"}
    matching = [row for row in rows if row["question_id"] == question["question_id"]]
    if len(matching) != 1:
        reason = "coverage_assessment_duplicate" if matching else "coverage_assessment_missing"
        return {**invalid, "reason": reason}
    row = matching[0]
    status = row.get("status")
    answers, aspects = row.get("answer_claim_ids"), row.get("uncovered_aspects")
    if (not isinstance(status, str) or status not in {"covered", "partial", "unanswered"}
            or not isinstance(answers, list) or not isinstance(aspects, list)
            or any(not isinstance(value, str) or not value.strip() for value in answers + aspects)
            or len(set(answers)) != len(answers) or len(set(aspects)) != len(aspects)
            or any(aspect not in question["question"] for aspect in aspects)
            or not isinstance(row.get("reason"), str) or not row["reason"].strip()
            or (status == "covered" and (not answers or aspects))
            or (status == "partial" and (not answers or not aspects))
            or (status == "unanswered" and (answers or not aspects))):
        return {**invalid, "reason": "coverage_assessment_invalid"}
    if set(answers) - _coverage_answer_claims(bundle, review):
        return {**invalid, "reason": "coverage_answer_not_published"}
    return {"status": status, "answer_claim_ids": list(answers),
            "uncovered_aspects": list(aspects), "reason": f"model_{status}"}


def finalize_task_coverage(bundle, questions):
    """Publish bounded coverage assessments after review, merge and final admission.

    ``questions`` is the trusted ``task_context()['questions']`` input. The model
    decides semantic coverage in its existing extraction call; this function only
    verifies the contract, surviving answer references and question quotations.
    Missing or conflicting assessments become explicit uncertainty, never an
    inference that an event did not occur. No model call or query is performed.
    """
    from mic.schemas import CoverageGap

    questions = list(questions or [])
    if (any(not isinstance(q, dict) or not isinstance(q.get("question_id"), str)
            or not q["question_id"] or not isinstance(q.get("question"), str)
            or not q["question"].strip() for q in questions)
            or len({q["question_id"] for q in questions}) != len(questions)):
        raise ValueError("task coverage requires unique question_id and original question text")
    known_ids = {q["question_id"] for q in questions}
    leaves = _coverage_leaves(bundle.content_review)
    results = []
    generated = []
    for question in questions:
        assessments = [{"contribution_index": index,
                        **_coverage_assessment(bundle, leaf, question, known_ids)}
                       for index, leaf in enumerate(leaves)]
        statuses = {assessment["status"] for assessment in assessments}
        if len(statuses) != 1:
            status, reason = "not_assessed", "coverage_assessments_conflict"
        elif statuses == {"not_assessed"}:
            status = "not_assessed"
            reason = assessments[0]["reason"] if len(assessments) == 1 else "coverage_assessment_invalid"
        else:
            status = assessments[0]["status"]
            reason = f"model_{status}"
        answers = list(dict.fromkeys(cid for assessment in assessments
                                     for cid in assessment["answer_claim_ids"])) if status != "not_assessed" else []
        aspects = list(dict.fromkeys(aspect for assessment in assessments
                                    for aspect in assessment["uncovered_aspects"])) if status != "not_assessed" else []
        entry = {"protocol": TASK_COVERAGE_PROTOCOL,
                 "question_id": question["question_id"], "question": question["question"],
                 "source_link_id": bundle.source_link_id,
                 "scope": "source_material" if bundle.source_link_id else "run_material",
                 "status": status, "answer_claim_ids": answers, "uncovered_aspects": aspects,
                 "reason": reason, "assessments": assessments,
                 "answer_claim_refs": [{"contribution_index": assessment["contribution_index"],
                                         "claim_id": cid}
                                        for assessment in assessments for cid in assessment["answer_claim_ids"]]
                                      if status != "not_assessed" else []}
        results.append(entry)
        if status != "covered":
            scope = "本来源材料" if bundle.source_link_id else "本轮已读材料"
            if status == "not_assessed":
                description = f"{scope}尚未完成以下问题的覆盖评估：{question['question']}"
            else:
                description = (f"{scope}对以下问题的覆盖状态为{'部分回答' if status == 'partial' else '未回答'}："
                               f"{question['question']}；未覆盖的需求原文：{'；'.join(aspects)}")
            generated.append(CoverageGap(gap_type="task_question_coverage", description=description,
                                         priority="medium"))

    # Replace only our previous generated rows, preserving all source/model gaps.
    previous = bundle.content_review.get("task_coverage_generated_gaps", [])
    bundle.coverage_gaps = [gap for gap in bundle.coverage_gaps
                            if gap.model_dump(mode="json") not in previous]
    bundle.coverage_gaps.extend(generated)
    bundle.content_review["task_coverage"] = deepcopy(results)
    bundle.content_review["task_coverage_generated_gaps"] = [gap.model_dump(mode="json") for gap in generated]
    return results


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
