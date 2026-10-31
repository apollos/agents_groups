"""Page fetchers returning the unified ``FetchResult`` (design 11.1).

``HttpFetcher`` wraps the legacy httpx GET; ``BrowserFetcher`` navigates in
the dedicated browser session. Both return the same structure so the
LinkReader runs *one* parsing chain (HTML/PDF decision, anti-bot check,
strict article scope, passage selection, hash) regardless of transport.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4.dammit import EncodingDetector

from mic.browser.contracts import FetchResult

NO_RETRY_HTTP_STATUSES = {404, 410, 451}
UNSUPPORTED_BROWSER_CONTENT = ("application/pdf", "application/octet-stream", "application/zip")
HTML_DECODE_FAILED = "html_decode_failed"

_HEADER_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^\s;\"']+)", re.IGNORECASE)


@dataclass
class DecodedHtml:
    html: str | None
    encoding: str | None = None
    encoding_source: str | None = None  # bom | http_header | html_meta | undeclared_utf8
    error: str | None = None
    tried: list[dict[str, str]] = field(default_factory=list)


def _codec_name(label: str) -> str | None:
    """Python codec for an encoding label, using the WHATWG label table when available."""
    try:
        import webencodings
        encoding = webencodings.lookup(label)
        if encoding is not None:
            return encoding.codec_info.name
    except ImportError:
        pass
    try:
        return codecs.lookup(label).name
    except LookupError:
        return None


def decode_html_bytes(content: bytes, content_type: str | None) -> DecodedHtml:
    """Decode HTML from the raw response bytes.

    Candidates in order: byte-order mark, HTTP ``charset``, HTML ``<meta>``
    declaration, then UTF-8 for an undeclared page. Each is tried strictly and
    the first that decodes the whole body wins. Nothing is guessed from the
    text itself; when no candidate decodes, the result is an explicit error.
    """
    body, bom_encoding = EncodingDetector.strip_byte_order_mark(content)
    candidates: list[tuple[str, str]] = []
    if bom_encoding:
        candidates.append((bom_encoding, "bom"))
    else:
        header = _HEADER_CHARSET.search(content_type or "")
        if header:
            candidates.append((header.group(1), "http_header"))
        declared = EncodingDetector.find_declared_encoding(body, is_html=True)
        if declared:
            candidates.append((declared, "html_meta"))
        candidates.append(("utf-8", "undeclared_utf8"))
    tried: list[dict[str, str]] = []
    for label, source in candidates:
        codec = _codec_name(label)
        if codec is None:
            tried.append({"label": label, "source": source, "result": "unknown_encoding"})
            continue
        try:
            text = body.decode(codec)
        except UnicodeDecodeError:
            tried.append({"label": label, "source": source, "result": "decode_error"})
            continue
        return DecodedHtml(text, encoding=codec, encoding_source=source, tried=tried)
    return DecodedHtml(None, error=HTML_DECODE_FAILED, tried=tried)


def _fetch_debug_enabled() -> bool:
    return os.getenv("MIC_FETCH_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def save_decode_failure_sample(url: str, content_type: str | None, content: bytes,
                               decoded: DecodedHtml) -> str | None:
    """Debug mode only: keep the undecodable bytes of a failing target under logs/."""
    if not _fetch_debug_enabled():
        return None
    from mic.logging_utils import log_dir

    digest = hashlib.sha256(content).hexdigest()
    directory = log_dir() / "fetch_decode_failures"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{digest[:24]}.bin").write_bytes(content)
    (directory / f"{digest[:24]}.json").write_text(json.dumps(
        {"url": url, "content_type": content_type, "sha256": digest, "bytes": len(content),
         "tried": decoded.tried}, ensure_ascii=False, indent=2))
    return digest


def decoded_fetch_result(url: str, final_url: str, ctype: str, content: bytes,
                         elapsed_ms: int | None) -> FetchResult:
    decoded = decode_html_bytes(content, ctype)
    if decoded.error:
        save_decode_failure_sample(url, ctype, content, decoded)
        return FetchResult(transport="http", requested_url=url, final_url=final_url, http_status=200,
                           content_type=ctype, blocked_reason=HTML_DECODE_FAILED,
                           error=";".join(f"{t['source']}={t['label']}:{t['result']}" for t in decoded.tried),
                           elapsed_ms=elapsed_ms, counted_as="http_read_attempts")
    return FetchResult(transport="http", requested_url=url, final_url=final_url, http_status=200,
                       content_type=ctype, html=decoded.html, encoding=decoded.encoding,
                       encoding_source=decoded.encoding_source, elapsed_ms=elapsed_ms,
                       counted_as="http_read_attempts")


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
        return decoded_fetch_result(url, final_url, ctype, resp.content, elapsed)


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
