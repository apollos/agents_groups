"""T19 / T21 / T22: user-authorised cookie import, scope/expiry, revocation, no value leakage."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from mic.browser.session_fallback import (
    CookieImportError,
    SessionStore,
    local_expiry,
    validate_cookies_v1,
)
from tests.browser_doubles import browser_runtime_block

ORIGIN = "https://news.example.com"
ALLOWED = ["news.example.com"]
NOW = 1_800_000_000.0


def _cookie(**kw):
    base = {"name": "sid", "value": "SECRET-COOKIE-VALUE", "domain": "news.example.com", "path": "/",
            "expires": NOW + 86400, "httpOnly": True, "secure": True, "sameSite": "Lax"}
    base.update(kw)
    return base


# --- T19: format / scope / expiry -------------------------------------------------------------

def test_valid_host_only_cookie_normalised():
    out = validate_cookies_v1([_cookie()], ALLOWED, ORIGIN, now=NOW)
    assert out[0]["domain"] == "news.example.com" and out[0]["secure"] is True and out[0]["httpOnly"] is True


def test_domain_cookie_with_leading_dot_within_scope_ok_but_broader_rejected():
    assert validate_cookies_v1([_cookie(domain=".news.example.com")], ALLOWED, ORIGIN, now=NOW)
    with pytest.raises(CookieImportError) as exc:
        validate_cookies_v1([_cookie(domain=".example.com")], ALLOWED, ORIGIN, now=NOW)
    assert exc.value.code == "cookie_scope_mismatch"
    assert "SECRET" not in str(exc.value)


def test_public_suffix_domains_rejected():
    with pytest.raises(CookieImportError) as exc:
        validate_cookies_v1([_cookie(domain=".com.cn")], ["com.cn"], "https://x.com.cn", now=NOW)
    assert exc.value.code == "cookie_public_suffix"


def test_host_only_flag_contradiction_is_ambiguous():
    with pytest.raises(CookieImportError) as exc:
        validate_cookies_v1([_cookie(domain=".news.example.com", hostOnly=True)], ALLOWED, ORIGIN, now=NOW)
    assert exc.value.code == "cookie_ambiguous_domain"


def test_expired_cookie_rejected_and_unsupported_attribute_not_silently_dropped():
    with pytest.raises(CookieImportError) as exc:
        validate_cookies_v1([_cookie(expires=NOW - 1)], ALLOWED, ORIGIN, now=NOW)
    assert exc.value.code == "cookie_expired"
    with pytest.raises(CookieImportError) as exc2:
        validate_cookies_v1([_cookie(priority="High")], ALLOWED, ORIGIN, now=NOW)
    assert exc2.value.code == "cookie_unsupported_attribute"


def test_storage_state_and_other_formats_rejected():
    with pytest.raises(CookieImportError) as exc:
        validate_cookies_v1({"cookies": [], "origins": []}, ALLOWED, ORIGIN, now=NOW)
    assert exc.value.code == "cookie_unsupported_state"
    with pytest.raises(CookieImportError):
        validate_cookies_v1("netscape cookie jar", ALLOWED, ORIGIN, now=NOW)


def test_partition_key_kept_when_present():
    out = validate_cookies_v1([_cookie(partitionKey="https://news.example.com")], ALLOWED, ORIGIN, now=NOW)
    assert out[0]["partitionKey"] == "https://news.example.com"


def test_local_expiry_is_min_of_max_age_and_real_expiry():
    assert local_expiry([_cookie(expires=NOW + 100)], 7200, now=NOW) == NOW + 100
    assert local_expiry([_cookie(expires=NOW + 999999)], 7200, now=NOW) == NOW + 7200
    assert local_expiry([_cookie(expires=-1)], 7200, now=NOW) == NOW + 7200


# --- SessionStore: import / status / remove -------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    runtime = browser_runtime_block(session_fallback={
        "enabled": True, "allowed_origins": [ORIGIN],
        "cookie_sources": {ORIGIN: {"credential_id": "news_user", "source_file_env": "NEWS_COOKIES",
                                    "format": "playwright_cookies_json_v1",
                                    "allowed_cookie_domains": ALLOWED, "max_age_seconds": 3600}},
        "default_max_age_seconds": 7200, "apply_to_http_client": False})
    clock = {"t": NOW}
    s = SessionStore.from_runtime(runtime, env={"MIC_BROWSER_CREDENTIAL_DIR": str(tmp_path / "creds")},
                                  now=lambda: clock["t"])
    s._clock = clock
    return s


def _write_cookie_file(tmp_path: Path, cookies) -> Path:
    p = tmp_path / "user_cookies.json"
    p.write_text(json.dumps(cookies))
    return p


def test_import_registers_metadata_only_and_files_are_private(store, tmp_path):
    f = _write_cookie_file(tmp_path, [_cookie()])
    meta = store.import_cookies(ORIGIN, f)
    assert meta.credential_id == "news_user" and meta.cookie_count == 1
    assert meta.expires_at_local == NOW + 3600
    public = json.dumps(meta.public())
    assert "SECRET" not in public and "value" not in meta.public()
    registry = store.registry_path.read_text()
    assert "SECRET" not in registry
    assert stat.S_IMODE(store.registry_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.credential_dir.stat().st_mode) == 0o700
    cookie_file = store._cookie_path("news_user")
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600
    assert store.status()[0]["status"] == "active"


def test_import_from_env_source_and_origin_not_allowed(store, tmp_path):
    f = _write_cookie_file(tmp_path, [_cookie()])
    meta = store.import_cookies(ORIGIN, None, env={"NEWS_COOKIES": str(f)})
    assert meta.cookie_count == 1
    with pytest.raises(CookieImportError) as exc:
        store.import_cookies("https://other.example", f)
    assert exc.value.code == "cookie_origin_not_allowed"
    with pytest.raises(CookieImportError) as exc2:
        store.import_cookies(ORIGIN, None, env={})
    assert exc2.value.code == "cookie_source_unavailable"


def test_credential_for_respects_expiry_and_exclusion(store, tmp_path):
    f = _write_cookie_file(tmp_path, [_cookie()])
    meta = store.import_cookies(ORIGIN, f)
    cred = store.credential_for(ORIGIN)
    assert cred["credential_id"] == "news_user" and cred["cookies"][0]["name"] == "sid"
    assert store.credential_for(ORIGIN, exclude_versions={meta.version}) is None  # same version not reloaded
    assert store.credential_for("https://other.example") is None
    store._clock["t"] = NOW + 3601
    assert store.credential_for(ORIGIN) is None
    assert store.status()[0]["status"] == "expired"


def test_remove_revokes_and_reports_domains_to_clear(store, tmp_path):
    f = _write_cookie_file(tmp_path, [_cookie()])
    store.import_cookies(ORIGIN, f)
    result = store.remove("news_user")
    assert result["status"] == "revoked" and result["removed_file"] is True
    assert result["domains_to_clear"] == ALLOWED
    assert not store._cookie_path("news_user").exists()
    assert store.credential_for(ORIGIN) is None  # revoked session never reused
    assert store.status()[0]["status"] == "revoked"


def test_disabled_fallback_never_returns_credentials(tmp_path):
    runtime = browser_runtime_block()  # session_fallback.enabled False
    s = SessionStore.from_runtime(runtime, env={"MIC_BROWSER_CREDENTIAL_DIR": str(tmp_path / "c")}, now=lambda: NOW)
    assert s.enabled is False
    assert s.credential_for(ORIGIN) is None


# --- T22: cookies never reach model inputs / reports --------------------------------------------------

def test_fetch_result_diagnostics_never_include_cookie_or_page_content():
    from mic.browser.contracts import FetchResult

    fr = FetchResult(transport="browser", requested_url="https://news.example.com/a", final_url="https://news.example.com/a",
                     html="<html>Cookie: SECRET-COOKIE-VALUE</html>", auth_mode="imported_cookie",
                     auth_context_id="ctx-1", authenticated_retry=True)
    d = fr.diagnostics()
    assert "html" not in d and "content" not in d
    assert "SECRET" not in json.dumps(d)
    assert d["auth_mode"] == "imported_cookie" and d["authenticated_retry"] is True
