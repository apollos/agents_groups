"""Tushare account-tier quota guard: min-interval pacing, minute budget, daily cap.

All state is cross-process (flocked files under STOCK_DATA_PACER_DIR) because the
token quota is shared by every process on the machine — agent CLI subprocesses,
manual runs, capability checks.
"""

from __future__ import annotations

import time

import pytest

import stock_data_ingestion.adapters.tushare_rate_limit as rl
from stock_data_ingestion.adapters.tushare_adapter import TushareAdapter
from stock_data_ingestion.adapters.tushare_rate_limit import (
    DailyQuotaExceeded,
    RateLimitedProApi,
    TushareRateLimiter,
    pace_min_interval,
)


# ---------------------------------------------------------------------------
# per-API minimum interval (stk_mins)
# ---------------------------------------------------------------------------


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
    assert slept == []
    assert not (tmp_path / "pacer_stk_mins").exists()


# ---------------------------------------------------------------------------
# minute window budget (requests_per_minute - safety_margin)
# ---------------------------------------------------------------------------


def test_minute_budget_enforced_at_cap_minus_margin(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PACER_DIR", str(tmp_path))
    slept: list[float] = []

    def fake_sleep(s):
        slept.append(s)
        # Pretend the wait moved us into the next minute window.
        monkeypatch.setattr(time, "time", lambda: base + 60.0)

    base = 1_700_000_000.0  # aligned enough: any fixed instant works
    monkeypatch.setattr(time, "time", lambda: base)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    limiter = TushareRateLimiter(requests_per_minute=3, safety_margin=1)
    limiter.acquire("daily")
    limiter.acquire("daily")
    assert slept == []  # budget is 3-1=2: first two calls pass immediately

    limiter.acquire("daily")  # third call must wait for the next window
    assert len(slept) == 1
    assert 0 < slept[0] <= 60


def test_minute_budget_disabled_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PACER_DIR", str(tmp_path))
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    limiter = TushareRateLimiter()  # no limits configured
    for _ in range(10):
        limiter.acquire("daily")
    assert slept == []


# ---------------------------------------------------------------------------
# per-day per-API cap (requests_per_day_per_api - safety_margin)
# ---------------------------------------------------------------------------


def test_daily_cap_refuses_call_that_would_exceed(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PACER_DIR", str(tmp_path))
    limiter = TushareRateLimiter(requests_per_day_per_api=3, safety_margin=1)
    limiter.acquire("daily")
    limiter.acquire("daily")  # budget 3-1=2 reached
    with pytest.raises(DailyQuotaExceeded, match="daily quota guard"):
        limiter.acquire("daily")
    # Other APIs keep their own counter.
    limiter.acquire("trade_cal")


def test_daily_counter_resets_on_new_day(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_PACER_DIR", str(tmp_path))
    (tmp_path / "tushare_day_daily").write_text("19990101 99999")
    limiter = TushareRateLimiter(requests_per_day_per_api=3, safety_margin=1)
    limiter.acquire("daily")  # stale day ignored, counter restarted
    assert (tmp_path / "tushare_day_daily").read_text().split()[1] == "1"


# ---------------------------------------------------------------------------
# wiring: config -> adapter -> proxied client
# ---------------------------------------------------------------------------


def test_adapter_builds_limiter_from_rate_limit_config():
    adapter = TushareAdapter(
        rate_limit={
            "requests_per_minute": 200,
            "requests_per_day_per_api": 100000,
            "safety_margin": 1,
            "min_interval_seconds": {"stk_mins": 90},
        }
    )
    assert adapter._limiter.minute_budget == 199
    assert adapter._limiter.day_budget == 99999
    assert adapter._limiter.min_intervals == {"stk_mins": 90.0}
    # no config -> everything disabled
    unlimited = TushareAdapter()._limiter
    assert unlimited.minute_budget == 0 and unlimited.day_budget == 0 and unlimited.min_intervals == {}


def test_rate_limited_pro_api_acquires_before_each_call():
    calls: list[str] = []

    class _Limiter:
        def acquire(self, api):
            calls.append(f"acquire:{api}")

    class _Pro:
        def daily(self, **kwargs):
            calls.append("call:daily")
            return "df"

        some_attr = 42

    pro = RateLimitedProApi(_Pro(), _Limiter())
    assert pro.daily(ts_code="600519.SH") == "df"
    assert calls == ["acquire:daily", "call:daily"]
    assert pro.some_attr == 42  # non-callable attributes pass through unwrapped
