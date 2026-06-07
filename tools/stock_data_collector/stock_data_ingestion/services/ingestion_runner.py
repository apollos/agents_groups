from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from typing import Any, Callable, Iterable, Sequence
from uuid import uuid4

from pydantic import BaseModel

from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter
from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.adapters.joinquant_adapter import JoinQuantAdapter
from stock_data_ingestion.adapters.tushare_adapter import TushareAdapter
from stock_data_ingestion.config import AppConfig, KNOWN_PROVIDERS, parse_provider_list
from stock_data_ingestion.normalization.datetime_utils import (
    build_quote_time_bucket,
    infer_bar_start_end,
    normalize_timestamp,
    normalize_trade_date,
    now_asia_shanghai,
)
from stock_data_ingestion.normalization.ticker import infer_exchange, normalize_ticker
from stock_data_ingestion.normalization.units import compute_vwap, normalize_amount, normalize_currency, normalize_turnover_rate, normalize_volume
from stock_data_ingestion.schemas.errors import ErrorCode, ErrorRecord
from stock_data_ingestion.schemas.quality import MergeMethod, QualityReport, SourceRole, ValidationStatus
from stock_data_ingestion.schemas.records import (
    AdjFactorRecord,
    AdapterFetchStatus,
    BarRecord,
    ConceptMembershipRecord,
    CorporateActionRecord,
    FinancialIndicatorRecord,
    FinancialStatementRecord,
    IndexBarRecord,
    IndexConstituentRecord,
    IndexRecord,
    IndustryMembershipRecord,
    MoneyFlowRecord,
    ProviderComparisonResult,
    ProviderFetchResult,
    RawPayloadIndexRecord,
    RealtimeQuoteRecord,
    SecurityMasterRecord,
    StandardRecord,
    TradeCalendarRecord,
    TradingStatusRecord,
    ValuationMetricRecord,
)
from stock_data_ingestion.schemas.requests import RequestType, StockDataRequest
from stock_data_ingestion.schemas.responses import ResponseStatus, StockDataResponse
from stock_data_ingestion.storage.parquet_store import ParquetStore
from stock_data_ingestion.storage.raw_object_store import RawObjectStore
from stock_data_ingestion.utils.hashing import sha256_json
from stock_data_ingestion.validation.comparison import DEFAULT_BAR_FIELDS, build_comparison_key, compare_standard_records
from stock_data_ingestion.validation.merge_policy import apply_canonical_merge_policy, mark_provider_specific_append
from stock_data_ingestion.validation.quality_score import score_record

PROVIDER_SPECIFIC_REQUEST_TYPES = {RequestType.industry_concept, RequestType.money_flow}
PROVIDER_SPECIFIC_RECORD_TYPES = {"industry_membership", "concept_membership", "money_flow"}

COMPARISON_FIELDS_BY_RECORD_TYPE: dict[str, list[str]] = {
    "security_master": ["name", "company_full_name", "list_status", "industry", "total_share", "float_share"],
    "trade_calendar": ["is_open", "prev_trade_date", "next_trade_date", "trading_sessions", "lunch_break"],
    "trading_status": [
        "is_trading",
        "is_suspended",
        "is_st",
        "is_star_st",
        "has_delisting_risk",
        "list_status",
        "limit_up_price",
        "limit_down_price",
        "hit_limit_up",
        "hit_limit_down",
        "tradability_status",
    ],
    "bar": DEFAULT_BAR_FIELDS,
    "realtime_quote": [
        "latest_price",
        "open",
        "high",
        "low",
        "pre_close",
        "change",
        "pct_change",
        "volume",
        "amount",
        "bid1_price",
        "ask1_price",
        "limit_up_price",
        "limit_down_price",
    ],
    "adj_factor": ["adj_factor"],
    "financial_statement": [
        "operating_revenue",
        "operating_profit",
        "net_profit",
        "parent_net_profit",
        "total_assets",
        "total_liabilities",
        "parent_equity",
        "operating_cash_flow",
    ],
    "financial_indicator": [
        "roe",
        "roa",
        "gross_margin",
        "net_margin",
        "revenue_yoy",
        "net_profit_yoy",
        "debt_asset_ratio",
        "current_ratio",
        "ocf_to_net_profit",
        "eps",
        "bps",
    ],
    "valuation_metric": [
        "pe",
        "pe_ttm",
        "pb",
        "ps",
        "ps_ttm",
        "dividend_yield",
        "total_market_value",
        "float_market_value",
        "turnover_rate",
        "volume_ratio",
        "amount",
    ],
    "index": ["index_name", "exchange", "list_date", "index_provider"],
    "index_bar": ["open", "high", "low", "close", "pre_close", "change", "pct_change", "volume", "amount"],
    "index_constituent": ["weight", "end_date"],
    "corporate_action": [
        "action_type",
        "announcement_date",
        "record_date",
        "ex_date",
        "dividend_payment_date",
        "cash_dividend_per_share",
        "stock_bonus_ratio",
        "rights_issue_ratio",
        "rights_issue_price",
    ],
}

REQUIRED_FIELDS_BY_RECORD_TYPE: dict[str, list[str]] = {
    "security_master": ["normalized_ticker", "name", "exchange", "list_status"],
    "trade_calendar": ["exchange", "calendar_date", "is_open"],
    "trading_status": ["normalized_ticker", "trade_date", "is_trading", "is_suspended"],
    "bar": ["open", "high", "low", "close", "volume", "amount"],
    "realtime_quote": ["normalized_ticker", "quote_time", "latest_price"],
    "adj_factor": ["normalized_ticker", "trade_date", "adj_factor"],
    "financial_statement": ["normalized_ticker", "report_period", "statement_type", "report_type"],
    "financial_indicator": ["normalized_ticker", "report_period"],
    "valuation_metric": ["normalized_ticker", "trade_date"],
    "industry_membership": ["normalized_ticker", "industry_system", "industry_name", "source_methodology"],
    "concept_membership": ["normalized_ticker", "concept_name", "source_methodology"],
    "money_flow": ["normalized_ticker", "trade_date", "source_methodology"],
    "index": ["index_code", "index_name"],
    "index_bar": ["index_code", "trade_date", "open", "high", "low", "close"],
    "index_constituent": ["index_code", "normalized_ticker", "effective_date"],
    "corporate_action": ["normalized_ticker", "action_type"],
}

RESPONSE_BUCKET_BY_RECORD_TYPE: dict[str, str] = {
    "security_master": "securities",
    "trade_calendar": "trade_calendar",
    "trading_status": "trading_status",
    "bar": "bars",
    "realtime_quote": "realtime_quotes",
    "adj_factor": "adj_factors",
    "financial_statement": "financial_statements",
    "financial_indicator": "financial_indicators",
    "valuation_metric": "valuation_metrics",
    "industry_membership": "industry_memberships",
    "concept_membership": "concept_memberships",
    "money_flow": "money_flow",
    "index": "indices",
    "index_bar": "index_bars",
    "index_constituent": "index_constituents",
    "corporate_action": "corporate_actions",
}

