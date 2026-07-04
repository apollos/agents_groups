from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import yaml

from agent_trade_intel.config import load_config
from agent_trade_intel.dashboard import DashboardService
from agent_trade_intel.db import SQLiteStore
from agent_trade_intel.demand import DemandRegistry
from agent_trade_intel.heartbeat import HeartbeatRecorder
from agent_trade_intel.queue import SQLiteMessageQueue
from agent_trade_intel.session import AgentSessionRepository
from agent_trade_intel.tickets import TicketRepository


def _config(tmp_path: Path) -> Path:
    cfg = {
        "agent": {"agent_id": "test_intel", "agent_group": "intelligence_collector"},
        "openclaw": {"model": {"primary": "openai/gpt-5.5", "fallbacks": [], "require_registered": False, "allow_openclaw_default": False}},
        "runtime": {"sqlite_path": str(tmp_path / "intel.db"), "workspace_root": str(tmp_path), "log_dir": str(tmp_path / "logs"), "timezone": "Asia/Shanghai"},
        "tools": {"python_executable": "python", "market_intelligence_collector": {"enabled": True}, "stock_data_collector": {"enabled": True}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "intel.db")
    store.init_schema()
    return store


def _demand(demand_id: str = "d1") -> dict:
    return {
        "schema_version": "demand.v1",
        "demand_id": demand_id,
        "demand_type": "intraday_monitoring",
        "source_type": "test_harness",
        "status": "active",
        "priority": "high",
        "active_from": "2026-06-11",
        "active_to": "2026-12-31",
        "target_scope": {"scope_type": "explicit_targets", "include_tickers": ["300750.SZ"], "pool_layers": ["current_holding"]},
        "targets": [{"target_type": "ticker", "ticker": "300750.SZ", "target_id": "company_300750", "pool_layer": "current_holding", "sellability": "sellable"}],
        "task_profile": {},
        "test_mode": True,
        "idempotency_key": f"demand:{demand_id}",
    }


def _seed(store: SQLiteStore) -> None:
    q = SQLiteMessageQueue(store)
    tickets = TicketRepository(store)
    DemandRegistry(store, q).register(_demand(), activate=True)
    ticket_id = tickets.create_ticket(ticket_type="COLLECTION_TASK_TICKET", source_agent="test_intel", summary_cn="任务")
    q.publish("intelligence.collection", {"ticket_id": ticket_id})
    dead = q.publish("t", {"x": 1})
    msg = q.lease(topics=["t"], worker_id="w")
    q.nack(msg.message_id, {"error_code": "E"}, retryable=False)
    assert dead
    AgentSessionRepository(store, "test_intel").start(model_ref="openai/gpt-5.5")
    HeartbeatRecorder(store, "test_intel").beat(state="processing", worker_id="test_intel:w1")
    with store.session() as con:
        con.execute(
            "INSERT INTO structured_events(event_id, ticker, event_type, summary_cn, payload_json, idempotency_key) "
            "VALUES ('e1', '300750.SZ', 'risk', '事件', '{}', 'ek1')"
        )
        con.execute(
            "INSERT INTO market_features(feature_id, ticker, feature_window, bucket_start, abnormality_score, feature_json, idempotency_key) "
            "VALUES ('f1', '300750.SZ', '10m', '2026-06-11T10:30:00+08:00', 0.9, '{}', 'fk1')"
        )
        con.execute(
            "INSERT INTO collection_runs(run_id, tool_name, operation, status) "
            "VALUES ('r1', 'stock_data_collector', 'fetch_historical_bars', 'success')"
        )
        con.execute(
            "INSERT INTO collection_tasks(task_id, task_type, tool_name, ticker, status, payload_json, idempotency_key) "
            "VALUES ('t1', 'intraday_snapshot_10m', 'stock_data_collector', '300750.SZ', 'done', '{}', 'tk1')"
        )


def test_dashboard_overview_reflects_live_data(tmp_path: Path):
    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path)
    _seed(store)
    service = DashboardService(cfg, state_store=store, bus_store=store, data_store=store)
    d = service.overview()

    assert d["agent_id"] == "test_intel"
    assert d["session"]["status"] == "running"
    assert d["worker_liveness"] == "active"
    assert d["queue_depth"].get("open", 0) >= 1
    assert d["queue_depth"].get("dead", 0) == 1
    assert d["dead_letters"][0]["error"]["error_code"] == "E"
    assert any(t["ticket_type"] == "COLLECTION_TASK_TICKET" for t in d["open_tickets_by_type"])
    assert d["demands"][0]["demand_id"] == "d1"
    assert d["events_created_today"] == 1
    assert d["features_created_today"] == 1
    assert d["recent_runs"][0]["tool_name"] == "stock_data_collector"
    assert any(t["task_type"] == "intraday_snapshot_10m" for t in d["tasks_today_by_type_status"])
    # overview is JSON-serialisable (what the HTTP API returns)
    assert json.loads(json.dumps(d, default=str))["agent_id"] == "test_intel"

    # a second poll after new writes reflects the change without restarting anything
    SQLiteMessageQueue(store).publish("intelligence.collection", {"ticket_id": "another"})
    d2 = service.overview()
    assert sum(d2["queue_depth"].values()) == sum(d["queue_depth"].values()) + 1


def test_dashboard_http_endpoints(tmp_path: Path):
    cfg = load_config(_config(tmp_path))
    store = _store(tmp_path)
    _seed(store)

    # Build the same handler run_dashboard uses, but bind to an ephemeral port for the test.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from agent_trade_intel.dashboard import DASHBOARD_HTML, DashboardService

    service = DashboardService(cfg, state_store=store, bus_store=store, data_store=store)
    page = DASHBOARD_HTML.replace("__REFRESH_SECONDS__", "5")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/api/overview":
                body = json.dumps(service.overview(), ensure_ascii=False, default=str).encode("utf-8")
                ctype = "application/json"
            else:
                body = page.encode("utf-8")
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as res:
            html = res.read().decode("utf-8")
        assert "情报收集员 Agent · 实时看板" in html
        assert "/api/overview" in html
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=5) as res:
            data = json.loads(res.read().decode("utf-8"))
        assert data["agent_id"] == "test_intel"
        assert data["queue_depth"].get("dead") == 1
    finally:
        server.shutdown()
        server.server_close()
