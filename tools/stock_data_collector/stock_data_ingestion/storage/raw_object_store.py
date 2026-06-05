from __future__ import annotations

import gzip
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote, unquote

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.records import RawPayloadIndexRecord
from stock_data_ingestion.utils.hashing import canonical_json_dumps, sha256_file, sha256_json


class RawObjectStore:
    """Local raw object store using gzip-compressed JSON Lines.

    This class intentionally stores raw external payloads outside SQLite. The database should keep only
    RawPayloadIndexRecord metadata plus raw_payload_id/raw_payload_ref/raw_row_index references.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def build_raw_payload_id(self, provider: str, request_type: str, fetch_date: date, request_id: str) -> str:
        safe_request_id = re.sub(r"[^A-Za-z0-9_]+", "_", request_id).strip("_")
        return f"raw_{provider}_{request_type}_{fetch_date.strftime('%Y%m%d')}_{safe_request_id}"

    def build_path(self, provider: str, request_type: str, fetch_date: date, raw_payload_id: str) -> Path:
        return (
            self.root
            / f"provider={provider}"
            / f"request_type={request_type}"
            / f"date={fetch_date.isoformat()}"
            / f"{raw_payload_id}.jsonl.gz"
        )

    def build_raw_payload_ref(self, path: str | Path) -> str:
        rel = Path(path).resolve().relative_to(self.root.resolve())
        return "raw://local/" + quote(rel.as_posix())

    def parse_raw_payload_ref(self, raw_payload_ref: str) -> Path:
        prefix = "raw://local/"
        if not raw_payload_ref.startswith(prefix):
            raise ValueError(f"INVALID_REQUEST: unsupported raw_payload_ref {raw_payload_ref!r}")
        rel = unquote(raw_payload_ref[len(prefix) :])
        path = (self.root / rel).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("INVALID_REQUEST: raw payload ref escapes root")
        return path

    def compute_raw_hash(self, raw_payload_ref_or_path: str | Path) -> str:
        path = self.parse_raw_payload_ref(raw_payload_ref_or_path) if str(raw_payload_ref_or_path).startswith("raw://") else Path(raw_payload_ref_or_path)
        if path.suffixes[-2:] == [".jsonl", ".gz"]:
            digest = hashlib.sha256()
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    obj = json.loads(line)
                    if obj.get("line_type") == "metadata":
                        obj.pop("raw_hash", None)
                    digest.update(canonical_json_dumps(obj).encode("utf-8"))
                    digest.update(b"\n")
            return "sha256:" + digest.hexdigest()
        return sha256_file(path)

    def verify_raw_hash(self, raw_payload_ref_or_path: str | Path, expected_hash: str) -> bool:
        return self.compute_raw_hash(raw_payload_ref_or_path) == expected_hash

    def save_raw_payload(
        self,
        *,
        provider: str,
        request_type: str,
        source_api: str,
        source_site: str,
        adapter_version: str,
        request_id: str,
        ingestion_run_id: str,
        sanitized_request_params: dict[str, Any],
        raw_records: Iterable[dict[str, Any]],
        idempotency_key: str,
        fetch_started_at: Optional[datetime] = None,
        fetch_completed_at: Optional[datetime] = None,
        provider_update_time: Optional[datetime] = None,
        timezone: str = "Asia/Shanghai",
    ) -> RawPayloadIndexRecord:
        started = fetch_started_at or now_asia_shanghai()
        completed = fetch_completed_at or now_asia_shanghai()
        fetch_date = completed.date()
        raw_payload_id = self.build_raw_payload_id(provider, request_type, fetch_date, request_id)
        path = self.build_path(provider, request_type, fetch_date, raw_payload_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Never overwrite an existing raw object. A repeated idempotent request returns the existing
        # object index. If callers need a new run, they must use a distinct request_id.
        if path.exists():
            existing_ref = self.build_raw_payload_ref(path)
            metadata, rows = self.load_raw_payload(existing_ref)
            return RawPayloadIndexRecord(
                raw_payload_id=metadata["raw_payload_id"],
                raw_payload_ref=existing_ref,
                provider=metadata["provider"],
                source_api=metadata["source_api"],
                source_site=metadata["source_site"],
                adapter_version=metadata["adapter_version"],
                request_id=metadata["request_id"],
                ingestion_run_id=metadata["ingestion_run_id"],
                request_type=metadata["request_type"],
                sanitized_request_params=metadata.get("sanitized_request_params", {}),
                request_params_hash=metadata["request_params_hash"],
                idempotency_key=metadata["idempotency_key"],
                fetch_started_at=datetime.fromisoformat(metadata["fetch_started_at"]),
                fetch_completed_at=datetime.fromisoformat(metadata["fetch_completed_at"]),
                provider_update_time=datetime.fromisoformat(metadata["provider_update_time"]) if metadata.get("provider_update_time") else None,
                raw_format=metadata.get("raw_format", "jsonl.gz"),
                content_encoding=metadata.get("content_encoding", "gzip"),
                timezone=metadata.get("timezone", "Asia/Shanghai"),
                raw_hash=metadata.get("raw_hash") or self.compute_raw_hash(path),
                rows_fetched=len(rows),
            )

        records = list(raw_records)
        request_params_hash = sha256_json(sanitized_request_params)
        metadata = {
            "line_type": "metadata",
            "schema_version": "raw_payload.v0.1",
            "raw_payload_id": raw_payload_id,
            "provider": provider,
            "source_site": source_site,
            "source_api": source_api,
            "adapter_version": adapter_version,
            "request_id": request_id,
            "ingestion_run_id": ingestion_run_id,
            "request_type": request_type,
            "sanitized_request_params": sanitized_request_params,
            "request_params_hash": request_params_hash,
            "idempotency_key": idempotency_key,
            "fetch_started_at": started.isoformat(),
            "fetch_completed_at": completed.isoformat(),
            "provider_update_time": provider_update_time.isoformat() if provider_update_time else None,
            "raw_format": "jsonl.gz",
            "content_encoding": "gzip",
            "timezone": timezone,
        }

        raw_lines = [
            {
                "line_type": "raw_record",
                "raw_row_index": i,
                "provider_symbol": row.get("provider_symbol") or row.get("ts_code") or row.get("code") or row.get("symbol"),
                "raw_data": row,
            }
            for i, row in enumerate(records)
        ]
        digest = hashlib.sha256()
        for obj in [metadata, *raw_lines]:
            digest.update(canonical_json_dumps(obj).encode("utf-8"))
            digest.update(b"\n")
        raw_hash = "sha256:" + digest.hexdigest()
        metadata["raw_hash"] = raw_hash

        with gzip.open(path, "wt", encoding="utf-8", newline="\n") as f:
            f.write(canonical_json_dumps(metadata) + "\n")
            for line in raw_lines:
                f.write(canonical_json_dumps(line) + "\n")

        raw_hash = self.compute_raw_hash(path)
        raw_payload_ref = self.build_raw_payload_ref(path)
        return RawPayloadIndexRecord(
            raw_payload_id=raw_payload_id,
            raw_payload_ref=raw_payload_ref,
            provider=provider,
            source_api=source_api,
            source_site=source_site,
            adapter_version=adapter_version,
            request_id=request_id,
            ingestion_run_id=ingestion_run_id,
            request_type=request_type,
            sanitized_request_params=sanitized_request_params,
            request_params_hash=request_params_hash,
            idempotency_key=idempotency_key,
            fetch_started_at=started,
            fetch_completed_at=completed,
            provider_update_time=provider_update_time,
            raw_format="jsonl.gz",
            content_encoding="gzip",
            timezone=timezone,
            raw_hash=raw_hash,
            rows_fetched=len(records),
        )

    def load_raw_payload(self, raw_payload_ref_or_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        path = self._resolve_ref_or_id(raw_payload_ref_or_id)
        metadata: dict[str, Any] | None = None
        rows: list[dict[str, Any]] = []
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                obj = json.loads(line)
                if obj.get("line_type") == "metadata":
                    metadata = obj
                elif obj.get("line_type") == "raw_record":
                    rows.append(obj)
        if metadata is None:
            raise ValueError("RAW_SAVE_FAILED: raw payload metadata line missing")
        return metadata, rows

    def list_raw_payloads(self, provider: str | None = None, request_type: str | None = None) -> list[str]:
        pattern = "*.jsonl.gz"
        root = self.root
        if provider:
            root = root / f"provider={provider}"
        if request_type:
            root = root / f"request_type={request_type}"
        if not root.exists():
            return []
        return [self.build_raw_payload_ref(path) for path in sorted(root.rglob(pattern))]

    def read_raw_record_by_index(self, raw_payload_ref_or_id: str, raw_row_index: int) -> dict[str, Any]:
        _, rows = self.load_raw_payload(raw_payload_ref_or_id)
        for row in rows:
            if row.get("raw_row_index") == raw_row_index:
                return row
        raise IndexError(f"raw_row_index {raw_row_index} not found")

    def _resolve_ref_or_id(self, raw_payload_ref_or_id: str) -> Path:
        if raw_payload_ref_or_id.startswith("raw://"):
            return self.parse_raw_payload_ref(raw_payload_ref_or_id)
        matches = list(self.root.rglob(f"{raw_payload_ref_or_id}.jsonl.gz"))
        if not matches:
            raise FileNotFoundError(raw_payload_ref_or_id)
        if len(matches) > 1:
            raise ValueError(f"INVALID_REQUEST: multiple raw payloads with id {raw_payload_ref_or_id}")
        return matches[0]
