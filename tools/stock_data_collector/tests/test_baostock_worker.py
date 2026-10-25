from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stock_data_ingestion.adapters import baostock_worker as worker
from stock_data_ingestion.config import (
    AppConfig,
    DataQualityConfig,
    DataSourcesConfig,
    ProviderConfig,
    StorageConfig,
)
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest
from stock_data_ingestion.services import ingestion_runner
from stock_data_ingestion.storage.database import Database
from stock_data_ingestion.storage.raw_object_store import RawObjectStore


_FAKE_SDK = r'''
import os, signal, time
from pathlib import Path
from types import SimpleNamespace
from baostock.common import context

root = Path(os.environ["BAOSTOCK_TEST_MARKERS"])
scenario = os.environ.get("BAOSTOCK_TEST_SCENARIO", "success")
(root / "pid").write_text(str(os.getpid()))

def hang(stage):
    (root / stage).write_text("entered")
    if scenario == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(3)
    (root / "late_api").write_text("should never execute after timeout")
    time.sleep(60)

class Socket:
    def close(self):
        (root / "socket_closed").write_text("closed")

class Result:
    error_code = "0"
    error_msg = ""
    def __init__(self, fields=(), rows=(), paginate=False):
        self.fields, self.rows, self.index = list(fields), list(rows), -1
        self.paginate = paginate
    def next(self):
        if self.paginate:
            hang("pagination")
        self.index += 1
        return self.index < len(self.rows)
    def get_row_data(self):
        return self.rows[self.index]

def login():
    context.default_socket = Socket()
    print("SDK login stdout noise fixture-secret-api-key")
    if scenario in ("login", "ignore_term"):
        hang("login")
    if scenario == "login_exception":
        raise RuntimeError("fixture-secret-api-key")
    return SimpleNamespace(error_code="0")

def logout():
    (root / "logout_called").write_text("unexpected network logout")
    hang("logout")

def query_stock_industry():
    print("SDK industry stdout noise")
    if scenario == "industry":
        hang("industry")
    return Result(["code", "industry"], [["sh.600000", "行业样例"]])

def query_stock_basic(code=None):
    print("SDK basic stdout noise")
    if scenario == "basic":
        hang("basic")
    if scenario == "provider_exception":
        raise TimeoutError("timed out fixture-secret-api-key")
    return Result(
        ["code", "code_name", "ipoDate", "outDate", "type", "status", "provider_extra"],
        [[code or "sh.600000", "证券样例", "2000-01-01", "", "1", "1", "原文（括号）"]],
        paginate=scenario == "pagination",
    )
'''


@pytest.fixture
def fake_sdk(tmp_path, monkeypatch):
    """Real child process, fake SDK module: no vendor connection is possible."""
    sdk_root = tmp_path / "fake_sdk"
    package = sdk_root / "baostock"
    (package / "common").mkdir(parents=True)
    (package / "__init__.py").write_text(_FAKE_SDK, encoding="utf-8")
    (package / "common" / "__init__.py").write_text("")
    (package / "common" / "context.py").write_text("default_socket = None\n")
    markers = tmp_path / "markers"
    markers.mkdir()
    project = Path(__file__).resolve().parents[1]
    # Prepend the fake SDK explicitly in child PYTHONPATH, irrespective of what
    # version is installed on the host; do not rely on parent sys.modules mocks.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(sdk_root), str(project)]))
    monkeypatch.setenv("STOCK_DATA_DISABLE_ENV_AUTOLOAD", "true")
    monkeypatch.setenv("BAOSTOCK_TEST_MARKERS", str(markers))
    return markers


def request(**kwargs):
    return StockDataRequest(
        request_id="req_worker_test", request_type="security_master",
        tickers=["600000.SH"], export_parquet=False, **kwargs,
    )


def _alive(pid: int) -> bool:
    if sys.platform.startswith("linux"):
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists():
            return False
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_success_round_trips_full_raw_result_and_closes_without_logout(fake_sdk):
    result = worker.fetch_security_master_in_worker(request(), timeout_seconds=5)
    assert result.status == "success"
    assert result.provider == "baostock"
    assert result.source_api == "query_stock_basic"
    assert result.rows_fetched == 1
    row = result.raw_records[0]
    assert row["normalized_ticker"] == "600000.SH"
    assert row["code_name"] == "证券样例"
    assert row["industry"] == "行业样例"
    assert row["provider_extra"] == "原文（括号）"
    assert result.started_at <= result.completed_at
    assert (fake_sdk / "socket_closed").exists()
    assert not (fake_sdk / "logout_called").exists()
    assert not _alive(int((fake_sdk / "pid").read_text()))


