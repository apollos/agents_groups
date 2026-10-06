"""Live verification scenarios 1-3 via the real tool CLI (real vendors, the tool's own SQLite).

Run: cd tools/stock_data_collector && /home/yu/.venv/mydev/bin/python /tmp/mctx_verify/live_driver.py [1 2 3]
Each CLI call:  python -m stock_data_ingestion.cli --debug --log-file <evidence>/debug.jsonl fetch market-context ... --trace-id live-<scenario>-<n>
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

TOOL_DIR = Path("/home/yu/Workspace/agents_groups/tools/stock_data_collector")
AGENT_DIR = Path("/home/yu/Workspace/agents_groups/agents/intelligence_collector_agent")
EVIDENCE_ROOT = AGENT_DIR / "docs/acceptance/evidence/market_context_review_20261006"
PY = "/home/yu/.venv/mydev/bin/python"
os.chdir(TOOL_DIR)

from stock_data_ingestion.config import load_config  # noqa: E402
from stock_data_ingestion.env import ensure_env_loaded  # noqa: E402

ensure_env_loaded()
load_config.cache_clear()
SQLITE = Path(load_config().storage.sqlite_path)
COMMIT = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=TOOL_DIR, text=True).strip()
TABLES = {"fx_rate": "fx_rates", "interest_rate": "interest_rates", "index_bar": "index_bars", "commodity_price": "commodity_prices"}


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def q(sql: str, params=()) -> list[dict]:
    con = sqlite3.connect(SQLITE)
    con.row_factory = sqlite3.Row
    try:
        out = []
        for r in con.execute(sql, params):
            d = dict(r)
            for k in ("date_resolution_details", "record_json", "business_key"):
                if isinstance(d.get(k), str):
                    try:
                        d[k] = json.loads(d[k])
                    except ValueError:
                        pass
            out.append(d)
        return out
    finally:
        con.close()


class Live:
    def __init__(self, name: str, description: str, *, keep: bool = False) -> None:
        self.name, self.dir = name, EVIDENCE_ROOT / name
        if self.dir.exists() and not keep:
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[dict] = []
        if keep and (self.dir / "inputs.json").exists():
            self.calls = json.loads((self.dir / "inputs.json").read_text(encoding="utf-8"))["calls"]
        self.inputs = {"scenario": name, "description": description, "data_mode": "live", "code_commit": COMMIT, "driver": "live_driver.py (copied alongside)",
                       "tool_sqlite": str(SQLITE), "python": PY, "cwd": str(TOOL_DIR), "env_note": "tools/stock_data_collector/.env loaded by the CLI (credentials redacted, STOCK_DATA_PREFER_IPV4 honoured)", "calls": self.calls}

    def call(self, label: str, args: list[str], *, note: str = "") -> dict:
        n = len(self.calls) + 1
        trace = f"live-{self.name}-{n}"
        cmd = [PY, "-m", "stock_data_ingestion.cli", "--debug", "--log-file", str(self.dir / "debug.jsonl"), "fetch", "market-context", *args, "--trace-id", trace, "--requested-by", "review_verification_20261006"]
        started = datetime.now(timezone(timedelta(hours=8)))
        proc = subprocess.run(cmd, cwd=TOOL_DIR, text=True, capture_output=True, timeout=600)
        ended = datetime.now(timezone(timedelta(hours=8)))
        (self.dir / f"response_{n}.json").write_text(proc.stdout, encoding="utf-8")
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        res = payload.get("result") or {}
        prov = res.get("provenance") or {}
        entry = {"n": n, "label": label, "note": note, "trace_id": trace, "command": cmd[1:], "started_at": started.isoformat(), "ended_at": ended.isoformat(), "returncode": proc.returncode,
                 "response_file": f"response_{n}.json", "status": payload.get("status"), "value": res.get("value"), "unit": res.get("unit"), "data_date": res.get("data_date"),
                 "quality_status": (res.get("quality") or {}).get("status"), "is_fresh": (res.get("quality") or {}).get("is_fresh"), "staleness_days": (res.get("quality") or {}).get("staleness_days"),
                 "stock_data_request_id": prov.get("stock_data_request_id"), "idempotency_key": prov.get("idempotency_key"), "record_ids": prov.get("record_ids") or [],
                 "warnings": payload.get("warnings"), "error_codes": [e.get("error_code") for e in payload.get("errors") or []], "stderr_tail_if_no_json": None if payload else proc.stderr[-2000:]}
        self.calls.append(entry)
        print(f"  [{n}] {label}: status={entry['status']} value={entry['value']} {entry['unit']} date={entry['data_date']} q={entry['quality_status']} fresh={entry['is_fresh']} errors={entry['error_codes']}")
        return payload

    def events(self) -> list[dict]:
        p = self.dir / "debug.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []

    def db_export_for(self, payloads: list[dict]) -> dict:
        req_ids = [((p.get("result") or {}).get("provenance") or {}).get("stock_data_request_id") for p in payloads]
        req_ids = [r for r in req_ids if r]
        rec_ids = sorted({rid for p in payloads for rid in (((p.get("result") or {}).get("provenance") or {}).get("record_ids") or [])})
        ph = lambda xs: ",".join("?" * len(xs))  # noqa: E731
        export: dict = {
            "ingestion_requests": q(f"select request_id, idempotency_key, status, created_at, updated_at from ingestion_requests where request_id in ({ph(req_ids)})", req_ids) if req_ids else [],
            "request_record_links": q(f"select * from market_context_request_records where request_id in ({ph(req_ids)})", req_ids) if req_ids else [],
            "current_records": {},
            "archived_records_touching_these_ids": q(f"select record_id, record_type, table_name, business_key, superseded_by_record_id, archived_at, request_id from market_context_record_revisions where record_id in ({ph(rec_ids)}) or superseded_by_record_id in ({ph(rec_ids)})", rec_ids + rec_ids) if rec_ids else [],
        }
        for rtype, table in TABLES.items():
            rows = q(f"select * from {table} where record_id in ({ph(rec_ids)}) order by 2", rec_ids) if rec_ids else []
            if rows:
                export["current_records"][table] = [{k: v for k, v in r.items() if k not in {"field_provenance", "raw_payload_ref"}} for r in rows[-3:]] if len(rows) > 3 else rows
                export["current_records"][f"{table}_count_in_provenance"] = len(rows)
        return export

    def finish(self, db_export: dict, checks: dict, extra: dict | None = None) -> None:
        self.inputs["checks"] = checks
        if extra:
            self.inputs.update(extra)
        dump(self.dir / "inputs.json", self.inputs)
        dump(self.dir / "db_export.json", db_export)
        failed = {k: v for k, v in checks.items() if v is not True}
        print(f"[{self.name}] checks: {len(checks) - len(failed)}/{len(checks)} passed" + (f"  FAILED: {failed}" if failed else ""))


def source_crosscheck() -> dict:
    """Direct vendor reads (same akshare functions the tool binds) so date/value/unit can be compared."""
    import akshare as ak
    from stock_data_ingestion.utils.network import apply_ipv4_preference_if_configured

    apply_ipv4_preference_if_configured()
    out: dict = {}

    def last(frame, date_col, cols):
        frame = frame.sort_values(date_col)
        row = frame.iloc[-1]
        return {"date": str(row[date_col])[:10], **{c: (None if str(row[c]) == "nan" else float(row[c])) for c in cols}}

    try:
        out["000300"] = {"func": "stock_zh_index_daily(symbol=sh000300)", **last(ak.stock_zh_index_daily(symbol="sh000300"), "date", ["close"])}
    except Exception as exc:  # noqa: BLE001
        out["000300"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out["HSTECH"] = {"func": "stock_hk_index_daily_sina(symbol=HSTECH)", **last(ak.stock_hk_index_daily_sina(symbol="HSTECH"), "date", ["close"])}
    except Exception as exc:  # noqa: BLE001
        out["HSTECH"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        fx = ak.currency_boc_sina(symbol="港币", start_date="20260901", end_date="20261006")
        out["HKDCNY"] = {"func": "currency_boc_sina(symbol=港币)", "unit": "CNY per 100 HKD", **last(fx, "日期", ["中行钞卖价/汇卖价", "中行汇买价"])}
    except Exception as exc:  # noqa: BLE001
        out["HKDCNY"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out["CU0_1d"] = {"func": "futures_zh_daily_sina(symbol=CU0)", "unit": "CNY/ton", **last(ak.futures_zh_daily_sina(symbol="CU0"), "date", ["close", "settle"])}
    except Exception as exc:  # noqa: BLE001
        out["CU0_1d"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        y = ak.bond_china_yield(start_date="20260901", end_date="20261006")
        y = y[y["曲线名称"] == "中债国债收益率曲线"]
        out["CN_CGB_10Y"] = {"func": "bond_china_yield (曲线名称=中债国债收益率曲线, 列=10年)", "unit": "percent", **last(y, "日期", ["10年"])}
    except Exception as exc:  # noqa: BLE001
        out["CN_CGB_10Y"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def scenario_1(recheck: bool = False) -> None:
    s = Live("01_live_five_categories", "五类真实采集：000300（A股指数）、HSTECH（港股指数）、HKDCNY（汇率）、CU0 日线（商品）、CN_CGB_10Y（利率）。与来源日期/数值/单位一致；过期数据明确标记。", keep=recheck)
    if recheck:
        # Recompute checks from the saved responses (no new vendor calls, evidence stays live-fetched).
        payloads = [json.loads((s.dir / f"response_{i}.json").read_text(encoding="utf-8")) for i in range(1, 6)]
        cross = json.loads((s.dir / "source_crosscheck.json").read_text(encoding="utf-8"))["reads"]
    else:
        payloads = [
            s.call("equity_index 000300", ["--context-type", "equity_index", "--symbol", "000300", "--context-id", "index_csi_300"]),
            s.call("hk_index HSTECH", ["--context-type", "hk_index", "--symbol", "HSTECH", "--context-id", "index_hstech"]),
            s.call("fx HKDCNY", ["--context-type", "fx", "--symbol", "HKDCNY", "--context-id", "fx_hkd_cny"]),
            s.call("commodity CU0 1d", ["--context-type", "commodity", "--symbol", "CU0", "--frequency", "1d", "--context-id", "commodity_cu0"]),
            s.call("interest_rate CN_CGB_10Y", ["--context-type", "interest_rate", "--symbol", "CN_CGB_10Y", "--context-id", "rate_cn_cgb_10y"]),
        ]
        cross = source_crosscheck()
        dump(s.dir / "source_crosscheck.json", {"note": "direct akshare reads right after the CLI calls; compare date/value/unit with response_N.json result.data_date/value/unit", "reads": cross})
    ev = s.events()
    checks = {}
    keys = ["000300", "HSTECH", "HKDCNY", "CU0_1d", "CN_CGB_10Y"]
    for p, key in zip(payloads, keys):
        res = p.get("result") or {}
        qd = res.get("quality") or {}
        src = cross.get(key) or {}
        as_of = (res.get("request_window") or {}).get("as_of")
        checks[f"{key}_has_value_and_date"] = res.get("value") is not None and res.get("data_date") is not None
        if res.get("data_date") == as_of:
            checks[f"{key}_fresh_same_day"] = qd.get("is_fresh") is True and qd.get("data_date_matches_as_of") is True
        else:
            # Not the request day: the gap must be stated explicitly; `stale` iff beyond max_staleness_days.
            over = (qd.get("staleness_days") or 0) > (qd.get("max_staleness_days") or 0)
            checks[f"{key}_older_data_marked_explicitly"] = (
                qd.get("data_date_matches_as_of") is False and qd.get("staleness_days") is not None
                and ((qd.get("is_fresh") is False and qd.get("status") == "stale") if over else (qd.get("is_fresh") is True and qd.get("status") == "fresh"))
            )
        if "date" in src:
            checks[f"{key}_date_matches_source"] = res.get("data_date") == src["date"]
            src_val = src.get("close") if key != "HKDCNY" else src.get("中行钞卖价/汇卖价")
            if key == "CN_CGB_10Y":
                src_val = src.get("10年")
            checks[f"{key}_value_matches_source"] = src_val is not None and res.get("value") is not None and abs(float(res["value"]) - float(src_val)) < 1e-6
    traces = {e["trace_id"] for e in ev}
    checks["debug_log_has_all_five_traces"] = all(f"live-{s.name}-{i}" in traces for i in range(1, 6))
    checks["debug_log_has_provider_calls_and_summaries"] = {"provider_call", "request_summary", "record_write", "request_record_link", "quality_decision"} <= {e["event"] for e in ev}
    checks["all_events_data_mode_live"] = all(e.get("data_mode") == "live" for e in ev)
    units = {key: (p.get("result") or {}).get("unit") for p, key in zip(payloads, keys)}
    s.finish(s.db_export_for(payloads), checks, {"units": units})


def scenario_2(recheck: bool = False) -> None:
    s = Live("02_live_realtime_reread", "实时快照重复读取：CU0 realtime 首次、同幂等键重复、重启（新进程）后重复。日期/数值/来源一致；重复读取不重新确认日期。幂等键按分钟，三次调用必须落在同一分钟内。", keep=recheck)
    attempts = 0
    if recheck:
        attempts = json.loads((s.dir / "inputs.json").read_text(encoding="utf-8")).get("attempts", 1)
        n = len(s.calls)
        p1, p2, p3 = (json.loads((s.dir / f"response_{i}.json").read_text(encoding="utf-8")) for i in range(n - 2, n + 1))
    while not recheck:
        attempts += 1
        # Start right after a minute boundary so three cold CLI processes share the minute key.
        now = time.time()
        wait = 60 - (now % 60) + 0.5
        print(f"  waiting {wait:.1f}s for the next minute boundary (attempt {attempts})")
        time.sleep(wait)
        before = len(s.calls)
        args = ["--context-type", "commodity", "--symbol", "CU0", "--frequency", "realtime", "--context-id", "commodity_cu0"]
        p1 = s.call("CU0 realtime first read (new process)", args)
        p2 = s.call("CU0 realtime repeat, same minute (new process)", args)
        p3 = s.call("CU0 realtime repeat after 'restart' (another new process, same minute)", args)
        keys = [c["idempotency_key"] for c in s.calls[before:]]
        if len(set(keys)) == 1 or attempts >= 3:
            break
        print("  idempotency keys differ (crossed a minute boundary) -> redo in a fresh minute")
    ev = s.events()
    last3 = s.calls[-3:]
    t1, t2, t3 = (c["trace_id"] for c in last3)
    by_trace = lambda t: [e for e in ev if e["trace_id"] == t]  # noqa: E731
    checks = {
        "three_calls_same_idempotency_key": len({c["idempotency_key"] for c in last3}) == 1,
        "same_date_value_source": len({(c["data_date"], c["value"]) for c in last3}) == 1 and len({((p.get("result") or {}).get("source") or {}).get("source_api") for p in (p1, p2, p3)}) == 1,
        "same_record_ids": len({tuple(sorted(c["record_ids"])) for c in last3}) == 1,
        "first_call_confirmed_date_once": sum(1 for e in by_trace(t1) if e["event"] == "snapshot_date_resolution") == 1,
        "repeats_served_from_store": all(any(str(w).startswith("served_from_store") for w in (c["warnings"] or [])) for c in last3[1:]),
        "repeats_do_not_reconfirm_date": all(not [e for e in by_trace(t) if e["event"] == "snapshot_date_resolution"] for t in (t2, t3)),
        "repeats_make_no_vendor_calls": all(not [e for e in by_trace(t) if e["event"] == "provider_call"] for t in (t2, t3)),
        "repeats_log_store_replay": all([e for e in by_trace(t) if e["event"] == "store_replay"] for t in (t2, t3)),
        "first_call_result_dated_or_unknown_explicitly": bool((last3[0]["quality_status"] in {"fresh", "stale"} and last3[0]["data_date"]) or (last3[0]["quality_status"] == "unknown_date" and last3[0]["data_date"] is None)),
    }
    export = s.db_export_for([p1, p2, p3])
    rec_ids = last3[0]["record_ids"]
    if rec_ids:
        ph = ",".join("?" * len(rec_ids))
        snaps = q(f"select record_id, trade_date, observed_at, latest, pre_settle, date_confidence, date_resolution_details, validation_status, request_id, fetch_time from commodity_prices where record_id in ({ph}) and frequency='realtime'", rec_ids)
        export["snapshot_date_confirmation"] = {"realtime_records": snaps, "reference_daily_bars": []}
        for sn in snaps:
            details = sn.get("date_resolution_details") or {}
            ref_ids = [b.get("record_id") for b in (details.get("reference_bars") or []) if isinstance(b, dict)] or [details.get("confirmation_bar_record_id")]
            ref_ids = [r for r in ref_ids if r]
            if ref_ids:
                export["snapshot_date_confirmation"]["reference_daily_bars"] += q(f"select record_id, trade_date, close, settle, validation_status, fetch_time from commodity_prices where record_id in ({','.join('?' * len(ref_ids))})", ref_ids)
    resolution = [e for e in ev if e["event"] == "snapshot_date_resolution"]
    s.finish(export, checks, {"attempts": attempts, "snapshot_date_resolution_events": resolution})


def scenario_3() -> None:
    s = Live("03_live_param_conflict", "参数冲突：CU0 正常后 contract=CU2612；US_CGB_10Y 正常后 tenor=2Y。冲突请求被拒绝（INVALID_REQUEST），0 次供应商调用。")
    p1 = s.call("CU0 1d normal", ["--context-type", "commodity", "--symbol", "CU0", "--frequency", "1d", "--context-id", "commodity_cu0"])
    p2 = s.call("CU0 1d with --contract CU2612 (conflicts with symbol binding)", ["--context-type", "commodity", "--symbol", "CU0", "--frequency", "1d", "--context-id", "commodity_cu0", "--contract", "CU2612"])
    p3 = s.call("US_CGB_10Y normal", ["--context-type", "interest_rate", "--symbol", "US_CGB_10Y", "--context-id", "rate_us_cgb_10y"])
    p4 = s.call("US_CGB_10Y with --tenor 2Y (conflicts with symbol binding)", ["--context-type", "interest_rate", "--symbol", "US_CGB_10Y", "--context-id", "rate_us_cgb_10y", "--tenor", "2Y"])
    ev = s.events()
    by_trace = lambda t: [e for e in ev if e["trace_id"] == t]  # noqa: E731
    t2, t4 = s.calls[1]["trace_id"], s.calls[3]["trace_id"]
    expected_msg = "symbol=CU0 的 contract 配置为 CU0，请求 contract=CU2612，与 symbol 绑定不一致。请使用已注册的 CU2612 symbol。"
    checks = {
        "CU0_normal_ok": p1.get("status") in {"success", "partial_success"} and (p1.get("result") or {}).get("value") is not None,
        "CU0_contract_conflict_rejected": p2.get("status") == "failed" and [e.get("error_code") for e in p2.get("errors") or []] == ["INVALID_REQUEST"] and (p2.get("errors") or [{}])[0].get("retryable") is False,
        "CU0_conflict_message_exact": any(e.get("error_message") == expected_msg for e in p2.get("errors") or []),
        "CU0_conflict_zero_vendor_calls": not [e for e in by_trace(t2) if e["event"] == "provider_call"],
        "CU0_conflict_identity_check_rejected": any(e["event"] == "identity_check" and e.get("decision") == "rejected" and "contract" in [c["field"] for c in e.get("conflicting_fields", [])] for e in by_trace(t2)),
        "CU0_conflict_warning_business_param_rejected": any(e["event"] == "business_param_rejected" and e["level"] == "WARNING" for e in by_trace(t2)),
        "CU0_conflict_no_records_written": not [e for e in by_trace(t2) if e["event"] in {"record_write", "request_record_link"}] and not ((p2.get("result") or {}).get("provenance") or {}).get("record_ids"),
        "US10Y_normal_ok": p3.get("status") in {"success", "partial_success"} and (p3.get("result") or {}).get("value") is not None,
        "US10Y_tenor_conflict_rejected": p4.get("status") == "failed" and [e.get("error_code") for e in p4.get("errors") or []] == ["INVALID_REQUEST"],
        "US10Y_conflict_zero_vendor_calls": not [e for e in by_trace(t4) if e["event"] == "provider_call"],
        "US10Y_conflict_identity_check_rejected": any(e["event"] == "identity_check" and e.get("decision") == "rejected" and "tenor" in [c["field"] for c in e.get("conflicting_fields", [])] for e in by_trace(t4)),
        "US10Y_conflict_no_records_written": not [e for e in by_trace(t4) if e["event"] in {"record_write", "request_record_link"}],
    }
    s.finish(s.db_export_for([p1, p3]), checks, {"identity_check_events": [e for e in ev if e["event"] == "identity_check"]})


if __name__ == "__main__":
    which = sys.argv[1:] or ["1", "2", "3"]
    print(f"tool sqlite: {SQLITE}  commit: {COMMIT}")
    if "1" in which:
        scenario_1()
    if "1-recheck" in which:
        scenario_1(recheck=True)
    if "2" in which:
        scenario_2()
    if "2-recheck" in which:
        scenario_2(recheck=True)
    if "3" in which:
        scenario_3()
