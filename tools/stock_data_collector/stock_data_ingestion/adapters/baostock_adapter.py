from __future__ import annotations

import contextlib
import importlib.util
import io
import math
import re
from datetime import date, datetime
from typing import Any, Iterable

from stock_data_ingestion.adapters.base import BaseDataAdapter
from stock_data_ingestion.normalization.datetime_utils import now_asia_shanghai, normalize_trade_date
from stock_data_ingestion.normalization.ticker import TickerNormalizationError, normalize_ticker, to_baostock_symbol
from stock_data_ingestion.schemas.errors import ErrorCode
from stock_data_ingestion.schemas.records import AdapterFetchStatus, ProviderFetchResult
from stock_data_ingestion.schemas.requests import StockDataRequest


class BaoStockAdapter(BaseDataAdapter):
    """BaoStock provider adapter for documented Python API v0.9.x.

    BaoStock is used as an A-share supplement/validator. The uploaded BaoStock
    manual documents stock K-line, daily valuation fields, adjustment factors,
    quarterly financial metrics, security basic data, trade dates, all-stock
    trading status, industry classification, major index constituents and
    dividend data. It does not document HK daily bars or money-flow data, so
    those are intentionally not implemented here.
    """

    provider_name = "baostock"
    source_site = "baostock"
    adapter_version = "0.1.0"

    _DAILY_BAR_FIELDS = (
        "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,"
        "tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
    )
    _WEEKLY_MONTHLY_FIELDS = "date,code,open,high,low,close,volume,amount,adjustflag,turn,pctChg"
    _VALUATION_FIELDS = "date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM"

    _SUPPORTED_EXCHANGES = {"SH", "SZ"}
    _FINANCIAL_ENDPOINTS = {
        "profit": "query_profit_data",
        "operation": "query_operation_data",
        "growth": "query_growth_data",
        "balance": "query_balance_data",
        "cash_flow": "query_cash_flow_data",
        "dupont": "query_dupont_data",
    }

    def __init__(self) -> None:
        super().__init__()
        self._bs: Any | None = None

    def is_available(self) -> bool:
        return importlib.util.find_spec("baostock") is not None

    def authenticate(self) -> bool:
        if importlib.util.find_spec("baostock") is None:
            return False
        import baostock as bs  # type: ignore

        # BaoStock's SDK prints "login success!" to stdout; redirect it so the CLI
        # stdout stays a clean single JSON document (see StockDataResponse contract).
        with contextlib.redirect_stdout(io.StringIO()):
            lg = bs.login()
        if getattr(lg, "error_code", "0") != "0":
            return False
        self._bs = bs
        self._authenticated = True
        return True

    def close(self) -> None:
        if self._bs is None or not self._authenticated:
            return
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self._bs.logout()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._authenticated = False

    def _ensure_ready(self, source_api: str) -> ProviderFetchResult | None:
        if not self.is_available():
            return self._unavailable_result(source_api, ErrorCode.PROVIDER_UNAVAILABLE, "baostock SDK is not installed")
        if self._bs is None or not self._authenticated:
            if not self.authenticate():
                return self._unavailable_result(source_api, ErrorCode.AUTH_FAILED, "BaoStock login failed")
        return None

    def _classify_error(self, exc: Exception) -> ErrorCode:
        msg = str(exc).lower()
        if "login" in msg or "登录" in msg:
            return ErrorCode.AUTH_FAILED
        if "timeout" in msg or "timed out" in msg:
            return ErrorCode.PROVIDER_TIMEOUT
        if "schema" in msg or "columns" in msg or "字段" in msg:
            return ErrorCode.PROVIDER_SCHEMA_CHANGED
        return ErrorCode.UNKNOWN_ERROR

    @staticmethod
    def _is_missing(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, float) and math.isnan(value):
            return True
        return str(value).strip() in {"", "--", "-", "—", "nan", "NaN", "None", "null", "NULL"}

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if cls._is_missing(value):
            return None
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:  # noqa: BLE001
                pass
        return value

    @classmethod
    def _clean_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        return {str(k): cls._json_safe(v) for k, v in row.items()}

    def _result_to_records(self, result: Any, source_api: str) -> list[dict[str, Any]]:
        error_code = str(getattr(result, "error_code", "0"))
        if error_code != "0":
            raise RuntimeError(f"BaoStock {source_api} failed: {error_code} {getattr(result, 'error_msg', '')}")
        # Prefer the row-by-row cursor API. BaoStock's ResultData.get_data() concatenates
        # paginated results via the removed pandas.DataFrame.append, which raises
        # "'DataFrame' object has no attribute 'append'" under pandas>=2.0 for multi-page
        # endpoints such as query_all_stock. next()/get_row_data() paginate safely.
        if hasattr(result, "next") and hasattr(result, "get_row_data"):
            rows: list[dict[str, Any]] = []
            fields = list(getattr(result, "fields", []) or [])
            while result.next():
                rows.append(self._clean_row(dict(zip(fields, result.get_row_data()))))
            return rows
        if hasattr(result, "get_data"):
            df = result.get_data()
            if df is None or bool(getattr(df, "empty", False)):
                return []
            return [self._clean_row(dict(row)) for row in df.to_dict(orient="records")]
        return []

    @staticmethod
    def _date_to_baostock(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value.isoformat()
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{8}", text):
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        return normalize_trade_date(text).isoformat()

    def _request_start_end(self, request: StockDataRequest) -> tuple[str | None, str | None]:
        return self._date_to_baostock(request.start_date), self._date_to_baostock(request.end_date)

    def _ticker_to_baostock(self, ticker: str) -> str | None:
        normalized = normalize_ticker(ticker)
        exchange = normalized.split(".")[1]
        if exchange not in self._SUPPORTED_EXCHANGES:
            return None
        try:
            return to_baostock_symbol(normalized)
        except TickerNormalizationError:
            return None

    def _decorate_identity(self, row: dict[str, Any], provider_symbol: str | None = None) -> dict[str, Any]:
        symbol = provider_symbol or str(row.get("code") or row.get("provider_symbol") or "")
        normalized = normalize_ticker(symbol)
        code, exchange = normalized.split(".")
        return {
            **row,
            "provider_symbol": symbol,
            "normalized_ticker": normalized,
            "symbol": code,
            "exchange": exchange,
            "market": "A_share",
            "asset_type": "stock",
            "currency": "CNY",
        }

    def _date_range(self, request: StockDataRequest) -> Iterable[date]:
        start = request.start_date or request.end_date or now_asia_shanghai().date()
        end = request.end_date or start
        day = start
        while day <= end:
            yield day
            day = date.fromordinal(day.toordinal() + 1)

    def _quarters_for_request(self, request: StockDataRequest) -> list[tuple[int, int]]:
        extra = request.extra_params or {}
        if extra.get("year") and extra.get("quarter"):
            return [(int(extra["year"]), int(extra["quarter"]))]
        if extra.get("period"):
            period = str(extra["period"]).replace("-", "")[:8]
            month = int(period[4:6])
            return [(int(period[:4]), {3: 1, 6: 2, 9: 3, 12: 4}.get(month, ((month - 1) // 3) + 1))]

        end = request.end_date or now_asia_shanghai().date()
        start = request.start_date
        if start is None:
            lookback = int(extra.get("financial_lookback_quarters", 8))
            year = end.year
            quarter = ((end.month - 1) // 3) + 1
            quarters: list[tuple[int, int]] = []
            for _ in range(lookback):
                quarters.append((year, quarter))
                quarter -= 1
                if quarter == 0:
                    quarter = 4
                    year -= 1
            return list(reversed(quarters))

        def yq(day: date) -> tuple[int, int]:
            return day.year, ((day.month - 1) // 3) + 1

        quarters = []
        year, quarter = yq(start)
        end_y, end_q = yq(end)
        while (year, quarter) <= (end_y, end_q):
            quarters.append((year, quarter))
            quarter += 1
            if quarter == 5:
                quarter = 1
                year += 1
        return quarters

    # ------------------------------------------------------------------
    # Security master / calendar / trading status
    # ------------------------------------------------------------------
    def fetch_security_master(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_stock_basic"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            industry_by_code: dict[str, dict[str, Any]] = {}
            try:
                industry_rs = self._bs.query_stock_industry()
                for row in self._result_to_records(industry_rs, "query_stock_industry"):
                    industry_by_code[str(row.get("code") or "")] = row
            except Exception:  # noqa: BLE001
                industry_by_code = {}

            requested = set(request.tickers or [])
            rows: list[dict[str, Any]] = []
            if requested:
                provider_symbols = [self._ticker_to_baostock(ticker) for ticker in requested]
                provider_symbols = [symbol for symbol in provider_symbols if symbol]
                for symbol in provider_symbols:
                    rs = self._bs.query_stock_basic(code=symbol)
                    rows.extend(self._result_to_records(rs, source_api))
            else:
                rs = self._bs.query_stock_basic()
                rows = self._result_to_records(rs, source_api)

            records: list[dict[str, Any]] = []
            for row in rows:
                security_type = str(row.get("type") or "").strip()
                status = str(row.get("status") or "").strip()
                if security_type and security_type != "1":
                    continue
                if status and status != "1":
                    continue
                code = str(row.get("code") or "")
                if not code.startswith(("sh.", "sz.")):
                    continue
                try:
                    decorated = self._decorate_identity(dict(row), code)
                except Exception:  # noqa: BLE001
                    continue
                ind = industry_by_code.get(code) or {}
                decorated.update(
                    {
                        "name": row.get("code_name"),
                        "list_date": row.get("ipoDate"),
                        "delist_date": row.get("outDate"),
                        "list_status": "L" if status == "1" else "D",
                        "industry": ind.get("industry"),
                        "industry_system": ind.get("industryClassification"),
                        "industry_update_date": ind.get("updateDate"),
                    }
                )
                records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_trade_calendar(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_trade_dates"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            start, end = self._request_start_end(request)
            rs = self._bs.query_trade_dates(start_date=start, end_date=end)
            base_rows = self._result_to_records(rs, source_api)
            exchanges = request.exchanges or ["SSE", "SZSE", "BSE"]
            records: list[dict[str, Any]] = []
            for exchange in exchanges:
                for row in base_rows:
                    records.append(
                        {
                            **row,
                            "exchange": str(exchange).upper(),
                            "calendar_date": row.get("calendar_date"),
                            "is_open": row.get("is_trading_day"),
                            "calendar_source": "baostock:query_trade_dates",
                            "calendar_derivation": "common_a_share_calendar_used_for_exchange",
                        }
                    )
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_trading_status(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_all_stock"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            wanted = set(request.tickers or [])
            records: list[dict[str, Any]] = []
            for day in self._date_range(request):
                day_text = day.isoformat()
                rs = self._bs.query_all_stock(day=day_text)
                for row in self._result_to_records(rs, source_api):
                    code = str(row.get("code") or "")
                    if not code.startswith(("sh.", "sz.")):
                        continue
                    try:
                        decorated = self._decorate_identity(dict(row), code)
                    except Exception:  # noqa: BLE001
                        continue
                    if wanted and decorated["normalized_ticker"] not in wanted:
                        continue
                    is_trading = str(row.get("tradeStatus") or "") == "1"
                    decorated.update(
                        {
                            "trade_date": day_text,
                            "is_trading": is_trading,
                            "is_suspended": not is_trading,
                            "tradability_status": "tradable" if is_trading else "suspended",
                            "not_tradable_reason": None if is_trading else "suspended_or_not_trading",
                            "source_methodology": "baostock:query_all_stock.tradeStatus",
                        }
                    )
                    records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def _frequency(self, request: StockDataRequest) -> str:
        return {
            "1d": "d",
            "1w": "w",
            "1mo": "m",
            "5m": "5",
            "15m": "15",
            "30m": "30",
            "60m": "60",
        }.get(str(request.frequency or "1d"), "d")

    def _adjustflag(self, request: StockDataRequest) -> str:
        return {"none": "3", "qfq": "2", "hfq": "1"}.get(str(request.adjust or "none"), "3")

    def fetch_historical_bars(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_history_k_data_plus"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            records: list[dict[str, Any]] = []
            start, end = self._request_start_end(request)
            frequency = self._frequency(request)
            fields = self._DAILY_BAR_FIELDS if frequency == "d" else self._WEEKLY_MONTHLY_FIELDS
            for ticker in request.tickers:
                provider_symbol = self._ticker_to_baostock(ticker)
                if provider_symbol is None:
                    continue
                rs = self._bs.query_history_k_data_plus(
                    provider_symbol,
                    fields,
                    start_date=start,
                    end_date=end,
                    frequency=frequency,
                    adjustflag=self._adjustflag(request),
                )
                for row in self._result_to_records(rs, source_api):
                    try:
                        decorated = self._decorate_identity(dict(row), provider_symbol)
                    except Exception:  # noqa: BLE001
                        continue
                    decorated.update(
                        {
                            "trade_date": row.get("date"),
                            "pre_close": row.get("preclose"),
                            "pct_change": row.get("pctChg"),
                            "turnover_rate": row.get("turn"),
                            "is_trading": str(row.get("tradestatus") or "1") == "1",
                            "is_suspended": str(row.get("tradestatus") or "1") == "0",
                            "is_st": str(row.get("isST") or "0") == "1",
                            "adjust": str(request.adjust or "none"),
                            "frequency": str(request.frequency or "1d"),
                            "volume_unit": "share",
                            "amount_unit": "CNY",
                            "raw_adjustflag": row.get("adjustflag"),
                            "raw_source_api": source_api,
                        }
                    )
                    records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_valuation_metric(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_history_k_data_plus.valuation"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready("query_history_k_data_plus")
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            records: list[dict[str, Any]] = []
            start, end = self._request_start_end(request)
            for ticker in request.tickers:
                provider_symbol = self._ticker_to_baostock(ticker)
                if provider_symbol is None:
                    continue
                rs = self._bs.query_history_k_data_plus(
                    provider_symbol,
                    self._VALUATION_FIELDS,
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="3",
                )
                for row in self._result_to_records(rs, source_api):
                    decorated = self._decorate_identity(dict(row), provider_symbol)
                    decorated.update(
                        {
                            "trade_date": row.get("date"),
                            "pe_ttm": row.get("peTTM"),
                            "pb": row.get("pbMRQ"),
                            "ps_ttm": row.get("psTTM"),
                            "pcf_ncf_ttm": row.get("pcfNcfTTM"),
                            "raw_source_api": "query_history_k_data_plus",
                        }
                    )
                    records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_adj_factor(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_adjust_factor"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            records: list[dict[str, Any]] = []
            start, end = self._request_start_end(request)
            for ticker in request.tickers:
                provider_symbol = self._ticker_to_baostock(ticker)
                if provider_symbol is None:
                    continue
                rs = self._bs.query_adjust_factor(code=provider_symbol, start_date=start, end_date=end)
                for row in self._result_to_records(rs, source_api):
                    decorated = self._decorate_identity(dict(row), provider_symbol)
                    decorated.update(
                        {
                            "trade_date": row.get("dividOperateDate"),
                            "factor_event_date": row.get("dividOperateDate"),
                            # BaoStock exposes event-based fore/back factors under
                            # a different method from Tushare daily adj_factor. Keep
                            # the method-specific columns separate to avoid false
                            # conflicts on the generic adj_factor field.
                            "adj_factor": None,
                            "fore_adjust_factor": row.get("foreAdjustFactor"),
                            "back_adjust_factor": row.get("backAdjustFactor"),
                            "event_adjust_factor": row.get("adjustFactor"),
                            "factor_method": "baostock_pct_change_adjustment_factor",
                        }
                    )
                    records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    # ------------------------------------------------------------------
    # Financial data
    # ------------------------------------------------------------------
    def _merge_quarterly_financial_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("code") or ""), str(row.get("pubDate") or ""), str(row.get("statDate") or ""))
            existing = merged.setdefault(key, {})
            existing.update({k: v for k, v in row.items() if not self._is_missing(v)})
        return list(merged.values())

    def _financial_endpoint_names(self, request: StockDataRequest) -> list[str]:
        raw = (request.extra_params or {}).get("baostock_financial_endpoints") or list(self._FINANCIAL_ENDPOINTS)
        if isinstance(raw, str):
            names = [part.strip() for part in raw.split(",") if part.strip()]
        else:
            names = [str(part).strip() for part in raw if str(part).strip()]
        return [name for name in names if name in self._FINANCIAL_ENDPOINTS]

    def fetch_financial_indicator(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "quarterly_financial_indicators"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            records: list[dict[str, Any]] = []
            quarters = self._quarters_for_request(request)
            endpoint_names = self._financial_endpoint_names(request)
            for ticker in request.tickers:
                provider_symbol = self._ticker_to_baostock(ticker)
                if provider_symbol is None:
                    continue
                per_ticker_rows: list[dict[str, Any]] = []
                for year, quarter in quarters:
                    for endpoint_name in endpoint_names:
                        func_name = self._FINANCIAL_ENDPOINTS[endpoint_name]
                        func = getattr(self._bs, func_name)
                        rs = func(code=provider_symbol, year=year, quarter=quarter)
                        for row in self._result_to_records(rs, func_name):
                            row = dict(row)
                            row["raw_source_api"] = func_name
                            row["financial_endpoint"] = endpoint_name
                            row["query_year"] = year
                            row["query_quarter"] = quarter
                            per_ticker_rows.append(row)
                for row in self._merge_quarterly_financial_rows(per_ticker_rows):
                    decorated = self._decorate_identity(row, provider_symbol)
                    decorated.update(
                        {
                            "report_period": str(row.get("statDate") or "").replace("-", ""),
                            "report_date": row.get("statDate"),
                            "announcement_date": row.get("pubDate"),
                            "roe": row.get("roeAvg") or row.get("dupontROE"),
                            "gross_margin": row.get("gpMargin"),
                            "net_margin": row.get("npMargin") or row.get("dupontNitogr"),
                            "net_profit_yoy": row.get("YOYNI") or row.get("YOYPNI"),
                            "debt_asset_ratio": row.get("liabilityToAsset"),
                            "current_ratio": row.get("currentRatio"),
                            "ocf_to_net_profit": row.get("CFOToNP"),
                            "eps": row.get("epsTTM"),
                            "total_share": row.get("totalShare"),
                            "float_share": row.get("liqaShare"),
                            "source_methodology": "baostock:quarterly_financial_indicator_bundle",
                        }
                    )
                    records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_financial_statement(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_profit_data_as_statement"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                provider_symbol = self._ticker_to_baostock(ticker)
                if provider_symbol is None:
                    continue
                for year, quarter in self._quarters_for_request(request):
                    rs = self._bs.query_profit_data(code=provider_symbol, year=year, quarter=quarter)
                    for row in self._result_to_records(rs, "query_profit_data"):
                        decorated = self._decorate_identity(dict(row), provider_symbol)
                        decorated.update(
                            {
                                "report_period": str(row.get("statDate") or "").replace("-", ""),
                                "report_date": row.get("statDate"),
                                "announcement_date": row.get("pubDate"),
                                "statement_type": "income_statement_proxy",
                                "report_type": "quarterly",
                                "operating_revenue": row.get("MBRevenue"),
                                "net_profit": row.get("netProfit"),
                                "raw_source_api": "query_profit_data",
                                "source_methodology": "baostock profitability table as statement proxy",
                            }
                        )
                        records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    # ------------------------------------------------------------------
    # Industry / index / corporate action
    # ------------------------------------------------------------------
    def fetch_industry_membership(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_stock_industry"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            wanted = set(request.tickers or [])
            rs = self._bs.query_stock_industry(date=self._date_to_baostock(request.end_date) if request.end_date else None)
            records: list[dict[str, Any]] = []
            for row in self._result_to_records(rs, source_api):
                code = str(row.get("code") or "")
                if not code.startswith(("sh.", "sz.")):
                    continue
                try:
                    decorated = self._decorate_identity(dict(row), code)
                except Exception:  # noqa: BLE001
                    continue
                if wanted and decorated["normalized_ticker"] not in wanted:
                    continue
                decorated.update(
                    {
                        "industry_system": row.get("industryClassification") or "baostock",
                        "industry_name": row.get("industry"),
                        "effective_date": row.get("updateDate"),
                        "source_methodology": "baostock:query_stock_industry",
                    }
                )
                records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_index_data(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "index_data"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            records: list[dict[str, Any]] = []
            index_code = str((request.extra_params or {}).get("index_code") or "").lower()
            constituent_func_name = {
                "sz50": "query_sz50_stocks",
                "sh.000016": "query_sz50_stocks",
                "hs300": "query_hs300_stocks",
                "sh.000300": "query_hs300_stocks",
                "zz500": "query_zz500_stocks",
                "sh.000905": "query_zz500_stocks",
            }.get(index_code)
            if constituent_func_name:
                func = getattr(self._bs, constituent_func_name)
                rs = func(date=self._date_to_baostock(request.end_date) if request.end_date else None)
                for row in self._result_to_records(rs, constituent_func_name):
                    code = str(row.get("code") or "")
                    decorated = self._decorate_identity(dict(row), code)
                    decorated.update(
                        {
                            "index_code": index_code,
                            "index_name": {"query_sz50_stocks": "上证50", "query_hs300_stocks": "沪深300", "query_zz500_stocks": "中证500"}[constituent_func_name],
                            "effective_date": row.get("updateDate") or request.end_date or request.start_date,
                            "raw_source_api": constituent_func_name,
                        }
                    )
                    records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_corporate_action(self, request: StockDataRequest) -> ProviderFetchResult:
        source_api = "query_dividend_data"
        started = now_asia_shanghai()
        unavailable = self._ensure_ready(source_api)
        if unavailable is not None:
            return unavailable
        try:
            assert self._bs is not None
            years: set[int] = set()
            start = request.start_date or date(now_asia_shanghai().year - 1, 1, 1)
            end = request.end_date or now_asia_shanghai().date()
            for year in range(start.year, end.year + 1):
                years.add(year)
            records: list[dict[str, Any]] = []
            for ticker in request.tickers:
                provider_symbol = self._ticker_to_baostock(ticker)
                if provider_symbol is None:
                    continue
                for year in sorted(years):
                    rs = self._bs.query_dividend_data(code=provider_symbol, year=str(year), yearType="operate")
                    for row in self._result_to_records(rs, source_api):
                        op_date = row.get("dividOperateDate")
                        if op_date:
                            try:
                                op_day = normalize_trade_date(op_date)
                                if op_day < start or op_day > end:
                                    continue
                            except Exception:  # noqa: BLE001
                                pass
                        decorated = self._decorate_identity(dict(row), provider_symbol)
                        decorated.update(
                            {
                                "action_type": "dividend",
                                "announcement_date": row.get("dividPlanAnnounceDate") or row.get("dividPreNoticeDate"),
                                "record_date": row.get("dividRegistDate"),
                                "ex_date": row.get("dividOperateDate"),
                                "dividend_payment_date": row.get("dividPayDate"),
                                "cash_dividend_per_share": row.get("dividCashPsBeforeTax"),
                                "stock_bonus_ratio": row.get("dividStocksPs"),
                                "raw_source_api": source_api,
                            }
                        )
                        records.append(decorated)
            return self._success_result(source_api, records, started)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(source_api, started, exc, self._classify_error(exc), retryable=True)

    def fetch_money_flow(self, request: StockDataRequest) -> ProviderFetchResult:
        return ProviderFetchResult(
            provider=self.provider_name,
            source_api="money_flow",
            source_site=self.source_site,
            adapter_version=self.adapter_version,
            status=AdapterFetchStatus.empty_result,
            raw_records=[],
            rows_fetched=0,
            started_at=now_asia_shanghai(),
            completed_at=now_asia_shanghai(),
            error=None,
        )

    def normalize_raw_data(self, result: ProviderFetchResult, request: StockDataRequest) -> list[Any]:
        return result.raw_records

    def map_provider_symbol_to_normalized_ticker(self, symbol: str) -> str:
        return normalize_ticker(symbol)

    def map_normalized_ticker_to_provider_symbol(self, ticker: str) -> str:
        symbol = self._ticker_to_baostock(ticker)
        if symbol is None:
            raise TickerNormalizationError(f"BaoStock does not support ticker {ticker!r}")
        return symbol
