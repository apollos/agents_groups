from __future__ import annotations

import os
from pathlib import Path

from stock_data_ingestion.config import load_config
from stock_data_ingestion.env import load_env, load_env_if_missing, reset_env_loader_state


_ENV_KEYS = [
    "TUSHARE_TOKEN",
    "JQDATA_USERNAME",
    "JQDATA_PASSWORD",
    "STOCK_DATA_SQLITE_PATH",
    "CUSTOM_PROVIDER_TIMEOUT",
    "CUSTOM_FUTURE_ENV_PARAM",
    "EXISTING_VALUE",
    "EMPTY_IN_FILE",
    "STOCK_DATA_ENV_FILE",
    "STOCK_DATA_ENV_OVERRIDE",
    "STOCK_DATA_DISABLE_ENV_AUTOLOAD",
]


def _clean_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reset_env_loader_state()
    load_config.cache_clear()
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_env_loads_all_missing_vars_and_does_not_override(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _clean_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TUSHARE_TOKEN=file-token",
                "JQDATA_USERNAME=file-user",
                "JQDATA_PASSWORD=file-password",
                "STOCK_DATA_SQLITE_PATH=data/from-dotenv.db",
                "CUSTOM_PROVIDER_TIMEOUT=45",
                "EXISTING_VALUE=from-dotenv",
                "EMPTY_IN_FILE=",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXISTING_VALUE", "from-shell")

    result = load_env(env_file=env_file)

    assert result["env_file"] == str(env_file.resolve())
    assert set(result["loaded_vars"]) >= {
        "TUSHARE_TOKEN",
        "JQDATA_USERNAME",
        "JQDATA_PASSWORD",
        "STOCK_DATA_SQLITE_PATH",
        "CUSTOM_PROVIDER_TIMEOUT",
    }
    assert "EXISTING_VALUE" in result["skipped_existing_vars"]
    assert "EMPTY_IN_FILE" in result["skipped_empty_vars"]
    assert result["missing_important_after"] == []

    assert os.environ["TUSHARE_TOKEN"] == "file-token"
    assert os.environ["JQDATA_USERNAME"] == "file-user"
    assert os.environ["JQDATA_PASSWORD"] == "file-password"
    assert os.environ["STOCK_DATA_SQLITE_PATH"] == "data/from-dotenv.db"
    assert os.environ["CUSTOM_PROVIDER_TIMEOUT"] == "45"
    assert os.environ["EXISTING_VALUE"] == "from-shell"
    assert "EMPTY_IN_FILE" not in os.environ


def test_load_env_if_missing_keeps_backward_compatibility_but_loads_all_vars(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _clean_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TUSHARE_TOKEN=file-token\nSTOCK_DATA_SQLITE_PATH=data/from-dotenv.db\n",
        encoding="utf-8",
    )

    result = load_env_if_missing(env_file=env_file)

    assert result["missing_before"] == ["TUSHARE_TOKEN", "JQDATA_USERNAME", "JQDATA_PASSWORD"]
    assert os.environ["TUSHARE_TOKEN"] == "file-token"
    assert os.environ["STOCK_DATA_SQLITE_PATH"] == "data/from-dotenv.db"


def test_direct_adapter_instantiation_loads_dotenv_before_reading_credentials(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _clean_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TUSHARE_TOKEN=file-token\nJQDATA_USERNAME=file-user\nJQDATA_PASSWORD=file-password\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    from stock_data_ingestion.adapters.joinquant_adapter import JoinQuantAdapter
    from stock_data_ingestion.adapters.tushare_adapter import TushareAdapter

    tushare = TushareAdapter()
    joinquant = JoinQuantAdapter()

    assert tushare.token == "file-token"
    assert joinquant.username == "file-user"
    assert joinquant.password == "file-password"


def test_load_config_honors_other_dotenv_env_vars(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _clean_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (tmp_path / ".env").write_text("STOCK_DATA_SQLITE_PATH=data/from-dotenv.db\n", encoding="utf-8")

    config = load_config(config_dir)

    assert str(config.storage.sqlite_path) == "data/from-dotenv.db"


def test_dotenv_searches_upward_from_subdirectories(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _clean_env(monkeypatch)
    nested = tmp_path / "scripts" / "jobs"
    nested.mkdir(parents=True)
    (tmp_path / ".env").write_text("CUSTOM_FUTURE_ENV_PARAM=from-parent\n", encoding="utf-8")
    monkeypatch.chdir(nested)

    result = load_env()

    assert result["env_file"] == str((tmp_path / ".env").resolve())
    assert os.environ["CUSTOM_FUTURE_ENV_PARAM"] == "from-parent"
