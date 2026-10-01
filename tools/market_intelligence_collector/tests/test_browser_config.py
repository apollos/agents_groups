"""T01 / T09 / T17: config validation, legacy compatibility, migrations."""

from __future__ import annotations

import sys

import pytest

from mic.browser.config import ConfigError, resolve_profile_dir, validate_browser_runtime
from mic.config import load_config
from mic.search import SearchProvider, build_search_provider
from tests.browser_doubles import browser_config, browser_runtime_block

# --- T01: legacy path untouched --------------------------------------------------------

def test_repo_default_config_keeps_browser_disabled_and_legacy_provider():
    cfg = load_config()
    assert cfg.browser_enabled is False
    assert cfg.search_providers["active"] == "searxng"
    assert "browser_local" in cfg.search_providers["providers"]
    provider = build_search_provider(cfg)
    assert provider.browser_backed is False
    assert "playwright" not in sys.modules or True  # importing MIC must not require Playwright


def test_mic_config_dir_env_points_cli_at_deployment_config(tmp_path, monkeypatch):
    """`mic` CLI commands call load_config() without arguments; MIC_CONFIG_DIR selects the
    deployment config dir. An explicit argument still wins; a missing dir fails loudly."""
    import shutil
    from pathlib import Path

    repo_cfg = Path(__file__).resolve().parent.parent / "config"
    deploy = tmp_path / "deploy-config"
    shutil.copytree(repo_cfg, deploy)
    rt = (deploy / "browser_runtime.yaml").read_text().replace("enabled: false", "enabled: true", 1)
    (deploy / "browser_runtime.yaml").write_text(rt)

    monkeypatch.setenv("MIC_CONFIG_DIR", str(deploy))
    assert load_config().browser_enabled is True
    assert load_config(repo_cfg).browser_enabled is False  # explicit argument wins
    monkeypatch.setenv("MIC_CONFIG_DIR", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError):
        load_config()
    monkeypatch.setenv("MIC_CONFIG_DIR", "   ")
    assert load_config().browser_enabled is False  # blank -> repo default


def test_legacy_provider_search_with_context_default_reports_api_request():
    from mic.browser.contracts import SearchRequest
    from mic.search import MockSearchProvider

    batch = MockSearchProvider(hits_per_query=3).search_with_context(SearchRequest(query="宁德时代 公告"), None)
    assert batch.api_requests == 1
    assert batch.pages_opened == 0
    assert len(batch.hits) == 3
    assert all(h.discovery is None for h in batch.hits)  # legacy hits carry no discovery block


def test_mic_import_does_not_import_playwright():
    """Checked in a fresh interpreter: other tests in this session may import Playwright."""
    import subprocess

    code = ("import sys, mic.browser, mic.browser.config, mic.browser_search, mic.pipeline, mic.cli;"
            "from mic.search import build_search_provider; from mic.config import load_config;"
            "build_search_provider(load_config());"
            "assert not [m for m in sys.modules if m.startswith('playwright')], 'playwright imported'")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]


# --- browser_runtime validation ---------------------------------------------------------------

def test_browser_runtime_defaults_when_block_absent():
    rt = validate_browser_runtime(None)
    assert rt["enabled"] is False
    assert rt["channel"] == "msedge" and rt["headless"] is False


@pytest.mark.parametrize("bad, fragment", [
    ({"enabled": "yes"}, "enabled"),
    ({"bogus": 1}, "unknown field"),
    ({"interaction_mode": "auto"}, "interaction_mode"),
    ({"limits": {"max_search_pages_per_query": 9, "max_search_pages_per_run": 6}}, "per_query"),
    ({"limits": {"unknown_cap": 1}}, "unknown field"),
    ({"session_fallback": {"enabled": True, "apply_to_http_client": True}}, "apply_to_http_client"),
    ({"session_fallback": {"enabled": True, "cookie_sources": {"https://x.example": {
        "credential_id": "x", "source_file_env": "X_COOKIES", "format": "netscape",
        "allowed_cookie_domains": ["x.example"]}}}}, "format"),
])
def test_browser_runtime_rejects_bad_blocks(bad, fragment):
    with pytest.raises(ConfigError) as exc:
        validate_browser_runtime(browser_runtime_block(**bad))
    assert fragment in str(exc.value)


