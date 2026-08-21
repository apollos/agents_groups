"""Keep agent config tests hermetic against a developer's shell / .env."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_agent_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENCLAW_AGENT_PRIMARY_MODEL",
        "OPENCLAW_AGENT_FALLBACK_MODELS",
        "INTEL_AGENT_PYTHON",
        "INTEL_AGENT_ENV_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
