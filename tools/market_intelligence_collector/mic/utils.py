"""Shared helpers: IDs, hashing, URL canonicalization, time parsing."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def new_id(prefix: str) -> str:
    """Stable, readable id with a short random suffix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


# Query params that rarely change document identity and add noise to dedup.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "ref", "source", "fr", "scene",
}


def canonicalize_url(url: str) -> str:
    """Normalize a URL for dedup: drop fragment, tracking params, trailing slash."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    query_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", query, ""))


def domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


_WS_RE = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def simhash(text: str, bits: int = 64) -> str:
    """Lightweight simhash over character 3-grams for near-duplicate detection."""
    text = normalize_ws(text)
    if not text:
        return "0" * (bits // 4)
    grams = [text[i:i + 3] for i in range(max(len(text) - 2, 1))]
    vector = [0] * bits
    for gram in grams:
        h = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vector[i] > 0:
            out |= (1 << i)
    return f"{out:0{bits // 4}x}"


def hamming_hex(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return 64


def parse_time_window_days(window: str | None) -> int | None:
    """'30d' -> 30, '12w' -> 84, '6m' -> 180, '1y' -> 365. None if not parseable."""
    if not window:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([dwmy])\s*", window.lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"d": 1, "w": 7, "m": 30, "y": 365}[unit]
