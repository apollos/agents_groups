"""Logging helpers for MIC runs.

All runtime logs are written under ``logs/`` by default. Raw page content is not
persisted; log records contain only run metadata, counters, provider/model status
and errors.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Tuple

_LOGGER_NAME = "mic"
_FILE_HANDLER_MARKER = "_mic_run_file_handler"
_CONSOLE_HANDLER_MARKER = "_mic_console_handler"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def logs_dir(log_dir: str | Path | None = None) -> Path:
    configured = log_dir or os.environ.get("MIC_LOG_DIR") or project_root() / "logs"
    path = Path(configured)
    if not path.is_absolute():
        path = project_root() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    return logs_dir()


def _level() -> int:
    value = os.environ.get("MIC_LOG_LEVEL", "INFO").upper()
    return getattr(logging, value, logging.INFO)


def setup_logging(
    run_id: str | None = None,
    *,
    console: bool = False,
    filename: str | None = None,
) -> Tuple[logging.Logger, Optional[Path]]:
    """Configure and return the project logger.

    Sequential runs replace the previous MIC file handler, so repeated API calls
    do not duplicate log lines. The root logger is also configured for modules
    that use ``logging.getLogger(__name__)``.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(_level())
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if console and not any(getattr(h, _CONSOLE_HANDLER_MARKER, False) for h in logger.handlers):
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(formatter)
        setattr(ch, _CONSOLE_HANDLER_MARKER, True)
        logger.addHandler(ch)

    log_path: Optional[Path] = None
    if run_id or filename:
        for handler in list(logger.handlers):
            if getattr(handler, _FILE_HANDLER_MARKER, False):
                logger.removeHandler(handler)
                handler.close()
        log_path = logs_dir() / (filename or f"{run_id}.log")
        fh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(formatter)
        setattr(fh, _FILE_HANDLER_MARKER, True)
        logger.addHandler(fh)
        os.environ["MIC_ACTIVE_LOG_FILE"] = str(log_path)

    # Also attach the same file handler to root for modules using their module
    # logger. Avoid duplicate root handlers by replacing only MIC-created ones.
    if log_path is not None:
        root = logging.getLogger()
        root.setLevel(_level())
        for handler in list(root.handlers):
            if getattr(handler, _FILE_HANDLER_MARKER, False):
                root.removeHandler(handler)
                handler.close()
        rfh = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        rfh.setFormatter(formatter)
        setattr(rfh, _FILE_HANDLER_MARKER, True)
        root.addHandler(rfh)

    return logger, log_path


def configure_logging(
    log_dir: str | Path | None = None,
    log_file: str | None = None,
    level: str | int = "INFO",
    console: bool = True,
) -> Path:
    """Backward-compatible logger used by CLI/tools."""
    if log_dir is not None:
        os.environ["MIC_LOG_DIR"] = str(log_dir)
    if isinstance(level, str):
        os.environ["MIC_LOG_LEVEL"] = level
    path_name = log_file or "mic.log"
    logger, path = setup_logging(filename=path_name, console=console)
    return path or (logs_dir(log_dir) / path_name)


def get_logger(name: str | None = None) -> logging.Logger:
    suffix = f".{name}" if name else ""
    return logging.getLogger(_LOGGER_NAME + suffix)
