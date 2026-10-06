"""Shared helpers for the simulated verification scenarios (FakeAK + pinned clock + temp DB)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOL_DIR = Path("/home/yu/Workspace/agents_groups/tools/stock_data_collector")
AGENT_DIR = Path("/home/yu/Workspace/agents_groups/agents/intelligence_collector_agent")
EVIDENCE_ROOT = AGENT_DIR / "docs/acceptance/evidence/market_context_review_20261006"
sys.path.insert(0, str(TOOL_DIR / "tests"))

import test_market_context as T  # noqa: E402  (FakeAK, _make_runner, _restart, _rows, AS_OF, LAST_TRADING)
from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter  # noqa: E402
from stock_data_ingestion.logging_config import set_default_log_fields, setup_logging  # noqa: E402
from stock_data_ingestion.services import market_context_requests as mcr  # noqa: E402
from stock_data_ingestion.services.market_context_service import MarketContextService  # noqa: E402

SH = timezone(timedelta(hours=8))
import os  # noqa: E402

os.chdir(TOOL_DIR)  # load_config() resolves config/ relative to the tool directory (same as pytest)


def git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=TOOL_DIR, text=True).strip()


def dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 6, 9, 0, 0, tzinfo=SH)
        mcr.now_asia_shanghai = lambda: self.now

    def set(self, hour: int, minute: int = 0) -> None:
        self.now = self.now.replace(hour=hour, minute=minute)


class Scenario:
    """One scenario = one evidence directory, one temp DB, one debug.jsonl."""

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.dir = EVIDENCE_ROOT / name
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.tmp = Path("/tmp/mctx_verify/work") / name
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True)
        self.fake = T.FakeAK()
        AKShareAdapter._import_ak = lambda self_, source_api, started, fake=self.fake: fake
        AKShareAdapter.is_available = lambda self_: True
        self.clock = Clock()
        self.runner = T._make_runner(self.tmp)
        self.service = MarketContextService(self.runner)
        setup_logging(self.dir / "debug.jsonl", debug=True)
        set_default_log_fields(data_mode="simulated")
        self.calls: list[dict] = []
        self.inputs = {
            "scenario": name,
            "description": description,
            "data_mode": "simulated",
            "code_commit": git_commit(),
            "driver": "sim_driver.py (copied alongside)",
            "fixtures": "tests/test_market_context.py: FakeAK (vendor stub), clock pinned via market_context_requests.now_asia_shanghai, temp SQLite",
            "temp_sqlite": str(self.tmp / "db.sqlite"),
            "calls": self.calls,
        }

    def restart(self) -> None:
        self.runner = T._restart(self.runner)
        self.service = MarketContextService(self.runner)

    def fetch(self, label: str, request, *, note: str = "", **knobs) -> object:
        n = len(self.calls) + 1
        request = request.model_copy(update={"trace_id": f"sim-{self.name}-call{n}"})
        resp = self.service.fetch(request)
        payload = resp.model_dump(mode="json")
        dump(self.dir / f"response_{n}.json", payload)
        self.calls.append(
            {
                "n": n,
                "label": label,
                "note": note,
                "request": request.model_dump(mode="json", exclude_none=True),
                "clock_now": self.clock.now.isoformat(),
                "fake_ak": {"fx_shift": self.fake.fx_shift, "spot_mode": self.fake.spot_mode, "fail": sorted(self.fake.fail), **knobs},
                "vendor_calls_so_far": len(self.fake.calls),
                "response_file": f"response_{n}.json",
                "status": resp.status,
                "value": resp.result.value,
                "data_date": str(resp.result.data_date) if resp.result.data_date else None,
                "quality_status": resp.result.quality.status,
                "stock_data_request_id": resp.result.provenance.stock_data_request_id,
                "idempotency_key": resp.result.provenance.idempotency_key,
                "record_ids": list(resp.result.provenance.record_ids),
                "warnings": list(resp.warnings),
                "error_codes": [e.error_code for e in resp.errors],
            }
        )
        return resp

    def rows(self, sql: str) -> list[dict]:
        return [{k: (json.loads(v) if isinstance(v, str) and k in {"record_json", "business_key", "date_resolution_details", "field_provenance"} else v) for k, v in r.items()} for r in T._rows(self.runner, sql)]

    def finish(self, db_export: dict, checks: dict) -> None:
        self.inputs["checks"] = checks
        dump(self.dir / "inputs.json", self.inputs)
        dump(self.dir / "db_export.json", db_export)
        failed = {k: v for k, v in checks.items() if v is not True}
        print(f"[{self.name}] checks: {len(checks) - len(failed)}/{len(checks)} passed" + (f"  FAILED: {failed}" if failed else ""))
        if failed:
            raise SystemExit(1)
