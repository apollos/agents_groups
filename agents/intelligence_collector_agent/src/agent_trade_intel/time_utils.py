from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
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


def is_trading_day(dt: datetime, config: dict[str, Any] | None = None) -> bool:
    """Trading-day check driven by market_calendar config.

    market_calendar.holidays lists non-trading weekdays (e.g. statutory holidays) as YYYY-MM-DD.
    market_calendar.extra_trading_days lists weekend make-up trading days as YYYY-MM-DD.
    """
    calendar = (config or {}).get("market_calendar", {}) or {}
    date_str = dt.date().isoformat()
    if date_str in set(calendar.get("extra_trading_days") or []):
        return True
    if date_str in set(calendar.get("holidays") or []):
        return False
    return is_weekday_trading_day(dt)


def validate_market_calendar(config: dict[str, Any] | None, year: int) -> dict[str, Any]:
    """Sanity-check the configured market_calendar for a given year.

    Without holiday entries the system falls back to "weekday == trading day", which mis-fires
    on statutory holidays and weekend make-up sessions. This surfaces that gap before a real run.
    """
    calendar = (config or {}).get("market_calendar", {}) or {}
    holidays = [str(d) for d in (calendar.get("holidays") or [])]
    extra_days = [str(d) for d in (calendar.get("extra_trading_days") or [])]

    def _invalid(dates: list[str]) -> list[str]:
        bad = []
        for d in dates:
            try:
                datetime.fromisoformat(d)
            except ValueError:
                bad.append(d)
        return bad

    invalid_entries = _invalid(holidays) + _invalid(extra_days)
    prefix = f"{year}-"
    holidays_in_year = [d for d in holidays if d.startswith(prefix)]
    extra_in_year = [d for d in extra_days if d.startswith(prefix)]
    warnings: list[str] = []
    if invalid_entries:
        warnings.append(f"invalid calendar dates (expect YYYY-MM-DD): {invalid_entries}")
    if not holidays_in_year:
        warnings.append(
            f"market_calendar.holidays has no entries for {year}; the agent will treat every weekday "
            "as a trading day (Spring Festival / National Day / make-up days will be wrong)"
        )
    return {
        "status": "warning" if warnings else "ok",
        "year": year,
        "holidays_in_year": len(holidays_in_year),
        "extra_trading_days_in_year": len(extra_in_year),
        "holidays_total": len(holidays),
        "extra_trading_days_total": len(extra_days),
        "warnings": warnings,
    }


def local_day_utc_range(day: str, tz: str = "Asia/Shanghai") -> tuple[str, str]:
    """UTC [start, end) bounds of one local calendar day, in SQLite datetime format.

    SQLite `datetime('now')` timestamps are naive UTC strings ("YYYY-MM-DD HH:MM:SS"), so date
    filters must convert the local trading day to a UTC range instead of comparing
    `date(created_at)` directly (which would shift 00:00-08:00 CST data onto the prior UTC day).
    """
    start_local = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(tz))
    end_local = start_local + timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (
        start_local.astimezone(timezone.utc).strftime(fmt),
        end_local.astimezone(timezone.utc).strftime(fmt),
    )


def market_phase(dt: datetime, config: dict[str, Any]) -> str:
    if not is_trading_day(dt, config):
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
