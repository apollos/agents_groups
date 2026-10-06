"""Network environment helpers shared by provider adapters.

``STOCK_DATA_PREFER_IPV4=true`` makes urllib3 (and therefore ``requests`` and AKShare)
resolve only IPv4 addresses. Several Chinese data hosts publish AAAA records that are
black-holed on some networks (observed 2026-10-06: ``curl -6`` to push2his.eastmoney.com
times out while IPv4 answers in <0.5s); without a per-request timeout AKShare then hangs
until the kernel gives up. This is an environment switch, not a data-source decision, so
it is opt-in and documented in README / .env.example.
"""

from __future__ import annotations

import os
import socket
from typing import Callable

_TRUE = {"1", "true", "yes", "y", "on"}
_applied = False
_original_gai_family: Callable[[], int] | None = None


def prefer_ipv4_enabled() -> bool:
    return str(os.getenv("STOCK_DATA_PREFER_IPV4", "")).strip().lower() in _TRUE


def apply_ipv4_preference_if_configured() -> bool:
    """Patch urllib3's address-family selector when the env switch is on. Idempotent."""
    global _applied, _original_gai_family
    if _applied or not prefer_ipv4_enabled():
        return _applied
    try:
        import urllib3.util.connection as urllib3_connection  # type: ignore
    except Exception:  # noqa: BLE001 - urllib3 missing means requests is missing too
        return False
    _original_gai_family = urllib3_connection.allowed_gai_family
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
    _applied = True
    return True


def reset_ipv4_preference() -> None:
    """Undo the patch (tests)."""
    global _applied, _original_gai_family
    if not _applied:
        return
    try:
        import urllib3.util.connection as urllib3_connection  # type: ignore

        if _original_gai_family is not None:
            urllib3_connection.allowed_gai_family = _original_gai_family
    except Exception:  # noqa: BLE001
        pass
    _applied = False
    _original_gai_family = None
