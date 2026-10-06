"""Translation of a business ``MarketContextRequest`` into the tool's ``StockDataRequest``.

Shared by ``MarketContextService`` (caller-facing) and ``IngestionRunner`` (which needs the
same daily-bar request to confirm the session date of a realtime commodity snapshot before
the snapshot record is stored). Keeping the window, idempotency-key and request layout in
one place guarantees both paths hit the same stored daily bars.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from stock_data_ingestion.config import AppConfig
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.market_context import ContextFrequency, ContextType, MarketContextRequest
from stock_data_ingestion.schemas.requests import Frequency, RequestType, StockDataRequest


def market_context_window(config: AppConfig, request: MarketContextRequest) -> tuple[date | None, date | None]:
    if request.mode == "history":
        return request.start_date, request.end_date
    if str(request.frequency) == ContextFrequency.realtime:
        return None, None
    as_of = request.effective_as_of()
    return as_of - timedelta(days=config.market_context.latest_lookback_days), as_of


def market_context_idempotency_key(
    request: MarketContextRequest, identity: dict[str, Any], providers: list[str], start: date | None, end: date | None
) -> str:
    parts = [
        "market_context",
        str(request.context_type),
        request.symbol.upper(),
        str(identity.get("market") or ""),
        str(identity.get("contract") or ""),
        str(identity.get("instrument_type") or ""),
        str(identity.get("tenor") or ""),
        str(identity.get("rate_type") or ""),
        str(request.frequency),
        start.isoformat() if start else "",
        end.isoformat() if end else "",
        "+".join(providers),
        request.mode,
    ]
    now = now_asia_shanghai()
    if str(request.frequency) == ContextFrequency.realtime:
        # Snapshots change continuously; identical requests inside one minute are the same run.
        parts.append(now.strftime("%Y%m%d%H%M"))
    elif request.mode == "latest" and request.effective_as_of() >= now.date():
        # Today's value may not be published yet at the first attempt: allow one
        # re-collection per hour instead of skipping forever. Older as_of windows are
        # closed and stay fully deterministic. Storage keeps one current row per business key.
        parts.append(now.strftime("%Y%m%d%H"))
    return ":".join(parts)


def build_market_context_stock_request(
    config: AppConfig,
    request: MarketContextRequest,
    identity: dict[str, Any],
    providers: list[str],
    canonical: str,
    *,
    parent_request_id: str | None = None,
) -> StockDataRequest:
    context_type = str(request.context_type)
    start, end = market_context_window(config, request)
    ctx = {
        "context_id": request.context_id,
        "context_type": context_type,
        "symbol": request.symbol,
        "frequency": str(request.frequency),
        "mode": request.mode,
        "as_of": request.effective_as_of().isoformat(),
        "market": identity.get("market"),
        "contract": identity.get("contract"),
        "instrument_type": identity.get("instrument_type"),
        "tenor": identity.get("tenor"),
        "rate_type": identity.get("rate_type"),
        "metrics": list(request.metrics),
        # Log correlation: the caller's trace id and, for internal sub-requests (daily bars
        # collected to confirm a snapshot date), the request that spawned them.
        "trace_id": request.trace_id,
        "parent_request_id": parent_request_id,
    }
    if context_type == ContextType.equity_index:
        request_type = RequestType.index_data
        extra = {
            "index_codes": [identity["index_code"]],
            "include_bars": True,
            "include_constituents": False,
            "market_context": ctx,
        }
        frequency: Frequency | None = Frequency.d1
    else:
        request_type = RequestType.market_context
        extra = {"market_context": ctx}
        frequency = Frequency.realtime if str(request.frequency) == ContextFrequency.realtime else Frequency.d1
    return StockDataRequest(
        request_id=f"req_{uuid4().hex[:16]}",
        request_type=request_type,
        universe_id=f"market_context:{context_type}:{request.symbol.upper()}",
        market=str(identity.get("market") or "A_share"),
        start_date=start,
        end_date=end,
        frequency=frequency,
        provider_priority=providers,
        canonical_provider=canonical,
        cross_validate=bool(request.cross_validate and len(providers) > 1),
        save_raw=request.save_raw,
        save_cleaned=request.save_cleaned,
        export_parquet=request.export_parquet,
        idempotency_key=market_context_idempotency_key(request, identity, providers, start, end),
        requested_by=request.requested_by,
        extra_params=extra,
    )


__all__ = ["build_market_context_stock_request", "market_context_idempotency_key", "market_context_window"]
