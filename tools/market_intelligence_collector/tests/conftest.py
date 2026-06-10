import pytest

from mic.config import load_config


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    # Isolate each test run in its own SQLite file and force mock mode.
    db_path = tmp_path / "mic_test.db"
    monkeypatch.setenv("MIC_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIC_ALLOW_MOCK", "true")
    # Keep the suite hermetic regardless of what the developer has in .env:
    # blank real search keys and point SearXNG at a dead port so the factory
    # always lands on the mock provider (setenv beats load_dotenv, which does
    # not override existing variables).
    monkeypatch.setenv("SERPAPI_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("OPENCLAW_VISION_MODEL", raising=False)
    # Reset the database singleton so the new URL is honored.
    import mic.store.database as dbmod
    dbmod._DB = None
    yield


@pytest.fixture
def config():
    return load_config()
