"""Data contracts for browser search and page fetching (design section 5, 11).

Pure dataclasses / pydantic-free so they can be serialised into diagnostics
and never carry Playwright objects, cookies or authorization headers.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from mic.schemas import SearchHit

PageStatus = Literal[
    "ok", "no_results", "captcha", "login_required", "consent_required",
    "network_error", "timeout", "parse_error", "query_mismatch", "browser_closed",
    "budget_exhausted", "engine_unavailable",
]

PAGE_STATUSES: tuple[str, ...] = (
    "ok", "no_results", "captcha", "login_required", "consent_required",
    "network_error", "timeout", "parse_error", "query_mismatch", "browser_closed",
    "budget_exhausted", "engine_unavailable",
)

# Statuses that indicate a session problem a user-authorised session may fix.
SESSION_BLOCK_STATUSES: frozenset[str] = frozenset({"captcha", "login_required"})

QueryMatchStatus = Literal["exact", "corrected", "unknown", "mismatch"]
UrlResolution = Literal["direct", "decoded", "pending"]
ResultKind = Literal["organic", "ad", "ai_overview", "knowledge_panel",
                     "people_also_ask", "video", "shopping", "news_module",
                     "site_internal", "aggregate", "unknown"]


@dataclass
class RawResult:
    """One parsed result card before it becomes a ``SearchHit``."""

    title: str
    raw_href: str
    url: str | None  # resolved target URL, None when pending
    snippet: str = ""
    display_url: str | None = None
    date_text: str | None = None
    result_kind: str = "organic"
    url_resolution: str = "direct"
    rank_in_page: int = 0


@dataclass
class NextPage:
    """A pagination control confirmed on the current page. Never executable."""

    kind: Literal["link", "load_more"]
    href: str | None = None
    label: str | None = None
    expected_page_index: int | None = None


@dataclass
class SearchPageResult:
    engine: str
    adapter_version: str
    query_requested: str
    page_index: int
    page_attempt_id: str
    requested_url: str
    status: str = "ok"
    final_url: str | None = None
    query_observed: str | None = None
    query_match_status: str = "unknown"
    hits: list[SearchHit] = field(default_factory=list)
    next_page: NextPage | None = None
    page_fingerprint: str | None = None
    relevance: str | None = None  # low | ok | None (not evaluated)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    auth_mode: str = "anonymous"  # anonymous | profile | imported_cookie
    auth_context_id: str | None = None
    authenticated_retry: bool = False

    def to_record(self) -> dict[str, Any]:
        d = asdict(self)
        d["hits"] = [h.model_dump(mode="json") for h in self.hits]
        return d


@dataclass
class SearchBatch:
    """Result of ``search_with_context`` for one query."""

    query: str
    query_family: str | None
    provider: str
    hits: list[SearchHit] = field(default_factory=list)
    page_attempts: list[SearchPageResult] = field(default_factory=list)
    outcome: str = "completed"  # completed | partial | blocked | empty | failed | budget_exhausted
    stop_reason: str | None = None
    quality: dict[str, Any] = field(default_factory=dict)
    # Legacy (non-browser) providers count external API requests here instead
    # of page attempts; they must never be reported as browser pages.
    api_requests: int = 0

    @property
    def pages_opened(self) -> int:
        return len(self.page_attempts)


@dataclass
class SearchRequest:
    query: str
    query_family: str | None = None
    query_id: str | None = None
    limit: int = 10
    language: str | None = None


@dataclass
class FetchResult:
    """Unified HTTP / browser page fetch outcome (design 11.1).

    ``html`` / ``content`` live in memory only and are never persisted.
    """

    transport: str  # http | browser | mock
    requested_url: str
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    html: str | None = None
    content: bytes | None = None
    browser_title: str | None = None
    rendered_text: str | None = None
    visibility: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int | None = None
    blocked_reason: str | None = None  # network_error|timeout|http_status|captcha|login_required|...
    error: str | None = None
    auth_mode: str = "anonymous"
    auth_context_id: str | None = None
    authenticated_retry: bool = False
    counted_as: str | None = None  # http_read_attempts | browser_read_attempts

    @property
    def ok(self) -> bool:
        return self.blocked_reason is None and (self.html is not None or self.content is not None)

    def diagnostics(self) -> dict[str, Any]:
        """Serialisable diagnostics without page content."""
        return {
            "transport": self.transport,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "browser_title": self.browser_title,
            "visibility": dict(self.visibility),
            "elapsed_ms": self.elapsed_ms,
            "blocked_reason": self.blocked_reason,
            "error": self.error,
            "auth_mode": self.auth_mode,
            "auth_context_id": self.auth_context_id,
            "authenticated_retry": self.authenticated_retry,
            "counted_as": self.counted_as,
            "content_length": (len(self.html) if self.html is not None
                               else (len(self.content) if self.content is not None else None)),
        }


def page_fingerprint(urls: list[str]) -> str:
    """Order-sensitive fingerprint of result locators for duplicate-page detection."""
    joined = "\n".join(u.strip() for u in urls)
    return hashlib.sha256(joined.encode("utf-8", errors="ignore")).hexdigest()[:24]
