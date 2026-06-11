from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .db import SQLiteStore, dumps_json
from .ids import make_idempotency_key, new_id


class MarketFeatureBuilder:
    """Build 10-minute market feature records from stock data responses.

    The first version works with whatever the stock_data_collector returns. If the response does not
    include raw bars inline, it still persists a lightweight feature shell so downstream agents can
    see that the bucket was collected and inspect source_refs/quality.
    """

    def __init__(self, store: SQLiteStore, config: dict[str, Any]):
        self.store = store
        self.config = config

    def build_and_save(
        self,
        *,
        task: dict[str, Any],
        stock_result: dict[str, Any],
        quality: dict[str, Any],
        source_frequency: str | None = None,
    ) -> dict[str, Any]:
        target = task.get("target") or {}
        ticker = target.get("ticker")
        target_id = target.get("target_id")
        bucket_start = task.get("bucket_start") or task.get("as_of")
        bucket_size = task.get("bucket_size") or f"{self.config.get('cadence', {}).get('intraday_bucket_minutes', 10)}m"
        bucket_end = task.get("bucket_end") or _bucket_end(bucket_start, bucket_size)
        bars = _filter_bars_to_bucket(_extract_bars(stock_result), bucket_start, bucket_end)
        feature = _calculate_features(bars)
        data_quality = quality.get("data_quality") or quality.get("quality", {}).get("data_quality") or 0.0
        abnormality_score = feature.get("abnormality_score", 0.0)
        summary_cn = _summary(ticker, bucket_size, feature, bool(bars))
        feature_id = new_id("feature")
        idem = make_idempotency_key("market_feature", ticker, bucket_size, bucket_start, "v1")
        degraded_from = _degraded_from(bucket_size, source_frequency)
        payload = {
            "ticker": ticker,
            "target_id": target_id,
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "feature_window": bucket_size,
            "source_frequency": source_frequency,
            "degraded_from": degraded_from,
            "features": feature,
            "source_quality": quality,
            "has_inline_bars": bool(bars),
        }
        with self.store.session() as con:
            row = con.execute("SELECT feature_id FROM market_features WHERE idempotency_key=?", (idem,)).fetchone()
            if row:
                feature_id = str(row["feature_id"])
            else:
                con.execute(
                    """
                    INSERT INTO market_features(
                      feature_id, ticker, target_id, feature_window, bucket_start, bucket_end,
                      timestamp, abnormality_score, data_quality, summary_cn, feature_json,
                      source_refs_json, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feature_id,
                        ticker,
                        target_id,
                        bucket_size,
                        bucket_start,
                        bucket_end,
                        bucket_end,
                        abnormality_score,
                        data_quality,
                        summary_cn,
                        dumps_json(payload),
                        dumps_json(_source_refs(stock_result)),
                        idem,
                    ),
                )
        return {
            "feature_id": feature_id,
            "ticker": ticker,
            "feature_window": bucket_size,
            "bucket_start": bucket_start,
            "abnormality_score": abnormality_score,
            "data_quality": data_quality,
            "summary_cn": summary_cn,
            "payload": payload,
        }


def _extract_bars(stock_result: dict[str, Any]) -> list[dict[str, Any]]:
    stdout = stock_result.get("stdout") if isinstance(stock_result, dict) else None
    if isinstance(stdout, list):
        return [x for x in stdout if isinstance(x, dict)]
    if not isinstance(stdout, dict):
        return []
    data = stdout.get("data") or {}
    bars = data.get("bars") or []
    return bars if isinstance(bars, list) else []


_BAR_TIME_KEYS = ("datetime", "timestamp", "trade_time", "time", "bar_time")


def _filter_bars_to_bucket(bars: list[dict[str, Any]], bucket_start: str | None, bucket_end: str | None) -> list[dict[str, Any]]:
    """Keep only bars inside [bucket_start, bucket_end).

    This is what aggregates underlying 5m/15m bars into the configured bucket window. If bar
    timestamps are missing or unparsable, the full list is kept (whole-day fetch fallback).
    """
    if not bars or not bucket_start or not bucket_end:
        return bars
    try:
        start = datetime.fromisoformat(bucket_start)
        end = datetime.fromisoformat(bucket_end)
    except ValueError:
        return bars
    filtered: list[dict[str, Any]] = []
    for bar in bars:
        ts = _bar_time(bar)
        if ts is None:
            return bars
        if ts.tzinfo is None and start.tzinfo is not None:
            ts = ts.replace(tzinfo=start.tzinfo)
        if start <= ts < end:
            filtered.append(bar)
    return filtered if filtered else bars


def _bar_time(bar: dict[str, Any]) -> datetime | None:
    for key in _BAR_TIME_KEYS:
        value = bar.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _degraded_from(bucket_size: str, source_frequency: str | None) -> str | None:
    if not source_frequency or source_frequency == bucket_size:
        return None
    try:
        bucket_minutes = int(bucket_size.rstrip("m"))
        source_minutes = int(source_frequency.rstrip("m"))
    except ValueError:
        return None
    if source_minutes > bucket_minutes:
        return f"{bucket_size}_to_{source_frequency}"
    return None


def _calculate_features(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {
            "price_features": {},
            "volume_features": {},
            "trend_features": {},
            "abnormality_score": 0.0,
            "calculation_status": "no_inline_bars",
        }
    try:
        first = bars[0]
        last = bars[-1]
        open_price = float(first.get("open") or first.get("close") or 0)
        close_price = float(last.get("close") or 0)
        amount = sum(float(b.get("amount") or 0) for b in bars)
        volume = sum(float(b.get("volume") or 0) for b in bars)
        ret = (close_price / open_price - 1.0) if open_price else 0.0
        vwap = amount / volume if volume else None
        score = min(1.0, abs(ret) * 20 + (0.1 if amount > 0 else 0.0))
        return {
            "price_features": {"return_window": ret, "open": open_price, "close": close_price},
            "volume_features": {"amount": amount, "volume": volume},
            "trend_features": {"vwap": vwap, "above_vwap": bool(vwap and close_price > vwap)},
            "abnormality_score": round(score, 4),
            "calculation_status": "ok",
        }
    except Exception as exc:
        return {"calculation_status": "failed", "error": str(exc), "abnormality_score": 0.0}


def _summary(ticker: str | None, bucket_size: str, feature: dict[str, Any], has_bars: bool) -> str:
    if not has_bars:
        return f"{ticker or '目标'} 已完成 {bucket_size} 时间桶采集，但工具响应未内联行情明细；请通过 stock_data 查询源数据。"
    ret = feature.get("price_features", {}).get("return_window")
    amount = feature.get("volume_features", {}).get("amount")
    return f"{ticker or '目标'} {bucket_size} 特征已生成，区间收益约 {ret:.2%}，成交额 {amount:.0f}。"


def _source_refs(stock_result: dict[str, Any]) -> list[str]:
    stdout = stock_result.get("stdout") if isinstance(stock_result, dict) else None
    if isinstance(stdout, dict):
        req_id = stdout.get("request_id")
        refs = ((stdout.get("persistence") or {}).get("raw_payload_refs") or [])
        out = []
        if req_id:
            out.append(f"stock_data://request/{req_id}")
        out.extend(refs)
        return out
    return []


def _bucket_end(bucket_start: str | None, bucket_size: str) -> str | None:
    if not bucket_start:
        return None
    try:
        mins = int(bucket_size.rstrip("m"))
        return (datetime.fromisoformat(bucket_start) + timedelta(minutes=mins)).isoformat()
    except Exception:
        return None
