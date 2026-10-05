"""Persist MIC semantic decisions without falling back to string identity keys."""
from __future__ import annotations

from .db import dumps_json, loads_json
from .ids import make_idempotency_key, new_id, stable_hash, utc_now_iso


def history(store, target_id: str) -> dict:
    # Imported only for MIC collection; other Agent tools don't require MIC.
    from mic.event_resolution import candidate
    with store.session() as con:
        rows = con.execute(
            "SELECT event_id, payload_json FROM structured_events WHERE target_id IS ? "
            "ORDER BY rowid DESC LIMIT 65", (target_id,)).fetchall()
    return {"complete": len(rows) <= 64,
            "candidates": [candidate(loads_json(r["payload_json"], {}), "event:" + r["event_id"])
                           for r in reversed(rows[:64])]}


def _reference(con, target_id, resolution):
    from mic.event_resolution import fingerprint
    ref = resolution.get("candidate_ref", "")
    if ref.startswith("event:"):
        row = con.execute("SELECT event_id, payload_json FROM structured_events "
                          "WHERE event_id=? AND target_id IS ?", (ref[6:], target_id)).fetchone()
        if row and fingerprint(loads_json(row["payload_json"], {})) == resolution.get("candidate_fingerprint"):
            return row["event_id"]
    else:
        row = con.execute("SELECT event_id, fingerprint FROM event_resolution_refs "
                          "WHERE ref=? AND target_id IS ?", (ref, target_id)).fetchone()
        if row and row["fingerprint"] == resolution.get("candidate_fingerprint"):
            return row["event_id"]
    return None


def save(persister, *, task, report):
    from mic.event_resolution import PROTOCOL, fingerprint
    counts = {k: 0 for k in ("events", "events_linked", "events_replayed", "events_unresolved",
                             "events_pending", "events_follow_up", "source_event_rows",
                             "coverage_gaps", "event_variable_links")}
    target = task.get("target") or {}
    target_id = target.get("target_id") or task.get("target_id")
    events = list(report.get("all_events") or report.get("top_events") or [])
    with persister.store.session() as con:
        # The event row, evidence ledger, references and counters change atomically.
        con.execute("BEGIN IMMEDIATE")
        try:
            waiting = events
            while waiting:
                deferred = []
                progressed = False
                waiting_refs = {(e.get("event_resolution") or {}).get("ref") for e in waiting}
                for event in waiting:
                    resolution = event.get("event_resolution") or {}
                    ref = resolution.get("ref")
                    candidate_ref = resolution.get("candidate_ref")
                    if candidate_ref in waiting_refs and candidate_ref != ref:
                        deferred.append(event)
                        continue
                    progressed = True
                    _save_one(con, persister, target, target_id, report, event, counts, PROTOCOL, fingerprint)
                if not progressed:
                    # Cycles / invalid references cannot become confirmed new events.
                    for event in deferred:
                        _save_one(con, persister, target, target_id, report,
                                  {**event, "event_resolution": {**event.get("event_resolution", {}),
                                   "status": "pending", "reason": "cyclic_candidate_reference"}},
                                  counts, PROTOCOL, fingerprint)
                    break
                waiting = deferred
            for gap in report.get("coverage_gaps", []) or []:
                con.execute("INSERT OR IGNORE INTO coverage_gaps(gap_id,target_id,ticker,priority,status,"
                            "description,suggested_next_queries_json,source_run_id) VALUES(?,?,?,?,'open',?,?,?)",
                            (gap.get("gap_id") or new_id("gap"), target_id, target.get("ticker"),
                             gap.get("priority", "normal"), gap.get("description") or str(gap)[:300],
                             dumps_json(gap.get("suggested_next_queries", [])), report.get("search_run_id")))
                counts["coverage_gaps"] += 1
            con.commit()
        except Exception:
            con.rollback()
            raise
    return counts


