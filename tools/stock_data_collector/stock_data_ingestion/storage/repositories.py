from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple, Type

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorRecord
from stock_data_ingestion.schemas.records import (
    AdjFactorRecord,
    BarRecord,
    CommodityPriceRecord,
    ConceptMembershipRecord,
    CorporateActionRecord,
    FinancialIndicatorRecord,
    FinancialStatementRecord,
    FxRateRecord,
    IndexBarRecord,
    InterestRateRecord,
    IndexConstituentRecord,
    IndexRecord,
    IndustryMembershipRecord,
    MoneyFlowRecord,
    ProviderComparisonResult,
    ProviderFetchResult,
    RawPayloadIndexRecord,
    RealtimeQuoteRecord,
    SecurityMasterRecord,
    TradeCalendarRecord,
    TradingStatusRecord,
    ValuationMetricRecord,
)
from stock_data_ingestion.schemas.quality import DataQualityConflict
from stock_data_ingestion.storage import models
from stock_data_ingestion.storage.models import Base


STANDARD_MODEL_BY_RECORD_TYPE: dict[str, Type[Base]] = {
    "security_master": models.SecurityModel,
    "trade_calendar": models.TradeCalendarModel,
    "trading_status": models.TradingStatusModel,
    "realtime_quote": models.RealtimeQuoteModel,
    "adj_factor": models.AdjFactorModel,
    "financial_statement": models.FinancialStatementModel,
    "financial_indicator": models.FinancialIndicatorModel,
    "valuation_metric": models.ValuationMetricModel,
    "industry_membership": models.IndustryMembershipModel,
    "concept_membership": models.ConceptMembershipModel,
    "money_flow": models.MoneyFlowModel,
    "index": models.IndexModel,
    "index_bar": models.IndexBarModel,
    "index_constituent": models.IndexConstituentModel,
    "corporate_action": models.CorporateActionModel,
    "fx_rate": models.FxRateModel,
    "commodity_price": models.CommodityPriceModel,
    "interest_rate": models.InterestRateModel,
}

# Market-context records: one current row per business key (the table's unique constraint),
# newer same-key records replace it and the previous row is archived in full.
MARKET_CONTEXT_RECORD_CLASS_BY_TYPE: dict[str, Type[BaseModel]] = {
    "fx_rate": FxRateRecord,
    "interest_rate": InterestRateRecord,
    "index_bar": IndexBarRecord,
    "commodity_price": CommodityPriceRecord,
}
MARKET_CONTEXT_BUSINESS_KEYS: dict[str, tuple[str, ...]] = {
    "fx_rate": ("base_currency", "quote_currency", "quote_basis", "rate_type", "rate_date", "effective_provider"),
    "interest_rate": ("rate_type", "market", "curve_name", "tenor", "rate_date", "effective_provider"),
    "index_bar": ("index_code", "frequency", "trade_date", "timestamp", "effective_provider"),
    "commodity_price": ("commodity", "instrument_type", "market", "contract", "frequency", "trade_date", "observed_at", "effective_provider"),
}
_MODEL_AUDIT_COLUMNS = {"id", "created_at", "updated_at"}
# Domain value columns reported in record_write events (per record type).
MARKET_CONTEXT_VALUE_FIELDS: dict[str, tuple[str, ...]] = {
    "fx_rate": ("rate",),
    "interest_rate": ("rate_value",),
    "index_bar": ("close", "open", "high", "low"),
    "commodity_price": ("close", "settle", "latest", "pre_settle"),
}


class MarketContextUpsertOutcome(NamedTuple):
    """Result of :meth:`Repository.upsert_market_context_record`.

    ``record`` is the record the store holds after the call (the incoming one for
    ``inserted``/``replaced``, the stored one for ``kept_existing``); callers must report it.
    """

    record: BaseModel
    table: str
    action: str  # inserted | replaced | kept_existing
    incoming_record_id: str
    existing_record_id: str | None
    archived_record_ids: list[str]
    comparison_basis: str | None  # provider_update_time | fetch_time | None (insert)
    business_key: dict[str, Any]


def _as_aware(value: Any) -> datetime:
    """SQLite returns naive datetimes; compare everything in Asia/Shanghai."""
    if value is None:
        return datetime.min.replace(tzinfo=timezone(timedelta(hours=8)))
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone(timedelta(hours=8)))
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


