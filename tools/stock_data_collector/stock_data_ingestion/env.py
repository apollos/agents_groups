from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


IMPORTANT_ENV_KEYS: tuple[str, ...] = (
    "TUSHARE_TOKEN",
    "JQDATA_USERNAME",
    "JQDATA_PASSWORD",
)

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_LOADED_ENV_FILES: set[Path] = set()


@dataclass(frozen=True)
class EnvLoadResult:
    """Result of a .env autoload attempt."""

    loaded_files: tuple[Path, ...]
    set_keys: tuple[str, ...]
    skipped_existing_keys: tuple[str, ...]
    skipped_empty_keys: tuple[str, ...]
    missing_important_before: tuple[str, ...]
    missing_important_keys: tuple[str, ...]
    disabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        first_file = str(self.loaded_files[0]) if self.loaded_files else None
        return {
            "loaded": bool(self.set_keys),
            "env_file": first_file,
            "env_files": [str(path) for path in self.loaded_files],
            "loaded_vars": list(self.set_keys),
            "set_keys": list(self.set_keys),
            "skipped_existing_vars": list(self.skipped_existing_keys),
            "skipped_existing_keys": list(self.skipped_existing_keys),
            "skipped_empty_vars": list(self.skipped_empty_keys),
            "skipped_empty_keys": list(self.skipped_empty_keys),
            "missing_before": list(self.missing_important_before),
            "missing_after": list(self.missing_important_keys),
            "missing_important_before": list(self.missing_important_before),
            "missing_important_after": list(self.missing_important_keys),
            "disabled": self.disabled,
        }


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in _TRUE_VALUES)


def _is_missing_env_value(value: str | None) -> bool:
    return value is None or value.strip() == ""


def _as_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    return Path(path).expanduser()


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except Exception:
        return path.expanduser().absolute()


