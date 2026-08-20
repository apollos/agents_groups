"""Per-API minimum-interval pacing for rate-limited Tushare endpoints (stk_mins)."""

from __future__ import annotations

import time

from stock_data_ingestion.adapters.tushare_adapter import TushareAdapter, pace_min_interval


def test_pace_min_interval_spaces_consecutive_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PACER_DIR", str(tmp_path))
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    pace_min_interval("stk_mins", 90)
    assert slept == []  # first call: no prior timestamp, no wait

    pace_min_interval("stk_mins", 90)
    assert len(slept) == 1
    assert 80 < slept[0] <= 90  # immediate second call waits nearly the full interval

    assert (tmp_path / "pacer_stk_mins").exists()  # cross-process state persisted


def test_pace_min_interval_skips_after_interval_elapsed(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PACER_DIR", str(tmp_path))
    (tmp_path / "pacer_stk_mins").write_text(str(time.time() - 120))
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    pace_min_interval("stk_mins", 90)
    assert slept == []


def test_pace_min_interval_disabled_when_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PACER_DIR", str(tmp_path))
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    pace_min_interval("stk_mins", 0)
    pace_min_interval("stk_mins", 0)
    assert slept == []
    assert not (tmp_path / "pacer_stk_mins").exists()


def test_adapter_reads_min_interval_from_rate_limit_config():
    adapter = TushareAdapter(rate_limit={"requests_per_minute": 180, "min_interval_seconds": {"stk_mins": 90}})
    assert adapter._min_intervals == {"stk_mins": 90.0}
    # no config -> pacing disabled
    assert TushareAdapter()._min_intervals == {}
    assert TushareAdapter(rate_limit={"requests_per_minute": 180})._min_intervals == {}


def test_adapter_paces_only_configured_apis(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PACER_DIR", str(tmp_path))
    paced: list[tuple[str, float]] = []
    monkeypatch.setattr(
        "stock_data_ingestion.adapters.tushare_adapter.pace_min_interval",
        lambda api, seconds: paced.append((api, seconds)),
    )
    adapter = TushareAdapter(rate_limit={"min_interval_seconds": {"stk_mins": 90}})
    adapter._pace("stk_mins")
    adapter._pace("daily")  # not configured -> no pacing
    assert paced == [("stk_mins", 90.0)]
