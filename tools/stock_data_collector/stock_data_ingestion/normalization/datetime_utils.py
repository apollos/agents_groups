from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_asia_shanghai() -> datetime:
    return datetime.now(tz=ASIA_SHANGHAI)


def normalize_trade_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.astimezone(ASIA_SHANGHAI).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if len(raw) == 8 and raw.isdigit():
        return datetime.strptime(raw, "%Y%m%d").date()
    return datetime.fromisoformat(raw).date()


def normalize_timestamp(value: str | date | datetime, tz: str = "Asia/Shanghai") -> datetime:
    zone = ZoneInfo(tz)
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    else:
        raw = str(value).strip()
        if len(raw) == 8 and raw.isdigit():
            dt = datetime.strptime(raw, "%Y%m%d")
        else:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone)
    return dt.astimezone(zone)


def to_asia_shanghai(value: datetime) -> datetime:
    return normalize_timestamp(value, "Asia/Shanghai")


def validate_date_range(start_date: str | date, end_date: str | date) -> tuple[date, date]:
    start = normalize_trade_date(start_date)
    end = normalize_trade_date(end_date)
    if start > end:
        raise ValueError("INVALID_DATE_RANGE: start_date must be <= end_date")
    return start, end


def infer_bar_start_end(
    trade_date: str | date,
    frequency: str,
    timestamp: datetime | None = None,
    tz: str = "Asia/Shanghai",
) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    day = normalize_trade_date(trade_date)
    if frequency in {"1d", "1w", "1mo"}:
        start = datetime.combine(day, time(9, 30), tzinfo=zone)
        end = datetime.combine(day, time(15, 0), tzinfo=zone)
        return start, end
    minute_map = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
    if frequency not in minute_map:
        raise ValueError(f"INVALID_REQUEST: unsupported frequency {frequency}")
    if timestamp is None:
        start = datetime.combine(day, time(9, 30), tzinfo=zone)
    else:
        start = normalize_timestamp(timestamp, tz)
    return start, start + timedelta(minutes=minute_map[frequency])


def build_quote_time_bucket(value: datetime, seconds: int = 3) -> datetime:
    ts = to_asia_shanghai(value)
    epoch = int(ts.timestamp())
    bucket = epoch - (epoch % seconds)
    return datetime.fromtimestamp(bucket, tz=ASIA_SHANGHAI)
