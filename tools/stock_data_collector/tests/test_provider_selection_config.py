from __future__ import annotations

from pathlib import Path

from stock_data_ingestion.config import (
    DataQualityConfig,
    DataSourcesConfig,
    StorageConfig,
    AppConfig,
    load_config,
    parse_provider_list,
)
from stock_data_ingestion.schemas.requests import RequestType, StockDataRequest
from stock_data_ingestion.services.ingestion_runner import IngestionRunner
from stock_data_ingestion.storage.raw_object_store import RawObjectStore


def test_parse_provider_list_accepts_aliases() -> None:
    assert parse_provider_list("THUShare, ak, baostock, jqdata") == ["tushare", "akshare", "baostock", "joinquant"]
    assert parse_provider_list("tencent, bao_stock, jq") == ["akshare", "baostock", "joinquant"]


def test_default_provider_config_includes_baostock_and_disables_jqdata_when_requested() -> None:
    data_sources = DataSourcesConfig.model_validate(
        {
            "canonical_provider": "tushare",
            "provider_priority": ["tushare", "akshare", "baostock", "joinquant"],
            "active_providers": ["tushare", "akshare", "baostock"],
            "providers": {"joinquant": {"enabled": False}},
        }
    )
    assert data_sources.providers_for_request("historical_bars") == ["tushare", "akshare", "baostock"]
    assert data_sources.supplement_providers == ["akshare", "baostock"]
    assert data_sources.validator_providers == ["akshare", "baostock"]
    assert data_sources.market_data_lookback_days == 400
    assert data_sources.financial_lookback_quarters == 8


def test_active_provider_config_can_select_single_provider() -> None:
    data_sources = DataSourcesConfig.model_validate(
        {
            "canonical_provider": "tushare",
            "provider_priority": ["tushare", "akshare", "joinquant"],
            "active_providers": ["akshare"],
        }
    )
    assert data_sources.active_providers == ["akshare"]
    assert data_sources.provider_priority == ["akshare"]
    assert data_sources.canonical_provider == "akshare"
    assert data_sources.providers_for_request("historical_bars") == ["akshare"]
    assert data_sources.canonical_for_request("historical_bars") == "akshare"


def test_request_type_override_uses_specific_provider_set() -> None:
    data_sources = DataSourcesConfig.model_validate(
        {
            "canonical_provider": "tushare",
            "active_providers": ["tushare", "akshare"],
            "request_provider_overrides": {
                "security_master": ["akshare"],
                "historical_bars": ["tushare", "akshare"],
            },
        }
    )
    assert data_sources.providers_for_request("security_master") == ["akshare"]
    assert data_sources.canonical_for_request("security_master") == "akshare"
    assert data_sources.providers_for_request("historical_bars") == ["tushare", "akshare"]


def test_env_provider_override_is_applied_by_load_config(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "data_sources.yaml").write_text(
        """
canonical_provider: tushare
provider_priority: [tushare, akshare, joinquant]
active_providers: [tushare, akshare, joinquant]
providers:
  tushare: {enabled: true}
  akshare: {enabled: true}
  joinquant: {enabled: true}
""",
        encoding="utf-8",
    )
    (config_dir / "storage.yaml").write_text("sqlite_path: data/test.db\n", encoding="utf-8")
    (config_dir / "data_quality.yaml").write_text("{}\n", encoding="utf-8")

    # Keep this test independent from the developer machine's project .env.
    # A local .env may contain STOCK_DATA_PROVIDERS=tushare,akshare,
    # which would otherwise be loaded automatically and mask this test's
    # explicit STOCK_DATA_ACTIVE_PROVIDERS override.
    for env_name in [
        "STOCK_DATA_PROVIDERS",
        "STOCK_DATA_ENABLED_PROVIDERS",
        "STOCK_DATA_ACTIVE_PROVIDERS",
        "STOCK_DATA_DISABLED_PROVIDERS",
        "STOCK_DATA_EXCLUDED_PROVIDERS",
        "STOCK_DATA_PROVIDER_PRIORITY",
        "STOCK_DATA_CANONICAL_PROVIDER",
        "STOCK_DATA_ENABLE_TUSHARE",
        "STOCK_DATA_ENABLE_AKSHARE",
        "STOCK_DATA_ENABLE_JOINQUANT",
        "STOCK_DATA_ENABLE_BAOSTOCK",
        "STOCK_DATA_PROVIDER_TUSHARE_ENABLED",
        "STOCK_DATA_PROVIDER_AKSHARE_ENABLED",
        "STOCK_DATA_PROVIDER_JOINQUANT_ENABLED",
        "STOCK_DATA_PROVIDER_BAOSTOCK_ENABLED",
    ]:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("STOCK_DATA_DISABLE_ENV_AUTOLOAD", "true")

    monkeypatch.setenv("STOCK_DATA_ACTIVE_PROVIDERS", "akshare")
    monkeypatch.setenv("STOCK_DATA_CANONICAL_PROVIDER", "akshare")
    load_config.cache_clear()
    config = load_config(config_dir)
    load_config.cache_clear()

    assert config.data_sources.active_providers == ["akshare"]
    assert config.data_sources.provider_priority == ["akshare"]
    assert config.data_sources.canonical_provider == "akshare"
    assert not config.data_sources.providers["tushare"].enabled
    assert config.data_sources.providers["akshare"].enabled
    assert not config.data_sources.providers["joinquant"].enabled


def test_runner_injects_configured_provider_selection(tmp_path: Path) -> None:
    config = AppConfig(
        data_sources=DataSourcesConfig.model_validate(
            {
                "canonical_provider": "tushare",
                "active_providers": ["akshare"],
            }
        ),
        storage=StorageConfig(raw_object_root=tmp_path / "raw", parquet_root=tmp_path / "parquet"),
        data_quality=DataQualityConfig(),
    )
    runner = IngestionRunner(config, RawObjectStore(tmp_path / "raw"), adapters={"akshare": object()})
    request = StockDataRequest(
        request_id="req_provider_selection",
        request_type=RequestType.security_master,
        tickers=["600519.SH"],
    )

    resolved = runner._request_with_configured_providers(request)

    assert resolved.provider_priority == ["akshare"]
    assert resolved.canonical_provider == "akshare"
    assert resolved.extra_params["resolved_provider_priority"] == ["akshare"]
    assert resolved.extra_params["resolved_canonical_provider"] == "akshare"
