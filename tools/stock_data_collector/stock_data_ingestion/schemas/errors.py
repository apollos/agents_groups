from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai


class ErrorCode(StrEnum):
    AUTH_FAILED = "AUTH_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TOKEN_MISSING = "TOKEN_MISSING"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_SCHEMA_CHANGED = "PROVIDER_SCHEMA_CHANGED"
    EMPTY_RESULT = "EMPTY_RESULT"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_TICKER = "INVALID_TICKER"
    INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    CROSS_VALIDATION_FAILED = "CROSS_VALIDATION_FAILED"
    RAW_SAVE_FAILED = "RAW_SAVE_FAILED"
    STORAGE_FAILED = "STORAGE_FAILED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ErrorRecord(BaseModel):
    error_id: str = Field(default_factory=lambda: f"err_{uuid4().hex}")
    provider: Optional[str] = None
    source_api: Optional[str] = None
    source_site: Optional[str] = None
    error_code: ErrorCode
    error_message: str
    retryable: bool = False
    retry_count: int = 0
    suggested_action: Optional[str] = None
    created_at: datetime = Field(default_factory=now_asia_shanghai)

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        *,
        provider: Optional[str] = None,
        source_api: Optional[str] = None,
        source_site: Optional[str] = None,
        error_code: ErrorCode = ErrorCode.UNKNOWN_ERROR,
        retryable: bool = False,
        retry_count: int = 0,
        suggested_action: Optional[str] = None,
    ) -> "ErrorRecord":
        return cls(
            provider=provider,
            source_api=source_api,
            source_site=source_site,
            error_code=error_code,
            error_message=str(exc),
            retryable=retryable,
            retry_count=retry_count,
            suggested_action=suggested_action,
        )
