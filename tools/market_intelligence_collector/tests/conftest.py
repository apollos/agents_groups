import pytest

from mic.config import load_config


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    # Isolate each test run in its own SQLite file and force mock mode.
    db_path = tmp_path / "mic_test.db"
    monkeypatch.setenv("MIC_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIC_ALLOW_MOCK", "true")
    # Reset the database singleton so the new URL is honored.
    import mic.store.database as dbmod
    dbmod._DB = None
    yield


@pytest.fixture
def config():
    return load_config()
