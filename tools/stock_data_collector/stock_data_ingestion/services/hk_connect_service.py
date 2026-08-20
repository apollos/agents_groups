"""HK-connect southbound snapshot collection.

Collects the structured HK-connect fields research needs and text search cannot
answer reliably: connect eligibility, southbound holding and its 1/5/10-day
market-value changes, turnover and price. Data source is Eastmoney (via the
optional ``akshare`` package, with bounded direct-API fallbacks).

This lives in the tool (not in an agent) on purpose: agents orchestrate and
verify, tools collect. The tool owns the Eastmoney anti-scraping handling
(EASTMONEY_COOKIE from .env, browser-like headers, hard deadlines), so agents
only need to invoke ``fetch hk-connect`` and consume structured JSON.

Persistence note: snapshots are research-domain data consumed and stored by the
calling agent; this service returns structured results only.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Hard wall-clock deadlines for each network step. Per-request timeouts do not
# cover everything that can wedge: getaddrinfo on a stuck DNS proxy (observed on
# WSL 2026-08-19: datacenter-web.eastmoney.com resolution black-holed) hangs
# BEFORE any socket timeout applies, and akshare's own requests carry no timeout
# at all. Without these bounds one hung fetch blocks the caller indefinitely.
AKSHARE_DEADLINE_SECONDS = 60.0
DIRECT_FETCH_DEADLINE_SECONDS = 45.0

# Eastmoney's WAF drops bare python HTTP clients once an IP looks suspicious,
# but passes requests that carry browser-like headers plus previously issued
# (anonymous) cookies. akshare calls go through AKShareAdapter's cookie
# injection; the direct fallbacks below attach these headers themselves.
_EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}
_GGT_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_SOURCE_URL = "https://data.eastmoney.com/hsgtcg/"

COOKIE_ENV = "EASTMONEY_COOKIE"

# Snapshot fields a *high-quality* snapshot is expected to fill. Having a row is
# not the same as having data: completeness lets callers tell the two apart.
HK_REQUIRED_FIELDS = (
    "last_price_hkd",
    "turnover_hkd",
    "southbound_holding_shares",
    "southbound_holding_market_value_hkd",
    "southbound_holding_pct",
    "southbound_mv_change_1d",
    "southbound_mv_change_5d",
    "southbound_mv_change_10d",
)

# Fields defined in the snapshot schema without a wired provider yet. Reported
# separately so callers see "no source" instead of a collection failure.
HK_UNSOURCED_FIELDS = ("buyback_amount_hkd", "ah_premium_pct", "hk_liquidity_score")


def _with_deadline(fn: Callable[[], T], seconds: float, what: str) -> T:
    """Run ``fn`` with a hard deadline, raising TimeoutError when it is exceeded.

    Uses a daemon thread (not ThreadPoolExecutor: its atexit hook joins workers,
    so a truly wedged fetch would block interpreter shutdown). A timed-out
    thread leaks quietly until the hang resolves or the process exits.
    """
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True, name="hk-connect-fetch")
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"{what} did not finish within {seconds:.0f}s (likely DNS or WAF hang)")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _cookie() -> str:
    return os.environ.get(COOKIE_ENV, "").strip()


def _hk_code(ticker: str) -> str:
    return str(ticker).split(".")[0].zfill(5)


def _date_yyyymmdd(as_of: str | None) -> str:
    if not as_of:
        return datetime.now(timezone.utc).strftime("%Y%m%d")
    return str(as_of)[:10].replace("-", "")


def _pick(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _num(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_ggt_components_direct(cookie: str | None = None) -> list[dict[str, Any]]:
    """Fetch the HK-connect component list straight from the Eastmoney clist API.

    Same endpoint and params as ``ak.stock_hk_ggt_components_em``, but with
    browser-like headers, an optional browser cookie and a per-request timeout so
    the WAF does not reset (or black-hole) the connection. Returns rows keyed
    like the akshare frame (代码/名称/最新价/成交额) so field picking is uniform.
    """
    import requests

    headers = dict(_EASTMONEY_HEADERS)
    if cookie is None:
        cookie = _cookie()
    if cookie:
        headers["Cookie"] = cookie
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {
            "pn": str(page), "pz": "100", "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "fid": "f12",
            "fs": "b:DLMK0146,b:DLMK0144",
            "fields": "f2,f6,f12,f14",
        }
        resp = requests.get(_GGT_CLIST_URL, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        rows.extend(
            {"代码": r.get("f12"), "名称": r.get("f14"),
             "最新价": r.get("f2"), "成交额": r.get("f6")}
            for r in diff
        )
        total = int(data.get("total") or 0)
        if not diff or len(rows) >= total:
            break
        page += 1
    return rows


def _fetch_southbound_holding_direct(
    code: str, end_date: str, cookie: str | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """Latest southbound holding row for one stock from the Eastmoney datacenter API.

    Same report akshare's ``stock_hsgt_stock_statistics_em`` reads
    (RPT_MUTUAL_STOCK_HOLDRANKS), but filtered to the stock and sorted by trade
    date descending, so one bounded request returns the most recent published row
    <= ``end_date``. That also covers the "as_of not published yet" case: holding
    stats publish after settlement (late evening / T+1), so intraday queries for
    today legitimately have no data and the latest row is the previous session.

    Returns ``(row, holding_date)`` with akshare-style Chinese keys so downstream
    field picking is identical for both sources; ``(None, None)`` when the stock
    has no southbound holding rows at all (e.g. not connect-eligible).
    """
    import requests

    headers = dict(_EASTMONEY_HEADERS)
    headers["Referer"] = "https://data.eastmoney.com/hsgtcg/StockStatistics.aspx"
    if cookie is None:
        cookie = _cookie()
    if cookie:
        headers["Cookie"] = cookie
    day = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
    params = {
        "reportName": "RPT_MUTUAL_STOCK_HOLDRANKS",
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageSize": "1",
        "pageNumber": "1",
        "filter": f"(INTERVAL_TYPE=\"1\")(RN=1)(SECURITY_CODE=\"{code}\")(TRADE_DATE<='{day}')",
    }
    resp = requests.get(_DATACENTER_URL, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    data = (((resp.json() or {}).get("result") or {}).get("data")) or []
    if not data:
        return None, None
    raw = data[0]
    holding_date = str(raw.get("TRADE_DATE") or "")[:10].replace("-", "") or None
    row = {
        "股票代码": raw.get("SECURITY_CODE"),
        "股票简称": raw.get("SECURITY_NAME"),
        "当日收盘价": raw.get("CLOSE_PRICE"),
        "持股数量": raw.get("HOLD_SHARES"),
        "持股市值": raw.get("HOLD_MARKET_CAP"),
        "持股数量占发行股百分比": raw.get("HOLD_SHARES_RATIO"),
        "持股市值变化-1日": raw.get("HOLD_MARKETCAP_CHG1"),
        "持股市值变化-5日": raw.get("HOLD_MARKETCAP_CHG5"),
        "持股市值变化-10日": raw.get("HOLD_MARKETCAP_CHG10"),
    }
    return row, holding_date


@dataclass
class HKConnectSnapshot:
    ticker: str
    company_name: str | None
    as_of: str
    hk_connect_eligible: bool
    last_price_hkd: float | None = None
    turnover_hkd: float | None = None
    southbound_holding_shares: float | None = None
    southbound_holding_market_value_hkd: float | None = None
    southbound_holding_pct: float | None = None
    southbound_mv_change_1d: float | None = None
    southbound_mv_change_5d: float | None = None
    southbound_mv_change_10d: float | None = None
    buyback_amount_hkd: float | None = None
    ah_premium_pct: float | None = None
    hk_liquidity_score: float | None = None
    source_url: str = _SOURCE_URL


class HKConnectCollector:
    """Collect HK-connect snapshots for one or more tickers.

    akshare calls run through :class:`AKShareAdapter`'s Eastmoney cookie
    injection (browser headers + EASTMONEY_COOKIE from .env), bounded by hard
    deadlines; on failure each step falls back to a bounded direct Eastmoney
    API call.
    """

    provider = "eastmoney_via_akshare"

    def __init__(self) -> None:
        from stock_data_ingestion.adapters.akshare_adapter import AKShareAdapter

        self._ak_adapter = AKShareAdapter()
        self._holding_date: str | None = None

    # -- component list (shared by all tickers in one invocation) -----------
    def _load_components(self) -> list[dict[str, Any]]:
        import akshare as ak

        try:
            components = _with_deadline(
                lambda: self._ak_adapter._call_ak(  # noqa: SLF001 - shared cookie/retry machinery
                    ak.stock_hk_ggt_components_em, retry_on_transient=False
                ),
                AKSHARE_DEADLINE_SECONDS,
                "akshare ggt components",
            )
            return components.to_dict("records")
        except Exception as exc:  # noqa: BLE001 - WAF resets surface as several types
            logger.info(
                "akshare ggt components failed (%s); falling back to direct Eastmoney fetch",
                exc,
            )
            return _with_deadline(
                _fetch_ggt_components_direct,
                DIRECT_FETCH_DEADLINE_SECONDS * 2,  # paged: ~7 requests for ~600 stocks
                "direct ggt components fetch",
            )

    # -- southbound holding --------------------------------------------------
    def _load_holding(self, code: str, date: str) -> dict[str, Any] | None:
        """Southbound holding row for one stock; sets ``self._holding_date``.

        akshare for the as_of date first; when that fails — data not published
        yet (stats appear after settlement, late evening or T+1, so akshare
        crashes on the empty payload), WAF reset, or DNS hang — one direct
        request returns the latest published row <= as_of instead.
        """
        import akshare as ak

        self._holding_date = None
        try:
            holding = _with_deadline(
                lambda: self._ak_adapter._call_ak(  # noqa: SLF001
                    ak.stock_hsgt_stock_statistics_em,
                    retry_on_transient=False,
                    symbol="南向持股",
                    start_date=date,
                    end_date=date,
                ),
                AKSHARE_DEADLINE_SECONDS,
                "akshare southbound holding",
            )
            rows = holding.to_dict("records")
            if not rows:
                # An empty full-market frame means the date has no published stats
                # (weekend/holiday or pre-publication); the direct fallback returns
                # the latest published row <= as_of instead.
                raise ValueError(f"no southbound holding stats published for {date}")
            # Data for the date exists; a missing row here means the stock simply
            # has no southbound holding (do not fall back to an older session).
            self._holding_date = date
            return next(
                (r for r in rows if str(_pick(r, "股票代码", "代码", "code")).zfill(5) == code),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "akshare southbound holding for %s failed (%s); falling back to direct fetch",
                date,
                exc,
            )
            hrow, self._holding_date = _with_deadline(
                lambda: _fetch_southbound_holding_direct(code, date),
                DIRECT_FETCH_DEADLINE_SECONDS,
                "direct southbound holding fetch",
            )
            return hrow

    # -- one snapshot ---------------------------------------------------------
    def collect_one(
        self,
        *,
        ticker: str,
        component_rows: list[dict[str, Any]] | None,
        company_name: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """One snapshot; ``component_rows=None`` means the component list is unavailable.

        The component list and the holding stats live on different Eastmoney
        clusters (push2 vs datacenter-web) that get blocked independently, so a
        degraded snapshot from holding stats alone is still worth returning:
        southbound holding implies connect eligibility, and the direct holding
        row carries the close price. Only turnover is component-only.
        """
        code = _hk_code(ticker)
        date = _date_yyyymmdd(as_of)
        comp = next(
            (
                r
                for r in (component_rows or [])
                if str(_pick(r, "代码", "股票代码", "code")).zfill(5) == code
            ),
            None,
        )
        hrow = self._load_holding(code, date)
        if component_rows is None and hrow is None:
            raise RuntimeError(
                "component list unavailable and no southbound holding row found: "
                "nothing usable to snapshot"
            )

        snapshot = HKConnectSnapshot(
            ticker=f"{code}.HK",
            company_name=(
                company_name
                or _pick(comp or {}, "名称", "股票简称", "name")
                or _pick(hrow or {}, "股票简称", "名称", "name")
            ),
            as_of=str(as_of)[:10] if as_of else datetime.now(timezone.utc).date().isoformat(),
            # Only connect-eligible stocks can be held southbound, so a holding row
            # is a sound eligibility signal when the component list is unreachable.
            hk_connect_eligible=comp is not None if component_rows is not None else True,
            last_price_hkd=_num(_pick(comp or hrow or {}, "最新价", "当日收盘价", "收盘价")),
            turnover_hkd=_num(_pick(comp or {}, "成交额")),
            southbound_holding_shares=_num(_pick(hrow or {}, "持股数量")),
            southbound_holding_market_value_hkd=_num(_pick(hrow or {}, "持股市值")),
            southbound_holding_pct=_num(
                _pick(hrow or {}, "持股数量占发行股百分比", "持股占比", "占发行股百分比")
            ),
            southbound_mv_change_1d=_num(_pick(hrow or {}, "持股市值变化-1日")),
            southbound_mv_change_5d=_num(_pick(hrow or {}, "持股市值变化-5日")),
            southbound_mv_change_10d=_num(_pick(hrow or {}, "持股市值变化-10日")),
        )
        missing_fields = [name for name in HK_REQUIRED_FIELDS if getattr(snapshot, name) is None]
        filled = len(HK_REQUIRED_FIELDS) - len(missing_fields)
        return {
            **asdict(snapshot),
            "quality": {
                "usable": True,
                "source": self.provider,
                "components_available": component_rows is not None,
                "has_holding": snapshot.southbound_holding_shares is not None,
                "holding_data_date": self._holding_date,
                "hk_connect_eligible": snapshot.hk_connect_eligible,
                "missing_fields": missing_fields,
                "unsourced_fields": [
                    name for name in HK_UNSOURCED_FIELDS if getattr(snapshot, name) is None
                ],
                "field_completeness": {
                    "required_count": len(HK_REQUIRED_FIELDS),
                    "filled_count": filled,
                    "ratio": round(filled / len(HK_REQUIRED_FIELDS), 4),
                },
            },
            "errors": [],
        }


def _cookie_hint() -> str:
    if _cookie():
        return (
            f"{COOKIE_ENV} may have expired: refresh the Cookie from a browser session on "
            "quote.eastmoney.com into the tool .env, or wait for the Eastmoney rate-limit "
            "to cool down"
        )
    return (
        f"{COOKIE_ENV} is not configured: copy the Cookie header from a browser session on "
        "quote.eastmoney.com into the stock_data_collector .env "
        "(anonymous cookie is enough, no login required)"
    )


def collect_hk_connect_snapshots(
    tickers: list[str], *, as_of: str | None = None
) -> dict[str, Any]:
    """Collect snapshots for ``tickers``; returns the CLI response payload.

    Per-ticker failures are isolated: one blocked ticker does not fail the
    batch. ``status`` aggregates to success / partial_success / failed.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    response: dict[str, Any] = {
        "status": "failed",
        "request_type": "hk_connect_snapshot",
        "as_of": str(as_of)[:10] if as_of else datetime.now(timezone.utc).date().isoformat(),
        "provider": HKConnectCollector.provider,
        "started_at": started_at,
        "data": {"hk_connect_snapshots": []},
        "errors": [],
    }
    try:
        import akshare  # noqa: F401
    except ImportError:
        response["errors"].append(
            {
                "error_code": "AKSHARE_NOT_INSTALLED",
                "error_message": "fetch hk-connect requires the akshare package (pip install akshare)",
                "retryable": False,
            }
        )
        response["completed_at"] = datetime.now(timezone.utc).isoformat()
        return response

    collector = HKConnectCollector()
    # Component-list failure is degraded, not fatal: the push2 quotes cluster gets
    # WAF-blocked independently of datacenter-web, and holding stats alone still
    # make a usable snapshot (eligibility inferred, close price included; only
    # turnover is component-only and lands in missing_fields).
    component_rows: list[dict[str, Any]] | None
    try:
        component_rows = collector._load_components()  # noqa: SLF001 - module-level orchestration
    except Exception as exc:  # noqa: BLE001
        component_rows = None
        response["warnings"] = [
            {
                "warning_code": "HK_CONNECT_COMPONENTS_UNAVAILABLE",
                "warning_message": f"component list unavailable, degrading to holding-only: {exc}",
                "suggested_action": _cookie_hint(),
            }
        ]
        logger.warning("hk_connect component list unavailable, degrading: %s", exc)

    snapshots: list[dict[str, Any]] = []
    failed = 0
    for ticker in tickers:
        try:
            snapshots.append(
                collector.collect_one(ticker=ticker, component_rows=component_rows, as_of=as_of)
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            error = {
                "error_code": "HK_CONNECT_COLLECT_FAILED",
                "error_message": str(exc),
                "retryable": True,
                "suggested_action": _cookie_hint(),
                "ticker": ticker,
            }
            response["errors"].append(error)
            snapshots.append(
                {
                    "ticker": f"{_hk_code(ticker)}.HK",
                    "as_of": response["as_of"],
                    "quality": {"usable": False, "cookie_configured": bool(_cookie())},
                    "errors": [error],
                }
            )
            logger.warning("hk_connect collect failed for %s: %s", ticker, exc)
    response["data"]["hk_connect_snapshots"] = snapshots
    if failed == 0:
        response["status"] = "success"
    elif failed < len(tickers):
        response["status"] = "partial_success"
    response["completed_at"] = datetime.now(timezone.utc).isoformat()
    return response
