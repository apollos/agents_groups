#!/usr/bin/env python3
"""Repeatable acceptance entry for intelligence_collector_agent + MIC.

Subcommands (all databases live under one isolated workspace; nothing in the
deployed configuration, the persistent pilot workspace or the repository is
modified by this tool):

  prepare     create a frozen, isolated acceptance workspace (no collection)
  run         execute exactly ONE registered real collection through the normal
              Agent path (demand -> runtime tick -> plan -> MIC subprocess ->
              result publish), then audit databases read-only
  redeliver   re-open the already-acked collection message of a finished run and
              let the Agent consume it again: must reuse, never re-spend
  next-cycle  on a throw-away copy of the databases, tick the runtime one day
              later: a new task must be planned and must not reuse the old run
  export      read-only content review export for a finished run
  summary     aggregate all runs of the workspace into one judgement block

Budget per real run is fixed (section 3.2 of the handoff) and the batch budget
is 2 runs / 6 gateway requests per frozen code version. Credentials are read
from the existing Agent/MIC environment only and are never written anywhere.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CAPS = {"max_queries": 2, "max_search_hits": 20, "max_links_to_read": 6,
        "max_http_read_attempts": 6, "max_browser_read_attempts": 2,
        "max_model_calls": 3, "max_gateway_requests": 3, "max_run_seconds": 300}
BATCH_MAX_RUNS = 2
BATCH_MAX_GATEWAY_REQUESTS = 6
PRIMARY = "volcengine-agent-plan/deepseek-v4.1-flash"
FALLBACK = "deepseek/deepseek-v4-flash"
GATEWAY = "http://127.0.0.1:18791/v1"
REQUEST_MODEL = "openclaw/main"
TARGET_ID, TICKER, COMPANY = "company_300750", "300750.SZ", "宁德时代"
TZ = ZoneInfo("Asia/Shanghai")
SCHEMA = "collector-acceptance/1"
TABLES = {"briefs": "analysis_brief", "facts": "fact_item", "metrics": "metric_observation",
          "events": "event_card", "relations": "relation_record", "risks": "risk_flag",
          "catalysts": "catalyst_item", "customer_supplier_signals": "customer_supplier_signal",
          "price_cost_margin_signals": "price_cost_margin_signal",
          "policy_signals": "policy_regulatory_signal", "analyst_questions": "analyst_question"}
FORMAL = ("facts", "metrics", "events", "relations", "risks", "catalysts",
          "customer_supplier_signals", "price_cost_margin_signals", "policy_signals")
SECRETS: list[str] = []


class AcceptanceError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def redact(text):
    for secret in SECRETS:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer <redacted>", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "<redacted>", text)


def encoded(value):
    return redact(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def emit(value):
    print(encoded(value), flush=True)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(encoded(value) + "\n", encoding="utf-8")
    tmp.replace(path)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def unpack(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect_ro(path):
    require(path.is_file(), f"缺少数据库：{path}")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def rows(db, sql, args=(), json_fields=()):
    out = [dict(r) for r in db.execute(sql, args)]
    for value in out:
        for key in json_fields:
            if isinstance(value.get(key), str):
                try:
                    value[key] = json.loads(value[key])
                except ValueError:
                    pass
    return out


# --- code / config freeze ----------------------------------------------------

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def code_fingerprint(repo):
    status = git(repo, "status", "--porcelain")
    diff = subprocess.run(["git", "-C", str(repo), "diff", "HEAD", "--", "agents", "tools"],
                          check=True, capture_output=True, text=True).stdout
    untracked = [line[3:] for line in status.splitlines() if line.startswith("??")]
    # git reports an untracked directory as a single "??" entry; walk it so the
    # python files inside (including this tool) are part of the frozen version.
    files = []
    for rel in untracked:
        path = repo / rel
        if path.is_dir():
            files.extend(p.relative_to(repo).as_posix() for p in path.rglob("*.py"))
        elif path.is_file() and path.suffix == ".py":
            files.append(rel)
    untracked_hash = hashlib.sha256()
    for rel in sorted(set(files)):
        untracked_hash.update(rel.encode())
        untracked_hash.update((repo / rel).read_bytes())
    return {"branch": git(repo, "branch", "--show-current"), "head": git(repo, "rev-parse", "HEAD"),
            "dirty": bool(status), "tracked_diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
            "untracked_python_sha256": untracked_hash.hexdigest(),
            "status_porcelain": status.splitlines()}


def check_mic(mc):
    from mic.browser.config import resolve_profile_dir
    from mic.merge import MergeResult
    from mic.modeling.adapter import ModelRegistry
    adapter = ModelRegistry(mc).get("openclaw_research")
    require(not mc.allow_mock and adapter and adapter.usable and not adapter.allow_mock,
            "需要已配置的真实网关凭据及 MIC_ALLOW_MOCK=false。")
    require(adapter.endpoint.rstrip("/") == GATEWAY and adapter.model == REQUEST_MODEL,
            "网关地址或请求模型与已验证配置不一致。")
    require(adapter.max_output_tokens == 65536
            and mc.model_registry.get("default_max_output_tokens") == 65536, "有效输出 token 上限必须为 65536。")
    tasks = mc.model_policies.get("tasks", {})
    require(tasks and all(p.get("models") and all(m.get("model_id") == "openclaw_research" for m in p["models"])
                          for p in tasks.values()), "文本任务必须全部走 openclaw_research。")
    require(mc.browser_enabled and mc.search_providers.get("active") == "browser_local",
            "需要 browser_local 浏览器搜索配置。")
    require(mc.output_schema.get("limits", {}).get("strict_evidence_review") is True
            and not mc.call_governance.get("vision_extract", {}).get("enabled", True),
            "需要严格证据校验开启、视觉调用关闭。")
    require(mc.merge_policy.get("rules", {}).get("save_structured", {}).get("min_overall_score") == 70,
            "合并入库门槛必须保持为 70。")
    require("decision_diagnostics" in MergeResult.__dataclass_fields__, "缺少入库门槛诊断。")
    require(mc.get_target_profile(TARGET_ID), f"MIC 缺少 {COMPANY} 目标配置。")
    profile = resolve_profile_dir(mc.browser_runtime)
    require(profile.is_dir(), "浏览器专用 profile 目录不存在。")
    return profile.resolve()


def repo_paths(repo):
    import agent_trade_intel
    import mic
    agent_root = repo / "agents/intelligence_collector_agent"
    require(Path(agent_trade_intel.__file__).resolve().parent == (agent_root / "src/agent_trade_intel").resolve(),
            "当前 Python 导入的 Agent 不是指定仓库中的版本。")
    require(Path(mic.__file__).resolve().parent == (repo / "tools/market_intelligence_collector/mic").resolve(),
            "当前 Python 导入的 MIC 不是指定仓库中的版本。")
    return agent_root


def demand_document(demand_id, label):
    return {"schema_version": "demand.v1", "demand_id": demand_id, "demand_type": "on_demand_research",
            "source_type": f"acceptance_{label}", "status": "suspended", "priority": "normal",
            "test_mode": True, "timezone": "Asia/Shanghai",
            "target_scope": {"scope_type": "explicit_targets"},
            "targets": [{"target_type": "company", "target_id": TARGET_ID, "ticker": TICKER,
                         "company_name": COMPANY, "collect_mic": True, "collect_stock": False}],
            "schedule_window": {"allow_non_trading_day": True},
            "task_profile": {"mic": {"enabled": True, "focus": ["operating_update"], "time_window": "30d",
                                     "budget_profile": dict(CAPS)}, "stock_data": {"enabled": False}},
            "alert_policy": {"notify_owner": False, "notify_channels": []}, "idempotency_key": demand_id}


# --- prepare -----------------------------------------------------------------

def prepare(repo, root, label):
    import yaml
    from agent_trade_intel.agent import _mic_task_profile
    from agent_trade_intel.config import load_config as load_agent
    from agent_trade_intel.planner import TaskGraphPlanner
    from agent_trade_intel.stores import create_stores, init_unique_stores
    from mic.config import CONFIG_FILES, load_config as load_mic
    from mic.store.database import Database

    repo, root = repo.expanduser().resolve(), root.expanduser().resolve()
    agent_root = repo_paths(repo)
    require(not root.exists(), f"目标目录已存在：{root}；不覆盖，请换一个新目录。")
    original_path = agent_root / "config/intelligence_collector.yaml"
    original = load_agent(original_path)
    mc = load_mic(original.tools.mic_config_dir)
    profile = check_mic(mc)
    require(original.model.primary == PRIMARY and original.model.fallbacks == [FALLBACK],
            "Agent 主模型或 fallback 与已验证配置不一致。")
    root.mkdir(parents=True, mode=0o700)
    dest = root / "mic-config"
    dest.mkdir(mode=0o700)
    for name in CONFIG_FILES:
        source = mc.config_dir / (name + ".yaml")
        if source.is_file():
            shutil.copyfile(source, dest / source.name)
    runtime = copy.deepcopy(mc.browser_runtime)
    runtime.setdefault("limits", {}).update(CAPS)
    runtime.setdefault("cache", {})["reuse_analysis"] = False
    (dest / "browser_runtime.yaml").write_text(yaml.safe_dump({"browser_runtime": runtime}, allow_unicode=True), encoding="utf-8")
    gp = dest / "call_governance.yaml"
    governance = yaml.safe_load(gp.read_text(encoding="utf-8"))
    governance.setdefault("call_governance", {}).setdefault("budgets", {})["max_batch_triage_calls"] = 1
    gp.write_text(yaml.safe_dump(governance, allow_unicode=True), encoding="utf-8")
    raw = copy.deepcopy(original.raw)
    raw.setdefault("agent", {}).update(agent_id="intelligence_collector_acceptance", agent_group="intelligence_collector_acceptance")
    raw.setdefault("runtime", {}).update(workspace_root=str(root), state_sqlite_path=str(root / "state.db"),
                                         bus_sqlite_path=str(root / "bus.db"), data_sqlite_path=str(root / "data.db"),
                                         log_dir=str(root / "logs"))
    raw.setdefault("reports", {})["output_dir"] = str(root / "reports")
    raw.setdefault("queue", {}).update(consume_topics=["intelligence.collection"], lease_seconds=300)
    raw.setdefault("capability_verification", {}).update(run_on_startup=False, run_pre_market=False)
    raw.setdefault("mic_task_defaults", {})["test_mode_budget_profile"] = dict(CAPS)
    raw["mic_task_defaults"]["deep_collect"] = {"focus": ["operating_update"], "time_window": "30d", "budget_profile": dict(CAPS)}
    tool = raw.setdefault("tools", {})
    tool["python_executable"] = sys.executable
    tool.setdefault("market_intelligence_collector", {}).update(
        enabled=True, config_dir=str(dest), execution_mode="subprocess", runs_dir=str(root / "mic_runs"),
        python_executable=sys.executable, timeout_seconds=300)
    for name in ("stock_data_collector", "hk_connect_collector", "market_context_collector"):
        tool.setdefault(name, {})["enabled"] = False
    raw.setdefault("openclaw", {})["model"] = {"primary": PRIMARY, "fallbacks": [FALLBACK],
                                              "require_registered": True, "allow_openclaw_default": False}
    config_path = root / "agent-config.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    pins = {"MIC_CONFIG_DIR": str(dest), "MIC_DATABASE_URL": "sqlite:///" + str(root / "mic.db"),
            "MIC_LOG_DIR": str(root / "mic-logs"), "MIC_ALLOW_MOCK": "false",
            "INTEL_AGENT_PYTHON": sys.executable, "INTEL_AGENT_MIC_PYTHON": sys.executable,
            "OPENCLAW_AGENT_PRIMARY_MODEL": PRIMARY, "OPENCLAW_AGENT_FALLBACK_MODELS": FALLBACK,
            "OPENCLAW_GATEWAY_BASE_URL": GATEWAY, "OPENCLAW_REQUEST_MODEL": REQUEST_MODEL,
            runtime.get("profile_dir_env", "MIC_BROWSER_PROFILE_DIR"): str(profile)}
    os.environ.update(pins)
    cfg = load_agent(config_path)
    check_mic(load_mic(dest))
    for p in (cfg.runtime.state_sqlite_path, cfg.runtime.bus_sqlite_path, cfg.runtime.data_sqlite_path,
              cfg.runtime.log_dir, cfg.runtime.reports_dir, Path(cfg.tools.mic_runs_dir)):
        require(Path(p).is_absolute() and Path(p).is_relative_to(root), "运行目录越出工作区。")
    demand_id = f"collector_acceptance_catl_{label}"
    demand = demand_document(demand_id, label)
    now = datetime.now(TZ).isoformat()
    planned = TaskGraphPlanner(cfg.raw).plan(demand, request_ticket_id="offline-preview", as_of=now, market_phase="off_hours")
    require(len(planned) == 1 and planned[0]["task_type"] == "mic_deep_collect"
            and planned[0]["target"]["target_id"] == TARGET_ID, "离线规划未得到唯一预期任务。")
    require(_mic_task_profile(planned[0], cfg.raw) == {"focus": ["operating_update"], "time_window": "30d", "budget_profile": CAPS},
            "实际任务预算与准备配置不一致。")
    stores = create_stores(cfg)
    init_unique_stores(stores)
    db = Database(pins["MIC_DATABASE_URL"])
    try:
        db.create_all()
    finally:
        db.engine.dispose()
    save(root / "demand.json", demand)
    config_files = [config_path, root / "demand.json", *sorted(dest.glob("*.yaml"))]
    manifest = {"schema": SCHEMA, "label": label, "created_at": now, "repo": str(repo), "python": sys.executable,
                "code": code_fingerprint(repo), "original_agent_config": str(original_path),
                "original_mic_config": str(mc.config_dir), "environment_pins": pins,
                "config_sha256": {str(p.relative_to(root)): digest_file(p) for p in config_files},
                "budget_per_run": CAPS, "batch_budget": {"max_runs": BATCH_MAX_RUNS,
                                                         "max_gateway_requests": BATCH_MAX_GATEWAY_REQUESTS},
                "max_output_tokens": 65536, "merge_min_overall_score": 70, "demand_id": demand_id,
                "target": {"target_id": TARGET_ID, "ticker": TICKER, "company_name": COMPANY},
                "credential_storage": "existing agent/MIC environment only; nothing stored here"}
    save(root / "acceptance-manifest.json", manifest)
    return {"status": "ACCEPTANCE_WORKSPACE_READY", "workspace": str(root), "manifest": manifest,
            "effect": {"model_calls": 0, "browser_launches": 0, "collection_started": False,
                       "deployment_changed": False}}


# --- shared load -------------------------------------------------------------

def load_workspace(root, *, verify_code=True):
    from agent_trade_intel.config import load_config as load_agent
    from mic.config import load_config as load_mic
    from mic.modeling.adapter import ModelRegistry
    root = root.expanduser().resolve()
    manifest_path = root / "acceptance-manifest.json"
    require(manifest_path.is_file(), "工作区缺少 acceptance-manifest.json；请先执行 prepare。")
    manifest = load(manifest_path)
    require(manifest.get("schema") == SCHEMA, "工作区记录版本不匹配。")
    repo = Path(manifest["repo"]).resolve()
    require(sys.executable == manifest["python"], "请使用准备工作区时的 Python 解释器。")
    repo_paths(repo)
    for rel, expected in manifest["config_sha256"].items():
        path = (root / rel).resolve()
        require(path.is_relative_to(root) and path.is_file() and digest_file(path) == expected,
                f"工作区配置已变化：{rel}")
    code = code_fingerprint(repo)
    frozen = manifest["code"]
    code_matches = (code["head"] == frozen["head"] and code["tracked_diff_sha256"] == frozen["tracked_diff_sha256"]
                    and code["untracked_python_sha256"] == frozen["untracked_python_sha256"])
    if verify_code:
        require(code_matches, "代码相对准备时已变化；请按新版本重新 prepare 一个工作区。")
    original = load_agent(manifest["original_agent_config"])
    load_mic(original.tools.mic_config_dir)  # loads the deployed .env (credentials) into this process only
    os.environ.update(manifest["environment_pins"])
    cfg = load_agent(root / "agent-config.yaml")
    mc = load_mic(cfg.tools.mic_config_dir)
    SECRETS.extend(a.api_key for a in ModelRegistry(mc).adapters.values() if a.api_key)
    check_mic(mc)
    require(cfg.model.primary == PRIMARY and cfg.model.fallbacks == [FALLBACK], "Agent 模型不匹配。")
    require(cfg.runtime.workspace_root == root and cfg.tools.mic_execution_mode == "subprocess", "工作区或执行模式不匹配。")
    require(cfg.tools.mic_python_executable == sys.executable, "MIC 子进程解释器不匹配。")
    require(all(mc.browser_runtime["limits"].get(k) == v for k, v in CAPS.items()), "MIC 预算不匹配。")
    return root, manifest, cfg, mc, code_matches


def readiness():
    require(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "请在图形桌面会话中运行。")
    try:
        with socket.create_connection(("127.0.0.1", 18791), timeout=3):
            pass
    except OSError:
        raise AcceptanceError("18791 网关未监听。请先手动启动 OpenClaw gateway。") from None


def run_dirs(root):
    return sorted(p for p in root.glob("run-*") if p.is_dir())


def batch_usage(root):
    total, runs = 0, []
    for d in run_dirs(root):
        result = d / "result.json"
        if result.is_file():
            data = load(result)
            sent = ((data.get("collection_diagnostics") or {}).get("budget_used") or {}).get("gateway_requests_sent") or 0
            total += int(sent)
            runs.append({"run_dir": d.name, "gateway_requests_sent": sent, "status": data.get("status")})
        else:
            runs.append({"run_dir": d.name, "gateway_requests_sent": None, "status": "incomplete"})
    return {"runs": runs, "gateway_requests_total": total}


# --- run ---------------------------------------------------------------------

@contextmanager
def stop_handlers(agent, stop):
    done = threading.Event()

    def handler(signum, frame):
        stop.set()

    def relay():
        announced = False
        while not done.wait(0.1):
            if stop.is_set():
                if not announced:
                    emit({"status": "ACCEPTANCE_STOP_REQUESTED", "message": "正在停止本轮采集并清理浏览器。"})
                    announced = True
                event = getattr(agent, "_cancel_event", None)
                if event is not None:
                    event.set()

    old = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    thread = threading.Thread(target=relay, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join(timeout=1)
        for sig, previous in old.items():
            signal.signal(sig, previous)


def execute_cycle(agent, cfg, root, manifest, run_dir, now, stop):
    from agent_trade_intel.agent import _mic_task_profile
    from agent_trade_intel.runtime import RuntimeController
    controller = RuntimeController(agent.config, state_store=agent.state_store, bus_store=agent.bus_store,
                                   data_store=agent.data_store)
    demand_id = manifest["demand_id"]
    steps = {}
    try:
        require(not stop.is_set(), "收到停止请求，尚未启动采集。")
        demand = load(root / "demand.json")
        current = agent.registry.get(demand_id)
        if current is None:
            steps["registered"] = agent.registry.register(demand, activate=False)
        require(agent.registry.get(demand_id)["status"] == "suspended", "需求不是暂停状态；未启动。")
        steps["demand_start"] = agent.registry.apply_lifecycle(demand_id, "resume")
        steps["runtime"] = controller.tick(now=now, run_capability_validation=False)
        require(steps["runtime"]["status"] == "ok" and len(steps["runtime"]["created"]) == 2,
                "Runtime 没有恰好生成一个采集请求；已停止。")
        steps["plan"] = agent.run_once(topics=["intelligence.collection"])
        require(steps["plan"].get("status") == "processed" and steps["plan"].get("result", {}).get("task_count") == 1,
                "Agent 没有恰好规划一个任务。")
        pending = [m for m in agent.queue.list_messages(status="open") if m["topic"] == "intelligence.collection"]
        require(len(pending) == 1, "待采集任务数量不是 1。")
        ticket = agent.tickets.get(pending[0]["payload"]["ticket_id"])
        task = ticket["payload"]
        require(ticket["ticket_type"] == "COLLECTION_TASK_TICKET" and task.get("task_type") == "mic_deep_collect"
                and task.get("target", {}).get("target_id") == TARGET_ID, "实际任务超出本轮范围。")
        effective = _mic_task_profile(task, agent.config.raw)
        require(effective == {"focus": ["operating_update"], "time_window": "30d", "budget_profile": CAPS},
                "实际任务预算与准备配置不一致。")
        steps["task_id"] = task["task_id"]
        steps["task_idempotency_key"] = task.get("idempotency_key")
        steps["collect_message_id"] = pending[0]["message_id"]
        steps["collect_ticket_id"] = ticket["ticket_id"]
        save(run_dir / "steps.json", steps)
        require(not stop.is_set(), "收到停止请求，尚未调用 MIC。")
        emit({"status": "ACCEPTANCE_COLLECTION_START", "workspace": str(root), "run_dir": str(run_dir),
              "target": COMPANY, "demand_id": demand_id, "effective_profile": effective,
              "max_output_tokens": 65536, "execution_mode": "subprocess", "automatic_retry": False})
        steps["collect"] = agent.run_once(topics=["intelligence.collection"])
        return steps
    finally:
        steps["supervisor"] = agent.mic.last_outcome
        current = agent.registry.get(demand_id)
        if current and current["status"] == "active":
            steps["demand_stop"] = agent.registry.apply_lifecycle(demand_id, "suspend")
        steps["stop_cleanup"] = controller._consume_demand_messages()
        save(run_dir / "steps.json", steps)


# --- audit -------------------------------------------------------------------

def mic_audit(root, report):
    run_id = report.get("search_run_id")
    if not run_id:
        return None
    with closing(connect_ro(root / "mic.db")) as db:
        run = db.execute("SELECT status FROM search_run WHERE id=?", (run_id,)).fetchone()
        require(run is not None, "MIC 数据库缺少报告对应的运行记录。")
        links = rows(db, "SELECT id,title,url FROM source_link WHERE search_run_id=?", (run_id,))
        counts = {k: 0 for k in TABLES}
        violations, missing_publication, sources, model_requests = [], [], [], []
        for row in links:
            lid = row["id"]
            per_link = {}
            for key, table in TABLES.items():
                n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE source_link_id=?", (lid,)).fetchone()[0]
                counts[key] += n
                if n:
                    per_link[key] = n
            has_diag = any(c["name"] == "request_diagnostics" for c in db.execute("PRAGMA table_info(model_run)"))
            diag_col = "request_diagnostics" if has_diag else "NULL AS request_diagnostics"
            models = rows(db, f"SELECT id,status,output_tokens,error_type,model_name,provider_request_id,{diag_col} "
                              "FROM model_run WHERE source_link_id=?", (lid,), ("request_diagnostics",))
            model_requests.extend({"source_link_id": lid, **m} for m in models)
            reads = rows(db, "SELECT read_status,diagnostics,extracted_publish_time FROM link_read_attempt WHERE source_link_id=?",
                         (lid,), ("diagnostics",))
            allowed, publication_times = False, []
            for read in reads:
                diag = read["diagnostics"] or {}
                window = (diag.get("fetch") or {}).get("time_window", {})
                allowed |= (read["read_status"] == "read" and window.get("allowed") is True
                            and window.get("status") == "in_window" and (diag.get("publication_time") or {}).get("status") == "known")
                if read["read_status"] == "read" and read["extracted_publish_time"]:
                    publication_times.append(read["extracted_publish_time"])
            formal = sum(per_link.get(k, 0) for k in FORMAL)
            if formal or models:
                if formal and not allowed:
                    violations.append(lid)
                if formal and not publication_times:
                    missing_publication.append(lid)
                sources.append({**row, "date_gate_passed": allowed, "counts": per_link,
                                "publication_times_utc": publication_times, "model_runs": models})
        counts["coverage_gaps"] = db.execute("SELECT COUNT(*) FROM coverage_gap WHERE search_run_id=?", (run_id,)).fetchone()[0]
        reported = report.get("structured_outputs", {})
        mismatches = {k: {"report": reported.get(k), "database": n} for k, n in counts.items() if reported.get(k) != n}
        real_requests = [m for m in model_requests if (m.get("request_diagnostics") or {}).get("is_mock") is not True]
        caps_ok = all(((m.get("request_diagnostics") or {}).get("requested_max_tokens") == 65536) for m in model_requests)
        truncated = [m["id"] for m in model_requests if (m.get("request_diagnostics") or {}).get("output_truncated")]
        return {"run_status": run["status"], "counts": counts, "count_mismatches": mismatches,
                "formal_total": sum(counts[k] for k in FORMAL),
                "date_gate_violations": violations, "missing_publication_time": missing_publication,
                "sources": sources, "model_requests": model_requests,
                "model_checks": {"all_requests_real": bool(model_requests) and len(real_requests) == len(model_requests),
                                 "all_requests_capped_at_65536": bool(model_requests) and caps_ok,
                                 "truncated_requests": truncated,
                                 "served_models": sorted({(m.get("request_diagnostics") or {}).get("served_model") or "unknown"
                                                          for m in model_requests})}}


def audit(root, manifest, steps):
    demand_id = manifest["demand_id"]
    with closing(connect_ro(root / "data.db")) as data, closing(connect_ro(root / "bus.db")) as bus:
        runs = rows(data, "SELECT * FROM collection_runs WHERE demand_id=? ORDER BY rowid", (demand_id,))
        attempts = rows(data, "SELECT a.attempt_id,a.state,a.cleanup,a.error_code,a.worker_pid,a.budget_used_json FROM collection_attempt a "
                              "JOIN collection_tasks t ON a.task_id=t.task_id WHERE t.demand_id=?", (demand_id,), ("budget_used_json",))
        tickets = [t for t in rows(bus, "SELECT ticket_id,ticket_type,status,payload_json FROM tickets WHERE ticket_type IN "
                                        "('COLLECTION_REQUEST_TICKET','COLLECTION_TASK_TICKET')", json_fields=("payload_json",))
                   if (t["payload_json"] or {}).get("demand_id") == demand_id]
        ticket_ids = {t["ticket_id"] for t in tickets}
        deliveries = [{"message_id": r["message_id"], "status": r["status"], "attempts": r["attempts"]}
                      for r in rows(bus, "SELECT message_id,status,attempts,payload_json FROM messages WHERE topic='intelligence.collection'",
                                    json_fields=("payload_json",)) if (r["payload_json"] or {}).get("ticket_id") in ticket_ids]
        results = [r["payload_json"] for r in rows(bus, "SELECT payload_json FROM messages WHERE topic='collection.result'",
                                                   json_fields=("payload_json",)) if (r["payload_json"] or {}).get("ticket_id") in ticket_ids]
        this_run = runs[-1] if runs else None
        report = unpack(this_run["result_json"]) if this_run else {}
        quality = unpack(this_run["quality_json"]) if this_run else {}
        errors = unpack(this_run["errors_json"]) if this_run else []
        diag = report.get("collection_diagnostics", {})
        mic = mic_audit(root, report) if (root / "mic.db").is_file() else None
        events = rows(data, "SELECT * FROM structured_events WHERE source_run_id=?", (report.get("search_run_id"),),
                      ("payload_json", "impact_json", "source_refs_json"))
        expected = report.get("all_events") or report.get("top_events") or []
        # Business-event ledger (Codex F3): MIC source rows -> Agent business events.
        # "Agent event rows == MIC event rows" is no longer the assertion; the ledger
        # must account for every source row as primary / linked / replayed.
        ledger_rows = rows(data, "SELECT content_key,event_id,link_status,source_domain FROM structured_event_sources WHERE source_run_id=?",
                           (report.get("search_run_id"),))
        ledger = {"mic_event_rows": len(expected), "source_rows_recorded": len(ledger_rows),
                  "independent_events": len({r["event_id"] for r in ledger_rows}),
                  "new_events": sum(1 for r in ledger_rows if r["link_status"] == "primary"),
                  "linked_rows": sum(1 for r in ledger_rows if r["link_status"] == "linked"),
                  "replayed_rows": sum(1 for r in ledger_rows if r["link_status"] == "replayed"),
                  "events_created_by_this_run": len(events)}
        used = diag.get("budget_used", {})
        sent = used.get("gateway_requests_sent", 0)
        checks = {
            "one_task_planned": steps.get("plan", {}).get("result", {}).get("task_count") == 1,
            "one_collection_attempt": len(attempts) == 1 and len(runs) == 1,
            "both_messages_acked_once": len(deliveries) == 2 and all(d["status"] == "done" and d["attempts"] == 1 for d in deliveries),
            "request_and_task_done": len(tickets) == 2 and all(t["status"] == "done" for t in tickets),
            "result_published_once": len(results) == 1,
            "supervisor_completed_and_reaped": (steps.get("supervisor") or {}).get("status") == "completed"
                                               and (steps.get("supervisor") or {}).get("cleanup") == "complete",
            "gateway_requests_within_budget": isinstance(sent, int) and 0 <= sent <= CAPS["max_gateway_requests"],
            "mic_budget_within_caps": bool(used) and all(used.get(k, 0) <= CAPS[c] for k, c in {
                "queries_attempted": "max_queries", "search_hits": "max_search_hits", "links_selected_for_read": "max_links_to_read",
                "http_read_attempts": "max_http_read_attempts", "browser_read_attempts": "max_browser_read_attempts",
                "model_calls": "max_model_calls", "gateway_requests_sent": "max_gateway_requests"}.items()),
            "run_time_within_deadline": isinstance(diag.get("elapsed_seconds"), (int, float)) and diag["elapsed_seconds"] <= CAPS["max_run_seconds"],
            "reported_budget_limits_match": all(diag.get("budget_limits", {}).get(k) == v for k, v in CAPS.items()),
            "agent_output_usable": quality.get("usable") is True,
            "mic_database_counts_match_report": bool(mic and mic["run_status"] == "completed" and not mic["count_mismatches"]),
            "formal_records_passed_date_gate": bool(mic) and not mic["date_gate_violations"] and not mic["missing_publication_time"],
            "all_model_requests_real_and_64k": bool(mic and mic["model_checks"]["all_requests_real"]
                                                   and mic["model_checks"]["all_requests_capped_at_65536"]) if (mic and mic["model_requests"]) else None,
            "agent_event_ledger_consistent": (ledger["source_rows_recorded"] == ledger["mic_event_rows"]
                                              and ledger["new_events"] + ledger["linked_rows"] + ledger["replayed_rows"] == ledger["source_rows_recorded"]
                                              and ledger["new_events"] == ledger["events_created_by_this_run"]) if expected else None,
            "browser_cleanup_complete": diag.get("cleanup", {}).get("cleanup") == "complete",
            "acceptance_demand_suspended": data.execute("SELECT status FROM collection_demands WHERE demand_id=?", (demand_id,)).fetchone()[0] == "suspended",
            "no_active_demands": data.execute("SELECT COUNT(*) FROM collection_demands WHERE status='active'").fetchone()[0] == 0,
            "no_collection_messages_left": bus.execute("SELECT COUNT(*) FROM messages WHERE topic='intelligence.collection' AND status IN ('open','in_progress')").fetchone()[0] == 0,
            "no_active_attempts_left": data.execute("SELECT COUNT(*) FROM collection_attempt WHERE state IN ('starting','running','cancelling')").fetchone()[0] == 0,
        }
        engineering = [k for k in checks if k not in ("agent_output_usable", "agent_event_ledger_consistent", "all_model_requests_real_and_64k")]
        execution_verified = all(checks[k] for k in engineering) and checks["all_model_requests_real_and_64k"] is not False \
            and checks["agent_event_ledger_consistent"] is not False
        return {"execution_verified": execution_verified, "usable": quality.get("usable") is True,
                "business_positive_candidate": execution_verified and quality.get("usable") is True and bool(mic and mic["formal_total"]),
                "real_model_used": isinstance(sent, int) and sent > 0,
                "checks": checks, "search_run_id": report.get("search_run_id"),
                "ids": {"demand_id": demand_id, "task_id": steps.get("task_id"), "task_idempotency_key": steps.get("task_idempotency_key"),
                        "collect_message_id": steps.get("collect_message_id"), "collect_ticket_id": steps.get("collect_ticket_id"),
                        "agent_run_id": this_run["run_id"] if this_run else None,
                        "attempt_ids": [a["attempt_id"] for a in attempts], "mic_run_id": report.get("search_run_id")},
                "collection_diagnostics": diag, "agent_quality": quality, "agent_errors": errors,
                "mic_database": mic, "attempts": attempts, "supervisor": steps.get("supervisor"),
                "message_deliveries": deliveries, "collection_result_messages": results,
                "agent_structured_events": len(events), "agent_event_ledger": ledger}


def content_review(root, report):
    """Read-only export: formal records with evidence, rejected candidates with reasons."""
    rid = report.get("search_run_id")
    if not rid:
        return {"search_run_id": None, "sources": [], "formal_records": [], "rejected_candidates": []}
    diag = report.get("collection_diagnostics", {})
    decisions = {d.get("source_link_id"): d for d in diag.get("output_decisions", [])}
    with closing(connect_ro(root / "mic.db")) as db:
        queries = rows(db, "SELECT id,query_text,query_family,priority_score,executed FROM search_query WHERE search_run_id=? ORDER BY rowid", (rid,))
        pages = rows(db, "SELECT query_requested,query_observed,engine,status,error_code,result_count FROM search_page_attempt WHERE search_run_id=? ORDER BY rowid", (rid,))
        sources = rows(db, "SELECT id,title,url,domain,source_type,triage_score,triage_decision,read_status,metadata FROM source_link WHERE search_run_id=? ORDER BY rowid", (rid,), ("metadata",))
        formal, rejected = [], []
        for src in sources:
            lid = src["id"]
            src["read_attempts"] = rows(db, "SELECT read_status,failure_reason,http_status,content_length,extracted_title,extracted_publish_time,content_hash,diagnostics FROM link_read_attempt WHERE source_link_id=? ORDER BY rowid", (lid,), ("diagnostics",))
            for attempt in src["read_attempts"]:
                d = attempt.pop("diagnostics", None) or {}
                attempt["transport"] = d.get("transport")
                attempt["publication_time"] = d.get("publication_time")
                attempt["time_window"] = (d.get("fetch") or {}).get("time_window")
                attempt["body_scope_status"] = (d.get("body_scope") or {}).get("status")
                attempt["body_scope_reason"] = (d.get("body_scope") or {}).get("reason")
            src["model_outputs"] = rows(db, "SELECT model_run_id,schema_valid,decision,overall_score,confidence,validation_errors FROM model_output WHERE source_link_id=? ORDER BY rowid", (lid,), ("validation_errors",))
            src["merged"] = rows(db, "SELECT id,decision,overall_score,confidence,merge_method FROM merged_analysis WHERE source_link_id=? ORDER BY rowid", (lid,))
            src["admission"] = decisions.get(lid)
            retrieved = src["read_attempts"][-1] if src["read_attempts"] else {}
            for key in FORMAL:
                for rec in rows(db, f"SELECT * FROM {TABLES[key]} WHERE source_link_id=? ORDER BY rowid", (lid,),
                                ("entities", "metrics", "evidence_locator", "scope", "comparison", "impact_channels", "impact",
                                 "tracking_variables", "subject_entity", "object_entity", "evidence")):
                    formal.append({"record_type": key, "record_id": rec.get("id"), "merged_analysis_id": rec.get("merged_analysis_id"),
                                   "source_link_id": lid, "source": {"url": src["url"], "title": src["title"], "source_type": src["source_type"],
                                                                     "published_at": retrieved.get("extracted_publish_time"),
                                                                     "content_hash": retrieved.get("content_hash")},
                                   "record": {k: v for k, v in rec.items() if k not in ("id", "merged_analysis_id", "source_link_id")}})
            if src["admission"] and src["admission"].get("reason") != "accepted":
                rejected.append({"source_link_id": lid, "url": src["url"], "title": src["title"], "admission": src["admission"],
                                 "model_outputs": src["model_outputs"], "merged": src["merged"]})
        gaps = rows(db, "SELECT * FROM coverage_gap WHERE search_run_id=? ORDER BY rowid", (rid,))
        briefs = [r for s in sources for r in rows(db, "SELECT one_sentence,what_happened,why_it_matters,uncertainty,confidence FROM analysis_brief WHERE source_link_id=?", (s["id"],))]
    read_sources = [s for s in sources if s["read_attempts"]]
    executed = [q for q in queries if q["executed"]]
    verified_families = {q["query_family"] for q in executed if any(
        p["status"] == "ok" and p["query_requested"] == q["query_text"] and p["query_observed"] == q["query_text"] for p in pages)}
    funnel = {"search_hits": len(sources), "selected_for_read": len(read_sources),
              "read_ok": sum(1 for s in read_sources if any(a["read_status"] == "read" for a in s["read_attempts"])),
              "date_gate_passed": sum(1 for s in read_sources if any((a.get("time_window") or {}).get("allowed") is True for a in s["read_attempts"])),
              "model_analyzed": sum(1 for s in sources if s["model_outputs"]),
              "schema_valid": sum(1 for s in sources if any(o["schema_valid"] for o in s["model_outputs"])),
              "admitted": sum(1 for s in sources if (s["admission"] or {}).get("reason") == "accepted"),
              "formal_records": len(formal),
              "read_failure_reasons": dict(Counter(a["failure_reason"] for s in read_sources for a in s["read_attempts"] if a["failure_reason"])),
              "time_window_filtered": diag.get("time_window_filter", {}).get("filtered_by_reason"),
              "admission_reasons": dict(Counter((d or {}).get("reason") for d in decisions.values()))}
    return {"search_run_id": rid, "queries": queries, "search_pages": pages,
            "query_families_executed": sorted({q["query_family"] for q in executed}),
            "query_families_live_verified": sorted(verified_families), "funnel": funnel,
            "sources": sources, "formal_records": formal, "rejected_candidates": rejected,
            "briefs_not_formal": briefs, "coverage_gaps_not_formal": gaps,
            # Report-level list includes the batch-triage call, which has no model_run row.
            "model_requests": diag.get("model_requests"),
            "effect": {"database_read_only": True, "new_model_calls": 0, "new_searches": 0}}


def finalize(root, manifest, run_dir, steps, *, stopped=False, failure=None):
    result = audit(root, manifest, steps)
    report = {}
    with closing(connect_ro(root / "data.db")) as db:
        row = db.execute("SELECT result_json FROM collection_runs WHERE run_id=?", (result["ids"]["agent_run_id"],)).fetchone() \
            if result["ids"]["agent_run_id"] else None
        if row:
            report = unpack(row[0])
            save(run_dir / "mic-report.json", report)
    review = content_review(root, report)
    save(run_dir / "content-review.json", review)
    result.update(status="ACCEPTANCE_RUN_COMPLETE" if result["execution_verified"] else "ACCEPTANCE_RUN_INCOMPLETE",
                  workspace=str(root), run_dir=str(run_dir), label=manifest["label"], code=manifest["code"],
                  reference_time=(report.get("time_window_filter") or report.get("collection_diagnostics", {}).get("time_window_filter") or {}).get("reference_time"),
                  funnel=review["funnel"], query_families_executed=review["query_families_executed"],
                  query_families_live_verified=review["query_families_live_verified"],
                  formal_record_count=len(review["formal_records"]), rejected_candidate_count=len(review["rejected_candidates"]),
                  content_review_file=str(run_dir / "content-review.json"),
                  batch_usage=batch_usage(root) | {"this_run_included": False})
    if stopped:
        result["status"] = "ACCEPTANCE_RUN_STOPPED"
    if failure:
        result["failure"] = failure
        result["execution_verified"] = False
        result["status"] = "ACCEPTANCE_RUN_STOPPED" if stopped else "ACCEPTANCE_RUN_INCOMPLETE"
    save(run_dir / "result.json", result)
    result["batch_usage"] = batch_usage(root)
    save(run_dir / "result.json", result)
    return result


def run(root):
    root, manifest, cfg, mc, _ = load_workspace(root)
    with (root / ".acceptance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AcceptanceError("本工作区已有采集进程在运行。") from None
        usage = batch_usage(root)
        require(len(usage["runs"]) < BATCH_MAX_RUNS, f"本批次已执行 {len(usage['runs'])} 次采集，达到上限 {BATCH_MAX_RUNS}。")
        require(usage["gateway_requests_total"] + CAPS["max_gateway_requests"] <= BATCH_MAX_GATEWAY_REQUESTS,
                "本批次网关请求预算不足以再执行一次完整采集。")
        incomplete = [r for r in usage["runs"] if r["status"] == "incomplete"]
        require(not incomplete, f"存在未完成的运行目录：{incomplete}；请先检查，不自动重复。")
        with closing(connect_ro(root / "data.db")) as db:
            require(db.execute("SELECT COUNT(*) FROM collection_attempt WHERE state IN ('starting','running','cancelling')").fetchone()[0] == 0,
                    "仍有未完成的采集进程记录。")
            demand = db.execute("SELECT status FROM collection_demands WHERE demand_id=?", (manifest["demand_id"],)).fetchone()
            require(demand is None or demand[0] == "suspended", "验收需求不是暂停状态。")
        with closing(connect_ro(root / "bus.db")) as db:
            require(db.execute("SELECT COUNT(*) FROM messages WHERE topic='intelligence.collection' AND status IN ('open','in_progress')").fetchone()[0] == 0,
                    "仍有待处理采集消息。")
        readiness()
        index = len(usage["runs"]) + 1
        run_dir = root / f"run-{index}"
        run_dir.mkdir(mode=0o700)
        now = datetime.now(TZ).isoformat()
        save(run_dir / "started.json", {"started_at": now, "pid": os.getpid(), "target": TARGET_ID,
                                        "code": manifest["code"], "budget": CAPS})
        from agent_trade_intel.agent import IntelligenceCollectorAgent
        from agent_trade_intel.logging_setup import setup_logging
        os.chdir(root)
        setup_logging(cfg.runtime.log_dir)
        agent = IntelligenceCollectorAgent(cfg)
        stop = threading.Event()
        failure = None
        with stop_handlers(agent, stop):
            try:
                steps = execute_cycle(agent, cfg, root, manifest, run_dir, now, stop)
            except Exception as exc:  # noqa: BLE001
                failure = {"error_type": type(exc).__name__,
                           "message": str(exc) if isinstance(exc, AcceptanceError) else f"执行异常，已停止：{type(exc).__name__}: {exc}"[:500]}
                path = run_dir / "steps.json"
                steps = load(path) if path.is_file() else {}
            return finalize(root, manifest, run_dir, steps, stopped=stop.is_set(), failure=failure)


# --- redeliver ---------------------------------------------------------------

def snapshot_counts(root):
    with closing(connect_ro(root / "data.db")) as data, closing(connect_ro(root / "bus.db")) as bus, closing(connect_ro(root / "mic.db")) as mic:
        return {"collection_runs": data.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0],
                "collection_attempts": data.execute("SELECT COUNT(*) FROM collection_attempt").fetchone()[0],
                "structured_events": data.execute("SELECT COUNT(*) FROM structured_events").fetchone()[0],
                "result_messages": bus.execute("SELECT COUNT(*) FROM messages WHERE topic='collection.result'").fetchone()[0],
                "mic_search_runs": mic.execute("SELECT COUNT(*) FROM search_run").fetchone()[0],
                "mic_model_runs": mic.execute("SELECT COUNT(*) FROM model_run").fetchone()[0],
                "mic_formal": sum(mic.execute(f"SELECT COUNT(*) FROM {TABLES[k]}").fetchone()[0] for k in FORMAL)}


def redeliver(root, run_dir):
    root, manifest, cfg, mc, _ = load_workspace(root)
    run_dir = (root / run_dir).resolve() if not Path(run_dir).is_absolute() else Path(run_dir)
    result = load(run_dir / "result.json")
    message_id = result["ids"]["collect_message_id"]
    require(message_id, "该运行没有记录采集消息 ID。")
    before = snapshot_counts(root)
    from agent_trade_intel.agent import IntelligenceCollectorAgent
    from agent_trade_intel.logging_setup import setup_logging
    os.chdir(root)
    setup_logging(cfg.runtime.log_dir)
    agent = IntelligenceCollectorAgent(cfg)
    # Simulate a duplicate delivery of the SAME message (lease expiry / operator retry):
    # the queue's own retry path re-opens it for a new attempt.
    with agent.bus_store.session() as con:
        row = con.execute("SELECT status, attempts FROM messages WHERE message_id=?", (message_id,)).fetchone()
        require(row is not None and row["status"] == "done", "采集消息不是已完成状态，无法做重投验证。")
        con.execute("UPDATE messages SET status='open', lease_owner=NULL, lease_until=NULL, available_at=datetime('now') WHERE message_id=?", (message_id,))
    outcome = agent.run_once(topics=["intelligence.collection"])
    after = snapshot_counts(root)
    inner = (outcome.get("result") or {}) if isinstance(outcome, dict) else {}
    checks = {"same_message_processed": outcome.get("status") == "processed" and outcome.get("message_id") == message_id,
              "reused_previous_run": inner.get("reused") is True and inner.get("run_id") == result["ids"]["agent_run_id"],
              "no_new_collection_run": after["collection_runs"] == before["collection_runs"],
              "no_new_attempt_or_worker": after["collection_attempts"] == before["collection_attempts"] and agent.mic.last_outcome is None,
              "no_new_mic_run_or_model_request": after["mic_search_runs"] == before["mic_search_runs"] and after["mic_model_runs"] == before["mic_model_runs"],
              "no_duplicate_business_records": after["mic_formal"] == before["mic_formal"] and after["structured_events"] == before["structured_events"],
              "result_message_not_duplicated": after["result_messages"] == before["result_messages"]}
    with closing(connect_ro(root / "bus.db")) as bus:
        final = bus.execute("SELECT status, attempts FROM messages WHERE message_id=?", (message_id,)).fetchone()
    out = {"status": "ACCEPTANCE_REDELIVER_PASS" if all(checks.values()) else "ACCEPTANCE_REDELIVER_FAIL",
           "run_dir": str(run_dir), "message_id": message_id, "message_final": dict(final) if final else None,
           "outcome": outcome, "before": before, "after": after, "checks": checks,
           "effect": {"new_model_calls": after["mic_model_runs"] - before["mic_model_runs"], "browser_launch": agent.mic.last_outcome is not None}}
    save(run_dir / "redeliver.json", out)
    return out


# --- next cycle (offline, throw-away copy) -----------------------------------

def next_cycle(root, run_dir):
    root, manifest, cfg, mc, _ = load_workspace(root)
    run_dir = (root / run_dir).resolve() if not Path(run_dir).is_absolute() else Path(run_dir)
    result = load(run_dir / "result.json")
    started = load(run_dir / "started.json")["started_at"]
    tomorrow = (datetime.fromisoformat(started) + timedelta(days=1)).isoformat()
    from agent_trade_intel.agent import IntelligenceCollectorAgent, _mic_task_key
    from agent_trade_intel.config import load_config as load_agent
    from agent_trade_intel.runtime import RuntimeController
    import yaml
    with tempfile.TemporaryDirectory(prefix="acceptance-next-cycle-") as tmp:
        tmp = Path(tmp)
        for name in ("state.db", "bus.db", "data.db"):
            shutil.copyfile(root / name, tmp / name)
        raw = yaml.safe_load((root / "agent-config.yaml").read_text(encoding="utf-8"))
        raw["runtime"].update(workspace_root=str(tmp), state_sqlite_path=str(tmp / "state.db"), bus_sqlite_path=str(tmp / "bus.db"),
                              data_sqlite_path=str(tmp / "data.db"), log_dir=str(tmp / "logs"))
        raw["reports"]["output_dir"] = str(tmp / "reports")
        raw["tools"]["market_intelligence_collector"]["enabled"] = False  # never a real collection here
        raw["tools"]["market_intelligence_collector"]["runs_dir"] = str(tmp / "mic_runs")
        path = tmp / "agent-config.yaml"
        path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        copy_cfg = load_agent(path)
        agent = IntelligenceCollectorAgent(copy_cfg)
        controller = RuntimeController(agent.config, state_store=agent.state_store, bus_store=agent.bus_store, data_store=agent.data_store)
        demand_id = manifest["demand_id"]
        old_key = result["ids"]["task_idempotency_key"]
        old_task_ticket = result["ids"]["collect_ticket_id"]

        def open_collection_messages():
            return [m for m in agent.queue.list_messages(status="open", topic="intelligence.collection")]

        def task_rows():
            with agent.data_store.session() as con:
                return con.execute("SELECT COUNT(*) FROM collection_tasks").fetchone()[0]

        def drain_plans(limit=5):
            outcomes = []
            for _ in range(limit):
                outcome = agent.run_once(topics=["intelligence.collection"])
                if outcome.get("status") == "idle":
                    break
                outcomes.append(outcome)
            return outcomes

        agent.registry.apply_lifecycle(demand_id, "resume")
        # Same day: the request ticket key has minute resolution by design (see
        # DemandCompiler.compile_demand / cadence_due docstring), so a re-tick may
        # create a request. Dedup must happen at the task layer: planning that
        # request must resolve to the already-done task ticket and open nothing.
        tasks_before = task_rows()
        # A different minute on the same local date, so the request key differs
        # from the real run's and the task layer is what gets exercised.
        started_dt = datetime.fromisoformat(started)
        later = started_dt + timedelta(minutes=7)
        same_day_now = (later if later.date() == started_dt.date() else started_dt - timedelta(minutes=7)).isoformat()
        same_day = controller.tick(now=same_day_now, run_capability_validation=False)
        same_day_plans = drain_plans()
        same_day_created = [tid for o in same_day_plans for tid in (o.get("result") or {}).get("created", [])]
        same_day_open_after = open_collection_messages()
        tasks_after_same_day = task_rows()
        same_day_dedup = (
            len(same_day["created"]) == 2 and len(same_day_plans) == 1
            and same_day_plans[0].get("result", {}).get("status") == "planned"
            and old_task_ticket in same_day_created
            and all(tid == old_task_ticket or tid.startswith("msg_") for tid in same_day_created)
            and tasks_after_same_day == tasks_before and same_day_open_after == []
        )

        next_day = controller.tick(now=tomorrow, run_capability_validation=False)
        plan = agent.run_once(topics=["intelligence.collection"])
        pending = [m for m in open_collection_messages()
                   if m["payload"].get("ticket_type") == "COLLECTION_TASK_TICKET"]
        new_task = agent.tickets.get(pending[0]["payload"]["ticket_id"])["payload"] if len(pending) == 1 else None
        # Same business facts in a later cycle (Codex F3 / Q4): the next-cycle task
        # re-extracts the same award notice from another copy — new run id, new
        # link ids, reworded summaries, swapped type labels. Business events must
        # not grow; the rows must attach as linked evidence. An exact replay of
        # the old report is additionally a pure no-op.
        from agent_trade_intel.adapters.common import ToolResult
        with agent.data_store.session() as con:
            old_task = unpack(con.execute("SELECT payload_json FROM collection_tasks WHERE task_id=?",
                                          (result["ids"]["task_id"],)).fetchone()["payload_json"])
            events_before = con.execute("SELECT COUNT(*) FROM structured_events").fetchone()[0]
            ledger_before = con.execute("SELECT COUNT(*) FROM structured_event_sources").fetchone()[0]
            old_report = unpack(con.execute("SELECT result_json FROM collection_runs WHERE run_id=?",
                                            (result["ids"]["agent_run_id"],)).fetchone()["result_json"])
        old_events = old_report.get("all_events") or old_report.get("top_events") or []
        rewritten = []
        for ev in copy.deepcopy(old_events):
            ev["summary"] = "转载：" + str(ev.get("summary", "")).replace("中标", "成功中标")
            ev["event_type"] = {"major_order": "tender", "tender": "major_order"}.get(ev.get("event_type"), ev.get("event_type"))
            if ev.get("source_link_id"):
                ev["source_link_id"] = str(ev["source_link_id"]) + "_next"
            rewritten.append(ev)
        next_report = {**old_report, "search_run_id": str(old_report.get("search_run_id")) + "_next",
                       "all_events": rewritten, "top_events": rewritten[:5]}
        next_task_for_save = new_task or {**old_task, "task_id": "task_next_cycle",
                                          "idempotency_key": str(old_key) + ":next"}
        resaved_next = agent.persister.save_mic_structures(
            task=next_task_for_save, result=ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence",
                                                       request={}, status="success", result=next_report))
        with agent.data_store.session() as con:
            events_after_next = con.execute("SELECT COUNT(*) FROM structured_events").fetchone()[0]
            ledger_after_next = con.execute("SELECT COUNT(*) FROM structured_event_sources").fetchone()[0]
        replay = ToolResult(tool_name="market_intelligence_collector", operation="collect_intelligence", request={},
                            status="success", result=old_report)
        resaved = agent.persister.save_mic_structures(task=old_task, result=replay)
        with agent.data_store.session() as con:
            events_after = con.execute("SELECT COUNT(*) FROM structured_events").fetchone()[0]
        agent.registry.apply_lifecycle(demand_id, "suspend")
        event_ledger = {"events_before": events_before, "events_after_rewritten_next_cycle": events_after_next,
                        "events_after_exact_replay": events_after, "ledger_rows_before": ledger_before,
                        "ledger_rows_after_rewritten_next_cycle": ledger_after_next,
                        "rewritten_save_counts": resaved_next, "exact_replay_counts": resaved}
        checks = {"same_facts_rewritten_next_cycle_add_no_events": (
                      bool(old_events) and events_after_next == events_before and resaved_next.get("events", 0) == 0
                      and resaved_next.get("events_linked", 0) == len(old_events)
                      and ledger_after_next == ledger_before + len(old_events)),
                  "same_facts_exact_replay_is_noop": events_after == events_before and resaved.get("events", 0) == 0
                      and resaved.get("events_linked", 0) == 0 and resaved.get("events_replayed", 0) == len(old_events),
                  "same_day_retick_deduplicated_at_task_layer": same_day["status"] == "ok" and same_day_dedup,
                  "next_day_creates_one_request": next_day["status"] == "ok" and len(next_day["created"]) == 2,
                  "next_day_plans_one_task": plan.get("status") == "processed" and plan.get("result", {}).get("task_count") == 1,
                  "new_task_has_new_idempotency_key": bool(new_task) and new_task.get("idempotency_key") not in (None, old_key),
                  "new_task_would_not_reuse_old_run": bool(new_task) and agent._successful_mic_run(new_task) is None,
                  "old_task_key_still_maps_to_old_run": agent._successful_mic_run({"idempotency_key": old_key}) == result["ids"]["agent_run_id"],
                  "same_task_key_for_same_target": bool(new_task) and _mic_task_key(new_task, TARGET_ID) is not None}
        out = {"status": "ACCEPTANCE_NEXT_CYCLE_PASS" if all(checks.values()) else "ACCEPTANCE_NEXT_CYCLE_FAIL",
               "run_dir": str(run_dir), "reference_now": started, "simulated_next_now": tomorrow,
               "same_day_now": same_day_now, "same_day_tick": same_day, "same_day_plans": same_day_plans,
               "same_day_task_rows_before_after": [tasks_before, tasks_after_same_day],
               "next_day_tick": next_day, "plan": plan,
               "new_task": {k: new_task.get(k) for k in ("task_id", "idempotency_key", "as_of", "task_type")} if new_task else None,
               "old_task_idempotency_key": old_key, "event_ledger": event_ledger, "checks": checks,
               "effect": {"databases": "throw-away copy only", "new_model_calls": 0, "browser_launch": False,
                          "workspace_databases_modified": False}}
    save(run_dir / "next-cycle.json", out)
    return out


# --- export / summary ----------------------------------------------------------

def export(root, run_dir):
    root, manifest, cfg, mc, _ = load_workspace(root, verify_code=False)
    run_dir = (root / run_dir).resolve() if not Path(run_dir).is_absolute() else Path(run_dir)
    report = load(run_dir / "mic-report.json") if (run_dir / "mic-report.json").is_file() else {}
    review = content_review(root, report)
    save(run_dir / "content-review.json", review)
    return review


def summary(root):
    root = root.expanduser().resolve()
    manifest = load(root / "acceptance-manifest.json")
    runs = []
    for d in run_dirs(root):
        entry = {"run_dir": d.name}
        for name in ("result", "redeliver", "next-cycle"):
            path = d / f"{name}.json"
            if path.is_file():
                data = load(path)
                entry[name] = {k: data.get(k) for k in ("status", "execution_verified", "usable", "business_positive_candidate",
                                                        "search_run_id", "formal_record_count", "rejected_candidate_count", "checks")
                               if k in data}
                if name == "result":
                    entry[name]["budget_used"] = (data.get("collection_diagnostics") or {}).get("budget_used")
                    entry[name]["elapsed_seconds"] = (data.get("collection_diagnostics") or {}).get("elapsed_seconds")
                    entry[name]["served_models"] = ((data.get("mic_database") or {}).get("model_checks") or {}).get("served_models")
                    entry[name]["ids"] = data.get("ids")
        runs.append(entry)
    out = {"status": "ACCEPTANCE_SUMMARY", "workspace": str(root), "label": manifest["label"], "code": manifest["code"],
           "budget_per_run": CAPS, "batch_budget": manifest["batch_budget"], "batch_usage": batch_usage(root), "runs": runs}
    save(root / "acceptance-summary.json", out)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3])
    p.add_argument("--label", default=datetime.now(TZ).strftime("%Y%m%d_%H%M"))
    for name in ("run", "summary"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", type=Path, required=True)
    for name in ("redeliver", "next-cycle", "export"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", type=Path, required=True)
        p.add_argument("--run-dir", default="run-1")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "prepare":
            result = prepare(args.repo, args.workspace, args.label)
        elif args.command == "run":
            result = run(args.workspace)
        elif args.command == "redeliver":
            result = redeliver(args.workspace, args.run_dir)
        elif args.command == "next-cycle":
            result = next_cycle(args.workspace, args.run_dir)
        elif args.command == "export":
            result = export(args.workspace, args.run_dir)
        else:
            result = summary(args.workspace)
    except KeyboardInterrupt:
        emit({"status": "ACCEPTANCE_INTERRUPTED", "workspace": str(args.workspace)})
        return 130
    except Exception as exc:  # noqa: BLE001
        emit({"status": "ACCEPTANCE_ERROR", "command": args.command, "error_type": type(exc).__name__,
              "message": str(exc) if isinstance(exc, AcceptanceError) else f"{type(exc).__name__}: {exc}"[:800],
              "workspace": str(args.workspace)})
        return 1
    emit(result)
    status = result.get("status", "")
    return 0 if status.endswith(("READY", "COMPLETE", "PASS", "SUMMARY")) or args.command == "export" else 2


if __name__ == "__main__":
    sys.exit(main())