@pytest.mark.parametrize("stage", ["login", "industry", "basic", "pagination"])
def test_each_network_stage_is_bounded_and_worker_reaped(fake_sdk, monkeypatch, stage):
    monkeypatch.setenv("BAOSTOCK_TEST_SCENARIO", stage)
    began = time.monotonic()
    result = worker.fetch_security_master_in_worker(request(), timeout_seconds=1)
    assert time.monotonic() - began < 3.5
    assert (fake_sdk / stage).exists(), "fixture must reach the simulated vendor hang"
    assert result.error.error_code == ErrorCode.PROVIDER_TIMEOUT
    assert result.error.retry_count == 0
    assert result.raw_records == []
    assert not _alive(int((fake_sdk / "pid").read_text()))
    assert not (fake_sdk / "late_api").exists()


def test_ignoring_term_is_killed_and_cannot_execute_late_api(fake_sdk, monkeypatch):
    monkeypatch.setenv("BAOSTOCK_TEST_SCENARIO", "ignore_term")
    began = time.monotonic()
    result = worker.fetch_security_master_in_worker(request(), timeout_seconds=1)
    assert 1.8 <= time.monotonic() - began < 3.5
    assert result.error.error_code == ErrorCode.PROVIDER_TIMEOUT
    pid = int((fake_sdk / "pid").read_text())
    assert not _alive(pid)
    # Cross the fake SDK's scheduled late action. A daemon-thread timeout would
    # allow that action to happen after the caller had already received failure.
    while time.monotonic() - began < 3.5:
        time.sleep(0.05)
    assert not (fake_sdk / "late_api").exists()


@pytest.mark.parametrize("scenario", ["login_exception", "provider_exception"])
def test_sdk_errors_and_stdout_noise_do_not_expose_details(fake_sdk, monkeypatch, scenario):
    monkeypatch.setenv("BAOSTOCK_TEST_SCENARIO", scenario)
    result = worker.fetch_security_master_in_worker(request(), timeout_seconds=5)
    assert result.status == "failed"
    assert "fixture-secret-api-key" not in result.model_dump_json()
    assert "SDK" not in result.model_dump_json()
    assert result.error.error_code == (
        ErrorCode.PROVIDER_TIMEOUT if scenario == "provider_exception" else ErrorCode.UNKNOWN_ERROR
    )
    assert (fake_sdk / "socket_closed").exists()
    assert not (fake_sdk / "logout_called").exists()


@pytest.mark.parametrize("script,code", [
    ("print('fixture-secret-api-key'); raise SystemExit(3)", ErrorCode.UNKNOWN_ERROR),
    ("print('fixture-secret-api-key invalid json')", ErrorCode.PROVIDER_SCHEMA_CHANGED),
    ("print('{}')", ErrorCode.PROVIDER_SCHEMA_CHANGED),
])
def test_nonzero_or_invalid_protocol_is_fixed_structured_failure(monkeypatch, script, code):
    monkeypatch.setattr(worker, "_worker_command", lambda: [sys.executable, "-c", script])
    result = worker.fetch_security_master_in_worker(request(), timeout_seconds=5)
    assert result.error.error_code == code
    assert result.status == "failed"
    assert "fixture-secret-api-key" not in result.model_dump_json()


def test_wrong_provider_protocol_is_rejected(monkeypatch):
    payload = ProviderFetchResult(
        provider="other", source_api="query_stock_basic", source_site="other",
        adapter_version="test", status="success", raw_records=[{"code": "sh.600000"}],
    ).model_dump_json()
    monkeypatch.setattr(worker, "_worker_command", lambda: [sys.executable, "-c", f"print({payload!r})"])
    result = worker.fetch_security_master_in_worker(request(), timeout_seconds=5)
    assert result.error.error_code == ErrorCode.PROVIDER_SCHEMA_CHANGED


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_never_launches(monkeypatch, timeout):
    monkeypatch.setattr(worker, "_worker_command", lambda: pytest.fail("must not launch"))
    result = worker.fetch_security_master_in_worker(request(), timeout_seconds=timeout)
    assert result.error.error_code == ErrorCode.INVALID_REQUEST


