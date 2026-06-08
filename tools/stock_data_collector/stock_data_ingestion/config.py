from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, Field, model_validator

from stock_data_ingestion.env import ensure_env_loaded

KNOWN_PROVIDERS: tuple[str, ...] = ("tushare", "akshare", "baostock", "joinquant")
DEFAULT_PROVIDER_PRIORITY: list[str] = ["tushare", "akshare", "baostock", "joinquant"]
_PROVIDER_ALIASES: dict[str, str] = {
    "tushare": "tushare",
    "ts": "tushare",
    "tu": "tushare",
    "tu_share": "tushare",
    "thushare": "tushare",  # tolerate the common THUShare typo
    "thu_share": "tushare",
    "akshare": "akshare",
    "ak": "akshare",
    "akshare_tencent": "akshare",
    "tencent": "akshare",
    "baostock": "baostock",
    "bao_stock": "baostock",
    "bs": "baostock",
    "证券宝": "baostock",
    "joinquant": "joinquant",
    "jointquant": "joinquant",
    "jqdata": "joinquant",
    "jq": "joinquant",
}
REQUEST_TYPE_ENV_SUFFIXES: dict[str, str] = {
    "security_master": "SECURITY_MASTER",
    "trade_calendar": "TRADE_CALENDAR",
    "trading_status": "TRADING_STATUS",
    "historical_bars": "HISTORICAL_BARS",
    "realtime_quote": "REALTIME_QUOTE",
    "adj_factor": "ADJ_FACTOR",
    "financial_statement": "FINANCIAL_STATEMENT",
    "financial_indicator": "FINANCIAL_INDICATOR",
    "valuation_metric": "VALUATION_METRIC",
    "industry_concept": "INDUSTRY_CONCEPT",
    "money_flow": "MONEY_FLOW",
    "index_data": "INDEX_DATA",
    "corporate_action": "CORPORATE_ACTION",
}


def normalize_provider_name(provider: str) -> str:
    key = str(provider or "").strip().lower().replace("-", "_").replace(" ", "")
    if key in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[key]
    raise ValueError(f"UNKNOWN_PROVIDER: {provider!r}. Supported providers: {', '.join(KNOWN_PROVIDERS)}")


