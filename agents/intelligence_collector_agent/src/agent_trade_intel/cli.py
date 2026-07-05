from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from .agent import IntelligenceCollectorAgent
from .checkpoint import CheckpointManager
from .circuit_breaker import CircuitBreaker
from .config import load_config
from .dashboard import run_dashboard
from .db import SQLiteStore
from .demand import DemandCompiler, DemandRegistry
from .heartbeat import HeartbeatRecorder
from .logging_setup import get_logger, setup_logging
from .openclaw import OpenClawArtifactRenderer, OpenClawModelValidator
from .pools import PoolRepository
from .query_service import IntelligenceQueryService
from .queue import SQLiteMessageQueue
from .reader import IntelligenceReader
from .recovery import RecoveryManager
from .reports import DailyReportBuilder
from .request_center import RequestCenter, list_mic_targets, resolve_mic_config_dir
from .runtime import RuntimeController
from .session import AgentSessionRepository
from .stores import create_stores, init_unique_stores
from .tickets import TicketRepository
from .time_utils import market_phase, parse_dt, validate_market_calendar

logger = get_logger("cli")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="intel-agent")
    parser.add_argument("--config", required=True, help="Path to intelligence collector YAML config")
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db")
    init_db.add_argument("--reset", action="store_true", help="Delete existing SQLite files before re-initialising")

    config_cmd = sub.add_parser("config")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("validate")

    calendar_cmd = sub.add_parser("calendar", help="Trading-calendar helpers")
    cal_sub = calendar_cmd.add_subparsers(dest="calendar_command", required=True)
    cal_val = cal_sub.add_parser("validate", help="Check market_calendar holiday coverage for a year")
    cal_val.add_argument("--year", type=int, default=None, help="Defaults to the current local year")

    db_cmd = sub.add_parser("db")
    db_sub = db_cmd.add_subparsers(dest="db_command", required=True)
    backup = db_sub.add_parser("backup")
    backup.add_argument("--output-dir", help="Backup directory; defaults to <workspace>/data/backups/<timestamp>")

    demand = sub.add_parser("demand")
    demand_sub = demand.add_subparsers(dest="demand_command", required=True)
    val = demand_sub.add_parser("validate")
    val.add_argument("--file", required=True)
    reg = demand_sub.add_parser("register")
    reg.add_argument("--file", required=True)
    reg.add_argument("--activate", action="store_true")
    demand_sub.add_parser("list").add_argument("--status")
    get = demand_sub.add_parser("get")
    get.add_argument("--demand-id", required=True)
    for action in ("suspend", "resume", "cancel"):
        p = demand_sub.add_parser(action)
        p.add_argument("--demand-id", required=True)
    comp = demand_sub.add_parser("compile")
    comp.add_argument("--demand-id")
    comp.add_argument("--as-of", required=True)
    comp.add_argument("--market-phase")

    runtime = sub.add_parser("runtime")
    rt_sub = runtime.add_subparsers(dest="runtime_command", required=True)
    tick = rt_sub.add_parser("tick")
    tick.add_argument("--now", required=True)
    tick.add_argument("--market-phase")
    tick.add_argument("--run-capability-validation", action="store_true")
    rt_sub.add_parser("recover")
    hb = rt_sub.add_parser("heartbeat")
    hb.add_argument("--limit", type=int, default=10)
    rt_sub.add_parser("capability-validate")

    agent = sub.add_parser("agent")
    ag_sub = agent.add_subparsers(dest="agent_command", required=True)
    ag_sub.add_parser("run-once")
    idle = ag_sub.add_parser("run-until-idle")
    idle.add_argument("--max-messages", type=int, default=100)
    ag_sub.add_parser("status")
    ag_sub.add_parser("checkpoint")
    resume = ag_sub.add_parser("resume")
    resume.add_argument("--max-messages", type=int, default=100)

    queue = sub.add_parser("queue")
    q_sub = queue.add_subparsers(dest="queue_command", required=True)
    q_pub = q_sub.add_parser("publish")
    q_pub.add_argument("--topic", required=True)
    q_pub.add_argument("--ticket-id")
    q_pub.add_argument("--payload-json", help="Inline JSON payload; merged with --ticket-id when both given")
    q_pub.add_argument("--priority", type=int, default=10)
    q_pub.add_argument("--correlation-id")
    q_pub.add_argument("--expires-at")
    q_list = q_sub.add_parser("list")
    q_list.add_argument("--status")
    q_list.add_argument("--topic")
    q_list.add_argument("--limit", type=int, default=50)
    q_inspect = q_sub.add_parser("inspect")
    q_inspect.add_argument("--message-id", required=True)
    q_retry = q_sub.add_parser("retry")
    q_retry.add_argument("--message-id", required=True)
    q_dead = q_sub.add_parser("dead-letter")
    q_dead.add_argument("--limit", type=int, default=50)

    pool = sub.add_parser("pool")
    pool_sub = pool.add_subparsers(dest="pool_command", required=True)
    pool_set = pool_sub.add_parser("set")
    pool_set.add_argument("--layer", required=True)
    pool_set.add_argument("--ticker", required=True)
    pool_set.add_argument("--target-id")
    pool_set.add_argument("--company-name")
    pool_set.add_argument("--sellability", choices=["sellable", "t1_locked", "frozen"])
    pool_set.add_argument("--st", action="store_true")
    pool_set.add_argument("--suspended", action="store_true")
    pool_remove = pool_sub.add_parser("remove")
    pool_remove.add_argument("--layer", required=True)
    pool_remove.add_argument("--ticker", required=True)
    pool_list = pool_sub.add_parser("list")
    pool_list.add_argument("--layer")

    query = sub.add_parser("query")
    query_sub = query.add_subparsers(dest="query_command", required=True)
    q_req = query_sub.add_parser("request")
    q_req.add_argument("--query-type", required=True)
    q_req.add_argument("--ticker")
    q_req.add_argument("--target-id")
    q_req.add_argument("--source-agent", default="cli_operator")
    q_req.add_argument("--filters-json")
    q_req.add_argument("--limit", type=int, default=50)
    q_resp = query_sub.add_parser("responses")
    q_resp.add_argument("--limit", type=int, default=20)

    ticket = sub.add_parser("ticket")
    t_sub = ticket.add_subparsers(dest="ticket_command", required=True)
    t_list = t_sub.add_parser("list")
    t_list.add_argument("--type")
    t_list.add_argument("--status")
    t_list.add_argument("--limit", type=int, default=50)
    t_chain = t_sub.add_parser("chain")
    t_chain.add_argument("--correlation-id", required=True)

    read = sub.add_parser("read")
    r_sub = read.add_subparsers(dest="read_command", required=True)
    r_events = r_sub.add_parser("events")
    r_events.add_argument("--target-id")
    r_events.add_argument("--ticker")
    r_events.add_argument("--limit", type=int, default=50)
    r_features = r_sub.add_parser("market-features")
    r_features.add_argument("--ticker", required=True)
    r_features.add_argument("--window")
    r_features.add_argument("--limit", type=int, default=100)
    r_status = r_sub.add_parser("collection-status")
    r_status.add_argument("--demand-id")
    r_dq = r_sub.add_parser("data-quality")
    r_dq.add_argument("--status")
    r_sub.add_parser("capabilities")

    tools = sub.add_parser("tools")
    tools_sub = tools.add_subparsers(dest="tools_command", required=True)
    tools_sub.add_parser("verify-capabilities")
    tools_sub.add_parser("list-mic-targets")

    request = sub.add_parser("request", help="One-command collection requests for industries / companies / stocks")
    request_sub = request.add_subparsers(dest="request_command", required=True)
    req_ind = request_sub.add_parser("industry", help="Register an industry line: MIC profile + daily demand target")
    req_ind.add_argument("--name", required=True, help="Industry name used in search queries, e.g. AI算力")
    req_ind.add_argument("--target-id", help="Explicit target_id; defaults to industry_<hash>")
    req_ind.add_argument("--aliases", help="Comma-separated aliases")
    req_ind.add_argument("--products", help="Comma-separated products / sub-segments")
    req_ind.add_argument("--upstream", help="Comma-separated upstream terms")
    req_ind.add_argument("--downstream", help="Comma-separated downstream terms")
    req_ind.add_argument("--metrics", help="Comma-separated core metrics to track")
    req_ind.add_argument("--companies", help="Comma-separated representative companies")
    req_ind.add_argument("--tracking-variables", help="Comma-separated research variables, e.g. prosperity_score,policy_change")
    req_ind.add_argument("--demand-id", default=None)
    req_ind.add_argument("--priority", default="normal")
    req_ind.add_argument("--test-mode", action="store_true")
    req_com = request_sub.add_parser("company", help="Register a company: MIC profile + daily demand target (+pool if A-share)")
    req_com.add_argument("--name", required=True, help="Company short name, e.g. 北方华创")
    req_com.add_argument("--ticker", help="A-share (002371.SZ) or HK (0700.HK) ticker")
    req_com.add_argument("--target-id", help="Explicit target_id; defaults to company_<code>")
    req_com.add_argument("--aliases", help="Comma-separated aliases")
    req_com.add_argument("--products", help="Comma-separated products")
    req_com.add_argument("--segments", help="Comma-separated business segments")
    req_com.add_argument("--customers", help="Comma-separated known customers")
    req_com.add_argument("--competitors", help="Comma-separated competitors")
    req_com.add_argument("--upstream", help="Comma-separated upstream terms")
    req_com.add_argument("--downstream", help="Comma-separated downstream terms")
    req_com.add_argument("--markets", help="Comma-separated markets, e.g. A股,港股通")
    req_com.add_argument("--industry-id", help="Research-pool industry line this company belongs to, e.g. industry_ai_semi")
    req_com.add_argument("--tracking-variables", help="Comma-separated research variables, e.g. orders,gross_margin,inventory")
    req_com.add_argument("--demand-id", default=None)
    req_com.add_argument("--pool-layer", default="watchlist")
    req_com.add_argument("--no-pool", action="store_true")
    req_com.add_argument("--priority", default="normal")
    req_com.add_argument("--test-mode", action="store_true")
    req_stk = request_sub.add_parser("stock", help="Register an A-share ticker for post-close data refresh (+pool)")
    req_stk.add_argument("--ticker", required=True)
    req_stk.add_argument("--company-name")
    req_stk.add_argument("--demand-id", default=None)
    req_stk.add_argument("--pool-layer", default="watchlist")
    req_stk.add_argument("--no-pool", action="store_true")
    req_stk.add_argument("--priority", default="normal")
    req_stk.add_argument("--test-mode", action="store_true")
    req_rm = request_sub.add_parser("remove", help="Remove a target from a managed demand")
    req_rm.add_argument("--demand-id", required=True)
    req_rm.add_argument("--target-id")
    req_rm.add_argument("--ticker")
    req_batch = request_sub.add_parser(
        "batch", help="Register a whole research pool from one YAML/JSON spec (see examples/research_pool_full.yaml)"
    )
    req_batch.add_argument("--file", required=True, help="Spec file with defaults / demands / industries / companies / stocks")
    req_batch.add_argument("--test-mode", action="store_true", help="Force test_mode on all entries (budget clamped)")
    req_batch.add_argument(
        "--update-demand-config",
        action="store_true",
        help="Also apply demands: overrides (budget/priority/task_profile) to demands that already exist",
    )
    request_sub.add_parser("status", help="Show MIC-registered targets, managed demands and pool members")

    oc = sub.add_parser("openclaw")
    oc_sub = oc.add_subparsers(dest="openclaw_command", required=True)
    oc_sub.add_parser("validate-model")
    render = oc_sub.add_parser("render-artifacts")
    render.add_argument("--output-dir", required=True)

    report = sub.add_parser("report")
    rep_sub = report.add_subparsers(dest="report_command", required=True)
    daily = rep_sub.add_parser("daily")
    daily.add_argument("--trade-date", required=True)
    daily.add_argument("--output-dir")
    daily.add_argument("--format", choices=["json", "html", "both"], default="both")

    dashboard = sub.add_parser("dashboard", help="Serve the live monitoring dashboard (polls the SQLite stores)")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8700)
    dashboard.add_argument("--refresh-seconds", type=int, default=5)

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(
        cfg.runtime.log_dir,
        level=str(cfg.get("logging.level", "INFO")),
        retention_days=int(cfg.get("logging.retention_days", 14)),
    )
    logger.info("cli invoked: command=%s", args.command)
    if args.command == "init-db" and getattr(args, "reset", False):
        _reset_sqlite_files(cfg)
    stores = create_stores(cfg)
    init_unique_stores(stores)
    state_store = stores["state"]
    bus_store = stores["bus"]
    data_store = stores["data"]

    if args.command == "init-db":
        _print({
            "status": "ok",
            "reset": bool(getattr(args, "reset", False)),
            "state_sqlite_path": str(cfg.runtime.state_sqlite_path),
            "bus_sqlite_path": str(cfg.runtime.bus_sqlite_path),
            "data_sqlite_path": str(cfg.runtime.data_sqlite_path),
            "workspace_root": str(cfg.runtime.workspace_root),
            "log_dir": str(cfg.runtime.log_dir),
        })
        return

    if args.command == "config":
        if args.config_command == "validate":
            calendar_check = validate_market_calendar(
                cfg.raw, parse_dt(None, cfg.runtime.timezone).year
            )
            _print({
                "status": "valid",
                "config_path": str(cfg.path),
                "agent_id": cfg.runtime.agent_id,
                "agent_group": cfg.runtime.agent_group,
                "model_primary": cfg.model.primary,
                "workspace_root": str(cfg.runtime.workspace_root),
                "state_sqlite_path": str(cfg.runtime.state_sqlite_path),
                "bus_sqlite_path": str(cfg.runtime.bus_sqlite_path),
                "data_sqlite_path": str(cfg.runtime.data_sqlite_path),
                "reports_dir": str(cfg.runtime.reports_dir),
                "tools": {
                    "mic_enabled": cfg.tools.mic_enabled,
                    "stock_enabled": cfg.tools.stock_enabled,
                },
                "market_calendar": calendar_check,
            })
        return

    if args.command == "calendar":
        if args.calendar_command == "validate":
            year = args.year or parse_dt(None, cfg.runtime.timezone).year
            _print(validate_market_calendar(cfg.raw, year))
        return

    if args.command == "db":
        if args.db_command == "backup":
            _print(_backup_databases(cfg, args.output_dir))
        return

    if args.command == "demand":
        queue = SQLiteMessageQueue(bus_store)
        registry = DemandRegistry(data_store, queue)
        tickets = TicketRepository(bus_store)
        compiler = DemandCompiler(data_store, queue, tickets, cfg.runtime.agent_id, cfg.runtime.agent_group)
        if args.demand_command == "validate":
            demand = _load_structured_file(args.file)
            registry.validate(demand)
            _print({"status": "valid"})
        elif args.demand_command == "register":
            demand = _load_structured_file(args.file)
            result = registry.register(demand, activate=args.activate)
            # Warn early when targets are missing from MIC's target_profiles.yaml:
            # MIC collection would otherwise fail at execution time with Unknown target_id.
            missing = RequestCenter(cfg, data_store=data_store, bus_store=bus_store).unregistered_mic_targets(demand)
            if missing:
                result["mic_unregistered_targets"] = missing
                result["warning"] = (
                    "these target_ids are not registered in MIC target_profiles.yaml; "
                    "use `intel-agent request industry|company` or edit the file before collection runs"
                )
            _print(result)
        elif args.demand_command == "list":
            _print({"items": registry.list(status=args.status)})
        elif args.demand_command == "get":
            _print(registry.get(args.demand_id) or {})
        elif args.demand_command in {"suspend", "resume", "cancel"}:
            _print(registry.apply_lifecycle(args.demand_id, args.demand_command))
        elif args.demand_command == "compile":
            as_of = args.as_of
            phase = args.market_phase or market_phase(parse_dt(as_of, cfg.runtime.timezone), cfg.raw)
            if args.demand_id:
                demand = registry.get(args.demand_id)
                if not demand:
                    raise SystemExit(f"demand not found: {args.demand_id}")
                _print({"created": compiler.compile_demand(demand, as_of=as_of, market_phase=phase)})
            else:
                _print({"created": compiler.compile_active_demands(registry, as_of=as_of, market_phase=phase)})
        return

    if args.command == "runtime":
        queue = SQLiteMessageQueue(bus_store)
        tickets = TicketRepository(bus_store)
        if args.runtime_command == "tick":
            controller = RuntimeController(cfg, state_store=state_store, bus_store=bus_store, data_store=data_store)
            _print(
                controller.tick(
                    now=args.now,
                    phase=args.market_phase,
                    run_capability_validation=args.run_capability_validation,
                )
            )
        elif args.runtime_command == "recover":
            recovery = RecoveryManager(bus_store, queue, tickets, cfg.runtime.agent_id, cfg.runtime.agent_group)
            _print(recovery.recover())
        elif args.runtime_command == "heartbeat":
            hb = HeartbeatRecorder(state_store, cfg.runtime.agent_id)
            _print({"items": hb.latest(limit=args.limit)})
        elif args.runtime_command == "capability-validate":
            worker = IntelligenceCollectorAgent(cfg)
            res = worker.capabilities.verify_stock_intraday()
            _print({"capability_id": res.capability_id, "status": res.status, "capabilities": res.capabilities, "errors": res.errors})
        return

    if args.command == "agent":
        worker = IntelligenceCollectorAgent(cfg)
        if args.agent_command == "run-once":
            _print(worker.run_once())
        elif args.agent_command == "run-until-idle":
            _print(worker.run_until_idle(max_messages=args.max_messages))
        elif args.agent_command == "status":
            _print(_agent_status(cfg, state_store, bus_store, data_store))
        elif args.agent_command == "checkpoint":
            checkpoints = CheckpointManager(state_store, cfg.runtime.agent_id)
            checkpoint_id = checkpoints.save(
                session_id=None,
                state="manual",
                checkpoint={"reason": "cli manual checkpoint", "queue_depth": worker.queue.depth_by_status()},
            )
            _print({"status": "ok", "checkpoint_id": checkpoint_id})
        elif args.agent_command == "resume":
            recovery = RecoveryManager(worker.bus_store, worker.queue, worker.tickets, cfg.runtime.agent_id, cfg.runtime.agent_group)
            recovered = recovery.recover()
            result = worker.run_until_idle(max_messages=args.max_messages)
            _print({"recovered": recovered, "run": result})
        return

    if args.command == "queue":
        q = SQLiteMessageQueue(bus_store)
        if args.queue_command == "publish":
            payload: dict[str, Any] = {}
            if args.payload_json:
                payload.update(json.loads(args.payload_json))
            if args.ticket_id:
                payload["ticket_id"] = args.ticket_id
            if not payload:
                raise SystemExit("queue publish requires --ticket-id and/or --payload-json")
            msg_id = q.publish(
                args.topic,
                payload,
                priority=args.priority,
                correlation_id=args.correlation_id,
                expires_at=args.expires_at,
            )
            _print({"status": "published", "message_id": msg_id, "topic": args.topic})
        elif args.queue_command == "list":
            _print({"items": q.list_messages(status=args.status, topic=args.topic, limit=args.limit)})
        elif args.queue_command == "inspect":
            _print(q.inspect(args.message_id) or {"status": "not_found", "message_id": args.message_id})
        elif args.queue_command == "retry":
            ok = q.retry_message(args.message_id)
            _print({"status": "requeued" if ok else "not_found", "message_id": args.message_id})
        elif args.queue_command == "dead-letter":
            _print({"items": q.list_messages(status="dead", limit=args.limit)})
        return

    if args.command == "pool":
        pool_repo = PoolRepository(data_store)
        if args.pool_command == "set":
            _print(
                pool_repo.upsert_member(
                    pool_layer=args.layer,
                    ticker=args.ticker,
                    target_id=args.target_id,
                    company_name=args.company_name,
                    sellability=args.sellability,
                    is_st=args.st,
                    is_suspended=args.suspended,
                )
            )
        elif args.pool_command == "remove":
            removed = pool_repo.remove_member(pool_layer=args.layer, ticker=args.ticker)
            _print({"status": "removed" if removed else "not_found", "layer": args.layer, "ticker": args.ticker})
        elif args.pool_command == "list":
            _print({"items": pool_repo.list_members(pool_layer=args.layer)})
        return

    if args.command == "query":
        queue = SQLiteMessageQueue(bus_store)
        tickets = TicketRepository(bus_store)
        service = IntelligenceQueryService(
            IntelligenceReader(data_store=data_store, bus_store=bus_store, state_store=state_store),
            tickets,
            queue,
            cfg.runtime.agent_id,
        )
        if args.query_command == "request":
            target = {}
            if args.ticker:
                target["ticker"] = args.ticker
            if args.target_id:
                target["target_id"] = args.target_id
            filters = json.loads(args.filters_json) if args.filters_json else {}
            _print(
                service.publish_request(
                    query_type=args.query_type,
                    target=target,
                    filters=filters,
                    limit=args.limit,
                    source_agent=args.source_agent,
                    target_agent_group=cfg.runtime.agent_group,
                )
            )
        elif args.query_command == "responses":
            _print({"items": tickets.list(ticket_type="INTELLIGENCE_QUERY_RESPONSE_TICKET", limit=args.limit)})
        return

    if args.command == "ticket":
        repo = TicketRepository(bus_store)
        if args.ticket_command == "list":
            _print({"items": repo.list(ticket_type=args.type, status=args.status, limit=args.limit)})
        elif args.ticket_command == "chain":
            _print({"items": repo.by_correlation(args.correlation_id)})
        return

    if args.command == "read":
        reader = IntelligenceReader(data_store=data_store, bus_store=bus_store, state_store=state_store)
        if args.read_command == "events":
            _print(reader.read_recent_events(target_id=args.target_id, ticker=args.ticker, limit=args.limit))
        elif args.read_command == "market-features":
            _print(reader.read_market_features(ticker=args.ticker, window=args.window, limit=args.limit))
        elif args.read_command == "collection-status":
            _print(reader.read_collection_status(demand_id=args.demand_id))
        elif args.read_command == "data-quality":
            _print(reader.read_data_quality_issues(status=args.status))
        elif args.read_command == "capabilities":
            _print(reader.read_tool_capabilities())
        return

    if args.command == "tools":
        if args.tools_command == "verify-capabilities":
            worker = IntelligenceCollectorAgent(cfg)
            res = worker.capabilities.verify_stock_intraday()
            _print({"capability_id": res.capability_id, "status": res.status, "capabilities": res.capabilities, "errors": res.errors})
        elif args.tools_command == "list-mic-targets":
            config_dir = resolve_mic_config_dir(cfg)
            _print({"mic_config_dir": str(config_dir), "items": list_mic_targets(config_dir)})
        return

    if args.command == "request":
        center = RequestCenter(cfg, data_store=data_store, bus_store=bus_store)
        if args.request_command == "industry":
            _print(
                center.request_industry(
                    name=args.name,
                    target_id=args.target_id,
                    aliases=args.aliases,
                    products=args.products,
                    upstream=args.upstream,
                    downstream=args.downstream,
                    metrics=args.metrics,
                    companies=args.companies,
                    tracking_variables=args.tracking_variables,
                    demand_id=args.demand_id,
                    priority=args.priority,
                    test_mode=args.test_mode,
                )
            )
        elif args.request_command == "company":
            _print(
                center.request_company(
                    name=args.name,
                    ticker=args.ticker,
                    target_id=args.target_id,
                    aliases=args.aliases,
                    products=args.products,
                    segments=args.segments,
                    customers=args.customers,
                    competitors=args.competitors,
                    upstream=args.upstream,
                    downstream=args.downstream,
                    markets=args.markets,
                    industry_id=args.industry_id,
                    tracking_variables=args.tracking_variables,
                    demand_id=args.demand_id,
                    pool_layer=None if args.no_pool else args.pool_layer,
                    priority=args.priority,
                    test_mode=args.test_mode,
                )
            )
        elif args.request_command == "stock":
            _print(
                center.request_stock(
                    ticker=args.ticker,
                    company_name=args.company_name,
                    demand_id=args.demand_id,
                    pool_layer=None if args.no_pool else args.pool_layer,
                    priority=args.priority,
                    test_mode=args.test_mode,
                )
            )
        elif args.request_command == "batch":
            spec = _load_structured_file(args.file)
            if args.test_mode:
                spec.setdefault("defaults", {})["test_mode"] = True
            _print(center.request_batch(spec, update_demand_config=args.update_demand_config))
        elif args.request_command == "remove":
            _print(center.remove_target(demand_id=args.demand_id, target_id=args.target_id, ticker=args.ticker))
        elif args.request_command == "status":
            _print(center.status())
        return

    if args.command == "openclaw":
        if args.openclaw_command == "validate-model":
            _print(OpenClawModelValidator(cfg).validate())
        elif args.openclaw_command == "render-artifacts":
            _print(OpenClawArtifactRenderer(cfg).render(args.output_dir))
        return

    if args.command == "dashboard":
        run_dashboard(
            cfg,
            state_store=state_store,
            bus_store=bus_store,
            data_store=data_store,
            host=args.host,
            port=args.port,
            refresh_seconds=args.refresh_seconds,
        )
        return

    if args.command == "report":
        if args.report_command == "daily":
            out = args.output_dir or cfg.runtime.reports_dir
            builder = DailyReportBuilder(
                data_store=data_store,
                bus_store=bus_store,
                state_store=state_store,
                output_dir=out,
                agent_id=cfg.runtime.agent_id,
                queue=SQLiteMessageQueue(bus_store),
                timezone=cfg.runtime.timezone,
            )
            _print(builder.build(trade_date=args.trade_date, output_format=args.format))
        return


