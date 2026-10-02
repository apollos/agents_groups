"""Per-run execution context (design section 5.1).

Carries identity, budget, deadline/cancellation, the lazily-started browser
session and the attempt recorder through search and reading. It is created in
the process that executes the run; Playwright objects, DB sessions and HTTP
clients are never handed across process boundaries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mic.budget import RunBudget
from mic.utils import new_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mic.browser.session import BrowserSession

TICKER_RE = re.compile(r"\d{5,6}(?:\.(?:SZ|SH|HK|BJ))?", re.IGNORECASE)


@dataclass
class TargetIdentity:
    """Target identity terms, kept apart from supply-chain association terms."""

    target_id: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    official_domains: list[str] = field(default_factory=list)
    # Association terms (customers / suppliers / competitors / industry words)
    # never count as a target identity match.
    association_terms: list[str] = field(default_factory=list)

    @classmethod
    def from_profile(cls, profile) -> TargetIdentity:
        raw = getattr(profile, "raw", {}) or {}
        tickers = [str(t) for t in (raw.get("tickers") or raw.get("ticker_symbols") or [])]
        if raw.get("ticker"):
            tickers.append(str(raw["ticker"]))
        aliases = []
        for a in profile.aliases or []:
            if not a or a == profile.canonical_name:
                continue
            # Pure codes listed as aliases ("300750", "00700.HK") are tickers:
            # they need context before they count as an identity match.
            if TICKER_RE.fullmatch(str(a).strip()):
                tickers.append(str(a).strip())
            else:
                aliases.append(a)
        return cls(
            target_id=profile.target_id,
            canonical_name=profile.canonical_name,
            aliases=aliases,
            tickers=[t for t in dict.fromkeys(tickers) if t],
            official_domains=[d.lower() for d in (raw.get("official_domains") or [])],
            association_terms=[t for t in dict.fromkeys(
                [*profile.customers, *profile.suppliers, *profile.competitors,
                 *profile.products, *profile.upstream_terms, *profile.downstream_terms]) if t],
        )

    def identity_terms(self) -> list[str]:
        return [t for t in dict.fromkeys([self.canonical_name, *self.aliases]) if t]


class AttemptRecorder:
    """Pluggable sink for search/read attempt records.

    The pipeline wires this to the repository; probes and tests use the
    in-memory default. Recording must never raise into the caller.
    """

    def __init__(self) -> None:
        self.page_attempts: list[dict[str, Any]] = []
        self.read_attempts: list[dict[str, Any]] = []
        self._on_page_start: Callable[[dict[str, Any]], str | None] | None = None
        self._on_page_finish: Callable[[str | None, dict[str, Any]], None] | None = None

    def bind(self, on_page_start: Callable[[dict[str, Any]], str | None] | None,
             on_page_finish: Callable[[str | None, dict[str, Any]], None] | None) -> None:
        self._on_page_start = on_page_start
        self._on_page_finish = on_page_finish

    def page_started(self, record: dict[str, Any]) -> str | None:
        self.page_attempts.append(dict(record, state="attempting"))
        if self._on_page_start is None:
            return None
        try:
            return self._on_page_start(record)
        except Exception:  # noqa: BLE001 - diagnostics must not kill the run
            return None

    def page_finished(self, handle: str | None, record: dict[str, Any]) -> None:
        for i in range(len(self.page_attempts) - 1, -1, -1):
            if self.page_attempts[i].get("page_attempt_id") == record.get("page_attempt_id"):
                self.page_attempts[i] = dict(record, state="finished")
                break
        if self._on_page_finish is None:
            return
        try:
            self._on_page_finish(handle, record)
        except Exception:  # noqa: BLE001
            pass


def config_fingerprint(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class RunContext:
    run_id: str
    attempt_id: str = field(default_factory=lambda: new_id("attempt"))
    config_fingerprint: str = ""
    artifact_dir: Path | None = None
    identity: TargetIdentity | None = None
    budget: RunBudget = field(default_factory=RunBudget)
    recorder: AttemptRecorder = field(default_factory=AttemptRecorder)
    browser_runtime: dict[str, Any] = field(default_factory=dict)
    interaction_mode: str = "unattended"
    # Session fallback / credentials access is provided by a store object with
    # ``credential_for(origin)``; None disables authenticated retries.
    session_store: Any = None
    # Result of clearing expired / revoked managed cookies from the dedicated profile at
    # browser start (domain counts only, never values). None when nothing had to be swept.
    credential_sweep: dict[str, Any] | None = None
    clock: Callable[[], float] = time.monotonic
    _browser: BrowserSession | None = field(default=None, repr=False)
    _browser_factory: Callable[[RunContext], BrowserSession] | None = field(
        default=None, repr=False)
    _owner_pid: int = field(default_factory=os.getpid, repr=False)

    # --- cancellation ----------------------------------------------------

    def cancel(self, reason: str = "cancelled") -> None:
        self.budget.cancel(reason)

    @property
    def cancelled(self) -> bool:
        return self.budget.cancelled

    def check_alive(self) -> None:
        self.budget.check_alive()

    # --- browser session (lazy) ------------------------------------------

    def set_browser_factory(self, factory: Callable[[RunContext], BrowserSession]) -> None:
        self._browser_factory = factory

    @property
    def browser_started(self) -> bool:
        return self._browser is not None

    def browser(self) -> BrowserSession:
        """Start the browser on first use; same-process ownership is enforced."""
        if os.getpid() != self._owner_pid:
            raise RuntimeError("BrowserSession may only be used in the process that owns the run")
        if self._browser is None:
            if self._browser_factory is None:
                from mic.browser.session import BrowserSession, browser_session_from_runtime
                self._browser_factory = lambda ctx: browser_session_from_runtime(
                    ctx.browser_runtime, run_id=ctx.run_id, attempt_id=ctx.attempt_id,
                    clock=ctx.clock)
                assert BrowserSession  # keep the import meaningful for type checkers
            self._browser = self._browser_factory(self)
            self._browser.start()
            self._sweep_stale_credentials(self._browser)
        return self._browser

    def _sweep_stale_credentials(self, browser: BrowserSession) -> None:
        """Clear cookies of expired / revoked managed credentials from the dedicated profile.

        The profile is persistent, so cookies injected by an earlier run would otherwise keep
        authenticating requests after their registry entry expired (review R8). Values are never
        logged; only domain counts are recorded.
        """
        store = self.session_store
        getter = getattr(store, "domains_to_clear", None)
        if getter is None:
            return
        try:
            domains = list(getter())
        except Exception:
            return
        if not domains:
            return
        try:
            cleared = browser.clear_cookies_for_domains(domains)
        except Exception as exc:  # pragma: no cover - diagnostics only
            self.credential_sweep = {"domains": len(domains), "cleared": 0, "error": type(exc).__name__}
            return
        self.credential_sweep = {"domains": len(domains), "cleared": cleared}

    def close(self) -> dict[str, Any]:
        """Release the browser (if started). Returns cleanup diagnostics."""
        if self._browser is None:
            return {"browser_started": False, "cleanup": "not_needed"}
        try:
            return self._browser.close()
        finally:
            self._browser = None

    # --- reporting -------------------------------------------------------

    def auth_context(self) -> dict[str, Any]:
        """Non-sensitive auth context description for diagnostics/cache keys."""
        if self._browser is None:
            return {"auth_mode": "anonymous", "auth_context_id": None}
        return self._browser.auth_context()
