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
from .db import SQLiteStore
from .demand import DemandCompiler, DemandRegistry
from .heartbeat import HeartbeatRecorder
from .logging_setup import get_logger, setup_logging
from .openclaw import OpenClawArtifactRenderer, OpenClawModelValidator
from .queue import SQLiteMessageQueue
from .reader import IntelligenceReader
from .recovery import RecoveryManager
from .reports import DailyReportBuilder
from .session import AgentSessionRepository
from .stores import create_stores, init_unique_stores
from .tickets import TicketRepository
from .time_utils import market_phase, parse_dt

logger = get_logger("cli")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="intel-agent")
    parser.add_argument("--config", required=True, help="Path to intelligence collector YAML config")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")

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
    rt_sub.add_parser("recover")
    hb = rt_sub.add_parser("heartbeat")
    hb.add_argument("--limit", type=int, default=10)

    agent = sub.add_parser("agent")
    ag_sub = agent.add_subparsers(dest="agent_command", required=True)
    ag_sub.add_parser("run-once")
    idle = ag_sub.add_parser("run-until-idle")
    idle.add_argument("--max-messages", type=int, default=100)
    ag_sub.add_parser("status")
    resume = ag_sub.add_parser("resume")
    resume.add_argument("--max-messages", type=int, default=100)

    queue = sub.add_parser("queue")
    q_sub = queue.add_subparsers(dest="queue_command", required=True)
    q_list = q_sub.add_parser("list")
    q_list.add_argument("--status")
    q_list.add_argument("--limit", type=int, default=50)
    q_inspect = q_sub.add_parser("inspect")
    q_inspect.add_argument("--message-id", required=True)
    q_retry = q_sub.add_parser("retry")
    q_retry.add_argument("--message-id", required=True)
    q_dead = q_sub.add_parser("dead-letter")
    q_dead.add_argument("--limit", type=int, default=50)

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

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(
        cfg.runtime.log_dir,
        level=str(cfg.get("logging.level", "INFO")),
        retention_days=int(cfg.get("logging.retention_days", 14)),
    )
    logger.info("cli invoked: command=%s", args.command)
    stores = create_stores(cfg)
    init_unique_stores(stores)
    state_store = stores["state"]
    bus_store = stores["bus"]
    data_store = stores["data"]

    if args.command == "init-db":
        _print({
            "status": "ok",
            "state_sqlite_path": str(cfg.runtime.state_sqlite_path),
            "bus_sqlite_path": str(cfg.runtime.bus_sqlite_path),
            "data_sqlite_path": str(cfg.runtime.data_sqlite_path),
            "workspace_root": str(cfg.runtime.workspace_root),
            "log_dir": str(cfg.runtime.log_dir),
        })
        return

    if args.command == "demand":
        registry = DemandRegistry(data_store)
        queue = SQLiteMessageQueue(bus_store)
        tickets = TicketRepository(bus_store)
        compiler = DemandCompiler(data_store, queue, tickets, cfg.runtime.agent_id, cfg.runtime.agent_group)
        if args.demand_command == "validate":
            demand = _load_structured_file(args.file)
            registry.validate(demand)
            _print({"status": "valid"})
        elif args.demand_command == "register":
            demand = _load_structured_file(args.file)
            _print(registry.register(demand, activate=args.activate))
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
            registry = DemandRegistry(data_store)
            compiler = DemandCompiler(data_store, queue, tickets, cfg.runtime.agent_id, cfg.runtime.agent_group)
            phase = args.market_phase or market_phase(parse_dt(args.now, cfg.runtime.timezone), cfg.raw)
            created = compiler.compile_active_demands(registry, as_of=args.now, market_phase=phase)
            _print({"status": "ok", "market_phase": phase, "created": created})
        elif args.runtime_command == "recover":
            recovery = RecoveryManager(bus_store, queue, tickets, cfg.runtime.agent_id, cfg.runtime.agent_group)
            _print(recovery.recover())
        elif args.runtime_command == "heartbeat":
            hb = HeartbeatRecorder(state_store, cfg.runtime.agent_id)
            _print({"items": hb.latest(limit=args.limit)})
        return

    if args.command == "agent":
        worker = IntelligenceCollectorAgent(cfg)
        if args.agent_command == "run-once":
            _print(worker.run_once())
        elif args.agent_command == "run-until-idle":
            _print(worker.run_until_idle(max_messages=args.max_messages))
        elif args.agent_command == "status":
            _print(_agent_status(cfg, state_store, bus_store, data_store))
        elif args.agent_command == "resume":
            recovery = RecoveryManager(worker.bus_store, worker.queue, worker.tickets, cfg.runtime.agent_id, cfg.runtime.agent_group)
            recovered = recovery.recover()
            result = worker.run_until_idle(max_messages=args.max_messages)
            _print({"recovered": recovered, "run": result})
        return

    if args.command == "queue":
        q = SQLiteMessageQueue(bus_store)
        if args.queue_command == "list":
            _print({"items": q.list_messages(status=args.status, limit=args.limit)})
        elif args.queue_command == "inspect":
            _print(q.inspect(args.message_id) or {"status": "not_found", "message_id": args.message_id})
        elif args.queue_command == "retry":
            ok = q.retry_message(args.message_id)
            _print({"status": "requeued" if ok else "not_found", "message_id": args.message_id})
        elif args.queue_command == "dead-letter":
            _print({"items": q.list_messages(status="dead", limit=args.limit)})
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
        return

    if args.command == "openclaw":
        if args.openclaw_command == "validate-model":
            _print(OpenClawModelValidator(cfg).validate())
        elif args.openclaw_command == "render-artifacts":
            _print(OpenClawArtifactRenderer(cfg).render(args.output_dir))
        return

    if args.command == "report":
        if args.report_command == "daily":
            out = args.output_dir or cfg.runtime.reports_dir
            builder = DailyReportBuilder(data_store=data_store, bus_store=bus_store, state_store=state_store, output_dir=out, agent_id=cfg.runtime.agent_id)
            _print(builder.build(trade_date=args.trade_date))
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