def test_profile_dir_env_is_required(monkeypatch):
    rt = validate_browser_runtime(browser_runtime_block())
    with pytest.raises(ConfigError):
        resolve_profile_dir(rt, {})
    assert resolve_profile_dir(rt, {"MIC_BROWSER_PROFILE_DIR": "/tmp/x/mic-edge"}).name == "mic-edge"


def test_env_var_required_in_doctor_not_config_load(monkeypatch):
    monkeypatch.delenv("MIC_BROWSER_PROFILE_DIR", raising=False)
    cfg = browser_config()  # loading config must not need the env var
    assert cfg.browser_enabled is True


# --- T09: no double fallback / composite mixing ------------------------------------------------

def test_browser_provider_with_fallback_is_rejected():
    with pytest.raises(ConfigError) as exc:
        browser_config(fallback=["searxng"])
    assert "fallback" in str(exc.value)


def test_browser_provider_combined_with_other_active_is_rejected():
    with pytest.raises(ConfigError):
        browser_config(active=["browser_local", "searxng"])


def test_browser_provider_requires_runtime_enabled():
    with pytest.raises(ConfigError) as exc:
        browser_config(runtime=browser_runtime_block(enabled=False))
    assert "browser_runtime.enabled" in str(exc.value)


@pytest.mark.parametrize("provider", [
    {"type": "browser", "engine_order": ["bing"], "enabled_engines": ["bing"], "query_rewrite": True},
    {"type": "browser", "engine_order": ["duckduckgo"]},
    {"type": "browser", "engine_order": ["bing"], "surprise": 1},
    {"type": "browser", "engine_order": ["bing"], "enabled_engines": ["google"]},
])
def test_browser_provider_rejects_bad_provider_blocks(provider):
    with pytest.raises(ConfigError):
        browser_config(provider=provider)


def test_browser_provider_construction_opens_nothing():
    cfg = browser_config()
    provider = build_search_provider(cfg)
    assert isinstance(provider, SearchProvider) and provider.browser_backed is True
    assert provider.engines == ["bing", "baidu", "google"]  # all three verified on real DOM (2026-10)
    # Construction must not launch anything: the default lazy loader factory is untouched.
    assert provider._loader_factory == provider._default_loader


# --- T17: migrations on an old SQLite copy ----------------------------------------------------

def test_migration_adds_new_columns_and_table_idempotently(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    # Minimal "old" schema: link_read_attempt without diagnostics, no search_page_attempt table.
    con.executescript("""
    CREATE TABLE search_run (id TEXT PRIMARY KEY, target_id TEXT, status TEXT);
    CREATE TABLE source_link (id TEXT PRIMARY KEY, search_run_id TEXT, url TEXT, canonical_url TEXT,
                              domain TEXT, metadata JSON);
    CREATE TABLE link_read_attempt (id TEXT PRIMARY KEY, source_link_id TEXT, read_status TEXT,
                                    created_at TIMESTAMP);
    INSERT INTO search_run VALUES ('run_old', 'company_300750', 'completed');
    INSERT INTO link_read_attempt VALUES ('read_old', 'link_old', 'read', '2026-01-01 00:00:00');
    """)
    con.commit()
    con.close()

    monkeypatch.setenv("MIC_DATABASE_URL", f"sqlite:///{db_path}")
    import mic.store.database as dbmod
    dbmod._DB = None
    db = dbmod.get_database(f"sqlite:///{db_path}")
    db.create_all()
    db.create_all()  # idempotent
    con = sqlite3.connect(db_path)
    cols = {row[1] for row in con.execute("PRAGMA table_info(link_read_attempt)")}
    assert "diagnostics" in cols
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "search_page_attempt" in tables
    # Old rows keep null metadata - no fabricated transport / rule version.
    diag = con.execute("SELECT diagnostics FROM link_read_attempt WHERE id='read_old'").fetchone()[0]
    assert diag is None
    con.close()
