from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional


def _fmt_date(value: str | date | datetime | None) -> str:
    if value is None:
        return "none"
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    raw = str(value).replace("-", "")
    return raw if raw else "none"


def _sorted_join(values: Iterable[str] | None) -> str:
    if not values:
        return "none"
    return ",".join(sorted(str(v) for v in values if str(v))) or "none"


def generate_idempotency_key(
    *,
    module_name: str,
    request_type: str,
    provider: str,
    tickers: Iterable[str] | None = None,
    universe_id: Optional[str] = None,
    exchanges: Iterable[str] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    frequency: Optional[str] = None,
    adjust: Optional[str] = None,
    fields: Iterable[str] | None = None,
    provider_set: Iterable[str] | None = None,
    schema_version: str = "v0.1",
) -> str:
    # Identity slot: tickers when present, else an explicit universe, else the exchange set.
    # The exchange fallback keeps exchange-scoped request types (e.g. trade_calendar) from
    # collapsing to a single shared key across exchanges.
    if tickers:
        identity = _sorted_join(tickers)
    elif universe_id:
        identity = universe_id
    else:
        identity = _sorted_join(exchanges)
    parts = [
        module_name,
        request_type,
        provider,
        identity,
        _fmt_date(start_date),
        _fmt_date(end_date),
        frequency or "none",
        adjust or "none",
        _sorted_join(fields),
    ]
    if provider_set is not None:
        parts.append(_sorted_join(provider_set))
    parts.append(schema_version)
    return ":".join(parts)


def assert_same_key(expected: str, actual: str) -> None:
    if expected != actual:
        raise ValueError("IDEMPOTENCY_CONFLICT: idempotency key mismatch")
