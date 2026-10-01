"""Configuration loading.

All behaviour is config-driven (spec section 2.1). This module loads the YAML
files under ``config/`` into a single, attribute-friendly ``MICConfig`` object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

CONFIG_FILES = [
    "access_profiles",
    "search_providers",
    "target_profiles",
    "analyst_taxonomy",
    "query_families",
    "query_scoring",
    "source_packs",
    "model_registry",
    "model_policies",
    "merge_policy",
    "call_governance",
    "output_schema",
    "storage_policy",
    "browser_runtime",
]


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class MICConfig:
    """Aggregated configuration. Each attribute maps to one YAML file's root."""

    raw: dict[str, Any] = field(default_factory=dict)
    config_dir: Path = field(default_factory=lambda: _project_root() / "config")
    _browser_runtime_cache: dict[str, Any] | None = field(default=None, repr=False)

    # Convenience accessors -------------------------------------------------
    @property
    def search_providers(self) -> dict[str, Any]:
        return self.raw.get("search_providers", {})

    @property
    def target_profiles(self) -> dict[str, Any]:
        return self.raw.get("target_profiles", {})

    @property
    def analyst_taxonomy(self) -> dict[str, Any]:
        return self.raw.get("analyst_taxonomy", {})

    @property
    def strong_fact_keywords(self) -> list[str]:
        return self.raw.get("strong_fact_keywords", [])

    @property
    def query_families(self) -> dict[str, Any]:
        return self.raw.get("query_families", {})

    @property
    def query_scoring(self) -> dict[str, Any]:
        return self.raw.get("query_scoring", {})

    @property
    def source_packs(self) -> dict[str, Any]:
        return self.raw.get("source_packs", {})

    @property
    def source_type_by_domain(self) -> dict[str, str]:
        return self.raw.get("source_type_by_domain", {})

    @property
    def model_registry(self) -> dict[str, Any]:
        return self.raw.get("model_registry", {})

    @property
    def pricing_hints(self) -> dict[str, Any]:
        return self.raw.get("pricing_hints", {})

    @property
    def model_policies(self) -> dict[str, Any]:
        return self.raw.get("model_policies", {})

    @property
    def merge_policy(self) -> dict[str, Any]:
        return self.raw.get("merge_policy", {})

    @property
    def call_governance(self) -> dict[str, Any]:
        return self.raw.get("call_governance", {})

    @property
    def output_schema(self) -> dict[str, Any]:
        return self.raw.get("output_schema", {})

    @property
    def storage_policy(self) -> dict[str, Any]:
        return self.raw.get("storage_policy", {})

    @property
    def access_profiles(self) -> dict[str, Any]:
        return self.raw.get("access_profiles", {})

    @property
    def browser_runtime(self) -> dict[str, Any]:
        """Validated browser runtime block (defaults with enabled=False when absent)."""
        if self._browser_runtime_cache is None:
            from mic.browser.config import validate_browser_runtime
            self._browser_runtime_cache = validate_browser_runtime(
                self.raw.get("browser_runtime"))
        return self._browser_runtime_cache

    def set_browser_runtime(self, block: dict[str, Any] | None) -> None:
        """Replace the browser block and re-validate (tests / probes)."""
        self.raw["browser_runtime"] = block
        self._browser_runtime_cache = None
        self.browser_runtime  # noqa: B018 - validate now

    @property
    def browser_enabled(self) -> bool:
        return bool(self.browser_runtime.get("enabled"))

    @property
    def browser_fetch(self) -> dict[str, Any]:
        """``access_profiles.browser_fetch`` block (reader fetch strategy)."""
        return self.access_profiles.get("browser_fetch", {}) or {}

    # Runtime ---------------------------------------------------------------
    @property
    def database_url(self) -> str:
        return os.environ.get("MIC_DATABASE_URL", "sqlite:///mic.db")

    @property
    def allow_mock(self) -> bool:
        return os.environ.get("MIC_ALLOW_MOCK", "true").lower() in ("1", "true", "yes")

    def get_target_profile(self, target_id: str) -> dict[str, Any] | None:
        return self.target_profiles.get(target_id)


def load_config(config_dir: str | Path | None = None) -> MICConfig:
    """Load all config files and environment variables."""
    load_dotenv(_project_root() / ".env")
    # Explicit argument (Agent adapter / tests) > MIC_CONFIG_DIR (lets the `mic` CLI operate a
    # deployment config dir outside the source tree) > repo config/.
    env_dir = os.environ.get("MIC_CONFIG_DIR", "").strip()
    cfg_dir = Path(config_dir) if config_dir else (Path(env_dir) if env_dir else _project_root() / "config")
    if not cfg_dir.is_dir():
        raise FileNotFoundError(f"MIC config dir not found: {cfg_dir}")
    raw: dict[str, Any] = {}
    for name in CONFIG_FILES:
        data = _read_yaml(cfg_dir / f"{name}.yaml")
        # Each file is keyed by its versioned root; flatten that one level so
        # consumers can read e.g. cfg.query_families["families"].
        if name in data:
            raw[name] = data[name]
        # Some files carry extra top-level keys (e.g. source_type_by_domain).
        for extra_key, extra_val in data.items():
            if extra_key != name:
                raw[extra_key] = extra_val
    cfg = MICConfig(raw=raw, config_dir=cfg_dir)
    # Fail fast on a mistyped browser block instead of silently running with
    # defaults (design 13.4). Validation never opens a browser or the network.
    cfg.browser_runtime  # noqa: B018 - property call validates and caches
    validate_search_provider_config(cfg)
    return cfg


def validate_search_provider_config(cfg: MICConfig) -> None:
    """Cross-file checks for the browser search provider (design 5.4).

    The browser provider has its own internal engine scheduler; wrapping it in
    the legacy composite/fallback chain would double-count budget, so that
    combination is rejected at load time.
    """
    from mic.browser.config import ConfigError

    sp = cfg.search_providers or {}
    providers = sp.get("providers", {}) or {}
    active = sp.get("active", "mock")
    names = [active] if isinstance(active, str) else list(active or [])
    browser_names = [n for n in names if (providers.get(n, {}) or {}).get("type") == "browser"]
    if not browser_names:
        return
    if len(names) > 1:
        raise ConfigError(
            "search_providers.active: a browser provider cannot be combined with other "
            "active providers (internal engine scheduling replaces the composite chain)")
    fallback = sp.get("fallback")
    if fallback not in (None, [], ""):
        raise ConfigError(
            "search_providers.fallback must be empty when the active provider is of type "
            "browser (no double fallback layers)")
    pcfg = providers.get(browser_names[0], {}) or {}
    from mic.browser_search import validate_browser_provider_config

    try:
        validate_browser_provider_config(pcfg)
    except ConfigError as exc:
        raise ConfigError(f"search_providers.providers.{browser_names[0]}: {exc}") from exc
    if not cfg.browser_enabled:
        raise ConfigError(
            "search_providers.active selects a browser provider but browser_runtime.enabled "
            "is false; enable browser_runtime.yaml or choose another provider")
