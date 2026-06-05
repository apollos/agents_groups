from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

SENSITIVE_ENV_NAMES = {"TUSHARE_TOKEN", "JQDATA_USERNAME", "JQDATA_PASSWORD"}


class CredentialRedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self.secrets = {s for s in (secrets or []) if s}
        for env_name in SENSITIVE_ENV_NAMES:
            value = os.getenv(env_name)
            if value:
                self.secrets.add(value)

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for secret in self.secrets:
            msg = msg.replace(secret, "***REDACTED***")
        record.msg = msg
        record.args = ()
        return True


def setup_logging(log_path: str | Path | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("stock_data_ingestion")
    logger.setLevel(level)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s ingestion_run_id=%(ingestion_run_id)s "
        "provider=%(provider)s source_api=%(source_api)s request_type=%(request_type)s "
        "rows_fetched=%(rows_fetched)s raw_payload_id=%(raw_payload_id)s status=%(status)s error_code=%(error_code)s %(message)s"
    )

    class DefaultsFilter(logging.Filter):
        defaults = {
            "request_id": "-",
            "ingestion_run_id": "-",
            "provider": "-",
            "source_api": "-",
            "request_type": "-",
            "rows_fetched": "-",
            "raw_payload_id": "-",
            "status": "-",
            "error_code": "-",
        }

        def filter(self, record: logging.LogRecord) -> bool:
            for key, value in self.defaults.items():
                if not hasattr(record, key):
                    setattr(record, key, value)
            return True

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(DefaultsFilter())
    stream_handler.addFilter(CredentialRedactingFilter())
    logger.addHandler(stream_handler)

    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(DefaultsFilter())
        file_handler.addFilter(CredentialRedactingFilter())
        logger.addHandler(file_handler)
    return logger
