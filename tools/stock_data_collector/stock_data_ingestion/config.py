from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from stock_data_ingestion.env import ensure_env_loaded, load_env, load_env_if_missing


class ProviderConfig(BaseModel):
    enabled: bool = True
    role: str = "validator_supplement"
    auth_env: dict[str, str] = Field(default_factory=dict)
    retry_attempts: int = 3
    timeout_seconds: int = 30
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    allow_fallback: bool = True
    allow_cross_validation: bool = True
    allow_field_level_supplement: bool = True


class DataSourcesConfig(BaseModel):
    canonical_provider: str = "tushare"
    provider_priority: list[str] = Field(default_factory=lambda: ["tushare", "akshare", "joinquant"])
    validator_providers: list[str] = Field(default_factory=lambda: ["akshare", "joinquant"])
    supplement_providers: list[str] = Field(default_factory=lambda: ["akshare", "joinquant"])
    allow_field_level_merge: bool = True
    allow_fallback_when_canonical_missing: bool = True
    allow_majority_override_canonical: bool = False
    quarantine_on_critical_conflict: bool = True
    manual_review_on_trading_critical_conflict: bool = True
    minimum_quality_for_supplement: float = 0.65
    minimum_quality_for_trading_use: float = 0.80
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


class StorageConfig(BaseModel):
    sqlite_path: Path = Path("data/stock_data.db")
    enable_wal: bool = True
    raw_object_root: Path = Path("data/raw_objects")
    parquet_root: Path = Path("data/parquet")
    compress_raw_payload: bool = True
    raw_format: str = "jsonl.gz"
    timezone: str = "Asia/Shanghai"
    log_path: Path = Path("logs/stock_data_ingestion.log")


class DataQualityConfig(BaseModel):
    field_tolerances: dict[str, dict[str, float]] = Field(default_factory=dict)
    provider_reliability: dict[str, float] = Field(default_factory=dict)
    quality_weights: dict[str, float] = Field(default_factory=dict)
    quality_adjustments: dict[str, float] = Field(default_factory=dict)
    conflict_severity_rules: dict[str, str] = Field(default_factory=dict)
    critical_fields: list[str] = Field(default_factory=list)
    supplement_field_whitelist: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    data_sources: DataSourcesConfig
    storage: StorageConfig
    data_quality: DataQualityConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def find_config_dir(config_dir: str | Path | None = None) -> Path:
    # Load .env before reading STOCK_DATA_CONFIG_DIR, so config location itself
    # can be supplied from .env.
    ensure_env_loaded()

    if config_dir:
        return Path(config_dir)
    env_dir = os.getenv("STOCK_DATA_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    cwd_config = Path.cwd() / "config"
    if cwd_config.exists():
        return cwd_config
    return Path(__file__).resolve().parents[1] / "config"


@lru_cache(maxsize=8)
def load_config(config_dir: str | Path | None = None) -> AppConfig:
    # First pass loads .env from cwd/project root. After config dir is resolved,
    # second pass also allows config/.env and parent .env to participate.
    ensure_env_loaded(config_dir=config_dir)
    root = find_config_dir(config_dir)
    ensure_env_loaded(config_dir=root)

    data_sources = DataSourcesConfig.model_validate(_load_yaml(root / "data_sources.yaml"))
    storage_data = _load_yaml(root / "storage.yaml")
    if os.getenv("STOCK_DATA_SQLITE_PATH"):
        storage_data["sqlite_path"] = os.getenv("STOCK_DATA_SQLITE_PATH")
    storage = StorageConfig.model_validate(storage_data)
    data_quality = DataQualityConfig.model_validate(_load_yaml(root / "data_quality.yaml"))
    return AppConfig(data_sources=data_sources, storage=storage, data_quality=data_quality)
