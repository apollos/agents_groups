"""`mic browser setup` - interactive preparation of the dedicated profile.

Opens the MIC Edge profile (same lock, same lifecycle as a run) on an engine
home page or an allowed URL so the operator can log in / clear consent pages
by normal browsing. Nothing is scraped, no cookie values are read or printed;
the browser is closed normally so the persistent profile keeps its state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from mic.browser.config import ConfigError, resolve_profile_dir
from mic.browser.session import BrowserSession

ENGINE_HOME = {
    "bing": "https://www.bing.com/",
    "google": "https://www.google.com/",
    "baidu": "https://www.baidu.com/",
}


def allowed_setup_hosts(config) -> set[str]:
    runtime = config.browser_runtime
    hosts = {urlparse(u).hostname for u in ENGINE_HOME.values()}
    sf = runtime.get("session_fallback") or {}
    for origin in sf.get("allowed_origins") or []:
        host = urlparse(origin).hostname
        if host:
            hosts.add(host)
    for host in runtime.get("setup_allowed_hosts") or []:
        hosts.add(str(host).lower())
    return {h.lower() for h in hosts if h}


def resolve_setup_url(config, engine: str | None, url: str | None) -> str:
    if bool(engine) == bool(url):
        raise ConfigError("browser setup: pass exactly one of --engine or --url")
    if engine:
        if engine not in ENGINE_HOME:
            raise ConfigError(f"browser setup: unknown engine {engine!r}")
        return ENGINE_HOME[engine]
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError("browser setup: --url must be an http(s) URL")
    host = parsed.hostname.lower()
    allowed = allowed_setup_hosts(config)
    if not any(host == a or host.endswith("." + a) for a in allowed):
        raise ConfigError(
            f"browser setup: host {host!r} is not an engine or session_fallback.allowed_origins host; "
            "add it to browser_runtime.session_fallback.allowed_origins or setup_allowed_hosts")
    return url or ""


def _wait_until_closed(page, session, *, deadline: float, poll_seconds: float = 0.5) -> str:
    """Return why waiting stopped: ``closed`` (window/page gone), ``cap`` or ``error``."""
    while time.monotonic() < deadline:
        if page.is_closed() or not session.started:
            return "closed"
        try:
            page.wait_for_event("close", timeout=poll_seconds * 1000)
            return "closed"
        except Exception as exc:  # noqa: BLE001 - TimeoutError keeps polling; anything else = gone
            if type(exc).__name__ == "TimeoutError" or "Timeout" in type(exc).__name__:
                continue
            return "error"
    return "cap"


def run_setup(config, *, engine: str | None = None, url: str | None = None,
              wait: Callable[[], None] | None = None, max_seconds: float = 1800,
              env: dict[str, str] | None = None) -> dict[str, Any]:
    """Open the dedicated profile for manual interaction. Returns lifecycle diagnostics."""
    runtime = config.browser_runtime
    if not runtime.get("enabled"):
        raise ConfigError("browser_runtime.enabled is false; enable it before `browser setup`")
    target = resolve_setup_url(config, engine, url)
    profile_dir = resolve_profile_dir(runtime, env)
    session = BrowserSession(runtime=runtime, run_id="setup", attempt_id="setup", profile_dir=profile_dir)
    start = time.monotonic()
    session.start()
    try:
        with session.page() as page:
            nav = session.navigate(page, target, 30)
            if wait is not None:
                wait()
            else:
                # Block until the operator closes the window or the cap elapses. The sync
                # Playwright API only dispatches events (page/context "close") while a
                # Playwright call is in flight, so poll with wait_for_event rather than sleep;
                # a persistent context has no ``browser`` object to ask about connectivity.
                _wait_until_closed(page, session, deadline=start + max_seconds)
    finally:
        diag = session.close()
    return {"url": target, "profile_id": profile_dir.name, "navigation": nav.get("status"),
            "elapsed_seconds": round(time.monotonic() - start, 1), **diag}