def parse_provider_list(value: str | Iterable[str] | None) -> list[str]:
    """Parse provider names from env/config/CLI, preserving order and removing duplicates."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.lower() in {"none", "null", "off", "disabled"}:
            return []
        raw_items = [item for item in re.split(r"[,;\s]+", text) if item]
    else:
        raw_items = [str(item) for item in value if str(item).strip()]
    result: list[str] = []
    for item in raw_items:
        normalized = normalize_provider_name(item)
        if normalized not in result:
            result.append(normalized)
    return result


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled", "disable"}:
        return False
    return None


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
    # Hard allow-list. When set, only these providers are active even if a
    # provider entry says enabled=true. Use this for normal operations.
    active_providers: list[str] | None = None
    canonical_provider: str = "tushare"
    provider_priority: list[str] = Field(default_factory=lambda: list(DEFAULT_PROVIDER_PRIORITY))
    validator_providers: list[str] = Field(default_factory=lambda: ["akshare", "baostock", "joinquant"])
    supplement_providers: list[str] = Field(default_factory=lambda: ["akshare", "baostock", "joinquant"])
    # Optional per request-type provider list. Keys use request_type enum values,
    # e.g. historical_bars, security_master, money_flow.
    request_provider_overrides: dict[str, list[str]] | None = Field(default_factory=dict)
    allow_field_level_merge: bool = True
    allow_fallback_when_canonical_missing: bool = True
    allow_majority_override_canonical: bool = False
    quarantine_on_critical_conflict: bool = True
    manual_review_on_trading_critical_conflict: bool = True
    minimum_quality_for_supplement: float = 0.65
    minimum_quality_for_trading_use: float = 0.80
    market_data_lookback_days: int = 400
    financial_lookback_quarters: int = 8
    default_daily_update_time: str = "20:30"
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_provider_fields(self) -> "DataSourcesConfig":
        if self.active_providers is not None:
            self.active_providers = parse_provider_list(self.active_providers)
        self.provider_priority = parse_provider_list(self.provider_priority) or list(DEFAULT_PROVIDER_PRIORITY)
        self.validator_providers = parse_provider_list(self.validator_providers)
        self.supplement_providers = parse_provider_list(self.supplement_providers)
        self.canonical_provider = normalize_provider_name(self.canonical_provider)

        normalized_providers: dict[str, ProviderConfig] = {}
        for name, cfg in self.providers.items():
            normalized_providers[normalize_provider_name(name)] = cfg
        for name in KNOWN_PROVIDERS:
            normalized_providers.setdefault(name, ProviderConfig())
        self.providers = normalized_providers

        normalized_overrides: dict[str, list[str]] = {}
        if self.request_provider_overrides is None:
            self.request_provider_overrides = {}
        for request_type, providers in self.request_provider_overrides.items():
            key = str(request_type).strip().lower().replace("-", "_")
            parsed = parse_provider_list(providers)
            if parsed:
                normalized_overrides[key] = parsed
        self.request_provider_overrides = normalized_overrides

        if self.active_providers is not None:
            allowed = set(self.active_providers)
            for name, cfg in self.providers.items():
                cfg.enabled = name in allowed and cfg.enabled
            self.provider_priority = [p for p in self.provider_priority if p in allowed]
            for provider in self.active_providers:
                if provider not in self.provider_priority:
                    self.provider_priority.append(provider)

        active = self.effective_provider_priority()
        if not active:
            raise ValueError("INVALID_PROVIDER_CONFIG: at least one provider must be enabled")
        if self.canonical_provider not in active:
            self.canonical_provider = active[0]

        self.validator_providers = [p for p in self.validator_providers if p in active and p != self.canonical_provider]
        self.supplement_providers = [p for p in self.supplement_providers if p in active and p != self.canonical_provider]
        for request_type, providers in list(self.request_provider_overrides.items()):
            filtered = [p for p in providers if p in active]
            if filtered:
                self.request_provider_overrides[request_type] = filtered
            else:
                self.request_provider_overrides.pop(request_type, None)
        return self

    def effective_provider_priority(self) -> list[str]:
        ordered = [p for p in self.provider_priority if self.providers.get(p, ProviderConfig()).enabled]
        for provider in KNOWN_PROVIDERS:
            cfg = self.providers.get(provider)
            if cfg and cfg.enabled and provider not in ordered:
                ordered.append(provider)
        if self.active_providers is not None:
            allowed = set(self.active_providers)
            ordered = [p for p in ordered if p in allowed]
        return ordered

    def effective_canonical_provider(self) -> str:
        active = self.effective_provider_priority()
        if self.canonical_provider in active:
            return self.canonical_provider
        if not active:
            raise ValueError("INVALID_PROVIDER_CONFIG: no enabled providers")
        return active[0]

    def provider_is_enabled(self, provider: str) -> bool:
        provider = normalize_provider_name(provider)
        if self.active_providers is not None and provider not in self.active_providers:
            return False
        return bool(self.providers.get(provider, ProviderConfig()).enabled)

    def providers_for_request(self, request_type: str) -> list[str]:
        key = str(request_type).strip().lower().replace("-", "_")
        configured = self.request_provider_overrides.get(key) or self.effective_provider_priority()
        return [provider for provider in configured if self.provider_is_enabled(provider)]

    def canonical_for_request(self, request_type: str) -> str:
        providers = self.providers_for_request(request_type)
        if self.canonical_provider in providers:
            return self.canonical_provider
        return providers[0] if providers else self.effective_canonical_provider()


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


def _split_env_provider_list(*names: str) -> list[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return parse_provider_list(value)
    return []


def _apply_provider_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply provider selection from .env/shell before Pydantic validation.

    Supported variables:
      STOCK_DATA_ACTIVE_PROVIDERS=tushare,akshare
      STOCK_DATA_ENABLED_PROVIDERS=tushare,akshare
      STOCK_DATA_PROVIDERS=tushare,akshare
      STOCK_DATA_PROVIDER_PRIORITY=akshare,tushare
      STOCK_DATA_CANONICAL_PROVIDER=akshare
      STOCK_DATA_DISABLED_PROVIDERS=joinquant
      STOCK_DATA_ENABLE_JOINQUANT=false
      STOCK_DATA_ENABLE_BAOSTOCK=true
      STOCK_DATA_PROVIDERS_HISTORICAL_BARS=tushare,akshare,baostock
      STOCK_DATA_PROVIDERS_SECURITY_MASTER=akshare
    """

    result = dict(data or {})
    providers_cfg = dict(result.get("providers") or {})

    active = _split_env_provider_list("STOCK_DATA_ACTIVE_PROVIDERS", "STOCK_DATA_ENABLED_PROVIDERS", "STOCK_DATA_PROVIDERS")
    priority = _split_env_provider_list("STOCK_DATA_PROVIDER_PRIORITY")
    disabled = _split_env_provider_list("STOCK_DATA_DISABLED_PROVIDERS", "STOCK_DATA_EXCLUDED_PROVIDERS")
    canonical = os.getenv("STOCK_DATA_CANONICAL_PROVIDER")

    if active:
        result["active_providers"] = active
        result["provider_priority"] = active
        for provider in KNOWN_PROVIDERS:
            cfg = dict(providers_cfg.get(provider) or {})
            cfg["enabled"] = provider in active
            providers_cfg[provider] = cfg
    elif priority:
        result["provider_priority"] = priority

    for provider in KNOWN_PROVIDERS:
        env_flag = _parse_bool(os.getenv(f"STOCK_DATA_ENABLE_{provider.upper()}"))
        if env_flag is None:
            env_flag = _parse_bool(os.getenv(f"STOCK_DATA_PROVIDER_{provider.upper()}_ENABLED"))
        if env_flag is not None:
            cfg = dict(providers_cfg.get(provider) or {})
            cfg["enabled"] = env_flag
            providers_cfg[provider] = cfg
            if not env_flag and provider not in disabled:
                disabled.append(provider)

    if disabled:
        disabled_set = set(disabled)
        configured_active = parse_provider_list(result.get("active_providers")) if result.get("active_providers") is not None else []
        if configured_active:
            configured_active = [provider for provider in configured_active if provider not in disabled_set]
            result["active_providers"] = configured_active
            result["provider_priority"] = configured_active
        for provider in disabled_set:
            cfg = dict(providers_cfg.get(provider) or {})
            cfg["enabled"] = False
            providers_cfg[provider] = cfg

    if canonical:
        result["canonical_provider"] = normalize_provider_name(canonical)

    request_overrides = dict(result.get("request_provider_overrides") or {})
    for request_type, suffix in REQUEST_TYPE_ENV_SUFFIXES.items():
        override = _split_env_provider_list(
            f"STOCK_DATA_PROVIDERS_{suffix}",
            f"STOCK_DATA_ACTIVE_PROVIDERS_{suffix}",
        )
        if override:
            request_overrides[request_type] = override
    if request_overrides:
        result["request_provider_overrides"] = request_overrides

    result["providers"] = providers_cfg
    return result


def find_config_dir(config_dir: str | Path | None = None) -> Path:
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
    explicit_config_dir = config_dir is not None
    ensure_env_loaded(config_dir=config_dir)
    root = find_config_dir(config_dir)
    ensure_env_loaded(config_dir=root)

    data_sources = DataSourcesConfig.model_validate(_apply_provider_env_overrides(_load_yaml(root / "data_sources.yaml")))
    storage_data = _load_yaml(root / "storage.yaml")
    if not explicit_config_dir and os.getenv("STOCK_DATA_SQLITE_PATH"):
        storage_data["sqlite_path"] = os.getenv("STOCK_DATA_SQLITE_PATH")
    storage = StorageConfig.model_validate(storage_data)
    data_quality = DataQualityConfig.model_validate(_load_yaml(root / "data_quality.yaml"))
    return AppConfig(data_sources=data_sources, storage=storage, data_quality=data_quality)