def test_wrong_request_type_never_launches(monkeypatch):
    monkeypatch.setattr(worker, "_worker_command", lambda: pytest.fail("must not launch"))
    req = StockDataRequest(request_id="wrong_type", request_type="historical_bars", tickers=["600000.SH"])
    result = worker.fetch_security_master_in_worker(req, timeout_seconds=1)
    assert result.error.error_code == ErrorCode.INVALID_REQUEST


def test_parent_keyboard_interrupt_reaps_worker(fake_sdk, monkeypatch):
    original_popen = worker.subprocess.Popen
    children = []

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        children.append(process)

        def interrupted(**kwargs):
            raise KeyboardInterrupt

        process.communicate = interrupted
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    with pytest.raises(KeyboardInterrupt):
        worker.fetch_security_master_in_worker(request(), timeout_seconds=5)
    assert len(children) == 1
    assert children[0].poll() is not None


def test_unconfirmed_cleanup_is_fatal_not_a_provider_result(monkeypatch):
    class Unreapable:
        stdin = stdout = None

        def communicate(self, **kwargs):
            raise subprocess.TimeoutExpired("fake_worker", 1)

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, **kwargs):
            raise subprocess.TimeoutExpired("fake_worker", kwargs["timeout"])

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **kw: Unreapable())
    with pytest.raises(worker.WorkerCleanupError, match="did not exit"):
        worker.fetch_security_master_in_worker(request(), timeout_seconds=1)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux parent-death safeguard")
