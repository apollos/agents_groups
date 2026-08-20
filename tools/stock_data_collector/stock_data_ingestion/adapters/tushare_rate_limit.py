"""Cross-process rate limiting for the Tushare token quota.

Tushare enforces account-tier quotas per token (current tier, checked
2026-08-20: 200 calls/minute, 100,000 calls/day per API), plus stricter
per-API caps on some endpoints (stk_mins). The quota is shared by every
process using the token — the agent CLI, manual runs, capability checks —
so counters live in flocked files under a shared state directory instead
of process memory.

All limits are enforced at (cap - safety_margin): with the default margin
of 1 we stop one call short of the vendor cap and never trigger it.
Configured in config/data_sources.yaml under providers.tushare.rate_limit;
adjust the numbers there when the account tier changes.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai

logger = logging.getLogger(__name__)


class DailyQuotaExceeded(RuntimeError):
    """Raised instead of making a call that would exceed the per-day API cap."""


def _state_dir() -> Path:
    # Overridable so tests never touch the real home directory.
    return Path(os.environ.get("STOCK_DATA_PACER_DIR") or "~/.cache/stock_data_ingestion").expanduser()


@contextmanager
def _locked(path: Path) -> Iterator[Any]:
    """Open ``path`` for read-modify-write under an exclusive flock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read_key_count(f: Any) -> tuple[str, int]:
    parts = (f.read().strip() or " ").split()
    if len(parts) == 2:
        try:
            return parts[0], int(parts[1])
        except ValueError:
            pass
    return "", 0


def _write_key_count(f: Any, key: str, count: int) -> None:
    f.seek(0)
    f.truncate()
    f.write(f"{key} {count}")
    f.flush()


def pace_min_interval(api: str, min_interval_seconds: float) -> None:
    """Enforce a minimum interval between calls to a per-token rate-limited API.

    Some endpoints are capped harder than the account-wide quota (observed
    2026-08-20: stk_mins allowed 1 call/minute, then 1/hour after repeats).
    The last-call timestamp lives in a file shared by all processes on this
    machine, because callers like `tools verify-capabilities` invoke the CLI
    once per frequency — separate processes — where an in-process sleep
    would not help.
    """
    if min_interval_seconds <= 0:
        return
    state = _state_dir() / f"pacer_{api}"
    try:
        last = float(state.read_text().strip())
    except (OSError, ValueError):
        last = 0.0
    wait = last + float(min_interval_seconds) - time.time()
    if wait > 0:
        logger.info("pacing %s: sleeping %.1fs (min interval %.0fs per token tier)", api, wait, min_interval_seconds)
        time.sleep(wait)
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(str(time.time()))
    except OSError:
        # Pacing is best-effort: an unwritable state dir must not break the fetch.
        logger.warning("cannot persist pacer state at %s", state)


class TushareRateLimiter:
    """Guard every Tushare call against the account-tier quota, cross-process.

    ``acquire(api)`` must be called once before each API call. It enforces,
    in order:

    1. the per-API minimum interval (``min_interval_seconds``, e.g. stk_mins);
    2. the per-minute call budget (sleeps into the next minute window when
       the current window is exhausted);
    3. the per-day per-API cap (raises :class:`DailyQuotaExceeded` rather
       than making a call that would cross it — quota resets next day, so
       burning the last call is never worth a failed response).
    """

    def __init__(
        self,
        *,
        requests_per_minute: int = 0,
        requests_per_day_per_api: int = 0,
        safety_margin: int = 1,
        min_interval_seconds: dict[str, Any] | None = None,
    ) -> None:
        margin = max(0, int(safety_margin))
        rpm = int(requests_per_minute or 0)
        rpd = int(requests_per_day_per_api or 0)
        self.minute_budget = max(0, rpm - margin) if rpm > 0 else 0  # 0 = unlimited
        self.day_budget = max(0, rpd - margin) if rpd > 0 else 0  # 0 = unlimited
        self.min_intervals: dict[str, float] = {
            str(api): float(seconds) for api, seconds in (min_interval_seconds or {}).items()
        }

    @classmethod
    def from_rate_limit_config(cls, rate_limit: dict[str, Any] | None) -> "TushareRateLimiter":
        cfg = rate_limit or {}
        return cls(
            requests_per_minute=int(cfg.get("requests_per_minute") or 0),
            requests_per_day_per_api=int(cfg.get("requests_per_day_per_api") or 0),
            safety_margin=int(cfg.get("safety_margin", 1)),
            min_interval_seconds=cfg.get("min_interval_seconds") or {},
        )

    def acquire(self, api: str) -> None:
        interval = self.min_intervals.get(api, 0.0)
        if interval > 0:
            pace_min_interval(api, interval)
        self._acquire_minute_slot()
        self._count_daily_call(api)

    def _acquire_minute_slot(self) -> None:
        if self.minute_budget <= 0:
            return
        state = _state_dir() / "tushare_minute_window"
        while True:
            with _locked(state) as f:
                window, count = _read_key_count(f)
                now = time.time()
                current = str(int(now // 60))
                if window != current:
                    window, count = current, 0
                if count < self.minute_budget:
                    _write_key_count(f, window, count + 1)
                    return
                wait = (int(now // 60) + 1) * 60 - now
            # Lock released before sleeping so other processes are not blocked.
            logger.info(
                "tushare minute budget exhausted (%d calls); sleeping %.1fs into next window",
                self.minute_budget,
                wait,
            )
            time.sleep(wait)

    def _count_daily_call(self, api: str) -> None:
        if self.day_budget <= 0:
            return
        state = _state_dir() / f"tushare_day_{api}"
        today = now_asia_shanghai().strftime("%Y%m%d")
        with _locked(state) as f:
            day, count = _read_key_count(f)
            if day != today:
                day, count = today, 0
            if count >= self.day_budget:
                raise DailyQuotaExceeded(
                    f"tushare daily quota guard: {api} already made {count} calls today "
                    f"(budget {self.day_budget} = cap - safety margin); refusing to exceed. "
                    "Quota resets at the next Beijing calendar day."
                )
            _write_key_count(f, day, count + 1)


class RateLimitedProApi:
    """Proxy around ``tushare.pro_api()`` that acquires a quota slot per call.

    Wrapping the client object means every endpoint the adapter uses —
    ``pro.daily``, ``pro.hk_daily``, ``getattr(pro, endpoint)`` — is guarded
    without touching each call site.
    """

    def __init__(self, pro: Any, limiter: TushareRateLimiter) -> None:
        self._pro = pro
        self._limiter = limiter

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._pro, name)
        if not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._limiter.acquire(name)
            return attr(*args, **kwargs)

        return wrapper
