"""Unified market-context entry point (indices / FX / commodities / rates).

The service turns a business ``MarketContextRequest`` into the tool's regular ingestion
pipeline (``IngestionRunner.run``), so raw retention, standardization, quality scoring,
SQLite/Parquet persistence and idempotency are exactly the ones every other request
type uses. On top of the stored standard records it computes what callers need to
reason about the data without touching vendors:

* which observation answers "as of <day>" (never relabelling an older row as today's);
* freshness relative to the caller's tolerance;
* change metrics with explicit period units (trading day / observation / snapshot vs
  previous settle) and kinds (percent / percentage point + basis points);
* anomaly flags, cross-provider conflicts, and full provenance (request/run/record/raw ids).

A-share indices reuse ``request_type=index_data`` and ``IndexBarRecord``; the other
categories use ``request_type=market_context`` whose vendor bindings live in
``config/market_context_sources.yaml``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorCode, ErrorRecord
from stock_data_ingestion.schemas.market_context import (
    CHANGE_PERIODS,
    DEFAULT_HEADLINE_METRIC,
    REALTIME_HEADLINE_METRIC,
    ChangeMetric,
    ContextFrequency,
    ContextType,
    MarketContextObservation,
    MarketContextProvenance,
    MarketContextQuality,
    MarketContextRequest,
    MarketContextResponse,
    MarketContextResult,
    MarketContextSource,
)
from stock_data_ingestion.schemas.requests import StockDataRequest
from stock_data_ingestion.schemas.quality import ValidationStatus
from stock_data_ingestion.schemas.responses import StockDataResponse
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.services.market_context_requests import build_market_context_stock_request

# Record bucket and date column per context type.
_BUCKET_BY_TYPE: dict[str, str] = {
    ContextType.equity_index: "index_bars",
    ContextType.hk_index: "index_bars",
    ContextType.fx: "fx_rates",
    ContextType.commodity: "commodity_prices",
    ContextType.interest_rate: "interest_rates",
}
_DATE_FIELD_BY_TYPE: dict[str, str] = {
    ContextType.equity_index: "trade_date",
    ContextType.hk_index: "trade_date",
    ContextType.fx: "rate_date",
    ContextType.commodity: "trade_date",
    ContextType.interest_rate: "rate_date",
}
_VALUE_FIELDS_BY_TYPE: dict[str, list[str]] = {
    ContextType.equity_index: ["open", "high", "low", "close", "pre_close", "change", "pct_change", "volume", "amount"],
    ContextType.hk_index: ["open", "high", "low", "close", "pre_close", "change", "pct_change", "volume", "amount"],
    ContextType.commodity: ["open", "high", "low", "close", "settle", "latest", "pre_close", "pre_settle", "volume", "open_interest"],
    ContextType.interest_rate: ["rate_value"],
}
# Unit label returned with the headline value.
_UNIT_BY_TYPE: dict[str, str] = {
    ContextType.equity_index: "index_points",
    ContextType.hk_index: "index_points",
    ContextType.interest_rate: "percent",
}
# 1-period moves beyond these thresholds are flagged (not rejected) as anomalies.
_LARGE_MOVE_THRESHOLD: dict[str, float] = {
    ContextType.equity_index: 0.15,
    ContextType.hk_index: 0.15,
    ContextType.fx: 0.05,
    ContextType.commodity: 0.20,
    ContextType.interest_rate: 1.0,  # percentage points
}
# Daily bars of exchange-traded series are one row per trading day; quote/yield tables are
# "observations" (BOC publishes on some non-trading days, curves skip holidays).
_PERIOD_UNIT_BY_TYPE: dict[str, str] = {
    ContextType.equity_index: "trading_day",
    ContextType.hk_index: "trading_day",
    ContextType.commodity: "trading_day",
    ContextType.fx: "observation",
    ContextType.interest_rate: "observation",
}

# These are decisions already made by the ingestion pipeline. The market-context
# summary must not turn an isolated or failed record into an automatically usable value.
_BLOCKING_VALIDATION_STATUSES = frozenset({
    ValidationStatus.quarantined,
    ValidationStatus.manual_review_required,
    ValidationStatus.conflicted_high,
    ValidationStatus.failed,
})


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _d(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _dt(value: Any) -> datetime | None:
    """Parse a stored/returned timestamp; naive values are Asia/Shanghai (SQLite drops tzinfo).

    First responses and store read-backs must expose identical ``observed_at`` values.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed


