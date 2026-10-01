"""User-authorised cookie / session fallback (design section 10.2 - 10.3).

Only reads cookie files the user explicitly configured (``cookie_sources``);
never scans or decrypts a daily browser profile. Cookie values live in a
restricted credential directory outside the repository and are never logged,
printed, placed in exceptions, passed to models or stored in business SQLite.
Status output is metadata only.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mic.browser.config import resolve_credential_dir
from mic.browser.profile_lock import ensure_private_dir, write_private_file

FORMAT_V1 = "playwright_cookies_json_v1"

_ALLOWED_COOKIE_KEYS = {"name", "value", "url", "domain", "path", "expires", "httpOnly",
                        "secure", "sameSite", "hostOnly", "partitionKey"}
_SAMESITE = {"Strict", "Lax", "None"}
# Minimal public-suffix guard: never accept cookies scoped to these.
_PUBLIC_SUFFIXES = {
    "com", "cn", "net", "org", "gov", "edu", "io", "co", "info", "biz",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "co.uk", "org.uk", "com.hk",
    "com.tw", "co.jp", "com.au", "com.sg",
}
_CREDENTIAL_DIR_DEFAULT_PERMS = 0o700


class CookieImportError(ValueError):
    """Import rejected; ``code`` is one of the documented cookie_* codes."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _host_of_origin(origin: str) -> str:
    return (urlparse(origin).hostname or "").lower()


def _within(host: str, allowed: str) -> bool:
    host, allowed = host.lower().lstrip("."), allowed.lower().lstrip(".")
    return host == allowed or host.endswith("." + allowed)


def host_within_any(host: str, allowed: list[str]) -> bool:
    return any(_within(host, a) for a in allowed)


def validate_cookies_v1(payload: Any, allowed_domains: list[str], origin: str,
                        now: float | None = None) -> list[dict[str, Any]]:
    """Validate a ``playwright_cookies_json_v1`` payload against the scope.

    Returns normalised cookie dicts acceptable to ``BrowserContext.add_cookies``.
    Scope, attribute and expiry problems raise ``CookieImportError``; values
    are never included in error messages.
    """
    now = time.time() if now is None else now
    if isinstance(payload, dict) and ("cookies" in payload or "origins" in payload):
        raise CookieImportError(
            "cookie_unsupported_state",
            "a full storage_state (cookies + origins/localStorage) is not supported by the "
            "cookie importer; log in normally inside the dedicated profile instead "
            "(mic browser setup --url ...)")
    if not isinstance(payload, list) or not payload:
        raise CookieImportError("cookie_format_invalid",
                                "expected a non-empty JSON array of cookie objects")
    if not allowed_domains:
        raise CookieImportError("cookie_scope_mismatch", "allowed_cookie_domains is empty")
    origin_host = _host_of_origin(origin)
    for allowed in allowed_domains:
        if allowed.lstrip(".").lower() in _PUBLIC_SUFFIXES or "." not in allowed.strip("."):
            raise CookieImportError("cookie_public_suffix",
                                    f"allowed domain {allowed!r} is a public suffix")
        if not _within(origin_host, allowed) and not _within(allowed, origin_host):
            raise CookieImportError("cookie_scope_mismatch",
                                    f"allowed domain {allowed!r} is unrelated to origin host "
                                    f"{origin_host!r}")

    out: list[dict[str, Any]] = []
    for i, c in enumerate(payload):
        if not isinstance(c, dict):
            raise CookieImportError("cookie_format_invalid", f"cookie[{i}] is not an object")
        unknown = sorted(set(c) - _ALLOWED_COOKIE_KEYS)
        if unknown:
            raise CookieImportError("cookie_unsupported_attribute",
                                    f"cookie[{i}] has unsupported attribute(s) {unknown}")
        name = c.get("name")
        if not isinstance(name, str) or not name or not isinstance(c.get("value"), str):
            raise CookieImportError("cookie_format_invalid",
                                    f"cookie[{i}] requires string name and value")
        has_url, has_domain = bool(c.get("url")), bool(c.get("domain"))
        if has_url == has_domain:
            raise CookieImportError("cookie_format_invalid",
                                    f"cookie[{i}] ({name}) needs exactly one of url or domain+path")
        if has_domain and not c.get("path"):
            raise CookieImportError("cookie_format_invalid",
                                    f"cookie[{i}] ({name}) domain cookies require path")
        if has_url:
            parsed = urlparse(str(c["url"]))
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise CookieImportError("cookie_format_invalid", f"cookie[{i}] ({name}) bad url")
            host = parsed.hostname.lower()
            host_only = True
            if c.get("hostOnly") is False:
                raise CookieImportError("cookie_ambiguous_domain",
                                        f"cookie[{i}] ({name}) url-form cookie marked hostOnly=false")
        else:
            raw_domain = str(c["domain"]).lower()
            host = raw_domain.lstrip(".")
            host_only = not raw_domain.startswith(".")
            if "hostOnly" in c and bool(c["hostOnly"]) != host_only:
                raise CookieImportError(
                    "cookie_ambiguous_domain",
                    f"cookie[{i}] ({name}) hostOnly flag contradicts leading-dot domain form")
        if "." not in host or host in _PUBLIC_SUFFIXES:
            raise CookieImportError("cookie_public_suffix",
                                    f"cookie[{i}] ({name}) domain {host!r} is a public suffix")
        # Scope: the cookie's own domain must be within an allowed domain. A
        # domain-cookie is broader than host-only, so its base must itself be
        # within an allowed domain (never widen the allowed scope).
        if not host_within_any(host, allowed_domains):
            raise CookieImportError(
                "cookie_scope_mismatch",
                f"cookie[{i}] ({name}) domain {host!r} is outside allowed_cookie_domains "
                f"{allowed_domains}; adjust the scope explicitly or use the dedicated profile")
        expires = c.get("expires", -1)
        if expires is None:
            expires = -1
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            raise CookieImportError("cookie_format_invalid", f"cookie[{i}] ({name}) bad expires")
        if 0 < expires <= now:
            raise CookieImportError("cookie_expired", f"cookie[{i}] ({name}) already expired")
        same_site = c.get("sameSite")
        if same_site is not None and same_site not in _SAMESITE:
            raise CookieImportError("cookie_format_invalid",
                                    f"cookie[{i}] ({name}) sameSite must be Strict/Lax/None")
        norm: dict[str, Any] = {"name": name, "value": c["value"]}
        if has_url:
            norm["url"] = c["url"]
        else:
            norm["domain"] = c["domain"]
            norm["path"] = c["path"]
        norm["expires"] = float(expires)
        norm["httpOnly"] = bool(c.get("httpOnly", False))
        norm["secure"] = bool(c.get("secure", False))
        if same_site is not None:
            norm["sameSite"] = same_site
        if c.get("partitionKey") is not None:
            if not isinstance(c["partitionKey"], (str, dict)):
                raise CookieImportError("cookie_unsupported_attribute",
                                        f"cookie[{i}] ({name}) unsupported partitionKey form")
            norm["partitionKey"] = c["partitionKey"]
        out.append(norm)
    return out