def test_parent_sigkill_does_not_leave_sdk_worker(fake_sdk, monkeypatch):
    monkeypatch.setenv("BAOSTOCK_TEST_SCENARIO", "login")
    script = (
        "from stock_data_ingestion.adapters.baostock_worker import fetch_security_master_in_worker\n"
        "from stock_data_ingestion.schemas.requests import StockDataRequest\n"
        "fetch_security_master_in_worker(StockDataRequest(request_id='orphan', "
        "request_type='security_master', tickers=['600000.SH']), timeout_seconds=60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker_pid = None
    try:
        deadline = time.monotonic() + 5
        while not (fake_sdk / "login").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (fake_sdk / "login").exists()
        worker_pid = int((fake_sdk / "pid").read_text())
        parent.kill()
        parent.wait(timeout=2)
        deadline = time.monotonic() + 2
        while _alive(worker_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _alive(worker_pid)
        assert not (fake_sdk / "late_api").exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
        if worker_pid is not None and _alive(worker_pid):
            os.kill(worker_pid, signal.SIGKILL)


def _config(tmp_path):
    return AppConfig(
        data_sources=DataSourcesConfig(
            active_providers=["tushare", "akshare", "baostock"],
            provider_priority=["tushare", "akshare", "baostock"],
            providers={name: ProviderConfig() for name in ("tushare", "akshare", "baostock")},
        ),
        storage=StorageConfig(
            raw_object_root=tmp_path / "raw", parquet_root=tmp_path / "parquet",
            sqlite_path=tmp_path / "stock.db",
        ),
        data_quality=DataQualityConfig(),
    )


def _result(provider, rows):
    return ProviderFetchResult(
        provider=provider, source_api="security_master", source_site=provider,
        adapter_version="test", status="success", raw_records=rows,
        started_at=now_asia_shanghai(), completed_at=now_asia_shanghai(),
    )


def test_default_runner_passes_config_only_for_security_master(fake_sdk, monkeypatch, tmp_path):
    config = _config(tmp_path)
    config.data_sources.providers["baostock"].timeout_seconds = 7
    runner = ingestion_runner.IngestionRunner(config, RawObjectStore(config.storage.raw_object_root))
    calls = []

    def fake_worker(req, *, timeout_seconds):
        calls.append((req, timeout_seconds))
        return _result("baostock", [])

    monkeypatch.setattr(ingestion_runner, "fetch_security_master_in_worker", fake_worker)
    adapter = runner.adapters["baostock"]
    runner._fetch(adapter, request())
    assert calls[0][1] == 7
    expected = _result("baostock", [{"marker": "unchanged"}])
    monkeypatch.setattr(adapter, "fetch_trade_calendar", lambda req: expected)
    req = StockDataRequest(request_id="calendar", request_type="trade_calendar")
    assert runner._fetch(adapter, req) is expected
    assert len(calls) == 1


def test_injected_adapter_cannot_accidentally_launch_real_sdk(fake_sdk, monkeypatch, tmp_path):
    config = _config(tmp_path)
    expected = _result("baostock", [])
    from stock_data_ingestion.adapters.baostock_adapter import BaoStockAdapter

    adapter = BaoStockAdapter()
    monkeypatch.setattr(adapter, "fetch_security_master", lambda req: expected)
    runner = ingestion_runner.IngestionRunner(
        config, RawObjectStore(config.storage.raw_object_root), adapters={"baostock": adapter},
    )
    monkeypatch.setattr(ingestion_runner, "fetch_security_master_in_worker", lambda *a, **kw: pytest.fail("injected adapter must stay injected"))
    assert runner._fetch(adapter, request()) is expected
    assert not (fake_sdk / "pid").exists()


def test_runner_propagates_unconfirmed_worker_cleanup(fake_sdk, monkeypatch, tmp_path):
    config = _config(tmp_path)
    runner = ingestion_runner.IngestionRunner(config, RawObjectStore(config.storage.raw_object_root))

    def failed_cleanup(*args, **kwargs):
        raise worker.WorkerCleanupError("termination not confirmed")

    monkeypatch.setattr(ingestion_runner, "fetch_security_master_in_worker", failed_cleanup)
    with pytest.raises(worker.WorkerCleanupError, match="termination not confirmed"):
        runner._fetch(runner.adapters["baostock"], request())


def test_timeout_keeps_prior_provider_raw_and_formal_identity(fake_sdk, monkeypatch, tmp_path):
    monkeypatch.setenv("BAOSTOCK_TEST_SCENARIO", "basic")
    config = _config(tmp_path)
    config.data_sources.providers["baostock"].timeout_seconds = 1
    store = RawObjectStore(config.storage.raw_object_root)
    database = Database(config.storage.sqlite_path)
    database.init()
    runner = ingestion_runner.IngestionRunner(config, store, database=database)
    row = {
        "normalized_ticker": "600000.SH", "ts_code": "600000.SH", "name": "证券样例",
        "fullname": "证券样例股份有限公司", "list_status": "L", "industry": "行业样例",
    }
    for name in ("tushare", "akshare"):
        monkeypatch.setattr(runner.adapters[name], "fetch_security_master", lambda req, name=name: _result(name, [dict(row)]))
    response = runner.run(request(provider_priority=["tushare", "akshare", "baostock"]))
    assert response.status == "partial_success"
    assert len(response.data.securities) == 1
    identity = response.data.securities[0]
    assert identity["normalized_ticker"] == "600000.SH"
    assert identity["name"] == "证券样例"
    assert identity["field_provenance"]["name"]["provider"] == "tushare"
    assert [r.provider for r in response.provider_results] == ["tushare", "akshare", "baostock"]
    assert response.provider_results[-1].error.error_code == ErrorCode.PROVIDER_TIMEOUT
    assert any(e.error_code == ErrorCode.PROVIDER_TIMEOUT for e in response.errors)
    for result in response.provider_results[:2]:
        assert result.raw_payload_ref
        metadata, rows = store.load_raw_payload(result.raw_payload_ref)
        assert rows[0]["raw_data"]["name"] == "证券样例"
        assert metadata["provider"] == result.provider
        assert store.verify_raw_hash(result.raw_payload_ref, result.raw_hash)
    with sqlite3.connect(config.storage.sqlite_path) as connection:
        stored = connection.execute("SELECT normalized_ticker,name,field_provenance FROM securities").fetchall()
        assert len(stored) == 1
        assert stored[0][:2] == ("600000.SH", "证券样例")
        import json

        assert json.loads(stored[0][2])["name"]["provider"] == "tushare"
        fetches = dict(connection.execute("SELECT provider,status FROM source_fetch_logs").fetchall())
        assert fetches == {"tushare": "success", "akshare": "success", "baostock": "failed"}
        runs = connection.execute("SELECT status,completed_at FROM ingestion_runs").fetchall()
        assert len(runs) == 1 and runs[0][0] == "partial_success" and runs[0][1]
        assert connection.execute("SELECT count(*) FROM raw_payload_index").fetchone()[0] == 2
    database.engine.dispose()
