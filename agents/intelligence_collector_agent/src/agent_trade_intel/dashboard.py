from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .config import CollectorConfig
from .db import SQLiteStore, loads_json
from .logging_setup import get_logger
from .time_utils import market_phase, parse_dt

logger = get_logger("dashboard")

# Heartbeat freshness thresholds used to derive the worker liveness pill.
HEARTBEAT_BUSY_SECONDS = 120
HEARTBEAT_STALE_SECONDS = 900


class DashboardService:
    """Read-only aggregation over the three SQLite stores for the live dashboard.

    Every call opens fresh connections, so each poll reflects the current database state while
    the agent / runtime keep writing from other processes (WAL mode allows concurrent readers).
    """

    def __init__(
        self,
        config: CollectorConfig,
        *,
        state_store: SQLiteStore,
        bus_store: SQLiteStore,
        data_store: SQLiteStore,
    ):
        self.config = config
        self.state_store = state_store
        self.bus_store = bus_store
        self.data_store = data_store

    def overview(self) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat(timespec="seconds")
        today = now_utc.date().isoformat()
        local_now = parse_dt(now_iso, self.config.runtime.timezone)
        overview: dict[str, Any] = {
            "generated_at": now_iso,
            "local_time": local_now.isoformat(timespec="seconds"),
            "agent_id": self.config.runtime.agent_id,
            "agent_group": self.config.runtime.agent_group,
            "model": self.config.model.primary,
            "market_phase": market_phase(local_now, self.config.raw),
            "today_utc": today,
        }
        overview.update(self._state_section(now_utc))
        overview.update(self._bus_section(today))
        overview.update(self._data_section(today))
        return overview

    # ------------------------------------------------------------------ state

    def _state_section(self, now_utc: datetime) -> dict[str, Any]:
        agent_id = self.config.runtime.agent_id
        with self.state_store.session() as con:
            session = con.execute(
                "SELECT session_id, model_ref, status, started_at, stopped_at FROM agent_sessions "
                "WHERE agent_id=? ORDER BY started_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
            checkpoint = con.execute(
                "SELECT checkpoint_id, state, market_phase, current_ticket_id, checkpoint_json, created_at "
                "FROM agent_checkpoints WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
            heartbeats = con.execute(
                "SELECT worker_id, state, message_id, ticket_id, created_at FROM runtime_heartbeats "
                "WHERE agent_id=? ORDER BY created_at DESC LIMIT 12",
                (agent_id,),
            ).fetchall()
            breakers = con.execute(
                "SELECT tool_name, status, consecutive_failures, cooldown_until, updated_at FROM circuit_breakers"
            ).fetchall()
            capability = con.execute(
                "SELECT tool_name, status, checked_at, capabilities_json, errors_json FROM tool_capabilities "
                "ORDER BY checked_at DESC LIMIT 3"
            ).fetchall()

        heartbeat_rows = [dict(r) for r in heartbeats]
        latest_hb_age = _age_seconds(heartbeat_rows[0]["created_at"], now_utc) if heartbeat_rows else None
        if latest_hb_age is None:
            worker_liveness = "no_heartbeat"
        elif latest_hb_age <= HEARTBEAT_BUSY_SECONDS:
            worker_liveness = "active"
        elif latest_hb_age <= HEARTBEAT_STALE_SECONDS:
            worker_liveness = "recent"
        else:
            worker_liveness = "stale"
        return {
            "session": dict(session) if session else None,
            "checkpoint": (
                dict(checkpoint) | {"checkpoint": loads_json(checkpoint["checkpoint_json"], {})} if checkpoint else None
            ),
            "heartbeats": heartbeat_rows,
            "heartbeat_age_seconds": latest_hb_age,
            "worker_liveness": worker_liveness,
            "circuit_breakers": [dict(r) for r in breakers],
            "capability_checks": [
                dict(r)
                | {
                    "capabilities": loads_json(r["capabilities_json"], {}),
                    "errors": loads_json(r["errors_json"], []),
                }
                for r in capability
            ],
        }

    # -------------------------------------------------------------------- bus

    def _bus_section(self, today: str) -> dict[str, Any]:
        with self.bus_store.session() as con:
            depth = con.execute("SELECT status, COUNT(*) c FROM messages GROUP BY status").fetchall()
            by_topic = con.execute(
                "SELECT topic, status, COUNT(*) c FROM messages GROUP BY topic, status ORDER BY topic"
            ).fetchall()
            recent_messages = con.execute(
                "SELECT message_id, topic, status, priority, attempts, max_attempts, lease_owner, "
                "target_agent_id, target_agent_group, error_json, updated_at, created_at "
                "FROM messages ORDER BY updated_at DESC LIMIT 15"
            ).fetchall()
            dead = con.execute(
                "SELECT message_id, topic, error_json, updated_at FROM messages WHERE status='dead' "
                "ORDER BY updated_at DESC LIMIT 10"
            ).fetchall()
            open_tickets = con.execute(
                "SELECT ticket_type, COUNT(*) c FROM tickets WHERE status IN ('open','in_progress') "
                "GROUP BY ticket_type ORDER BY c DESC"
            ).fetchall()
            recent_tickets = con.execute(
                "SELECT ticket_id, ticket_type, status, priority, summary_cn, source_agent, updated_at "
                "FROM tickets ORDER BY updated_at DESC LIMIT 15"
            ).fetchall()
            tickets_today = con.execute(
                "SELECT COUNT(*) c FROM tickets WHERE date(created_at)=?", (today,)
            ).fetchone()
        return {
            "queue_depth": {str(r["status"]): int(r["c"]) for r in depth},
            "queue_by_topic": [dict(r) for r in by_topic],
            "recent_messages": [
                dict(r) | {"error": loads_json(r["error_json"], None)} for r in recent_messages
            ],
            "dead_letters": [dict(r) | {"error": loads_json(r["error_json"], None)} for r in dead],
            "open_tickets_by_type": [dict(r) for r in open_tickets],
            "recent_tickets": [dict(r) for r in recent_tickets],
            "tickets_created_today": int(tickets_today["c"]) if tickets_today else 0,
        }

    # ------------------------------------------------------------------- data

    def _data_section(self, today: str) -> dict[str, Any]:
        with self.data_store.session() as con:
            demands = con.execute(
                "SELECT demand_id, demand_type, status, priority, active_from, active_to, test_mode, updated_at "
                "FROM collection_demands ORDER BY updated_at DESC LIMIT 20"
            ).fetchall()
            tasks_today = con.execute(
                "SELECT task_type, status, COUNT(*) c FROM collection_tasks WHERE date(created_at)=? "
                "GROUP BY task_type, status",
                (today,),
            ).fetchall()
            recent_tasks = con.execute(
                "SELECT task_id, task_type, tool_name, ticker, status, bucket_start, updated_at "
                "FROM collection_tasks ORDER BY updated_at DESC LIMIT 15"
            ).fetchall()
            runs_today = con.execute(
                "SELECT tool_name, status, COUNT(*) c FROM collection_runs WHERE date(created_at)=? "
                "GROUP BY tool_name, status",
                (today,),
            ).fetchall()
            recent_runs = con.execute(
                "SELECT run_id, tool_name, operation, status, started_at, completed_at, created_at "
                "FROM collection_runs ORDER BY created_at DESC LIMIT 15"
            ).fetchall()
            events_today = con.execute(
                "SELECT COUNT(*) c FROM structured_events WHERE date(created_at)=?", (today,)
            ).fetchone()
            recent_events = con.execute(
                "SELECT event_id, ticker, event_type, event_date, summary_cn, confidence, data_quality, created_at "
                "FROM structured_events ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            features_today = con.execute(
                "SELECT COUNT(*) c FROM market_features WHERE date(created_at)=?", (today,)
            ).fetchone()
            recent_features = con.execute(
                "SELECT feature_id, ticker, feature_window, bucket_start, abnormality_score, data_quality, summary_cn, created_at "
                "FROM market_features ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            open_issues = con.execute(
                "SELECT issue_id, severity, issue_type, ticker, tool_name, summary_cn, created_at "
                "FROM data_quality_issues WHERE status='open' ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            open_gaps = con.execute(
                "SELECT gap_id, ticker, priority, description, created_at FROM coverage_gaps "
                "WHERE status='open' ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            latest_report = con.execute(
                "SELECT report_id, trade_date, html_path, json_path, created_at FROM daily_collection_reports "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return {
            "demands": [dict(r) for r in demands],
            "tasks_today_by_type_status": [dict(r) for r in tasks_today],
            "recent_tasks": [dict(r) for r in recent_tasks],
            "runs_today_by_tool_status": [dict(r) for r in runs_today],
            "recent_runs": [dict(r) for r in recent_runs],
            "events_created_today": int(events_today["c"]) if events_today else 0,
            "recent_events": [dict(r) for r in recent_events],
            "features_created_today": int(features_today["c"]) if features_today else 0,
            "recent_features": [dict(r) for r in recent_features],
            "open_data_quality_issues": [dict(r) for r in open_issues],
            "open_coverage_gaps": [dict(r) for r in open_gaps],
            "latest_report": dict(latest_report) if latest_report else None,
        }


def _age_seconds(sqlite_utc_ts: str | None, now_utc: datetime) -> int | None:
    """SQLite datetime('now') timestamps are naive UTC strings."""
    if not sqlite_utc_ts:
        return None
    try:
        ts = datetime.strptime(str(sqlite_utc_ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            ts = datetime.fromisoformat(str(sqlite_utc_ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return max(0, int((now_utc - ts).total_seconds()))


def run_dashboard(
    config: CollectorConfig,
    *,
    state_store: SQLiteStore,
    bus_store: SQLiteStore,
    data_store: SQLiteStore,
    host: str = "127.0.0.1",
    port: int = 8700,
    refresh_seconds: int = 5,
) -> None:
    """Serve the live dashboard until interrupted (Ctrl+C)."""
    service = DashboardService(config, state_store=state_store, bus_store=bus_store, data_store=data_store)
    page = DASHBOARD_HTML.replace("__REFRESH_SECONDS__", str(max(1, refresh_seconds)))

    class Handler(BaseHTTPRequestHandler):
        server_version = "IntelDashboard/1.0"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urlparse(self.path).path
            try:
                if path in {"/", "/index.html"}:
                    body = page.encode("utf-8")
                    self._send(200, "text/html; charset=utf-8", body)
                elif path == "/api/overview":
                    body = json.dumps(service.overview(), ensure_ascii=False, default=str).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                elif path == "/healthz":
                    self._send(200, "application/json; charset=utf-8", b'{"status":"ok"}')
                else:
                    self._send(404, "application/json; charset=utf-8", b'{"error":"not_found"}')
            except Exception as exc:  # noqa: BLE001 - dashboard must not crash on a bad poll
                logger.exception("dashboard request failed: %s", path)
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(500, "application/json; charset=utf-8", body)

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("dashboard http: " + fmt, *args)

    server = ThreadingHTTPServer((host, port), Handler)
    logger.info("dashboard listening on http://%s:%s (refresh every %ss)", host, port, refresh_seconds)
    print(f"情报收集员看板已启动: http://{host}:{port}  (每 {refresh_seconds}s 自动刷新, Ctrl+C 退出)")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info("dashboard stopped")


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>情报收集员 Agent · 实时看板</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#ffffff; --line:#e6e8ec; --ink:#1f2430; --muted:#6b7280;
    --accent:#4f46e5; --green:#16a34a; --amber:#d97706; --red:#dc2626; --blue:#2563eb;
    --shadow:0 1px 2px rgba(16,24,40,.04),0 4px 16px rgba(16,24,40,.06);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;}
  .wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
  header.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
  .title h1{font-size:20px;margin:0 0 6px}
  .title .sub{color:var(--muted);font-size:13px}
  .ctrl{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px}
  .ctrl select{padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:#fff}
  .ctrl button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:7px 14px;font-weight:600;cursor:pointer}
  .ctrl button.paused{background:var(--amber)}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600}
  .pill .dot{width:7px;height:7px;border-radius:50%}
  .pill.run{background:#ecfdf3;color:var(--green)} .pill.run .dot{background:var(--green)}
  .pill.idle{background:#eff6ff;color:var(--blue)} .pill.idle .dot{background:var(--blue)}
  .pill.warn{background:#fffbeb;color:var(--amber)} .pill.warn .dot{background:var(--amber)}
  .pill.bad{background:#fef2f2;color:var(--red)} .pill.bad .dot{background:var(--red)}
  #errbar{display:none;background:#fef2f2;border:1px solid #fecaca;color:#991b1b;border-radius:10px;
    padding:10px 14px;margin-bottom:14px;font-size:13px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px 12px;box-shadow:var(--shadow)}
  .card .k{font-size:12px;color:var(--muted);font-weight:600;letter-spacing:.4px}
  .card .v{font-size:24px;font-weight:700;margin-top:6px;line-height:1.1}
  .card .meta{font-size:12px;color:var(--muted);margin-top:6px}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .chip{font-size:11px;background:#f3f4f6;border:1px solid var(--line);border-radius:8px;padding:3px 8px;color:#374151}
  .chip b{color:var(--ink)}
  section.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);margin-bottom:14px;overflow:hidden}
  section.panel > .head{display:flex;align-items:center;justify-content:space-between;padding:13px 16px;cursor:pointer;user-select:none}
  section.panel > .head h2{font-size:14px;margin:0}
  section.panel > .head .hint{font-size:12px;color:var(--muted)}
  section.panel > .body{padding:0 16px 16px;display:none}
  section.panel.open > .body{display:block}
  .caret{transition:transform .15s;color:var(--muted)}
  section.panel.open .caret{transform:rotate(90deg)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:12px;white-space:nowrap}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  .ok{color:var(--green);font-weight:600} .no{color:var(--red);font-weight:600} .wn{color:var(--amber);font-weight:600}
  .mut{color:var(--muted)}
  .mono{font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
  .twocol{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:820px){.twocol{grid-template-columns:1fr}}
  .small{font-size:12px;color:var(--muted)}
  .empty{color:var(--muted);font-size:12.5px;padding:8px 2px}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="title">
      <h1>情报收集员 Agent · 实时看板</h1>
      <div class="sub" id="subline">连接中…</div>
    </div>
    <div class="ctrl">
      <span id="phasePill"></span>
      <span id="sessionPill"></span>
      <span id="livePill"></span>
      <label>刷新
        <select id="intervalSel">
          <option value="2">2s</option>
          <option value="5">5s</option>
          <option value="10">10s</option>
          <option value="30">30s</option>
        </select>
      </label>
      <button id="pauseBtn" onclick="togglePause()">暂停</button>
      <span class="small" id="lastFetch"></span>
    </div>
  </header>

  <div id="errbar"></div>
  <div class="grid" id="kpis"></div>

  <section class="panel open"><div class="head"><h2>工具调用流水（collection_runs）</h2><span class="hint" id="runsHint"></span><span class="caret">▶</span></div><div class="body" id="runsBody"></div></section>
  <section class="panel open"><div class="head"><h2>消息队列</h2><span class="hint" id="queueHint"></span><span class="caret">▶</span></div><div class="body" id="queueBody"></div></section>
  <section class="panel"><div class="head"><h2>Ticket</h2><span class="hint" id="ticketHint"></span><span class="caret">▶</span></div><div class="body" id="ticketBody"></div></section>
  <section class="panel"><div class="head"><h2>今日采集任务</h2><span class="hint" id="taskHint"></span><span class="caret">▶</span></div><div class="body" id="taskBody"></div></section>
  <section class="panel"><div class="head"><h2>产出：事件 / 行情特征</h2><span class="hint" id="outputHint"></span><span class="caret">▶</span></div><div class="body" id="outputBody"></div></section>
  <section class="panel"><div class="head"><h2>质量问题 / 覆盖缺口 / 死信</h2><span class="hint" id="issueHint"></span><span class="caret">▶</span></div><div class="body" id="issueBody"></div></section>
  <section class="panel"><div class="head"><h2>Demand 与能力 / 熔断 / 心跳</h2><span class="hint" id="sysHint"></span><span class="caret">▶</span></div><div class="body" id="sysBody"></div></section>
</div>

<script>
let REFRESH = __REFRESH_SECONDS__;
let paused = false;
let timer = null;
let failures = 0;

const $ = (id)=>document.getElementById(id);
const esc = (s)=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const short = (s,n=10)=>{s=String(s||"");return s.length>n? s.slice(0,n)+"…":s;};
const utcLocal = (ts)=>{ if(!ts) return "-"; const d=new Date(String(ts).replace(" ","T")+(String(ts).includes("+")?"":"Z"));
  return isNaN(d)? esc(ts): d.toLocaleTimeString("zh-CN",{hour12:false})+" "+d.toLocaleDateString("zh-CN",{month:"2-digit",day:"2-digit"});};
const stClass=(s)=>({success:"ok",done:"ok",closed:"ok",available:"ok",running:"ok",active:"ok",
  open:"wn",in_progress:"wn",recent:"wn",degraded:"wn",half_open:"wn",
  failed:"no",dead:"no",stale:"no",unavailable:"no",cancelled:"mut",expired:"mut",rotated:"mut",stopped:"mut"}[s]||"");
const st=(s)=>`<span class="${stClass(s)}">${esc(s)}</span>`;

function table(headers, rows){
  if(!rows.length) return '<div class="empty">暂无数据</div>';
  return `<table><thead><tr>${headers.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

function render(d){
  const q = d.queue_depth||{};
  const hbAge = d.heartbeat_age_seconds;
  const ses = d.session||{};

  $("subline").innerHTML = `Agent <b>${esc(d.agent_id)}</b> · 模型 <b>${esc(d.model)}</b> · 服务器时间 ${utcLocal(d.generated_at)}（本地时区 ${esc(d.local_time)}）`;
  $("phasePill").innerHTML = `<span class="pill ${d.market_phase==='intraday'?'run':(d.market_phase==='non_trading_day'?'idle':'warn')}"><span class="dot"></span>${esc(d.market_phase)}</span>`;
  $("sessionPill").innerHTML = `<span class="pill ${ses.status==='running'?'run':'idle'}"><span class="dot"></span>会话 ${esc(ses.status||'无')}</span>`;
  const liveMap = {active:["run","工作中"],recent:["warn","近期活跃"],stale:["bad","心跳超时"],no_heartbeat:["idle","无心跳"]};
  const lv = liveMap[d.worker_liveness]||["idle",d.worker_liveness];
  $("livePill").innerHTML = `<span class="pill ${lv[0]}"><span class="dot"></span>${lv[1]}${hbAge!=null?` · ${hbAge}s 前`:''}</span>`;

  // KPI cards
  const openTk = (d.open_tickets_by_type||[]).reduce((a,b)=>a+b.c,0);
  const tasks = d.tasks_today_by_type_status||[];
  const tSum = (s)=>tasks.filter(t=>t.status===s).reduce((a,b)=>a+b.c,0);
  const runsToday = (d.runs_today_by_tool_status||[]).reduce((a,b)=>a+b.c,0);
  const runsFail = (d.runs_today_by_tool_status||[]).filter(r=>r.status!=='success').reduce((a,b)=>a+b.c,0);
  const deadN = q.dead||0;
  const cbOpen = (d.circuit_breakers||[]).filter(b=>b.status!=='closed').length;
  const issueN = (d.open_data_quality_issues||[]).length, gapN=(d.open_coverage_gaps||[]).length;
  const kpi = (k,v,meta,cls)=>`<div class="card"><div class="k">${k}</div><div class="v ${cls||''}">${v}</div>${meta?`<div class="meta">${meta}</div>`:''}</div>`;
  $("kpis").innerHTML =
    kpi("队列 待处理 / 处理中", `${q.open||0} / ${q.in_progress||0}`, `已完成 ${q.done||0} · 过期 ${q.expired||0}`)+
    kpi("死信消息", deadN, deadN?'需要处理':'无', deadN?'no':'ok')+
    kpi("未关闭 Ticket", openTk, `今日新建 ${d.tickets_created_today||0}`)+
    kpi("今日任务 完成/失败/进行", `${tSum('done')} / ${tSum('failed')} / ${tSum('open')+tSum('in_progress')}`)+
    kpi("今日工具调用", runsToday, runsFail?`其中失败 ${runsFail}`:'全部成功', runsFail?'wn':'')+
    kpi("今日事件 / 特征", `${d.events_created_today||0} / ${d.features_created_today||0}`, "structured_events / market_features")+
    kpi("质量问题 / 覆盖缺口", `${issueN} / ${gapN}`, "均为 open 状态", (issueN+gapN)?'wn':'')+
    kpi("熔断器", cbOpen?`${cbOpen} 个异常`:"正常", (d.circuit_breakers||[]).map(b=>`${b.tool_name}:${b.status}`).join(" ")||"无记录", cbOpen?'no':'ok');

  // runs
  $("runsHint").textContent = `今日 ${runsToday} 次`;
  $("runsBody").innerHTML =
    `<div class="chips" style="margin:12px 0 4px">${(d.runs_today_by_tool_status||[]).map(r=>`<span class="chip">${esc(r.tool_name)} ${st(r.status)} <b>${r.c}</b></span>`).join("")||'<span class="small">今日暂无调用</span>'}</div>`+
    table(["时间","工具","操作","状态"],(d.recent_runs||[]).map(r=>
      `<tr><td class="mut">${utcLocal(r.created_at)}</td><td>${esc(r.tool_name)}</td><td>${esc(r.operation)}</td><td>${st(r.status)}</td></tr>`));

  // queue
  $("queueHint").textContent = `open ${q.open||0} · in_progress ${q.in_progress||0} · dead ${deadN}`;
  $("queueBody").innerHTML = `<div class="twocol" style="margin-top:12px">
    <div><h3 class="small">按 topic 统计</h3>${table(["topic","状态","数量"],(d.queue_by_topic||[]).map(r=>
      `<tr><td class="mono">${esc(r.topic)}</td><td>${st(r.status)}</td><td class="num">${r.c}</td></tr>`))}</div>
    <div><h3 class="small">最近消息</h3>${table(["更新","topic","状态","尝试"],(d.recent_messages||[]).map(m=>
      `<tr><td class="mut">${utcLocal(m.updated_at)}</td><td class="mono">${esc(m.topic)}</td><td>${st(m.status)}</td><td class="num">${m.attempts}/${m.max_attempts}</td></tr>`))}</div>
  </div>`;

  // tickets
  $("ticketHint").textContent = `未关闭 ${openTk}`;
  $("ticketBody").innerHTML = `<div class="chips" style="margin:12px 0 4px">${(d.open_tickets_by_type||[]).map(t=>`<span class="chip">${esc(t.ticket_type)} <b>${t.c}</b></span>`).join("")||'<span class="small">无未关闭票据</span>'}</div>`+
    table(["更新","类型","状态","摘要"],(d.recent_tickets||[]).map(t=>
      `<tr><td class="mut">${utcLocal(t.updated_at)}</td><td class="mono">${esc(t.ticket_type)}</td><td>${st(t.status)}</td><td>${esc(short(t.summary_cn,60))}</td></tr>`));

  // tasks
  $("taskHint").textContent = `今日 ${tasks.reduce((a,b)=>a+b.c,0)} 个`;
  $("taskBody").innerHTML = `<div class="chips" style="margin:12px 0 4px">${tasks.map(t=>`<span class="chip">${esc(t.task_type)} ${st(t.status)} <b>${t.c}</b></span>`).join("")||'<span class="small">今日暂无任务</span>'}</div>`+
    table(["更新","类型","标的","工具","时间桶","状态"],(d.recent_tasks||[]).map(t=>
      `<tr><td class="mut">${utcLocal(t.updated_at)}</td><td class="mono">${esc(t.task_type)}</td><td>${esc(t.ticker||'-')}</td><td class="mut">${esc(short(t.tool_name,18))}</td><td class="mut">${esc(short(t.bucket_start||'-',16))}</td><td>${st(t.status)}</td></tr>`));

  // outputs
  $("outputHint").textContent = `今日事件 ${d.events_created_today||0} · 特征 ${d.features_created_today||0}`;
  $("outputBody").innerHTML = `<div class="twocol" style="margin-top:12px">
    <div><h3 class="small">最新结构化事件</h3>${table(["时间","标的","类型","置信度","摘要"],(d.recent_events||[]).map(e=>
      `<tr><td class="mut">${utcLocal(e.created_at)}</td><td>${esc(e.ticker||'-')}</td><td class="mono">${esc(e.event_type)}</td><td class="num">${e.confidence!=null?Number(e.confidence).toFixed(2):'-'}</td><td>${esc(short(e.summary_cn,40))}</td></tr>`))}</div>
    <div><h3 class="small">最新行情特征</h3>${table(["时间桶","标的","窗口","异常分","摘要"],(d.recent_features||[]).map(f=>
      `<tr><td class="mut">${esc(short(f.bucket_start,16))}</td><td>${esc(f.ticker)}</td><td>${esc(f.feature_window)}</td><td class="num ${f.abnormality_score>=0.75?'no':''}">${f.abnormality_score!=null?Number(f.abnormality_score).toFixed(2):'-'}</td><td>${esc(short(f.summary_cn,36))}</td></tr>`))}</div>
  </div>`;

  // issues
  $("issueHint").textContent = `问题 ${issueN} · 缺口 ${gapN} · 死信 ${deadN}`;
  $("issueBody").innerHTML = `<div class="twocol" style="margin-top:12px">
    <div><h3 class="small">数据质量问题（open）</h3>${table(["时间","严重度","标的","摘要"],(d.open_data_quality_issues||[]).map(i=>
      `<tr><td class="mut">${utcLocal(i.created_at)}</td><td>${st(i.severity)}</td><td>${esc(i.ticker||'-')}</td><td>${esc(short(i.summary_cn,44))}</td></tr>`))}
      <h3 class="small" style="margin-top:14px">覆盖缺口（open）</h3>${table(["时间","标的","优先级","描述"],(d.open_coverage_gaps||[]).map(g=>
      `<tr><td class="mut">${utcLocal(g.created_at)}</td><td>${esc(g.ticker||'-')}</td><td>${esc(g.priority)}</td><td>${esc(short(g.description,44))}</td></tr>`))}</div>
    <div><h3 class="small">死信消息</h3>${table(["时间","topic","错误"],(d.dead_letters||[]).map(m=>
      `<tr><td class="mut">${utcLocal(m.updated_at)}</td><td class="mono">${esc(m.topic)}</td><td class="mono">${esc(short((m.error&&m.error.error_code)||JSON.stringify(m.error)||'-',36))}</td></tr>`))}</div>
  </div>`;

  // system
  const caps = d.capability_checks||[];
  let capRows = [];
  caps.forEach(c=>{
    const fr = (c.capabilities&&c.capabilities.frequencies)||{};
    Object.keys(fr).forEach(k=>capRows.push(
      `<tr><td class="mut">${utcLocal(c.checked_at)}</td><td>${esc(c.tool_name)}</td><td><b>${esc(k)}</b></td><td>${fr[k].usable?'<span class="ok">usable</span>':'<span class="no">not usable</span>'}</td><td class="num">${fr[k].data_quality!=null?Number(fr[k].data_quality).toFixed(2):'-'}</td></tr>`));
    if(c.capabilities&&c.capabilities.recommended_intraday_mode) capRows.push(
      `<tr><td></td><td colspan="4" class="small">推荐盘中模式：<b>${esc(c.capabilities.recommended_intraday_mode)}</b></td></tr>`);
  });
  $("sysHint").textContent = `Demand ${(d.demands||[]).length} · 心跳 ${(d.heartbeats||[]).length}`;
  $("sysBody").innerHTML = `<div class="twocol" style="margin-top:12px">
    <div><h3 class="small">Demand</h3>${table(["demand","类型","状态","优先级","有效期"],(d.demands||[]).map(x=>
      `<tr><td class="mono">${esc(short(x.demand_id,20))}</td><td class="mono">${esc(x.demand_type)}</td><td>${st(x.status)}</td><td>${esc(x.priority)}</td><td class="mut">${esc(x.active_from||'-')} ~ ${esc(x.active_to||'-')}</td></tr>`))}
      <h3 class="small" style="margin-top:14px">工具能力验证</h3>${capRows.length?`<table><thead><tr><th>时间</th><th>工具</th><th>频率</th><th>可用</th><th>质量</th></tr></thead><tbody>${capRows.join("")}</tbody></table>`:'<div class="empty">暂无能力验证记录</div>'}</div>
    <div><h3 class="small">最近心跳</h3>${table(["时间","状态","worker","ticket"],(d.heartbeats||[]).map(h=>
      `<tr><td class="mut">${utcLocal(h.created_at)}</td><td>${h.state==='processing'?'<span class="ok">processing</span>':esc(h.state)}</td><td class="mono">${esc(short((h.worker_id||'').split(':').pop(),14))}</td><td class="mono">${esc(short(h.ticket_id||'-',18))}</td></tr>`))}
      <h3 class="small" style="margin-top:14px">会话 / Checkpoint</h3>${table(["项","值"],[
        `<tr><td>session</td><td class="mono">${esc(short((ses.session_id||'-'),24))} ${st(ses.status||'-')} <span class="mut">起于 ${utcLocal(ses.started_at)}</span></td></tr>`,
        `<tr><td>checkpoint</td><td class="mono">${d.checkpoint?`${esc(d.checkpoint.state)} <span class="mut">${utcLocal(d.checkpoint.created_at)} · ${esc((d.checkpoint.checkpoint&&d.checkpoint.checkpoint.reason)||'')}</span>`:'-'}</td></tr>`,
        `<tr><td>最新日报</td><td class="mono">${d.latest_report?`${esc(d.latest_report.trade_date)} <span class="mut">${utcLocal(d.latest_report.created_at)}</span>`:'尚未生成'}</td></tr>`,
      ])}</div>
  </div>`;
}

async function poll(){
  try{
    const res = await fetch("/api/overview",{cache:"no-store"});
    if(!res.ok) throw new Error("HTTP "+res.status);
    const data = await res.json();
    render(data);
    failures = 0;
    $("errbar").style.display = "none";
    $("lastFetch").textContent = "最后刷新 " + new Date().toLocaleTimeString("zh-CN",{hour12:false});
  }catch(e){
    failures++;
    $("errbar").style.display = "block";
    $("errbar").textContent = `拉取数据失败（连续 ${failures} 次）：${e.message}。看板服务或数据库可能不可用，将继续重试。`;
  }
}

function schedule(){
  if(timer) clearInterval(timer);
  if(!paused) timer = setInterval(poll, REFRESH*1000);
}
function togglePause(){
  paused = !paused;
  const b = $("pauseBtn");
  b.textContent = paused? "继续":"暂停";
  b.classList.toggle("paused", paused);
  schedule();
  if(!paused) poll();
}
document.querySelectorAll("section.panel > .head").forEach(h=>{
  h.addEventListener("click",()=>h.parentElement.classList.toggle("open"));
});
const sel = $("intervalSel");
[...sel.options].forEach(o=>{ if(Number(o.value)===REFRESH) o.selected=true; });
sel.addEventListener("change",()=>{ REFRESH = Number(sel.value); schedule(); });

poll();
schedule();
</script>
</body>
</html>
"""
