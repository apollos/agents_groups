"""Codex e2e review b62a210 (2026-10-04), F3 / Q4: same business event, different wording.

Fixture: the five MIC event rows the real run produced for the 2026-09-15 award
notice (two media copies: news.bjx.com.cn and energytrend.cn). Independent of
how they are reworded, re-labelled (tender vs major_order), re-run or re-sourced,
the Agent must end with three business events — CATL lot 2, Envision lot 1, the
project-level notice — and count the rest as linked evidence. Different lots,
different projects and real changes must stay apart.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.event_identity import amount_wan, capacity_mwh, signature
from agent_trade_intel.persistence import ResultPersister

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "codex_e2e_b62a210_all_events.json").read_text("utf-8"))
EVENTS = FIXTURE["events"]
TASK = {"task_id": "task_a", "idempotency_key": "collection_task:d:mic_deep_collect:company_300750:2026-10-04",
        "target": {"target_id": "company_300750", "ticker": "300750.SZ", "company_name": "宁德时代"}}


def store(tmp_path):
    s = SQLiteStore(tmp_path / "data.db")
    s.init_schema()
    return s


def result(events, run_id="run_a"):
    r = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={})
    r.status = "success"
    r.result = {"search_run_id": run_id, "all_events": events, "top_events": events[:5]}
    return r.finish()


def rows(s, sql, *args):
    with s.session() as con:
        return [dict(r) for r in con.execute(sql, args)]


def test_signature_parses_both_media_copies_to_the_same_quantities():
    bjx = next(e for e in EVENTS if e["source"]["domain"] == "news.bjx.com.cn" and e["entities"]["subject"] == "宁德时代")
    et = next(e for e in EVENTS if e["source"]["domain"] == "energytrend.cn" and e["entities"]["subject"] == "宁德时代")
    assert amount_wan(bjx["metrics"]) == amount_wan(et["metrics"]) == 4141.622   # 元 canonical vs 万元 candidate
    assert capacity_mwh(bjx["metrics"]) == capacity_mwh(et["metrics"]) == 40.0
    a, b = signature(bjx, published_at="2026-09-15T11:47:00+08:00"), signature(et, published_at="2026-09-15T14:41:00+08:00")
    assert a.business_key == b.business_key == "宁德时代|award"   # major_order and tender are one action family
    assert a.compatible_with(b)


def test_real_run_yields_three_business_events_from_five_source_rows(tmp_path):
    s = store(tmp_path)
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=result(EVENTS))
    assert counts["source_event_rows"] == 5
    assert counts["events"] == 3 and counts["events_linked"] == 2 and counts["events_unresolved"] == 0
    events = rows(s, "SELECT business_key, source_count, source_corroboration_status FROM structured_events ORDER BY business_key")
    assert [e["business_key"] for e in events] == ["任丘智弘储能试点项目|award", "宁德时代|award", "远景能源|award"]
    assert [e["source_count"] for e in events] == [1, 2, 2]
    # Two different domains corroborate the two awards.
    assert [e["source_corroboration_status"] for e in events][1:] == ["multi_source", "multi_source"]
    ledger = rows(s, "SELECT link_status, COUNT(*) n FROM structured_event_sources GROUP BY link_status")
    assert {r["link_status"]: r["n"] for r in ledger} == {"primary": 3, "linked": 2}


def rewrite(events, *, run_suffix):
    """A later cycle: new run / link ids, reworded summaries, swapped type labels."""
    out = []
    for e in copy.deepcopy(events):
        e["summary"] = "转载：" + e["summary"].replace("中标", "成功中标").replace("。", "，详见公示。")
        e["event_type"] = {"major_order": "tender", "tender": "major_order"}.get(e["event_type"], e["event_type"])
        e["source_link_id"] = e["source_link_id"] + run_suffix
        e["confidence"] = round(e["confidence"] * 0.9, 3)
        out.append(e)
    return out


def test_next_cycle_rewording_retyping_and_resourcing_add_evidence_not_events(tmp_path):
    s = store(tmp_path)
    p = ResultPersister(s)
    first = p.save_mic_structures(task=TASK, result=result(EVENTS, "run_a"))
    assert first["events"] == 3
    next_task = {**TASK, "task_id": "task_b",
                 "idempotency_key": "collection_task:d:mic_deep_collect:company_300750:2026-10-05"}
    second = p.save_mic_structures(task=next_task, result=result(rewrite(EVENTS, run_suffix="_b"), "run_b"))
    assert second["events"] == 0
    assert second["events_linked"] == 5 and second["events_replayed"] == 0
    assert rows(s, "SELECT COUNT(*) n FROM structured_events")[0]["n"] == 3
    # Evidence is queryable per business event and per run.
    assert rows(s, "SELECT COUNT(*) n FROM structured_event_sources")[0]["n"] == 10
    assert rows(s, "SELECT COUNT(*) n FROM structured_event_sources WHERE source_run_id='run_b'")[0]["n"] == 5
    by_event = rows(s, "SELECT event_id, COUNT(*) n FROM structured_event_sources GROUP BY event_id ORDER BY n")
    assert [r["n"] for r in by_event] == [2, 4, 4]
    # Exact replay of the first report is still a pure no-op.
    third = p.save_mic_structures(task=TASK, result=result(EVENTS, "run_a"))
    assert third["events"] == 0 and third["events_linked"] == 0 and third["events_replayed"] == 5
    assert rows(s, "SELECT COUNT(*) n FROM structured_event_sources")[0]["n"] == 10


def catl(summary="宁德时代中标储能系统。", capacity=40, amount_wan_=4141.622, date=None, product="钠离子储能系统",
         subject="宁德时代", event_type="major_order", published="2026-09-15T11:47:00+08:00", link="link_x"):
    return {"event_type": event_type, "event_date": date, "summary": summary,
            "entities": {"subject": subject, "product": product},
            "metrics": {"capacity": capacity, "amount_candidate": amount_wan_, "currency": "CNY万元"},
            "impact": {"direction": "positive", "channels": ["revenue"]}, "confidence": 0.8,
            "source_link_id": link,
            "source": {"url": f"https://example.test/{link}", "domain": "example.test", "published_at": published}}


def test_different_lot_project_subject_date_or_news_cycle_are_not_merged(tmp_path):
    s = store(tmp_path)
    p = ResultPersister(s)
    base = catl(date="2026-09-15")
    variants = {
        "other_lot_capacity": catl(capacity=360, amount_wan_=19461.6, summary="宁德时代中标一标段。"),
        "other_amount_same_capacity": catl(amount_wan_=5000, summary="宁德时代中标另一项目40MWh。"),
        "other_subject": catl(subject="远景能源", summary="远景能源中标储能系统。"),
        "other_family": catl(event_type="capacity_change", summary="宁德时代储能产能变更40MWh。"),
        "other_event_date": catl(date="2026-08-01", summary="宁德时代中标（8月）。"),
        "older_news_cycle": catl(published="2026-06-01T09:00:00+08:00", summary="宁德时代中标（6月报道）。"),
    }
    counts = p.save_mic_structures(task=TASK, result=result([base, *variants.values()]))
    assert counts["events"] == 7 and counts["events_linked"] == 0


def test_rows_without_subject_or_quantity_are_unresolved_and_never_merged(tmp_path):
    s = store(tmp_path)
    p = ResultPersister(s)
    a = catl(subject="", summary="某公司中标储能系统40MWh。")
    b = catl(subject="", summary="某公司中标储能系统40MWh，转载。")
    no_qty_1 = catl(capacity=None, amount_wan_=None, product="钠离子储能系统", summary="宁德时代中标钠电储能。")
    no_qty_2 = catl(capacity=None, amount_wan_=None, product="钠离子储能系统", summary="宁德时代拿下钠电储能订单。")
    no_qty_3 = catl(capacity=None, amount_wan_=None, product="动力电池", summary="宁德时代中标动力电池。")
    counts = p.save_mic_structures(task=TASK, result=result([a, b, no_qty_1, no_qty_2, no_qty_3]))
    assert counts["events_unresolved"] == 2          # no subject → two separate unresolved rows
    assert counts["events_linked"] == 1              # same subject + same product, no quantities on either side
    assert counts["events"] == 4
    statuses = rows(s, "SELECT dedup_status, COUNT(*) n FROM structured_events GROUP BY dedup_status")
    assert {r["dedup_status"]: r["n"] for r in statuses} == {"unresolved": 2, "keyed": 2}


def test_legacy_database_without_business_columns_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE structured_events (event_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL DEFAULT 'structured_event.v1',
          target_id TEXT, ticker TEXT, company_name TEXT, event_type TEXT NOT NULL, event_subtype TEXT, event_date TEXT,
          summary_cn TEXT NOT NULL, impact_json TEXT NOT NULL DEFAULT '{}', source_refs_json TEXT NOT NULL DEFAULT '[]',
          source_level TEXT, confidence REAL, data_quality REAL, source_corroboration_status TEXT, source_run_id TEXT,
          source_url TEXT, source_domain TEXT, source_type TEXT, published_at TEXT, retrieved_at TEXT, query_family TEXT,
          payload_json TEXT NOT NULL DEFAULT '{}', idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO structured_events(event_id, event_type, summary_cn, idempotency_key) VALUES ('old', 'tender', '旧事件', 'k_old');
    """)
    legacy_catl = catl()   # a pre-v9 copy of the CATL lot-2 award, saved before business keys existed
    con.execute("INSERT INTO structured_events(event_id, target_id, event_type, summary_cn, idempotency_key, payload_json, published_at) "
                "VALUES ('old_catl', ?, 'major_order', ?, 'k_old_catl', ?, ?)",
                (TASK["target"]["target_id"], legacy_catl["summary"], json.dumps(legacy_catl, ensure_ascii=False),
                 legacy_catl["source"]["published_at"]))
    con.commit(); con.close()
    s = SQLiteStore(path)
    s.init_schema()
    cols = {r["name"] for r in rows(s, "PRAGMA table_info(structured_events)")}
    assert {"business_key", "dedup_status", "source_count"} <= cols
    old = rows(s, "SELECT business_key, dedup_status, source_count FROM structured_events WHERE event_id='old'")[0]
    assert old == {"business_key": None, "dedup_status": None, "source_count": 1}
    # First save after the upgrade keys legacy rows (additively) so re-extracted copies link to them.
    reworded = copy.deepcopy(legacy_catl)
    reworded["summary"] = "转载：" + reworded["summary"]
    reworded["event_type"] = "tender"
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=result([reworded], run_id="run_after_upgrade"))
    assert counts["legacy_events_keyed"] == 2
    assert counts["events"] == 0 and counts["events_linked"] == 1
    keyed = {r["event_id"]: r for r in rows(s, "SELECT event_id, business_key, dedup_status, source_count FROM structured_events")}
    assert keyed["old"]["dedup_status"] == "unresolved" and keyed["old"]["business_key"] is None
    assert keyed["old_catl"] == {"event_id": "old_catl", "business_key": "宁德时代|award", "dedup_status": "keyed", "source_count": 2}
    ledger = rows(s, "SELECT content_key, event_id, link_status FROM structured_event_sources ORDER BY content_key")
    assert {(r["event_id"], r["link_status"]) for r in ledger} == {("old", "primary"), ("old_catl", "primary"), ("old_catl", "linked")}
    assert len(rows(s, "SELECT 1 FROM structured_events")) == 2   # nothing deleted, nothing added
