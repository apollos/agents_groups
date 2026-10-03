"""Browser session lifecycle (design section 8).

``BrowserSession`` owns one persistent Edge context for one run: dedicated
profile directory, application-level mutex, lazy start, bounded page count,
explicit close with cleanup diagnostics. Playwright is imported lazily inside
``PlaywrightBackend`` so the rest of MIC never depends on it.

Backends are pluggable so the lifecycle can be exercised with a test double.
A backend only needs ``launch()`` returning an object with ``new_page()``,
``pages``, ``close()``, ``add_cookies()``, ``clear_cookies()`` and ``cookies()``;
pages need ``goto()``, ``content()``, ``url``, ``title()``, ``inner_text()``,
``close()``, ``is_closed()``, ``wait_for_selector()``.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mic.browser.config import ConfigError, resolve_profile_dir
from mic.browser.profile_lock import ProfileBusy, ProfileLock, ensure_private_dir
from mic.logging_utils import get_logger

logger = get_logger("browser.session")


class BrowserUnavailable(RuntimeError):
    """Browser could not start: dependency, GUI, channel or launch failure."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class BrowserClosed(RuntimeError):
    """The window/browser went away during the run (user closed it or crash)."""


def gui_available(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def cookie_domain_within_any(cookie_domain: str, allowed: list[str]) -> bool:
    """True when a cookie's domain attribute (``host``, ``.host`` or ``sub.host``) lies within
    one of the authorised domains - the same scope the import validation applied."""
    host = cookie_domain.lower().lstrip(".")
    if not host:
        return False
    for a in allowed:
        a = str(a).lower().lstrip(".")
        if a and (host == a or host.endswith("." + a)):
            return True
    return False


class PlaywrightBackend:
    """Real backend: sync Playwright persistent context on the Edge channel."""

    def __init__(self) -> None:
        self._pw = None

    @staticmethod
    def available() -> tuple[bool, str | None]:
        try:
            from playwright import sync_api  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, f"playwright not importable: {exc}"
        try:
            from importlib.metadata import version as _dist_version
            version = _dist_version("playwright")
        except Exception:  # noqa: BLE001
            version = "unknown"
        return True, version

    def launch(self, *, user_data_dir: Path, channel: str, headless: bool,
               accept_downloads: bool, chromium_sandbox: bool,
               timeout_ms: int, executable_path: str | None = None,
               locale: str | None = None, viewport: dict[str, int] | None = None):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        kwargs: dict[str, Any] = {
            "user_data_dir": str(user_data_dir), "headless": headless,
            "accept_downloads": accept_downloads, "chromium_sandbox": chromium_sandbox,
            "timeout": timeout_ms, "ignore_default_args": ["--enable-automation"],
        }
        if executable_path:
            kwargs["executable_path"] = executable_path
        else:
            kwargs["channel"] = channel
        if locale:
            kwargs["locale"] = locale
        if viewport:
            kwargs["viewport"] = viewport
        return self._pw.chromium.launch_persistent_context(**kwargs)

    def stop(self) -> None:
        if self._pw is not None:
            try:
                self._pw.stop()
            finally:
                self._pw = None


@dataclass
class BrowserSession:
    runtime: dict[str, Any]
    run_id: str
    attempt_id: str
    profile_dir: Path
    backend: Any = field(default_factory=PlaywrightBackend)
    clock: Callable[[], float] = time.monotonic
    credential_versions: dict[str, str] = field(default_factory=dict)
    _context: Any = field(default=None, repr=False)
    _lock: ProfileLock | None = field(default=None, repr=False)
    _open_pages: int = 0
    started_at: float | None = None
    browser_processes: list[dict[str, int]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=lambda: {
        "navigations": 0, "pages_opened": 0, "pages_closed": 0, "cookies_injected": 0})
    _closed_by_user: bool = False

    # --- lifecycle -------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._context is not None

    def start(self) -> BrowserSession:
        if self._context is not None:
            return self
        if not self.runtime.get("headless", False) and not gui_available():
            raise BrowserUnavailable(
                "gui_unavailable",
                "no DISPLAY/WAYLAND_DISPLAY; the windowed browser route requires the Ubuntu "
                "graphical desktop (headless is not silently substituted)")
        ok, info = self.backend.available() if hasattr(self.backend, "available") else (True, None)
        if not ok:
            raise BrowserUnavailable("dependency_missing",
                                     f"{info}; install with: pip install 'market-intelligence-collector[browser]'")
        ensure_private_dir(self.profile_dir)
        self._lock = ProfileLock(self.profile_dir, self.run_id, self.attempt_id)
        try:
            self._lock.acquire()
        except ProfileBusy:
            self._lock = None
            raise
        timeout_ms = int(float(self.runtime.get("browser_start_timeout_seconds", 20)) * 1000)
        try:
            self._context = self.backend.launch(
                user_data_dir=self.profile_dir,
                channel=self.runtime.get("channel", "msedge"),
                headless=bool(self.runtime.get("headless", False)),
                accept_downloads=bool(self.runtime.get("accept_downloads", False)),
                chromium_sandbox=bool(self.runtime.get("chromium_sandbox", True)),
                timeout_ms=timeout_ms,
                executable_path=self.runtime.get("executable_path") or None,
                locale=self.runtime.get("locale") or None,
                viewport=self.runtime.get("viewport") or None,
            )
        except Exception as exc:  # noqa: BLE001
            self._release_lock()
            try:
                self.backend.stop()
            except Exception:  # noqa: BLE001
                pass
            msg = str(exc)
            code = "browser_launch_failed"
            if "Executable doesn't exist" in msg or "channel" in msg.lower() and "not found" in msg.lower():
                code = "browser_missing"
            raise BrowserUnavailable(code, msg[:400]) from exc
        self.started_at = self.clock()
        # Register the browser's identity (pid / pgid / starttime) for the supervisor while the
        # parent chain worker -> driver -> browser is intact: this, not the profile path, is
        # what makes a process ours (an attempt that lost the profile-lock race must never
        # signal the lock holder's browser).
        try:
            from mic.browser.runner import register_browser_processes
            self.browser_processes = register_browser_processes()
        except Exception:  # noqa: BLE001 - diagnostics only
            self.browser_processes = []
        try:
            self._context.on("close", self._on_context_close)
        except Exception:  # noqa: BLE001 - test doubles may not support events
            pass
        # Persistent contexts open an initial blank tab; keep page accounting honest.
        logger.info("browser_started run_id=%s profile=%s channel=%s headless=%s",
                    self.run_id, self.profile_dir.name, self.runtime.get("channel"),
                    self.runtime.get("headless"))
        return self

    def _on_context_close(self, *_: Any) -> None:
        self._closed_by_user = True

    def _release_lock(self) -> None:
        if self._lock is not None:
            try:
                self._lock.release()
            finally:
                self._lock = None

    def close(self) -> dict[str, Any]:
        """Close pages, context and Playwright; always release the profile lock.

        Returns cleanup diagnostics; ``cleanup_incomplete`` is reported rather
        than hidden when something could not be closed.
        """
        diag: dict[str, Any] = {"browser_started": self._context is not None,
                                "closed_by_user": self._closed_by_user, **self.stats}
        problems: list[str] = []
        ctx, self._context = self._context, None
        if ctx is not None:
            try:
                for page in list(getattr(ctx, "pages", []) or []):
                    try:
                        page.close()
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"page_close: {type(exc).__name__}")
                ctx.close()
            except Exception as exc:  # noqa: BLE001
                if not self._closed_by_user:
                    problems.append(f"context_close: {type(exc).__name__}")
        try:
            self.backend.stop()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"backend_stop: {type(exc).__name__}")
        self._release_lock()
        diag["cleanup"] = "complete" if not problems else "cleanup_incomplete"
        diag["problems"] = problems
        return diag

    # --- pages -----------------------------------------------------------

    def _require_context(self):
        if self._context is None:
            raise BrowserClosed("browser session is not started")
        if self._closed_by_user:
            raise BrowserClosed("browser window was closed")
        return self._context

    @contextmanager
    def page(self):
        """Open a tab for one task; always closed afterwards."""
        ctx = self._require_context()
        max_pages = int(self.runtime.get("max_open_pages", 2))
        if self._open_pages >= max_pages:
            raise RuntimeError(f"max_open_pages={max_pages} reached")
        try:
            page = ctx.new_page()
        except Exception as exc:  # noqa: BLE001
            raise BrowserClosed(f"cannot open page: {exc}") from exc
        self._open_pages += 1
        self.stats["pages_opened"] += 1
        try:
            yield page
        finally:
            self._open_pages -= 1
            try:
                if not page.is_closed():
                    page.close()
                self.stats["pages_closed"] += 1
            except Exception:  # noqa: BLE001
                pass

    def navigate(self, page, url: str, timeout_seconds: float) -> dict[str, Any]:
        """Navigate with a bounded timeout. Returns {status, final_url, elapsed_ms, error}."""
        if not url.lower().startswith(("http://", "https://")):
            return {"status": "rejected", "error": "non_http_url", "final_url": None, "elapsed_ms": 0}
        start = self.clock()
        self.stats["navigations"] += 1
        try:
            resp = page.goto(url, timeout=max(1, int(timeout_seconds * 1000)),
                             wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if self._closed_by_user or "closed" in str(exc).lower():
                self._closed_by_user = True
                status = "browser_closed"
            elif "Timeout" in name or "timeout" in str(exc).lower():
                status = "timeout"
            else:
                status = "network_error"
            return {"status": status, "error": f"{name}: {str(exc)[:200]}",
                    "final_url": None, "elapsed_ms": int((self.clock() - start) * 1000)}
        http_status = getattr(resp, "status", None) if resp is not None else None
        return {"status": "navigated", "http_status": http_status, "final_url": page.url,
                "elapsed_ms": int((self.clock() - start) * 1000), "error": None}

    # --- cookies / auth ----------------------------------------------------

    def add_cookies(self, cookies: list[dict[str, Any]], credential_id: str, version: str) -> None:
        ctx = self._require_context()
        ctx.add_cookies(cookies)
        self.stats["cookies_injected"] += len(cookies)
        self.credential_versions[credential_id] = version

    def clear_cookies_for_domains(self, domains: list[str]) -> int:
        """Remove every cookie whose *actual* domain falls within one of ``domains``.

        Playwright's ``clear_cookies(domain=str)`` is an exact match, so ``news.example.com``
        would leave legitimately imported ``.news.example.com`` / ``a.news.example.com``
        cookies behind (review). We therefore enumerate the profile's cookies, select those
        within scope (host-only or domain cookies, any subdomain) and clear each by
        name/domain/path. Returns the number of cookies removed; values are never logged.
        """
        ctx = self._require_context()
        try:
            cookies = ctx.cookies()
        except Exception:  # noqa: BLE001 - fall back to the coarse clear below
            cookies = None
        if cookies is None:
            ctx.clear_cookies()
            return 0
        targets = [c for c in cookies if cookie_domain_within_any(str(c.get("domain", "")), domains)]
        if not targets:
            return 0
        try:
            for c in targets:
                ctx.clear_cookies(name=c.get("name"), domain=c.get("domain"), path=c.get("path"))
        except TypeError:
            # Older Playwright without filter support: clear all cookies in the
            # dedicated profile (never touches the user's daily browser).
            ctx.clear_cookies()
            return len(targets)
        # Verify instead of trusting the call: count what is actually gone.
        try:
            remaining = ctx.cookies()
        except Exception:  # noqa: BLE001
            return len(targets)
        left = sum(1 for c in remaining if cookie_domain_within_any(str(c.get("domain", "")), domains))
        return max(0, len(targets) - left)

    def auth_context(self) -> dict[str, Any]:
        mode = "imported_cookie" if self.credential_versions else "profile"
        payload = f"{self.profile_dir.resolve()}|" + "|".join(
            f"{k}={v}" for k, v in sorted(self.credential_versions.items()))
        ctx_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return {"auth_mode": mode, "auth_context_id": ctx_id,
                "credential_versions": dict(self.credential_versions)}

    def describe(self) -> dict[str, Any]:
        """Non-sensitive description for reports."""
        return {"profile_id": self.profile_dir.name, "channel": self.runtime.get("channel"),
                "headless": bool(self.runtime.get("headless", False)), "started": self.started,
                **self.auth_context()}


def browser_session_from_runtime(runtime: dict[str, Any], *, run_id: str, attempt_id: str,
                                 backend: Any | None = None,
                                 clock: Callable[[], float] = time.monotonic) -> BrowserSession:
    if not runtime.get("enabled"):
        raise ConfigError("browser_runtime.enabled is false")
    profile_dir = resolve_profile_dir(runtime)
    return BrowserSession(runtime=runtime, run_id=run_id, attempt_id=attempt_id,
                          profile_dir=profile_dir, backend=backend or PlaywrightBackend(),
                          clock=clock)
