"""HK Connect structured data adapter (V0.8).

Collects the structured southbound / HK-connect fields the research pool needs and that
text search cannot answer reliably: eligibility, southbound holding and its 1/5/10-day
changes, turnover and price. Default provider is AKShare (Eastmoney data underneath);
``akshare`` is imported lazily so the agent runs without it when HK collection is off.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .common import ToolResult

logger = logging.getLogger(__name__)

# Snapshot fields the research pool expects a *high-quality* snapshot to fill. Having a row
# is not the same as having data: completeness lets `eval hk-connect` tell the two apart
# (V0.8.1). buyback / AH premium / liquidity stay in the schema but are not counted until a
# data source actually feeds them, so completeness measures achievable fields only.
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

# Fields defined in the snapshot schema without a wired provider yet. Reported separately so
# the evaluation layer can see "no source" instead of mistaking them for collection failures.
HK_UNSOURCED_FIELDS = ("buyback_amount_hkd", "ah_premium_pct", "hk_liquidity_score")


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


def calc_ah_premium_pct(*, a_price_cny: float | None, h_price_hkd: float | None, cny_hkd: float | None) -> float | None:
    """A-share premium over H-share: A price converted to HKD vs H price, in percent."""
    if not a_price_cny or not h_price_hkd or not cny_hkd:
        return None
    return round((a_price_cny * cny_hkd / h_price_hkd - 1.0) * 100, 4)


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
    source_url: str = "https://data.eastmoney.com/hsgtcg/"


class HKConnectAdapter:
    tool_name = "hk_connect_collector"

    def __init__(self, provider: str = "akshare"):
        self.provider = provider

    def collect_snapshot(
        self,
        *,
        target_id: str | None,
        ticker: str,
        company_name: str | None = None,
        as_of: str | None = None,
    ) -> ToolResult:
        result = ToolResult(
            tool_name=self.tool_name,
            operation="hk_connect_daily_snapshot",
            request={"target_id": target_id, "ticker": ticker, "company_name": company_name, "as_of": as_of},
        )
        try:
            import akshare  # noqa: F401
        except ImportError:
            result.status = "failed"
            result.errors.append(
                {
                    "error_code": "AKSHARE_NOT_INSTALLED",
                    "error_message": "hk_connect_collector requires the akshare package (pip install akshare)",
                    "retryable": False,
                }
            )
            result.quality = {"usable": False}
            return result.finish()
        try:
            snapshot = self._collect_akshare(ticker=ticker, company_name=company_name, as_of=as_of)
            missing_fields = [name for name in HK_REQUIRED_FIELDS if getattr(snapshot, name) is None]
            filled = len(HK_REQUIRED_FIELDS) - len(missing_fields)
            result.status = "success"
            result.result = asdict(snapshot)
            result.quality = {
                "usable": True,
                "source": "eastmoney_via_akshare",
                "has_holding": snapshot.southbound_holding_shares is not None,
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
            }
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.errors.append(
                {"error_code": "HK_CONNECT_COLLECT_FAILED", "error_message": str(exc), "retryable": True}
            )
            result.quality = {"usable": False}
            logger.warning("hk_connect collect failed for %s: %s", ticker, exc)
        return result.finish()

    def _collect_akshare(self, *, ticker: str, company_name: str | None, as_of: str | None) -> HKConnectSnapshot:
        import akshare as ak

        code = _hk_code(ticker)
        date = _date_yyyymmdd(as_of)

        components = ak.stock_hk_ggt_components_em()
        component_rows = components.to_dict("records")
        comp = next(
            (r for r in component_rows if str(_pick(r, "代码", "股票代码", "code")).zfill(5) == code),
            None,
        )

        holding = ak.stock_hsgt_stock_statistics_em(symbol="南向持股", start_date=date, end_date=date)
        holding_rows = holding.to_dict("records")
        hrow = next(
            (r for r in holding_rows if str(_pick(r, "股票代码", "代码", "code")).zfill(5) == code),
            None,
        )

        return HKConnectSnapshot(
            ticker=f"{code}.HK",
            company_name=(
                company_name
                or _pick(comp or {}, "名称", "股票简称", "name")
                or _pick(hrow or {}, "股票简称", "名称", "name")
            ),
            as_of=str(as_of)[:10] if as_of else datetime.now(timezone.utc).date().isoformat(),
            hk_connect_eligible=comp is not None,
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
