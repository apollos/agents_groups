"""Bound the default BaoStock security-master fetch without changing SDK transport.

The provider timeout covers this complete fetch, including login and pagination.
Only the parent ingestion runner persists or normalizes the returned raw result.
The worker stays in the caller's process group for the CLI supervisor's cleanup;
on Linux it also dies if its parent disappears. Other platforms retain ordinary
timeout/interruption cleanup, but do not have the Linux parent-death safeguard.
"""

from __future__ import annotations

import contextlib
import math
import os
import subprocess
import sys
import time
from datetime import datetime

from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorCode, ErrorRecord
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest

_TERMINATE_GRACE_SECONDS = 1.0
_KILL_GRACE_SECONDS = 1.0


class WorkerCleanupError(RuntimeError):
    """The caller must stop; worker termination could not be confirmed."""


# Arm parent-death handling before importing the adapter/SDK. Check the original
# parent PID afterwards as well: it might have exited before prctl was installed.
_BOOTSTRAP = """\
import ctypes, os, signal, sys, runpy
if sys.platform.startswith('linux'):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot arm BaoStock worker parent-death guard')
    if os.getppid() != int(sys.argv[1]):
        os._exit(1)
runpy.run_module('stock_data_ingestion.adapters.baostock_worker', run_name='__main__')
"""


def _worker_command() -> list[str]:
    return [sys.executable, "-c", _BOOTSTRAP, str(os.getpid())]


def _failure(started: datetime, code: ErrorCode, message: str) -> ProviderFetchResult:
    return ProviderFetchResult(
        provider="baostock",
        source_api="query_stock_basic",
        source_site="baostock",
        adapter_version="0.1.0",
        status=AdapterFetchStatus.failed,
        started_at=started,
        completed_at=now_asia_shanghai(),
        error=ErrorRecord(
            provider="baostock",
            source_api="query_stock_basic",
            source_site="baostock",
            error_code=code,
            error_message=message,
            retryable=code == ErrorCode.PROVIDER_TIMEOUT,
        ),
    )


def _stop_and_reap(process: subprocess.Popen[str]) -> None:
    """Do not leave a timed-out or interrupted SDK call alive in the background."""
    try:
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired as exc:
                    raise WorkerCleanupError("BaoStock worker did not exit after forced termination") from exc
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def fetch_security_master_in_worker(
    request: StockDataRequest, *, timeout_seconds: float
) -> ProviderFetchResult:
    """Run once, preserving prior providers when BaoStock hangs or fails.

    At most two additional seconds are reserved for terminate/kill and wait.
    No retry, provider substitution, or fallback to an unbounded in-process call.
    Worker diagnostics are discarded rather than reflecting SDK output (which may
    contain request details) into the public response or corrupting its JSON.
    """
    started = now_asia_shanghai()
    if str(request.request_type) != "security_master":
        return _failure(started, ErrorCode.INVALID_REQUEST, "BaoStock worker only supports security_master")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return _failure(started, ErrorCode.INVALID_REQUEST, "BaoStock timeout_seconds must be positive and finite")
    deadline = time.monotonic() + timeout_seconds
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            _worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            # Inherit the CLI process group: its outer supervisor owns the group.
        )
        stdout, _ = process.communicate(
            input=request.model_dump_json(),
            timeout=max(0.0, deadline - time.monotonic()),
        )
        if process.returncode != 0:
            return _failure(started, ErrorCode.UNKNOWN_ERROR, "BaoStock security_master worker exited unsuccessfully")
        try:
            result = ProviderFetchResult.model_validate_json(stdout)
            if result.provider != "baostock" or result.source_api != "query_stock_basic":
                raise ValueError("unexpected worker result identity")
        except (ValueError, TypeError):
            return _failure(started, ErrorCode.PROVIDER_SCHEMA_CHANGED, "BaoStock security_master worker returned an invalid result")
        if result.error is not None:
            # Keep structured failure identity/counts, not arbitrary SDK exception
            # strings that might echo connection details or request credentials.
            error = result.error.model_copy(update={
                "error_message": f"BaoStock security_master returned {result.error.error_code}",
                "suggested_action": None,
            })
            result = result.model_copy(update={"error": error})
        return result
    except subprocess.TimeoutExpired:
        return _failure(
            started,
            ErrorCode.PROVIDER_TIMEOUT,
            f"BaoStock security_master exceeded its {timeout_seconds:g}s provider deadline; worker terminated without retry",
        )
    except (OSError, UnicodeError):
        # Do not expose exception text, child output, or environment credentials.
        return _failure(started, ErrorCode.PROVIDER_UNAVAILABLE, "BaoStock security_master worker could not be executed")
    finally:
        if process is not None:
            _stop_and_reap(process)


def _close_sdk_socket() -> None:
    """Local close only; SDK logout performs another potentially blocking query."""
    context = sys.modules.get("baostock.common.context")
    if context is None:
        return
    connection = getattr(context, "default_socket", None)
    context.default_socket = None
    if connection is not None:
        with contextlib.suppress(Exception):
            connection.close()


def main() -> int:
    try:
        request = StockDataRequest.model_validate_json(sys.stdin.read())
        if str(request.request_type) != "security_master":
            return 2
        # The SDK prints during login and failures. Keep the protocol's stdout a
        # single JSON document, including when a supplemental query emits noise.
        with contextlib.redirect_stdout(sys.stderr):
            from stock_data_ingestion.adapters.baostock_adapter import BaoStockAdapter

            result = BaoStockAdapter().fetch_security_master(request)
        sys.stdout.write(result.model_dump_json())
        sys.stdout.flush()
        return 0
    except Exception:
        # Parent publishes a fixed error; neither a traceback nor raw SDK output.
        return 1
    finally:
        _close_sdk_socket()


if __name__ == "__main__":
    raise SystemExit(main())
