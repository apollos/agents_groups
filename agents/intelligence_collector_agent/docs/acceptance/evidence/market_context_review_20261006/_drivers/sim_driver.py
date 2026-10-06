"""Simulated verification scenarios 4-6 (same-day update / late older result / unknown snapshot date).

Run: /home/yu/.venv/mydev/bin/python /tmp/mctx_verify/sim_driver.py
Evidence: agents/intelligence_collector_agent/docs/acceptance/evidence/market_context_review_20261006/<scenario>/
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/tmp/mctx_verify")
from sim_common import SH, Scenario, T  # noqa: E402

from stock_data_ingestion.adapters import base as adapter_base  # noqa: E402
from stock_data_ingestion.services.ingestion_runner import IngestionRunner  # noqa: E402

FX_SQL = "select * from fx_rates where base_currency='HKD' and quote_currency='CNY' and rate_type='spot_sell' and rate_date='2026-10-06'"


def fx_export(s: Scenario, request_ids: list[str]) -> dict:
    ids = ",".join(f"'{r}'" for r in request_ids)
    return {
        "current_record_HKDCNY_spot_sell_2026-10-06": s.rows(FX_SQL),
        "archived_records": s.rows("select * from market_context_record_revisions where json_extract(business_key,'$.rate_type')='spot_sell' and json_extract(business_key,'$.rate_date')='2026-10-06'"),
        "request_record_links_for_spot_sell_current_or_archived": s.rows(
            f"select l.* from market_context_request_records l where l.request_id in ({ids}) and l.record_id in "
            f"(select record_id from fx_rates where rate_type='spot_sell' and rate_date='2026-10-06' union select record_id from market_context_record_revisions where json_extract(business_key,'$.rate_type')='spot_sell' and json_extract(business_key,'$.rate_date')='2026-10-06')"
        ),
        "request_record_link_counts_per_request": s.rows(f"select request_id, record_type, count(*) as n from market_context_request_records where request_id in ({ids}) group by request_id, record_type"),
        "ingestion_requests": s.rows(f"select request_id, idempotency_key, status, created_at from ingestion_requests where request_id in ({ids})"),
    }


def scenario_same_day_update() -> None:
    s = Scenario("04_same_day_update", "同日更新：模拟 85.59→86.59，重复请求，重启后重复。当前记录 86.59，旧记录归档，溯源与新记录一致。")
    s.clock.set(9)
    nine = s.fetch("09:00 first collection (BOC spot_sell=85.59)", T._fx_latest())
    old = s.rows(FX_SQL)[0]
    s.clock.set(10)
    s.fake.fx_shift = 1.0
    ten = s.fetch("10:00 vendor now quotes 86.59 -> replace + archive", T._fx_latest())
    new = s.rows(FX_SQL)[0]
    calls = len(s.fake.calls)
    repeat = s.fetch("10:xx repeat (same idempotency key) -> served from store", T._fx_latest())
    s.restart()
    restarted = s.fetch("process restart, same request -> served from store", T._fx_latest())
    archived = s.rows(f"select * from market_context_record_revisions where record_id='{old['record_id']}'")
    checks = {
        "first_value_85.59": nine.result.value == 85.59,
        "update_value_86.59": ten.result.value == 86.59 and ten.status == "success",
        "current_row_is_86.59_new_record_id": new["rate"] == 86.59 and new["record_id"] != old["record_id"],
        "current_row_provenance_is_new": new["request_id"] == ten.result.provenance.stock_data_request_id and new["raw_payload_id"] != old["raw_payload_id"],
        "old_record_archived_whole": len(archived) == 1 and archived[0]["record_json"]["rate"] == 85.59 and archived[0]["superseded_by_record_id"] == new["record_id"],
        "repeat_and_restart_no_vendor_calls": len(s.fake.calls) == calls,
        "repeat_served_from_store_86.59": repeat.result.value == 86.59 and any(w.startswith("served_from_store") for w in repeat.warnings) and new["record_id"] in repeat.result.provenance.record_ids,
        "restart_served_from_store_86.59": restarted.result.value == 86.59 and any(w.startswith("served_from_store") for w in restarted.warnings) and new["record_id"] in restarted.result.provenance.record_ids,
        "repeat_same_idempotency_key_as_10:00": repeat.result.provenance.idempotency_key == ten.result.provenance.idempotency_key == restarted.result.provenance.idempotency_key,
    }
    s.finish(fx_export(s, [nine.result.provenance.stock_data_request_id, ten.result.provenance.stock_data_request_id]), checks)


def scenario_late_older_result() -> None:
    s = Scenario("05_late_older_result", "迟到旧结果：10点请求A保存86.59；11点请求B的供应商抓取完成于08:00（旧数据）被拒绝；B 重复请求与重启后重复均返回 86.59，且请求—记录关联存在。")
    s.clock.set(9)
    nine = s.fetch("09:00 first collection 85.59", T._fx_latest())
    s.clock.set(10)
    s.fake.fx_shift = 1.0
    ten = s.fetch("10:00 request A: 86.59 replaces 85.59", T._fx_latest())
    current = s.rows(FX_SQL)[0]
    revisions_before = len(s.rows("select record_id from market_context_record_revisions"))
    s.clock.set(11)
    s.fake.fx_shift = 0.0
    early = datetime(2026, 10, 6, 8, 0, 0, tzinfo=SH)
    adapter_base.now_asia_shanghai = lambda: early
    late = s.fetch("11:00 request B: vendor result fetch_time=08:00 (older than A) -> stale_update_rejected, adopts A's 86.59", T._fx_latest(), note="adapters.base.now_asia_shanghai pinned to 08:00 so the new fetch is older than A", fetch_time_pinned="2026-10-06T08:00:00+08:00")
    after = s.rows(FX_SQL)[0]
    calls = len(s.fake.calls)
    adapter_base.now_asia_shanghai = lambda: datetime(2026, 10, 6, 11, 30, tzinfo=SH)
    s.clock.set(11, 30)
    repeat = s.fetch("11:30 request B repeated (same hourly idempotency key) -> store replay via request-record link", T._fx_latest())
    s.restart()
    restarted = s.fetch("process restart, request B again -> store replay via request-record link", T._fx_latest())
    b_id = late.result.provenance.stock_data_request_id
    links = s.rows(f"select * from market_context_request_records where request_id='{b_id}' and record_id='{current['record_id']}'")
    checks = {
        "A_value_86.59": ten.result.value == 86.59,
        "B_first_response_86.59_with_stale_update_rejected": late.result.value == 86.59 and any(w.startswith("stale_update_rejected") for w in late.warnings),
        "current_row_unchanged_after_B": after["record_id"] == current["record_id"] and after["rate"] == 86.59 and after["raw_payload_id"] == current["raw_payload_id"],
        "nothing_new_archived_by_B": len(s.rows("select record_id from market_context_record_revisions")) == revisions_before,
        "record_request_id_still_A_not_B": after["request_id"] == ten.result.provenance.stock_data_request_id and after["request_id"] != b_id,
        "B_linked_to_adopted_record": len(links) == 1,
        "B_repeat_no_vendor_calls": len(s.fake.calls) == calls,
        "B_repeat_86.59_served_from_store_with_A_record": repeat.result.value == 86.59 and any(w.startswith("served_from_store") for w in repeat.warnings) and current["record_id"] in repeat.result.provenance.record_ids,
        "B_restart_86.59_served_from_store_with_A_record": restarted.result.value == 86.59 and any(w.startswith("served_from_store") for w in restarted.warnings) and current["record_id"] in restarted.result.provenance.record_ids,
        "B_repeat_and_restart_same_idempotency_key_as_B": repeat.result.provenance.idempotency_key == late.result.provenance.idempotency_key == restarted.result.provenance.idempotency_key,
        "B_repeat_series_has_no_85.59": all(o.values.get("rate") != 85.59 for o in repeat.result.series) and all(o.values.get("rate") != 85.59 for o in restarted.result.series),
    }
    export = fx_export(s, [nine.result.provenance.stock_data_request_id, ten.result.provenance.stock_data_request_id, b_id])
    export["request_B_links_to_current_record"] = links
    export["request_B_resolution_via_query_service"] = None
    from stock_data_ingestion.services.query_service import QueryService

    with s.runner.database.session() as session:
        resolved = QueryService(session).resolve_request_records("fx", b_id)
        export["request_B_resolution_via_query_service"] = {"source": resolved["source"], "missing": resolved["missing"], "chains": resolved["chains"], "n_rows": len(resolved["rows"]), "current_record_in_rows": any(r["record_id"] == current["record_id"] for r in resolved["rows"])}
    s.finish(export, checks)


def snapshot_export(s: Scenario) -> dict:
    return {
        "realtime_records_commodity_prices": s.rows("select record_id, trade_date, observed_at, latest, pre_settle, date_confidence, date_resolution_details, validation_status, request_id from commodity_prices where frequency='realtime'"),
        "reference_daily_bars_last_two_sessions": s.rows("select record_id, trade_date, close, settle, validation_status, fetch_time, request_id from commodity_prices where frequency='1d' order by trade_date desc limit 2"),
        "daily_bar_count": s.rows("select count(*) as n from commodity_prices where frequency='1d'"),
        "raw_payload_index_snapshot": s.rows("select raw_payload_id, source_api, fetch_completed_at, rows_fetched, request_id from raw_payload_index where source_api='futures_zh_spot'"),
        "request_record_links_realtime": s.rows("select l.* from market_context_request_records l join ingestion_requests r on r.request_id=l.request_id where l.record_type='commodity_price' and r.idempotency_key like '%realtime%'"),
        "ingestion_requests": s.rows("select request_id, idempotency_key, status from ingestion_requests"),
    }


def unknown_checks(resp, s: Scenario, must_mention: list[str]) -> dict:
    err = next((e for e in resp.errors if e.error_code == "SNAPSHOT_DATE_UNCONFIRMED"), None)
    return {
        "status_failed": resp.status == "failed",
        "error_SNAPSHOT_DATE_UNCONFIRMED": err is not None,
        "data_date_null_observed_at_null_value_null": resp.result.data_date is None and resp.result.observed_at is None and resp.result.value is None,
        "quality_unknown_date_unusable_is_fresh_null": resp.result.quality.status == "unknown_date" and resp.result.quality.usable is False and resp.result.quality.is_fresh is None,
        "no_realtime_standard_record": s.rows("select count(*) as n from commodity_prices where frequency='realtime'")[0]["n"] == 0,
        "raw_snapshot_kept": s.rows("select count(*) as n from raw_payload_index where source_api='futures_zh_spot'")[0]["n"] >= 1,
        "error_message_mentions_evidence": err is not None and all(m in err.error_message for m in must_mention),
    }


def scenario_unknown_date_daily_failed() -> None:
    s = Scenario("06a_unknown_date_daily_failed", "日期无法确认（a）：日线采集失败，realtime 快照无参考日线，日期未知、不可用、不写标准 realtime 记录。")
    s.fake.fail.add("futures_zh_daily_sina")
    resp = s.fetch("CU0 realtime; futures_zh_daily_sina blocked", T._cu_realtime())
    s.finish(snapshot_export(s), unknown_checks(resp, s, ["no daily bars"] if "no daily bars" in (resp.errors[0].error_message if resp.errors else "") else []))


def _blocked_rescore(blocked_date):
    original = IngestionRunner._rescore_record

    def mark(self, record, conflicts):
        record = original(self, record, conflicts)
        if getattr(record, "frequency", None) == "1d" and getattr(record, "record_type", "") == "commodity_price" and record.trade_date == blocked_date:
            return record.model_copy(update={"validation_status": "quarantined"})
        return record

    IngestionRunner._rescore_record = mark
    return original


def scenario_unknown_date_blocked(name: str, blocked_date, label: str) -> None:
    s = Scenario(name, f"日期无法确认：参考日线中{label}（{blocked_date}）被隔离（validation_status=quarantined）。确认立即失败，不跳过被隔离日线找更早日期。")
    original = _blocked_rescore(blocked_date)
    try:
        resp = s.fetch(f"CU0 realtime; daily bar {blocked_date} quarantined", T._cu_realtime(), note=f"IngestionRunner._rescore_record patched: 1d bar {blocked_date} -> validation_status=quarantined", quarantined_daily_bar=str(blocked_date))
    finally:
        IngestionRunner._rescore_record = original
    bars = s.rows(f"select record_id, trade_date, validation_status from commodity_prices where frequency='1d' and trade_date='{blocked_date}'")
    checks = unknown_checks(resp, s, [bars[0]["record_id"], "validation_status=quarantined", "no earlier bar is substituted"])
    checks["blocked_bar_persisted_as_quarantined"] = bool(bars) and bars[0]["validation_status"] == "quarantined"
    checks["all_30_daily_bars_kept"] = s.rows("select count(*) as n from commodity_prices where frequency='1d'")[0]["n"] == 30
    export = snapshot_export(s)
    export["quarantined_reference_bar"] = bars
    s.finish(export, checks)


if __name__ == "__main__":
    which = sys.argv[1:] or ["4", "5", "6a", "6b", "6c"]
    if "4" in which:
        scenario_same_day_update()
    if "5" in which:
        scenario_late_older_result()
    if "6a" in which:
        scenario_unknown_date_daily_failed()
    if "6b" in which:
        scenario_unknown_date_blocked("06b_unknown_date_last_bar_quarantined", T.LAST_TRADING, "最后一条日线")
    if "6c" in which:
        scenario_unknown_date_blocked("06c_unknown_date_prev_bar_quarantined", T.LAST_TRADING - timedelta(days=1), "前一条日线")
