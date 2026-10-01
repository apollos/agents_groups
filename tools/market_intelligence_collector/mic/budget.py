"""Single run budget (design section 9).

Every outward operation - a search page navigation, an HTTP body fetch, a
browser body navigation, an authenticated session retry, a model request
actually sent to the gateway - must be *reserved* here before it is attempted.
Failed operations still count; operations that were never sent do not.

The effective limit of each counter is the minimum of the task request, the
deployment ceiling and - for time - the remaining wall clock. Nothing in page
content, model output or fallback logic may raise a limit.

The clock is injectable so budget/deadline behaviour is tested without sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Counter names are the public reporting contract (summary.budget_used).
COUNTERS = (
    "queries_attempted",
    "queries_completed",
    "search_page_attempts",
    "search_hits",
    "links_selected_for_read",
    "http_read_attempts",
    "browser_read_attempts",
    "authenticated_retries",
    "model_calls",
    "gateway_requests_sent",
)

# Deployment limit key -> counter it caps. Keys follow browser_runtime.limits.
LIMIT_TO_COUNTER = {
    "max_queries": "queries_attempted",
    "max_search_pages_per_run": "search_page_attempts",
    "max_search_hits": "search_hits",
    "max_links_to_read": "links_selected_for_read",
    "max_http_read_attempts": "http_read_attempts",
    "max_browser_read_attempts": "browser_read_attempts",
    "max_authenticated_retries_per_run": "authenticated_retries",
    "max_model_calls": "model_calls",
    "max_gateway_requests": "gateway_requests_sent",
}

# Limits that are not plain run counters but still part of the budget.
SCOPED_LIMITS = (
    "max_search_pages_per_query",
    "max_engines_per_query",
    "max_hits_per_query",
    "results_per_page_cap",
    "max_authenticated_retries_per_origin",
    "max_run_seconds",
    "max_page_seconds",
    "max_retries_per_page",
    "min_engine_interval_seconds",
)

DEFAULT_LIMITS: dict[str, int] = {
    "max_queries": 2,
    "max_search_pages_per_query": 3,
    "max_search_pages_per_run": 6,
    "max_engines_per_query": 2,
    "results_per_page_cap": 10,
    "max_hits_per_query": 30,
    "max_search_hits": 60,
    "max_links_to_read": 2,
    "max_http_read_attempts": 2,
    "max_browser_read_attempts": 2,
    "max_authenticated_retries_per_origin": 1,
    "max_authenticated_retries_per_run": 1,
    "max_model_calls": 3,
    "max_gateway_requests": 3,
    "max_run_seconds": 300,
    "max_page_seconds": 25,
    "min_engine_interval_seconds": 3,
    "max_retries_per_page": 0,
}


class BudgetExceeded(RuntimeError):
    """Raised when an operation is requested without remaining budget."""

    def __init__(self, counter: str, limit: int | float, used: int | float):
        super().__init__(f"budget exhausted: {counter} used={used} limit={limit}")
        self.counter = counter
        self.limit = limit
        self.used = used


def merge_limits(deployment: dict[str, Any] | None,
                 task: dict[str, Any] | None) -> dict[str, int]:
    """Effective limits = min(deployment ceiling, task request).

    Task profiles may only tighten; unknown task keys are ignored; keys missing
    from both fall back to the conservative defaults above.
    """
    merged: dict[str, int] = {}
    deployment = deployment or {}
    task = task or {}
    for key, default in DEFAULT_LIMITS.items():
        dep = deployment.get(key, default)
        value = dep
        if key in task and task[key] is not None:
            value = min(dep, task[key])
        merged[key] = int(value)
    return merged


@dataclass
class RunBudget:
    """Counts and gates every budgeted operation for one run.

    ``clock`` must be a monotonic seconds function; ``started_at`` is sampled
    from it on construction so ``remaining_seconds`` is wall-clock based.
    """

    limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    clock: Callable[[], float] = time.monotonic
    started_at: float | None = None
    used: dict[str, int] = field(default_factory=lambda: {c: 0 for c in COUNTERS})
    # Per-origin authenticated retry counts (origin -> n).
    auth_retries_by_origin: dict[str, int] = field(default_factory=dict)
    # Per-query page attempt counts (query_id -> n) and engines tried.
    pages_by_query: dict[str, int] = field(default_factory=dict)
    engines_by_query: dict[str, set[str]] = field(default_factory=dict)
    hits_by_query: dict[str, int] = field(default_factory=dict)
    cancelled: bool = False
    cancel_reason: str | None = None
    # Explicit deadline (monotonic seconds) handed down by a supervisor; when
    # set it wins over max_run_seconds.
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()
        for c in COUNTERS:
            self.used.setdefault(c, 0)
        if self.deadline_at is None:
            self.deadline_at = self.started_at + float(self.limits.get("max_run_seconds", 300))
        else:
            # A supervisor deadline may only shorten the run.
            self.deadline_at = min(self.deadline_at,
                                   self.started_at + float(self.limits.get("max_run_seconds", 300)))

    # --- time / cancellation -------------------------------------------

    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - (self.started_at or 0.0))

    def remaining_seconds(self) -> float:
        return max(0.0, (self.deadline_at or 0.0) - self.clock())

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0.0

    def cancel(self, reason: str = "cancelled") -> None:
        self.cancelled = True
        self.cancel_reason = reason

    def check_alive(self) -> None:
        """Raise when the run must stop (cancelled or past deadline)."""
        if self.cancelled:
            raise BudgetExceeded("cancelled", 0, 0)
        if self.expired():
            raise BudgetExceeded("max_run_seconds", self.limits.get("max_run_seconds", 0),
                                 round(self.elapsed_seconds(), 1))

    def page_timeout_seconds(self) -> float:
        """Per-page timeout bounded by the remaining run time."""
        return max(0.0, min(float(self.limits.get("max_page_seconds", 25)),
                            self.remaining_seconds()))

    # --- generic counters ------------------------------------------------

    def limit_for(self, counter: str) -> int | None:
        for key, c in LIMIT_TO_COUNTER.items():
            if c == counter:
                return int(self.limits.get(key, DEFAULT_LIMITS[key]))
        return None

    def can(self, counter: str, n: int = 1) -> bool:
        if self.cancelled or self.expired():
            return False
        limit = self.limit_for(counter)
        if limit is None:
            return True
        return self.used.get(counter, 0) + n <= limit

    def reserve(self, counter: str, n: int = 1) -> None:
        """Reserve ``n`` units *before* the operation; raises if not allowed."""
        self.check_alive()
        limit = self.limit_for(counter)
        if limit is not None and self.used.get(counter, 0) + n > limit:
            raise BudgetExceeded(counter, limit, self.used.get(counter, 0))
        self.used[counter] = self.used.get(counter, 0) + n

    def record(self, counter: str, n: int = 1) -> None:
        """Record an un-gated statistic (e.g. queries_completed)."""
        self.used[counter] = self.used.get(counter, 0) + n

    # --- search-specific reservations ----------------------------------

    def can_open_search_page(self, query_id: str, engine: str) -> tuple[bool, str | None]:
        if self.cancelled:
            return False, "cancelled"
        if self.expired():
            return False, "run_deadline"
        if not self.can("search_page_attempts"):
            return False, "max_search_pages_per_run"
        per_q = int(self.limits.get("max_search_pages_per_query", 3))
        if self.pages_by_query.get(query_id, 0) + 1 > per_q:
            return False, "max_search_pages_per_query"
        engines = self.engines_by_query.setdefault(query_id, set())
        max_engines = int(self.limits.get("max_engines_per_query", 2))
        if engine not in engines and len(engines) + 1 > max_engines:
            return False, "max_engines_per_query"
        return True, None

    def reserve_search_page(self, query_id: str, engine: str) -> None:
        ok, reason = self.can_open_search_page(query_id, engine)
        if not ok:
            raise BudgetExceeded(reason or "search_page_attempts",
                                 self.limits.get(reason or "", 0),
                                 self.pages_by_query.get(query_id, 0))
        self.reserve("search_page_attempts")
        self.pages_by_query[query_id] = self.pages_by_query.get(query_id, 0) + 1
        self.engines_by_query.setdefault(query_id, set()).add(engine)

    def can_accept_hit(self, query_id: str) -> bool:
        if not self.can("search_hits"):
            return False
        return self.hits_by_query.get(query_id, 0) + 1 <= int(
            self.limits.get("max_hits_per_query", 30))

    def accept_hit(self, query_id: str) -> None:
        self.reserve("search_hits")
        self.hits_by_query[query_id] = self.hits_by_query.get(query_id, 0) + 1

    # --- authenticated retry ---------------------------------------------

    def can_authenticated_retry(self, origin: str) -> tuple[bool, str | None]:
        if not self.can("authenticated_retries"):
            return False, "max_authenticated_retries_per_run"
        per_origin = int(self.limits.get("max_authenticated_retries_per_origin", 1))
        if self.auth_retries_by_origin.get(origin, 0) + 1 > per_origin:
            return False, "max_authenticated_retries_per_origin"
        return True, None

    def reserve_authenticated_retry(self, origin: str) -> None:
        ok, reason = self.can_authenticated_retry(origin)
        if not ok:
            raise BudgetExceeded(reason or "authenticated_retries", 1,
                                 self.auth_retries_by_origin.get(origin, 0))
        self.reserve("authenticated_retries")
        self.auth_retries_by_origin[origin] = self.auth_retries_by_origin.get(origin, 0) + 1

    # --- reporting -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "limits": dict(self.limits),
            "used": {c: int(self.used.get(c, 0)) for c in COUNTERS},
            "elapsed_seconds": round(self.elapsed_seconds(), 1),
            "remaining_seconds": round(self.remaining_seconds(), 1),
            "cancelled": self.cancelled,
            "cancel_reason": self.cancel_reason,
        }

    def used_summary(self) -> dict[str, int]:
        return {c: int(self.used.get(c, 0)) for c in COUNTERS}