def _dedupe_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = _safe_resolve(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return tuple(result)


def _cwd_env_candidates() -> list[Path]:
    candidates: list[Path] = []
    cwd = _safe_resolve(Path.cwd())
    for base in (cwd, *cwd.parents):
        candidates.append(base / ".env")
        candidates.append(base / "config" / ".env")
    return candidates


def _package_env_candidates() -> list[Path]:
    package_root = Path(__file__).resolve().parent
    project_root = package_root.parent
    return [
        project_root / ".env",
        project_root / "config" / ".env",
    ]


def discover_env_files(env_file: str | Path | None = None, config_dir: str | Path | None = None) -> tuple[Path, ...]:
    """Discover .env files in deterministic order.

    Order:
    1. Explicit ``env_file`` argument.
    2. ``STOCK_DATA_ENV_FILE`` environment variable.
    3. ``config_dir/.env`` and ``config_dir.parent/.env`` when a config dir is known.
    4. Current working directory and its parents, including ``config/.env`` in each.
    5. Project root next to the installed package, including ``config/.env``.
    """
    candidates: list[Path] = []

    explicit = _as_path(env_file)
    if explicit is not None:
        candidates.append(explicit)

    env_file_from_env = _as_path(os.getenv("STOCK_DATA_ENV_FILE"))
    if env_file_from_env is not None:
        candidates.append(env_file_from_env)

    cfg = _as_path(config_dir)
    if cfg is not None:
        candidates.extend([cfg / ".env", cfg.parent / ".env"])

    candidates.extend(_cwd_env_candidates())
    candidates.extend(_package_env_candidates())

    return tuple(path for path in _dedupe_paths(candidates) if path.is_file())


def _fallback_parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _read_env_file(path: Path) -> dict[str, str]:
    """Read a .env file as key/value strings."""
    try:
        from dotenv import dotenv_values  # type: ignore

        parsed = dotenv_values(path)
        return {str(k): str(v) for k, v in parsed.items() if k and v is not None}
    except Exception:
        return _fallback_parse_env_file(path)


def _merge_values_into_environ(values: Mapping[str, str], *, override: bool) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    set_keys: list[str] = []
    skipped_existing_keys: list[str] = []
    skipped_empty_keys: list[str] = []

    for key, value in values.items():
        if value is None or str(value).strip() == "":
            skipped_empty_keys.append(key)
            continue
        current = os.environ.get(key)
        if override or _is_missing_env_value(current):
            os.environ[key] = str(value)
            set_keys.append(key)
        else:
            skipped_existing_keys.append(key)

    return tuple(set_keys), tuple(skipped_existing_keys), tuple(skipped_empty_keys)


def ensure_env_loaded(
    *,
    env_file: str | Path | None = None,
    config_dir: str | Path | None = None,
    important_keys: Iterable[str] = IMPORTANT_ENV_KEYS,
    reload: bool = False,
    override: bool | None = None,
) -> EnvLoadResult:
    """Load available .env files into ``os.environ``.

    Every key in .env is loaded so future project-level parameters can be
    configured from .env as well. Existing non-empty OS environment variables are
    not overwritten by default; empty strings are treated as missing values.

    Set ``STOCK_DATA_ENV_OVERRIDE=true`` or pass ``override=True`` to let .env
    override existing values. Set ``STOCK_DATA_DISABLE_ENV_AUTOLOAD=true`` to
    disable automatic .env loading.
    """
    important = tuple(important_keys)
    missing_before = tuple(key for key in important if _is_missing_env_value(os.environ.get(key)))

    if _truthy(os.environ.get("STOCK_DATA_DISABLE_ENV_AUTOLOAD")):
        return EnvLoadResult(
            loaded_files=(),
            set_keys=(),
            skipped_existing_keys=(),
            skipped_empty_keys=(),
            missing_important_before=missing_before,
            missing_important_keys=missing_before,
            disabled=True,
        )

    if override is None:
        override = _truthy(os.environ.get("STOCK_DATA_ENV_OVERRIDE"))

    loaded_files: list[Path] = []
    set_keys: list[str] = []
    skipped_existing_keys: list[str] = []
    skipped_empty_keys: list[str] = []

    for path in discover_env_files(env_file=env_file, config_dir=config_dir):
        resolved = _safe_resolve(path)
        if not reload and resolved in _LOADED_ENV_FILES:
            continue

        values = _read_env_file(resolved)
        new_set_keys, new_skipped_existing, new_skipped_empty = _merge_values_into_environ(values, override=override)
        set_keys.extend(new_set_keys)
        skipped_existing_keys.extend(new_skipped_existing)
        skipped_empty_keys.extend(new_skipped_empty)
        loaded_files.append(resolved)
        _LOADED_ENV_FILES.add(resolved)

    missing_after = tuple(key for key in important if _is_missing_env_value(os.environ.get(key)))
    return EnvLoadResult(
        loaded_files=tuple(loaded_files),
        set_keys=tuple(dict.fromkeys(set_keys)),
        skipped_existing_keys=tuple(dict.fromkeys(skipped_existing_keys)),
        skipped_empty_keys=tuple(dict.fromkeys(skipped_empty_keys)),
        missing_important_before=missing_before,
        missing_important_keys=missing_after,
    )


def load_env(
    *,
    env_file: str | Path | None = None,
    config_dir: str | Path | None = None,
    override: bool | None = None,
    reload: bool = False,
) -> dict[str, Any]:
    """Dictionary-returning wrapper for tests, scripts, and older code."""
    return ensure_env_loaded(env_file=env_file, config_dir=config_dir, override=override, reload=reload).as_dict()


def load_env_if_missing(
    required_vars: Iterable[str] | None = None,
    *,
    env_file: str | Path | None = None,
    config_dir: str | Path | None = None,
    override: bool | None = None,
    reload: bool = False,
) -> dict[str, Any]:
    """Backward-compatible wrapper.

    It now loads all variables from .env, not only selected required variables,
    while still reporting which important variables remain missing.
    """
    return ensure_env_loaded(
        env_file=env_file,
        config_dir=config_dir,
        important_keys=tuple(required_vars or IMPORTANT_ENV_KEYS),
        override=override,
        reload=reload,
    ).as_dict()


def reset_env_loader_state() -> None:
    """Reset internal .env load cache for tests and interactive debugging."""
    _LOADED_ENV_FILES.clear()
