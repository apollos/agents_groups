from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError


PLACEHOLDER_MODELS = {
    "",
    "default",
    "openclaw/default",
    "replace_with_registered_openclaw_model",
    "replace-with-registered-openclaw-model",
    "<registered_openclaw_model>",
}


@dataclass(frozen=True)
class AgentModelConfig:
    primary: str
    fallbacks: list[str]
    require_registered: bool = True
    allow_openclaw_default: bool = False


@dataclass(frozen=True)
class RuntimeConfig:
    agent_id: str
    agent_group: str
    state_sqlite_path: Path
    bus_sqlite_path: Path
    data_sqlite_path: Path
    workspace_root: Path
    log_dir: Path
    reports_dir: Path
    timezone: str = "Asia/Shanghai"

    @property
    def sqlite_path(self) -> Path:
        """Backward-compatible alias for the agent-private state DB."""
        return self.state_sqlite_path


@dataclass(frozen=True)
class ToolConfig:
    mic_enabled: bool
    stock_enabled: bool
    mic_config_dir: str | None
    stock_config_dir: str | None
    python_executable: str
    stock_working_dir: str | None


@dataclass(frozen=True)
class CollectorConfig:
    raw: dict[str, Any]
    path: Path
    runtime: RuntimeConfig
    model: AgentModelConfig
    tools: ToolConfig

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def _expand_path(value: str | os.PathLike[str], base_dir: Path) -> Path:
    text = os.path.expandvars(os.path.expanduser(str(value)))
    p = Path(text)
    if not p.is_absolute():
        p = base_dir / p
    return p.resolve()


def _expand_optional_path(value: str | os.PathLike[str] | None, base_dir: Path) -> str | None:
    if value in {None, ""}:
        return None
    return str(_expand_path(value, base_dir))


def _resolve_workspace_root(runtime_raw: dict[str, Any], base_dir: Path) -> Path:
    configured = runtime_raw.get("workspace_root")
    if configured not in {None, "", "auto", "openclaw"}:
        return _expand_path(configured, base_dir)

    for env_name in ("OPENCLAW_WORKSPACE_ROOT", "OPENCLAW_WORKSPACE", "OPENCLAW_WORKDIR"):
        value = os.environ.get(env_name)
        if value:
            return _expand_path(value, base_dir)

    # Optional best-effort OpenClaw CLI discovery. This intentionally never fails local tests.
    if configured == "openclaw":
        for cmd in (["openclaw", "workspace", "path", "--plain"], ["openclaw", "workspace", "path"]):
            try:
                proc = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
            except Exception:
                continue
            if proc.returncode == 0:
                candidate = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
                if candidate:
                    return _expand_path(candidate, base_dir)
    return base_dir.parent if base_dir.name == "config" else base_dir


def _validate_model(primary: str, allow_default: bool) -> None:
    normalized = (primary or "").strip().lower()
    hard_placeholders = {"replace_with_registered_openclaw_model", "replace-with-registered-openclaw-model", "<registered_openclaw_model>"}
    if normalized in hard_placeholders:
        raise ConfigError(
            "openclaw.model.primary must be set to a registered OpenClaw model; placeholder values are not allowed."
        )
    if not allow_default and normalized in {"", "default", "openclaw/default"}:
        raise ConfigError(
            "openclaw.model.primary must be set to a registered OpenClaw model. "
            "This agent must not silently inherit the OpenClaw default model."
        )


def load_config(path: str | os.PathLike[str]) -> CollectorConfig:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    base_dir = cfg_path.parent

    runtime_raw = raw.get("runtime", {}) or {}
    openclaw_raw = raw.get("openclaw", {}) or {}
    agent_raw = raw.get("agent", {}) or {}
    tools_raw = raw.get("tools", {}) or {}

    agent_id = str(agent_raw.get("agent_id") or "intelligence_collector")
    agent_group = str(agent_raw.get("agent_group") or "intelligence_collector")
    workspace_root = _resolve_workspace_root(runtime_raw, base_dir)

    # New split-store defaults. For backwards compatibility, when legacy sqlite_path is supplied
    # and split paths are absent, all three stores point to that legacy file.
    legacy_sqlite_path = runtime_raw.get("sqlite_path")
    state_path_raw = runtime_raw.get("state_sqlite_path") or runtime_raw.get("state_db_path")
    bus_path_raw = runtime_raw.get("bus_sqlite_path") or runtime_raw.get("bus_db_path")
    data_path_raw = runtime_raw.get("data_sqlite_path") or runtime_raw.get("data_db_path")
    if legacy_sqlite_path and not any([state_path_raw, bus_path_raw, data_path_raw]):
        state_path_raw = bus_path_raw = data_path_raw = legacy_sqlite_path
    state_sqlite_path = _expand_path(state_path_raw or "data/intelligence_collector_state.db", workspace_root)
    bus_sqlite_path = _expand_path(bus_path_raw or "data/ticket_bus.db", workspace_root)
    data_sqlite_path = _expand_path(data_path_raw or "data/intelligence_collector_data.db", workspace_root)

    log_dir = _expand_path(runtime_raw.get("log_dir", "logs"), workspace_root)
    reports_dir = _expand_path(raw.get("reports", {}).get("output_dir", "reports"), workspace_root)

    model_raw = openclaw_raw.get("model", {}) or {}
    primary = str(model_raw.get("primary") or "")
    allow_default = bool(model_raw.get("allow_openclaw_default", False))
    _validate_model(primary, allow_default)

    model = AgentModelConfig(
        primary=primary,
        fallbacks=[str(x) for x in model_raw.get("fallbacks", [])],
        require_registered=bool(model_raw.get("require_registered", True)),
        allow_openclaw_default=allow_default,
    )
    runtime = RuntimeConfig(
        agent_id=agent_id,
        agent_group=agent_group,
        state_sqlite_path=state_sqlite_path,
        bus_sqlite_path=bus_sqlite_path,
        data_sqlite_path=data_sqlite_path,
        workspace_root=workspace_root,
        log_dir=log_dir,
        reports_dir=reports_dir,
        timezone=str(runtime_raw.get("timezone", "Asia/Shanghai")),
    )
    stock_cfg = tools_raw.get("stock_data_collector", {}) or {}
    mic_cfg = tools_raw.get("market_intelligence_collector", {}) or {}
    tools = ToolConfig(
        mic_enabled=bool(mic_cfg.get("enabled", True)),
        stock_enabled=bool(stock_cfg.get("enabled", True)),
        mic_config_dir=_expand_optional_path(mic_cfg.get("config_dir"), workspace_root),
        stock_config_dir=_expand_optional_path(stock_cfg.get("config_dir"), workspace_root),
        python_executable=str(tools_raw.get("python_executable") or os.environ.get("PYTHON", "python")),
        stock_working_dir=_expand_optional_path(stock_cfg.get("working_dir"), workspace_root),
    )
    return CollectorConfig(raw=raw, path=cfg_path, runtime=runtime, model=model, tools=tools)


def deep_get(raw: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = raw
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part, default)
    return cur
