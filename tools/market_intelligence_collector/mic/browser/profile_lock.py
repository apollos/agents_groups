"""Application-level mutual exclusion for the dedicated browser profile (design 8.1).

A persistent Chromium profile cannot be used by two browser instances. The OS
file lock (``fcntl.flock``) is the actual mutex; the JSON content of the lock
file is diagnostics only (run/attempt/pid/start identity) and liveness is never
inferred from file existence. Chromium's own ``Singleton*`` files are left
alone and no other process is ever killed from here.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProfileBusy(RuntimeError):
    """The profile is in use by another run (error code ``profile_busy``)."""

    code = "profile_busy"

    def __init__(self, holder: dict[str, Any] | None):
        self.holder = holder or {}
        super().__init__(f"browser profile busy: {self.holder}")


def _process_start_identity(pid: int) -> str | None:
    """Best-effort pid start identity (Linux /proc starttime) for diagnostics."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            parts = fh.read().rsplit(")", 1)[-1].split()
        return parts[19]  # starttime field (22nd overall, index 19 after comm)
    except (OSError, IndexError):
        return None


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, stat.S_IRWXU)
    except OSError:
        pass
    return path


def write_private_file(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (tmp + replace) with mode 0600."""
    if not path.parent.exists():
        ensure_private_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except Exception:
        try:
            tmp.unlink()
        finally:
            raise
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass
class ProfileLock:
    profile_dir: Path
    run_id: str
    attempt_id: str
    _fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self.profile_dir.parent / f".{self.profile_dir.name}.mic-lock"

    def holder(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return None

    def acquire(self) -> ProfileLock:
        ensure_private_dir(self.profile_dir.parent)
        ensure_private_dir(self.profile_dir)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self.holder()
            os.close(fd)
            raise ProfileBusy(holder) from None
        self._fd = fd
        info = {
            "run_id": self.run_id, "attempt_id": self.attempt_id, "pid": os.getpid(),
            "pid_start": _process_start_identity(os.getpid()),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(info).encode("utf-8"))
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> ProfileLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def probe_lock(profile_dir: Path) -> dict[str, Any]:
    """Doctor helper: is the profile currently locked, and by whom (diagnostics)."""
    lock = ProfileLock(profile_dir, run_id="doctor", attempt_id="doctor")
    if not lock.lock_path.exists():
        return {"locked": False, "lock_path": str(lock.lock_path)}
    fd = os.open(lock.lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"locked": True, "holder": lock.holder(), "lock_path": str(lock.lock_path)}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return {"locked": False, "stale_holder": lock.holder(), "lock_path": str(lock.lock_path)}
    finally:
        os.close(fd)
