"""Deterministic MOCK protocol payloads, never a semantic review implementation.

Only used by the explicit mock adapter and offline test doubles. Approval labels
are simulated so integration tests exercise transport, not LLM correctness.
"""
from copy import deepcopy

from mic.content_review_policy import GROUPS, METADATA_FIELDS, PROTOCOL


def attach_mock_review(raw, passages):
    out = deepcopy(raw)
    claims, bindings = [], {}
    body = {p["passage_id"]: p["text"] for p in passages if p["passage_id"] != "title"}
    fallback = next(iter(body), None)
    for group in ("brief", *GROUPS):
        objects = [out.get("brief", {})] if group == "brief" else out.get(group, [])
        for index, obj in enumerate(objects):
            path = "/brief" if group == "brief" else f"/{group}/{index}"
            pid = obj.get("evidence_locator", {}).get("passage_id")
            pid = pid if pid in body else fallback
            for field, value in obj.items():
                if field in METADATA_FIELDS or field.endswith("_id"):
                    continue
                cid = f"mock_{len(claims)}"
                claims.append({"id": cid, "statement": value if isinstance(value, str) else field,
                    "kind": "observation", "status": "source_supported",
                    "reason": "OFFLINE MOCK LABEL; not a semantic accuracy assessment",
                    "evidence": [{"passage_id": pid, "quote": body[pid]}] if pid else [],
                    "depends_on": []})
                bindings[path + "/" + field] = [cid]
    out["content_review"] = {"protocol": PROTOCOL, "synthetic": True,
                              "claims": claims, "bindings": bindings}
    return out
