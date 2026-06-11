from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo
from typing import Any


def parse_dt(value: str | None, tz: str = "Asia/Shanghai") -> datetime:
    if not value:
        return datetime.now(ZoneInfo(tz))
    text = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(ZoneInfo(tz))


def is_weekday_trading_day(dt: datetime) -> bool:
    # First edition fallback: weekdays only. Production can replace with stock_data trade-calendar.
    return dt.weekday() < 5


def market_phase(dt: datetime, config: dict[str, Any]) -> str:
    if not is_weekday_trading_day(dt):
        return "non_trading_day"
    t = dt.time()
    windows = config.get("schedule", {}).get("market_windows", {})
    pre = _parse_range(windows.get("pre_market", ["08:00", "09:25"]))
    am = _parse_range(windows.get("morning", ["09:30", "11:30"]))
    lunch = _parse_range(windows.get("lunch_break", ["11:30", "13:00"]))
    pm = _parse_range(windows.get("afternoon", ["13:00", "15:00"]))
    post = _parse_range(windows.get("post_market", ["15:00", "20:30"]))
    if _in_range(t, pre):
        return "pre_market"
    if _in_range(t, am) or _in_range(t, pm):
        return "intraday"
    if _in_range(t, lunch):
        return "lunch_break"
    if _in_range(t, post):
        return "post_market"
    return "off_hours"


def floor_bucket(dt: datetime, minutes: int) -> datetime:
    """Floor dt to a bucket boundary. Supports buckets larger than one hour (e.g. 120m)."""
    total = dt.hour * 60 + dt.minute
    floored = (total // minutes) * minutes
    return dt.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def _parse_range(value: list[str] | tuple[str, str]) -> tuple[time, time]:
    return (_parse_time(value[0]), _parse_time(value[1]))


def _parse_time(value: str) -> time:
    h, m = value.split(":")[:2]
    return time(int(h), int(m))


def _in_range(t: time, rng: tuple[time, time]) -> bool:
    start, end = rng
    return start <= t < end
