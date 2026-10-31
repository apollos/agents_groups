import shutil
from pathlib import Path

import pytest
import yaml

from mic.config import load_config


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    # Isolate each test run in its own SQLite file and explicit offline config.
    db_path = tmp_path / "mic_test.db"
    monkeypatch.setenv("MIC_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIC_ALLOW_MOCK", "true")
    # The repository defaults intentionally select the live browser route. Every
    # ordinary unit test must instead load an isolated copy that explicitly
    # selects mock and disables browser startup. Tests that need live defaults
    # pass the repository config directory explicitly.
    repo_config = Path(__file__).resolve().parent.parent / "config"
    offline_config = tmp_path / "mic-offline-config"
    shutil.copytree(repo_config, offline_config)
    search_path = offline_config / "search_providers.yaml"
    search = yaml.safe_load(search_path.read_text(encoding="utf-8"))
    search["search_providers"]["active"] = "mock"
    search["search_providers"]["fallback"] = []
    search_path.write_text(yaml.safe_dump(search, allow_unicode=True, sort_keys=False), encoding="utf-8")
    runtime_path = offline_config / "browser_runtime.yaml"
    runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
    runtime["browser_runtime"]["enabled"] = False
    runtime_path.write_text(yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("MIC_CONFIG_DIR", str(offline_config))
    monkeypatch.setenv("MIC_BROWSER_PROFILE_DIR", "")
    # Keep the suite hermetic regardless of what the developer has in .env.
    monkeypatch.setenv("SERPAPI_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    # Blank model keys too: adapters prefer a real call whenever a key is set,
    # so a developer's real .env would otherwise make these offline tests hit
    # live APIs (non-deterministic, slow, and costly). Mock stays forced.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "")
    monkeypatch.setenv("OPENCLAW_GATEWAY_BASE_URL", "")
    monkeypatch.setenv("OPENCLAW_REQUEST_MODEL", "")
    monkeypatch.delenv("OPENCLAW_VISION_MODEL", raising=False)
    # Reset the database singleton so the new URL is honored.
    import mic.store.database as dbmod
    dbmod._DB = None
    yield


@pytest.fixture
def config():
    return load_config()