class Repository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _to_model_kwargs(self, model_cls: Type[Base], record: BaseModel | dict[str, Any]) -> dict[str, Any]:
        data = record.model_dump(mode="python") if isinstance(record, BaseModel) else dict(record)
        columns = model_cls.__table__.columns.keys()
        kwargs = {key: value for key, value in data.items() if key in columns}
        # SQLAlchemy JSON columns cannot serialize Pydantic models/enums reliably in python mode.
        for key, value in list(kwargs.items()):
            if isinstance(value, BaseModel):
                kwargs[key] = value.model_dump(mode="json")
            elif isinstance(value, list):
                kwargs[key] = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
            elif isinstance(value, dict):
                kwargs[key] = {
                    k: v.model_dump(mode="json") if isinstance(v, BaseModel) else v
                    for k, v in value.items()
                }
        return kwargs

    def insert_skip_duplicate(self, model_cls: Type[Base], record: BaseModel | dict[str, Any]) -> bool:
        obj = model_cls(**self._to_model_kwargs(model_cls, record))
        try:
            with self.session.begin_nested():
                self.session.add(obj)
                self.session.flush()
            return True
        except IntegrityError:
            # Keep the outer transaction alive. Repeated writes are expected in idempotent runs.
            return False

    def insert_raw_payload_index(self, record: RawPayloadIndexRecord) -> bool:
        return self.insert_skip_duplicate(models.RawPayloadIndexModel, record)

    def insert_provider_fetch_result(
        self,
        result: ProviderFetchResult,
        request_id: str | None = None,
        ingestion_run_id: str | None = None,
    ) -> bool:
        data = result.model_dump(mode="python")
        data["request_id"] = request_id
        data["ingestion_run_id"] = ingestion_run_id
        data["error"] = result.error.model_dump(mode="json") if result.error else None
        data["validation_status"] = "failed" if str(result.status) in {"failed", "unavailable"} else "validated"
        data["data_quality"] = 1.0 if str(result.status) == "success" else 0.0
        return self.insert_skip_duplicate(models.SourceFetchLogModel, data)

    def insert_provider_comparison(self, result: ProviderComparisonResult) -> bool:
        data = result.model_dump(mode="python")
        data["conflicts"] = [c.model_dump(mode="json") for c in result.conflicts]
        data["validation_status"] = "validated" if result.status == "matched" else "conflicted_high"
        return self.insert_skip_duplicate(models.ProviderComparisonModel, data)

    def insert_conflict(self, conflict: DataQualityConflict) -> bool:
        return self.insert_skip_duplicate(models.DataQualityConflictModel, conflict)

    def insert_conflicts(self, conflicts: Iterable[DataQualityConflict]) -> int:
        return sum(1 for conflict in conflicts if self.insert_conflict(conflict))

    def insert_bar(self, record: BarRecord) -> bool:
        model_cls = models.BAR_MODEL_BY_FREQUENCY.get(record.frequency)
        if model_cls is None:
            raise ValueError(f"INVALID_REQUEST: unsupported bar frequency {record.frequency}")
        return self.insert_skip_duplicate(model_cls, record)

    def insert_standard_record(self, record: BaseModel) -> tuple[bool, str]:
        if isinstance(record, BarRecord):
            inserted = self.insert_bar(record)
            table = "daily_bars" if record.frequency == "1d" else "minute_bars" if record.frequency.endswith("m") else "weekly_bars"
            return inserted, table
        record_type = getattr(record, "record_type", None)
        model_cls = STANDARD_MODEL_BY_RECORD_TYPE.get(str(record_type))
        if model_cls is None:
            raise ValueError(f"INVALID_REQUEST: unsupported record_type {record_type}")
        return self.insert_skip_duplicate(model_cls, record), model_cls.__tablename__

    def insert_standard_records(self, records: Iterable[BaseModel]) -> list[str]:
        tables: list[str] = []
        for record in records:
            inserted, table = self.insert_standard_record(record)
            if inserted:
                tables.append(table)
        return tables

    # ------------------------------------------------------------------
    # Market-context records: current row per business key + revision archive
    # ------------------------------------------------------------------
    def _row_to_record(self, record_type: str, row: Base) -> BaseModel:
        record_cls = MARKET_CONTEXT_RECORD_CLASS_BY_TYPE[record_type]
        data = {col.name: getattr(row, col.name) for col in row.__table__.columns if col.name not in _MODEL_AUDIT_COLUMNS}
        return record_cls.model_validate(data)

    def _find_market_context_rows(self, model_cls: Type[Base], record_type: str, values: dict[str, Any]) -> list[Base]:
        """All current rows for the business key, newest first.

        Normally at most one row exists; databases written before the current-row rule may hold
        duplicates (NULL business-key parts bypass the SQLite UNIQUE constraint).
        """
        stmt = select(model_cls)
        for key in MARKET_CONTEXT_BUSINESS_KEYS[record_type]:
            column = getattr(model_cls, key)
            value = values.get(key)
            stmt = stmt.where(column.is_(None) if value is None else column == value)
        rows = list(self.session.execute(stmt).scalars().all())
        rows.sort(key=lambda row: (_as_aware(getattr(row, "fetch_time", None)) or datetime.min.replace(tzinfo=timezone.utc), row.id), reverse=True)
        return rows

    def _find_current_market_context_row(self, model_cls: Type[Base], record_type: str, values: dict[str, Any]) -> Base | None:
        rows = self._find_market_context_rows(model_cls, record_type, values)
        return rows[0] if rows else None

    @staticmethod
    def _comparison_basis(incoming: dict[str, Any], existing: Base) -> str:
        inc_put, cur_put = incoming.get("provider_update_time"), getattr(existing, "provider_update_time", None)
        return "provider_update_time" if inc_put is not None and cur_put is not None else "fetch_time"

    @classmethod
    def _is_newer(cls, incoming: dict[str, Any], existing: Base) -> bool:
        """Compare provider_update_time when both sides have it, otherwise fetch_time."""
        if cls._comparison_basis(incoming, existing) == "provider_update_time":
            return _as_aware(incoming["provider_update_time"]) > _as_aware(existing.provider_update_time)
        return _as_aware(incoming.get("fetch_time")) > _as_aware(getattr(existing, "fetch_time", None))

    def upsert_market_context_record(self, record: BaseModel) -> MarketContextUpsertOutcome:
        """Save one fx_rate / interest_rate / index_bar / commodity_price record.

        * no row for the business key            -> insert;
        * existing row and the incoming is newer  -> archive the full existing row in
          ``market_context_record_revisions`` and replace every column (values, record_id,
          quality, request/run ids, raw refs, provenance, fetch/provider times) atomically;
        * incoming is older or same age           -> keep the existing row untouched.

        Returns a :class:`MarketContextUpsertOutcome`; callers must report ``outcome.record``.
        """
        record_type = str(getattr(record, "record_type", ""))
        if record_type not in MARKET_CONTEXT_BUSINESS_KEYS:
            raise ValueError(f"INVALID_REQUEST: {record_type!r} is not a market-context record type")
        model_cls = STANDARD_MODEL_BY_RECORD_TYPE[record_type]
        kwargs = self._to_model_kwargs(model_cls, record)
        table = model_cls.__tablename__
        business_key = {key: kwargs.get(key) for key in MARKET_CONTEXT_BUSINESS_KEYS[record_type]}
        with self.session.begin_nested():
            rows = self._find_market_context_rows(model_cls, record_type, kwargs)
            if not rows:
                self.session.add(model_cls(**kwargs))
                self.session.flush()
                return MarketContextUpsertOutcome(record, table, "inserted", kwargs["record_id"], None, [], None, business_key)
            existing = rows[0]
            basis = self._comparison_basis(kwargs, existing)
            if not self._is_newer(kwargs, existing):
                return MarketContextUpsertOutcome(
                    self._row_to_record(record_type, existing), table, "kept_existing", kwargs["record_id"], existing.record_id, [], basis, business_key
                )
            archived_at = now_asia_shanghai()
            archived_record_ids = [row.record_id for row in rows]  # before the in-place update refreshes rows[0]
            for row in rows:
                # Every replaced row (including legacy duplicates of the key) is archived in full.
                archived = {col.name: getattr(row, col.name) for col in row.__table__.columns if col.name not in _MODEL_AUDIT_COLUMNS}
                self.session.add(
                    models.MarketContextRecordRevisionModel(
                        record_id=archived["record_id"],
                        record_type=record_type,
                        table_name=table,
                        business_key={key: _json_safe(archived.get(key)) for key in MARKET_CONTEXT_BUSINESS_KEYS[record_type]},
                        record_json={k: _json_safe(v) for k, v in archived.items()},
                        superseded_by_record_id=kwargs["record_id"],
                        request_id=archived.get("request_id"),
                        ingestion_run_id=archived.get("ingestion_run_id"),
                        archived_at=archived_at,
                    )
                )
            for duplicate in rows[1:]:
                self.session.delete(duplicate)
            self.session.execute(update(model_cls).where(model_cls.id == existing.id).values(**kwargs, updated_at=archived_at))
            self.session.flush()
            return MarketContextUpsertOutcome(
                record, table, "replaced", kwargs["record_id"], archived_record_ids[0], archived_record_ids, basis, business_key
            )

    def link_request_record(self, request_id: str, record_type: str, record_id: str) -> bool:
        """Associate a request with a record it finally adopted (idempotent; same transaction as the record write)."""
        stmt = (
            sqlite_insert(models.MarketContextRequestRecordModel)
            .values(request_id=request_id, record_type=record_type, record_id=record_id)
            .on_conflict_do_nothing()
        )
        return bool(self.session.execute(stmt).rowcount)

    def get_request_record_links(self, request_id: str, record_type: str | None = None) -> list[models.MarketContextRequestRecordModel]:
        stmt = select(models.MarketContextRequestRecordModel).where(models.MarketContextRequestRecordModel.request_id == request_id)
        if record_type:
            stmt = stmt.where(models.MarketContextRequestRecordModel.record_type == record_type)
        return list(self.session.execute(stmt).scalars().all())

    def get_market_context_revision(self, record_id: str) -> models.MarketContextRecordRevisionModel | None:
        stmt = select(models.MarketContextRecordRevisionModel).where(models.MarketContextRecordRevisionModel.record_id == record_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def insert_ingestion_request(self, request: BaseModel, status: str = "created") -> bool:
        data = request.model_dump(mode="python")
        row = {
            "request_id": data["request_id"],
            "schema_version": data["schema_version"],
            "request_type": data["request_type"],
            "idempotency_key": data["idempotency_key"],
            "requested_by": data.get("requested_by", "manual"),
            "request_json": request.model_dump(mode="json"),
            "status": status,
        }
        return self.insert_skip_duplicate(models.IngestionRequestModel, row)

    def insert_ingestion_run(self, ingestion_run_id: str, request_id: str, request_type: str, started_at, status: str = "running") -> bool:
        return self.insert_skip_duplicate(
            models.IngestionRunModel,
            {
                "ingestion_run_id": ingestion_run_id,
                "request_id": request_id,
                "request_type": request_type,
                "started_at": started_at,
                "status": status,
            },
        )

    def update_ingestion_request_status(self, request_id: str, idempotency_key: str, status: str, *, raw_payload_id: str | None = None, data_quality: float = 0.0) -> None:
        values: dict[str, Any] = {
            "status": status,
            "data_quality": data_quality,
            "validation_status": "validated" if status == "success" else status,
            "updated_at": now_asia_shanghai(),
        }
        if raw_payload_id:
            values["raw_payload_id"] = raw_payload_id
        stmt = (
            update(models.IngestionRequestModel)
            .where(
                (models.IngestionRequestModel.request_id == request_id)
                | (models.IngestionRequestModel.idempotency_key == idempotency_key)
            )
            .values(**values)
        )
        self.session.execute(stmt)

    def update_ingestion_run_status(
        self,
        ingestion_run_id: str,
        status: str,
        *,
        provider_results: Iterable[ProviderFetchResult] = (),
        error_records: Iterable[ErrorRecord] = (),
        raw_payload_id: str | None = None,
        data_quality: float = 0.0,
        completed_at: datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "completed_at": completed_at or now_asia_shanghai(),
            "provider_results": [result.model_dump(mode="json") for result in provider_results],
            "error_records": [error.model_dump(mode="json") for error in error_records],
            "data_quality": data_quality,
            "validation_status": "validated" if status == "success" else status,
            "updated_at": now_asia_shanghai(),
        }
        if raw_payload_id:
            values["raw_payload_id"] = raw_payload_id
        self.session.execute(
            update(models.IngestionRunModel)
            .where(models.IngestionRunModel.ingestion_run_id == ingestion_run_id)
            .values(**values)
        )

    def has_successful_idempotency_key(self, key: str) -> bool:
        if not key:
            return False
        stmt = select(models.IngestionRequestModel).where(
            models.IngestionRequestModel.idempotency_key == key,
            models.IngestionRequestModel.status == "success",
        )
        return self.session.execute(stmt).first() is not None

    def get_successful_request_by_idempotency_key(self, key: str):  # type: ignore[no-untyped-def]
        if not key:
            return None
        stmt = select(models.IngestionRequestModel).where(
            models.IngestionRequestModel.idempotency_key == key,
            models.IngestionRequestModel.status == "success",
        )
        return self.session.execute(stmt).scalar_one_or_none()
