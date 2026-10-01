"""Page fetchers returning the unified ``FetchResult`` (design 11.1).

``HttpFetcher`` wraps the legacy httpx GET; ``BrowserFetcher`` navigates in
the dedicated browser session. Both return the same structure so the
LinkReader runs *one* parsing chain (HTML/PDF decision, anti-bot check,
strict article scope, passage selection, hash) regardless of transport.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from mic.browser.contracts import FetchResult

NO_RETRY_HTTP_STATUSES = {404, 410, 451}
UNSUPPORTED_BROWSER_CONTENT = ("application/pdf", "application/octet-stream", "application/zip")


class HttpFetcher:
    transport = "http"

    def __init__(self, timeout: float = 15, user_agent: str = "MIC/0.3",
                 getter: Callable[..., Any] | None = None):
        self.timeout = timeout
        self.user_agent = user_agent
        self._get = getter or httpx.get

    def fetch(self, url: str) -> FetchResult:
        start = time.monotonic()
        try:
            resp = self._get(url, timeout=self.timeout, follow_redirects=True,
                             headers={"User-Agent": self.user_agent})
        except httpx.TimeoutException as exc:
            return FetchResult(transport="http", requested_url=url, blocked_reason="timeout",
                               error=f"{type(exc).__name__}", counted_as="http_read_attempts",
                               elapsed_ms=int((time.monotonic() - start) * 1000))
        except (httpx.HTTPError, OSError) as exc:
            return FetchResult(transport="http", requested_url=url, blocked_reason="network_error",
                               error=f"{type(exc).__name__}: {str(exc)[:160]}", counted_as="http_read_attempts",
                               elapsed_ms=int((time.monotonic() - start) * 1000))
        elapsed = int((time.monotonic() - start) * 1000)
        ctype = resp.headers.get("content-type", "")
        final_url = str(resp.url)
        if resp.status_code != 200:
            return FetchResult(transport="http", requested_url=url, final_url=final_url,
                               http_status=resp.status_code, content_type=ctype, blocked_reason="http_status",
                               error=f"http_{resp.status_code}", elapsed_ms=elapsed, counted_as="http_read_attempts")
        looks_pdf = ("pdf" in ctype.lower() or final_url.lower().split("?")[0].endswith(".pdf"))
        if looks_pdf and b"%PDF" in resp.content[:1024]:
            return FetchResult(transport="http", requested_url=url, final_url=final_url, http_status=200,
                               content_type=ctype or "application/pdf", content=resp.content,
                               elapsed_ms=elapsed, counted_as="http_read_attempts")
        return FetchResult(transport="http", requested_url=url, final_url=final_url, http_status=200,
                           content_type=ctype, html=resp.text, elapsed_ms=elapsed,
                           counted_as="http_read_attempts")


class BrowserFetcher:
    """Navigate with the shared browser session and return the rendered DOM.

    Visibility diagnostics (title, body text length, viewport) are recorded so
    the caller can tell a blank render from a real article; the decision about
    article scope still belongs to the strict parser downstream.
    """

    transport = "browser"

    def __init__(self, session, clock: Callable[[], float] = time.monotonic,
                 settle_seconds: float = 1.0):
        self.session = session
        self.clock = clock
        self.settle_seconds = settle_seconds

    def fetch(self, url: str, timeout_seconds: float) -> FetchResult:
        start = self.clock()
        auth = self.session.auth_context()
        base = {"transport": "browser", "requested_url": url, "auth_mode": auth["auth_mode"],
                "auth_context_id": auth["auth_context_id"], "counted_as": "browser_read_attempts"}
        with self.session.page() as page:
            nav = self.session.navigate(page, url, timeout_seconds)
            if nav["status"] != "navigated":
                return FetchResult(**base, final_url=nav.get("final_url"), blocked_reason=nav["status"],
                                   error=nav.get("error"), elapsed_ms=nav.get("elapsed_ms", 0))
            http_status = nav.get("http_status")
            if http_status is not None and http_status != 200:
                return FetchResult(**base, final_url=page.url, http_status=http_status,
                                   blocked_reason="http_status", error=f"http_{http_status}",
                                   elapsed_ms=int((self.clock() - start) * 1000))
            # Bounded settle for client-side rendered bodies. Never networkidle.
            remaining = timeout_seconds - (self.clock() - start)
            if remaining > 0 and self.settle_seconds > 0:
                try:
                    page.wait_for_load_state("load", timeout=int(min(remaining, self.settle_seconds) * 1000))
                except Exception:  # noqa: BLE001 - settle is best-effort
                    pass
            try:
                final_url = page.url
                title = page.title()
                html = page.content()
                visibility = self._visibility(page)
            except Exception as exc:  # noqa: BLE001
                return FetchResult(**base, blocked_reason="browser_closed",
                                   error=f"{type(exc).__name__}: {str(exc)[:160]}",
                                   elapsed_ms=int((self.clock() - start) * 1000))
        ctype = self._content_type(html)
        if any(c in (ctype or "") for c in UNSUPPORTED_BROWSER_CONTENT) or \
                final_url.lower().split("?")[0].endswith(".pdf"):
            # Browser PDF viewer text is not treated as HTML (design 11.2).
            return FetchResult(**base, final_url=final_url, http_status=http_status, content_type=ctype,
                               blocked_reason="unsupported_content", error="browser_pdf_unsupported",
                               visibility=visibility, browser_title=title,
                               elapsed_ms=int((self.clock() - start) * 1000))
        return FetchResult(**base, final_url=final_url, http_status=http_status or 200, content_type=ctype,
                           html=html, browser_title=title, visibility=visibility,
                           elapsed_ms=int((self.clock() - start) * 1000))

    @staticmethod
    def _visibility(page) -> dict[str, Any]:
        try:
            return page.evaluate(
                "() => ({body_text_length: (document.body && document.body.innerText || '').length,"
                " viewport_w: window.innerWidth, viewport_h: window.innerHeight,"
                " ready_state: document.readyState, hidden: document.hidden === true})")
        except Exception:  # noqa: BLE001
            return {"body_text_length": None}

    @staticmethod
    def _content_type(html: str | None) -> str | None:
        if html is None:
            return None
        head = html[:400].lower()
        if "<embed" in head and "application/pdf" in head:
            return "application/pdf"
        return "text/html"
