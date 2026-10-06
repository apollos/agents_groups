from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE_LOGGER = "agent_trade_intel"
AGENT_EVENT_FILE = "agent.jsonl"
TOOL_STDERR_FILE = "tool_stderr.jsonl"
LOG_FILE_MAX_BYTES = 20 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5
# Same correlation vocabulary as the stock tool's JSONL events (null when unknown).
CORRELATION_FIELDS = ("trace_id", "request_id", "parent_request_id", "ingestion_run_id", "context_id", "context_type", "symbol", "idempotency_key")

_configured = False
_tool_sinks: dict[str, "ToolLogSink"] = {}
_sink_lock = threading.Lock()
_default_fields: dict[str, Any] = {"data_mode": "live"}


def set_default_log_fields(**fields: Any) -> None:
    """Process-wide defaults merged into every structured event (e.g. ``data_mode="simulated"``)."""
    _default_fields.update(fields)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class _StructuredEventFilter(logging.Filter):
    """Only records emitted through :func:`emit` reach ``agent.jsonl``."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return hasattr(record, "event")


class JsonlEventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        details: dict[str, Any] = {**_default_fields, **(getattr(record, "details", {}) or {})}
        payload: dict[str, Any] = {
            "event": getattr(record, "event", "log"),
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        for field in CORRELATION_FIELDS:
            payload[field] = details.pop(field, None)
        payload["data_mode"] = details.pop("data_mode", "live")
        payload["logger"] = record.name
        payload.update(details)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def emit(logger: logging.Logger, level: int, event: str, **details: Any) -> None:
    """Structured event: ``event`` plus correlation fields (null when missing) and details."""
    if not logger.isEnabledFor(level):
        return
    logger.log(level, event, extra={"event": event, "details": _json_safe(details)})


def setup_logging(
    log_dir: str | Path,
    *,
    level: str = "INFO",
    retention_days: int = 14,
    debug: bool | None = None,
) -> logging.Logger:
    """Configure the package logger once per process.

    * ``intelligence_collector.log`` -- human-readable, daily rotation (unchanged);
    * ``agent.jsonl`` -- structured events (:func:`emit`), 20 MiB x 5 rotation;
    * stderr -- WARNING and above; stdout is never used (the CLI prints JSON there).

    ``debug=True`` selects DEBUG regardless of the configured level; the default is INFO.
    """
    global _configured
    logger = logging.getLogger(PACKAGE_LOGGER)
    if debug is not None:
        level = "DEBUG" if debug else "INFO"
    if _configured:
        if debug is not None:
            logger.setLevel(getattr(logging, level, logging.INFO))
        return logger

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path / "intelligence_collector.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    event_handler = logging.handlers.RotatingFileHandler(
        log_path / AGENT_EVENT_FILE, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUP_COUNT, encoding="utf-8"
    )
    event_handler.setFormatter(JsonlEventFormatter())
    event_handler.addFilter(_StructuredEventFilter())
    event_handler.setLevel(logging.DEBUG)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.WARNING)

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.addHandler(file_handler)
    logger.addHandler(event_handler)
    logger.addHandler(stderr_handler)
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{PACKAGE_LOGGER}.{name}")


def debug_enabled() -> bool:
    return logging.getLogger(PACKAGE_LOGGER).isEnabledFor(logging.DEBUG)


class ToolLogSink:
    """Agent-owned on-disk copy of a tool subprocess' structured stderr.

    Each subprocess writes its JSONL log lines to stderr; the agent captures them and appends
    them here after the call (success, failure and timeout alike). Concurrent subprocesses
    therefore never share or co-rotate a file: one rotating file per agent process, written
    under a lock. Non-JSON stderr lines (tracebacks) are wrapped so the file stays JSONL.
    """

    def __init__(self, path: str | Path, *, max_bytes: int = LOG_FILE_MAX_BYTES, backup_count: int = LOG_FILE_BACKUP_COUNT) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = logging.handlers.RotatingFileHandler(self.path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._lock = threading.Lock()

    def write(self, stderr: str | bytes | None, *, trace_id: str | None = None, tool: str | None = None, outcome: str | None = None) -> int:
        """Append every stderr line; returns the number of lines written."""
        if not stderr:
            return 0
        text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
        written = 0
        with self._lock:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if not (line.startswith("{") and line.endswith("}")):
                    line = json.dumps(
                        {
                            "event": "tool_stderr_text",
                            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
                            "level": "INFO",
                            "trace_id": trace_id,
                            "tool": tool,
                            "call_outcome": outcome,
                            "line": line,
                        },
                        ensure_ascii=False,
                    )
                record = logging.LogRecord("tool_stderr", logging.INFO, __file__, 0, line, None, None)
                self._handler.handle(record)
                written += 1
        return written

    def close(self) -> None:
        self._handler.close()


def get_tool_log_sink(log_dir: str | Path, filename: str = TOOL_STDERR_FILE) -> ToolLogSink:
    """One sink per (process, path): shared by every adapter so rotation is serialised."""
    path = str(Path(log_dir) / filename)
    with _sink_lock:
        sink = _tool_sinks.get(path)
        if sink is None:
            sink = ToolLogSink(path)
            _tool_sinks[path] = sink
        return sink
