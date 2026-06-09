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
    cfg_dir = Path(config_dir) if config_dir else _project_root() / "config"
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
    return MICConfig(raw=raw, config_dir=cfg_dir)