def _agent_status(cfg, state_store: SQLiteStore, bus_store: SQLiteStore, data_store: SQLiteStore) -> dict[str, Any]:
    """One-screen operational overview: session, checkpoint, heartbeat, queue depth, breakers."""
    queue = SQLiteMessageQueue(bus_store)
    sessions = AgentSessionRepository(state_store, cfg.runtime.agent_id)
    checkpoints = CheckpointManager(state_store, cfg.runtime.agent_id)
    heartbeats = HeartbeatRecorder(state_store, cfg.runtime.agent_id)
    breaker = CircuitBreaker(state_store, cfg.raw)
    with bus_store.session() as con:
        open_tickets = con.execute(
            "SELECT ticket_type, COUNT(*) c FROM tickets WHERE status IN ('open', 'in_progress') GROUP BY ticket_type"
        ).fetchall()
    with state_store.session() as con:
        latest_capability = con.execute(
            "SELECT capability_id, tool_name, status, checked_at FROM tool_capabilities ORDER BY checked_at DESC LIMIT 1"
        ).fetchone()
    with data_store.session() as con:
        open_tasks = con.execute(
            "SELECT task_type, COUNT(*) c FROM collection_tasks WHERE status IN ('open', 'in_progress') GROUP BY task_type"
        ).fetchall()
    return {
        "agent_id": cfg.runtime.agent_id,
        "state_sqlite_path": str(cfg.runtime.state_sqlite_path),
        "bus_sqlite_path": str(cfg.runtime.bus_sqlite_path),
        "data_sqlite_path": str(cfg.runtime.data_sqlite_path),
        "workspace_root": str(cfg.runtime.workspace_root),
        "log_dir": str(cfg.runtime.log_dir),
        "latest_session": sessions.latest(),
        "latest_checkpoint": checkpoints.latest(),
        "latest_heartbeats": heartbeats.latest(limit=3),
        "queue_depth_by_status": queue.depth_by_status(),
        "open_tickets_by_type": [dict(r) for r in open_tickets],
        "open_tasks_by_type": [dict(r) for r in open_tasks],
        "latest_capability_check": dict(latest_capability) if latest_capability else None,
        "circuit_breakers": breaker.states(),
    }


def _unique_sqlite_paths(cfg) -> list[Path]:
    paths = []
    for p in (cfg.runtime.state_sqlite_path, cfg.runtime.bus_sqlite_path, cfg.runtime.data_sqlite_path):
        if Path(p) not in paths:
            paths.append(Path(p))
    return paths


def _reset_sqlite_files(cfg) -> None:
    for path in _unique_sqlite_paths(cfg):
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()
                logger.info("reset removed %s", candidate)


def _backup_databases(cfg, output_dir: str | None) -> dict[str, Any]:
    """Daily archive backup (design §14.1): consistent SQLite copies via the backup API."""
    import sqlite3
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target_dir = Path(output_dir) if output_dir else (cfg.runtime.workspace_root / "data" / "backups" / stamp)
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in _unique_sqlite_paths(cfg):
        if not path.exists():
            continue
        dest = target_dir / path.name
        src = sqlite3.connect(str(path))
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        copied.append(str(dest))
    return {"status": "ok", "backup_dir": str(target_dir), "files": copied}


def _load_structured_file(path: str) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".yaml", ".yml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
