from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .db import SQLiteStore, dumps_json
from .ids import make_idempotency_key, new_id


class MarketFeatureBuilder:
    """Build intraday market feature records from stock data responses.

    Works with whatever the stock_data_collector returns. If the response does not include raw
    bars inline, it still persists a lightweight feature shell so downstream agents can see that
    the bucket was collected and inspect source_refs/quality.

    Optional enrichment inputs:
    - daily_result: 1d bars query result used to derive prev_close (limit up/down features).
    - history_result: minute bars query over past N days used for the 20d same-bucket amount ratio.
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
        daily_result: dict[str, Any] | None = None,
        history_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = task.get("target") or {}
        ticker = target.get("ticker")
        target_id = target.get("target_id")
        bucket_start = task.get("bucket_start") or task.get("as_of")
        bucket_size = task.get("bucket_size") or f"{self.config.get('cadence', {}).get('intraday_bucket_minutes', 10)}m"
        bucket_end = task.get("bucket_end") or _bucket_end(bucket_start, bucket_size)
        day_bars = _extract_bars(stock_result)
        bars = _filter_bars_to_bucket(day_bars, bucket_start, bucket_end)
        prev_close = _extract_prev_close(_extract_bars(daily_result) if daily_result else [], bucket_start)
        history_bars = _extract_bars(history_result) if history_result else []
        feature = _calculate_features(
            bars,
            day_bars=day_bars,
            prev_close=prev_close,
            history_bars=history_bars,
            bucket_start=bucket_start,
            bucket_end=bucket_end,
            ticker=ticker,
            target=target,
            config=self.config,
        )
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


def should_emit_feature_ticket(feature: dict[str, Any], config: dict[str, Any]) -> tuple[bool, bool, list[str]]:
    """Multi-condition emit decision per design §7.4.

    Returns (emit_market_feature_ticket, trigger_risk_review, reasons).
    """
    mf_cfg = config.get("market_features", {}) or {}
    thresholds = mf_cfg.get("thresholds", {}) or {}
    risk_cfg = mf_cfg.get("risk_review", {}) or {}
    features = (feature.get("payload") or {}).get("features", {}) or {}
    price = features.get("price_features", {}) or {}
    volume = features.get("volume_features", {}) or {}
    tradability = features.get("tradability_features", {}) or {}
    reasons: list[str] = []

    score_gte = float(thresholds.get("abnormality_score_gte", mf_cfg.get("abnormality_ticket_threshold", 0.75)))
    if float(feature.get("abnormality_score") or 0.0) >= score_gte:
        reasons.append(f"abnormality_score>={score_gte}")
    ret = price.get("return_window")
    ret_gte = thresholds.get("return_abs_gte")
    if ret is not None and ret_gte is not None and abs(float(ret)) >= float(ret_gte):
        reasons.append(f"abs(return)>={ret_gte}")
    ratio = volume.get("amount_ratio_vs_20d_same_bucket")
    ratio_gte = thresholds.get("amount_ratio_vs_20d_same_bucket_gte")
    if ratio is not None and ratio_gte is not None and float(ratio) >= float(ratio_gte):
        reasons.append(f"amount_ratio>={ratio_gte}")
    for key, cfg_key in (
        ("distance_to_limit_up", "distance_to_limit_up_lte"),
        ("distance_to_limit_down", "distance_to_limit_down_lte"),
    ):
        dist = tradability.get(key)
        lte = thresholds.get(cfg_key)
        if dist is not None and lte is not None and float(dist) <= float(lte):
            reasons.append(f"{key}<={lte}")
    if tradability.get("hit_limit_up") or tradability.get("hit_limit_down"):
        reasons.append("hit_limit")

    risk_review = False
    neg_ret_lte = risk_cfg.get("negative_return_lte")
    if ret is not None and neg_ret_lte is not None and float(ret) <= float(neg_ret_lte):
        risk_review = True
        reasons.append(f"return<={neg_ret_lte}(risk_review)")
    if bool(risk_cfg.get("hit_limit_down", True)) and tradability.get("hit_limit_down"):
        risk_review = True
        reasons.append("hit_limit_down(risk_review)")
    if bool(risk_cfg.get("trading_status_abnormal", True)) and tradability.get("is_suspended"):
        risk_review = True
        reasons.append("suspended(risk_review)")
    return bool(reasons), risk_review, reasons


def _extract_bars(stock_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(stock_result, dict):
        return []
    stdout = stock_result.get("stdout")
    if isinstance(stdout, list):
        return [x for x in stdout if isinstance(x, dict)]
    if not isinstance(stdout, dict):
        return []
    data = stdout.get("data") or {}
    bars = data.get("bars") or []
    return bars if isinstance(bars, list) else []


_BAR_TIME_KEYS = ("datetime", "timestamp", "trade_time", "time", "bar_time", "trade_date", "date")


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


def _extract_prev_close(daily_bars: list[dict[str, Any]], bucket_start: str | None) -> float | None:
    """Latest daily close strictly before the bucket date."""
    if not daily_bars or not bucket_start:
        return None
    try:
        bucket_date = datetime.fromisoformat(bucket_start).date()
    except ValueError:
        return None
    best: tuple[Any, float] | None = None
    for bar in daily_bars:
        ts = _bar_time(bar)
        close = bar.get("close")
        if ts is None or close is None:
            continue
        if ts.date() >= bucket_date:
            continue
        if best is None or ts.date() > best[0]:
            try:
                best = (ts.date(), float(close))
            except (TypeError, ValueError):
                continue
    return best[1] if best else None


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


def _limit_pct(ticker: str | None, is_st: bool, config: dict[str, Any]) -> float:
    rules = (config.get("market_features", {}) or {}).get("limit_rules", {}) or {}
    if is_st:
        return float(rules.get("st_pct", 0.05))
    code = (ticker or "").split(".")[0]
    for prefix, pct in sorted((rules.get("prefix_pct", {}) or {}).items(), key=lambda kv: -len(kv[0])):
        if code.startswith(str(prefix)):
            return float(pct)
    # A-share defaults: STAR (688) and ChiNext (300/301) are 20%, BSE (4xx/8xx) 30%, main board 10%.
    if code.startswith(("688", "300", "301")):
        return float(rules.get("growth_board_pct", 0.20))
    if code.startswith(("4", "8")) and len(code) == 6:
        return float(rules.get("bse_pct", 0.30))
    return float(rules.get("default_pct", 0.10))


def _same_bucket_amount_ratio(
    history_bars: list[dict[str, Any]],
    bucket_start: str | None,
    bucket_end: str | None,
    bucket_amount: float,
    *,
    min_days: int = 3,
) -> float | None:
    """bucket amount vs average amount of the same time-of-day window over past days."""
    if not history_bars or not bucket_start or not bucket_end or bucket_amount <= 0:
        return None
    try:
        start = datetime.fromisoformat(bucket_start)
        end = datetime.fromisoformat(bucket_end)
    except ValueError:
        return None
    start_t, end_t = start.time(), end.time()
    per_day: dict[Any, float] = {}
    for bar in history_bars:
        ts = _bar_time(bar)
        if ts is None:
            continue
        if ts.date() >= start.date():
            continue
        if not (start_t <= ts.time() < end_t):
            continue
        try:
            per_day[ts.date()] = per_day.get(ts.date(), 0.0) + float(bar.get("amount") or 0)
        except (TypeError, ValueError):
            continue
    amounts = [v for v in per_day.values() if v > 0]
    if len(amounts) < min_days:
        return None
    avg = sum(amounts) / len(amounts)
    return round(bucket_amount / avg, 4) if avg > 0 else None


def _calculate_features(
    bars: list[dict[str, Any]],
    *,
    day_bars: list[dict[str, Any]] | None = None,
    prev_close: float | None = None,
    history_bars: list[dict[str, Any]] | None = None,
    bucket_start: str | None = None,
    bucket_end: str | None = None,
    ticker: str | None = None,
    target: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or {}
    target = target or {}
    if not bars:
        return {
            "price_features": {},
            "volume_features": {},
            "trend_features": {},
            "tradability_features": {
                "is_suspended": bool(target.get("is_suspended", False)),
                "is_st": bool(target.get("is_st", False)),
            },
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

        price_features: dict[str, Any] = {"return_window": ret, "open": open_price, "close": close_price}
        if prev_close:
            price_features["prev_close"] = prev_close
            price_features["day_return"] = round(close_price / prev_close - 1.0, 6)
        day_bars = day_bars or bars
        highs = [float(b.get("high") or b.get("close") or 0) for b in day_bars]
        lows = [float(b.get("low") or b.get("close") or 0) for b in day_bars if (b.get("low") or b.get("close"))]
        day_high, day_low = (max(highs), min(lows)) if highs and lows else (None, None)
        if day_high is not None and day_low is not None and day_high > day_low:
            price_features["position_in_intraday_range"] = round((close_price - day_low) / (day_high - day_low), 4)

        volume_features: dict[str, Any] = {"amount": amount, "volume": volume}
        ratio = _same_bucket_amount_ratio(history_bars or [], bucket_start, bucket_end, amount)
        if ratio is not None:
            volume_features["amount_ratio_vs_20d_same_bucket"] = ratio

        is_st = bool(target.get("is_st", False))
        tradability: dict[str, Any] = {
            "is_suspended": bool(target.get("is_suspended", False)),
            "is_st": is_st,
        }
        if prev_close:
            pct = _limit_pct(ticker, is_st, config)
            limit_up = round(prev_close * (1 + pct), 2)
            limit_down = round(prev_close * (1 - pct), 2)
            tradability.update(
                {
                    "limit_pct": pct,
                    "limit_up_price": limit_up,
                    "limit_down_price": limit_down,
                    "distance_to_limit_up": round(max(0.0, (limit_up - close_price) / close_price), 6) if close_price else None,
                    "distance_to_limit_down": round(max(0.0, (close_price - limit_down) / close_price), 6) if close_price else None,
                    "hit_limit_up": close_price >= limit_up - 1e-6,
                    "hit_limit_down": close_price <= limit_down + 1e-6,
                }
            )

        score = _abnormality_score(ret=ret, amount=amount, ratio=ratio, tradability=tradability, config=config)
        return {
            "price_features": price_features,
            "volume_features": volume_features,
            "trend_features": {"vwap": vwap, "above_vwap": bool(vwap and close_price > vwap)},
            "tradability_features": tradability,
            "abnormality_score": score,
            "calculation_status": "ok",
        }
    except Exception as exc:
        return {"calculation_status": "failed", "error": str(exc), "abnormality_score": 0.0}


def _abnormality_score(
    *,
    ret: float,
    amount: float,
    ratio: float | None,
    tradability: dict[str, Any],
    config: dict[str, Any],
) -> float:
    """Score in [0, 1]: max of normalized return / volume-ratio / limit-proximity components."""
    thresholds = (config.get("market_features", {}) or {}).get("thresholds", {}) or {}
    ret_ref = float(thresholds.get("return_abs_gte", 0.02))
    ratio_ref = float(thresholds.get("amount_ratio_vs_20d_same_bucket_gte", 3.0))
    dist_ref = float(thresholds.get("distance_to_limit_up_lte", 0.015))
    components = [min(1.0, abs(ret) / ret_ref * 0.75 + (0.05 if amount > 0 else 0.0))]
    if ratio is not None and ratio_ref > 0:
        components.append(min(1.0, ratio / ratio_ref * 0.75))
    for key in ("distance_to_limit_up", "distance_to_limit_down"):
        dist = tradability.get(key)
        if dist is not None and dist_ref > 0:
            components.append(min(1.0, max(0.0, 1.0 - float(dist) / dist_ref)))
    if tradability.get("hit_limit_up") or tradability.get("hit_limit_down"):
        components.append(1.0)
    return round(max(components), 4)


def _summary(ticker: str | None, bucket_size: str, feature: dict[str, Any], has_bars: bool) -> str:
    if not has_bars:
        return f"{ticker or '目标'} 已完成 {bucket_size} 时间桶采集，但工具响应未内联行情明细；请通过 stock_data 查询源数据。"
    ret = feature.get("price_features", {}).get("return_window")
    amount = feature.get("volume_features", {}).get("amount")
    parts = [f"{ticker or '目标'} {bucket_size} 特征已生成，区间收益约 {ret:.2%}，成交额 {amount:.0f}。"]
    ratio = feature.get("volume_features", {}).get("amount_ratio_vs_20d_same_bucket")
    if ratio is not None:
        parts.append(f"成交额为过去同时间段均值 {ratio:.1f} 倍。")
    tradability = feature.get("tradability_features", {})
    if tradability.get("hit_limit_up"):
        parts.append("已触及涨停。")
    if tradability.get("hit_limit_down"):
        parts.append("已触及跌停。")
    return "".join(parts)


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
