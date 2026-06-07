from __future__ import annotations

from pathlib import Path
from typing import Any

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.cli import build_parser
from stock_data_ingestion.config import DataQualityConfig, DataSourcesConfig, StorageConfig, load_config, parse_provider_list
from stock_data_ingestion.schemas.records import ProviderFetchResult
from stock_data_ingestion.schemas.requests import RequestType, StockDataRequest
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.storage.raw_object_store import RawObjectStore


class DummyAdapter(BaseDataAdapter):
    def __init__(self, name: str) -> None:
        self.provider_name = name
        self.source_site = name
        super().__init__()

    def is_available(self) -> bool:
        return True

    def authenticate(self) -> bool:
        return True

    def fetch_security_master(self, request: StockDataRequest) -> ProviderFetchResult:
        return self._success_result("dummy_security_master", [], request.created_at)

    def normalize_raw_data(self, result: ProviderFetchResult, request: StockDataRequest) -> list[Any]:
        return []

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return symbol

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        return ticker


def _write_min_config(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "data_sources.yaml").write_text(
        """
canonical_provider: tushare
provider_priority: [tushare, akshare, joinquant]
validator_providers: [akshare, joinquant]
supplement_providers: [akshare, joinquant]
providers:
  tushare: {enabled: true, role: canonical}
  akshare: {enabled: true, role: validator_supplement}
  joinquant: {enabled: true, role: validator_supplement}
""",
        encoding="utf-8",
    )
    (root / "storage.yaml").write_text("sqlite_path: data/test.db\n", encoding="utf-8")
    (root / "data_quality.yaml").write_text("{}\n", encoding="utf-8")


def test_parse_provider_list_aliases_and_order() -> None:
    assert parse_provider_list("THUShare, ak, jqdata, ak") == ["tushare", "akshare", "joinquant"]


def test_env_can_disable_joinquant(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _write_min_config(config_dir)
    monkeypatch.setenv("STOCK_DATA_PROVIDERS", "tushare,akshare")
    monkeypatch.setenv("STOCK_DATA_CANONICAL_PROVIDER", "tushare")
    load_config.cache_clear()

    cfg = load_config(config_dir)

    assert cfg.data_sources.effective_provider_priority() == ["tushare", "akshare"]
    assert cfg.data_sources.effective_canonical_provider() == "tushare"
    assert not cfg.data_sources.provider_is_enabled("joinquant")


def test_single_provider_env_becomes_canonical(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    _write_min_config(config_dir)
    monkeypatch.setenv("STOCK_DATA_PROVIDERS", "akshare")
    load_config.cache_clear()

    cfg = load_config(config_dir)

    assert cfg.data_sources.effective_provider_priority() == ["akshare"]
    assert cfg.data_sources.effective_canonical_provider() == "akshare"


def test_runner_enforces_config_provider_allow_list(tmp_path: Path) -> None:
    data_sources = DataSourcesConfig(active_providers=["akshare"], canonical_provider="tushare")
    storage = StorageConfig(raw_object_root=tmp_path / "raw", parquet_root=tmp_path / "parquet")
    runner = IngestionRunner(
        config=type("Cfg", (), {"data_sources": data_sources, "storage": storage, "data_quality": DataQualityConfig()})(),
        raw_store=RawObjectStore(tmp_path / "raw"),
        adapters={"tushare": DummyAdapter("tushare"), "akshare": DummyAdapter("akshare")},
    )
    req = StockDataRequest(request_id="req_test", request_type=RequestType.security_master)

    effective = runner._request_with_configured_providers(req)

    assert effective.provider_priority == ["akshare"]
    assert effective.canonical_provider == "akshare"
    assert effective.cross_validate is False


def test_cli_fetch_subcommands_accept_provider_override() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "fetch",
            "historical-bars",
            "--providers",
            "tushare",
            "akshare",
            "--canonical-provider",
            "tushare",
            "--tickers",
            "600519.SH",
            "--start-date",
            "20250101",
            "--end-date",
            "20250630",
        ]
    )
    assert args.providers == ["tushare", "akshare"]
    assert args.canonical_provider == "tushare"
