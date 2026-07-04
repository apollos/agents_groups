from __future__ import annotations

import traceback
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from threading import Event, Thread
from typing import Any, Callable, Iterator

from .adapters.mic_adapter import MICAdapter
from .adapters.stock_data_adapter import StockDataCLIAdapter
from .capabilities import ToolCapabilityVerifier
from .checkpoint import CheckpointManager
from .circuit_breaker import CircuitBreaker
from .config import CollectorConfig
from .db import dumps_json
from .demand import DemandRegistry
from .errors import QueueEmpty
from .heartbeat import HeartbeatRecorder
from .ids import make_idempotency_key, new_id, stable_hash, utc_now_iso
from .logging_setup import get_logger
from .market_features import MarketFeatureBuilder, should_emit_feature_ticket
from .memory import AgentMemory
from .persistence import ResultPersister
from .planner import TaskGraphPlanner
from .pools import PoolRepository, resolve_demand_targets
from .quality import QualityGate
from .query_service import QUERY_REQUEST_TICKET, QUERY_REQUEST_TOPIC, IntelligenceQueryService
from .queue import QueueMessage, SQLiteMessageQueue
from .reader import IntelligenceReader
from .session import AgentSessionRepository
from .stores import create_stores, init_unique_stores
from .tickets import TicketRepository, priority_to_int

logger = get_logger("agent")


@contextmanager
def lease_heartbeat(keepalive: Callable[[], None], interval_seconds: int) -> Iterator[None]:
    """Keep extending the message lease while a single long blocking tool call runs.

    MIC deep collects regularly outlive queue.lease_seconds. Without a background heartbeat the
    message would be requeued mid-run and a second worker would execute the same collection.
    """
    stop = Event()

    def loop() -> None:
        while not stop.wait(interval_seconds):
            try:
                keepalive()
            except Exception:  # pragma: no cover - keepalive must never kill the tool call
                logger.warning("lease heartbeat failed", exc_info=True)

    thread = Thread(target=loop, daemon=True, name="lease-heartbeat")
    thread.start()
    try:
        keepalive()
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