PARQUET_DATA_TYPE_BY_RECORD_TYPE: dict[str, str] = {
    "security_master": "securities",
    "trade_calendar": "trade_calendar",
    "trading_status": "trading_status",
    "bar": "bars",
    "realtime_quote": "realtime_quotes",
    "adj_factor": "adj_factors",
    "financial_statement": "financial_statements",
    "financial_indicator": "financial_indicators",
    "valuation_metric": "valuation_metrics",
    "industry_membership": "industry_memberships",
    "concept_membership": "concept_memberships",
    "money_flow": "money_flow",
    "index": "indices",
    "index_bar": "index_bars",
    "index_constituent": "index_constituents",
    "corporate_action": "corporate_actions",
}


def _value(raw: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in raw and raw[name] is not None and raw[name] != "":
            return raw[name]
    return default


def _float(raw: dict[str, Any], *names: str, default: float | None = None) -> float | None:
    value = _value(raw, *names, default=None)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(raw: dict[str, Any], *names: str, default: bool = False) -> bool:
    value = _value(raw, *names, default=None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "是", "开市", "交易", "正常", "open", "tradable"}:
        return True
    if text in {"0", "false", "f", "no", "n", "否", "休市", "停牌", "closed", "suspended"}:
        return False
    return default


def _date(raw: dict[str, Any], *names: str, default: date | None = None) -> date | None:
    value = _value(raw, *names, default=None)
    if value is None:
        return default
    return normalize_trade_date(value)


def _dt(raw: dict[str, Any], *names: str, default: datetime | None = None) -> datetime | None:
    value = _value(raw, *names, default=None)
    if value is None:
        return default
    return normalize_timestamp(value)


def _clean_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


class IngestionRunner:
    def __init__(
        self,
        config: AppConfig,
        raw_store: RawObjectStore,
        database: Any | None = None,
        adapters: dict[str, BaseDataAdapter] | None = None,
        parquet_store: ParquetStore | None = None,
    ) -> None:
        self.config = config
        self.raw_store = raw_store
        self.database = database
        self.parquet_store = parquet_store or ParquetStore(config.storage.parquet_root)
        self.adapters = adapters or {
            "tushare": TushareAdapter(),
            "akshare": AKShareAdapter(),
            "joinquant": JoinQuantAdapter(),
        }

    def _request_with_configured_providers(self, request: StockDataRequest) -> StockDataRequest:
        """Resolve providers from config/.env unless the caller selected them.

        StockDataRequest defaults to all providers. Runtime provider selection is
        controlled by config/data_sources.yaml and STOCK_DATA_* env variables.
        If a caller explicitly passes provider_priority/canonical_provider, those
        values are kept but filtered through the active provider allow-list.
        """

        default_provider_settings = (
            request.provider_priority == list(KNOWN_PROVIDERS)
            and request.canonical_provider == "tushare"
        )
        request_type = str(request.request_type)
        if default_provider_settings:
            provider_priority = self.config.data_sources.providers_for_request(request_type)
            canonical = self.config.data_sources.canonical_for_request(request_type)
        else:
            provider_priority = [
                provider
                for provider in parse_provider_list(request.provider_priority)
                if self.config.data_sources.provider_is_enabled(provider)
            ]
            if not provider_priority:
                provider_priority = self.config.data_sources.providers_for_request(request_type)
            canonical = request.canonical_provider
            if canonical not in provider_priority:
                canonical = provider_priority[0] if provider_priority else self.config.data_sources.canonical_for_request(request_type)

        payload = request.model_dump()
        payload["provider_priority"] = provider_priority
        payload["canonical_provider"] = canonical
        payload["cross_validate"] = bool(request.cross_validate and len(provider_priority) > 1)
        payload["extra_params"] = {
            **dict(payload.get("extra_params") or {}),
            "resolved_provider_priority": provider_priority,
            "resolved_canonical_provider": canonical,
        }
        if default_provider_settings:
            # Regenerate idempotency key so changing providers via config/.env
            # cannot accidentally reuse a previous provider-set key.
            payload["idempotency_key"] = None
        return StockDataRequest.model_validate(payload)

    def run(self, request: StockDataRequest) -> StockDataResponse:
        request = self._request_with_configured_providers(request)
        ingestion_run_id = f"run_{now_asia_shanghai().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        created_at = now_asia_shanghai()
        provider_results: list[ProviderFetchResult] = []
        raw_indices: list[RawPayloadIndexRecord] = []
        comparisons: list[ProviderComparisonResult] = []
        conflicts: list[Any] = []
        normalized_records: list[StandardRecord] = []
        merged_records: list[StandardRecord] = []
        errors: list[ErrorRecord] = []
        warnings: list[str] = []
        persisted_tables: list[str] = []
        parquet_refs: list[str] = []

        if not request.provider_priority:
            errors.append(
                ErrorRecord(
                    error_code=ErrorCode.INVALID_REQUEST,
                    error_message="No active data providers are configured for this request type.",
                    retryable=False,
                    suggested_action="Set active_providers in config/data_sources.yaml or STOCK_DATA_ACTIVE_PROVIDERS in .env.",
                )
            )

        self._start_persistence(request, ingestion_run_id, created_at, errors)
        if self._is_idempotent_success(request, errors):
            return StockDataResponse(
                request_id=request.request_id,
                status=ResponseStatus.success,
                created_at=created_at,
                completed_at=now_asia_shanghai(),
                canonical_provider=request.canonical_provider,
                request=request,
                quality_report=QualityReport(warnings=["idempotency_key already succeeded; skipped duplicate write"]),
            )

        if request.save_cleaned and not request.save_raw:
            errors.append(
                ErrorRecord(
                    error_code=ErrorCode.INVALID_REQUEST,
                    error_message="save_cleaned=True requires save_raw=True because standard records must keep raw payload provenance.",
                    retryable=False,
                    suggested_action="Enable save_raw or disable save_cleaned for dry-run collection.",
                )
            )

        canonical_returned_rows = False
        for provider in self._provider_order(request):
            adapter = self.adapters.get(provider)
            if adapter is None:
                continue
            if not self._provider_enabled(provider):
                continue
            if provider != request.canonical_provider and not self._should_fetch_supplement(provider, request, canonical_returned_rows):
                continue

            result = self._fetch(adapter, request)
            if result.error is not None:
                errors.append(result.error)
            if result.provider == request.canonical_provider and result.rows_fetched > 0 and result.status == AdapterFetchStatus.success:
                canonical_returned_rows = True

            if request.save_raw and result.status in {AdapterFetchStatus.success, AdapterFetchStatus.empty_result}:
                result = self._save_raw_for_result(result, request, ingestion_run_id, raw_indices, errors)
            provider_results.append(result)

        if request.save_cleaned and request.save_raw:
            normalized_records = self._normalize_results(provider_results, request, ingestion_run_id, errors)
            merged_records, comparisons, conflicts, merge_warnings = self._merge_records(normalized_records, request)
            warnings.extend(merge_warnings)
            merged_records = [self._rescore_record(record, conflicts) for record in merged_records]

        if self.database is not None:
            persisted_tables.extend(
                self._persist(
                    request=request,
                    ingestion_run_id=ingestion_run_id,
                    raw_indices=raw_indices,
                    provider_results=provider_results,
                    comparisons=comparisons,
                    conflicts=conflicts,
                    records=merged_records if request.save_cleaned else [],
                    errors=errors,
                )
            )

        if request.export_parquet and request.save_cleaned and merged_records:
            parquet_refs.extend(self._export_parquet(merged_records, errors))

        status = self._response_status(provider_results, merged_records, errors)
        quality_report = self._build_quality_report(merged_records, comparisons, conflicts, warnings)
        response = StockDataResponse(
            request_id=request.request_id,
            status=status,
            created_at=created_at,
            completed_at=now_asia_shanghai(),
            timezone=self.config.storage.timezone,
            canonical_provider=request.canonical_provider,
            request=request,
            provider_results=provider_results,
            provider_comparisons=comparisons,
            quality_report=quality_report,
            errors=errors,
        )
        self._fill_response_data(response, merged_records)
        response.persistence.saved = bool(persisted_tables or parquet_refs or raw_indices)
        response.persistence.tables_written = sorted(set(persisted_tables))
        response.persistence.parquet_refs = parquet_refs
        response.persistence.raw_payload_ids = [idx.raw_payload_id for idx in raw_indices]
        response.persistence.raw_payload_refs = [idx.raw_payload_ref for idx in raw_indices]

        if self.database is not None:
            self._finish_persistence(request, ingestion_run_id, response, errors)
        return response

    def _is_idempotent_success(self, request: StockDataRequest, errors: list[ErrorRecord]) -> bool:
        if self.database is None or not request.idempotency_key:
            return False
        try:
            from stock_data_ingestion.storage.repositories import Repository

            with self.database.session() as session:
                return Repository(session).has_successful_idempotency_key(request.idempotency_key)
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error_from_exception(exc, ErrorCode.STORAGE_FAILED, "idempotency_check"))
            return False

    def _start_persistence(self, request: StockDataRequest, ingestion_run_id: str, created_at: datetime, errors: list[ErrorRecord]) -> None:
        if self.database is None:
            return
        try:
            from stock_data_ingestion.storage.repositories import Repository

            with self.database.session() as session:
                repo = Repository(session)
                repo.insert_ingestion_request(request, status="running")
                repo.insert_ingestion_run(ingestion_run_id, request.request_id, str(request.request_type), created_at)
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error_from_exception(exc, ErrorCode.STORAGE_FAILED, "persistence_start"))

    def _finish_persistence(self, request: StockDataRequest, ingestion_run_id: str, response: StockDataResponse, errors: list[ErrorRecord]) -> None:
        if self.database is None:
            return
        try:
            from stock_data_ingestion.storage.repositories import Repository

            with self.database.session() as session:
                repo = Repository(session)
                status = str(response.status)
                data_quality = response.quality_report.data_quality_score
                raw_payload_id = response.persistence.raw_payload_ids[0] if response.persistence.raw_payload_ids else None
                repo.update_ingestion_request_status(request.request_id, request.idempotency_key or "", status, raw_payload_id=raw_payload_id, data_quality=data_quality)
                repo.update_ingestion_run_status(
                    ingestion_run_id,
                    status,
                    provider_results=response.provider_results,
                    error_records=errors,
                    raw_payload_id=raw_payload_id,
                    data_quality=data_quality,
                    completed_at=response.completed_at,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error_from_exception(exc, ErrorCode.STORAGE_FAILED, "persistence_finish"))

    def _provider_order(self, request: StockDataRequest) -> list[str]:
        order = list(dict.fromkeys([request.canonical_provider, *request.provider_priority]))
        return [provider for provider in order if provider in self.adapters and self._provider_enabled(provider)]

    def _provider_enabled(self, provider: str) -> bool:
        return self.config.data_sources.provider_is_enabled(provider)

    def _should_fetch_supplement(self, provider: str, request: StockDataRequest, canonical_returned_rows: bool) -> bool:
        if RequestType(str(request.request_type)) in PROVIDER_SPECIFIC_REQUEST_TYPES:
            return True
        if request.cross_validate:
            return True
        if canonical_returned_rows:
            return False
        if not request.fallback_enabled or not self.config.data_sources.allow_fallback_when_canonical_missing:
            return False
        return provider in self.config.data_sources.supplement_providers

    def _fetch(self, adapter: BaseDataAdapter, request: StockDataRequest) -> ProviderFetchResult:
        try:
            dispatch: dict[RequestType, Callable[[StockDataRequest], ProviderFetchResult]] = {
                RequestType.security_master: adapter.fetch_security_master,
                RequestType.trade_calendar: adapter.fetch_trade_calendar,
                RequestType.trading_status: adapter.fetch_trading_status,
                RequestType.historical_bars: adapter.fetch_historical_bars,
                RequestType.realtime_quote: adapter.fetch_realtime_quote,
                RequestType.adj_factor: adapter.fetch_adj_factor,
                RequestType.financial_statement: adapter.fetch_financial_statement,
                RequestType.financial_indicator: adapter.fetch_financial_indicator,
                RequestType.valuation_metric: adapter.fetch_valuation_metric,
                RequestType.industry_concept: adapter.fetch_industry_membership,
                RequestType.money_flow: adapter.fetch_money_flow,
                RequestType.index_data: adapter.fetch_index_data,
                RequestType.corporate_action: adapter.fetch_corporate_action,
            }
            request_type = RequestType(str(request.request_type))
            return dispatch.get(request_type, adapter.fetch_historical_bars)(request)
        except Exception as exc:  # noqa: BLE001
            now = now_asia_shanghai()
            return ProviderFetchResult(
                provider=adapter.provider_name,
                source_api=str(request.request_type),
                source_site=getattr(adapter, "source_site", adapter.provider_name),
                adapter_version=getattr(adapter, "adapter_version", "0.1.0"),
                status=AdapterFetchStatus.failed,
                started_at=now,
                completed_at=now,
                error=ErrorRecord.from_exception(
                    exc,
                    provider=adapter.provider_name,
                    source_api=str(request.request_type),
                    source_site=getattr(adapter, "source_site", adapter.provider_name),
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    retryable=False,
                ),
            )

    def _save_raw_for_result(
        self,
        result: ProviderFetchResult,
        request: StockDataRequest,
        ingestion_run_id: str,
        raw_indices: list[RawPayloadIndexRecord],
        errors: list[ErrorRecord],
    ) -> ProviderFetchResult:
        try:
            raw_index = self.raw_store.save_raw_payload(
                provider=result.provider,
                request_type=str(request.request_type),
                source_api=result.source_api,
                source_site=result.source_site,
                adapter_version=result.adapter_version,
                request_id=request.request_id,
                ingestion_run_id=ingestion_run_id,
                sanitized_request_params=request.model_dump(mode="json", exclude={"created_at"}),
                raw_records=result.raw_records,
                idempotency_key=request.idempotency_key or "",
                fetch_started_at=result.started_at,
                fetch_completed_at=result.completed_at,
                timezone=self.config.storage.timezone,
            )
            raw_indices.append(raw_index)
            return result.model_copy(
                update={
                    "raw_payload_id": raw_index.raw_payload_id,
                    "raw_payload_ref": raw_index.raw_payload_ref,
                    "raw_hash": raw_index.raw_hash,
                }
            )
        except Exception as exc:  # noqa: BLE001
            error = self._error_from_exception(exc, ErrorCode.RAW_SAVE_FAILED, result.source_api, provider=result.provider, source_site=result.source_site)
            errors.append(error)
            return result.model_copy(update={"status": AdapterFetchStatus.failed, "error": error})

    def _normalize_results(
        self,
        results: Iterable[ProviderFetchResult],
        request: StockDataRequest,
        ingestion_run_id: str,
        errors: list[ErrorRecord],
    ) -> list[StandardRecord]:
        records: list[StandardRecord] = []
        for result in results:
            if result.status != AdapterFetchStatus.success:
                continue
            if not result.raw_payload_id or not result.raw_payload_ref:
                errors.append(
                    ErrorRecord(
                        provider=result.provider,
                        source_api=result.source_api,
                        source_site=result.source_site,
                        error_code=ErrorCode.RAW_SAVE_FAILED,
                        error_message="successful provider result has no raw payload reference; standardization skipped",
                        retryable=False,
                    )
                )
                continue
            for idx, raw in enumerate(result.raw_records):
                try:
                    produced = self._raw_to_standard_records(result, raw, idx, request, ingestion_run_id)
                    records.extend(produced)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        self._error_from_exception(
                            exc,
                            ErrorCode.NORMALIZATION_FAILED,
                            result.source_api,
                            provider=result.provider,
                            source_site=result.source_site,
                            suggested_action="Inspect provider schema and field mapping for this request_type.",
                        )
                    )
        return records

    def _merge_records(
        self, records: list[StandardRecord], request: StockDataRequest
    ) -> tuple[list[StandardRecord], list[ProviderComparisonResult], list[Any], list[str]]:
        if not records:
            return [], [], [], ["no records normalized"]
        if RequestType(str(request.request_type)) in PROVIDER_SPECIFIC_REQUEST_TYPES:
            result = mark_provider_specific_append(records)
            return [r for r in result.records if isinstance(r, StandardRecord)], [], result.conflicts, result.warnings

        grouped: dict[str, dict[str, list[StandardRecord]]] = defaultdict(lambda: defaultdict(list))
        for record in records:
            grouped[build_comparison_key(record)][record.provider].append(record)

        merged: list[StandardRecord] = []
        comparisons: list[ProviderComparisonResult] = []
        conflicts: list[Any] = []
        warnings: list[str] = []
        for provider_records in grouped.values():
            canonical = provider_records.get(request.canonical_provider, [None])[0]
            supplements = [rec for provider, recs in provider_records.items() if provider != request.canonical_provider for rec in recs]
            if canonical is None and not (request.fallback_enabled and self.config.data_sources.allow_fallback_when_canonical_missing):
                warnings.append("canonical_missing_fallback_disabled")
                continue
            record_type = getattr(canonical or supplements[0], "record_type") if (canonical or supplements) else "unknown"
            fields = COMPARISON_FIELDS_BY_RECORD_TYPE.get(str(record_type), [])
            result = apply_canonical_merge_policy(
                canonical,
                supplements,
                comparison_fields=fields,
                allow_majority_override_canonical=self.config.data_sources.allow_majority_override_canonical,
                quarantine_on_critical_conflict=self.config.data_sources.quarantine_on_critical_conflict,
                allow_field_level_merge=self.config.data_sources.allow_field_level_merge,
                supplement_field_whitelist=self.config.data_quality.supplement_field_whitelist,
            )
            merged.extend([r for r in result.records if isinstance(r, StandardRecord)])
            conflicts.extend(result.conflicts)
            warnings.extend(result.warnings)
            if canonical is not None:
                for supplement in supplements:
                    comparisons.append(compare_standard_records(canonical, supplement, fields))
        return merged, comparisons, conflicts, warnings

    def _persist(
        self,
        *,
        request: StockDataRequest,
        ingestion_run_id: str,
        raw_indices: Sequence[RawPayloadIndexRecord],
        provider_results: Sequence[ProviderFetchResult],
        comparisons: Sequence[ProviderComparisonResult],
        conflicts: Sequence[Any],
        records: Sequence[StandardRecord],
        errors: list[ErrorRecord],
    ) -> list[str]:
        if self.database is None:
            return []
        tables: list[str] = []
        try:
            from stock_data_ingestion.storage.repositories import Repository

            with self.database.session() as session:
                repo = Repository(session)
                for idx in raw_indices:
                    if repo.insert_raw_payload_index(idx):
                        tables.append("raw_payload_index")
                for result in provider_results:
                    if repo.insert_provider_fetch_result(result, request.request_id, ingestion_run_id):
                        tables.append("source_fetch_logs")
                for cmp in comparisons:
                    if repo.insert_provider_comparison(cmp):
                        tables.append("provider_comparisons")
                for conflict in conflicts:
                    if repo.insert_conflict(conflict):
                        tables.append("data_quality_conflicts")
                tables.extend(repo.insert_standard_records(records))
        except Exception as exc:  # noqa: BLE001
            errors.append(self._error_from_exception(exc, ErrorCode.STORAGE_FAILED, "sqlite_persistence"))
        return tables

    def _export_parquet(self, records: Sequence[StandardRecord], errors: list[ErrorRecord]) -> list[str]:
        refs: list[str] = []
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            data_type = PARQUET_DATA_TYPE_BY_RECORD_TYPE.get(record.record_type)
            if not data_type:
                continue
            grouped[data_type].append(record.model_dump(mode="json"))
        for data_type, rows in grouped.items():
            try:
                partition_cols = self._parquet_partitions(rows[0])
                refs.extend(self.parquet_store.write_records(data_type, rows, partition_cols=partition_cols))
            except Exception as exc:  # noqa: BLE001
                errors.append(self._error_from_exception(exc, ErrorCode.STORAGE_FAILED, f"parquet_export:{data_type}"))
        return refs

    def _parquet_partitions(self, row: dict[str, Any]) -> list[str]:
        record_type = row.get("record_type")
        if record_type == "bar":
            return ["frequency", "trade_date", "effective_provider"]
        if "trade_date" in row:
            return ["trade_date", "effective_provider"]
        if "report_period" in row:
            return ["report_period", "effective_provider"]
        if "effective_date" in row:
            return ["effective_date", "effective_provider"]
        return ["effective_provider"] if "effective_provider" in row else []

    def _fill_response_data(self, response: StockDataResponse, records: Sequence[StandardRecord]) -> None:
        for record in records:
            bucket = RESPONSE_BUCKET_BY_RECORD_TYPE.get(record.record_type)
            if not bucket:
                continue
            getattr(response.data, bucket).append(record.model_dump(mode="json"))

    def _response_status(
        self,
        results: Sequence[ProviderFetchResult],
        records: Sequence[StandardRecord],
        errors: Sequence[ErrorRecord],
    ) -> ResponseStatus:
        if not records:
            return ResponseStatus.failed if errors or not results else ResponseStatus.partial_success
        blocking_errors = [e for e in errors if e.error_code not in {ErrorCode.EMPTY_RESULT}]
        if blocking_errors or any(r.status in {AdapterFetchStatus.failed, AdapterFetchStatus.unavailable} for r in results):
            return ResponseStatus.partial_success
        return ResponseStatus.success

    def _build_quality_report(
        self,
        records: Sequence[StandardRecord],
        comparisons: Sequence[ProviderComparisonResult],
        conflicts: Sequence[Any],
        warnings: Sequence[str],
    ) -> QualityReport:
        if not records:
            return QualityReport(
                data_quality_score=0.0,
                completeness_score=0.0,
                consistency_score=0.0,
                timeliness_score=1.0 if comparisons else 0.0,
                provider_reliability_score=0.0,
                anomaly_score=0.0,
                provenance_score=0.0,
                cross_provider_checks=[cmp.model_dump(mode="json") for cmp in comparisons],
                conflicts=list(conflicts),
                warnings=list(warnings),
            )
        avg_quality = sum(record.data_quality for record in records) / len(records)
        provenance_ok = sum(1 for r in records if r.field_provenance and r.raw_payload_ref) / len(records)
        return QualityReport(
            data_quality_score=avg_quality,
            completeness_score=1.0,
            consistency_score=0.0 if conflicts else 1.0,
            timeliness_score=1.0,
            provider_reliability_score=avg_quality,
            anomaly_score=0.5 if conflicts else 1.0,
            provenance_score=provenance_ok,
            cross_provider_checks=[cmp.model_dump(mode="json") for cmp in comparisons],
            conflicts=list(conflicts),
            warnings=list(warnings),
        )

    def _rescore_record(self, record: StandardRecord, conflicts: Sequence[Any]) -> StandardRecord:
        record_conflicts = [c for c in conflicts if getattr(c, "canonical_record_id", None) == record.record_id or getattr(c, "conflict_id", None) in record.conflict_ids]
        q = score_record(
            provider=record.effective_provider or record.provider,
            required_fields=REQUIRED_FIELDS_BY_RECORD_TYPE.get(record.record_type, []),
            record_values=record.model_dump(mode="python"),
            field_provenance=record.field_provenance,
            raw_payload_ref=record.raw_payload_ref,
            merge_method=str(record.merge_method),
            conflicts=record_conflicts,
            provider_reliability=self.config.data_quality.provider_reliability or None,
        )
        return record.model_copy(update={"data_quality": q.data_quality_score}, deep=True)

    def _raw_to_standard_records(
        self,
        result: ProviderFetchResult,
        raw: dict[str, Any],
        raw_row_index: int,
        request: StockDataRequest,
        ingestion_run_id: str,
    ) -> list[StandardRecord]:
        dispatch: dict[RequestType, Callable[[ProviderFetchResult, dict[str, Any], int, StockDataRequest, str], list[StandardRecord]]] = {
            RequestType.security_master: self._normalize_security_master,
            RequestType.trade_calendar: self._normalize_trade_calendar,
            RequestType.trading_status: self._normalize_trading_status,
            RequestType.historical_bars: self._normalize_bar,
            RequestType.realtime_quote: self._normalize_realtime_quote,
            RequestType.adj_factor: self._normalize_adj_factor,
            RequestType.financial_statement: self._normalize_financial_statement,
            RequestType.financial_indicator: self._normalize_financial_indicator,
            RequestType.valuation_metric: self._normalize_valuation_metric,
            RequestType.industry_concept: self._normalize_industry_or_concept,
            RequestType.money_flow: self._normalize_money_flow,
            RequestType.index_data: self._normalize_index_data,
            RequestType.corporate_action: self._normalize_corporate_action,
        }
        request_type = RequestType(str(request.request_type))
        return dispatch[request_type](result, raw, raw_row_index, request, ingestion_run_id)

    def _common_record_kwargs(
        self,
        *,
        result: ProviderFetchResult,
        request: StockDataRequest,
        ingestion_run_id: str,
        raw_row_index: int,
        domain_values: dict[str, Any],
        record_type: str,
    ) -> dict[str, Any]:
        source_role = SourceRole.canonical if result.provider == request.canonical_provider else SourceRole.validator
        merge_method = MergeMethod.canonical_only if result.provider == request.canonical_provider else MergeMethod.fallback_single_source
        values = _clean_dict(domain_values)
        provenance = {
            field: {
                "provider": result.provider,
                "source_api": result.source_api,
                "source_role": str(source_role),
                "raw_payload_id": result.raw_payload_id,
                "raw_payload_ref": result.raw_payload_ref,
                "raw_row_index": raw_row_index,
            }
            for field, value in values.items()
            if value is not None and not (isinstance(value, (list, dict)) and not value)
        }
        data = {
            **values,
            "record_type": record_type,
            "provider": result.provider,
            "source_api": result.source_api,
            "source_site": result.source_site,
            "adapter_version": result.adapter_version,
            "canonical_provider": request.canonical_provider,
            "effective_provider": result.provider,
            "source_role": source_role,
            "merge_method": merge_method,
            "validation_status": ValidationStatus.unvalidated,
            "field_provenance": provenance,
            "supplement_flags": {},
            "conflict_ids": [],
            "canonical_value_suspect": False,
            "fetch_time": result.completed_at,
            "provider_update_time": None,
            "ingested_at": now_asia_shanghai(),
            "request_id": request.request_id,
            "ingestion_run_id": ingestion_run_id,
            "request_params_hash": sha256_json(request.model_dump(mode="json", exclude={"idempotency_key"})),
            "idempotency_key": request.idempotency_key or "",
            "raw_payload_id": result.raw_payload_id or "",
            "raw_payload_ref": result.raw_payload_ref or "",
            "raw_hash": result.raw_hash or "",
            "raw_format": "jsonl.gz",
            "raw_row_index": raw_row_index,
            "data_quality": 0.0,
            "quality_flags": [],
        }
        q = score_record(
            provider=result.provider,
            required_fields=REQUIRED_FIELDS_BY_RECORD_TYPE.get(record_type, []),
            record_values=data,
            field_provenance=provenance,
            raw_payload_ref=result.raw_payload_ref,
            merge_method=str(merge_method),
            provider_reliability=self.config.data_quality.provider_reliability or None,
        )
        data["data_quality"] = q.data_quality_score
        return data

    def _stock_identity(self, raw: dict[str, Any], request: StockDataRequest) -> dict[str, Any]:
        symbol = _value(raw, "normalized_ticker", "ts_code", "provider_symbol", "symbol", "code", "证券代码", "股票代码", default=None)
        if symbol is None and request.tickers:
            symbol = request.tickers[0]
        ticker = normalize_ticker(symbol)
        exchange = _value(raw, "exchange", "交易所", default=None) or ticker.split(".")[1]
        return {
            "normalized_ticker": ticker,
            "provider_symbol": str(_value(raw, "provider_symbol", "ts_code", "symbol", "code", "证券代码", "股票代码", default=ticker)),
            "exchange": str(exchange).replace("SSE", "SH").replace("SZSE", "SZ").replace("BSE", "BJ"),
            "market": _value(raw, "market", "市场", default=request.market),
            "asset_type": _value(raw, "asset_type", default="stock"),
        }

    def _normalize_security_master(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        domain = {
            **identity,
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "name": _value(raw, "name", "股票简称", "简称"),
            "company_full_name": _value(raw, "fullname", "company_full_name", "公司全称"),
            "list_date": _date(raw, "list_date", "上市日期"),
            "delist_date": _date(raw, "delist_date", "退市日期"),
            "list_status": _value(raw, "list_status", "上市状态", default="L"),
            "board": _value(raw, "board", "板块"),
            "area": _value(raw, "area", "地区"),
            "industry": _value(raw, "industry", "行业"),
            "total_share": _float(raw, "total_share", "总股本"),
            "float_share": _float(raw, "float_share", "流通股本"),
            "main_business": _value(raw, "main_business", "主营业务"),
        }
        return [SecurityMasterRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="security_master"))]

    def _normalize_trade_calendar(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        calendar_date = _date(raw, "calendar_date", "cal_date", "trade_date", "date", "日期")
        domain = {
            "exchange": _value(raw, "exchange", "交易所", default=(request.exchanges[0] if request.exchanges else "SSE")),
            "calendar_date": calendar_date,
            "is_open": _bool(raw, "is_open", "is_trade", "open", "是否开市", default=False),
            "prev_trade_date": _date(raw, "prev_trade_date", "pretrade_date", "上一交易日"),
            "next_trade_date": _date(raw, "next_trade_date", "下一交易日"),
            "trading_sessions": _value(raw, "trading_sessions", default=[{"start": "09:30", "end": "11:30"}, {"start": "13:00", "end": "15:00"}]),
            "lunch_break": _value(raw, "lunch_break", default={"start": "11:30", "end": "13:00"}),
            "timezone": _value(raw, "timezone", default=self.config.storage.timezone),
        }
        return [TradeCalendarRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="trade_calendar"))]

    def _normalize_trading_status(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        is_suspended = _bool(raw, "is_suspended", "suspended", "停牌", default=False)
        is_trading = _bool(raw, "is_trading", "交易", default=not is_suspended)
        domain = {
            **identity,
            "trade_date": _date(raw, "trade_date", "date", "日期", default=request.end_date or request.start_date),
            "timezone": _value(raw, "timezone", default=self.config.storage.timezone),
            "is_trading": is_trading,
            "is_suspended": is_suspended,
            "suspend_reason": _value(raw, "suspend_reason", "停牌原因"),
            "is_st": _bool(raw, "is_st", "ST", default=False),
            "is_star_st": _bool(raw, "is_star_st", "*ST", default=False),
            "has_delisting_risk": _bool(raw, "has_delisting_risk", "退市风险", default=False),
            "list_status": _value(raw, "list_status", "上市状态"),
            "limit_up_price": _float(raw, "limit_up_price", "涨停价"),
            "limit_down_price": _float(raw, "limit_down_price", "跌停价"),
            "hit_limit_up": _bool(raw, "hit_limit_up", "是否触及涨停", default=False),
            "hit_limit_down": _bool(raw, "hit_limit_down", "是否触及跌停", default=False),
            "tradability_status": _value(raw, "tradability_status", default="tradable" if is_trading and not is_suspended else "not_tradable"),
            "not_tradable_reason": _value(raw, "not_tradable_reason", "不可交易原因"),
        }
        return [TradingStatusRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="trading_status"))]

    def _normalize_bar(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        trade_date = _date(raw, "trade_date", "日期", "date")
        ts = _dt(raw, "timestamp", "time", "datetime", default=normalize_timestamp(trade_date))
        volume, volume_unit = self._normalize_volume_for_provider(raw, result.provider)
        amount, amount_unit = self._normalize_amount_for_provider(raw, result.provider)
        start, end = infer_bar_start_end(trade_date, str(request.frequency or "1d"), ts)
        domain = {
            **identity,
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "trade_date": trade_date,
            "timestamp": ts,
            "timezone": _value(raw, "timezone", default=self.config.storage.timezone),
            "frequency": str(request.frequency or _value(raw, "frequency", default="1d")),
            "bar_start_time": start,
            "bar_end_time": end,
            "trading_session": _value(raw, "trading_session", default="regular"),
            "is_complete": _bool(raw, "is_complete", default=True),
            "open": _float(raw, "open", "开盘"),
            "high": _float(raw, "high", "最高"),
            "low": _float(raw, "low", "最低"),
            "close": _float(raw, "close", "收盘"),
            "pre_close": _float(raw, "pre_close", "昨收"),
            "change": _float(raw, "change", "涨跌额"),
            "pct_change": _float(raw, "pct_chg", "pct_change", "涨跌幅"),
            "volume": volume,
            "volume_unit": volume_unit,
            "amount": amount,
            "amount_unit": amount_unit,
            "vwap": _float(raw, "vwap", default=compute_vwap(amount, volume)),
            "turnover_rate": normalize_turnover_rate(_float(raw, "turnover_rate", "换手率")),
            "turnover_rate_free_float": normalize_turnover_rate(_float(raw, "turnover_rate_free_float", "自由流通换手率")),
            "adjust": str(request.adjust or _value(raw, "adjust", default="none")),
            "adj_factor": _float(raw, "adj_factor"),
        }
        return [BarRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="bar"))]

    def _normalize_volume_for_provider(self, raw: dict[str, Any], provider: str) -> tuple[float | None, str]:
        if provider == "tushare":
            return normalize_volume(_value(raw, "vol", "volume", "成交量"), provider="tushare")
        if provider == "akshare":
            return normalize_volume(_value(raw, "成交量", "volume", "vol"), unit=_value(raw, "volume_unit", default="hand"))
        return normalize_volume(_value(raw, "volume", "vol", "成交量"), unit=_value(raw, "volume_unit", default="share"))

    def _normalize_amount_for_provider(self, raw: dict[str, Any], provider: str) -> tuple[float | None, str]:
        if provider == "tushare":
            return normalize_amount(_value(raw, "amount", "成交额"), provider="tushare")
        return normalize_amount(_value(raw, "amount", "money", "成交额"), unit=_value(raw, "amount_unit", default="CNY"))

    def _normalize_realtime_quote(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        quote_time = _dt(raw, "quote_time", "time", "timestamp", "报价时间", default=now_asia_shanghai())
        volume, _ = self._normalize_volume_for_provider(raw, result.provider)
        amount, _ = self._normalize_amount_for_provider(raw, result.provider)
        bid = _float(raw, "bid1_price", "买一价")
        ask = _float(raw, "ask1_price", "卖一价")
        latest = _float(raw, "latest_price", "price", "最新价", "close")
        limit_up = _float(raw, "limit_up_price", "涨停价")
        limit_down = _float(raw, "limit_down_price", "跌停价")
        domain = {
            **identity,
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "timezone": _value(raw, "timezone", default=self.config.storage.timezone),
            "quote_time": quote_time,
            "quote_time_bucket": build_quote_time_bucket(quote_time),
            "latest_price": latest,
            "open": _float(raw, "open", "开盘"),
            "high": _float(raw, "high", "最高"),
            "low": _float(raw, "low", "最低"),
            "pre_close": _float(raw, "pre_close", "昨收"),
            "change": _float(raw, "change", "涨跌额"),
            "pct_change": _float(raw, "pct_change", "涨跌幅"),
            "volume": volume,
            "amount": amount,
            "bid1_price": bid,
            "bid1_volume": _float(raw, "bid1_volume", "买一量"),
            "ask1_price": ask,
            "ask1_volume": _float(raw, "ask1_volume", "卖一量"),
            "bid_ask_spread": (ask - bid) if ask is not None and bid is not None else None,
            "limit_up_price": limit_up,
            "limit_down_price": limit_down,
            "distance_to_limit_up_pct": ((limit_up - latest) / latest * 100) if latest and limit_up else None,
            "distance_to_limit_down_pct": ((latest - limit_down) / latest * 100) if latest and limit_down else None,
        }
        return [RealtimeQuoteRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="realtime_quote"))]

    def _normalize_adj_factor(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        domain = {**identity, "trade_date": _date(raw, "trade_date", "date"), "adj_factor": _float(raw, "adj_factor", "factor")}
        return [AdjFactorRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="adj_factor"))]

    def _normalize_financial_statement(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        domain = {
            **identity,
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "report_period": str(_value(raw, "report_period", "end_date", "报告期")),
            "report_date": _date(raw, "report_date", "报告日期"),
            "announcement_date": _date(raw, "ann_date", "announcement_date", "公告日期"),
            "statement_type": _value(raw, "statement_type", default="income_statement"),
            "report_type": _value(raw, "report_type", default="standard"),
            "operating_revenue": _float(raw, "operating_revenue", "revenue", "营业收入"),
            "operating_profit": _float(raw, "operating_profit", "营业利润"),
            "net_profit": _float(raw, "net_profit", "净利润"),
            "parent_net_profit": _float(raw, "parent_net_profit", "n_income_attr_p", "归母净利润"),
            "total_assets": _float(raw, "total_assets", "总资产"),
            "total_liabilities": _float(raw, "total_liabilities", "总负债"),
            "parent_equity": _float(raw, "parent_equity", "归母权益"),
            "operating_cash_flow": _float(raw, "operating_cash_flow", "经营现金流"),
        }
        return [FinancialStatementRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="financial_statement"))]

    def _normalize_financial_indicator(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        domain = {
            **identity,
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "report_period": str(_value(raw, "report_period", "end_date", "报告期")),
            "report_date": _date(raw, "report_date", "报告日期"),
            "announcement_date": _date(raw, "ann_date", "announcement_date", "公告日期"),
            "roe": _float(raw, "roe", "ROE"),
            "roa": _float(raw, "roa", "ROA"),
            "gross_margin": _float(raw, "gross_margin", "grossprofit_margin", "毛利率"),
            "net_margin": _float(raw, "net_margin", "netprofit_margin", "净利率"),
            "revenue_yoy": _float(raw, "revenue_yoy", "or_yoy", "营收同比"),
            "net_profit_yoy": _float(raw, "net_profit_yoy", "净利润同比"),
            "debt_asset_ratio": _float(raw, "debt_asset_ratio", "资产负债率"),
            "current_ratio": _float(raw, "current_ratio", "流动比率"),
            "ocf_to_net_profit": _float(raw, "ocf_to_net_profit", "经营现金流净利润比"),
            "eps": _float(raw, "eps", "EPS"),
            "bps": _float(raw, "bps", "BPS"),
        }
        return [FinancialIndicatorRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="financial_indicator"))]

    def _normalize_valuation_metric(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        amount, _ = self._normalize_amount_for_provider(raw, result.provider)
        domain = {
            **identity,
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "trade_date": _date(raw, "trade_date", "date", default=request.end_date or request.start_date),
            "pe": _float(raw, "pe", "PE"),
            "pe_ttm": _float(raw, "pe_ttm", "PE_TTM"),
            "pb": _float(raw, "pb", "PB"),
            "ps": _float(raw, "ps", "PS"),
            "ps_ttm": _float(raw, "ps_ttm", "PS_TTM"),
            "dividend_yield": _float(raw, "dividend_yield", "股息率"),
            "total_market_value": _float(raw, "total_market_value", "total_mv", "总市值"),
            "float_market_value": _float(raw, "float_market_value", "circ_mv", "流通市值"),
            "turnover_rate": normalize_turnover_rate(_float(raw, "turnover_rate", "换手率")),
            "volume_ratio": _float(raw, "volume_ratio", "量比"),
            "amount": amount,
        }
        return [ValuationMetricRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="valuation_metric"))]

    def _normalize_industry_or_concept(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        if _value(raw, "concept_name", "概念名称") is not None:
            domain = {
                **identity,
                "concept_code": _value(raw, "concept_code", "概念代码"),
                "concept_name": _value(raw, "concept_name", "概念名称"),
                "concept_strength": _float(raw, "concept_strength", "关联强度"),
                "evidence": _value(raw, "evidence", "证据"),
                "effective_date": _date(raw, "effective_date", "生效日期", default=request.start_date),
                "end_date": _date(raw, "end_date", "结束日期"),
                "source_methodology": _value(raw, "source_methodology", "口径说明", default=f"{result.provider}:{result.source_api}"),
            }
            return [ConceptMembershipRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="concept_membership"))]
        domain = {
            **identity,
            "industry_system": _value(raw, "industry_system", "行业体系", default=result.provider),
            "industry_code": _value(raw, "industry_code", "行业代码"),
            "industry_name": _value(raw, "industry_name", "industry", "行业名称", "行业"),
            "industry_level": int(_value(raw, "industry_level", "行业层级", default=1)),
            "parent_industry_code": _value(raw, "parent_industry_code", "父行业代码"),
            "effective_date": _date(raw, "effective_date", "生效日期", default=request.start_date),
            "end_date": _date(raw, "end_date", "结束日期"),
            "source_methodology": _value(raw, "source_methodology", "口径说明", default=f"{result.provider}:{result.source_api}"),
        }
        return [IndustryMembershipRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="industry_membership"))]

    def _normalize_money_flow(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        domain = {
            **identity,
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "timezone": _value(raw, "timezone", default=self.config.storage.timezone),
            "trade_date": _date(raw, "trade_date", "date", default=request.end_date or request.start_date),
            "frequency": str(request.frequency or _value(raw, "frequency", default="1d")),
            "source_methodology": _value(raw, "source_methodology", "口径说明", default=f"{result.provider}:{result.source_api}"),
            "main_net_inflow": _float(raw, "main_net_inflow", "主力净流入"),
            "super_large_net_inflow": _float(raw, "super_large_net_inflow", "超大单净流入"),
            "large_net_inflow": _float(raw, "large_net_inflow", "大单净流入"),
            "medium_net_inflow": _float(raw, "medium_net_inflow", "中单净流入"),
            "small_net_inflow": _float(raw, "small_net_inflow", "小单净流入"),
            "main_net_inflow_ratio": _float(raw, "main_net_inflow_ratio", "主力净流入占比"),
        }
        return [MoneyFlowRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="money_flow"))]

    def _normalize_index_data(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        if _value(raw, "normalized_ticker", "con_code", "stock_code", "ts_code") is not None:
            identity = self._stock_identity({**raw, "provider_symbol": _value(raw, "normalized_ticker", "con_code", "stock_code", "ts_code")}, request)
            domain = {
                **identity,
                "index_code": str(_value(raw, "index_code", "index_ts_code", "指数代码")),
                "index_name": _value(raw, "index_name", "指数名称"),
                "weight": _float(raw, "weight", "权重"),
                "effective_date": _date(raw, "effective_date", "in_date", "成分生效日期", default=request.start_date or date.today()),
                "end_date": _date(raw, "end_date", "out_date", "成分结束日期"),
            }
            return [IndexConstituentRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="index_constituent"))]
        if _value(raw, "open", "high", "low", "close") is not None:
            trade_date = _date(raw, "trade_date", "date")
            ts = _dt(raw, "timestamp", "time", default=normalize_timestamp(trade_date))
            domain = {
                "currency": normalize_currency(_value(raw, "currency", default="CNY")),
                "timezone": _value(raw, "timezone", default=self.config.storage.timezone),
                "index_code": str(_value(raw, "index_code", "ts_code", "指数代码")),
                "index_name": _value(raw, "index_name", "指数名称"),
                "exchange": _value(raw, "exchange", "交易所"),
                "market": _value(raw, "market", default="A_share"),
                "asset_type": "index",
                "trade_date": trade_date,
                "timestamp": ts,
                "frequency": str(request.frequency or _value(raw, "frequency", default="1d")),
                "open": _float(raw, "open"),
                "high": _float(raw, "high"),
                "low": _float(raw, "low"),
                "close": _float(raw, "close"),
                "pre_close": _float(raw, "pre_close"),
                "change": _float(raw, "change"),
                "pct_change": _float(raw, "pct_change", "pct_chg"),
                "volume": _float(raw, "volume", "vol"),
                "amount": _float(raw, "amount"),
                "adjust": "none",
            }
            return [IndexBarRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="index_bar"))]
        domain = {
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "index_code": str(_value(raw, "index_code", "ts_code", "指数代码")),
            "index_name": _value(raw, "index_name", "name", "指数名称"),
            "exchange": _value(raw, "exchange", "交易所"),
            "market": _value(raw, "market", default="A_share"),
            "asset_type": "index",
            "list_date": _date(raw, "list_date", "上市日期"),
            "index_provider": _value(raw, "index_provider", default=result.provider),
        }
        return [IndexRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="index"))]

    def _normalize_corporate_action(self, result: ProviderFetchResult, raw: dict[str, Any], idx: int, request: StockDataRequest, run_id: str) -> list[StandardRecord]:
        identity = self._stock_identity(raw, request)
        action_type = _value(raw, "action_type", "类型", default="dividend")

        # Tushare corporate-action endpoints use endpoint-specific names:
        # dividend: cash_div/cash_div_tax, stk_div/stk_bo_rate/stk_co_rate, pay_date
        # share_float: ann_date, float_date, float_share, float_ratio
        # Map the fields that fit the common CorporateActionRecord without losing
        # the original raw row in Raw Object Store. Endpoint-specific fields remain
        # traceable through raw_payload_ref/raw_row_index.
        stock_bonus_ratio = _float(raw, "stock_bonus_ratio", "stk_div", "送股比例")
        if stock_bonus_ratio is None:
            stk_bo_rate = _float(raw, "stk_bo_rate") or 0.0
            stk_co_rate = _float(raw, "stk_co_rate") or 0.0
            summed = stk_bo_rate + stk_co_rate
            stock_bonus_ratio = summed if summed else None

        domain = {
            **identity,
            "currency": normalize_currency(_value(raw, "currency", default="CNY")),
            "action_type": action_type,
            "announcement_date": _date(raw, "announcement_date", "ann_date", "公告日期"),
            "record_date": _date(raw, "record_date", "股权登记日"),
            "ex_date": _date(raw, "ex_date", "float_date", "除权除息日", "解禁日期"),
            "dividend_payment_date": _date(raw, "dividend_payment_date", "pay_date", "派息日"),
            "cash_dividend_per_share": _float(raw, "cash_dividend_per_share", "cash_div_tax", "cash_div", "每股现金分红"),
            "stock_bonus_ratio": stock_bonus_ratio,
            "rights_issue_ratio": _float(raw, "rights_issue_ratio", "配股比例"),
            "rights_issue_price": _float(raw, "rights_issue_price", "配股价格"),
        }
        return [CorporateActionRecord(**self._common_record_kwargs(result=result, request=request, ingestion_run_id=run_id, raw_row_index=idx, domain_values=domain, record_type="corporate_action"))]

    def _error_from_exception(
        self,
        exc: Exception,
        code: ErrorCode,
        source_api: str,
        *,
        provider: str | None = None,
        source_site: str | None = None,
        suggested_action: str | None = None,
    ) -> ErrorRecord:
        return ErrorRecord.from_exception(
            exc,
            provider=provider,
            source_api=source_api,
            source_site=source_site,
            error_code=code,
            retryable=False,
            suggested_action=suggested_action,
        )
