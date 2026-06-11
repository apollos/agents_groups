from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

PACKAGE_LOGGER = "agent_trade_intel"

_configured = False


def setup_logging(log_dir: str | Path, *, level: str = "INFO", retention_days: int = 14) -> logging.Logger:
    """Configure the package logger to write daily-rotated files under log_dir.

    stdout is intentionally left untouched: the CLI prints JSON results there, so logs go to
    file only (plus stderr for WARNING and above).
    """
    global _configured
    logger = logging.getLogger(PACKAGE_LOGGER)
    if _configured:
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

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.WARNING)

    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.addHandler(file_handler)
    logger.addHandler(stderr_handler)
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{PACKAGE_LOGGER}.{name}")