class IntelligenceCollectorAgent:
    """OpenClaw-compatible intelligence collection worker.

    The agent has split SQLite stores: private state for memory/checkpoints/sessions, shared bus
    for messages/Tickets and data store for collection outputs. It only consumes messages targeted
    to its configured agent_id/agent_group.
    """

    def __init__(self, config: CollectorConfig):
        self.config = config
        stores = create_stores(config)
        init_unique_stores(stores)
        self.state_store = stores["state"]
        self.bus_store = stores["bus"]
        self.data_store = stores["data"]
        # Backward-compatible alias for older internals/tests; new code should choose the
        # boundary explicitly.
        self.store = self.data_store
        self.queue = SQLiteMessageQueue(self.bus_store)
        self.tickets = TicketRepository(self.bus_store)
        self.registry = DemandRegistry(self.data_store, self.queue)
        self.pool_repo = PoolRepository(self.data_store)
        self.query_service = IntelligenceQueryService(
            IntelligenceReader(data_store=self.data_store, bus_store=self.bus_store, state_store=self.state_store),
            self.tickets,
            self.queue,
            config.runtime.agent_id,
        )
        self.memory = AgentMemory(self.state_store, config.runtime.agent_id)
        self.checkpoints = CheckpointManager(self.state_store, config.runtime.agent_id)
        self.sessions = AgentSessionRepository(self.state_store, config.runtime.agent_id)
        self.planner = TaskGraphPlanner(config.raw)
        self.quality = QualityGate(config.raw)
        self.persister = ResultPersister(self.data_store)
        self.feature_builder = MarketFeatureBuilder(self.data_store, config.raw)
        self.mic = MICAdapter(
            config.tools.mic_config_dir,
            timeout_seconds=int(config.get("tools.market_intelligence_collector.timeout_seconds", 900)),
        )
        self.stock = StockDataCLIAdapter(
            config_dir=config.tools.stock_config_dir,
            python_executable=config.tools.python_executable,
            working_dir=config.tools.stock_working_dir,
            timeout_seconds=int(config.get("tools.stock_data_collector.timeout_seconds", 180)),
        )
        self.capabilities = ToolCapabilityVerifier(self.state_store, self.stock, config.raw)
        self.heartbeats = HeartbeatRecorder(self.state_store, config.runtime.agent_id)
        self.breaker = CircuitBreaker(self.state_store, config.raw)
        self.lease_seconds = int(config.get("queue.lease_seconds", 300))
        self.retry_delay_seconds = int(config.get("queue.retry_delay_seconds", 60))
        self._current_lease: tuple[str, str] | None = None  # (message_id, worker_id)
        self._current_session_id: str | None = None
        self._startup_capability_checked = False

    def run_once(self, *, worker_id: str | None = None, topics: list[str] | None = None) -> dict[str, Any]:
        worker_id = worker_id or f"{self.config.runtime.agent_id}:{new_id('worker')}"
        topics = topics or self.config.get("queue.consume_topics", ["intelligence.collection", QUERY_REQUEST_TOPIC])
        self.queue.expire_messages()
        self.queue.requeue_expired_leases()
        self._maybe_run_startup_capability_check()
        session_id = self._ensure_session()
        self._current_session_id = session_id
        try:
            msg = self.queue.lease(
                topics=topics,
                worker_id=worker_id,
                lease_seconds=self.lease_seconds,
                target_agent_id=self.config.runtime.agent_id,
                target_agent_group=self.config.runtime.agent_group,
            )
        except QueueEmpty:
            self.heartbeats.beat(state="idle", worker_id=worker_id, session_id=session_id)
            self._checkpoint(session_id=session_id, state="idle", checkpoint={"reason": "queue_empty"})
            return {"status": "idle", "reason": "queue_empty"}
        self._current_lease = (msg.message_id, worker_id)
        ticket_id = msg.payload.get("ticket_id")
        self.heartbeats.beat(state="processing", worker_id=worker_id, session_id=session_id, message_id=msg.message_id, ticket_id=ticket_id)
        logger.info("processing message %s ticket=%s", msg.message_id, ticket_id)
        try:
            result = self._handle_message(msg, session_id=session_id)
            action = result.get("_message_action", "ack") if isinstance(result, dict) else "ack"
            if action == "retry":
                error = result.get("_retry_error") or {"error_code": "RETRYABLE_TOOL_FAILURE", "error_message": "retryable tool failure"}
                self.queue.nack(msg.message_id, error, retryable=True, retry_delay_seconds=self.retry_delay_seconds)
                self._checkpoint(
                    session_id=session_id,
                    state="retry_scheduled",
                    checkpoint={"last_message_id": msg.message_id, "result": result, "error": error},
                    current_ticket_id=ticket_id,
                )
                return {"status": "retry_scheduled", "message_id": msg.message_id, "result": _public_result(result)}
            if action == "dead":
                error = result.get("_retry_error") or {"error_code": "NON_RETRYABLE_TOOL_FAILURE", "error_message": "non-retryable tool failure"}
                self.queue.nack(msg.message_id, error, retryable=False, retry_delay_seconds=self.retry_delay_seconds)
                self._checkpoint(
                    session_id=session_id,
                    state="dead_lettered",
                    checkpoint={"last_message_id": msg.message_id, "result": result, "error": error},
                    current_ticket_id=ticket_id,
                )
                return {"status": "dead_lettered", "message_id": msg.message_id, "result": _public_result(result)}
            self.queue.ack(msg.message_id)
            self._checkpoint(
                session_id=session_id,
                state="running",
                checkpoint={"last_message_id": msg.message_id, "result": result},
                current_ticket_id=ticket_id,
            )
            return {"status": "processed", "message_id": msg.message_id, "result": result}
        except Exception as exc:
            tb = traceback.format_exc(limit=20)
            error = {"error_code": "AGENT_TASK_FAILED", "error_message": str(exc), "traceback": tb[-4000:]}
            logger.exception("task failed for message %s ticket=%s", msg.message_id, ticket_id)
            self.queue.nack(msg.message_id, error, retryable=True, retry_delay_seconds=self.retry_delay_seconds)
            self._emit_fault(error, parent_ticket_id=ticket_id, correlation_id=msg.correlation_id)
            self._checkpoint(
                session_id=session_id,
                state="failed_task",
                checkpoint={"last_message_id": msg.message_id, "error": error},
                current_ticket_id=ticket_id,
            )
            return {"status": "failed", "message_id": msg.message_id, "error": error}
        finally:
            self._current_lease = None

    def _keepalive(self) -> None:
        """Extend the active message lease and record a heartbeat.

        Must be called between long-running tool invocations so a multi-call task does not lose
        its lease and get re-executed by another worker.
        """
        if self._current_lease:
            message_id, worker_id = self._current_lease
            self.queue.extend_lease(message_id, worker_id, self.lease_seconds)
            self.heartbeats.beat(state="processing", worker_id=worker_id, session_id=self._current_session_id, message_id=message_id)

    def run_until_idle(self, *, max_messages: int = 100) -> dict[str, Any]:
        processed = []
        for _ in range(max_messages):
            res = self.run_once()
            if res.get("status") == "idle":
                break
            processed.append(res)
        return {"status": "ok", "processed_count": len(processed), "processed": processed}

    def _handle_message(self, msg: QueueMessage, *, session_id: str) -> dict[str, Any]:
        ticket_id = msg.payload.get("ticket_id")
        if not ticket_id:
            raise ValueError("message payload missing ticket_id")
        ticket = self.tickets.get(ticket_id)
        if not ticket:
            raise ValueError(f"ticket not found: {ticket_id}")
        ticket_type = ticket["ticket_type"]
        self.tickets.update_status(ticket_id, "in_progress", "agent leased message")
        if ticket_type == "COLLECTION_REQUEST_TICKET":
            return self._handle_collection_request(ticket)
        if ticket_type == "COLLECTION_TASK_TICKET":
            return self._handle_collection_task(ticket)
        if ticket_type == QUERY_REQUEST_TICKET:
            return self.query_service.handle_request_ticket(ticket)
        self.tickets.update_status(ticket_id, "done", "unsupported ticket type ignored")
        return {"status": "ignored", "ticket_type": ticket_type}

    def _handle_collection_request(self, ticket: dict[str, Any]) -> dict[str, Any]:
        payload = ticket["payload"]
        demand_id = payload.get("demand_id")
        demand = self.registry.get(demand_id)
        if not demand:
            raise ValueError(f"demand not found: {demand_id}")
        as_of = payload.get("as_of") or utc_now_iso()
        market_phase = payload.get("market_phase") or "unknown"
        # dynamic_pool scopes (e.g. current_holding) resolve against the pool_members table.
        targets = resolve_demand_targets(demand, self.pool_repo)
        task_payloads = self.planner.plan(
            demand, request_ticket_id=ticket["ticket_id"], as_of=as_of, market_phase=market_phase, targets=targets
        )
        created: list[str] = []
        for task in task_payloads:
            target = task.get("target") or {}
            ticker = target.get("ticker")
            priority = demand.get("priority", "normal")
            task_ticket_id = self.tickets.create_ticket(
                ticket_type="COLLECTION_TASK_TICKET",
                source_agent=self.config.runtime.agent_id,
                target_agent_group=self.config.runtime.agent_group,
                target_agent_id=self.config.runtime.agent_id,
                priority=priority,
                parent_ticket_id=ticket["ticket_id"],
                correlation_id=ticket.get("correlation_id"),
                related_tickers=[ticker] if ticker else [],
                summary_cn=_task_summary(task),
                payload=task,
                idempotency_key=task["idempotency_key"],
            )
            self._save_task(task, task_ticket_id=task_ticket_id, request_ticket_id=ticket["ticket_id"], demand_id=demand_id)
            msg_id = self.queue.publish(
                "intelligence.collection",
                {"ticket_id": task_ticket_id, "ticket_type": "COLLECTION_TASK_TICKET"},
                priority=priority_to_int(priority),
                correlation_id=ticket.get("correlation_id"),
                idempotency_key=make_idempotency_key("message", "collection_task", task_ticket_id),
                target_agent_id=self.config.runtime.agent_id,
                target_agent_group=self.config.runtime.agent_group,
            )
            created.extend([task_ticket_id, msg_id])
        self.tickets.update_status(ticket["ticket_id"], "done", f"planned {len(task_payloads)} tasks")
        return {"status": "planned", "task_count": len(task_payloads), "created": created}

    def _handle_collection_task(self, ticket: dict[str, Any]) -> dict[str, Any]:
        task = ticket["payload"]
        task_type = task.get("task_type")
        if task_type == "tool_capability_check":
            return self._execute_capability_check(ticket, task)
        if task_type in {"mic_deep_collect", "black_swan_scan"}:
            return self._execute_mic_task(ticket, task)
        if task_type in {"intraday_snapshot_10m", "candidate_full_stock_snapshot", "post_close_stock_refresh"}:
            return self._execute_stock_task(ticket, task)
        self.tickets.update_status(ticket["ticket_id"], "done", "unknown task type ignored")
        return {"status": "ignored", "task_type": task_type}

    def _execute_capability_check(self, ticket: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        if not self.config.tools.stock_enabled:
            self._emit_data_quality(
                severity="P1",
                issue_type="tool_disabled",
                summary_cn="stock_data_collector 已在配置中禁用，跳过盘中能力验证。",
                payload={"tool_name": "stock_data_collector"},
                parent_ticket_id=ticket["ticket_id"],
                correlation_id=ticket.get("correlation_id"),
            )
            self.tickets.update_status(ticket["ticket_id"], "done", "stock_data_collector disabled")
            return {"status": "skipped", "reason": "stock_data_collector disabled"}
        result = self.capabilities.verify_stock_intraday(keepalive=self._keepalive)
        if result.status == "available":
            self.tickets.update_status(ticket["ticket_id"], "done", "capability verification completed")
        else:
            self.tickets.update_status(ticket["ticket_id"], "done", "capability verification completed with unavailable status")
            self._emit_data_quality(
                severity="P1",
                issue_type="capability_gap",
                summary_cn="stock_data_collector 盘中分钟级能力验证未通过或不可用。",
                payload={"capability_id": result.capability_id, "errors": result.errors, "capabilities": result.capabilities},
                parent_ticket_id=ticket["ticket_id"],
                correlation_id=ticket.get("correlation_id"),
            )
        # capability.validation.result message for Runtime / config maintenance consumers.
        self.queue.publish(
            "intelligence.capability",
            {
                "event": "capability.validation.result",
                "tool_name": "stock_data_collector",
                "capability_id": result.capability_id,
                "status": result.status,
                "ticket_id": ticket["ticket_id"],
            },
            priority=priority_to_int("normal"),
            correlation_id=ticket.get("correlation_id"),
            idempotency_key=make_idempotency_key("message", "capability_result", result.capability_id),
        )
        return {"status": result.status, "capability_id": result.capability_id, "capabilities": result.capabilities}

    def _execute_mic_task(self, ticket: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        target = task.get("target") or {}
        target_id = target.get("target_id") or target.get("ticker") or "unknown_target"
        if not self.config.tools.mic_enabled:
            self._emit_data_quality(
                severity="P1",
                issue_type="tool_disabled",
                summary_cn="market_intelligence_collector 已在配置中禁用，跳过 MIC 采集任务。",
                payload={"tool_name": "market_intelligence_collector", "target_id": target_id},
                ticker=target.get("ticker"),
                target_id=target_id,
                parent_ticket_id=ticket["ticket_id"],
                correlation_id=ticket.get("correlation_id"),
            )
            self.tickets.update_status(ticket["ticket_id"], "done", "MIC disabled")
            return {"status": "skipped", "reason": "market_intelligence_collector disabled"}
        if not self.breaker.allow(self.mic.tool_name):
            return self._handle_circuit_open(ticket, tool_name=self.mic.tool_name, ticker=target.get("ticker"), target_id=target_id)
        # A requeued/duplicated message must not re-run an already-successful collection: reuse
        # the persisted run instead of spending MIC budget twice.
        existing_run = self._successful_mic_run(task)
        if existing_run:
            self.tickets.update_status(ticket["ticket_id"], "done", "MIC task already completed by earlier run")
            self._publish_collection_result(ticket, status="success", run_ids=[existing_run], usable=True)
            return {"status": "success", "run_id": existing_run, "reused": True}
        task_profile = _mic_task_profile(task, self.config.raw, default_focus=target.get("focus"))
        # MIC deep collect is one long blocking call; renew the lease on a background thread so
        # the message is not requeued (and double-executed) while MIC is still running.
        heartbeat_interval = max(30, self.lease_seconds // 3)
        with lease_heartbeat(self._keepalive, heartbeat_interval):
            result = self.mic.collect(target_id=target_id, task_profile=task_profile)
        self._record_breaker(self.mic.tool_name, result.status)
        run_id = self.persister.save_run(task=task, ticket_id=ticket["ticket_id"], result=result, demand_id=task.get("demand_id"))
        q = self.quality.evaluate(result, context={"priority": ticket.get("priority")})
        saved = self.persister.save_mic_structures(task=task, result=result) if result.status == "success" else {"events": 0, "coverage_gaps": 0}
        if q["severity"] in {"P0", "P1"}:
            self._emit_data_quality(
                severity=q["severity"],
                issue_type="mic_quality",
                summary_cn="MIC 情报采集存在质量或工具问题。",
                payload={"quality": q, "errors": result.errors, "run_id": run_id},
                parent_ticket_id=ticket["ticket_id"],
                correlation_id=ticket.get("correlation_id"),
            )
        if saved.get("events"):
            self._emit_event_summary(ticket=ticket, target=target, count=saved["events"], run_id=run_id)
        if saved.get("coverage_gaps"):
            self.queue.publish(
                "coverage_gap.created",
                {"target": target, "count": saved["coverage_gaps"], "run_id": run_id, "ticket_id": ticket["ticket_id"]},
                priority=priority_to_int("normal"),
                correlation_id=ticket.get("correlation_id"),
                idempotency_key=make_idempotency_key("message", "coverage_gap", ticket["ticket_id"], run_id),
            )
        action, retry_error = _tool_message_action(q, result.errors)
        if action == "retry":
            self.tickets.update_status(ticket["ticket_id"], "open", "MIC task scheduled for retry", retry_error)
        else:
            self.tickets.update_status(ticket["ticket_id"], "done" if q["usable"] else "failed", "MIC task completed")
            self._publish_collection_result(ticket, status=result.status, run_ids=[run_id], usable=bool(q["usable"]))
        out = {"status": result.status, "run_id": run_id, "quality": q, "saved": saved}
        if action == "retry":
            out["_message_action"] = "retry"
            out["_retry_error"] = retry_error
        return out

    def _successful_mic_run(self, task: dict[str, Any]) -> str | None:
        """run_id of an earlier successful MIC run for the same task idempotency key, if any."""
        if not task.get("idempotency_key"):
            return None
        idem = make_idempotency_key("run", task.get("idempotency_key"), self.mic.tool_name, "collect_intelligence")
        with self.data_store.session() as con:
            row = con.execute(
                "SELECT run_id FROM collection_runs WHERE idempotency_key=? AND status='success'",
                (idem,),
            ).fetchone()
        return str(row["run_id"]) if row else None

    def _execute_stock_task(self, ticket: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        task_type = task.get("task_type")
        target = task.get("target") or {}
        ticker = target.get("ticker")
        if not ticker:
            raise ValueError("stock task missing target.ticker")
        if not self.config.tools.stock_enabled:
            self._emit_data_quality(
                severity="P1",
                issue_type="tool_disabled",
                summary_cn="stock_data_collector 已在配置中禁用，跳过股票数据采集任务。",
                payload={"tool_name": "stock_data_collector", "task_type": task_type},
                ticker=ticker,
                target_id=target.get("target_id"),
                parent_ticket_id=ticket["ticket_id"],
                correlation_id=ticket.get("correlation_id"),
            )
            self.tickets.update_status(ticket["ticket_id"], "done", "stock_data_collector disabled")
            return {"status": "skipped", "reason": "stock_data_collector disabled"}
        if not self.breaker.allow(self.stock.tool_name):
            return self._handle_circuit_open(ticket, tool_name=self.stock.tool_name, ticker=ticker, target_id=target.get("target_id"))
        intraday_frequency: str | None = None
        calls = []
        if task_type == "intraday_snapshot_10m":
            # Use latest verified capability to decide frequency. Default fallback is 15m, then 1d status only.
            intraday_frequency = _preferred_intraday_frequency(self.capabilities.latest_stock_capabilities(), self.config.raw)
            if intraday_frequency:
                freq = intraday_frequency
                calls.append(
                    lambda: self.stock.fetch_historical_bars(
                        tickers=[ticker],
                        start_date=(task.get("bucket_start") or task.get("as_of"))[:10],
                        end_date=(task.get("bucket_end") or task.get("as_of"))[:10],
                        frequency=freq,
                        adjust="none",
                        cross_validate=False,
                    )
                )
            else:
                calls.append(
                    lambda: self.stock.fetch_trading_status(
                        tickers=[ticker],
                        start_date=(task.get("as_of") or utc_now_iso())[:10],
                        end_date=(task.get("as_of") or utc_now_iso())[:10],
                    )
                )
        elif task_type == "candidate_full_stock_snapshot":
            as_of_date = (task.get("as_of") or utc_now_iso())[:10]
            calls.extend(
                [
                    lambda: self.stock.fetch_trading_status(tickers=[ticker], end_date=as_of_date),
                    lambda: self.stock.fetch_historical_bars(tickers=[ticker], end_date=as_of_date, frequency="1d", adjust="none", cross_validate=True),
                    lambda: self.stock.fetch_adj_factor(tickers=[ticker], end_date=as_of_date),
                    lambda: self.stock.fetch_valuation(tickers=[ticker], end_date=as_of_date),
                    lambda: self.stock.fetch_money_flow(tickers=[ticker], end_date=as_of_date),
                    lambda: self.stock.fetch_financial_indicator(tickers=[ticker]),
                    lambda: self.stock.fetch_financial_statement(tickers=[ticker]),
                    lambda: self.stock.fetch_corporate_action(tickers=[ticker], end_date=as_of_date),
                ]
            )
        else:  # post_close_stock_refresh
            target_date = (task.get("as_of") or utc_now_iso())[:10]
            calls.extend(
                [
                    lambda: self.stock.fetch_trading_status(tickers=[ticker], start_date=target_date, end_date=target_date),
                    lambda: self.stock.fetch_historical_bars(tickers=[ticker], start_date=target_date, end_date=target_date, frequency="1d", adjust="none", cross_validate=True),
                    lambda: self.stock.fetch_adj_factor(tickers=[ticker], start_date=target_date, end_date=target_date),
                    lambda: self.stock.fetch_valuation(tickers=[ticker], start_date=target_date, end_date=target_date),
                    lambda: self.stock.fetch_money_flow(tickers=[ticker], start_date=target_date, end_date=target_date),
                    lambda: self.stock.fetch_corporate_action(tickers=[ticker], start_date=target_date, end_date=target_date),
                ]
            )
        run_ids = []
        quality_decisions = []
        feature = None
        for call in calls:
            # Each CLI call can take up to tool timeout; renew the lease first so a multi-call
            # task is never requeued mid-flight and double-executed.
            self._keepalive()
            res = call()
            self._record_breaker(self.stock.tool_name, res.status)
            run_id = self.persister.save_run(task=task, ticket_id=ticket["ticket_id"], result=res, demand_id=task.get("demand_id"))
            run_ids.append(run_id)
            q = self.quality.evaluate(res)
            quality_decisions.append(q)
            if q["severity"] in {"P0", "P1"}:
                self._emit_data_quality(
                    severity=q["severity"],
                    issue_type="stock_data_quality",
                    summary_cn=f"{ticker} 的 {res.operation} 采集存在质量或工具问题。",
                    payload={"quality": q, "errors": res.errors, "run_id": run_id},
                    ticker=ticker,
                    target_id=target.get("target_id"),
                    parent_ticket_id=ticket["ticket_id"],
                    correlation_id=ticket.get("correlation_id"),
                )
            if task_type == "intraday_snapshot_10m" and res.operation == "fetch_historical_bars":
                source_result = res.result
                source_quality = q
                if bool(self.config.get("intraday.use_query_bars_after_fetch", True)) and intraday_frequency:
                    self._keepalive()
                    qres = self.stock.query_bars(
                        ticker=ticker,
                        start_date=(task.get("bucket_start") or task.get("as_of"))[:10],
                        end_date=(task.get("bucket_end") or task.get("as_of"))[:10],
                        frequency=intraday_frequency,
                        adjust="none",
                        trading_ready=bool(self.config.get("intraday.query_bars_trading_ready", False)),
                        minimum_quality=float(self.config.get("quality.minimum_quality_for_public_pool", 0.8)),
                    )
                    self._record_breaker(self.stock.tool_name, qres.status)
                    qrun_id = self.persister.save_run(task=task, ticket_id=ticket["ticket_id"], result=qres, demand_id=task.get("demand_id"))
                    run_ids.append(qrun_id)
                    qq = self.quality.evaluate(qres)
                    quality_decisions.append(qq)
                    if qq["severity"] in {"P0", "P1"}:
                        self._emit_data_quality(
                            severity=qq["severity"],
                            issue_type="stock_data_quality",
                            summary_cn=f"{ticker} 的 query_bars 采集存在质量或工具问题。",
                            payload={"quality": qq, "errors": qres.errors, "run_id": qrun_id},
                            ticker=ticker,
                            target_id=target.get("target_id"),
                            parent_ticket_id=ticket["ticket_id"],
                            correlation_id=ticket.get("correlation_id"),
                        )
                    if qres.status == "success":
                        source_result = qres.result
                        source_quality = qq
                daily_result, history_result = self._feature_enrichment_queries(
                    ticker=ticker, task=task, intraday_frequency=intraday_frequency
                )
                feature = self.feature_builder.build_and_save(
                    task=task,
                    stock_result=source_result,
                    quality=source_quality,
                    source_frequency=intraday_frequency,
                    daily_result=daily_result,
                    history_result=history_result,
                )
                emit, risk_review, reasons = should_emit_feature_ticket(feature, self.config.raw)
                if emit:
                    self._emit_market_feature(ticket=ticket, feature=feature, risk_review=risk_review, reasons=reasons)
        usable = all(q["severity"] != "P0" and q.get("usable") for q in quality_decisions)
        retry_errors: list[dict[str, Any]] = []
        for q in quality_decisions:
            action, retry_error = _tool_message_action(q, q.get("issues") or [])
            if action == "retry" and retry_error:
                retry_errors.append(retry_error)
        if retry_errors:
            self.tickets.update_status(ticket["ticket_id"], "open", "stock data task scheduled for retry", {"retry_errors": retry_errors})
            return {
                "status": "retry_scheduled",
                "run_ids": run_ids,
                "quality": quality_decisions,
                "feature": feature,
                "_message_action": "retry",
                "_retry_error": {"error_code": "RETRYABLE_STOCK_DATA_FAILURE", "error_message": "one or more stock_data calls are retryable", "retryable": True, "errors": retry_errors},
            }
        self.tickets.update_status(ticket["ticket_id"], "done" if usable else "failed", "stock data task completed")
        self._publish_collection_result(ticket, status="success" if usable else "partial_or_failed", run_ids=run_ids, usable=usable)
        return {"status": "success" if usable else "partial_or_failed", "run_ids": run_ids, "quality": quality_decisions, "feature": feature}

    def _feature_enrichment_queries(
        self, *, ticker: str, task: dict[str, Any], intraday_frequency: str | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Local-store query_bars lookups that enrich market features (prev close, 20d history).

        These read already-persisted rows from stock_data's store, so they are cheap and are not
        recorded as collection runs. Failures degrade to un-enriched features instead of failing
        the task.
        """
        enrich_cfg = self.config.get("market_features.enrichment", {}) or {}
        if not bool(enrich_cfg.get("enabled", False)) or not intraday_frequency:
            return None, None
        bucket_date_str = (task.get("bucket_start") or task.get("as_of") or utc_now_iso())[:10]
        try:
            bucket_date = date.fromisoformat(bucket_date_str)
        except ValueError:
            return None, None
        prev_day = (bucket_date - timedelta(days=1)).isoformat()
        daily_result: dict[str, Any] | None = None
        history_result: dict[str, Any] | None = None
        try:
            self._keepalive()
            lookback = int(enrich_cfg.get("prev_close_lookback_days", 10))
            daily = self.stock.query_bars(
                ticker=ticker,
                start_date=(bucket_date - timedelta(days=lookback)).isoformat(),
                end_date=prev_day,
                frequency="1d",
                adjust="none",
            )
            if daily.status == "success":
                daily_result = daily.result
        except Exception:
            logger.warning("prev-close enrichment query failed for %s", ticker, exc_info=True)
        try:
            history_days = int(enrich_cfg.get("same_bucket_history_days", 20))
            if history_days > 0:
                self._keepalive()
                # Calendar window is padded so ~history_days trading days are covered.
                hist = self.stock.query_bars(
                    ticker=ticker,
                    start_date=(bucket_date - timedelta(days=int(history_days * 1.6) + 3)).isoformat(),
                    end_date=prev_day,
                    frequency=intraday_frequency,
                    adjust="none",
                )
                if hist.status == "success":
                    history_result = hist.result
        except Exception:
            logger.warning("same-bucket history enrichment query failed for %s", ticker, exc_info=True)
        return daily_result, history_result

    def _publish_collection_result(self, ticket: dict[str, Any], *, status: str, run_ids: list[str], usable: bool) -> str:
        """collection.result message for Runtime / report builder consumers (design §5.2)."""
        return self.queue.publish(
            "collection.result",
            {
                "ticket_id": ticket["ticket_id"],
                "ticket_type": ticket["ticket_type"],
                "task_type": (ticket.get("payload") or {}).get("task_type"),
                "status": status,
                "usable": usable,
                "run_ids": run_ids,
            },
            priority=priority_to_int("normal"),
            correlation_id=ticket.get("correlation_id"),
            idempotency_key=make_idempotency_key("message", "collection_result", ticket["ticket_id"]),
        )

    def _handle_circuit_open(self, ticket: dict[str, Any], *, tool_name: str, ticker: str | None, target_id: str | None) -> dict[str, Any]:
        """Tool circuit is open: do not call the tool, surface a quality issue, finish the ticket."""
        logger.warning("circuit open for %s; skipping ticket %s", tool_name, ticket["ticket_id"])
        breaker_state = self.breaker.state(tool_name) or {}
        self._emit_data_quality(
            severity="P1",
            issue_type="tool_circuit_open",
            summary_cn=f"{tool_name} 熔断中，采集任务被跳过，等待冷却后重试。",
            payload={
                "tool_name": tool_name,
                "cooldown_until": breaker_state.get("cooldown_until"),
                "consecutive_failures": breaker_state.get("consecutive_failures"),
            },
            ticker=ticker,
            target_id=target_id,
            parent_ticket_id=ticket["ticket_id"],
            correlation_id=ticket.get("correlation_id"),
        )
        self.tickets.update_status(ticket["ticket_id"], "open", f"{tool_name} circuit open; retry after cooldown")
        return {
            "status": "circuit_open",
            "tool_name": tool_name,
            "_message_action": "retry",
            "_retry_error": {"error_code": "TOOL_CIRCUIT_OPEN", "error_message": f"{tool_name} circuit is open", "retryable": True},
        }

    def _record_breaker(self, tool_name: str, status: str) -> None:
        if status == "failed":
            self.breaker.record_failure(tool_name)
        else:
            self.breaker.record_success(tool_name)

    def _save_task(self, task: dict[str, Any], *, task_ticket_id: str, request_ticket_id: str, demand_id: str) -> None:
        target = task.get("target") or {}
        with self.data_store.session() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO collection_tasks(
                  task_id, demand_id, request_ticket_id, task_ticket_id, task_type,
                  target_id, ticker, bucket_start, bucket_size, tool_name, priority,
                  payload_json, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.get("task_id"),
                    demand_id,
                    request_ticket_id,
                    task_ticket_id,
                    task.get("task_type"),
                    target.get("target_id"),
                    target.get("ticker"),
                    task.get("bucket_start"),
                    task.get("bucket_size"),
                    task.get("tool_name"),
                    task.get("priority", "normal"),
                    dumps_json(task),
                    task.get("idempotency_key"),
                ),
            )

    def _emit_data_quality(
        self,
        *,
        severity: str,
        issue_type: str,
        summary_cn: str,
        payload: dict[str, Any],
        ticker: str | None = None,
        target_id: str | None = None,
        parent_ticket_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        issue_id = self.persister.save_quality_issue(
            severity=severity,
            issue_type=issue_type,
            summary_cn=summary_cn,
            payload=payload,
            ticker=ticker,
            target_id=target_id,
        )
        ticket_id = self.tickets.create_ticket(
            ticket_type="DATA_QUALITY_TICKET",
            source_agent=self.config.runtime.agent_id,
            target_agent_group="data_maintenance",
            priority="urgent" if severity == "P0" else "high" if severity == "P1" else "normal",
            parent_ticket_id=parent_ticket_id,
            correlation_id=correlation_id,
            related_tickers=[ticker] if ticker else [],
            summary_cn=summary_cn,
            payload={"issue_id": issue_id, **payload},
            payload_ref=f"db://data_quality_issues/{issue_id}",
            idempotency_key=make_idempotency_key("data_quality", issue_type, ticker or target_id or "none", stable_hash(payload, 8)),
        )
        self.queue.publish(
            "intelligence.quality",
            {"ticket_id": ticket_id, "ticket_type": "DATA_QUALITY_TICKET"},
            priority=priority_to_int("high"),
            correlation_id=correlation_id,
            idempotency_key=make_idempotency_key("message", "dq", ticket_id),
        )
        return ticket_id

    def _emit_fault(self, error: dict[str, Any], *, parent_ticket_id: str | None, correlation_id: str | None) -> str:
        ticket_id = self.tickets.create_ticket(
            ticket_type="FAULT_TICKET",
            source_agent=self.config.runtime.agent_id,
            target_agent_group="runtime_controller",
            priority="urgent",
            parent_ticket_id=parent_ticket_id,
            correlation_id=correlation_id,
            summary_cn="情报收集员 Agent 执行任务失败，需要 Runtime Controller 或负责人检查。",
            payload=error,
            idempotency_key=make_idempotency_key("fault", self.config.runtime.agent_id, error.get("error_code"), stable_hash(error, 8)),
        )
        self.queue.publish(
            "intelligence.quality",
            {"ticket_id": ticket_id, "ticket_type": "FAULT_TICKET"},
            priority=30,
            correlation_id=correlation_id,
            idempotency_key=make_idempotency_key("message", "fault", ticket_id),
        )
        return ticket_id

    def _emit_event_summary(self, *, ticket: dict[str, Any], target: dict[str, Any], count: int, run_id: str) -> str:
        ticker = target.get("ticker")
        event_ticket_id = self.tickets.create_ticket(
            ticket_type="EVENT_TICKET",
            source_agent=self.config.runtime.agent_id,
            target_agent_group="public_data_pool",
            priority="normal",
            parent_ticket_id=ticket["ticket_id"],
            correlation_id=ticket.get("correlation_id"),
            related_tickers=[ticker] if ticker else [],
            summary_cn=f"MIC 采集为 {ticker or target.get('target_id') or '目标'} 生成 {count} 个结构化事件。",
            payload={"run_id": run_id, "event_count": count, "target": target},
            idempotency_key=make_idempotency_key("event_summary", ticket["ticket_id"], run_id),
        )
        self.queue.publish(
            "intelligence.events",
            {"ticket_id": event_ticket_id, "ticket_type": "EVENT_TICKET"},
            priority=10,
            correlation_id=ticket.get("correlation_id"),
            idempotency_key=make_idempotency_key("message", "event_summary", event_ticket_id),
        )
        return event_ticket_id

    def _emit_market_feature(
        self,
        *,
        ticket: dict[str, Any],
        feature: dict[str, Any],
        risk_review: bool = False,
        reasons: list[str] | None = None,
    ) -> str:
        payload = dict(feature)
        payload["emit_reasons"] = reasons or []
        payload["risk_review"] = risk_review
        ticket_id = self.tickets.create_ticket(
            ticket_type="MARKET_FEATURE_TICKET",
            source_agent=self.config.runtime.agent_id,
            target_agent_group="risk_control" if risk_review else "G3_trading_behavior",
            priority="urgent" if risk_review else "normal",
            parent_ticket_id=ticket["ticket_id"],
            correlation_id=ticket.get("correlation_id"),
            related_tickers=[feature["ticker"]],
            summary_cn=feature["summary_cn"],
            payload=payload,
            payload_ref=f"db://market_features/{feature['feature_id']}",
            idempotency_key=make_idempotency_key("market_feature_ticket", feature["feature_id"]),
        )
        self.queue.publish(
            "intelligence.events",
            {"ticket_id": ticket_id, "ticket_type": "MARKET_FEATURE_TICKET"},
            priority=10,
            correlation_id=ticket.get("correlation_id"),
            idempotency_key=make_idempotency_key("message", "market_feature", ticket_id),
        )
        return ticket_id

    def _maybe_run_startup_capability_check(self) -> None:
        """Design §9.2: verify tool capability on startup / once per trading day."""
        if self._startup_capability_checked:
            return
        self._startup_capability_checked = True
        cap_cfg = self.config.get("capability_verification", {}) or {}
        if not bool(cap_cfg.get("run_on_startup", False)) or not self.config.tools.stock_enabled:
            return
        latest = self.capabilities.latest_stock_capabilities()
        today = date.today().isoformat()
        if latest and str(latest.get("checked_at", ""))[:10] == today:
            return
        try:
            result = self.capabilities.verify_stock_intraday(keepalive=self._keepalive)
            logger.info("startup capability verification: %s", result.status)
        except Exception:
            logger.exception("startup capability verification failed")

    def _ensure_session(self) -> str:
        latest = self.sessions.latest()
        if latest and latest.get("status") == "running":
            if not self._session_expired(latest):
                return latest["session_id"]
            # Session rotation (design §14.1): close long-lived sessions and start a fresh one.
            self.sessions.stop(latest["session_id"], status="rotated")
            logger.info("rotated agent session %s", latest["session_id"])
        return self.sessions.start(
            model_ref=self.config.model.primary,
            metadata={
                "openclaw_model": {
                    "primary": self.config.model.primary,
                    "fallbacks": self.config.model.fallbacks,
                    "require_registered": self.config.model.require_registered,
                },
                "config_path": str(self.config.path),
            },
        )

    def _session_expired(self, session: dict[str, Any]) -> bool:
        max_age_minutes = int(self.config.get("runtime.session_rotate_minutes", 720))
        if max_age_minutes <= 0:
            return False
        started_at = session.get("started_at")
        if not started_at:
            return False
        try:
            started = datetime.strptime(str(started_at), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return datetime.now(timezone.utc) - started > timedelta(minutes=max_age_minutes)

    def _checkpoint(self, *, session_id: str, state: str, checkpoint: dict[str, Any], current_ticket_id: str | None = None) -> str:
        checkpoint_id = self.checkpoints.save(
            session_id=session_id,
            state=state,
            checkpoint={"agent_id": self.config.runtime.agent_id, "created_at": utc_now_iso(), **checkpoint},
            current_ticket_id=current_ticket_id,
        )
        if bool(self.config.get("queue.publish_checkpoint_messages", False)):
            self.queue.publish(
                "checkpoint.created",
                {"checkpoint_id": checkpoint_id, "agent_id": self.config.runtime.agent_id, "state": state},
                priority=0,
                idempotency_key=make_idempotency_key("message", "checkpoint", checkpoint_id),
            )
        return checkpoint_id


def _task_summary(task: dict[str, Any]) -> str:
    target = task.get("target") or {}
    label = target.get("ticker") or target.get("company_name") or target.get("target_id") or "目标"
    return f"执行 {label} 的 {task.get('task_type')} 采集任务。"


def _mic_task_profile(task: dict[str, Any], config: dict[str, Any], default_focus: list[str] | None = None) -> dict[str, Any]:
    """Resolve the MIC task profile: demand task_profile > YAML mic_task_defaults.

    No budget is hardcoded here; YAML config is the single source of defaults. When the demand is
    in test_mode, the budget is clamped by mic_task_defaults.test_mode_budget_profile.
    """
    defaults_cfg = config.get("mic_task_defaults", {}) or {}
    profiles = (task.get("task_profile") or {}).get("mic", {}) or {}
    if task.get("task_type") == "black_swan_scan":
        d = defaults_cfg.get("black_swan", {}) or {}
        focus = profiles.get("black_swan_focus") or profiles.get("focus") or default_focus or d.get("focus") or ["risk"]
        budget = profiles.get("black_swan_budget_profile") or profiles.get("budget_profile") or d.get("budget_profile") or {}
        time_window = profiles.get("black_swan_time_window") or profiles.get("time_window") or d.get("time_window") or "7d"
    else:
        d = defaults_cfg.get("deep_collect", {}) or {}
        focus = profiles.get("focus") or default_focus or d.get("focus") or ["risk"]
        budget = profiles.get("budget_profile") or d.get("budget_profile") or {}
        time_window = profiles.get("time_window") or d.get("time_window") or "30d"
    if task.get("test_mode"):
        cap = defaults_cfg.get("test_mode_budget_profile", {}) or {}
        budget = {
            key: min(int(budget.get(key, limit)), int(limit))
            for key, limit in cap.items()
        } or budget
    return {"focus": focus, "time_window": time_window, "budget_profile": budget}


def _preferred_intraday_frequency(capability: dict[str, Any] | None, config: dict[str, Any]) -> str | None:
    fallback = config.get("capability_verification", {}).get("stock_data_collector", {}).get(
        "fallback_order", ["5m", "15m", "none"]
    )
    freqs = ((capability or {}).get("capabilities") or {}).get("frequencies", {})
    for freq in fallback:
        if freq == "none":
            return None
        info = freqs.get(freq)
        if info and info.get("usable"):
            return freq
    # If no verification exists yet, use conservative configured default.
    return config.get("capability_verification", {}).get("stock_data_collector", {}).get("unverified_default_frequency", "15m")

NON_RETRYABLE_ERROR_CODES = {
    "TOKEN_MISSING",
    "AUTH_FAILED",
    "PERMISSION_DENIED",
    "STORAGE_FAILED",
    "RAW_SAVE_FAILED",
    "INVALID_REQUEST",
    "INVALID_TICKER",
    "INVALID_DATE_RANGE",
    "EMPTY_RESULT",
    "EASTMONEY_COOKIE_INVALID",
}

RETRYABLE_ERROR_CODES = {
    "RATE_LIMITED",
    "PROVIDER_TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "STOCK_DATA_CLI_FAILED",
    "STOCK_DATA_TIMEOUT",
    "STOCK_DATA_ADAPTER_ERROR",
    "MIC_TOOL_FAILED",
    "MIC_READ_FAILED",
}


def _tool_message_action(quality: dict[str, Any], errors: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Return ack/retry/dead decision for the delivery message.

    Data conflicts and human-actionable credential/storage faults are acknowledged and surfaced via
    DATA_QUALITY/FAULT tickets; transient provider/tool failures are nacked for retry.
    """
    if quality.get("usable"):
        return "ack", None
    issue_codes = {str(e.get("error_code")) for e in errors or [] if e.get("error_code")}
    issue_codes.update(str(i.get("error_code")) for i in quality.get("issues", []) if isinstance(i, dict) and i.get("error_code"))
    # Conflicts are not fixed by blind retry; they require review/quarantine.
    if quality.get("decision") in {"quarantine", "accept_with_review"}:
        return "ack", None
    if issue_codes & NON_RETRYABLE_ERROR_CODES:
        return "ack", None
    if issue_codes & RETRYABLE_ERROR_CODES:
        return "retry", {"error_code": next(iter(issue_codes & RETRYABLE_ERROR_CODES)), "error_message": "retryable tool/provider failure", "retryable": True, "errors": errors}
    if any(e.get("retryable") for e in errors or []):
        return "retry", {"error_code": "RETRYABLE_TOOL_FAILURE", "error_message": "tool reported retryable failure", "retryable": True, "errors": errors}
    return "ack", None


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if not k.startswith("_")}
