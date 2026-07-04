from __future__ import annotations

from typing import Any

from .ids import make_idempotency_key
from .logging_setup import get_logger
from .queue import SQLiteMessageQueue
from .reader import IntelligenceReader
from .tickets import TicketRepository, priority_to_int

logger = get_logger("query_service")

QUERY_REQUEST_TOPIC = "query.intelligence.request"
QUERY_RESPONSE_TOPIC = "query.intelligence.response"
QUERY_REQUEST_TICKET = "INTELLIGENCE_QUERY_REQUEST_TICKET"
QUERY_RESPONSE_TICKET = "INTELLIGENCE_QUERY_RESPONSE_TICKET"

SUPPORTED_QUERY_TYPES = {
    "recent_events",
    "market_features",
    "collection_status",
    "data_quality",
    "tool_capabilities",
}


class IntelligenceQueryService:
    """Message-based intelligence query service (design §11.2–11.4).

    Other agents publish query.intelligence.request messages pointing at an
    INTELLIGENCE_QUERY_REQUEST_TICKET; the collector answers with an
    INTELLIGENCE_QUERY_RESPONSE_TICKET plus a query.intelligence.response message targeted back
    at the requesting agent. The Reader stays an internal detail of this service.
    """

    def __init__(self, reader: IntelligenceReader, tickets: TicketRepository, queue: SQLiteMessageQueue, agent_id: str):
        self.reader = reader
        self.tickets = tickets
        self.queue = queue
        self.agent_id = agent_id

    def publish_request(
        self,
        *,
        query_type: str,
        target: dict[str, Any] | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        source_agent: str,
        target_agent_group: str = "intelligence_collector",
        priority: str = "normal",
        correlation_id: str | None = None,
    ) -> dict[str, str]:
        """Helper for requesting agents / CLI tests to file a query through the queue."""
        payload = {
            "query_type": query_type,
            "target": target or {},
            "filters": filters or {},
            "limit": limit,
        }
        target_label = (target or {}).get("target_id") or (target or {}).get("ticker") or "all"
        idem = make_idempotency_key("query_request", query_type, target_label, source_agent, str(filters or {}))
        ticket_id = self.tickets.create_ticket(
            ticket_type=QUERY_REQUEST_TICKET,
            source_agent=source_agent,
            target_agent_group=target_agent_group,
            priority=priority,
            correlation_id=correlation_id,
            summary_cn=f"{source_agent} 请求查询情报：{query_type} / {target_label}。",
            payload=payload,
            idempotency_key=idem,
        )
        message_id = self.queue.publish(
            QUERY_REQUEST_TOPIC,
            {"ticket_id": ticket_id, "ticket_type": QUERY_REQUEST_TICKET},
            priority=priority_to_int(priority),
            correlation_id=correlation_id,
            idempotency_key=make_idempotency_key("message", "query_request", ticket_id),
            target_agent_group=target_agent_group,
        )
        return {"ticket_id": ticket_id, "message_id": message_id}

    def handle_request_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        payload = ticket.get("payload") or {}
        query_type = payload.get("query_type") or "recent_events"
        target = payload.get("target") or {}
        filters = payload.get("filters") or {}
        limit = int(payload.get("limit") or 50)
        if query_type not in SUPPORTED_QUERY_TYPES:
            result: dict[str, Any] = {"status": "unsupported_query_type", "query_type": query_type, "items": []}
        else:
            result = self._execute(query_type, target=target, filters=filters, limit=limit)
        items = result.get("items") or []
        evidence_refs = _evidence_refs(query_type, items)
        response_payload = {
            "query_type": query_type,
            "target": target,
            "filters": filters,
            "status": result.get("status", "success"),
            "count": len(items),
            "items": items,
        }
        summary = f"返回 {target.get('ticker') or target.get('target_id') or '全部目标'} 的 {query_type} 查询结果 {len(items)} 条。"
        response_ticket_id = self.tickets.create_ticket(
            ticket_type=QUERY_RESPONSE_TICKET,
            source_agent=self.agent_id,
            target_agent_group=ticket.get("source_agent"),
            target_agent_id=ticket.get("source_agent"),
            priority=ticket.get("priority", "normal"),
            parent_ticket_id=ticket["ticket_id"],
            correlation_id=ticket.get("correlation_id"),
            summary_cn=summary,
            payload=response_payload,
            evidence_refs=evidence_refs,
            idempotency_key=make_idempotency_key("query_response", ticket["ticket_id"], "v1"),
        )
        message_id = self.queue.publish(
            QUERY_RESPONSE_TOPIC,
            {"ticket_id": response_ticket_id, "ticket_type": QUERY_RESPONSE_TICKET},
            priority=priority_to_int(ticket.get("priority", "normal")),
            correlation_id=ticket.get("correlation_id"),
            idempotency_key=make_idempotency_key("message", "query_response", response_ticket_id),
            target_agent_id=ticket.get("source_agent"),
        )
        self.tickets.update_status(ticket["ticket_id"], "done", f"answered with {len(items)} items")
        logger.info("answered query ticket %s (%s items) -> %s", ticket["ticket_id"], len(items), response_ticket_id)
        return {
            "status": "answered",
            "query_type": query_type,
            "count": len(items),
            "response_ticket_id": response_ticket_id,
            "response_message_id": message_id,
        }

    def _execute(self, query_type: str, *, target: dict[str, Any], filters: dict[str, Any], limit: int) -> dict[str, Any]:
        if query_type == "recent_events":
            return self.reader.read_recent_events(
                target_id=target.get("target_id"),
                ticker=target.get("ticker"),
                event_types=filters.get("event_types"),
                min_confidence=filters.get("min_confidence"),
                min_data_quality=filters.get("minimum_data_quality"),
                since=filters.get("since"),
                limit=limit,
            )
        if query_type == "market_features":
            return self.reader.read_market_features(
                ticker=target.get("ticker"), window=filters.get("window"), limit=limit
            )
        if query_type == "collection_status":
            res = self.reader.read_collection_status(demand_id=filters.get("demand_id"), limit=limit)
            return {"status": res.get("status"), "items": res.get("tasks") or [], "demand": res.get("demand"), "runs": res.get("runs")}
        if query_type == "data_quality":
            return self.reader.read_data_quality_issues(status=filters.get("status"), limit=limit)
        if query_type == "tool_capabilities":
            return self.reader.read_tool_capabilities(tool_name=filters.get("tool_name"), limit=limit)
        return {"status": "unsupported_query_type", "items": []}


def _evidence_refs(query_type: str, items: list[dict[str, Any]]) -> list[str]:
    key = {
        "recent_events": "event_id",
        "market_features": "feature_id",
        "collection_status": "task_id",
        "data_quality": "issue_id",
        "tool_capabilities": "capability_id",
    }.get(query_type)
    if not key:
        return []
    return [str(item[key]) for item in items if isinstance(item, dict) and item.get(key)]