class MarketContextService:
    def __init__(self, runner: IngestionRunner) -> None:
        self.runner = runner
        self.config = runner.config

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def fetch(self, request: MarketContextRequest) -> MarketContextResponse:
        created_at = now_asia_shanghai()
        context_type = str(request.context_type)
        warnings: list[str] = []
        errors: list[ErrorRecord] = []

        provider_priority, canonical = self._providers(request, context_type)
        entry = self._symbol_entry(context_type, request.symbol, canonical, provider_priority)
        if entry is None and context_type != ContextType.equity_index:
            errors.append(
                ErrorRecord(
                    error_code=ErrorCode.INVALID_REQUEST,
                    error_message=(
                        f"no market-context binding for {context_type}:{request.symbol}; "
                        "add it to config/market_context_sources.yaml"
                    ),
                    retryable=False,
                    suggested_action="Register the symbol under providers.<provider>.<context_type>.symbols.",
                )
            )
            return self._failed(request, created_at, errors, warnings, entry={})
        entry = entry or {"name": request.symbol}
        # The symbol decides what is fetched; extra business parameters may only confirm the
        # identity bound to it. A mismatch is rejected before any vendor call or write.
        mismatch = self._validate_business_params(request, entry)
        if mismatch is not None:
            errors.append(mismatch)
            return self._failed(request, created_at, errors, warnings, entry=entry)
        identity = self._identity(request, entry)

        stock_request = build_market_context_stock_request(self.config, request, identity, provider_priority, canonical)
        stock_response = self.runner.run(stock_request)
        errors.extend(stock_response.errors)
        # Runner warnings are per record (e.g. one "canonical_only_not_validated" per row);
        # callers need the distinct set with a count.
        counts: dict[str, int] = defaultdict(int)
        for w in stock_response.quality_report.warnings:
            counts[str(w)] += 1
        warnings.extend(f"{w} (x{n})" if n > 1 else w for w, n in counts.items())

        records = self._records_from_response(stock_response, context_type)
        fallback_reasons = {
            str(row.get("fallback_reason"))
            for result in stock_response.provider_results
            for row in (result.raw_records or [])
            if row.get("fallback_reason")
        }
        for reason in sorted(fallback_reasons):
            warnings.append(f"source_fallback: {reason[:300]}")
        idempotent_skip = any("idempotency_key already succeeded" in w for w in stock_response.quality_report.warnings)
        if idempotent_skip and self.runner.database is not None:
            # Explicit idempotent hit only: read back the records of the run that originally
            # succeeded under this key (following revision chains if a newer same-key record
            # replaced them). A failed collection never silently reuses older rows.
            records = self._records_from_database(context_type, stock_request)
            if records:
                warnings.append("served_from_store: identical request already collected; records read back from SQLite")
            else:
                warnings.append("served_from_store: identical request already collected but its records could not be located")

        observations = self._observations(records, context_type)
        unknown_date = self._unknown_date_observations(records, context_type, observations, errors, warnings)

        result = self._build_result(request, entry, identity, observations, stock_response, records, warnings, unknown_date)
        status = self._status(result, errors)
        return MarketContextResponse(
            request_id=request.request_id,
            status=status,
            created_at=created_at,
            completed_at=now_asia_shanghai(),
            timezone=self.config.storage.timezone,
            request=request,
            result=result,
            errors=errors,
            warnings=warnings,
            stock_data_response=self._trim_stock_response(stock_response),
        )

    # ------------------------------------------------------------------
    # Request construction
    # ------------------------------------------------------------------
    def _providers(self, request: MarketContextRequest, context_type: str) -> tuple[list[str], str]:
        request_type = "index_data" if context_type == ContextType.equity_index else "market_context"
        if request.providers:
            from stock_data_ingestion.config import parse_provider_list

            providers = [p for p in parse_provider_list(request.providers) if self.config.data_sources.provider_is_enabled(p)]
        else:
            providers = self.config.data_sources.providers_for_request(request_type)
        canonical = request.canonical_provider or self.config.data_sources.canonical_for_request(request_type)
        if providers and canonical not in providers:
            canonical = providers[0]
        return providers, canonical

    def _symbol_entry(self, context_type: str, symbol: str, canonical: str, providers: list[str]) -> dict[str, Any] | None:
        for provider in [canonical, *providers]:
            entry = self.config.market_context.symbol_entry(provider, context_type, symbol)
            if entry is not None:
                return entry
        return None

    def _configured_identity(self, context_type: str, entry: dict[str, Any]) -> dict[str, Any]:
        """Business identity bound to the symbol by the tool's configuration (no request input).

        Missing keys stay ``None``: the tool then cannot vouch for that part of the identity.
        """
        if context_type in {ContextType.equity_index, ContextType.hk_index}:
            return {"market": entry.get("market"), "exchange": entry.get("exchange"), "currency": entry.get("currency")}
        if context_type == ContextType.fx:
            source = self._fx_source()
            return {
                "base_currency": (str(entry["base_currency"]).upper() if entry.get("base_currency") else None),
                "quote_currency": (str(entry["quote_currency"]).upper() if entry.get("quote_currency") else None),
                "quote_basis": _f(entry.get("quote_basis")) or _f(source.get("quote_basis")),
                "market": entry.get("market") or source.get("market"),
            }
        if context_type == ContextType.commodity:
            return {
                "commodity": entry.get("commodity"),
                "instrument_type": entry.get("instrument_type"),
                "market": entry.get("market"),
                "contract": entry.get("contract"),
                "price_unit": entry.get("price_unit"),
                "currency": entry.get("currency"),
            }
        if context_type == ContextType.interest_rate:
            return {
                "rate_type": entry.get("rate_type"),
                "market": entry.get("market"),
                "tenor": entry.get("tenor"),
                "currency": entry.get("currency"),
            }
        return {}

    def _validate_business_params(self, request: MarketContextRequest, entry: dict[str, Any]) -> ErrorRecord | None:
        """Request parameters may only agree with the identity bound to the symbol.

        * not provided            -> configured value is used;
        * provided and identical  -> allowed;
        * provided but different  -> INVALID_REQUEST (not retryable);
        * provided but the configuration does not define it -> INVALID_REQUEST.
        """
        context_type = str(request.context_type)
        configured = self._configured_identity(context_type, entry)
        if context_type in {ContextType.equity_index, ContextType.hk_index}:
            fields = ("market",)
        elif context_type == ContextType.fx:
            fields = ("market",)
        elif context_type == ContextType.commodity:
            fields = ("market", "contract", "instrument_type")
        elif context_type == ContextType.interest_rate:
            fields = ("market", "tenor", "rate_type")
        else:
            fields = ()
        problems: list[str] = []
        for field in fields:
            requested = getattr(request, field, None)
            if requested is None or str(requested).strip() == "":
                continue
            requested = str(requested).strip()
            bound = configured.get(field)
            if bound is None:
                problems.append(
                    f"symbol={request.symbol} 的 {field} 未在 market_context_sources.yaml 中声明，"
                    f"无法校验请求 {field}={requested}。请补充配置或去掉该参数。"
                )
            elif str(bound).strip().lower() != requested.lower():
                problems.append(
                    f"symbol={request.symbol} 的 {field} 配置为 {bound}，请求 {field}={requested}，与 symbol 绑定不一致。"
                    f"请使用已注册的 {requested} symbol。"
                )
        if not problems:
            return None
        return ErrorRecord(
            error_code=ErrorCode.INVALID_REQUEST,
            error_message=" ".join(problems),
            retryable=False,
            suggested_action=(
                "symbol 决定取数对象；market/contract/instrument_type/tenor/rate_type 只能与配置一致。"
                "如需其他合约或期限，先在 config/market_context_sources.yaml 注册对应 symbol，再请求该 symbol。"
            ),
        )

    def _identity(self, request: MarketContextRequest, entry: dict[str, Any]) -> dict[str, Any]:
        """Business identity used for DB look-ups, idempotency and the result (configuration only)."""
        context_type = str(request.context_type)
        symbol = request.symbol
        configured = self._configured_identity(context_type, entry)
        if context_type in {ContextType.equity_index, ContextType.hk_index}:
            code = symbol.upper().replace(".SH", "").replace(".SZ", "")
            return {"index_code": code, "name": entry.get("name"), **configured, "frequency": "1d"}
        if context_type == ContextType.fx:
            base, quote, basis = configured["base_currency"], configured["quote_currency"], configured["quote_basis"]
            return {
                **configured,
                "pair": f"{base}{quote}" if base and quote else None,
                "direction": f"{quote} per {basis:g} {base}" if base and quote and basis else None,
                "name": entry.get("name"),
            }
        if context_type == ContextType.commodity:
            return {**configured, "name": entry.get("name"), "frequency": str(request.frequency)}
        if context_type == ContextType.interest_rate:
            return {**configured, "name": entry.get("name"), "unit": "percent"}
        return {"name": entry.get("name")}

    def _fx_source(self) -> dict[str, Any]:
        """Quote basis and market are properties of the vendor table (BOC: CNY per 100 units)."""
        for provider in self.config.market_context.providers:
            for source in self.config.market_context.section(provider, ContextType.fx).get("sources") or []:
                if source.get("quote_basis") or source.get("market"):
                    return source
        return {}

    # ------------------------------------------------------------------
    # Records -> observations
    # ------------------------------------------------------------------
    def _records_from_response(self, response: StockDataResponse, context_type: str) -> list[dict[str, Any]]:
        bucket = _BUCKET_BY_TYPE.get(context_type)
        return list(getattr(response.data, bucket, [])) if bucket else []

    def _db_identity(self, context_type: str, identity: dict[str, Any]) -> dict[str, Any]:
        if context_type in {ContextType.equity_index, ContextType.hk_index}:
            return {"index_code": identity["index_code"], "frequency": "1d"}
        if context_type == ContextType.fx:
            return {k: identity[k] for k in ("base_currency", "quote_currency", "quote_basis")}
        if context_type == ContextType.commodity:
            return {k: identity[k] for k in ("commodity", "instrument_type", "market", "contract", "frequency")}
        if context_type == ContextType.interest_rate:
            return {k: identity[k] for k in ("rate_type", "market", "tenor")}
        return {}

    def _records_from_database(self, context_type: str, stock_request: StockDataRequest) -> list[dict[str, Any]]:
        """Records of the run that originally succeeded under this idempotency key.

        The association is by request id (via the stored ingestion request for the key);
        when a newer same-key record has since replaced one of those rows, the revision
        chain leads to the current row. Nothing is re-interpreted or re-dated here.
        """
        from stock_data_ingestion.services.query_service import QueryService
        from stock_data_ingestion.storage.repositories import Repository

        try:
            with self.runner.database.session() as session:  # type: ignore[union-attr]
                original = Repository(session).get_successful_request_by_idempotency_key(stock_request.idempotency_key or "")
                if original is None:
                    return []
                return QueryService(session).get_market_context_records_for_request(context_type, str(original.request_id))
        except Exception:  # noqa: BLE001 - read-back is best effort; the run errors are already reported
            return []

    def _unknown_date_observations(
        self,
        records: list[dict[str, Any]],
        context_type: str,
        observations: list[MarketContextObservation],
        errors: list[ErrorRecord],
        warnings: list[str],
    ) -> bool:
        """Realtime commodity rows whose date was never confirmed are not dated observations.

        Fresh collections never store such rows (the runner reports
        ``SNAPSHOT_DATE_UNCONFIRMED``); legacy rows read back from SQLite without a
        ``date_confidence`` are treated the same way rather than trusted as vendor dates.
        """
        if context_type != ContextType.commodity:
            return False
        unconfirmed = [
            r for r in records
            if str(r.get("frequency")) == ContextFrequency.realtime and r.get("date_confidence") not in {"vendor_timestamp", "confirmed_last_session"}
        ]
        if unconfirmed:
            ids = {str(r.get("record_id")) for r in unconfirmed}
            observations[:] = [o for o in observations if o.record_id not in ids]
            warnings.append(
                f"snapshot_date_unconfirmed: {len(unconfirmed)} stored realtime record(s) carry no date confirmation "
                f"(date_confidence={sorted({str(r.get('date_confidence')) for r in unconfirmed})}); treated as unknown date"
            )
            if not any(e.error_code == ErrorCode.SNAPSHOT_DATE_UNCONFIRMED for e in errors):
                errors.append(
                    ErrorRecord(
                        error_code=ErrorCode.SNAPSHOT_DATE_UNCONFIRMED,
                        error_message="stored realtime snapshot has no confirmed data date (legacy record without date_confidence)",
                        retryable=True,
                        suggested_action="Re-collect the snapshot; the runner confirms the session date against daily bars before storing.",
                    )
                )
            return True
        return any(e.error_code == ErrorCode.SNAPSHOT_DATE_UNCONFIRMED for e in errors)

    def _observations(self, records: Iterable[dict[str, Any]], context_type: str) -> list[MarketContextObservation]:
        date_field = _DATE_FIELD_BY_TYPE[context_type]
        grouped: dict[tuple[date, str], dict[str, Any]] = {}
        for record in records:
            data_date = _d(record.get(date_field))
            if data_date is None:
                continue
            observed_at = _dt(record.get("observed_at")) if context_type == ContextType.commodity else None
            key = (data_date, observed_at.isoformat() if observed_at else "")
            obs = grouped.setdefault(
                key,
                {
                    "data_date": data_date,
                    "observed_at": observed_at,
                    "values": {},
                    "record_ids": [],
                    "providers": set(),
                    "source_apis": set(),
                    "raw_payload_ids": set(),
                    "raw_payload_refs": set(),
                    "raw_row_index": None,
                    "quality": [],
                    "validation_status": set(),
                },
            )
            if context_type == ContextType.fx:
                obs["values"][str(record.get("rate_type"))] = _f(record.get("rate"))
            else:
                for field in _VALUE_FIELDS_BY_TYPE.get(context_type, []):
                    value = _f(record.get(field))
                    if value is not None or field not in obs["values"]:
                        obs["values"][field] = value
            if record.get("record_id"):
                obs["record_ids"].append(str(record["record_id"]))
            for name, bucket in (
                ("effective_provider", "providers"),
                ("source_api", "source_apis"),
                ("raw_payload_id", "raw_payload_ids"),
                ("raw_payload_ref", "raw_payload_refs"),
                ("validation_status", "validation_status"),
            ):
                if record.get(name):
                    obs[bucket].add(str(record[name]))
            if obs["raw_row_index"] is None and record.get("raw_row_index") is not None:
                obs["raw_row_index"] = int(record["raw_row_index"])
            if record.get("data_quality") is not None:
                obs["quality"].append(float(record["data_quality"]))

        observations: list[MarketContextObservation] = []
        for key in sorted(grouped):
            obs = grouped[key]
            observations.append(
                MarketContextObservation(
                    data_date=obs["data_date"],
                    observed_at=obs["observed_at"],
                    values=obs["values"],
                    record_id=obs["record_ids"][0] if obs["record_ids"] else None,
                    provider=sorted(obs["providers"])[0] if obs["providers"] else None,
                    source_api=sorted(obs["source_apis"])[0] if obs["source_apis"] else None,
                    raw_payload_id=sorted(obs["raw_payload_ids"])[0] if obs["raw_payload_ids"] else None,
                    raw_payload_ref=sorted(obs["raw_payload_refs"])[0] if obs["raw_payload_refs"] else None,
                    raw_row_index=obs["raw_row_index"],
                    data_quality=(sum(obs["quality"]) / len(obs["quality"])) if obs["quality"] else None,
                    # An observation may combine multiple FX quote records. A blocking
                    # status must survive aggregation, regardless of alphabetical order.
                    validation_status=next(iter(sorted(obs["validation_status"] & _BLOCKING_VALIDATION_STATUSES)), None)
                    or next(iter(sorted(obs["validation_status"])), None),
                )
            )
        return observations

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------
    def _headline_metric(self, request: MarketContextRequest) -> str:
        if request.metrics:
            return request.metrics[0]
        if str(request.frequency) == ContextFrequency.realtime:
            return REALTIME_HEADLINE_METRIC.get(str(request.context_type), DEFAULT_HEADLINE_METRIC[str(request.context_type)])
        return DEFAULT_HEADLINE_METRIC[str(request.context_type)]

    def _unit(self, context_type: str, identity: dict[str, Any]) -> str:
        if context_type == ContextType.fx:
            return identity.get("direction") or f"{identity.get('quote_currency')} per {identity.get('quote_basis'):g} {identity.get('base_currency')}"
        if context_type == ContextType.commodity:
            return str(identity.get("price_unit") or "CNY")
        return _UNIT_BY_TYPE.get(context_type, "")

    def _build_result(
        self,
        request: MarketContextRequest,
        entry: dict[str, Any],
        identity: dict[str, Any],
        observations: list[MarketContextObservation],
        stock_response: StockDataResponse,
        records: list[dict[str, Any]],
        warnings: list[str],
        unknown_date: bool,
    ) -> MarketContextResult:
        context_type = str(request.context_type)
        as_of = request.effective_as_of()
        metric = self._headline_metric(request)
        realtime = str(request.frequency) == ContextFrequency.realtime
        quality = MarketContextQuality(max_staleness_days=request.effective_max_staleness_days())
        quality.warnings = []

        # Observations that may answer the request: never anything dated after as_of.
        eligible = [o for o in observations if o.data_date <= as_of]
        if request.mode == "history":
            eligible = [o for o in eligible if request.start_date and o.data_date >= request.start_date]
        later = [o for o in observations if o.data_date > as_of]
        if later:
            quality.warnings.append(f"{len(later)} observation(s) dated after as_of={as_of.isoformat()} were excluded")

        head = eligible[-1] if eligible else None
        # Dates come from the stored standard records as confirmed by the runner; the
        # service never re-derives them.
        data_date = head.data_date if head else None
        observed_at = head.observed_at if head else None
        if head is not None and realtime and context_type == ContextType.commodity:
            confidence = next((r.get("date_confidence") for r in records if r.get("record_id") == head.record_id), None)
            if confidence:
                quality.warnings.append(f"snapshot_date_confidence={confidence}")

        values = dict(head.values) if head else {}
        value = values.get(metric) if head else None
        requested_missing = [m for m in (request.metrics or [metric]) if values.get(m) is None]
        quality.missing_fields = requested_missing
        quality.observations = len(eligible)

        # Freshness is a property of the data date vs the caller's as_of, nothing else.
        if data_date is not None:
            quality.staleness_days = (as_of - data_date).days
            quality.data_date_matches_as_of = data_date == as_of
            quality.is_fresh = quality.staleness_days <= quality.max_staleness_days
        anomalies = self._anomalies(context_type, head, eligible, realtime)
        critical = [a for a in anomalies if a.get("severity") == "critical"]
        quality.anomalies = anomalies
        blocked = head is not None and head.validation_status in _BLOCKING_VALIDATION_STATUSES
        if blocked:
            quality.warnings.append(f"upstream_validation_blocked: {head.validation_status}; value retained for inspection only")
        quality.usable = head is not None and value is not None and not critical and not blocked
        if head is None and unknown_date:
            # Snapshot exists in raw form but its calendar date could not be confirmed:
            # no data date, no freshness verdict, not usable.
            quality.status = "unknown_date"
            quality.is_fresh = None
            quality.staleness_days = None
            quality.data_date_matches_as_of = None
        elif head is None:
            quality.status = "missing" if stock_response.status != "failed" or records else "failed"
        elif not quality.usable:
            quality.status = "failed"
        else:
            quality.status = "fresh" if quality.is_fresh else "stale"
        if quality.status == "stale":
            quality.warnings.append(
                f"stale: latest data_date {data_date.isoformat()} is {quality.staleness_days}d before as_of {as_of.isoformat()} "
                f"(tolerance {quality.max_staleness_days}d)"
            )
        if head is None and observations:
            quality.warnings.append("no observation on or before as_of")

        providers_seen = {r.get("effective_provider") for r in records if r.get("effective_provider")}
        quality.single_source = len(providers_seen) <= 1
        quality.cross_validated = bool(stock_response.provider_comparisons)
        quality.conflicts = [c if isinstance(c, dict) else c.model_dump(mode="json") for c in stock_response.quality_report.conflicts]
        if quality.single_source and head is not None:
            quality.warnings.append("single_source: value not cross-checked against a second provider")
        scores = [float(r["data_quality"]) for r in records if r.get("data_quality") is not None]
        quality.data_quality_score = sum(scores) / len(scores) if scores else None

        daily_bars = self._daily_bars_for_realtime(request, identity, head) if (realtime and head is not None and context_type == ContextType.commodity) else []
        changes = self._changes(context_type, metric, eligible, head, realtime, daily_bars)

        source = self._source(stock_response, head, context_type, entry)
        if head is not None and (source.source_site is None or source.adapter_version is None):
            # Served from store: provider_results are empty, take site/version from the records.
            for r in records:
                if head.record_id and r.get("record_id") == head.record_id:
                    source.source_site = source.source_site or r.get("source_site")
                    source.adapter_version = source.adapter_version or r.get("adapter_version")
                    break
        provenance = MarketContextProvenance(
            stock_data_request_id=stock_response.request_id,
            ingestion_run_ids=sorted({str(r["ingestion_run_id"]) for r in records if r.get("ingestion_run_id")}),
            idempotency_key=(stock_response.request.idempotency_key if isinstance(stock_response.request, StockDataRequest) else None),
            record_ids=[str(r["record_id"]) for r in records if r.get("record_id")],
            raw_payload_ids=sorted({*stock_response.persistence.raw_payload_ids, *{str(r["raw_payload_id"]) for r in records if r.get("raw_payload_id")}}),
            raw_payload_refs=sorted({*stock_response.persistence.raw_payload_refs, *{str(r["raw_payload_ref"]) for r in records if r.get("raw_payload_ref")}}),
            tables_written=list(stock_response.persistence.tables_written),
            parquet_refs=list(stock_response.persistence.parquet_refs),
        )
        series = eligible if request.mode == "history" else eligible[-(max(CHANGE_PERIODS) + 1):]
        return MarketContextResult(
            context_id=request.context_id,
            context_type=context_type,
            symbol=request.symbol,
            name=entry.get("name") or identity.get("name"),
            identity={k: v for k, v in identity.items() if k != "name"},
            request_window={
                "mode": request.mode,
                "as_of": as_of.isoformat(),
                "start_date": request.start_date.isoformat() if request.start_date else None,
                "end_date": request.end_date.isoformat() if request.end_date else None,
                "frequency": str(request.frequency),
                "lookback_days": None if request.mode == "history" or realtime else self.config.market_context.latest_lookback_days,
            },
            data_date=data_date,
            observed_at=observed_at,
            collected_at=stock_response.completed_at,
            metric=metric,
            value=value,
            unit=self._unit(context_type, identity),
            values=values,
            changes=changes,
            series=series,
            source=source,
            quality=quality,
            provenance=provenance,
        )

    def _daily_bars_for_realtime(
        self, request: MarketContextRequest, identity: dict[str, Any], head: MarketContextObservation
    ) -> list[MarketContextObservation]:
        """Stored daily bars of the same instrument, for N-session changes of a snapshot.

        Read-only: the bars were collected (and the snapshot date confirmed against them) by
        the runner. No vendor call, no re-dating.
        """
        if self.runner.database is None:
            return []
        from stock_data_ingestion.services.query_service import QueryService

        as_of = request.effective_as_of()
        try:
            with self.runner.database.session() as session:
                rows = QueryService(session).get_market_context_records(
                    ContextType.commodity,
                    {**self._db_identity(ContextType.commodity, identity), "frequency": "1d"},
                    as_of - timedelta(days=self.config.market_context.latest_lookback_days),
                    as_of,
                )
        except Exception:  # noqa: BLE001
            return []
        return self._observations(rows, ContextType.commodity)

    def _anomalies(self, context_type: str, head: MarketContextObservation | None, eligible: list[MarketContextObservation], realtime: bool) -> list[dict[str, Any]]:
        if head is None:
            return []
        out: list[dict[str, Any]] = []
        v = head.values
        if context_type in {ContextType.equity_index, ContextType.hk_index, ContextType.commodity}:
            high, low = v.get("high"), v.get("low")
            if high is not None and low is not None and high < low:
                out.append({"code": "high_below_low", "severity": "critical", "high": high, "low": low})
            px = v.get("latest") if realtime else v.get("close")
            if px is not None and high is not None and low is not None and not (low - 1e-9 <= px <= high + 1e-9):
                out.append({"code": "price_outside_range", "severity": "warning", "price": px, "high": high, "low": low})
            if px is not None and px <= 0:
                out.append({"code": "non_positive_price", "severity": "critical", "price": px})
        if context_type == ContextType.fx:
            for k, rate in v.items():
                if rate is not None and rate <= 0:
                    out.append({"code": "non_positive_rate", "severity": "critical", "rate_type": k, "rate": rate})
        metric_default = REALTIME_HEADLINE_METRIC.get(context_type) if realtime else None
        metric = metric_default or DEFAULT_HEADLINE_METRIC[context_type]
        if not realtime and len(eligible) >= 2:
            cur, prev = eligible[-1].values.get(metric), eligible[-2].values.get(metric)
            if cur is not None and prev is not None:
                threshold = _LARGE_MOVE_THRESHOLD[context_type]
                if context_type == ContextType.interest_rate:
                    move = abs(cur - prev)
                    if move > threshold:
                        out.append({"code": "large_move", "severity": "warning", "metric": metric, "percentage_points": move, "threshold": threshold})
                elif prev != 0:
                    move = abs(cur / prev - 1)
                    if move > threshold:
                        out.append({"code": "large_move", "severity": "warning", "metric": metric, "pct": move, "threshold": threshold})
        return out

    def _changes(
        self,
        context_type: str,
        metric: str,
        eligible: list[MarketContextObservation],
        head: MarketContextObservation | None,
        realtime: bool,
        daily_bars: list[MarketContextObservation],
    ) -> dict[str, ChangeMetric]:
        kind = "percentage_point" if context_type == ContextType.interest_rate else "percent"
        unit = _PERIOD_UNIT_BY_TYPE[context_type]
        changes: dict[str, ChangeMetric] = {}
        if head is None:
            return changes

        def _make(periods: int, from_obs: MarketContextObservation | None, to_obs: MarketContextObservation, from_value: float | None, to_value: float | None, period_unit: str, reason: str | None = None) -> ChangeMetric:
            blocked_endpoints = [o for o in (from_obs, to_obs) if o is not None and o.validation_status in _BLOCKING_VALIDATION_STATUSES]
            if blocked_endpoints:
                reason = "upstream_validation_blocked: " + ", ".join(
                    f"{o.data_date.isoformat()}={o.validation_status}" for o in blocked_endpoints
                )
            cm = ChangeMetric(periods=periods, period_unit=period_unit, kind=kind, from_date=from_obs.data_date if from_obs else None, to_date=to_obs.data_date, from_value=from_value, to_value=to_value, reason=reason)
            if reason is None and from_value is not None and to_value is not None:
                if kind == "percentage_point":
                    cm.value = to_value - from_value
                    cm.basis_points = cm.value * 100.0
                elif from_value != 0:
                    cm.value = (to_value / from_value - 1.0) * 100.0
                else:
                    cm.reason = "zero_base_value"
            return cm

        if realtime:
            prev_key = "pre_settle" if head.values.get("pre_settle") is not None else "pre_close"
            prev = head.values.get(prev_key)
            cur = head.values.get(metric)
            if cur is not None and prev is not None:
                cm = _make(1, None, head, prev, cur, f"snapshot_vs_{prev_key}")
                cm.from_date = None
                changes[f"snapshot_vs_{prev_key}"] = cm
            # Snapshot change over N sessions needs stored daily bars dated on/before the
            # snapshot's confirmed data date; otherwise the horizon is explicitly missing.
            bars = [b for b in daily_bars if b.data_date <= head.data_date]
            for n in CHANGE_PERIODS:
                ref: MarketContextObservation | None = None
                if bars:
                    if bars[-1].data_date == head.data_date:
                        # Snapshot belongs to the last bar's session: n sessions back = bars[-(n+1)].
                        ref = bars[-(n + 1)] if len(bars) > n else None
                    else:
                        # Vendor-dated snapshot after the last bar: the last bar is one session back.
                        ref = bars[-n] if len(bars) >= n else None
                if ref is not None and ref.values.get("close") is not None and cur is not None:
                    changes[f"{n}p"] = _make(n, ref, head, ref.values.get("close"), cur, "trading_day")
                    continue
                changes[f"{n}p"] = _make(n, None, head, None, cur, "trading_day", reason="realtime_only_snapshot: no confirmed daily history for this horizon")
            return changes

        for n in CHANGE_PERIODS:
            if len(eligible) > n:
                ref = eligible[-(n + 1)]
                changes[f"{n}p"] = _make(n, ref, head, ref.values.get(metric), head.values.get(metric), unit)
                if ref.values.get(metric) is None or head.values.get(metric) is None:
                    changes[f"{n}p"].reason = f"missing_{metric}_at_endpoint"
            else:
                changes[f"{n}p"] = _make(n, None, head, None, head.values.get(metric), unit, reason=f"insufficient_history: need {n + 1} observations, have {len(eligible)}")
        return changes

    def _source(self, stock_response: StockDataResponse, head: MarketContextObservation | None, context_type: str, entry: dict[str, Any]) -> MarketContextSource:
        attempted = [
            {
                "provider": r.provider,
                "source_api": r.source_api,
                "status": str(r.status),
                "rows_fetched": r.rows_fetched,
                "error": r.error.error_message if r.error else None,
            }
            for r in stock_response.provider_results
        ]
        provider = head.provider if head else None
        source_api = head.source_api if head else None
        source_site = None
        adapter_version = None
        for r in stock_response.provider_results:
            if provider is None or r.provider == provider:
                source_site = r.source_site
                adapter_version = r.adapter_version
                if source_api is None:
                    source_api = r.source_api
                break
        source_url = self._source_url(provider, context_type, source_api, entry)
        return MarketContextSource(
            provider=provider,
            source_api=source_api,
            source_site=source_site,
            source_url=source_url,
            adapter_version=adapter_version,
            providers_attempted=attempted,
        )

    def _source_url(self, provider: str | None, context_type: str, source_api: str | None, entry: dict[str, Any]) -> str | None:
        if not provider or not source_api:
            return None
        section = self.config.market_context.section(provider, context_type)
        funcs = {str(s.get("func")): s for s in section.get("sources") or []}
        for part in str(source_api).replace("|", "+").split("+"):
            src = funcs.get(part.strip())
            if src and src.get("source_url"):
                return str(src["source_url"])
        if context_type == ContextType.equity_index:
            if "stock_zh_index_daily_em" in source_api:
                return "https://quote.eastmoney.com/center/hszs.html"
            if "stock_zh_index_daily" in source_api:
                return "https://finance.sina.com.cn/stock/"
        return None

    def _status(self, result: MarketContextResult, errors: list[ErrorRecord]) -> str:
        if not result.quality.usable:
            return "failed"
        blocking = [e for e in errors if e.error_code not in {ErrorCode.EMPTY_RESULT}]
        if result.quality.status == "fresh" and not blocking:
            return "success"
        return "partial_success"

    def _failed(self, request: MarketContextRequest, created_at: datetime, errors: list[ErrorRecord], warnings: list[str], entry: dict[str, Any]) -> MarketContextResponse:
        quality = MarketContextQuality(status="failed", usable=False, max_staleness_days=request.effective_max_staleness_days())
        result = MarketContextResult(
            context_id=request.context_id,
            context_type=str(request.context_type),
            symbol=request.symbol,
            name=entry.get("name"),
            request_window={"mode": request.mode, "as_of": request.effective_as_of().isoformat(), "frequency": str(request.frequency)},
            quality=quality,
        )
        return MarketContextResponse(
            request_id=request.request_id,
            status="failed",
            created_at=created_at,
            completed_at=now_asia_shanghai(),
            timezone=self.config.storage.timezone,
            request=request,
            result=result,
            errors=errors,
            warnings=warnings,
        )

    def _trim_stock_response(self, response: StockDataResponse) -> dict[str, Any]:
        dumped = response.model_dump(mode="json", exclude={"data", "request"})
        # Raw rows are retained in the raw store; don't echo them through the result.
        for pr in dumped.get("provider_results", []):
            pr.pop("raw_records", None)
        dumped["request_id"] = response.request_id
        dumped["records_returned"] = {
            bucket: len(getattr(response.data, bucket)) for bucket in ("index_bars", "fx_rates", "commodity_prices", "interest_rates") if getattr(response.data, bucket)
        }
        return dumped


__all__ = ["MarketContextService"]
