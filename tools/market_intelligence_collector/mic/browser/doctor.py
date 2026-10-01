"""Provider-aware environment preflight (design section 14).

``doctor`` is offline: it checks Python/Playwright, the Edge executable, the
desktop session, profile path/lock and configuration, and reports leftover
worker runs. It never requests the public internet and never calls a model.
``--launch`` explicitly opens the dedicated browser on a blank page and closes
it again to exercise the lifecycle.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from mic.browser.config import ConfigError, resolve_credential_dir, resolve_profile_dir
from mic.browser.profile_lock import probe_lock
from mic.browser.session import BrowserSession, PlaywrightBackend, gui_available

EDGE_CANDIDATES = ("microsoft-edge", "microsoft-edge-stable", "microsoft-edge-beta", "msedge")


def _check(name: str, ok: bool, detail: Any = None, hint: str | None = None,
           severity: str = "error") -> dict[str, Any]:
    out: dict[str, Any] = {"check": name, "ok": bool(ok)}
    if detail is not None:
        out["detail"] = detail
    if not ok and hint:
        out["hint"] = hint
    if not ok:
        out["severity"] = severity
    return out


def find_edge(runtime: dict[str, Any]) -> str | None:
    explicit = runtime.get("executable_path")
    if explicit:
        return explicit if Path(explicit).exists() else None
    for name in EDGE_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    for path in ("/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge"):
        if Path(path).exists():
            return path
    return None


def leftover_runs(runs_dir: Path | None) -> list[dict[str, Any]]:
    """Worker run directories whose heartbeat is stale and that never finished."""
    if runs_dir is None or not runs_dir.exists():
        return []
    out = []
    for d in sorted(runs_dir.iterdir()):
        if not d.is_dir():
            continue
        result = d / "result.json"
        hb = d / "heartbeat.json"
        if result.exists():
            continue
        info: dict[str, Any] = {"run_dir": str(d)}
        if hb.exists():
            try:
                data = json.loads(hb.read_text(encoding="utf-8"))
                info.update({k: data.get(k) for k in ("pid", "attempt_id", "state", "at")})
                info["stale_seconds"] = round(time.time() - float(data.get("at_epoch", 0)), 1)
            except (OSError, ValueError):
                info["heartbeat"] = "unreadable"
        pid = info.get("pid")
        if isinstance(pid, int):
            info["pid_alive"] = _pid_alive(pid)
        out.append(info)
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_doctor(config, *, launch: bool = False, runs_dir: Path | None = None,
               env: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if env is None else env
    checks: list[dict[str, Any]] = []
    try:
        runtime = config.browser_runtime
        checks.append(_check("browser_runtime_config", True,
                             {"enabled": runtime.get("enabled"), "channel": runtime.get("channel"),
                              "headless": runtime.get("headless"),
                              "interaction_mode": runtime.get("interaction_mode")}))
    except ConfigError as exc:
        checks.append(_check("browser_runtime_config", False, str(exc),
                             "fix config/browser_runtime.yaml (unknown fields / types / ranges)"))
        return {"ok": False, "checks": checks}

    checks.append(_check("python", True, {"executable": sys.executable, "version": sys.version.split()[0]}))

    ok, info = PlaywrightBackend.available()
    checks.append(_check("playwright", ok, info,
                         "pip install 'market-intelligence-collector[browser]' "
                         "(Playwright drives the system Edge; no browser download needed)"))

    edge = find_edge(runtime)
    checks.append(_check("edge_executable", edge is not None, edge,
                         "install Microsoft Edge (channel=msedge) or set browser_runtime.executable_path"))

    gui = gui_available(env)
    headless = bool(runtime.get("headless", False))
    checks.append(_check("gui_session", gui or headless,
                         {"DISPLAY": bool(env.get("DISPLAY")), "WAYLAND_DISPLAY": bool(env.get("WAYLAND_DISPLAY"))},
                         "gui_unavailable: start the task from the Ubuntu graphical desktop "
                         "(SSH/systemd sessions have no display; headless is not substituted)"))

    try:
        profile_dir = resolve_profile_dir(runtime, env)
        lock = probe_lock(profile_dir)
        checks.append(_check("profile_dir", True, {"profile_id": profile_dir.name,
                                                   "exists": profile_dir.exists(),
                                                   "locked": lock.get("locked", False),
                                                   "holder": lock.get("holder")}))
        if lock.get("locked"):
            checks.append(_check("profile_lock", False, lock.get("holder"),
                                 "profile_busy: another MIC run holds the profile; wait for it or "
                                 "verify the holder pid before acting", severity="warning"))
        if profile_dir.exists():
            mode = profile_dir.stat().st_mode & 0o777
            checks.append(_check("profile_permissions", mode == 0o700, oct(mode),
                                 "chmod 700 the profile directory", severity="warning"))
    except ConfigError as exc:
        profile_dir = None
        checks.append(_check("profile_dir", False, str(exc),
                             f"export {runtime.get('profile_dir_env')}=<workspace>/browser/profiles/mic-edge"))

    sf = runtime.get("session_fallback") or {}
    if sf.get("enabled"):
        try:
            cred_dir = resolve_credential_dir(runtime, env)
            checks.append(_check("credential_dir", True, {"exists": cred_dir.exists()}))
        except ConfigError as exc:
            checks.append(_check("credential_dir", False, str(exc)))
        for origin, src in (sf.get("cookie_sources") or {}).items():
            set_ = bool((env.get(src.get("source_file_env", "")) or "").strip())
            checks.append(_check(f"cookie_source:{origin}", True,
                                 {"credential_id": src.get("credential_id"), "env_set": set_},
                                 severity="warning"))

    provider = (config.search_providers or {}).get("active")
    providers = (config.search_providers or {}).get("providers", {})
    ptype = (providers.get(provider, {}) or {}).get("type") if isinstance(provider, str) else None
    checks.append(_check("search_provider", True, {"active": provider, "type": ptype,
                                                   "browser_route": ptype == "browser"}))

    leftovers = leftover_runs(runs_dir)
    checks.append(_check("leftover_runs", not leftovers, leftovers,
                         "inspect run dirs without result.json; only act on verified ownership",
                         severity="warning"))

    hard_ok = all(c["ok"] for c in checks if c.get("severity", "error") == "error")
    result: dict[str, Any] = {"ok": hard_ok, "checks": checks,
                              "browser_enabled": bool(runtime.get("enabled"))}

    if launch:
        if not runtime.get("enabled"):
            result["launch"] = {"ok": False, "error": "browser_runtime.enabled is false"}
            result["ok"] = False
            return result
        if not hard_ok or profile_dir is None:
            result["launch"] = {"ok": False, "error": "preconditions failed; launch skipped"}
            return result
        session = BrowserSession(runtime=runtime, run_id="doctor", attempt_id="doctor",
                                 profile_dir=profile_dir)
        start = time.monotonic()
        try:
            session.start()
            with session.page() as page:
                nav = session.navigate(page, "about:blank", 5)
                if nav["status"] == "rejected":
                    # about:blank is not http(s); open via goto directly for the probe.
                    page.goto("about:blank")
            diag = session.close()
            result["launch"] = {"ok": diag.get("cleanup") == "complete",
                                "elapsed_ms": int((time.monotonic() - start) * 1000), **diag}
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", type(exc).__name__)
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
            result["launch"] = {"ok": False, "error_code": code, "error": str(exc)[:300]}
            result["ok"] = False
    return result
