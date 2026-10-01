"""Validation of the ``browser_runtime`` configuration (design section 13).

Validation is type/range/unknown-key checking only: it never opens a browser,
touches the network or calls a model. Errors are raised before any navigation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mic.budget import DEFAULT_LIMITS


class ConfigError(ValueError):
    """Invalid browser / provider configuration (fail fast, no fallback)."""


_TOP_KEYS = {
    "version": int, "enabled": bool, "channel": str, "headless": bool,
    "profile_dir_env": str, "interaction_mode": str, "human_wait_seconds": (int, float),
    "browser_start_timeout_seconds": (int, float), "page_timeout_seconds": (int, float),
    "cleanup_grace_seconds": (int, float), "max_open_pages": int,
    "chromium_sandbox": bool, "accept_downloads": bool, "session_fallback": dict,
    "artifacts": dict, "limits": dict, "cache": dict, "credential_dir_env": str,
    "locale": str, "viewport": dict, "executable_path": str,
    # Extra hosts the operator may open with `mic browser setup --url`.
    "setup_allowed_hosts": list,
}
_SESSION_KEYS = {
    "enabled": bool, "allowed_origins": list, "cookie_sources": dict,
    "default_max_age_seconds": (int, float), "apply_to_http_client": bool,
    "save_authenticated_page_artifacts": bool,
}
_COOKIE_SOURCE_KEYS = {
    "credential_id": str, "format": str, "source_file_env": str,
    "allowed_cookie_domains": list, "max_age_seconds": (int, float),
}
_ARTIFACT_KEYS = {"save_screenshots": str, "save_full_dom": bool, "retention_days": int}
_CACHE_KEYS = {"reuse_analysis": bool}
_INTERACTION_MODES = ("unattended", "interactive")
_SCREENSHOT_MODES = ("never", "on_error", "always")
_COOKIE_FORMATS = ("playwright_cookies_json_v1",)

DEFAULT_BROWSER_RUNTIME: dict[str, Any] = {
    "version": 1,
    "enabled": False,
    "channel": "msedge",
    "headless": False,
    "profile_dir_env": "MIC_BROWSER_PROFILE_DIR",
    "credential_dir_env": "MIC_BROWSER_CREDENTIAL_DIR",
    "interaction_mode": "unattended",
    "human_wait_seconds": 60,
    "browser_start_timeout_seconds": 20,
    "page_timeout_seconds": 25,
    "cleanup_grace_seconds": 5,
    "max_open_pages": 2,
    "chromium_sandbox": True,
    "accept_downloads": False,
    "session_fallback": {
        "enabled": False, "allowed_origins": [], "cookie_sources": {},
        "default_max_age_seconds": 7200, "apply_to_http_client": False,
        "save_authenticated_page_artifacts": False,
    },
    "artifacts": {"save_screenshots": "on_error", "save_full_dom": False, "retention_days": 7},
    "limits": dict(DEFAULT_LIMITS),
    "cache": {"reuse_analysis": False},
}


def _check_keys(block: dict[str, Any], allowed: dict[str, Any], path: str) -> None:
    unknown = sorted(set(block) - set(allowed))
    if unknown:
        raise ConfigError(f"{path}: unknown field(s) {unknown}")
    for key, typ in allowed.items():
        if key in block and block[key] is not None and not isinstance(block[key], typ):
            raise ConfigError(f"{path}.{key}: expected {typ}, got {type(block[key]).__name__}")
        if key in block and typ is int and isinstance(block[key], bool):
            raise ConfigError(f"{path}.{key}: expected int, got bool")


def _positive(block: dict[str, Any], key: str, path: str, allow_zero: bool = False) -> None:
    if key not in block:
        return
    v = block[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{path}.{key}: must be a number")
    if v < 0 or (v == 0 and not allow_zero):
        raise ConfigError(f"{path}.{key}: must be {'>= 0' if allow_zero else '> 0'}")


def validate_browser_runtime(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a merged, validated browser_runtime block.

    Missing file / empty block -> defaults with ``enabled: False`` (legacy
    configs keep loading). Unknown fields, wrong types and out-of-range values
    raise ``ConfigError``.
    """
    merged: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v)
                              for k, v in DEFAULT_BROWSER_RUNTIME.items()}
    if not raw:
        return merged
    if not isinstance(raw, dict):
        raise ConfigError("browser_runtime: must be a mapping")
    _check_keys(raw, _TOP_KEYS, "browser_runtime")
    if raw.get("version") not in (None, 1):
        raise ConfigError(f"browser_runtime.version: unsupported {raw.get('version')!r}")
    if raw.get("interaction_mode") not in (None, *_INTERACTION_MODES):
        raise ConfigError(f"browser_runtime.interaction_mode: must be one of {_INTERACTION_MODES}")
    for key in ("human_wait_seconds", "browser_start_timeout_seconds",
                "page_timeout_seconds", "cleanup_grace_seconds", "max_open_pages"):
        _positive(raw, key, "browser_runtime")

    session = raw.get("session_fallback") or {}
    _check_keys(session, _SESSION_KEYS, "browser_runtime.session_fallback")
    _positive(session, "default_max_age_seconds", "browser_runtime.session_fallback")
    for origin in session.get("allowed_origins", []) or []:
        _check_origin(origin, "browser_runtime.session_fallback.allowed_origins")
    for origin, src in (session.get("cookie_sources") or {}).items():
        _check_origin(origin, "browser_runtime.session_fallback.cookie_sources")
        if not isinstance(src, dict):
            raise ConfigError(f"cookie_sources[{origin}]: must be a mapping")
        _check_keys(src, _COOKIE_SOURCE_KEYS, f"cookie_sources[{origin}]")
        for req in ("credential_id", "format", "source_file_env", "allowed_cookie_domains"):
            if not src.get(req):
                raise ConfigError(f"cookie_sources[{origin}].{req}: required")
        if src["format"] not in _COOKIE_FORMATS:
            raise ConfigError(f"cookie_sources[{origin}].format: unsupported {src['format']!r}")
        _positive(src, "max_age_seconds", f"cookie_sources[{origin}]")
    if session.get("apply_to_http_client"):
        raise ConfigError("browser_runtime.session_fallback.apply_to_http_client: "
                          "not supported in this version (must be false)")

    artifacts = raw.get("artifacts") or {}
    _check_keys(artifacts, _ARTIFACT_KEYS, "browser_runtime.artifacts")
    if artifacts.get("save_screenshots") not in (None, *_SCREENSHOT_MODES):
        raise ConfigError(f"browser_runtime.artifacts.save_screenshots: one of {_SCREENSHOT_MODES}")
    _positive(artifacts, "retention_days", "browser_runtime.artifacts", allow_zero=True)

    limits = raw.get("limits") or {}
    unknown = sorted(set(limits) - set(DEFAULT_LIMITS))
    if unknown:
        raise ConfigError(f"browser_runtime.limits: unknown field(s) {unknown}")
    for key, val in limits.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ConfigError(f"browser_runtime.limits.{key}: must be a number")
        allow_zero = key in ("max_retries_per_page", "min_engine_interval_seconds",
                             "max_authenticated_retries_per_origin",
                             "max_authenticated_retries_per_run")
        if val < 0 or (val == 0 and not allow_zero):
            raise ConfigError(f"browser_runtime.limits.{key}: must be {'>= 0' if allow_zero else '> 0'}")
    if limits.get("max_search_pages_per_query", DEFAULT_LIMITS["max_search_pages_per_query"]) > \
            limits.get("max_search_pages_per_run", DEFAULT_LIMITS["max_search_pages_per_run"]):
        raise ConfigError("browser_runtime.limits: max_search_pages_per_query cannot exceed "
                          "max_search_pages_per_run")

    cache = raw.get("cache") or {}
    _check_keys(cache, _CACHE_KEYS, "browser_runtime.cache")

    for key, val in raw.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **val}
        else:
            merged[key] = val
    return merged


