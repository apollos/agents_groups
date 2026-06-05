from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Type

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorRecord
from stock_data_ingestion.schemas.records import (
    AdjFactorRecord,
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
}


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
