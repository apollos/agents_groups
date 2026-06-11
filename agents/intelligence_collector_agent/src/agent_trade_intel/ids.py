from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_hash(value: Any, length: int = 16) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def make_idempotency_key(*parts: Any) -> str:
    clean = []
    for p in parts:
        if isinstance(p, (dict, list, tuple)):
            clean.append(stable_hash(p, 24))
        else:
            clean.append(str(p).replace(" ", "_"))
    return ":".join(clean)
