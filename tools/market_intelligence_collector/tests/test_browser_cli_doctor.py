"""`mic browser doctor / cookies / setup`, `search probe`, `reader probe` offline behaviour."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from mic.browser.config import ConfigError
from mic.browser.doctor import run_doctor
from mic.browser.setup import resolve_setup_url
from mic.cli import app
from tests.browser_doubles import browser_config, browser_runtime_block

runner = CliRunner()


def test_doctor_reports_missing_profile_env_without_network(tmp_path, monkeypatch):
    cfg = browser_config()
    env = {"DISPLAY": ":0"}
    result = run_doctor(cfg, launch=False, runs_dir=tmp_path / "runs", env=env)
    checks = {c["check"]: c for c in result["checks"]}
    assert checks["browser_runtime_config"]["ok"] is True
    assert checks["profile_dir"]["ok"] is False and "MIC_BROWSER_PROFILE_DIR" in checks["profile_dir"]["hint"]
    assert checks["edge_executable"]["check"] == "edge_executable"
    assert checks["search_provider"]["detail"]["browser_route"] is True
    assert result["ok"] is False


def test_doctor_detects_gui_unavailable_and_lock_holder(tmp_path):
    from mic.browser.profile_lock import ProfileLock, ensure_private_dir

    cfg = browser_config()
    profile = ensure_private_dir(tmp_path / "mic-edge")
    lock = ProfileLock(profile, "other_run", "other_attempt")
    lock.acquire()
    try:
        result = run_doctor(cfg, env={"MIC_BROWSER_PROFILE_DIR": str(profile)})
    finally:
        lock.release()
    checks = {c["check"]: c for c in result["checks"]}
    assert checks["gui_session"]["ok"] is False
    assert checks["profile_dir"]["detail"]["locked"] is True
    assert checks["profile_lock"]["detail"]["run_id"] == "other_run"
    assert "profile_busy" in checks["profile_lock"]["hint"]


def test_doctor_launch_refused_when_disabled(tmp_path):
    from mic.config import load_config

    cfg = load_config()  # repo default: browser disabled
    result = run_doctor(cfg, launch=True, env={"MIC_BROWSER_PROFILE_DIR": str(tmp_path / "p"), "DISPLAY": ":0"})
    assert result["launch"]["ok"] is False
    assert "enabled" in result["launch"]["error"]


def test_cli_doctor_json_exit_code_nonzero_on_problems(monkeypatch):
    monkeypatch.delenv("MIC_BROWSER_PROFILE_DIR", raising=False)
    result = runner.invoke(app, ["browser", "doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert any(c["check"] == "profile_dir" for c in payload["checks"])


def test_cli_cookies_status_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("MIC_BROWSER_CREDENTIAL_DIR", str(tmp_path / "creds"))
    result = runner.invoke(app, ["browser", "cookies", "status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"credentials": []}


def test_cli_search_probe_requires_enabled_runtime():
    result = runner.invoke(app, ["search", "probe", "--query", "宁德时代 中标"])
    assert result.exit_code == 2
    assert "config_error" in result.stdout


def test_cli_reader_probe_browser_requires_enabled_runtime():
    result = runner.invoke(app, ["reader", "probe", "--url", "https://example.com/a", "--transport", "browser"])
    assert result.exit_code == 2
    assert "config_error" in result.stdout


def test_cli_browser_setup_requires_engine_or_url():
    result = runner.invoke(app, ["browser", "setup"])
    assert result.exit_code == 2


class _PlaywrightTimeout(Exception):
    """Stands in for playwright.sync_api.TimeoutError (matched by class name)."""


_PlaywrightTimeout.__name__ = "TimeoutError"


class _WaitPage:
    """Fake page whose close event is only observable through wait_for_event, like the sync API."""

    def __init__(self, close_after_polls: int | None, error: Exception | None = None):
        self.polls, self.close_after, self.error, self._closed = 0, close_after_polls, error, False

    def is_closed(self):
        return self._closed

    def wait_for_event(self, name, timeout):
        assert name == "close" and timeout <= 500
        self.polls += 1
        if self.error is not None:
            raise self.error
        if self.close_after is not None and self.polls >= self.close_after:
            self._closed = True
            return None
        raise _PlaywrightTimeout("Timeout 500ms exceeded")


class _Session:
    started = True


def test_setup_wait_uses_wait_for_event_to_observe_user_closing_window(monkeypatch):
    """Regression: a sleep loop never saw the close event (sync API dispatches events only
    during Playwright calls), so `browser setup` kept waiting after Edge was gone (2026-10-02)."""
    import time

    from mic.browser.setup import _wait_until_closed
    page = _WaitPage(close_after_polls=3)
    assert _wait_until_closed(page, _Session(), deadline=time.monotonic() + 60) == "closed"
    assert page.polls == 3
    # browser disappeared abruptly -> any non-timeout error ends the wait instead of looping
    gone = _WaitPage(None, error=RuntimeError("Target page, context or browser has been closed"))
    assert _wait_until_closed(gone, _Session(), deadline=time.monotonic() + 60) == "error"
    assert gone.polls == 1
    # cap: keeps polling with timeouts until the deadline, then reports cap
    slow = _WaitPage(None)
    assert _wait_until_closed(slow, _Session(), deadline=time.monotonic() + 0.01, poll_seconds=0.001) == "cap"


def test_setup_url_allowlist():
    cfg = browser_config(runtime=browser_runtime_block(
        session_fallback={"enabled": True, "allowed_origins": ["https://news.example.com"],
                          "cookie_sources": {}, "apply_to_http_client": False}))
    assert resolve_setup_url(cfg, "bing", None) == "https://www.bing.com/"
    assert resolve_setup_url(cfg, None, "https://news.example.com/login") == "https://news.example.com/login"
    with pytest.raises(ConfigError):
        resolve_setup_url(cfg, None, "https://evil.example/login")
    with pytest.raises(ConfigError):
        resolve_setup_url(cfg, "bing", "https://www.bing.com/")
    with pytest.raises(ConfigError):
        resolve_setup_url(cfg, None, "file:///etc/passwd")