def cookie_version(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()[:12]


def local_expiry(cookies: list[dict[str, Any]], max_age_seconds: float,
                 now: float | None = None) -> float:
    """Earliest of (now + max_age) and the shortest real cookie expiry."""
    now = time.time() if now is None else now
    exp = now + float(max_age_seconds)
    for c in cookies:
        e = c.get("expires", -1)
        if isinstance(e, (int, float)) and e > 0:
            exp = min(exp, float(e))
    return exp


@dataclass
class CredentialMeta:
    credential_id: str
    origin: str
    version: str
    allowed_cookie_domains: list[str]
    cookie_count: int
    imported_at: float
    expires_at_local: float
    status: str = "active"  # active | revoked | expired

    def public(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id, "origin": self.origin, "version": self.version,
            "allowed_cookie_domains": list(self.allowed_cookie_domains),
            "cookie_count": self.cookie_count, "status": self.status,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.imported_at)) + "Z",
            "expires_at_local": time.strftime("%Y-%m-%dT%H:%M:%S",
                                              time.gmtime(self.expires_at_local)) + "Z",
        }


@dataclass
class SessionStore:
    """Registry of imported credentials for the configured session fallback."""

    runtime: dict[str, Any]
    credential_dir: Path
    now: Any = time.time
    _registry: dict[str, CredentialMeta] = field(default_factory=dict)

    @classmethod
    def from_runtime(cls, runtime: dict[str, Any], env: dict[str, str] | None = None,
                     now: Any = time.time) -> SessionStore:
        store = cls(runtime=runtime, credential_dir=resolve_credential_dir(runtime, env), now=now)
        store._load_registry()
        return store

    # --- registry persistence ------------------------------------------------

    @property
    def registry_path(self) -> Path:
        return self.credential_dir / "registry.json"

    def _cookie_path(self, credential_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in credential_id)
        return self.credential_dir / f"{safe}.cookies.json"

    def _load_registry(self) -> None:
        self._registry = {}
        if not self.registry_path.exists():
            return
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return
        for cid, meta in (data.get("credentials") or {}).items():
            try:
                self._registry[cid] = CredentialMeta(**meta)
            except TypeError:
                continue

    def _save_registry(self) -> None:
        ensure_private_dir(self.credential_dir)
        payload = {"version": 1,
                   "credentials": {cid: m.__dict__ for cid, m in self._registry.items()}}
        write_private_file(self.registry_path, json.dumps(payload, indent=2).encode("utf-8"))

    # --- configuration helpers -------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool((self.runtime.get("session_fallback") or {}).get("enabled"))

    def source_for_origin(self, origin: str) -> dict[str, Any] | None:
        sf = self.runtime.get("session_fallback") or {}
        if origin not in (sf.get("allowed_origins") or []):
            return None
        return (sf.get("cookie_sources") or {}).get(origin)

    @staticmethod
    def origin_of(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}".lower()

    # --- import / status / remove ---------------------------------------------------

    def import_cookies(self, origin: str, file_path: Path | None = None,
                       env: dict[str, str] | None = None) -> CredentialMeta:
        """Validate and register the user's cookie file for ``origin``."""
        src = self.source_for_origin(origin)
        if src is None:
            raise CookieImportError("cookie_origin_not_allowed",
                                    f"origin {origin} is not in session_fallback.allowed_origins "
                                    "with a cookie_sources entry")
        env = os.environ if env is None else env
        if file_path is None:
            value = (env.get(src["source_file_env"]) or "").strip()
            if not value:
                raise CookieImportError("cookie_source_unavailable",
                                        f"{src['source_file_env']} is not set")
            file_path = Path(value).expanduser()
        if src.get("format", FORMAT_V1) != FORMAT_V1:
            raise CookieImportError("cookie_format_invalid", f"unsupported format {src.get('format')}")
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            raise CookieImportError("cookie_source_unavailable",
                                    f"cannot read cookie file: {type(exc).__name__}") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise CookieImportError("cookie_format_invalid", "cookie file is not valid JSON") from None
        allowed = [str(d) for d in src.get("allowed_cookie_domains") or []]
        now = float(self.now())
        cookies = validate_cookies_v1(payload, allowed, origin, now=now)
        max_age = float(src.get("max_age_seconds") or
                        (self.runtime.get("session_fallback") or {}).get(
                            "default_max_age_seconds", 7200))
        meta = CredentialMeta(
            credential_id=src["credential_id"], origin=origin, version=cookie_version(raw),
            allowed_cookie_domains=allowed, cookie_count=len(cookies), imported_at=now,
            expires_at_local=local_expiry(cookies, max_age, now=now), status="active")
        ensure_private_dir(self.credential_dir)
        write_private_file(self._cookie_path(meta.credential_id),
                           json.dumps(cookies).encode("utf-8"))
        self._registry[meta.credential_id] = meta
        self._save_registry()
        return meta

    def status(self) -> list[dict[str, Any]]:
        now = float(self.now())
        out = []
        for meta in self._registry.values():
            view = meta.public()
            if meta.status == "active" and meta.expires_at_local <= now:
                view["status"] = "expired"
            out.append(view)
        return out

    def remove(self, credential_id: str) -> dict[str, Any]:
        """Revoke: delete the stored cookie values and mark the registry entry.

        Returns the domains whose managed cookies must also be cleared from
        the dedicated profile (callers do that with a browser session).
        """
        meta = self._registry.get(credential_id)
        path = self._cookie_path(credential_id)
        try:
            path.unlink()
            removed_file = True
        except FileNotFoundError:
            removed_file = False
        if meta is None:
            return {"credential_id": credential_id, "removed_file": removed_file,
                    "domains_to_clear": [], "status": "unknown"}
        meta.status = "revoked"
        self._save_registry()
        return {"credential_id": credential_id, "removed_file": removed_file,
                "domains_to_clear": list(meta.allowed_cookie_domains), "status": "revoked"}

    # --- runtime use -------------------------------------------------------------

    def credential_for(self, origin: str,
                       exclude_versions: set[str] | None = None) -> dict[str, Any] | None:
        """Active, unexpired credential for ``origin`` not yet used in this failure chain.

        Returns {credential_id, version, cookies} or None. Cookie values are
        returned only to be injected into the dedicated browser context.
        """
        if not self.enabled:
            return None
        src = self.source_for_origin(origin)
        if src is None:
            return None
        meta = self._registry.get(src["credential_id"])
        if meta is None or meta.status != "active":
            return None
        if meta.expires_at_local <= float(self.now()):
            return None
        if exclude_versions and meta.version in exclude_versions:
            return None
        try:
            cookies = json.loads(self._cookie_path(meta.credential_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {"credential_id": meta.credential_id, "version": meta.version, "cookies": cookies,
                "allowed_cookie_domains": list(meta.allowed_cookie_domains)}