def _save_one(con, persister, target, target_id, report, event, counts, protocol, fingerprint):
    resolution = event.get("event_resolution") or {}
    source = event.get("source") or {}
    summary = event.get("summary") or ""
    # Exact replay requires the same source, not merely the same generated summary.
    # Different sites with identical wording still add source evidence.
    content = {k: event.get(k) for k in ("event_type", "event_date", "summary", "evidence_locator")}
    idem = make_idempotency_key("semantic_source", target_id, source.get("url") or event.get("source_link_id"),
                                stable_hash(dumps_json(content), 32))
    counts["source_event_rows"] += 1
    existing = con.execute("SELECT event_id FROM structured_event_sources WHERE content_key=?", (idem,)).fetchone()
    event_id = existing["event_id"] if existing else None
    predecessor = None
    relation = resolution.get("relation")
    valid = resolution.get("protocol") == protocol and resolution.get("status") == "resolved"
    valid = valid and resolution.get("event_fingerprint") == fingerprint(event)
    content_review_invalid = False
    if report.get("content_review_protocol") or event.get("content_review"):
        from mic.content_review import reviewed_event
        content_review_invalid = not reviewed_event(event)
        valid = valid and not content_review_invalid
    if valid and relation in {"same_event", "follow_up"}:
        predecessor = _reference(con, target_id, resolution)
        valid = predecessor is not None
    valid = valid and relation in {"new", "same_event", "follow_up"}
    if existing:
        counts["events_replayed"] += 1
        link_status = "replayed"
    elif not valid:
        reason = resolution.get("reason") if resolution.get("status") == "pending" else "candidate_reference_unverified"
        if content_review_invalid:
            reason = "content_review_missing_or_changed"
        con.execute("INSERT INTO pending_event_resolutions(content_key,target_id,source_run_id,reason,payload_json) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(content_key) DO UPDATE SET "
                    "reason=excluded.reason,payload_json=excluded.payload_json,source_run_id=excluded.source_run_id",
                    (idem, target_id, report.get("search_run_id"), reason or "missing_semantic_decision", dumps_json(event)))
        counts["events_pending"] += 1
        return
    elif relation == "same_event":
        event_id = predecessor
        counts["events_linked"] += 1
        link_status = "linked"
        # More reports don't prove independent corroboration: keep the existing
        # corroboration label, and account for source diversity separately.
        con.execute("UPDATE structured_events SET source_count=source_count+1 WHERE event_id=?", (event_id,))
    else:
        event_id = new_id("event")
        link_status = "follow_up" if predecessor else "primary"
        payload = {**event, "predecessor_event_id": predecessor}
        con.execute("INSERT INTO structured_events(event_id,target_id,ticker,company_name,event_type,event_date,"
                    "summary_cn,impact_json,source_refs_json,source_url,source_domain,source_type,published_at,"
                    "retrieved_at,query_family,confidence,source_run_id,payload_json,idempotency_key,"
                    "source_corroboration_status,dedup_status,source_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (event_id, target_id, target.get("ticker"), target.get("company_name"), event.get("event_type") or "other",
                     event.get("event_date"), summary, dumps_json(event.get("impact", {})),
                     dumps_json([event.get("source_link_id")]), source.get("url"), source.get("domain"),
                     source.get("source_type"), source.get("published_at"), utc_now_iso(), source.get("query_family"),
                     event.get("confidence"), report.get("search_run_id"), dumps_json(payload), idem,
                     event.get("source_corroboration_status"), "semantic"))
        counts["events"] += 1
        if predecessor:
            counts["events_follow_up"] += 1
            con.execute("INSERT INTO event_progress_links(event_id,predecessor_event_id,reason) VALUES(?,?,?)",
                        (event_id, predecessor, resolution.get("reason")))
    con.execute("DELETE FROM pending_event_resolutions WHERE content_key=?", (idem,))
    con.execute("INSERT OR IGNORE INTO structured_event_sources(content_key,event_id,target_id,source_run_id,"
                "source_link_id,source_url,source_domain,published_at,event_type,summary_cn,link_status,payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (idem,event_id,target_id,report.get("search_run_id"),event.get("source_link_id"),source.get("url"),
                 source.get("domain"),source.get("published_at"),event.get("event_type"),summary,link_status,dumps_json(event)))
    if resolution.get("ref"):
        con.execute("INSERT OR REPLACE INTO event_resolution_refs(ref,target_id,event_id,fingerprint) VALUES(?,?,?,?)",
                    (resolution["ref"],target_id,event_id,fingerprint(event)))
    counts["event_variable_links"] += persister._save_event_variable_links(
        con=con,event_id=event_id,target_id=target_id,ticker=target.get("ticker"),event=event,
        allowed_variables=[str(v) for v in target.get("tracking_variables", [])])
