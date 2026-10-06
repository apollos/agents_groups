"""Hard wall-clock deadline for vendor calls that carry no timeout of their own."""

from __future__ import annotations

import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def with_deadline(fn: Callable[[], T], seconds: float, what: str) -> T:
    """Run ``fn`` in a daemon thread and raise ``TimeoutError`` after ``seconds``.

    A daemon thread (not ThreadPoolExecutor) is used on purpose: the executor's atexit
    hook joins workers, so a wedged socket would block interpreter shutdown. A timed-out
    call leaks quietly until the hang resolves or the process exits.
    """
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True, name=f"deadline:{what}"[:60])
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"{what} did not finish within {seconds:.0f}s (likely DNS/IPv6 or WAF hang)")
    if "error" in box:
        raise box["error"]
    return box["value"]
