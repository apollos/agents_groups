"""Simulated tool CLI entry used as the Agent's `python_executable` target (scenario 07).

Invoked by fake_python.sh as:  sim_cli.py -m stock_data_ingestion.cli [--debug] fetch market-context ...
Runs the *real* `stock_data_ingestion.cli.main()` with: FakeAK vendor stub, temp SQLite under
$SIM_WORK_DIR, and the newest HSTECH daily bar forced to validation_status=quarantined.
Stdout stays the business JSON (also teed to $SIM_WORK_DIR/responses/); logs go to stderr.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

TOOL_DIR = Path("/home/yu/Workspace/agents_groups/tools/stock_data_collector")
os.chdir(TOOL_DIR)
sys.path.insert(0, str(TOOL_DIR / "tests"))

import test_market_context as T  # noqa: E402

from stock_data_ingestion import cli  # noqa: E402
from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter  # noqa: E402
from stock_data_ingestion.config import load_config  # noqa: E402
from stock_data_ingestion.logging_config import set_default_log_fields  # noqa: E402
from stock_data_ingestion.services.collector import StockDataCollector  # noqa: E402
from stock_data_ingestion.services.ingestion_runner import IngestionRunner  # noqa: E402
from stock_data_ingestion.storage.database import Database  # noqa: E402
from stock_data_ingestion.storage.raw_object_store import RawObjectStore  # noqa: E402

work = Path(os.environ["SIM_WORK_DIR"])
fake = T.FakeAK()
AKShareAdapter._import_ak = lambda self, source_api, started: fake
AKShareAdapter.is_available = lambda self: True

QUARANTINED_DATE = max(fake.hk_days)
_original_rescore = IngestionRunner._rescore_record


def _rescore(self, record, conflicts):
    record = _original_rescore(self, record, conflicts)
    if getattr(record, "record_type", "") == "index_bar" and getattr(record, "index_code", "") == "HSTECH" and record.trade_date == QUARANTINED_DATE:
        return record.model_copy(update={"validation_status": "quarantined"})
    return record


IngestionRunner._rescore_record = _rescore

load_config.cache_clear()
config = load_config().model_copy(deep=True)
config.storage.raw_object_root = work / "raw"
config.storage.parquet_root = work / "parquet"
config.storage.sqlite_path = work / "db.sqlite"
config.storage.log_path = work / "tool.log"


def _build(config_dir=None):
    db = Database(config.storage.sqlite_path)
    db.init()
    return StockDataCollector(IngestionRunner(config, RawObjectStore(config.storage.raw_object_root), database=db))


cli.load_config = lambda config_dir=None: config
cli._build_collector = _build
set_default_log_fields(data_mode="simulated")

argv = sys.argv[1:]
if argv[:2] == ["-m", "stock_data_ingestion.cli"]:
    argv = argv[2:]
buffer = io.StringIO()
with redirect_stdout(buffer):
    cli.main(argv)
out = buffer.getvalue()
responses = work / "responses"
responses.mkdir(exist_ok=True)
(responses / f"response_{len(list(responses.glob('response_*.json'))) + 1}.json").write_text(out, encoding="utf-8")
sys.stdout.write(out)
sys.stdout.flush()