def _check_origin(origin: Any, path: str) -> None:
    if not isinstance(origin, str) or not origin.startswith(("https://", "http://")):
        raise ConfigError(f"{path}: origin must be an http(s) URL origin, got {origin!r}")
    rest = origin.split("://", 1)[1]
    if "/" in rest or not rest:
        raise ConfigError(f"{path}: origin must not contain a path: {origin!r}")


def resolve_profile_dir(runtime: dict[str, Any], env: dict[str, str] | None = None) -> Path:
    """Profile directory from the configured env var; missing -> ConfigError.

    Never derived from cwd. The returned path is created by the session, not
    here.
    """
    env = os.environ if env is None else env
    env_name = runtime.get("profile_dir_env") or "MIC_BROWSER_PROFILE_DIR"
    value = (env.get(env_name) or "").strip()
    if not value:
        raise ConfigError(
            f"browser profile directory not configured: set {env_name} to a dedicated, "
            "persistent directory (e.g. <workspace>/browser/profiles/mic-edge)")
    return Path(value).expanduser()


def resolve_credential_dir(runtime: dict[str, Any], env: dict[str, str] | None = None) -> Path:
    """Credential directory (outside the repo). Defaults next to the profile dir."""
    env = os.environ if env is None else env
    env_name = runtime.get("credential_dir_env") or "MIC_BROWSER_CREDENTIAL_DIR"
    value = (env.get(env_name) or "").strip()
    if value:
        return Path(value).expanduser()
    return resolve_profile_dir(runtime, env).parent.parent / "credentials"


def public_runtime_view(runtime: dict[str, Any]) -> dict[str, Any]:
    """Non-sensitive copy for reports (no paths with content, no cookies)."""
    view = {k: v for k, v in runtime.items() if k not in ("session_fallback",)}
    session = runtime.get("session_fallback") or {}
    view["session_fallback"] = {
        "enabled": bool(session.get("enabled")),
        "allowed_origins": list(session.get("allowed_origins") or []),
        "cookie_source_count": len(session.get("cookie_sources") or {}),
    }
    return view
