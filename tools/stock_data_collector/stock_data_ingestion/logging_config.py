"""Structured (JSONL) logging for the ingestion tool.

Every record written by the package logger is one JSON object per line carrying the fixed
correlation fields below (``null`` when unknown). Business output stays on stdout; logs go to
stderr and, optionally, to a size-rotated file (20 MiB x 5 backups).

Correlation fields are kept in a context variable so that nested work (e.g. the daily-bar
collection the runner performs to confirm a realtime snapshot date) inherits ``trace_id`` and
records the request that spawned it as ``parent_request_id``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

PACKAGE_LOGGER = "stock_data_ingestion"
SENSITIVE_ENV_NAMES = {"TUSHARE_TOKEN", "JQDATA_USERNAME", "JQDATA_PASSWORD"}

# Fields present on every JSONL event (missing -> null).
CORRELATION_FIELDS: tuple[str, ...] = (
    "trace_id",
    "request_id",
    "parent_request_id",
    "ingestion_run_id",
    "context_id",
    "context_type",
    "symbol",
    "idempotency_key",
)
LOG_FILE_MAX_BYTES = 20 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5

_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("stock_data_log_context", default={})
# Process-wide defaults layered under the context (e.g. data_mode=simulated for scenario runs).
_default_fields: dict[str, Any] = {"data_mode": "live"}


def set_default_log_fields(**fields: Any) -> None:
    """Process-wide defaults for every event (e.g. ``data_mode="simulated"``)."""
    _default_fields.update(fields)


def current_log_context() -> dict[str, Any]:
    return {**_default_fields, **_log_context.get()}


@contextmanager
def log_context(**fields: Any) -> Iterator[dict[str, Any]]:
    """Push correlation fields for the duration of the block (``None`` values do not override)."""
    current = _log_context.get()
    merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
    token = _log_context.set(merged)
    try:
        yield merged
    finally:
        _log_context.reset(token)


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def emit(logger: logging.Logger, level: int, event: str, **details: Any) -> None:
    """Write one structured event; correlation fields come from the active log context."""
    if not logger.isEnabledFor(level):
        return
    logger.log(level, event, extra={"event": event, "details": json_safe(details)})


class CredentialRedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self.secrets = {s for s in (secrets or []) if s}
        for env_name in SENSITIVE_ENV_NAMES:
            value = os.getenv(env_name)
            if value:
                self.secrets.add(value)

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "***REDACTED***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        return True


class JsonlFormatter(logging.Formatter):
    """One JSON object per record with the fixed correlation fields first."""

    def __init__(self, redactor: CredentialRedactingFilter | None = None) -> None:
        super().__init__()
        self.redactor = redactor or CredentialRedactingFilter()

    def format(self, record: logging.LogRecord) -> str:
        context = current_log_context()
        payload: dict[str, Any] = {
            "event": getattr(record, "event", None) or "log",
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        for field in CORRELATION_FIELDS:
            payload[field] = context.get(field)
        payload["data_mode"] = context.get("data_mode")
        payload["logger"] = record.name
        details = getattr(record, "details", None)
        if isinstance(details, dict):
            payload.update({k: v for k, v in details.items() if k not in payload})
        elif record.getMessage():
            payload["message"] = record.getMessage()
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return self.redactor.redact(text)


class _StderrHandler(logging.StreamHandler):
    """Stream handler bound to *current* ``sys.stderr`` at emit time (survives stream replacement)."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:  # noqa: D401 - logging.StreamHandler assigns in __init__
        pass


def setup_logging(
    log_path: str | Path | None = None,
    level: int = logging.INFO,
    *,
    debug: bool | None = None,
    stream: Any = None,
) -> logging.Logger:
    """Configure the package logger once per process.

    ``debug=True`` selects DEBUG (detailed per-record events); the default is INFO. The log
    file rotates at 20 MiB with 5 backups; stdout is never used.
    """
    if debug is not None:
        level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False
    formatter = JsonlFormatter()

    stream_handler = logging.StreamHandler(stream) if stream is not None else _StderrHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{PACKAGE_LOGGER}.{name}")


def debug_enabled(logger: logging.Logger) -> bool:
    return logger.isEnabledFor(logging.DEBUG)


__all__ = [
    "CORRELATION_FIELDS",
    "CredentialRedactingFilter",
    "JsonlFormatter",
    "LOG_FILE_BACKUP_COUNT",
    "LOG_FILE_MAX_BYTES",
    "PACKAGE_LOGGER",
    "current_log_context",
    "debug_enabled",
    "emit",
    "get_logger",
    "json_safe",
    "log_context",
    "set_default_log_fields",
    "setup_logging",
]
