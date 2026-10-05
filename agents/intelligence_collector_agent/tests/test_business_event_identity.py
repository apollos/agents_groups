"""Codex e2e reviews b62a210 (F3 / Q4) and 693b3df (R1 / R2 / T1–T3): business-event identity.

Fixtures: the five MIC event rows of the first review run and the four rows of the
second (two media copies each: news.bjx.com.cn and energytrend.cn) for the
2026-09-15 award notice. Independent of wording, type label, run, source or a
"独立" / province prefix in the project name, the Agent must end with one business
event per matter — CATL lot 2, Envision lot 1, the project-level notice — and
count the rest as linked evidence. Different lots, different projects (even at
identical size), real changes and rows without a determinable matter stay apart.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from agent_trade_intel.adapters.common import ToolResult
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.event_identity import (EntityAliases, amount_wan, capacity_mwh, project_key, project_name,
                                              scope_of, signature)
from agent_trade_intel.persistence import ResultPersister

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "codex_e2e_b62a210_all_events.json").read_text("utf-8"))
EVENTS = FIXTURE["events"]
ROUND2 = json.loads((Path(__file__).parent / "fixtures" / "codex_e2e_693b3df_all_events.json").read_text("utf-8"))["events"]
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
    # Bare "capacity: 40" carries no unit → unknown, never assumed MWh (R2).
    assert capacity_mwh(bjx["metrics"]) is None and capacity_mwh(et["metrics"]) is None
    a, b = signature(bjx, published_at="2026-09-15T11:47:00+08:00"), signature(et, published_at="2026-09-15T14:41:00+08:00")
    # major_order and tender are one action family; the lot is CATL's own clause, the project its key.
    assert a.business_key == b.business_key == "宁德时代|award|任丘智弘|标段二"
    assert a.compatible_with(b)


def test_real_run_yields_three_business_events_from_five_source_rows(tmp_path):
    s = store(tmp_path)
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=result(EVENTS))
    assert counts["source_event_rows"] == 5
    assert counts["events"] == 3 and counts["events_linked"] == 2 and counts["events_unresolved"] == 0
    events = rows(s, "SELECT business_key, source_count, source_corroboration_status FROM structured_events ORDER BY business_key")
    assert [e["business_key"] for e in events] == ["任丘智弘|award|任丘智弘|", "宁德时代|award|任丘智弘|标段二", "远景能源|award|任丘智弘|标段一"]
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


def catl(summary="宁德时代中标任丘智弘储能项目二标段储能系统。", capacity=40, amount_wan_=4141.622, date=None, product="钠离子储能系统",
         subject="宁德时代", event_type="major_order", published="2026-09-15T11:47:00+08:00", link="link_x",
         project="任丘智弘储能项目", capacity_unit="MWh"):
    return {"event_type": event_type, "event_date": date, "summary": summary,
            "entities": {"subject": subject, "product": product, "object": project},
            "metrics": {"capacity": capacity, "capacity_unit": capacity_unit, "amount_candidate": amount_wan_, "currency": "CNY万元"},
            "impact": {"direction": "positive", "channels": ["revenue"]}, "confidence": 0.8,
            "source_link_id": link,
            "source": {"url": f"https://example.test/{link}", "domain": "example.test", "published_at": published}}


def test_different_lot_project_subject_date_or_news_cycle_are_not_merged(tmp_path):
    s = store(tmp_path)
    p = ResultPersister(s)
    base = catl(date="2026-09-15")
    variants = {
        "other_lot_same_project": catl(summary="宁德时代中标任丘智弘储能项目一标段。"),
        "other_lot_capacity": catl(capacity=360, amount_wan_=19461.6, summary="宁德时代中标任丘智弘储能项目一标段，360MWh。"),
        "other_amount_same_lot": catl(amount_wan_=5000, summary="宁德时代中标任丘智弘储能项目二标段，金额5000万元。"),
        "other_capacity_same_lot": catl(capacity=80, summary="宁德时代中标任丘智弘储能项目二标段，80MWh。"),
        "other_subject": catl(subject="远景能源", summary="远景能源中标任丘智弘储能项目二标段储能系统。"),
        "other_family": catl(event_type="capacity_change", summary="宁德时代储能产能变更40MWh。", project=""),
        "other_event_date": catl(date="2026-08-01", summary="宁德时代中标任丘智弘储能项目二标段（8月）。"),
        "older_news_cycle": catl(published="2026-06-01T09:00:00+08:00", summary="宁德时代中标任丘智弘储能项目二标段（6月报道）。"),
    }
    counts = p.save_mic_structures(task=TASK, result=result([base, *variants.values()]))
    assert counts["events"] == 9 and counts["events_linked"] == 0


def test_rows_without_subject_or_matter_are_unresolved_and_never_merged(tmp_path):
    s = store(tmp_path)
    p = ResultPersister(s)
    a = catl(subject="", summary="某公司中标储能系统40MWh。")
    b = catl(subject="", summary="某公司中标储能系统40MWh，转载。")
    # Same company, same size, no project named anywhere: identical quantities alone never
    # prove the same matter (R1) → two unresolved rows, not one event with two sources.
    no_scope_1 = catl(project="", summary="宁德时代中标钠电储能系统。")
    no_scope_2 = catl(project="", summary="宁德时代拿下钠电储能订单。")
    # Same project and lot without any quantity on either side: the matter is determined → linked.
    no_qty_1 = catl(capacity=None, amount_wan_=None, summary="宁德时代中标任丘智弘储能项目二标段。")
    no_qty_2 = catl(capacity=None, amount_wan_=None, summary="宁德时代拿下任丘智弘储能项目二标段订单。")
    counts = p.save_mic_structures(task=TASK, result=result([a, b, no_scope_1, no_scope_2, no_qty_1, no_qty_2]))
    assert counts["events_unresolved"] == 4
    assert counts["events_linked"] == 1
    assert counts["events"] == 5
    statuses = rows(s, "SELECT dedup_status, COUNT(*) n FROM structured_events GROUP BY dedup_status")
    assert {r["dedup_status"]: r["n"] for r in statuses} == {"unresolved": 4, "keyed": 1}


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
    assert keyed["old_catl"] == {"event_id": "old_catl", "business_key": "宁德时代|award|任丘智弘|标段二", "dedup_status": "keyed", "source_count": 2}
    ledger = rows(s, "SELECT content_key, event_id, link_status FROM structured_event_sources ORDER BY content_key")
    assert {(r["event_id"], r["link_status"]) for r in ledger} == {("old", "primary"), ("old_catl", "primary"), ("old_catl", "linked")}
    assert len(rows(s, "SELECT 1 FROM structured_events")) == 2   # nothing deleted, nothing added


# --- Codex review 693b3df (2026-10-05): R1 / R2, T1–T3 ---------------------------

def test_t1_second_review_rows_yield_two_matters_with_two_sources_each(tmp_path):
    """Real rows: the project notice split by '独立' in the subject and a 100-vs-400 unit mix-up."""
    notice = [e for e in ROUND2 if e["event_type"] == "tender"]
    assert [e["entities"]["subject"] for e in notice] == ["河北任丘智弘独立储能试点项目", "河北任丘智弘储能试点项目"]
    assert [e["metrics"].get("capacity") for e in notice] == [100, 400]          # bare numbers: no unit stated
    assert [capacity_mwh(e["metrics"]) for e in notice] == [None, None]          # → unknown, not 100 MWh vs 400 MWh
    assert {project_key(e["entities"]["subject"]) for e in notice} == {"任丘智弘"}
    s = store(tmp_path)
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=result(ROUND2, "run_3549da9f8002"))
    assert counts["source_event_rows"] == 4
    assert counts["events"] == 2 and counts["events_linked"] == 2 and counts["events_unresolved"] == 0
    events = rows(s, "SELECT event_id, business_key, event_date, source_count, source_corroboration_status FROM structured_events ORDER BY business_key")
    assert [e["business_key"] for e in events] == ["任丘智弘|award|任丘智弘|", "宁德时代|award|任丘智弘|标段二"]
    assert [e["source_count"] for e in events] == [2, 2]
    assert all(e["source_corroboration_status"] == "multi_source" for e in events)
    assert events[0]["event_date"].startswith("2026-09-15")                      # date of the notice is kept
    ledger = rows(s, "SELECT event_id, link_status, source_link_id, source_url FROM structured_event_sources ORDER BY rowid")
    assert [r["link_status"] for r in ledger] == ["primary", "primary", "linked", "linked"]
    assert len({r["source_link_id"] for r in ledger}) == 2 and all(r["source_url"] for r in ledger)   # citations kept


def synthetic(project, source):
    """Two different projects, everything else identical (review §3.2). Names are placeholders."""
    return {"event_type": "major_order", "event_date": "2026-09-15",
            "summary": f"宁德时代中标{project}二标段10MW/40MWh钠离子储能系统，中标价4141.622万元。",
            "entities": {"subject": "宁德时代", "object": project, "product": "钠离子储能系统"},
            "metrics": {"amount": 41416220, "amount_unit": "元", "currency": "CNY", "energy_mwh": 40.0,
                        "energy_evidence": {"passage_id": "p2", "quote": "40MWh"}},
            "impact": {"direction": "positive", "channels": ["revenue"]}, "confidence": 0.8,
            "source_link_id": source,
            "source": {"url": f"https://example.test/{source}", "domain": "example.test", "published_at": "2026-09-15T10:00:00+08:00"}}


def test_t2_different_projects_with_identical_quantities_date_and_subject_are_two_events(tmp_path):
    a, b = synthetic("甲地示例储能项目", "synthetic_a"), synthetic("乙地示例储能项目", "synthetic_b")
    sa, sb = signature(a, published_at=a["source"]["published_at"]), signature(b, published_at=b["source"]["published_at"])
    assert sa.amount_wan == sb.amount_wan and sa.capacity_mwh == sb.capacity_mwh == 40.0 and sa.event_date == sb.event_date
    assert sa.project == "甲地示例" and sb.project == "乙地示例" and not sa.compatible_with(sb)
    s = store(tmp_path)
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=result([a, b]))
    assert counts["events"] == 2 and counts["events_linked"] == 0
    # The same project written with / without province or "独立" still links.
    c = synthetic("河北甲地示例独立储能试点项目", "synthetic_c")
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=result([c], "run_c"))
    assert counts["events"] == 0 and counts["events_linked"] == 1


def test_t3_energy_is_taken_only_from_unit_bearing_fields():
    assert capacity_mwh({"capacity": 100, "capacity_unit": "MW", "volume": 400, "volume_unit": "MWh"}) == 400.0
    assert capacity_mwh({"capacity": 400, "capacity_unit": "MWh"}) == 400.0
    assert capacity_mwh({"capacity": "0.4GWh"}) == 400.0
    assert capacity_mwh({"capacity": 0.4, "capacity_unit": "GWh"}) == 400.0
    assert capacity_mwh({"volume": "40MWh"}) == 40.0
    assert capacity_mwh({"energy_mwh": 40.0}) == 40.0
    assert capacity_mwh({"energy_mwh": {"value": 0.04, "unit": "GWh", "canonical": 40.0}}) == 40.0
    assert capacity_mwh({"capacity": 100, "capacity_unit": "MW"}) is None          # power is not energy
    assert capacity_mwh({"capacity": 100, "volume": 400}) is None                  # no units → unknown
    assert capacity_mwh({"capacity": "100MW/400MWh"}) == 400.0                     # the one energy token is explicit
    assert capacity_mwh({"capacity": "30MW/120MWh+60MW/240MWh"}) is None            # two energy tokens: ambiguous


def test_scope_extraction_handles_variants_and_lot_by_clause():
    assert project_key("河北任丘智弘100MW/400MWh新型技术路线磷酸铁锂电池+钠电池独立储能试点项目储能系统设备采购中标结果公示") == "任丘智弘"
    assert project_key("任丘智弘独立储能试点项目(招标方)") == "任丘智弘"
    assert project_key("河北任丘智弘储能项目招标方") == "任丘智弘"
    assert project_key("甲地示例储能项目一期") == "甲地示例"
    assert project_key("宁德时代") == "" and project_key("") == ""
    both_lots = {"event_type": "major_order", "entities": {"subject": "宁德时代"},
                 "summary": "公示：一标段由远景能源中标（19461.6万元），二标段10MW/40MWh钠离子储能系统由宁德时代中标（4141.622万元）。"}
    assert scope_of(both_lots)["lot"] == "标段二"
    assert scope_of({"event_type": "tender", "entities": {"subject": "任丘智弘储能试点项目"}, "summary": "中标结果公示，分两个标段。"}) == {
        "project": "任丘智弘", "lot": "", "project_raw": "任丘智弘储能试点项目"}


# --- Codex review run_1b8ec1dde3b5 (2026-10-05): company aliases, project-name variants -----

ROUND3 = json.loads((Path(__file__).parent / "fixtures" / "codex_e2e_run_1b8ec1dde3b5_all_events.json").read_text("utf-8"))


def round3_result(events, run_id="run_1b8ec1dde3b5", aliases=None):
    r = result(events, run_id)
    r.result["target"] = ROUND3["report_target"]            # MIC profile canonical name (full company name)
    if aliases is not None:
        r.result["target_aliases"] = aliases
    return r


def test_project_key_is_the_head_before_type_descriptors_not_a_word_deletion():
    # All spellings of the one project seen across three reviews → one key …
    variants = ["河北任丘智弘储能项目", "河北任丘智弘储能试点项目", "河北任丘智弘独立储能试点项目",
                "河北任丘智弘100MW/400MWh磷酸铁锂+钠电独立储能试点项目储能系统采购中标结果公示",
                "河北任丘智弘新型技术路线（磷酸铁锂+钠电池）独立储能试点项目", "任丘智弘储能项目"]
    assert {project_key(v) for v in variants} == {"任丘智弘"}
    # … while the full name stays available as source context.
    assert project_name("河北任丘智弘100MW/400MWh磷酸铁锂+钠电独立储能试点项目储能系统采购") == "河北任丘智弘磷酸铁锂+钠电独立储能试点项目"
    # A descriptor not in the list ("钠电") cannot leak into the key: the cut is at the first descriptor.
    assert project_key("河北任丘智弘钠电独立储能试点项目") == "任丘智弘"
    # Different heads are different projects; a phase suffix is part of the head.
    assert project_key("甲地示例储能项目") != project_key("乙地示例储能项目")
    assert project_key("任丘智弘二期储能项目") == "任丘智弘二期"
    # No distinctive head → no project (award rows then stay unresolved rather than merge).
    assert project_key("新型技术路线储能试点项目") == "" and project_key("独立储能项目") == ""
    # Leading verbs / company words before the name do not become part of the head.
    assert project_key("公司全资子公司中标大唐和平共享储能电站项目") == "大唐和平"


def test_target_aliases_resolve_full_and_short_company_names_only():
    aliases = EntityAliases(["宁德时代", "宁德时代新能源科技股份有限公司", "CATL", "300750"], key="宁德时代")
    assert aliases.resolve("宁德时代新能源科技股份有限公司") == ("宁德时代", True)
    assert aliases.resolve("宁德时代") == ("宁德时代", True)
    assert aliases.resolve("CATL") == ("宁德时代", True)
    # Other companies in the same target's task are not the target (review §3 边界 1).
    assert aliases.resolve("远景能源") == ("远景能源", False)
    assert aliases.resolve("远景能源有限公司") == ("远景能源", False)        # suffix-only normalisation
    assert aliases.resolve("远景能源科技有限公司") == ("远景能源科技", False)  # no prefix guessing
    # Without a profile the subject is only normalised; keys stay stable with earlier saves.
    assert EntityAliases([]).resolve("宁德时代") == ("宁德时代", False)
    assert EntityAliases(["宁德时代新能源科技股份有限公司", "宁德时代"], key="宁德时代").key == "宁德时代"


def test_t_round3_five_real_rows_yield_three_matters_with_five_sources(tmp_path):
    events = ROUND3["events"]
    subjects = {e["id"]: e["entities"]["subject"] for e in events}
    assert subjects["evt_c0f486a8dc4a"] == "宁德时代新能源科技股份有限公司" and subjects["evt_258e426f7023"] == "宁德时代"
    assert subjects["evt_d68d828b789a"] == "河北任丘智弘储能试点项目" and subjects["evt_dc3c091f7738"] == "河北任丘智弘储能项目"
    s = store(tmp_path)
    # The saved report carries only the profile canonical name as "target" (no target_aliases yet).
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=round3_result(events))
    assert counts["source_event_rows"] == 5
    assert counts["events"] == 3 and counts["events_linked"] == 2 and counts["events_replayed"] == 0
    assert counts["events_unresolved"] == 0
    ev = {e["business_key"]: e for e in rows(s, "SELECT event_id, business_key, source_count, source_corroboration_status, event_date, payload_json FROM structured_events")}
    assert set(ev) == {"宁德时代|award|任丘智弘|标段二", "任丘智弘|award|任丘智弘|", "远景能源|award|任丘智弘|标段一"}
    assert ev["宁德时代|award|任丘智弘|标段二"]["source_count"] == 2 and ev["宁德时代|award|任丘智弘|标段二"]["source_corroboration_status"] == "multi_source"
    assert ev["任丘智弘|award|任丘智弘|"]["source_count"] == 2 and ev["任丘智弘|award|任丘智弘|"]["event_date"].startswith("2026-09-15")
    assert ev["远景能源|award|任丘智弘|标段一"]["source_count"] == 1 and ev["远景能源|award|任丘智弘|标段一"]["source_corroboration_status"] == "single_source"
    # Event → source mapping: every source row and citation kept, two of them as linked evidence.
    ledger = rows(s, "SELECT event_id, link_status, source_link_id, source_domain, source_url FROM structured_event_sources ORDER BY rowid")
    assert len(ledger) == 5 and all(r["source_url"] and r["source_link_id"] for r in ledger)
    assert sorted(r["link_status"] for r in ledger) == ["linked", "linked", "primary", "primary", "primary"]
    by_event = {}
    for r in ledger:
        by_event.setdefault(r["event_id"], set()).add(r["source_domain"])
    assert sorted(by_event.values(), key=len) == [{"energytrend.cn"}, {"energytrend.cn", "news.bjx.com.cn"}, {"energytrend.cn", "news.bjx.com.cn"}]
    # How each row was identified is stored with it (raw names → keys).
    identity = json.loads(ev["宁德时代|award|任丘智弘|标段二"]["payload_json"])["business_identity"]
    assert identity["subject"]["resolution"] == "target_alias" and identity["project"]["key"] == "任丘智弘"
    assert identity["project"]["raw"].endswith("项目") and identity["lot"] == "标段二" and identity["energy_mwh"] == 40.0
    # Redelivery of the same result: no new events, every row replayed.
    again = ResultPersister(s).save_mic_structures(task=TASK, result=round3_result(events))
    assert again["events"] == 0 and again["events_linked"] == 0 and again["events_replayed"] == 5
    # Next cycle: new run / task key, reworded within known aliases, type label changed → evidence, not events.
    cycle = []
    for e in copy.deepcopy(events):
        e["summary"] = "转载：" + e["summary"]
        e["event_type"] = "tender" if e["event_type"] == "major_order" else "major_order"
        e["source_link_id"] = e["source_link_id"] + "_cycle2"
        if e["entities"]["subject"] == "宁德时代":
            e["entities"]["subject"] = "CATL"
        cycle.append(e)
    next_task = {**TASK, "idempotency_key": TASK["idempotency_key"].replace("2026-10-04", "2026-10-06")}
    third = ResultPersister(s).save_mic_structures(task=next_task, result=round3_result(cycle, "run_cycle2", aliases=["宁德时代", "CATL", "300750"]))
    assert third["events"] == 0 and third["events_linked"] == 5 and third["events_replayed"] == 0
    assert len(rows(s, "SELECT 1 FROM structured_events")) == 3


def test_non_target_companies_sharing_the_task_are_not_one_subject(tmp_path):
    s = store(tmp_path)
    envision = catl(subject="远景能源", capacity=360, amount_wan_=19461.6, summary="远景能源中标任丘智弘储能项目一标段。")
    other = catl(subject="远景能源科技有限公司", capacity=360, amount_wan_=19461.6, summary="远景能源科技有限公司中标任丘智弘储能项目一标段。", link="link_y")
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=round3_result([envision, other], aliases=["宁德时代", "CATL"]))
    # Same target_id, same lot and quantities, but no alias evidence that these names are one company.
    assert counts["events"] == 2 and counts["events_linked"] == 0
    assert {r["business_key"] for r in rows(s, "SELECT business_key FROM structured_events")} == {
        "远景能源|award|任丘智弘|标段一", "远景能源科技|award|任丘智弘|标段一"}


def test_rows_keyed_under_earlier_rules_are_rekeyed_not_duplicated(tmp_path):
    s = store(tmp_path)
    full = catl(subject="宁德时代新能源科技股份有限公司", summary="宁德时代新能源科技股份有限公司中标任丘智弘储能项目二标段。")
    ResultPersister(s).save_mic_structures(task=TASK, result=result([full]))      # no profile names → key uses the long name
    assert rows(s, "SELECT business_key FROM structured_events")[0]["business_key"] == "宁德时代新能源科技|award|任丘智弘|标段二"
    short = catl(summary="宁德时代中标任丘智弘储能项目二标段（转载）。", link="link_z")
    counts = ResultPersister(s).save_mic_structures(task=TASK, result=round3_result([short], "run_b"))
    assert counts["events_rekeyed"] == 1 and counts["events"] == 0 and counts["events_linked"] == 1
    ev = rows(s, "SELECT business_key, source_count FROM structured_events")
    assert ev == [{"business_key": "宁德时代|award|任丘智弘|标段二", "source_count": 2}]
