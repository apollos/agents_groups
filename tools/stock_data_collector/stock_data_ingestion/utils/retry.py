from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def call_with_optional_tenacity(func: Callable[[], T], attempts: int = 3) -> T:
    try:
        from tenacity import retry, stop_after_attempt, wait_exponential
    except Exception:
        last_exc: Exception | None = None
        for _ in range(max(1, attempts)):
            try:
                return func()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    @retry(stop=stop_after_attempt(attempts), wait=wait_exponential(multiplier=0.5, max=8), reraise=True)
    def _wrapped() -> T:
        return func()

    return _wrapped()
